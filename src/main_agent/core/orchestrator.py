"""Orchestrator using Google Generative AI SDK directly (not via LangChain)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import google.generativeai as genai
from google.generativeai.types import RequestOptions

from main_agent.core.memory import ChannelMemoryStore, MemoryRecord, TaskCheckpointStore
from tools.ai_exchange_logger import log_ai_exchange
from tools import ToolRegistry, build_default_tool_registry

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
        self.max_tool_turns = max(1, int(os.getenv("MAX_TOOL_TURNS", "1")))
        self.max_review_turns = int(os.getenv("MAX_REVIEW_TURNS", "0"))
        self.prompt_include_history_context = (
            os.getenv("PROMPT_INCLUDE_HISTORY_CONTEXT", "false").strip().lower() == "true"
        )
        self.prompt_include_persona_context = (
            os.getenv("PROMPT_INCLUDE_PERSONA_CONTEXT", "false").strip().lower() == "true"
        )
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
        self.ask_heavy_task_timeout_sec = max(
            self.heavy_task_timeout_sec,
            int(os.getenv("ASK_HEAVY_TASK_TIMEOUT_SEC", "900")),
        )
        self._heavy_task_semaphore = asyncio.Semaphore(self.max_concurrent_heavy_tasks)
        self._queued_task_count = 0
        self._queued_task_lock = asyncio.Lock()
        self.heavy_task_priority_enabled = os.getenv("HEAVY_TASK_PRIORITY_ENABLED", "true").strip().lower() == "true"
        self.heavy_task_priority_poll_ms = max(10, int(os.getenv("HEAVY_TASK_PRIORITY_POLL_MS", "50")))
        self._priority_waiting: dict[str, int] = {"high": 0, "low": 0}

        checkpoint_path = os.getenv("CHECKPOINT_DB_PATH", "./data/runtime/checkpoints.sqlite3").strip()
        self._checkpoint_store: TaskCheckpointStore | None = None
        try:
            self._checkpoint_store = TaskCheckpointStore(checkpoint_path)
        except Exception:
            logger.exception("Failed to initialize checkpoint store: path=%s", checkpoint_path)

        self.last_tool_executions: list[dict[str, object]] = []

        self.gemini_max_requests_per_min = max(1, int(os.getenv("GEMINI_MAX_REQUESTS_PER_MIN", "12")))
        self._gemini_request_timestamps: deque[float] = deque()
        self._gemini_rate_limit_lock = asyncio.Lock()
        self._gemini_inflight_lock = asyncio.Lock()

        genai.configure(api_key=config.gemini_api_key)
        self.model = genai.GenerativeModel(model_name=config.gemini_model)
        self.gemini_503_fallback_model = (
            os.getenv("GEMINI_503_FALLBACK_MODEL", "gemma-4-31b-it").strip()
            or "gemma-4-31b-it"
        )
        self.gemini_503_fallback_cooldown_sec = max(
            30,
            int(os.getenv("GEMINI_503_FALLBACK_COOLDOWN_SEC", "300")),
        )
        self._prefer_fallback_until_monotonic = 0.0
        self._last_model_used_name = config.gemini_model
        self._fallback_model: Any | None = None
        if self.gemini_503_fallback_model != config.gemini_model:
            try:
                self._fallback_model = genai.GenerativeModel(model_name=self.gemini_503_fallback_model)
            except Exception:
                logger.exception(
                    "Failed to initialize 503 fallback model: %s",
                    self.gemini_503_fallback_model,
                )

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

        answer_text = await self._run_heavy_task("ask", _job)
        log_ai_exchange(
            component="main-agent",
            model=self.config.gemini_model,
            prompt=question,
            response=answer_text,
            metadata={
                "phase": "final_answer",
                "guild_id": guild_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "message_id": message_id,
            },
        )
        return answer_text

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
        priority = self._classify_task_priority(task_label)
        queue_depth = 0
        async with self._queued_task_lock:
            self._queued_task_count += 1
            queue_depth = max(0, self._queued_task_count - self.max_concurrent_heavy_tasks)
            self._priority_waiting[priority] = self._priority_waiting.get(priority, 0) + 1

        loop = asyncio.get_running_loop()
        wait_started = loop.time()
        entered_worker = False
        waiting_registered = True

        try:
            # Keep a single execution lane, but prefer short tasks when they are queued.
            if self.heavy_task_priority_enabled and priority == "low":
                while True:
                    async with self._queued_task_lock:
                        high_waiting = int(self._priority_waiting.get("high", 0))
                    if high_waiting <= 0:
                        break
                    await asyncio.sleep(self.heavy_task_priority_poll_ms / 1000.0)

            async with self._heavy_task_semaphore:
                entered_worker = True
                if waiting_registered:
                    async with self._queued_task_lock:
                        self._priority_waiting[priority] = max(0, int(self._priority_waiting.get(priority, 0)) - 1)
                    waiting_registered = False
                waited_sec = loop.time() - wait_started
                logger.info(
                    "Heavy task start: label=%s priority=%s waited_sec=%.3f queue_depth=%s concurrency=%s",
                    task_label,
                    priority,
                    waited_sec,
                    queue_depth,
                    self.max_concurrent_heavy_tasks,
                )
                timeout_sec = self.ask_heavy_task_timeout_sec if task_label == "ask" else self.heavy_task_timeout_sec
                return await asyncio.wait_for(work(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            timeout_sec = self.ask_heavy_task_timeout_sec if task_label == "ask" else self.heavy_task_timeout_sec
            logger.warning("Heavy task timed out: label=%s timeout=%s", task_label, timeout_sec)
            raise
        finally:
            async with self._queued_task_lock:
                self._queued_task_count = max(0, self._queued_task_count - 1)
                if waiting_registered and not entered_worker:
                    self._priority_waiting[priority] = max(0, int(self._priority_waiting.get(priority, 0)) - 1)

    def _classify_task_priority(self, task_label: str) -> str:
        label = (task_label or "").strip().lower()
        # Long-running asks and deep research should yield to lightweight utility tasks.
        if label == "ask" or label.startswith("deepdive:research"):
            return "low"
        if label.startswith("research_status:"):
            return "high"
        if label.startswith("mention_quick:"):
            return "high"
        if label.startswith("tool:"):
            return "high"
        return "high"

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
        retrieval_question = self._strip_runtime_hints(question)
        if not retrieval_question:
            retrieval_question = (question or "").strip()

        is_explicit_global_query = self._is_explicit_global_source_query(retrieval_question)
        has_followup_marker = self._has_followup_marker(retrieval_question)
        recall_intent = self._is_history_recall_query(retrieval_question)
        retrieval_limit = max(self.memory_top_k, 24) if recall_intent else self.memory_top_k
        retrieval_scope = "guild" if recall_intent else self.memory_scope
        # Keep prompt payload minimal by default; only include history when recall/follow-up needs it.
        use_history_context = bool(recall_intent or has_followup_marker)
        if is_explicit_global_query and not has_followup_marker:
            use_history_context = False
            retrieval_scope = "disabled(explicit_global_query)"
            self._log_ai_thought(
                stage="memory_context_policy",
                action="skip_history_context",
                reason="explicit_global_source_query_without_followup",
                question=retrieval_question,
            )
        elif (
            not has_followup_marker
            and not recall_intent
            and self._is_general_knowledge_query(retrieval_question)
        ):
            use_history_context = False
            retrieval_scope = "disabled(general_knowledge_query)"
            self._log_ai_thought(
                stage="memory_context_policy",
                action="skip_history_context",
                reason="general_knowledge_query",
                question=retrieval_question,
            )

        if use_history_context:
            retrieval_guild_ids = self._resolve_retrieval_guild_ids(guild_id)
            if retrieval_guild_ids is not None and len(retrieval_guild_ids) > 1 and retrieval_scope == "guild":
                history_records = await self.memory.fetch_relevant_messages_multi_guild(
                    guild_ids=retrieval_guild_ids,
                    channel_id=channel_id,
                    query_text=retrieval_question,
                    limit=retrieval_limit,
                )
            else:
                history_records = await self.memory.fetch_relevant_messages(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    query_text=retrieval_question,
                    limit=retrieval_limit,
                    scope=retrieval_scope,
                )
        else:
            history_records = []
        if recall_intent:
            history_records = self._rerank_records_for_recall(
                question=retrieval_question,
                records=history_records,
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
            retrieval_scope,
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
        if self.prompt_include_persona_context and self.persona_memory_enabled and self.persona_memory_include_in_prompt:
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

        prompt_history_context = history_context if self.prompt_include_history_context else "(省略)"
        prompt_persona_context = persona_context if self.prompt_include_persona_context else "(省略)"
        system_prompt = await self._build_system_prompt(prompt_history_context, prompt_persona_context)

        try:
            answer_text = await self._generate_with_tools(system_prompt, question)
            if not answer_text.strip():
                answer_text = "回答を生成できませんでした。質問を少し変えて再試行してください。"
            answer_text = self._sanitize_user_facing_error_phrases(answer_text)
            if self._last_model_is_gemma() and self._looks_like_internal_prompt_leak(answer_text):
                logger.warning("Detected internal prompt-leak style output; attempting to rescue actual response")
                rescued = self._extract_safe_response_from_leak(answer_text)
                if rescued:
                    answer_text = rescued
                else:
                    answer_text = "内部整形で問題が発生したため、この依頼は実行結果を確定できませんでした。もう一度依頼してください。"
            if self.memory_response_include_evidence:
                answer_text = self._append_memory_evidence(answer_text, history_records)
        except RuntimeError as exc:
            logger.warning("LLM invocation failed without crashing response: %s", str(exc)[:300])
            answer_text = "現在AI応答で一時的な問題が発生しています。少し時間をおいて再試行してください。"
        except Exception:
            logger.exception("LLM invocation failed")
            answer_text = "現在AI応答で問題が発生しています。時間をおいて再試行してください。"

        await self._store_conversation(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            question=retrieval_question,
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
        runtime_question = question
        executed_research_signatures: set[str] = set()

        base_question = self._strip_runtime_hints(runtime_question)
        has_followup_marker = self._has_followup_marker(base_question)
        if self._is_underspecified_external_research_query(base_question):
            if has_followup_marker:
                followup_topic = self._extract_followup_topic_from_recent_context(runtime_question)
                if followup_topic:
                    runtime_question = (
                        runtime_question.rstrip()
                        + "\n\n[Resolved Follow-up Topic]\n"
                        + f"- previous_topic: {followup_topic}"
                    )
                    self._log_ai_thought(
                        stage="followup_resolution",
                        mode="inherit_previous_topic",
                        previous_topic=followup_topic,
                    )
                else:
                    self._log_ai_thought(
                        stage="followup_resolution",
                        mode="clarify_required",
                        reason="followup_marker_detected_without_recent_topic",
                    )
                    return (
                        "調査対象が曖昧です。どの対象の最新議論を調べるか指定してください。"
                        "（例: oithxs/yorimichi, Claude Code, あるいはURL）"
                    )
            else:
                self._log_ai_thought(
                    stage="followup_resolution",
                    mode="standalone",
                    reason="underspecified_without_followup_marker",
                )

        for turn in range(1, self.max_tool_turns + 1):
            decision = await self._decide_next_action(
                system_prompt=system_prompt,
                question=runtime_question,
                now_jst=now_jst,
                scratchpad=scratchpad,
                turn=turn,
            )
            decisions = decision if isinstance(decision, list) else [decision]
            if not decisions:
                decisions = [{}]

            tool_names = self.tool_registry.tool_names()
            force_tool_decision: dict[str, Any] | None = None
            if len(decisions) == 1 and isinstance(decisions[0], dict):
                single_decision = decisions[0]
                action = str(single_decision.get("action", "respond")).strip().lower()
                if action in tool_names:
                    single_decision = {
                        "action": "tool",
                        "tool": action,
                        "args": single_decision.get("args", {}) if isinstance(single_decision.get("args", {}), dict) else {},
                        "reason": single_decision.get("reason", "fallback:action_as_tool_name"),
                    }
                    action = "tool"

                if action != "tool":
                    alias_tool = str(single_decision.get("tool", "")).strip()
                    if alias_tool in tool_names and not str(single_decision.get("action", "")).strip().lower() == "respond":
                        action = "tool"

                if self._should_force_research_job(
                    question=runtime_question,
                    turn=turn,
                    action=action,
                    scratchpad=scratchpad,
                ):
                    forced_source = self._infer_research_source_from_question(runtime_question)
                    forced_topic = self._resolve_research_topic(runtime_question)
                    force_tool_decision = {
                        "action": "tool",
                        "tool": "dispatch_research_job",
                        "args": {
                            "topic": forced_topic,
                            "source": forced_source,
                            "mode": "auto",
                        },
                        "reason": "guard:external_research_intent",
                    }
                    action = "tool"
                    self._log_ai_thought(
                        stage="force_dispatch_research_job",
                        source=forced_source,
                        topic=forced_topic,
                        reason="guard_external_research_intent",
                    )
                    single_decision = force_tool_decision

                logger.info(
                    "Agent decision: turn=%s action=%s tool=%s reason=%s",
                    turn,
                    action,
                    str(single_decision.get("tool", "")),
                    str(single_decision.get("reason", ""))[:180],
                )
                self._log_ai_thought(
                    stage="decision",
                    turn=turn,
                    action=action,
                    tool=str(single_decision.get("tool", "")),
                    reason=str(single_decision.get("reason", ""))[:180],
                )

                if action == "tool":
                    decisions = [single_decision]

            executed_any_tool = False
            pending_response: str | None = None

            for raw_decision in decisions:
                if not isinstance(raw_decision, dict):
                    continue

                action = str(raw_decision.get("action", "respond")).strip().lower()
                if action in tool_names:
                    raw_decision = {
                        "action": "tool",
                        "tool": action,
                        "args": raw_decision.get("args", {}) if isinstance(raw_decision.get("args", {}), dict) else {},
                        "reason": raw_decision.get("reason", "fallback:action_as_tool_name"),
                    }
                    action = "tool"

                if action != "tool":
                    alias_tool = str(raw_decision.get("tool", "")).strip()
                    if alias_tool in tool_names and not str(raw_decision.get("action", "")).strip().lower() == "respond":
                        action = "tool"

                if action != "tool":
                    response = str(raw_decision.get("response", "")).strip()
                    if response:
                        pending_response = response
                    continue

                tool_name = str(raw_decision.get("tool", "")).strip()
                args = raw_decision.get("args", {})

                # Fallback for malformed model outputs:
                # {"action":"execute_internal_action","parameters":{...}}
                if not tool_name:
                    maybe_action = str(raw_decision.get("action", "")).strip()
                    if maybe_action in tool_names:
                        tool_name = maybe_action
                    elif maybe_action:
                        tool_name = "execute_internal_action"
                        raw_payload = raw_decision.get("parameters")
                        if not isinstance(raw_payload, dict):
                            raw_payload = raw_decision.get("payload") if isinstance(raw_decision.get("payload"), dict) else {}
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

                if tool_name == "dispatch_research_job":
                    # Always wait for research completion to avoid premature user-facing answers.
                    args["wait"] = "true"
                    clean_runtime_question = self._strip_runtime_hints(runtime_question)
                    if (
                        self._is_explicit_global_source_query(clean_runtime_question)
                        and not self._has_followup_marker(clean_runtime_question)
                    ):
                        guarded_topic = self._resolve_research_topic(clean_runtime_question)
                        guarded_source = self._infer_research_source_from_question(clean_runtime_question)
                        args["topic"] = guarded_topic
                        args["source"] = guarded_source
                        self._log_ai_thought(
                            stage="dispatch_topic_guard",
                            action="override_topic_source",
                            topic=guarded_topic,
                            source=guarded_source,
                            reason="explicit_global_source_query_without_followup",
                        )

                    explicit_timeout = self._extract_timeout_from_research_controls(question)
                    if explicit_timeout is None:
                        explicit_timeout = self._extract_timeout_from_user_text(question)
                    requested_timeout = str(args.get("timeout_sec", "")).strip()
                    if explicit_timeout is not None:
                        args["timeout_sec"] = explicit_timeout
                    else:
                        args.pop("timeout_sec", None)
                    logger.info(
                        "dispatch_research_job timeout policy: explicit=%s requested=%s effective=%s",
                        explicit_timeout is not None,
                        requested_timeout or "(none)",
                        str(args.get("timeout_sec", "")).strip() or "(default)",
                    )
                    logger.info(
                        "[route] main-agent -> research-agent tool=%s topic=%s source=%s mode=%s timeout_sec=%s",
                        tool_name,
                        str(args.get("topic", ""))[:160],
                        str(args.get("source", "")),
                        str(args.get("mode", "")),
                        str(args.get("timeout_sec", "")).strip() or "(default)",
                    )
                    signature = self._research_dispatch_signature(args)
                    if signature in executed_research_signatures and scratchpad:
                        self._log_ai_thought(
                            stage="duplicate_dispatch_guard",
                            turn=turn,
                            signature=signature,
                            action="compose_final_response",
                        )
                        composed = await self._compose_final_response(
                            system_prompt=system_prompt,
                            question=runtime_question,
                            now_jst=now_jst,
                            scratchpad=scratchpad,
                        )
                        reviewed = await self._self_review_response(
                            system_prompt=system_prompt,
                            question=runtime_question,
                            response=composed,
                            scratchpad=scratchpad,
                        )
                        return self._ensure_sources_in_answer(reviewed, scratchpad)
                    executed_research_signatures.add(signature)

                logger.info("Tool execution: %s (%s/%s)", tool_name, turn, self.max_tool_turns)
                logger.info(
                    "[route] main-agent -> tool tool=%s turn=%s args=%s",
                    tool_name,
                    turn,
                    json.dumps(args, ensure_ascii=False)[:500],
                )
                tool_output = await asyncio.to_thread(self.tool_registry.execute, tool_name, args)
                preview = (tool_output or "").replace("\n", " ")[:180]
                logger.info(
                    "Tool result summary: tool=%s chars=%s preview=%s",
                    tool_name,
                    len(tool_output),
                    preview,
                )
                self.last_tool_executions.append({
                    "tool": tool_name,
                    "output": tool_output,
                })
                scratchpad.append(
                    f"[Tool:{tool_name}] args={json.dumps(args, ensure_ascii=False)}\n{tool_output}"
                )
                executed_any_tool = True

            if executed_any_tool:
                # Keep request fan-out bounded: one decision call + one final response call.
                composed_after_tool = await self._compose_final_response(
                    system_prompt=system_prompt,
                    question=runtime_question,
                    now_jst=now_jst,
                    scratchpad=scratchpad,
                )
                reviewed_after_tool = await self._self_review_response(
                    system_prompt=system_prompt,
                    question=runtime_question,
                    response=composed_after_tool,
                    scratchpad=scratchpad,
                )
                return self._ensure_sources_in_answer(reviewed_after_tool, scratchpad)

            if pending_response is not None:
                logger.info("[route] main-agent -> respond turn=%s", turn)
                if self._is_nonfinal_response(pending_response) or (
                    self._last_model_is_gemma() and self._looks_like_internal_prompt_leak(pending_response)
                ):
                    rescued = self._extract_safe_response_from_leak(pending_response) if self._looks_like_internal_prompt_leak(pending_response) else ""
                    if rescued and not self._is_nonfinal_response(rescued):
                        pending_response = rescued
                    else:
                        had_tool_context = bool(scratchpad)
                        scratchpad.append(f"[Agent] 非最終応答を検知: {pending_response[:120]}")
                        if had_tool_context:
                            continue
                    fallback_response = await self._compose_final_response(
                        system_prompt=system_prompt,
                        question=runtime_question,
                        now_jst=now_jst,
                        scratchpad=scratchpad,
                    )
                    fallback_reviewed = await self._self_review_response(
                        system_prompt=system_prompt,
                        question=runtime_question,
                        response=fallback_response,
                        scratchpad=scratchpad,
                    )
                    return self._ensure_sources_in_answer(fallback_reviewed, scratchpad)
                reviewed = await self._self_review_response(
                    system_prompt=system_prompt,
                    question=runtime_question,
                    response=pending_response,
                    scratchpad=scratchpad,
                )
                return self._ensure_sources_in_answer(reviewed, scratchpad)

            response = str((decision[0] if isinstance(decision, list) and decision else decision).get("response", "") if isinstance(decision, dict) or (isinstance(decision, list) and decision and isinstance(decision[0], dict)) else "").strip()
            if response:
                logger.info("[route] main-agent -> respond turn=%s", turn)
                if self._is_nonfinal_response(response) or (
                    self._last_model_is_gemma() and self._looks_like_internal_prompt_leak(response)
                ):
                    rescued = self._extract_safe_response_from_leak(response) if self._looks_like_internal_prompt_leak(response) else ""
                    if rescued and not self._is_nonfinal_response(rescued):
                        response = rescued
                    else:
                        had_tool_context = bool(scratchpad)
                        scratchpad.append(f"[Agent] 非最終応答を検知: {response[:120]}")
                        if had_tool_context:
                            continue
                    fallback_response = await self._compose_final_response(
                        system_prompt=system_prompt,
                        question=runtime_question,
                        now_jst=now_jst,
                        scratchpad=scratchpad,
                    )
                    fallback_reviewed = await self._self_review_response(
                        system_prompt=system_prompt,
                        question=runtime_question,
                        response=fallback_response,
                        scratchpad=scratchpad,
                    )
                    return self._ensure_sources_in_answer(fallback_reviewed, scratchpad)
                reviewed = await self._self_review_response(
                    system_prompt=system_prompt,
                    question=runtime_question,
                    response=response,
                    scratchpad=scratchpad,
                )
                return self._ensure_sources_in_answer(reviewed, scratchpad)

        composed = await self._compose_final_response(
            system_prompt=system_prompt,
            question=runtime_question,
            now_jst=now_jst,
            scratchpad=scratchpad,
        )
        reviewed = await self._self_review_response(
            system_prompt=system_prompt,
            question=runtime_question,
            response=composed,
            scratchpad=scratchpad,
        )
        return self._ensure_sources_in_answer(reviewed, scratchpad)

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
            self._log_ai_thought(
                stage="self_review",
                turn=review_turn,
                action=action,
            )

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
                    self.last_tool_executions.append({
                        "tool": tool_name,
                        "output": tool_output,
                    })
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
        strict_json_line = (
            "思考過程・補足説明・Markdownを出力せず、JSONオブジェクト1つだけを返す。\n"
            if self._is_gemma_model()
            else ""
        )
        prompt = (
            f"{system_prompt}\n\n"
            "[Reviewer Role]\n"
            "あなたは回答品質レビュー担当です。以下の回答を評価し、必要なら修正してください。\n"
            "出力はJSONのみ。\n"
            f"{strict_json_line}"
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
            response_mime_type=None if self._is_gemma_model() else "application/json",
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
        strict_json_line = (
            "思考過程・補足説明・Markdownを出力せず、JSONオブジェクト1つだけを返す。"
            if self._is_gemma_model()
            else ""
        )
        strict_json_policy_line = f"- {strict_json_line}\n" if strict_json_line else ""
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
            "- 予定/タスク追加依頼では必ず execute_internal_action を使う\n"
            "- プロンプト内に [Research Controls] ブロックがあれば、dispatch_research_job を優先して使う（ユーザーが調査を明示指示の証）\n"
            "- 重い調査（比較、深掘り、複数観点の調査、長文レポート要求）は dispatch_research_job を優先して使う\n"
            "- ユーザーが『deepdiveして』『深掘り調査して』『調べて』『調べてきて』等を明示した場合は dispatch_research_job を優先する\n"
            "- ユーザーが Gemini CLI 利用を明示した場合、dispatch_research_job の mode に gemini_cli を設定する\n"
            "- ユーザーがフォールバック/非Geminiを明示した場合、dispatch_research_job の mode に fallback を設定する\n"
            "- ユーザーが調査時間（秒/分）を指定した場合、dispatch_research_job の timeout_sec に秒換算した値を設定する\n"
            "- timeout_sec はユーザー指定値をそのまま使い、増減しない（例: 1分=60, 2分=120）\n"
            "- 区別ルール: 「タスク」「TODO」「やること」等の明示キーワード→add_task、「予定」「会議」「面接」等→add_calendar_event\n"
            "- add_calendar_event は2方式を許可: 1) timed(start_time,end_time) 2) all_day(true)+date(YYYY-MM-DD)\n"
            "- add_task は必須: title, optional: due_date(YYYY-MM-DD)\n"
            "- カレンダー/タスク操作での確認ルール: タイトル+日付（+時刻/終日指定）が揃っていれば、「〜してよろしいでしょうか」などの確認メッセージ不可。直ちに execute_internal_action を呼び出す\n"
            "- ユーザーが「実行して」「登録して」などの明示的指示をした場合、信頼度に関わらず躊躇なく tool 呼び出し実行\n"
            "- 入力が曖昧（例: 「来週のどこか」「時間未定」）な場合のみ追加質問する\n"
            "- 出力はJSONのみ\n"
            f"{strict_json_policy_line}"
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
            response_mime_type=None if self._is_gemma_model() else "application/json",
        )
        parsed = self._extract_json_object(raw)
        if parsed:
            return parsed
        return {
            "action": "respond",
            "response": "回答の内部整形で一時的な問題が発生しました。再構成して続行します。",
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
            "[Final Response Policy]\n"
            "- 結論を先に書く（簡潔・実用的）\n"
            "- 参照したURLがある場合は必ず最後に [参考URL] セクションとして列挙する\n"
            "- カレンダー/タスク操作完了時: 実行完了を伝え、詳細（日時、タイトル等）は簡潔に記載\n"
            "- 確認質問や了解待ちレスポンス（「よろしいですか？」「許可しますか？」等）は**絶対に出さない**\n"
            "- ツール実行エラー時のみ、簡潔なエラー説明+再試行オプション提示\n"
            "- 不足情報が必須（例: 不可解な入力）な場合のみ、簡潔に質問する\n\n"
            "[User Question]\n"
            f"{question}\n\n"
            "[Tool Results]\n"
            f"{observation}"
        )
        return await self._invoke_with_retry(prompt)

    def _extract_urls_from_text(self, text: str) -> list[str]:
        if not text:
            return []
        urls = re.findall(r"https?://[^\s\]\)\">]+", text)
        seen: set[str] = set()
        out: list[str] = []
        for url in urls:
            clean = self._normalize_extracted_url(url)
            if not clean:
                continue
            if clean in seen:
                continue
            seen.add(clean)
            out.append(clean)
        return out

    @staticmethod
    def _normalize_extracted_url(url: str) -> str:
        clean = (url or "").strip()
        if not clean:
            return ""
        for marker in ("\\n", "/n", "\n"):
            idx = clean.find(marker)
            if idx >= 0:
                clean = clean[:idx]
        clean = clean.rstrip(".,;)-")
        if not clean.startswith("http://") and not clean.startswith("https://"):
            return ""
        return clean

    def _collect_urls_from_scratchpad(self, scratchpad: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in scratchpad:
            for url in self._extract_urls_from_text(item):
                if url in seen:
                    continue
                seen.add(url)
                out.append(url)
        return out

    def _ensure_sources_in_answer(self, answer_text: str, scratchpad: list[str]) -> str:
        body = (answer_text or "").strip()
        urls = self._collect_urls_from_scratchpad(scratchpad)
        if not urls:
            return body
        if "参考URL" in body or "[参考にした情報源]" in body:
            return body
        src = "\n".join(f"- {u}" for u in urls[:12])
        return body + "\n\n[参考URL]\n" + src

    @staticmethod
    def _is_nonfinal_response(text: str) -> bool:
        body = (text or "").strip()
        if not body:
            return True
        patterns = [
            "回答を組み立て中に形式エラーが発生しました",
            "回答の内部整形で一時的な問題が発生しました",
            "質問を短くして再試行してください",
            "回答を生成できませんでした",
            "現在AI応答で問題が発生しています",
        ]
        return any(p in body for p in patterns)

    def _last_model_is_gemma(self) -> bool:
        return (self._last_model_used_name or "").strip().lower().startswith("gemma")

    def _looks_like_internal_prompt_leak(self, text: str) -> bool:
        body = (text or "").strip()
        if not body:
            return False
            
        # Gemma が内部プロンプトの見出しをそのまま出力し始めた場合のキーワード
        markers = [
            "Discord Personal AI Assistant",
            "[Final Response Policy]",
            "[Tool Results]",
            "The user wants to",
            "Self-correction",
            "Draft 1",
            "Draft 2",
            "Don't be assertive about unknowns",
            "Role:",
            "User Question:",
            "Tool Result:",
            "Constraints:",
            "Action:",
            "Response Draft:",
            "Conclusion first?",
            "Concise/Practical?",
            "No redundant introductions",
        ]
        
        # 1つでもあれば怪しいが、誤検知を防ぐため「複数ヒット(2つ以上)」または「特定決定的フレーズ」で判定
        hits = [m for m in markers if m in body]
        
        # 「Role:」と「Response Draft:」が同時にあればほぼ確実にリーク
        if "Role:" in hits and ("Response Draft:" in hits or "Action:" in hits):
            return True
            
        # それ以外でも3つ以上マーカーがあればアウト
        return len(hits) >= 3

    def _extract_safe_response_from_leak(self, text: str) -> str:
        """リークされた出力からユーザー向けの本来の回答（Response Draft など）を抽出して救済する"""
        body = (text or "").strip()
        if not body:
            return ""
        
        # 典型的なマーカーの後ろにある文章を抽出する
        markers = [
            "Response Draft:",
            "Draft 2:",
            "Draft 1:",
            "Draft:",
            "回答:"
        ]
        
        for marker in markers:
            idx = body.rfind(marker)
            if idx != -1:
                extracted = body[idx + len(marker):].strip()
                # 抽出した部分が短すぎなければ採用
                if len(extracted) > 5:
                    return extracted
        
        return ""

    @staticmethod
    def _sanitize_user_facing_error_phrases(text: str) -> str:
        body = (text or "").strip()
        if not body:
            return body
        body = body.replace(
            "回答を組み立て中に形式エラーが発生しました。質問を短くして再試行してください。",
            "回答の内部整形で一時的な問題が発生しました。しばらく待って再試行してください。",
        )
        body = body.replace(
            "回答の内部整形で一時的な問題が発生しました。再構成して続行します。",
            "回答の内部整形で一時的な問題が発生しました。しばらく待って再試行してください。",
        )
        body = body.replace(
            "質問を短くして再試行してください",
            "しばらく待って再試行してください",
        )
        return body

    @staticmethod
    def _extract_timeout_from_research_controls(question: str) -> str | None:
        text = question or ""
        if "[Research Controls]" not in text:
            return None
        match = re.search(r"^\s*-\s*timeout_sec:\s*(\d{1,4})\s*$", text, flags=re.MULTILINE)
        if match is None:
            return None
        try:
            value = int(match.group(1))
        except Exception:
            return None
        if value < 10 or value > 1800:
            return None
        return str(value)

    @staticmethod
    def _extract_timeout_from_user_text(question: str) -> str | None:
        text = DiscordOrchestrator._strip_runtime_hints(question)
        if not text:
            return None

        digit_map = str.maketrans("０１２３４５６７８９", "0123456789")
        normalized = text.translate(digit_map).lower()

        sec_match = re.search(r"(\d{1,4})\s*(?:秒(?:間)?|sec|secs|second|seconds)", normalized)
        min_match = re.search(r"(\d{1,3})\s*(?:分(?:間)?|min|mins|minute|minutes)", normalized)

        value: int | None = None
        if sec_match is not None:
            value = int(sec_match.group(1))
        elif min_match is not None:
            value = int(min_match.group(1)) * 60

        if value is None:
            return None
        value = max(10, min(value, 1800))
        return str(value)

    @staticmethod
    def _strip_runtime_hints(question: str) -> str:
        text = (question or "").strip()
        if not text:
            return ""

        markers = [
            "\n\n[Research Controls]",
            "\n[Research Controls]",
            "\n\n[Recent Conversation]",
            "\n[Recent Conversation]",
        ]
        cut_positions = [text.find(m) for m in markers if text.find(m) >= 0]
        if cut_positions:
            text = text[: min(cut_positions)].strip()

        text = re.sub(r"\n?-\s*『さっき』.*$", "", text).strip()
        return text

    @staticmethod
    def _is_history_recall_query(question: str) -> bool:
        text = (question or "").strip().lower()
        if not text:
            return False
        patterns = [
            r"さっき",
            r"先ほど",
            r"前回",
            r"前に",
            r"何について",
            r"何と言",
            r"何て言",
            r"言っていた",
            r"言ってた",
            r"覚えて",
            r"会話履歴",
            r"会話ログ",
            r"履歴ログ",
        ]
        return any(re.search(p, text) for p in patterns)

    @staticmethod
    def _should_force_research_job(
        question: str,
        turn: int,
        action: str,
        scratchpad: list[str],
    ) -> bool:
        if turn != 1 or action == "tool" or scratchpad:
            return False

        text = DiscordOrchestrator._strip_runtime_hints(question).lower()
        if not text:
            return False

        source_terms = (
            "agent-reach",
            "agent reach",
            "api",
            "gemini",
            "kubernetes",
            "docker",
            "python",
            "discord",
            "google",
            "calendar",
            "tasks",
            "notion",
            "x.com",
            "twitter",
            "ツイッター",
            "tweet",
            "youtube",
            "動画",
            "github",
            "issue",
            "release",
            "reddit",
            "subreddit",
            "news",
            "フォーラム",
            "コミュニティ",
            "sns",
        )
        research_terms = (
            "調べ",
            "調査",
            "深掘り",
            "比較",
            "分析",
            "まとめ",
            "動向",
            "反応",
            "議論",
            "最新",
            "トレンド",
            "評判",
            "口コミ",
            "出典",
            "ソース",
            "対策",
            "解決",
            "実例",
            "事例",
            "失敗",
            "失敗例",
            "運用",
            "方法",
            "手順",
            "導入",
            "違い",
        )

        has_source_term = any(term in text for term in source_terms)
        has_research_term = any(term in text for term in research_terms)
        if has_source_term and has_research_term:
            return True

        # Entity lookup intent (e.g., "yorimichiについて教えて") should be grounded with tools.
        if re.search(r"[a-z0-9][a-z0-9_.-]{2,}\s*(?:について教えて|とは|って何|を教えて)", text):
            if not re.search(r"(todo|to-do|to do|タスク|予定|会議|面接|登録|追加|実行)", text):
                return True

        return False

    @staticmethod
    def _research_dispatch_signature(args: dict[str, Any]) -> str:
        source = str(args.get("source", "")).strip().lower()
        mode = str(args.get("mode", "")).strip().lower()
        topic = str(args.get("topic", "")).strip().lower()
        topic = re.sub(r"\s+", " ", topic)
        return f"{source}|{mode}|{topic}"

    @staticmethod
    def _truncate_log_value(value: Any, max_len: int = 180) -> str:
        text = str(value or "").replace("\n", " ").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _log_ai_thought(self, stage: str, **fields: Any) -> None:
        parts: list[str] = []
        for key, value in fields.items():
            trimmed = self._truncate_log_value(value)
            if not trimmed:
                continue
            parts.append(f"{key}={trimmed}")
        detail = " ".join(parts)
        if detail:
            logger.info("[AI_THOUGHT] stage=%s %s", stage, detail)
        else:
            logger.info("[AI_THOUGHT] stage=%s", stage)

    @staticmethod
    def _infer_research_source_from_question(question: str) -> str:
        text = DiscordOrchestrator._strip_runtime_hints(question).lower()
        if any(term in text for term in ("x.com", "twitter", "ツイッター", "tweet")):
            return "x"
        if any(term in text for term in ("youtube", "動画")):
            return "youtube"
        if any(term in text for term in ("github", "issue", "release", "pull request", "pr")):
            return "github"
        if any(term in text for term in ("reddit", "subreddit")):
            return "reddit"
        return "auto"

    @staticmethod
    def _has_followup_marker(question: str) -> bool:
        text = (question or "").strip().lower()
        if not text:
            return False
        text = re.sub(r"「[^」]*」|『[^』]*』|\"[^\"]*\"", "", text)
        markers = (
            "それ",
            "その",
            "この件",
            "これ",
            "あれ",
            "同じ",
            "続き",
            "前の",
            "先ほど",
            "さっき",
            "さきほど",
            "つまり",
            "要するに",
            "っていうと",
            "ってこと",
            "それって",
            "そっち",
            "あっち",
            "こっち",
            "さっき",
            "前回",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_underspecified_external_research_query(question: str) -> bool:
        text = (question or "").strip().lower()
        if not text:
            return False

        if DiscordOrchestrator._is_explicit_global_source_query(text):
            return False

        source_terms = (
            "github",
            "x.com",
            "twitter",
            "ツイッター",
            "youtube",
            "reddit",
            "コミュニティ",
            "フォーラム",
            "sns",
        )
        research_terms = (
            "調べ",
            "調査",
            "議論",
            "動向",
            "反応",
            "最新",
            "トレンド",
            "要点",
            "まとめ",
            "比較",
            "分析",
        )
        if not any(term in text for term in source_terms):
            return False
        if not any(term in text for term in research_terms):
            return False

        if re.search(
            r"(github|twitter|youtube|reddit|x\.com|ツイッター)(?:上)?(?:の)?(?:最新)?(?:議論|動向|反応)",
            text,
        ):
            if not re.search(r"https?://", text) and not re.search(r"[a-z0-9_.-]+/[a-z0-9_.-]+", text):
                return True

        if re.search(r"https?://", text):
            return False
        if re.search(r"[a-z0-9_.-]+/[a-z0-9_.-]+", text):
            return False

        generic_tokens = {
            "github", "twitter", "youtube", "reddit", "x", "com", "sns",
            "最新", "議論", "反応", "動向", "調査", "調べ", "分析", "比較", "要点", "まとめ",
            "上", "について", "を", "の", "で", "に", "と",
        }
        tokens = re.findall(r"[a-z0-9_\-]+|[一-龥]{2,}|[ぁ-ん]{2,}|[ァ-ンー]{2,}", text)
        focus = [t for t in tokens if t not in generic_tokens]
        return len(focus) == 0

    @staticmethod
    def _is_explicit_global_source_query(text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        if DiscordOrchestrator._has_followup_marker(lowered):
            return False
        if re.search(r"https?://", lowered):
            return False
        if re.search(r"[a-z0-9_.-]+/[a-z0-9_.-]+", lowered):
            return False

        has_source = bool(re.search(r"(github|youtube|twitter|x\.com|reddit)", lowered))
        has_global_scope = any(token in lowered for token in ("全体", "全般", "界隈", "横断", "トレンド"))
        has_source_latest_phrase = bool(
            re.search(
                r"(github|youtube|twitter|x\.com|reddit)(?:上)?(?:の)?(?:最新)?(?:議論|動向|反応|情報|トレンド)",
                lowered,
            )
        )
        return has_source and (has_global_scope or has_source_latest_phrase)

    @staticmethod
    def _is_general_knowledge_query(text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return False

        # 明示的な過去参照や自己参照は履歴利用対象。
        if DiscordOrchestrator._has_followup_marker(lowered) or DiscordOrchestrator._is_history_recall_query(lowered):
            return False
        if re.search(r"(私|ぼく|僕|俺|わたし|自分|このサーバー|このチャンネル|さっき|前回)", lowered):
            return False

        # URLやリポジトリ指定がある場合は一般知識とはみなさない。
        if re.search(r"https?://", lowered) or re.search(r"[a-z0-9_.-]+/[a-z0-9_.-]+", lowered):
            return False

        generic_markers = (
            "とは",
            "意味",
            "使い方",
            "標準ライブラリ",
            "メリット",
            "デメリット",
            "違い",
            "比較",
            "なに",
            "何",
            "教えて",
            "解説",
            "仕組み",
            "方法",
            "解説",
        )

        # 質問が非常に短い場合（例: 「とは？」のみや「vmbr0とは？」など30文字以下）は、
        # 一般知識っぽく見えても文脈依存の可能性が高いため、一般知識クエリとはみなさない（履歴検索を許可する）。
        if len(lowered) < 30:
            return False

        return any(m in lowered for m in generic_markers)

    @staticmethod
    def _extract_followup_topic_from_recent_context(question: str) -> str:
        text = question or ""
        marker = "[Recent Conversation]"
        idx = text.find(marker)
        if idx < 0:
            return ""
        block = text[idx + len(marker):]
        lines = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("-")]
        user_lines: list[str] = []
        for ln in lines:
            lowered = ln.lower()
            if "] assistant:" in lowered:
                continue
            m = re.search(r"\]\s*[^:]+:\s*(.+)$", ln)
            if not m:
                continue
            content = m.group(1).strip()
            content = re.sub(r"<@!?\d+>", "", content).strip()
            if content:
                user_lines.append(content)

        for content in reversed(user_lines):
            lowered = content.lower()
            if DiscordOrchestrator._has_followup_marker(lowered):
                continue
            if re.search(r"(深掘り|掘り下げ|もっと詳しく|もう少し|詳しく|続けて|追加で|その点|この点)", lowered):
                continue
            if re.fullmatch(r"[\s\W_]*", content):
                continue
            return content

        return user_lines[-1] if user_lines else ""

    @staticmethod
    def _resolve_research_topic(question: str) -> str:
        text = question or ""
        explicit = DiscordOrchestrator._strip_runtime_hints(text).strip()
        if explicit:
            m = re.search(r"\[Resolved Follow-up Topic\]\s*-\s*previous_topic:\s*(.+)$", text, flags=re.DOTALL)
            if m:
                prior = (m.group(1) or "").strip()
                if prior and DiscordOrchestrator._has_followup_marker(explicit):
                    return f"{prior}\n\n[Follow-up Request]\n{explicit}"
            return explicit
        return (question or "").strip()

    @staticmethod
    def _rerank_records_for_recall(question: str, records: list[MemoryRecord]) -> list[MemoryRecord]:
        if not records:
            return records

        request_cues = ("調べて", "調査", "比較", "教えて", "まとめ", "説明")
        recall_cues = ("何について", "何て言", "言ってた", "言っていた", "覚えて", "前回", "さっき", "先ほど")
        denial_cues = ("見当たりません", "確認しましたが", "申し訳ありません", "わかりません")

        normalized_question = (question or "").strip().lower()
        time_pattern = re.compile(r"\d+\s*(?:分|秒)(?:間)?")
        focus_tokens = DiscordOrchestrator._extract_focus_tokens_for_recall(normalized_question)
        question_has_time = bool(time_pattern.search(normalized_question))
        recall_question = any(cue in normalized_question for cue in recall_cues)
        recency_threshold = None
        if recall_question:
            timestamps = sorted(
                (DiscordOrchestrator._timestamp_sort_key(record.timestamp) for record in records),
                reverse=True,
            )
            if timestamps:
                recency_threshold = timestamps[min(3, len(timestamps) - 1)]

        ranked: list[tuple[int, float, int, MemoryRecord]] = []
        for idx, record in enumerate(records):
            content = (record.content or "").strip()
            lowered = content.lower()
            score = 0
            focus_overlap = sum(1 for token in focus_tokens if token and token in lowered)

            if record.role == "user":
                score += 5
            else:
                score -= 1

            if focus_overlap > 0:
                score += focus_overlap * 6
            elif focus_tokens:
                score -= 4

            if any(cue in lowered for cue in request_cues) and (focus_overlap > 0 or not focus_tokens):
                score += 3
            if question_has_time and time_pattern.search(lowered):
                score += 2
            if any(cue in lowered for cue in recall_cues):
                score -= 4
            if any(cue in lowered for cue in denial_cues):
                score -= 6
            if normalized_question and normalized_question in lowered:
                score -= 3
            if recency_threshold is not None and DiscordOrchestrator._timestamp_sort_key(record.timestamp) >= recency_threshold:
                score += 8

            ts_value = DiscordOrchestrator._timestamp_sort_key(record.timestamp)
            ranked.append((score, ts_value, -idx, record))

        ranked.sort(reverse=True)
        return [row[3] for row in ranked]

    @staticmethod
    def _extract_focus_tokens_for_recall(question: str) -> list[str]:
        text = (question or "").strip().lower()
        if not text:
            return []

        tokens = re.findall(r"[a-z0-9_\-]+|[一-龥]{2,}|[ぁ-ん]{2,}|[ァ-ンー]{2,}", text)
        time_pattern = re.compile(r"\d+\s*(?:分|秒)(?:間)?")
        ignore_tokens = {
            "さっき",
            "先ほど",
            "前回",
            "前に",
            "何について",
            "何と言",
            "何て言",
            "言っていた",
            "言ってた",
            "覚えて",
            "会話履歴",
            "ログ",
            "調べて",
            "調査",
            "比較",
            "教えて",
            "まとめ",
            "説明",
        }

        out: list[str] = []
        for token in tokens:
            if len(token) < 2:
                continue
            if token in ignore_tokens:
                continue
            if time_pattern.search(token):
                continue
            if token not in out:
                out.append(token)
        return out[:8]

    @staticmethod
    def _timestamp_sort_key(timestamp_text: str) -> float:
        if not timestamp_text:
            return 0.0
        try:
            parsed = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _extract_json_object(text: str) -> Any | None:
        if not text:
            return None

        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?", "", candidate).strip()
            candidate = re.sub(r"```$", "", candidate).strip()

        try:
            parsed = json.loads(candidate)
            return parsed
        except Exception:
            pass

        def _decision_score(obj: Any) -> int:
            if isinstance(obj, dict):
                score = 0
                action = str(obj.get("action", "")).strip().lower()
                if action in {"tool", "respond", "approve", "rewrite", "needs_tool"}:
                    score += 20
                if "tool" in obj:
                    score += 6
                if "args" in obj and isinstance(obj.get("args"), dict):
                    score += 6
                if "response" in obj and isinstance(obj.get("response"), str):
                    score += 6
                if "reason" in obj:
                    score += 3
                return score

            if isinstance(obj, list):
                score = 0
                if obj:
                    score += 4
                dict_items = [it for it in obj if isinstance(it, dict)]
                if dict_items:
                    score += 4
                    if all(str(it.get("action", "")).strip().lower() in {"tool", "respond"} for it in dict_items):
                        score += 14
                return score

            return 0

        candidates: list[tuple[int, int, Any]] = []

        # Prefer explicit fenced JSON blocks when present.
        for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
            block_candidate = block.strip()
            if not block_candidate:
                continue
            try:
                parsed = json.loads(block_candidate)
            except Exception:
                continue
            if isinstance(parsed, (dict, list)):
                candidates.append((0, _decision_score(parsed), parsed))

        decoder = json.JSONDecoder()
        for idx, ch in enumerate(candidate):
            if ch not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[idx:])
            except Exception:
                continue
            if isinstance(parsed, (dict, list)):
                candidates.append((idx, _decision_score(parsed), parsed))

        if not candidates:
            return None

        # Higher score is better. For ties, prefer later occurrences (often the final answer JSON).
        candidates.sort(key=lambda item: (item[1], item[0]))
        _, best_score, best_value = candidates[-1]
        if best_score <= 0:
            return None
        return best_value

    def _is_gemma_model(self) -> bool:
        model = (self.config.gemini_model or "").strip().lower()
        return model.startswith("gemma")

    @staticmethod
    def _is_503_error(error: Exception) -> bool:
        msg = str(error or "").lower()
        return "503" in msg or "service unavailable" in msg

    async def _invoke_with_retry(
        self,
        prompt: str,
        max_output_tokens: int = 2048,
        response_mime_type: str | None = None,
    ) -> str:
        retries = max(0, int(os.getenv("GEMINI_MAX_RETRIES", "1")))
        last_error: Exception | None = None

        def _retry_wait_seconds(error: Exception, fallback: float) -> float:
            # Gemini quota errors often include "Please retry in 51.3s".
            message = str(error)
            match = re.search(r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)s", message, flags=re.IGNORECASE)
            if not match:
                return fallback
            try:
                recommended = float(match.group(1))
            except Exception:
                return fallback
            # Cap wait so the command remains responsive even on long quota delays.
            return max(fallback, min(recommended, 90.0))

        async def _acquire_gemini_slot() -> None:
            window_sec = 60.0
            while True:
                wait_sec = 0.0
                async with self._gemini_rate_limit_lock:
                    now = time.monotonic()
                    while self._gemini_request_timestamps and (now - self._gemini_request_timestamps[0]) >= window_sec:
                        self._gemini_request_timestamps.popleft()

                    if len(self._gemini_request_timestamps) < self.gemini_max_requests_per_min:
                        self._gemini_request_timestamps.append(now)
                        return

                    oldest = self._gemini_request_timestamps[0]
                    wait_sec = max(0.05, window_sec - (now - oldest))

                await asyncio.sleep(wait_sec)

        async def _generate_with_model(active_model: Any, active_model_name: str, active_response_mime_type: str | None) -> str:
            await _acquire_gemini_slot()
            async with self._gemini_inflight_lock:
                timeout_sec = None if self.config.gemini_timeout_sec <= 0 else self.config.gemini_timeout_sec
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        active_model.generate_content,
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            **{
                                "temperature": 0.2,
                                "top_p": 0.95,
                                "top_k": 40,
                                "max_output_tokens": max_output_tokens,
                                **(
                                    {"response_mime_type": active_response_mime_type}
                                    if active_response_mime_type
                                    else {}
                                ),
                            }
                        ),
                        request_options=RequestOptions(timeout=timeout_sec, retry=None),
                    ),
                    timeout=timeout_sec,
                )
            text = (getattr(response, "text", "") or "").strip()
            self._last_model_used_name = active_model_name
            log_ai_exchange(
                component="main-agent",
                model=active_model_name,
                prompt=prompt,
                response=text,
                metadata={
                    "phase": "model_call",
                    "attempt": 1,
                    "max_output_tokens": max_output_tokens,
                    "response_mime_type": active_response_mime_type or "",
                },
            )
            if text:
                return text
            return "回答を生成できませんでした。質問を少し変えて再試行してください。"

        # When 503 is observed once, keep using fallback for a cooldown window
        # to avoid repeated wasted Gemini calls on every turn.
        if (
            self._fallback_model is not None
            and time.monotonic() < self._prefer_fallback_until_monotonic
        ):
            fallback_response_mime = response_mime_type
            if self.gemini_503_fallback_model.strip().lower().startswith("gemma"):
                fallback_response_mime = None
            logger.info(
                "Fallback mode active; route main-agent -> model=%s",
                self.gemini_503_fallback_model,
            )
            return await _generate_with_model(
                active_model=self._fallback_model,
                active_model_name=self.gemini_503_fallback_model,
                active_response_mime_type=fallback_response_mime,
            )

        for attempt in range(retries + 1):
            try:
                timeout_sec = None if self.config.gemini_timeout_sec <= 0 else self.config.gemini_timeout_sec
                await _acquire_gemini_slot()
                async with self._gemini_inflight_lock:
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
                            request_options=RequestOptions(timeout=timeout_sec, retry=None),
                        ),
                        timeout=timeout_sec,
                    )
                text = (getattr(response, "text", "") or "").strip()
                self._last_model_used_name = self.config.gemini_model
                log_ai_exchange(
                    component="main-agent",
                    model=self.config.gemini_model,
                    prompt=prompt,
                    response=text,
                    metadata={
                        "phase": "model_call",
                        "attempt": attempt + 1,
                        "max_output_tokens": max_output_tokens,
                        "response_mime_type": response_mime_type or "",
                    },
                )
                if text:
                    return text
                return "回答を生成できませんでした。質問を少し変えて再試行してください。"
            except Exception as exc:
                log_ai_exchange(
                    component="main-agent",
                    model=self.config.gemini_model,
                    prompt=prompt,
                    response="",
                    metadata={
                        "phase": "model_call",
                        "attempt": attempt + 1,
                        "max_output_tokens": max_output_tokens,
                        "response_mime_type": response_mime_type or "",
                    },
                    error=str(exc),
                )
                last_error = exc
                if attempt == 0 and self._is_503_error(exc):
                    if self._fallback_model is not None:
                        self._prefer_fallback_until_monotonic = (
                            time.monotonic() + float(self.gemini_503_fallback_cooldown_sec)
                        )
                        logger.warning(
                            "Gemini returned 503 on first attempt; switching to fallback model=%s (cooldown=%ss)",
                            self.gemini_503_fallback_model,
                            self.gemini_503_fallback_cooldown_sec,
                        )
                        fallback_response_mime = response_mime_type
                        if self.gemini_503_fallback_model.strip().lower().startswith("gemma"):
                            fallback_response_mime = None
                        try:
                            return await _generate_with_model(
                                active_model=self._fallback_model,
                                active_model_name=self.gemini_503_fallback_model,
                                active_response_mime_type=fallback_response_mime,
                            )
                        except Exception as fallback_exc:
                            log_ai_exchange(
                                component="main-agent",
                                model=self.gemini_503_fallback_model,
                                prompt=prompt,
                                response="",
                                metadata={
                                    "phase": "model_call_fallback",
                                    "attempt": 1,
                                    "max_output_tokens": max_output_tokens,
                                    "response_mime_type": fallback_response_mime or "",
                                },
                                error=str(fallback_exc),
                            )
                            last_error = fallback_exc
                            break
                    else:
                        logger.warning(
                            "Gemini returned 503 on first attempt, but fallback model is unavailable."
                        )
                        break
                if isinstance(exc, asyncio.TimeoutError) and self.config.gemini_timeout_sec > 0:
                    logger.error(
                        "Gemini API timeout after %s seconds (attempt %s/%s). Consider increasing GEMINI_TIMEOUT_SEC.",
                        self.config.gemini_timeout_sec,
                        attempt + 1,
                        retries + 1,
                    )
                if attempt < retries:
                    await asyncio.sleep(_retry_wait_seconds(exc, fallback=2**attempt))

        logger.error(
            "All retries exhausted for Gemini invocation. Last error type: %s, msg: %s",
            type(last_error).__name__,
            str(last_error)[:200],
        )
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
    api_key = (
        os.getenv("MAIN_AGENT_GEMINI_API_KEY", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
    )
    return OrchestratorConfig(
        gemini_api_key=api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview").strip()
        or "gemini-3.1-flash-lite-preview",
        gemini_timeout_sec=int(os.getenv("GEMINI_TIMEOUT_SEC", "0")),
        profile_path=os.getenv("INITIAL_PROFILE_PATH", "./data/profiles/initial_profile.md").strip(),
        chromadb_path=os.getenv("CHROMADB_PATH", "./data/chromadb").strip(),
    )
