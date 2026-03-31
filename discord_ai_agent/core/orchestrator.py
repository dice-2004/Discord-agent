from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from core.memory import ChannelMemoryStore
from tools.search_tools import web_search

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
        self.tools = [web_search]
        self.tool_map = {tool.name: tool for tool in self.tools}
        self._profile_cache: str | None = None

        self.llm = ChatGoogleGenerativeAI(
            model=config.gemini_model,
            google_api_key=config.gemini_api_key,
            temperature=0.2,
        )

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
            limit=4,
        )
        context_lines = [
            f"- [{record.timestamp}] {record.role}: {record.content[:240]}"
            for record in history_records
            if record.content.strip()
        ]
        history_context = "\n".join(context_lines) if context_lines else "(関連履歴なし)"

        system_prompt = await self._build_system_prompt(history_context)
        messages: list[Any] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=question),
        ]

        try:
            final_ai_message = await self._invoke_with_retry(messages, with_tools=False)
            answer_text = self._extract_text(final_ai_message.content)
            if not answer_text.strip():
                answer_text = "回答を生成できませんでした。質問を少し変えて再試行してください。"
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

    async def _run_with_tools(self, base_messages: list[Any]) -> Any:
        conversation = list(base_messages)
        max_turns = 3

        for _ in range(max_turns):
            ai_message = await self._invoke_with_retry(conversation, with_tools=True)
            conversation.append(ai_message)

            tool_calls = getattr(ai_message, "tool_calls", None) or []
            if not tool_calls:
                return ai_message

            for call in tool_calls:
                tool_name = call.get("name", "")
                tool = self.tool_map.get(tool_name)
                if tool is None:
                    tool_output = f"ツール '{tool_name}' は利用できません。"
                else:
                    args = call.get("args", {})
                    try:
                        tool_output = await asyncio.to_thread(tool.invoke, args)
                    except Exception:
                        logger.exception("Tool execution failed: %s", tool_name)
                        tool_output = "ツール実行でエラーが発生しました。"

                conversation.append(
                    ToolMessage(
                        content=str(tool_output),
                        tool_call_id=call.get("id", ""),
                    )
                )

        return await self._invoke_with_retry(conversation, with_tools=False)

    async def _invoke_with_retry(self, messages: list[Any], with_tools: bool) -> Any:
        retries = 2
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    self.llm.ainvoke(messages),
                    timeout=self.config.gemini_timeout_sec,
                )
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(2**attempt)

        raise RuntimeError("Gemini invocation failed after retries") from last_error

    async def _build_system_prompt(self, history_context: str) -> str:
        profile_text = await self._load_profile_text()
        static_profile = profile_text if profile_text else "(initial profileは未設定)"

        return (
            "あなたはDiscord上の個人向けAIアシスタントです。"
            "\n- 不明点は断定しない\n- 必要時のみweb_searchを使う\n"
            "- 回答は簡潔かつ実用的にまとめる"
            "\n\n[Static Profile]\n"
            f"{static_profile}\n\n"
            "[Relevant Conversation Memory]\n"
            f"{history_context}\n"
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

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, str):
                    chunks.append(part)
                elif isinstance(part, dict) and "text" in part:
                    chunks.append(str(part.get("text", "")))
            return "\n".join(c for c in chunks if c).strip()
        return str(content)


def load_orchestrator_config_from_env() -> OrchestratorConfig:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    return OrchestratorConfig(
        gemini_api_key=api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview").strip() or "gemini-3.1-flash-lite-preview",
        gemini_timeout_sec=int(os.getenv("GEMINI_TIMEOUT_SEC", "30")),
        profile_path=os.getenv("INITIAL_PROFILE_PATH", "./data/profiles/initial_profile.md").strip(),
        chromadb_path=os.getenv("CHROMADB_PATH", "./data/chromadb").strip(),
    )
