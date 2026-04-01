"""Research Agent orchestrator - minimal LLM-based research coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import google.generativeai as genai

from tools import ToolRegistry, build_default_tool_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OrchestratorConfig:
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    gemini_timeout_sec: int = 60


class ResearchOrchestrator:
    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()
        self.tool_registry = build_default_tool_registry()
        self.model = genai.GenerativeModel(model_name=self.config.gemini_model)
        self.max_tool_turns = 2

    async def answer(
        self,
        topic: str,
        source: str = "auto",
        timeout_sec: int = 60,
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Research answer with tool-calling support.
        Continues research for at least timeout_sec seconds.
        Returns: (answer_text, decision_log)
        """
        await asyncio.sleep(0)

        # 60秒以上は「時間指定あり」の深掘りモードとして扱う。
        explicit_timed_research = timeout_sec >= 60
        if explicit_timed_research:
            # Timed research: allow enough turns to actually consume the requested budget.
            # Approximate one useful turn at ~6-10 seconds including tool/network latency.
            self.max_tool_turns = max(8, min(24, timeout_sec // 6))
        else:
            # Default behavior: return quickly when answer quality is sufficient.
            self.max_tool_turns = 2

        start_time = time.time()
        turn = 0
        scratchpad: list[str] = []
        decision_log: list[dict[str, Any]] = []
        tool_history: list[str] = []
        question = f"topic: {topic}\nsource: {source}"
        now_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")

        final_candidate_response = ""

        for turn in range(1, self.max_tool_turns + 1):
            elapsed = time.time() - start_time
            if explicit_timed_research and elapsed >= timeout_sec and turn > 1:
                logger.info(
                    "Timed research budget reached at turn=%d elapsed=%.1f/%d",
                    turn,
                    elapsed,
                    timeout_sec,
                )
                break

            now_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
            question = f"topic: {topic}\nsource: {source}"

            prompt = self._build_thinking_prompt(
                question=question,
                turn=turn,
                scratchpad=scratchpad,
                now_jst=now_jst,
            )

            decision = await self._make_decision(prompt)
            decision_log.append(
                {
                    "turn": turn,
                    "action": decision.get("action"),
                    "tool": decision.get("tool"),
                    "reason": decision.get("reason"),
                }
            )

            # Early respond: only if at max turns, ignore early completion otherwise
            if decision.get("action") == "respond":
                response_text = str(decision.get("response", "")).strip()
                final_candidate_response = response_text
                # Timed research must keep gathering evidence until budget is consumed.
                if explicit_timed_research and (elapsed < timeout_sec) and turn < self.max_tool_turns:
                    logger.info(
                        "Early completion at turn=%d/%d elapsed=%.1f/%d: forcing deeper research",
                        turn,
                        self.max_tool_turns,
                        elapsed,
                        timeout_sec,
                    )
                    forced_tool, forced_args = self._select_forced_tool(topic=topic, turn=turn, scratchpad=scratchpad)
                    logger.info(
                        "Forced tool execution: %s (turn %d/%d)",
                        forced_tool,
                        turn,
                        self.max_tool_turns,
                    )
                    forced_result = self.tool_registry.execute(forced_tool, forced_args)
                    forced_summary = forced_result[:400] if forced_result else "(空)"
                    forced_urls = self._extract_urls_from_result(forced_result)
                    forced_sources = ""
                    if forced_urls:
                        forced_sources = "\n\n[出典/参考URL]\n" + "\n".join(f"- {url}" for url in forced_urls)

                    scratchpad.append(
                        f"[❌ respond は拒否されました - ターン {turn}]\n"
                        f"理由: まだ {self.max_tool_turns - turn} ターン残っており、より詳細な調査が必須です。\n"
                        f"あなたが提案した回答:\n{response_text[:250]}...\n\n"
                        f"[⚠️ 重要: 時間指定リサーチ中のため調査を継続します]\n"
                        f"[自動実行ツール: {forced_tool}]\n"
                        f"{forced_summary}{forced_sources}\n"
                        f"(ターン進捗: {turn}/{self.max_tool_turns}, 経過時間: {elapsed:.0f}秒/{timeout_sec}秒)"
                    )
                    await asyncio.sleep(0.8)
                    continue
                else:
                    # Quick mode or final turn.
                    if self._looks_like_placeholder_response(response_text):
                        composed = await self._compose_final_response(
                            question=question,
                            now_jst=now_jst,
                            scratchpad=scratchpad,
                        )
                        if self._looks_like_placeholder_response(composed):
                            fallback = self._build_fallback_report(
                                question=question,
                                scratchpad=scratchpad,
                                candidate=final_candidate_response,
                            )
                            return fallback, decision_log
                        return self._ensure_sources_in_text(composed, scratchpad), decision_log

                    return self._ensure_sources_in_text(response_text, scratchpad), decision_log

            if decision.get("action") == "tool":
                tool_name = str(decision.get("tool", "")).strip()
                tool_args = decision.get("args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}

                # Prevent recursive dispatch inside research agent.
                if tool_name == "dispatch_research_job":
                    tool_name = "source_deep_dive"
                    tool_args = {"topic": topic, "source": source}

                # Avoid getting stuck on the same tool over and over.
                if len(tool_history) >= 2 and tool_history[-1] == tool_history[-2] == tool_name:
                    alt_tool, alt_args = self._select_forced_tool(topic=topic, turn=turn, scratchpad=scratchpad)
                    if alt_tool != tool_name:
                        logger.info(
                            "Tool repetition guard: %s -> %s (turn %d/%d)",
                            tool_name,
                            alt_tool,
                            turn,
                            self.max_tool_turns,
                        )
                        tool_name = alt_tool
                        tool_args = alt_args

                logger.info("Tool execution: %s (turn %d/%d)", tool_name, turn, self.max_tool_turns)
                result = self.tool_registry.execute(tool_name, tool_args)
                result_summary = result[:400] if result else "(空)"

                # Extract URLs from result for source attribution
                urls = self._extract_urls_from_result(result)
                source_attribution = ""
                if urls:
                    source_attribution = "\n\n[出典/参考URL]\n" + "\n".join(f"- {url}" for url in urls)

                scratchpad.append(f"[ツール結果: {tool_name}]\n{result_summary}{source_attribution}")
                tool_history.append(tool_name)
                if len(tool_history) > 8:
                    tool_history = tool_history[-8:]
                await asyncio.sleep(0.5)

        if explicit_timed_research:
            extra_round = 0
            # Keep continuing until budget is consumed; cap is high enough for long jobs.
            max_extra_rounds = max(12, min(240, timeout_sec * 2))
            while (time.time() - start_time) < timeout_sec and extra_round < max_extra_rounds:
                extra_round += 1
                elapsed = time.time() - start_time
                forced_tool, forced_args = self._select_forced_tool(topic=topic, turn=self.max_tool_turns + extra_round, scratchpad=scratchpad)
                logger.info(
                    "Timed continuation tool: %s (extra_round=%d/%d elapsed=%.1f/%d)",
                    forced_tool,
                    extra_round,
                    max_extra_rounds,
                    elapsed,
                    timeout_sec,
                )
                forced_result = self.tool_registry.execute(forced_tool, forced_args)
                forced_summary = forced_result[:400] if forced_result else "(空)"
                forced_urls = self._extract_urls_from_result(forced_result)
                forced_sources = ""
                if forced_urls:
                    forced_sources = "\n\n[出典/参考URL]\n" + "\n".join(f"- {url}" for url in forced_urls)
                scratchpad.append(
                    f"[継続調査: {forced_tool}]\n"
                    f"{forced_summary}{forced_sources}\n"
                    f"(継続ラウンド: {extra_round}, 経過時間: {elapsed:.0f}秒/{timeout_sec}秒)"
                )
                await asyncio.sleep(1.0)

        final_response = await self._compose_final_response(
            question=question,
            now_jst=now_jst,
            scratchpad=scratchpad,
        )
        if self._looks_like_placeholder_response(final_response):
            fallback = self._build_fallback_report(question=question, scratchpad=scratchpad, candidate=final_candidate_response)
            return fallback, decision_log
        return self._ensure_sources_in_text(final_response, scratchpad), decision_log

    def _build_thinking_prompt(
        self,
        question: str,
        turn: int,
        scratchpad: list[str],
        now_jst: str,
    ) -> str:
        observation = "\n\n".join(scratchpad) if scratchpad else "(ツール結果なし)"

        # Build policy based on turn position
        policy_lines = [
            "- [Feedback] がある場合は、そのフィードバックに従う",
            "- [Proposed Response] と [Feedback] が同時にある場合、respond ではなくツール実行が必須",
            "- 左上に [Feedback] マークがある場合、必ずツール実行を選択する",
            "- ツール呼び出しは具体的引数を与える",
        ]

        # Mid-loop: explicitly forbid respond
        if turn < self.max_tool_turns:
            policy_lines.append(f"- ターン {turn}/{self.max_tool_turns}: 中盤。必ずツール実行を選択。respond は禁止")
        else:
            policy_lines.append(f"- ターン {turn}/{self.max_tool_turns}: 最終ターン。respond で終了してよい")

        policy_lines.extend([
            "- 出力はJSONのみ",
            "- 形式1: {\"action\":\"tool\",\"tool\":\"...\",\"args\":{...},\"reason\":\"...\"}\n- 形式2: {\"action\":\"respond\",\"response\":\"...\"}"
        ])

        return (
            f"現在時刻: {now_jst}\n"
            f"現在ターン: {turn}/{self.max_tool_turns}\n\n"
            "[Available Tools]\n"
            f"{self.tool_registry.render_catalog()}\n\n"
            "[Policy]\n"
            + "\n".join(policy_lines) + "\n\n"
            "[Research Topic]\n"
            f"{question}\n\n"
            "[Observed Results]\n"
            f"{observation}"
        )

    async def _make_decision(self, prompt: str) -> dict[str, Any]:
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.model.generate_content,
                    prompt,
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.2,
                        top_p=0.95,
                        top_k=40,
                        max_output_tokens=256,
                        response_mime_type="application/json",
                    ),
                ),
                timeout=self.config.gemini_timeout_sec,
            )
            text = response.text if hasattr(response, "text") else str(response)
        except asyncio.TimeoutError:
            logger.warning("Gemini API timeout")
            return {"action": "respond", "response": "(タイムアウト)"}
        except Exception as exc:
            logger.exception("Gemini API error: %s", exc)
            return {"action": "respond", "response": f"(APIエラー: {exc})"}

        parsed = self._extract_json_object(text)
        if parsed:
            return parsed
        return {"action": "respond", "response": "(解析エラー)"}

    async def _compose_final_response(
        self,
        question: str,
        now_jst: str,
        scratchpad: list[str],
    ) -> str:
        observation = "\n\n".join(scratchpad) if scratchpad else "(ツール結果なし)"

        # Extract all unique URLs from scratchpad for source attribution
        all_urls = set()
        for item in scratchpad:
            urls = self._extract_urls_from_result(item)
            all_urls.update(urls)

        sources_section = ""
        if all_urls:
            sources_section = "\n\n[参考にした情報源]\n" + "\n".join(f"- {url}" for url in sorted(all_urls))

        prompt = (
            f"{self._build_system_prompt()}\n\n"
            "[Final Response Policy]\n"
            "- 結論を先に書く（簡潔・実用的）\n"
            "- 必ず参考URLを最後に記載する\n"
            "- 複数の視点からの情報を含める\n\n"
            "[Research Topic]\n"
            f"{question}\n\n"
            "[Tool Results]\n"
            f"{observation}{sources_section}"
        )
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self.model.generate_content, prompt),
                timeout=self.config.gemini_timeout_sec,
            )
            text = response.text if hasattr(response, "text") else str(response)
            return self._ensure_sources_in_text(text, scratchpad)
        except Exception as exc:
            logger.exception("Final response error: %s", exc)
            return self._build_fallback_report(question=question, scratchpad=scratchpad, candidate=f"(レスポンス生成失敗: {exc})")

    @staticmethod
    def _build_system_prompt() -> str:
        return "あなたは日本語で正確で簡潔な調査レポートを作成するアシスタントです。"

    def _extract_urls_from_result(self, result: str) -> list[str]:
        """Extract URLs from tool result text."""
        if not result:
            return []
        # Match http/https URLs
        url_pattern = r'https?://[^\s\n\]"]+'
        urls = re.findall(url_pattern, result)
        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in urls:
            # Clean up trailing punctuation that often appears in markdown
            url = url.rstrip('.,;)')
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        return unique_urls[:5]  # Return top 5 unique URLs

    def _collect_source_urls(self, scratchpad: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in scratchpad:
            for url in self._extract_urls_from_result(item):
                if url in seen:
                    continue
                seen.add(url)
                out.append(url)
        return out

    def _ensure_sources_in_text(self, text: str, scratchpad: list[str]) -> str:
        body = (text or "").strip()
        if not body:
            body = "(調査結果の生成に失敗しました)"

        urls = self._collect_source_urls(scratchpad)
        if not urls:
            return body + "\n\n[参考URL]\n- 取得できませんでした"

        # Avoid duplicating when section already exists.
        if "参考URL" in body or "[参考にした情報源]" in body:
            return body

        source_lines = "\n".join(f"- {u}" for u in urls[:12])
        return f"{body}\n\n[参考URL]\n{source_lines}"

    def _looks_like_placeholder_response(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        placeholders = {
            "(タイムアウト)",
            "(解析エラー)",
            "(レスポンス生成失敗)",
            "(調査結果の生成に失敗しました)",
        }
        if t in placeholders:
            return True
        if len(t) <= 16 and t.startswith("(") and t.endswith(")"):
            return True
        return False

    def _build_fallback_report(self, question: str, scratchpad: list[str], candidate: str = "") -> str:
        urls = self._collect_source_urls(scratchpad)
        tool_blocks = [s for s in scratchpad if s.startswith("[ツール結果:")]

        lines = [
            "調査を実施しましたが、最終整形を自動生成できなかったため、収集結果を要約して返します。",
            "",
            "[トピック]",
            question,
            "",
            "[収集結果サマリ]",
        ]

        if candidate and not self._looks_like_placeholder_response(candidate):
            lines.extend([candidate, ""])

        if tool_blocks:
            for idx, block in enumerate(tool_blocks[:6], start=1):
                snippet = block[:280].replace("\n\n", "\n")
                lines.append(f"{idx}. {snippet}")
        else:
            lines.append("- 有効なツール結果が取得できませんでした。")

        lines.extend(["", "[参考URL]"])
        if urls:
            lines.extend([f"- {u}" for u in urls[:12]])
        else:
            lines.append("- 取得できませんでした")

        return "\n".join(lines).strip()

    def _select_forced_tool(self, topic: str, turn: int, scratchpad: list[str]) -> tuple[str, dict[str, Any]]:
        urls = self._collect_source_urls(scratchpad)

        # Rotate forced tools to diversify evidence.
        mode = turn % 3
        if mode == 1:
            return "web_search", {"query": f"{topic} official benchmark performance adoption"}
        if mode == 2 and urls:
            return "read_url_markdown", {"url": urls[0]}
        return "source_deep_dive", {"topic": topic, "source": "auto"}

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        if not text:
            return None

        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?", "", candidate).strip()
            candidate = re.sub(r"```$", "", candidate).strip()

        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

        match = re.search(r"\{[\s\S]*\}", candidate)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None


def load_research_orchestrator_config() -> OrchestratorConfig:
    """Load Research orchestrator config from environment."""
    genai.configure(api_key=os.getenv("RESEARCH_GEMINI_API_KEY", ""))
    return OrchestratorConfig(
        gemini_model=os.getenv("RESEARCH_GEMINI_MODEL", "gemini-3.1-flash-lite-preview").strip(),
        gemini_timeout_sec=int(os.getenv("RESEARCH_GEMINI_TIMEOUT_SEC", "60")),
    )


async def build_research_orchestrator() -> ResearchOrchestrator:
    """Factory function for research orchestrator."""
    config = load_research_orchestrator_config()
    return ResearchOrchestrator(config)
