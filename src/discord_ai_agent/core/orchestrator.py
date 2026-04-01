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
from typing import Any, Awaitable, Callable
from uuid import uuid4

import google.generativeai as genai

from discord_ai_agent.core.memory import ChannelMemoryStore, TaskCheckpointStore
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
        self.persona_memory_enabled = os.getenv("PERSONA_MEMORY_ENABLED", "true").strip().lower() == "true"
        self.persona_memory_include_in_prompt = (
            os.getenv("PERSONA_MEMORY_INCLUDE_IN_PROMPT", "true").strip().lower() == "true"
        )
        self.persona_memory_max_facts = int(os.getenv("PERSONA_MEMORY_MAX_FACTS", "200"))
        self.directional_memory_enabled = False
        self.personal_guild_id: int | None = None
        self.family_guild_ids: set[int] = set()

        self.max_concurrent_heavy_tasks = max(1, int(os.getenv("MAX_CONCURRENT_HEAVY_TASKS", "1")))
        self.heavy_task_timeout_sec = max(30, int(os.getenv("HEAVY_TASK_TIMEOUT_SEC", "180")))
        self._heavy_task_semaphore = asyncio.Semaphore(self.max_concurrent_heavy_tasks)
        self._queued_task_count = 0
        self._queued_task_lock = asyncio.Lock()

        checkpoint_path = os.getenv("CHECKPOINT_DB_PATH", "./data/runtime/checkpoints.sqlite3").strip()
        self._checkpoint_store: TaskCheckpointStore | None = None
        try:
            self._checkpoint_store = TaskCheckpointStore(checkpoint_path)
        except Exception:
            logger.exception("Failed to initialize checkpoint store: path=%s", checkpoint_path)

        genai.configure(api_key=config.gemini_api_key)
        self.model = genai.GenerativeModel(model_name=config.gemini_model)

    def configure_directional_memory_policy(
        self,
        *,
        enabled: bool,
        personal_guild_id: int | None,
        family_guild_ids: set[int],
    ) -> None:
        self.directional_memory_enabled = bool(enabled)
        self.personal_guild_id = personal_guild_id if personal_guild_id and personal_guild_id > 0 else None
        self.family_guild_ids = {gid for gid in family_guild_ids if gid > 0}

    def _resolve_retrieval_guild_ids(self, guild_id: int | None) -> list[int] | None:
        if guild_id is None:
            return None
        if not self.directional_memory_enabled:
            return [guild_id]
        if self.personal_guild_id is None:
            return [guild_id]

        # 個人サーバーのみ、身内サーバー群へ参照可能。逆方向と身内間参照は不可。
        if guild_id == self.personal_guild_id:
            ordered = [guild_id]
            for gid in sorted(self.family_guild_ids):
                if gid != guild_id:
                    ordered.append(gid)
            return ordered

        return [guild_id]

    async def answer(
        self,
        question: str,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        message_id: int | None,
    ) -> str:
        async def _job() -> str:
            return await self._answer_impl(
                question=question,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                message_id=message_id,
            )

        return await self._run_heavy_task("ask", _job)

    async def execute_tool_job(
        self,
        tool_name: str,
        args: dict[str, Any],
        task_label: str | None = None,
    ) -> str:
        label = task_label or f"tool:{tool_name}"

        async def _job() -> str:
            return await asyncio.to_thread(self.tool_registry.execute, tool_name, args)

        return await self._run_heavy_task(label, _job)

    async def _run_heavy_task(
        self,
        task_label: str,
        work: Callable[[], Awaitable[Any]],
    ) -> Any:
        queue_depth = 0
        async with self._queued_task_lock:
            self._queued_task_count += 1
            queue_depth = max(0, self._queued_task_count - self.max_concurrent_heavy_tasks)

        loop = asyncio.get_running_loop()
        wait_started = loop.time()

        try:
            async with self._heavy_task_semaphore:
                waited_sec = loop.time() - wait_started
                logger.info(
                    "Heavy task start: label=%s waited_sec=%.3f queue_depth=%s concurrency=%s",
                    task_label,
                    waited_sec,
                    queue_depth,
                    self.max_concurrent_heavy_tasks,
                )
                return await asyncio.wait_for(work(), timeout=self.heavy_task_timeout_sec)
        except asyncio.TimeoutError:
            logger.warning("Heavy task timed out: label=%s timeout=%s", task_label, self.heavy_task_timeout_sec)
            raise
        finally:
            async with self._queued_task_lock:
                self._queued_task_count = max(0, self._queued_task_count - 1)

    async def save_workflow_checkpoint(
        self,
        workflow: str,
        status: str,
        payload: dict[str, Any],
        job_id: str | None = None,
    ) -> str:
        effective_job_id = (job_id or "").strip() or f"{workflow}-{uuid4().hex[:12]}"
        if self._checkpoint_store is None:
            return effective_job_id
        try:
            await self._checkpoint_store.upsert_checkpoint(
                job_id=effective_job_id,
                workflow=workflow,
                status=status,
                payload=payload,
            )
        except Exception:
            logger.exception("Failed to save workflow checkpoint: workflow=%s job_id=%s", workflow, effective_job_id)
        return effective_job_id

    async def load_workflow_checkpoint(self, job_id: str) -> dict[str, Any] | None:
        if self._checkpoint_store is None:
            return None
        try:
            return await self._checkpoint_store.get_checkpoint(job_id)
        except Exception:
            logger.exception("Failed to load workflow checkpoint: job_id=%s", job_id)
            return None

    async def list_workflow_checkpoints(
        self,
        workflow: str,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if self._checkpoint_store is None:
            return []
        try:
            return await self._checkpoint_store.list_checkpoints(
                workflow=workflow,
                status=status,
                limit=limit,
            )
        except Exception:
            logger.exception("Failed to list workflow checkpoints: workflow=%s", workflow)
            return []

    async def _answer_impl(
        self,
        question: str,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        message_id: int | None,
    ) -> str:
        retrieval_guild_ids = self._resolve_retrieval_guild_ids(guild_id)
        if retrieval_guild_ids is not None and len(retrieval_guild_ids) > 1 and self.memory_scope == "guild":
            history_records = await self.memory.fetch_relevant_messages_multi_guild(
                guild_ids=retrieval_guild_ids,
                channel_id=channel_id,
                query_text=question,
                limit=self.memory_top_k,
            )
        else:
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
        persona_context = "(ユーザープロファイル未設定)"
        if self.persona_memory_enabled and self.persona_memory_include_in_prompt:
            try:
                facts = await self.memory.get_user_profile_facts(user_id=user_id, limit=self.persona_memory_max_facts)
                if facts:
                    lines = []
                    for fact in facts[:30]:
                        k = str(fact.get("key", "")).strip()
                        v = str(fact.get("value", "")).strip()
                        if not k or not v:
                            continue
                        lines.append(f"- {k}: {v}")
                    if lines:
                        persona_context = "\n".join(lines)
            except Exception:
                logger.exception("Failed to load persona profile for prompt")

        system_prompt = await self._build_system_prompt(history_context, persona_context)

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
            tool_names = self.tool_registry.tool_names()
            if action in tool_names:
                decision = {
                    "action": "tool",
                    "tool": action,
                    "args": decision.get("args", {}) if isinstance(decision.get("args", {}), dict) else {},
                    "reason": decision.get("reason", "fallback:action_as_tool_name"),
                }
                action = "tool"

            if action != "tool":
                alias_tool = str(decision.get("tool", "")).strip()
                if alias_tool in tool_names and not str(decision.get("action", "")).strip().lower() == "respond":
                    action = "tool"
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

                # Fallback for malformed model outputs:
                # {"action":"execute_internal_action","parameters":{...}}
                if not tool_name:
                    maybe_action = str(decision.get("action", "")).strip()
                    if maybe_action in tool_names:
                        tool_name = maybe_action
                    elif maybe_action:
                        tool_name = "execute_internal_action"
                        raw_payload = decision.get("parameters")
                        if not isinstance(raw_payload, dict):
                            raw_payload = decision.get("payload") if isinstance(decision.get("payload"), dict) else {}
                        args = {
                            "action": maybe_action,
                            "payload_json": json.dumps(raw_payload, ensure_ascii=False),
                        }
                if not isinstance(args, dict):
                    args = {}
                if not args:
                    for key in ("parameters", "payload", "params"):
                        candidate = decision.get(key)
                        if isinstance(candidate, dict):
                            args = candidate
                            break

                if not tool_name or not isinstance(args, dict):
                    scratchpad.append("[Agent] 不正なツール要求を検知したため無視しました。")
                    continue

                logger.info("Tool execution: %s (%s/%s)", tool_name, turn, self.max_tool_turns)
                tool_output = await asyncio.to_thread(self.tool_registry.execute, tool_name, args)
                preview = (tool_output or "").replace("\n", " ")[:180]
                logger.info(
                    "Tool result summary: tool=%s chars=%s preview=%s",
                    tool_name,
                    len(tool_output),
                    preview,
                )
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
        raw = await self._invoke_with_retry(
            prompt,
            max_output_tokens=420,
            response_mime_type="application/json",
        )
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
            "- 予定の追加/参照依頼（今月の予定、明日の予定、カレンダー確認等）では必ず execute_internal_action を使う\n"
            "- add_calendar_event は2方式を許可: 1) timed(start_time,end_time) 2) all_day(true)+date(YYYY-MM-DD)。終日指定時は時刻確認を要求しない\n"
            "- 入力が明確な場合（例: 面接 4月5日 00:00-23:59）は確認質問せず実行する。曖昧なときだけ追加質問する\n"
            "- 出力はJSONのみ\n"
            "- 形式1: {\"action\":\"tool\",\"tool\":\"...\",\"args\":{...},\"reason\":\"...\"}\n"
            "- 形式2: {\"action\":\"respond\",\"response\":\"...\"}\n\n"
            "[User Question]\n"
            f"{question}\n\n"
            "[Observed Tool Results]\n"
            f"{observation}"
        )
        raw = await self._invoke_with_retry(
            prompt,
            max_output_tokens=380,
            response_mime_type="application/json",
        )
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

    async def _invoke_with_retry(
        self,
        prompt: str,
        max_output_tokens: int = 2048,
        response_mime_type: str | None = None,
    ) -> str:
        retries = 2
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.model.generate_content,
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            **{
                                "temperature": 0.2,
                                "top_p": 0.95,
                                "top_k": 40,
                                "max_output_tokens": max_output_tokens,
                                **(
                                    {"response_mime_type": response_mime_type}
                                    if response_mime_type
                                    else {}
                                ),
                            }
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

    async def _build_system_prompt(self, history_context: str, persona_context: str) -> str:
        profile_text = await self._load_profile_text()
        static_profile = profile_text if profile_text else "(initial profileは未設定)"

        return (
            "あなたはDiscord上の個人向けAIアシスタントです。"
            "\n- 不明点は断定しない"
            "\n- 必要時のみツールを使う"
            "\n- 回答は簡潔かつ実用的にまとめる"
            "\n- ユーザーの追加指示待ちにならないよう、自律的に必要情報を補って回答する"
            "\n- ユーザー固有プロファイルがある場合は尊重し、矛盾時は最新の明示指示を優先する"
            "\n\n[Static Profile]\n"
            f"{static_profile}\n\n"
            "[User Persona Memory]\n"
            f"{persona_context}\n\n"
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
