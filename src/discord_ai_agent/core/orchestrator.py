"""
Orchestrator using Google Generative AI SDK directly (not via LangChain)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import google.generativeai as genai

from discord_ai_agent.core.memory import ChannelMemoryStore
from discord_ai_agent.tools.search_tools import web_search

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

        # Initialize Gemini API
        genai.configure(api_key=config.gemini_api_key)

        self.model = genai.GenerativeModel(
            model_name=config.gemini_model,
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

        try:
            answer_text = await self._generate_with_tools(system_prompt, question)
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

    async def _generate_with_tools(self, system_prompt: str, question: str) -> str:
        """Gemini本体は通常生成のみ行い、ツール呼び出しはOrchestrator側で制御する。"""
        use_search = self._needs_web_search(question)
        search_context = ""

        if use_search:
            logger.info("Tool execution: web_search")
            try:
                search_result = await asyncio.to_thread(web_search, question)
                search_context = (
                    "\n\n[Web Search Results]\n"
                    f"{search_result}\n"
                    "[Instruction]\n"
                    "上記検索結果を優先し、事実と推測を分けて簡潔に回答してください。"
                )
            except Exception:
                logger.exception("web_search failed")
                search_context = "\n\n[Web Search Results]\n検索取得に失敗しました。既知情報のみで回答してください。"

        prompt = (
            f"{system_prompt}\n\n"
            f"[User Question]\n{question}"
            f"{search_context}"
        )
        return await self._invoke_with_retry(prompt)

    def _needs_web_search(self, question: str) -> bool:
        """最新性が必要な質問のみ検索を使う。"""
        q = (question or "").strip().lower()
        if not q:
            return False

        strong_keywords = [
            "今日", "最新", "ニュース", "現在", "今", "速報", "天気", "為替", "株価",
            "価格", "発売", "障害", "不具合", "何時", "何時から", "直近", "アップデート",
            "today", "latest", "news", "current", "weather", "price", "rate", "status",
        ]
        if any(k in q for k in strong_keywords):
            return True

        # 西暦年付き質問は鮮度依存のことが多い
        return re.search(r"20[2-9][0-9]", q) is not None

    async def _invoke_with_retry(self, prompt: str) -> str:
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
                            max_output_tokens=2048,
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
            "\n- 必要時のみweb_searchツールを使う"
            "\n- 回答は簡潔かつ実用的にまとめる"
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
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview").strip() or "gemini-3.1-flash-lite-preview",
        gemini_timeout_sec=int(os.getenv("GEMINI_TIMEOUT_SEC", "30")),
        profile_path=os.getenv("INITIAL_PROFILE_PATH", "./data/profiles/initial_profile.md").strip(),
        chromadb_path=os.getenv("CHROMADB_PATH", "./data/chromadb").strip(),
    )
