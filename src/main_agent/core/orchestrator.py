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

from main_agent.core.memory import ChannelMemoryStore, MemoryRecord, TaskCheckpointStore
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

        self.last_tool_executions: list[dict[str, object]] = []

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
        retrieval_question = self._strip_runtime_hints(question)
        if not retrieval_question:
            retrieval_question = (question or "").strip()

        is_explicit_global_query = self._is_explicit_global_source_query(retrieval_question)
        has_followup_marker = self._has_followup_marker(retrieval_question)
        recall_intent = self._is_history_recall_query(retrieval_question)
        retrieval_limit = max(self.memory_top_k, 24) if recall_intent else self.memory_top_k
        retrieval_scope = "guild" if recall_intent else self.memory_scope
        use_history_context = True
        if is_explicit_global_query and not has_followup_marker:
            use_history_context = False
            retrieval_scope = "disabled(explicit_global_query)"
            self._log_ai_thought(
                stage="memory_context_policy",
                action="skip_history_context",
                reason="explicit_global_source_query_without_followup",
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
            answer_text = self._sanitize_user_facing_error_phrases(answer_text)
            if self.memory_response_include_evidence:
                answer_text = self._append_memory_evidence(answer_text, history_records)
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

            if self._should_force_research_job(
                question=runtime_question,
                turn=turn,
                action=action,
                scratchpad=scratchpad,
            ):
                forced_source = self._infer_research_source_from_question(runtime_question)
                forced_topic = self._resolve_research_topic(runtime_question)
                decision = {
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
            logger.info(
                "Agent decision: turn=%s action=%s tool=%s reason=%s",
                turn,
                action,
                str(decision.get("tool", "")),
                str(decision.get("reason", ""))[:180],
            )
            self._log_ai_thought(
                stage="decision",
                turn=turn,
                action=action,
                tool=str(decision.get("tool", "")),
                reason=str(decision.get("reason", ""))[:180],
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

                if tool_name == "dispatch_research_job":
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
                continue

            response = str(decision.get("response", "")).strip()
            if response:
                if self._is_nonfinal_response(response):
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
            "質問を短くして再試行してください",
            "回答を生成できませんでした",
            "現在AI応答で問題が発生しています",
        ]
        return any(p in body for p in patterns)

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
        markers = (
            "それ",
            "その",
            "この件",
            "同じ",
            "続き",
            "前の",
            "先ほど",
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
