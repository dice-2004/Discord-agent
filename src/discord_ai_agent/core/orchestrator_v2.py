"""
Orchestrator using Google Generative AI SDK directly (not via LangChain)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

        # Create tool definitions for Gemini
        self.tools = [
            genai.Tool(
                function_declarations=[
                    genai.FunctionDeclaration(
                        name="web_search",
                        description="検索エンジンを使ってウェブから情報を取得します。",
                        parameters=genai.Schema(
                            type=genai.Type.OBJECT,
                            properties={
                                "query": genai.Schema(
                                    type=genai.Type.STRING,
                                    description="検索クエリ"
                                )
                            },
                            required=["query"]
                        )
                    )
                ]
            )
        ]

        self.model = genai.GenerativeModel(
            model_name=config.gemini_model,
            tools=self.tools,
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
        """Generate response using tools with thought signatures"""
        messages = [
            {"role": "user", "parts": [system_prompt + "\n\nユーザーの質問: " + question]}
        ]

        max_turns = 3
        for turn in range(max_turns):
            try:
                # Generate content with tools
                response = await asyncio.to_thread(
                    self.model.generate_content,
                    messages,
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.2,
                        top_p=0.95,
                        top_k=40,
                        max_output_tokens=2048,
                    ),
                )

                # Check if there are tool calls
                has_tool_call = False
                for part in response.parts:
                    if part.function_call:
                        has_tool_call = True
                        tool_name = part.function_call.name
                        tool_args = {k: v for k, v in part.function_call.args.items()}

                        logger.info(f"Tool call: {tool_name} with args: {tool_args}")

                        # Execute tool
                        if tool_name == "web_search":
                            try:
                                tool_result = await asyncio.to_thread(
                                    web_search.invoke,
                                    {"query": tool_args.get("query", "")}
                                )
                            except Exception as e:
                                logger.exception(f"Tool execution failed: {tool_name}")
                                tool_result = f"ツール実行エラー: {str(e)}"
                        else:
                            tool_result = f"ツール '{tool_name}' は認識されていません。"

                        # Add assistant response to history
                        messages.append({
                            "role": "model",
                            "parts": response.parts
                        })

                        # Add tool result to history
                        messages.append({
                            "role": "user",
                            "parts": [
                                genai.protos.Content.Part(
                                    function_response=genai.protos.FunctionResponse(
                                        name=tool_name,
                                        response={"result": str(tool_result)}
                                    )
                                )
                            ]
                        })
                        break

                # If no tool call, extract text and return
                if not has_tool_call:
                    answer_text = ""
                    for part in response.parts:
                        if hasattr(part, "text"):
                            answer_text += part.text
                    return answer_text.strip()

            except Exception as e:
                logger.exception(f"Generation failed on turn {turn}: {e}")
                if turn == max_turns - 1:
                    raise RuntimeError("Max tool call turns exceeded") from e
                await asyncio.sleep(2 ** turn)

        # If we exhausted turns without getting final answer, try one more time without tools
        try:
            messages[-1] = {"role": "user", "parts": [f"上記のツール呼び出しの結果を踏まえて、元の質問に対する最終的な回答を簡潔にまとめてください。"]}
            response = await asyncio.to_thread(
                self.model.generate_content,
                messages,
                generation_config=genai.types.GenerationConfig(temperature=0.2)
            )
            answer_text = ""
            for part in response.parts:
                if hasattr(part, "text"):
                    answer_text += part.text
            return answer_text.strip()
        except Exception as e:
            logger.exception("Final answer generation failed")
            return "ツール呼び出しが多すぎて最終的な回答を生成できませんでした。"

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
