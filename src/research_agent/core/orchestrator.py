"""Research Agent orchestrator - minimal LLM-based research coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from tools import ToolRegistry, build_default_tool_registry
from tools.research_loop import run_model_research_loop

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OrchestratorConfig:
    use_gemini_cli: bool = True
    gemini_command: str = "gemini"
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    gemini_timeout_sec: int = 60


class ResearchOrchestrator:
    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()
        self.tool_registry = build_default_tool_registry()
        self.max_tool_turns = 2
        self.last_transcript = ""
        self._deadline_monotonic: float | None = None

    async def answer(
        self,
        topic: str,
        source: str = "auto",
        timeout_sec: int = 60,
        time_specified: bool = False,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Run the Gemini CLI-based research loop and keep its raw transcript for later attachment."""
        await asyncio.sleep(0)
        if time_specified:
            self._deadline_monotonic = time.monotonic() + max(10, int(timeout_sec))
            if timeout_sec >= 60:
                self.max_tool_turns = max(6, min(18, timeout_sec // 8))
            else:
                self.max_tool_turns = 2
        else:
            self._deadline_monotonic = None
            self.max_tool_turns = 4

        question = f"topic: {topic}\nsource: {source}"

        def _call_model(prompt: str) -> str:
            if not self.config.use_gemini_cli:
                logger.warning("Gemini CLI is disabled in config; using CLI path anyway")
            return self._call_gemini_cli(prompt)

        try:
            loop_result = await asyncio.to_thread(
                run_model_research_loop,
                topic=question,
                source=source,
                timeout_sec=timeout_sec,
                model_name=self.config.gemini_model,
                model_call=_call_model,
                loop_label="gemini-cli-research",
                tool_registry=self.tool_registry,
                max_turns=self.max_tool_turns if self.max_tool_turns > 0 else None,
            )
            self.last_transcript = loop_result.transcript
            return loop_result.report, loop_result.decision_log
        finally:
            self._deadline_monotonic = None

    def _call_gemini_cli(self, prompt: str) -> str:
        command = (self.config.gemini_command or "gemini").strip() or "gemini"
        argv = shlex.split(command)
        if not argv:
            raise RuntimeError("gemini_cli_command_empty")

        if self.config.gemini_model:
            argv.extend(["--model", self.config.gemini_model])
        argv.extend(["--prompt", prompt])

        cli_timeout = int(self.config.gemini_timeout_sec)
        if self._deadline_monotonic is not None:
            remaining = int(self._deadline_monotonic - time.monotonic())
            if remaining <= 1:
                raise RuntimeError("research_timeout_reached_before_gemini_call")
            cli_timeout = max(5, min(cli_timeout, remaining))

        logger.info(
            "[route] research-agent -> gemini-cli command=%s model=%s timeout_sec=%s prompt_chars=%s",
            argv[0],
            self.config.gemini_model,
            cli_timeout,
            len(prompt),
        )

        try:
            completed = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                timeout=cli_timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"gemini_cli_not_found:{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"gemini_cli_timeout:{int(cli_timeout)}s") from exc
        except Exception as exc:
            raise RuntimeError(f"gemini_cli_exec_failed:{exc}") from exc

        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "gemini_cli_non_zero_exit").strip()
            raise RuntimeError(err[:1200])

        output = (completed.stdout or "").strip()
        if not output:
            raise RuntimeError("gemini_cli_empty_output")
        return output

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
                asyncio.to_thread(self._call_gemini_cli, prompt),
                timeout=self.config.gemini_timeout_sec,
            )
            text = response
        except asyncio.TimeoutError:
            logger.warning("Gemini CLI timeout")
            return {"action": "respond", "response": "(タイムアウト)"}
        except Exception as exc:
            logger.exception("Gemini CLI error: %s", exc)
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
        deterministic = self._compose_github_probe_summary(question=question, scratchpad=scratchpad)
        if deterministic:
            return self._ensure_sources_in_text(deterministic, scratchpad)

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
            "- 複数の視点からの情報を含める\n"
            "- GitHub情報は README本文 と About欄(description) を必ず区別して記述する\n"
            "- 断定時は Tool Results 内の根拠行に一致する内容のみを書く。不明なら不明と書く\n"
            "- [GitHub Repo Probe] がある場合、about_description/about_contains_kc3hack と README_* を最優先根拠にする\n"
            "- README_contains_kc3hack=no の場合、『READMEにKc3hack記載がある』とは書かない\n\n"
            "[Research Topic]\n"
            f"{question}\n\n"
            "[Tool Results]\n"
            f"{observation}{sources_section}"
        )
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._call_gemini_cli, prompt),
                timeout=self.config.gemini_timeout_sec,
            )
            text = response
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
            clean = self._normalize_extracted_url(url)
            if not clean:
                continue
            if clean not in seen:
                seen.add(clean)
                unique_urls.append(clean)
        return unique_urls[:5]  # Return top 5 unique URLs

    def _compose_github_probe_summary(self, question: str, scratchpad: list[str]) -> str:
        probe = self._extract_latest_probe_fields(scratchpad)
        if not probe:
            return ""

        repo = probe.get("repo", "(unknown)")
        stars = probe.get("stars", "unknown")
        open_issues = probe.get("open_issues", "unknown")
        open_prs = probe.get("open_prs", "unknown")
        about_desc = probe.get("about_description", "(none)")
        about_kc = probe.get("about_contains_kc3hack", "unknown")
        readme_state = probe.get("README", "unknown")
        readme_kc = probe.get("README_contains_kc3hack", "unknown")
        readme_head = probe.get("README_headline", "")
        readme_excerpt = probe.get("README_excerpt", "")
        latest_issue_title = probe.get("latest_issue_title", "")
        latest_issue_updated = probe.get("latest_issue_updated_at", "")
        latest_pr_title = probe.get("latest_pr_title", "")
        latest_pr_updated = probe.get("latest_pr_updated_at", "")

        lines: list[str] = [
            f"`{repo}` のGitHub状況を確認しました。",
            "",
            "- リポジトリ基本情報",
            f"  stars: {stars}",
            f"  open issues: {open_issues}",
            f"  open PRs: {open_prs}",
            "",
            "- READMEとAboutの区別",
            f"  About(description): {about_desc}",
            f"  AboutにKc3hack表記: {about_kc}",
            f"  README状態: {readme_state}",
            f"  READMEにKc3hack表記: {readme_kc}",
        ]
        if readme_head:
            lines.append(f"  README見出し: {readme_head}")
        if readme_excerpt:
            lines.append(f"  README抜粋: {readme_excerpt}")

        lines.extend(["", "- 最新の議論/更新（Issue・PR）"])
        if latest_issue_title:
            lines.append(f"  最新Issue: {latest_issue_title} (updated: {latest_issue_updated or 'unknown'})")
        else:
            lines.append("  最新Issue: 取得範囲内で確認できませんでした")
        if latest_pr_title:
            lines.append(f"  最新PR: {latest_pr_title} (updated: {latest_pr_updated or 'unknown'})")
        else:
            lines.append("  最新PR: 取得範囲内で確認できませんでした")

        if readme_kc == "no" and about_kc == "yes":
            lines.extend([
                "",
                "補足: Kc3hack表記はAbout欄由来であり、README本文由来ではありません。",
            ])

        return "\n".join(lines).strip()

    @staticmethod
    def _extract_latest_probe_fields(scratchpad: list[str]) -> dict[str, str]:
        joined = "\n\n".join(scratchpad or [])
        if "[GitHub Repo Probe]" not in joined:
            return {}
        blocks = joined.split("[GitHub Repo Probe]")
        latest = blocks[-1]
        fields: dict[str, str] = {}
        for line in latest.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("["):
                continue
            if ":" not in raw:
                continue
            key, value = raw.split(":", 1)
            k = key.strip()
            v = value.strip()
            if k and v:
                fields[k] = v
        return fields

    @staticmethod
    def _normalize_extracted_url(url: str) -> str:
        clean = (url or "").strip()
        if not clean:
            return ""
        for marker in ("\\n", "/n", "\n"):
            idx = clean.find(marker)
            if idx >= 0:
                clean = clean[:idx]
        clean = clean.rstrip('.,;)-')
        if not clean.startswith("http://") and not clean.startswith("https://"):
            return ""
        return clean

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
    cli_model = (
        os.getenv("RESEARCH_AGENT_GEMINI_MODEL", "").strip()
        or os.getenv("RESEARCH_GEMINI_MODEL", "gemini-3.1-flash-lite-preview").strip()
    )
    cli_timeout_raw = (
        os.getenv("RESEARCH_AGENT_GEMINI_TIMEOUT_SEC", "").strip()
        or os.getenv("RESEARCH_GEMINI_TIMEOUT_SEC", "60").strip()
    )
    try:
        cli_timeout = int(cli_timeout_raw)
    except ValueError:
        cli_timeout = 60

    return OrchestratorConfig(
        use_gemini_cli=os.getenv("RESEARCH_AGENT_USE_GEMINI_CLI", "true").strip().lower() == "true",
        gemini_command=os.getenv("RESEARCH_AGENT_GEMINI_COMMAND", "gemini").strip() or "gemini",
        gemini_model=cli_model,
        gemini_timeout_sec=max(30, cli_timeout),
    )


async def build_research_orchestrator() -> ResearchOrchestrator:
    """Factory function for research orchestrator."""
    config = load_research_orchestrator_config()
    return ResearchOrchestrator(config)
