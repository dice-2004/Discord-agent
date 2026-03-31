"""Orchestrator using Google Generative AI SDK directly (not via LangChain)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import google.generativeai as genai

from discord_ai_agent.core.memory import ChannelMemoryStore
from discord_ai_agent.tools import ToolRegistry, build_default_tool_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OrchestratorConfig:
    gemini_api_key: str
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    gemini_timeout_sec: int = 30
    profile_path: str = "./data/profiles/initial_profile.md"
    profile_max_chars: int = 12000
    chromadb_path: str = "./data/chromadb"


class DiscordOrchestrator:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self.memory = ChannelMemoryStore(persist_dir=config.chromadb_path, top_k=4)
        self._profile_cache: str | None = None
        self.tool_registry: ToolRegistry = build_default_tool_registry()
        self.max_tool_turns = int(os.getenv("MAX_TOOL_TURNS", "3"))
        self.max_review_turns = int(os.getenv("MAX_REVIEW_TURNS", "1"))
        self.memory_scope = os.getenv("MEMORY_RETRIEVAL_SCOPE", "guild").strip().lower() or "guild"
        self.memory_top_k = int(os.getenv("MEMORY_TOP_K", "8"))
        self.memory_response_include_evidence = (
            os.getenv("MEMORY_RESPONSE_INCLUDE_EVIDENCE", "false").strip().lower() == "true"
        )
        self.memory_response_evidence_items = int(os.getenv("MEMORY_RESPONSE_EVIDENCE_ITEMS", "3"))

        genai.configure(api_key=config.gemini_api_key)
        self.model = genai.GenerativeModel(model_name=config.gemini_model)

    async def answer(
        self,
        question: str,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        message_id: int | None,
    ) -> str:
        history_records = await self.memory.fetch_relevant_messages(
            guild_id=guild_id,
            channel_id=channel_id,
            query_text=question,
            limit=self.memory_top_k,
            scope=self.memory_scope,
        )
        hit_channels = sorted(
            {
                str((record.metadata or {}).get("channel_id", ""))
                for record in history_records
                if (record.metadata or {}).get("channel_id")
            }
        )
        logger.info(
            "Memory retrieval: scope=%s hits=%s channels=%s",
            self.memory_scope,
            len(history_records),
            hit_channels,
        )
        context_lines: list[str] = []
        for record in history_records:
            if not record.content.strip():
                continue
            md = record.metadata or {}
            source_channel = str(md.get("channel_id", ""))
            channel_tag = f" ch={source_channel}" if source_channel else ""
            context_lines.append(f"- [{record.timestamp}] {record.role}{channel_tag}: {record.content[:240]}")
        history_context = "\n".join(context_lines) if context_lines else "(関連履歴なし)"

        system_prompt = await self._build_system_prompt(history_context)

        try:
            answer_text = await self._generate_with_tools(system_prompt, question)
            if not answer_text.strip():
                answer_text = "回答を生成できませんでした。質問を少し変えて再試行してください。"
            if self.memory_response_include_evidence:
                answer_text = self._append_memory_evidence(answer_text, history_records)
        except Exception:
            logger.exception("LLM invocation failed")
            answer_text = "現在AI応答で問題が発生しています。時間をおいて再試行してください。"

        await self._store_conversation(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            question=question,
            answer=answer_text,
            request_message_id=message_id,
        )
        return answer_text

    def _append_memory_evidence(self, answer_text: str, records: list[Any]) -> str:
        if not answer_text.strip() or not records:
            return answer_text

        lines: list[str] = []
        seen_keys: set[str] = set()
        max_items = max(self.memory_response_evidence_items, 1)
        for record in records:
            if len(lines) >= max_items:
                break
            md = record.metadata or {}
            channel_id = str(md.get("channel_id", ""))
            channel_name = str(md.get("channel_name", "")).strip()
            ts = self._format_jst_timestamp(record.timestamp)
            snippet = (record.content or "").replace("\n", " ").strip()
            if len(snippet) > 70:
                snippet = snippet[:70] + "..."
            dedup_key = f"{channel_id}:{record.role}:{snippet}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            channel_label = f"#{channel_name}" if channel_name else f"ch={channel_id}"
            lines.append(f"- [{ts}] {channel_label} {record.role}: {snippet}")

        if not lines:
            return answer_text
        return answer_text.rstrip() + "\n\n[参照メモリ]\n" + "\n".join(lines)

    @staticmethod
    def _format_jst_timestamp(timestamp_text: str) -> str:
        if not timestamp_text:
            return "unknown"
        try:
            parsed = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            jst = parsed.astimezone(timezone(timedelta(hours=9)))
            return jst.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(timestamp_text)[:19]

    async def ingest_channel_history(
        self,
        guild_id: int | None,
        channel_id: int,
        messages: list[dict[str, Any]],
    ) -> int:
        """Store historical channel messages to improve memory recall for old conversations."""
        if not messages:
            return 0

        stored = 0
        ordered = sorted(messages, key=lambda m: int(m.get("message_id", 0)))
        for item in ordered:
            content = str(item.get("content", "")).strip()
            if not content:
                continue

            role = "assistant" if bool(item.get("is_bot", False)) else "user"
            user_id = int(item.get("author_id", 0) or 0)
            message_id = item.get("message_id")
            created_at = str(item.get("created_at", "")).strip()
            channel_name = str(item.get("channel_name", "")).strip()
            try:
                await self.memory.add_message(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    role=role,
                    content=content,
                    user_id=user_id,
                    message_id=int(message_id) if message_id is not None else None,
                    metadata={
                        "source": "discord_history",
                        "kind": "backfill",
                        "timestamp": created_at,
                        "channel_name": channel_name,
                    },
                )
                stored += 1
            except Exception:
                logger.exception("Failed to ingest historical message: channel=%s", channel_id)

        return stored

    async def _generate_with_tools(self, system_prompt: str, question: str) -> str:
        now_jst = datetime.now().strftime("%Y-%m-%d %H:%M")
        scratchpad: list[str] = []

        for turn in range(1, self.max_tool_turns + 1):
            decision = await self._decide_next_action(
                system_prompt=system_prompt,
                question=question,
                now_jst=now_jst,
                scratchpad=scratchpad,
                turn=turn,
            )
            action = str(decision.get("action", "respond")).strip().lower()
            logger.info(
                "Agent decision: turn=%s action=%s tool=%s reason=%s",
                turn,
                action,
                str(decision.get("tool", "")),
                str(decision.get("reason", ""))[:180],
            )

            if action == "tool":
                tool_name = str(decision.get("tool", "")).strip()
                args = decision.get("args", {})
                if not tool_name or not isinstance(args, dict):
                    scratchpad.append("[Agent] 不正なツール要求を検知したため無視しました。")
                    continue

                logger.info("Tool execution: %s (%s/%s)", tool_name, turn, self.max_tool_turns)
                tool_output = await asyncio.to_thread(self.tool_registry.execute, tool_name, args)
                logger.info("Tool result summary: tool=%s chars=%s", tool_name, len(tool_output))
                scratchpad.append(
                    f"[Tool:{tool_name}] args={json.dumps(args, ensure_ascii=False)}\n{tool_output}"
                )
                continue

            response = str(decision.get("response", "")).strip()
            if response:
                return await self._self_review_response(
                    system_prompt=system_prompt,
                    question=question,
                    response=response,
                    scratchpad=scratchpad,
                )

        composed = await self._compose_final_response(
            system_prompt=system_prompt,
            question=question,
            now_jst=now_jst,
            scratchpad=scratchpad,
        )
        return await self._self_review_response(
            system_prompt=system_prompt,
            question=question,
            response=composed,
            scratchpad=scratchpad,
        )

    async def _self_review_response(
        self,
        system_prompt: str,
        question: str,
        response: str,
        scratchpad: list[str],
    ) -> str:
        current = response
        working_scratchpad = list(scratchpad)

        for review_turn in range(1, self.max_review_turns + 1):
            review_decision = await self._review_decision(
                system_prompt=system_prompt,
                question=question,
                response=current,
                scratchpad=working_scratchpad,
            )
            action = str(review_decision.get("action", "approve")).strip().lower()
            logger.info("Agent self-review: turn=%s action=%s", review_turn, action)

            if action == "approve":
                return current

            if action == "rewrite":
                rewritten = str(review_decision.get("response", "")).strip()
                if rewritten:
                    current = rewritten
                continue

            if action == "needs_tool":
                tool_name = str(review_decision.get("tool", "")).strip()
                args = review_decision.get("args", {})
                if tool_name and isinstance(args, dict):
                    tool_output = await asyncio.to_thread(self.tool_registry.execute, tool_name, args)
                    working_scratchpad.append(
                        f"[ReviewTool:{tool_name}] args={json.dumps(args, ensure_ascii=False)}\n{tool_output}"
                    )
                    current = await self._compose_final_response(
                        system_prompt=system_prompt,
                        question=question,
                        now_jst=datetime.now().strftime("%Y-%m-%d %H:%M"),
                        scratchpad=working_scratchpad,
                    )
                continue

            return current

        return current

    async def _review_decision(
        self,
        system_prompt: str,
        question: str,
        response: str,
        scratchpad: list[str],
    ) -> dict[str, Any]:
        observation = "\n\n".join(scratchpad) if scratchpad else "(ツール結果なし)"
        prompt = (
            f"{system_prompt}\n\n"
            "[Reviewer Role]\n"
            "あなたは回答品質レビュー担当です。以下の回答を評価し、必要なら修正してください。\n"
            "出力はJSONのみ。\n"
            "- 形式1(問題なし): {\"action\":\"approve\"}\n"
            "- 形式2(書き換え): {\"action\":\"rewrite\",\"response\":\"...\"}\n"
            "- 形式3(追加ツール必要): {\"action\":\"needs_tool\",\"tool\":\"...\",\"args\":{...},\"reason\":\"...\"}\n"
            "条件: 断定し過ぎ・根拠不足・質問未回答があれば修正する。\n\n"
            "[Question]\n"
            f"{question}\n\n"
            "[Current Answer]\n"
            f"{response}\n\n"
            "[Tool Results]\n"
            f"{observation}\n\n"
            "[Available Tools]\n"
            f"{self.tool_registry.render_catalog()}"
        )
        raw = await self._invoke_with_retry(prompt, max_output_tokens=420)
        parsed = self._extract_json_object(raw)
        if parsed:
            return parsed
        return {"action": "approve"}

    async def _decide_next_action(
        self,
        system_prompt: str,
        question: str,
        now_jst: str,
        scratchpad: list[str],
        turn: int,
    ) -> dict[str, Any]:
        observation = "\n\n".join(scratchpad) if scratchpad else "(まだツール未実行)"
        prompt = (
            f"{system_prompt}\n\n"
            "[Agent Role]\n"
            "あなたは自律エージェントの意思決定器です。\n"
            f"現在時刻: {now_jst}\n"
            f"現在ターン: {turn}/{self.max_tool_turns}\n\n"
            "[Available Tools]\n"
            f"{self.tool_registry.render_catalog()}\n\n"
            "[Policy]\n"
            "- 情報不足時のみツールを使う\n"
            "- 回答可能なら即座に回答する\n"
            "- ツール呼び出しは具体的引数を与える\n"
            "- ツール引数はカタログ記載の必須キーをすべて含める\n"
            "- 出力はJSONのみ\n"
            "- 形式1: {\"action\":\"tool\",\"tool\":\"...\",\"args\":{...},\"reason\":\"...\"}\n"
            "- 形式2: {\"action\":\"respond\",\"response\":\"...\"}\n\n"
            "[User Question]\n"
            f"{question}\n\n"
            "[Observed Tool Results]\n"
            f"{observation}"
        )
        raw = await self._invoke_with_retry(prompt, max_output_tokens=380)
        parsed = self._extract_json_object(raw)
        if parsed:
            return parsed
        return {
            "action": "respond",
            "response": "回答を組み立て中に形式エラーが発生しました。質問を短くして再試行してください。",
        }

    async def _compose_final_response(
        self,
        system_prompt: str,
        question: str,
        now_jst: str,
        scratchpad: list[str],
    ) -> str:
        observation = "\n\n".join(scratchpad) if scratchpad else "(ツール結果なし)"
        prompt = (
            f"{system_prompt}\n\n"
            "[Policy]\n"
            f"- 現在時刻の基準: {now_jst}\n"
            "- まず結論を先に書く\n"
            "- 箇条書き中心で短く実用的にまとめる\n"
            "- 不足情報がある場合は不足点のみ簡潔に示す\n"
            "- 不要な確認質問はしない\n\n"
            "[User Question]\n"
            f"{question}\n\n"
            "[Tool Results]\n"
            f"{observation}"
        )
        return await self._invoke_with_retry(prompt)

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

    async def _invoke_with_retry(self, prompt: str, max_output_tokens: int = 2048) -> str:
        retries = 2
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.model.generate_content,
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            temperature=0.2,
                            top_p=0.95,
                            top_k=40,
                            max_output_tokens=max_output_tokens,
                        ),
                    ),
                    timeout=self.config.gemini_timeout_sec,
                )
                text = (getattr(response, "text", "") or "").strip()
                if text:
                    return text
                return "回答を生成できませんでした。質問を少し変えて再試行してください。"
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(2**attempt)

        raise RuntimeError("Gemini invocation failed after retries") from last_error

    async def _store_conversation(
        self,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        question: str,
        answer: str,
        request_message_id: int | None,
    ) -> None:
        try:
            await self.memory.add_message(
                guild_id=guild_id,
                channel_id=channel_id,
                role="user",
                content=question,
                user_id=user_id,
                message_id=request_message_id,
                metadata={"source": "discord", "kind": "question"},
            )
            await self.memory.add_message(
                guild_id=guild_id,
                channel_id=channel_id,
                role="assistant",
                content=answer,
                user_id=0,
                message_id=None,
                metadata={"source": "discord", "kind": "answer"},
            )
        except Exception:
            logger.exception("Failed to persist conversation memory")

    async def _build_system_prompt(self, history_context: str) -> str:
        profile_text = await self._load_profile_text()
        static_profile = profile_text if profile_text else "(initial profileは未設定)"

        return (
            "あなたはDiscord上の個人向けAIアシスタントです。"
            "\n- 不明点は断定しない"
            "\n- 必要時のみツールを使う"
            "\n- 回答は簡潔かつ実用的にまとめる"
            "\n- ユーザーの追加指示待ちにならないよう、自律的に必要情報を補って回答する"
            "\n\n[Static Profile]\n"
            f"{static_profile}\n\n"
            "[Relevant Conversation Memory]\n"
            f"{history_context}"
        )

    async def _load_profile_text(self) -> str:
        if self._profile_cache is not None:
            return self._profile_cache

        path = Path(self.config.profile_path)
        if not path.exists():
            logger.warning("initial_profile.md not found; continuing without static profile")
            self._profile_cache = ""
            return self._profile_cache

        try:
            text = await asyncio.to_thread(path.read_text, "utf-8")
        except Exception:
            logger.exception("Failed to load initial profile")
            self._profile_cache = ""
            return self._profile_cache

        if len(text) > self.config.profile_max_chars:
            logger.warning(
                "initial_profile.md exceeds max chars (%s). Truncating.",
                self.config.profile_max_chars,
            )
            text = text[: self.config.profile_max_chars]

        self._profile_cache = text
        return self._profile_cache


def load_orchestrator_config_from_env() -> OrchestratorConfig:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    return OrchestratorConfig(
        gemini_api_key=api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview").strip()
        or "gemini-3.1-flash-lite-preview",
        gemini_timeout_sec=int(os.getenv("GEMINI_TIMEOUT_SEC", "30")),
        profile_path=os.getenv("INITIAL_PROFILE_PATH", "./data/profiles/initial_profile.md").strip(),
        chromadb_path=os.getenv("CHROMADB_PATH", "./data/chromadb").strip(),
    )
