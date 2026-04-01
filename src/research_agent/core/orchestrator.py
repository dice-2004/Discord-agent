"""Research Agent orchestrator - minimal LLM-based research coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import google.generativeai as genai

from research_agent.tools.registry import ResearchToolRegistry, build_research_tool_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OrchestratorConfig:
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    gemini_timeout_sec: int = 60


class ResearchOrchestrator:
    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()
        self.tool_registry = build_research_tool_registry()
        self.model = genai.GenerativeModel(model_name=self.config.gemini_model)
        self.max_tool_turns = 2

    async def answer(
        self,
        topic: str,
        source: str = "auto",
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Research answer with tool-calling support.
        Returns: (answer_text, decision_log)
        """
        await asyncio.sleep(0)

        turn = 0
        scratchpad: list[str] = []
        decision_log: list[dict[str, Any]] = []

        for turn in range(1, self.max_tool_turns + 1):
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

            if decision.get("action") == "respond":
                response_text = str(decision.get("response", "")).strip()
                return response_text, decision_log

            if decision.get("action") == "tool":
                tool_name = str(decision.get("tool", "")).strip()
                tool_args = decision.get("args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}

                logger.info("Tool execution: %s (turn %d/%d)", tool_name, turn, self.max_tool_turns)
                result = self.tool_registry.execute(tool_name, tool_args)
                result_summary = result[:400] if result else "(空)"
                scratchpad.append(f"[ツール結果: {tool_name}]\n{result_summary}")

        final_response = await self._compose_final_response(
            question=question,
            now_jst=now_jst,
            scratchpad=scratchpad,
        )
        return final_response, decision_log

    def _build_thinking_prompt(
        self,
        question: str,
        turn: int,
        scratchpad: list[str],
        now_jst: str,
    ) -> str:
        observation = "\n\n".join(scratchpad) if scratchpad else "(ツール結果なし)"
        return (
            f"現在時刻: {now_jst}\n"
            f"現在ターン: {turn}/{self.max_tool_turns}\n\n"
            "[Available Tools]\n"
            f"{self.tool_registry.render_catalog()}\n\n"
            "[Policy]\n"
            "- 情報不足時のみツールを使う\n"
            "- 回答可能なら即座に回答する\n"
            "- ツール呼び出しは具体的引数を与える\n"
            "- 出力はJSONのみ\n"
            "- 形式1: {\"action\":\"tool\",\"tool\":\"...\",\"args\":{...},\"reason\":\"...\"}\n"
            "- 形式2: {\"action\":\"respond\",\"response\":\"...\"}\n\n"
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
        prompt = (
            f"{self._build_system_prompt()}\n\n"
            "[Final Response Policy]\n"
            "- 結論を先に書く（簡潔・実用的）\n"
            "- 出典URL等があれば記載\n\n"
            "[Research Topic]\n"
            f"{question}\n\n"
            "[Tool Results]\n"
            f"{observation}"
        )
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self.model.generate_content, prompt),
                timeout=self.config.gemini_timeout_sec,
            )
            return response.text if hasattr(response, "text") else str(response)
        except Exception as exc:
            logger.exception("Final response error: %s", exc)
            return f"(レスポンス生成失敗: {exc})"

    @staticmethod
    def _build_system_prompt() -> str:
        return "あなたは日本語で正確で簡潔な調査レポートを作成するアシスタントです。"

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
