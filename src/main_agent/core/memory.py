from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import chromadb

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MemoryRecord:
    role: str
    content: str
    timestamp: str
    user_id: str
    message_id: str | None = None
    metadata: dict[str, Any] | None = None


class TaskCheckpointStore:
    """SQLite-backed checkpoint store for resumable long-running workflows."""

    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._initialize()

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    job_id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_workflow_checkpoints_workflow_status
                ON workflow_checkpoints (workflow, status, updated_at)
                """
            )

    async def upsert_checkpoint(
        self,
        job_id: str,
        workflow: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(
            self._upsert_checkpoint_sync,
            job_id,
            workflow,
            status,
            payload,
        )

    def _upsert_checkpoint_sync(
        self,
        job_id: str,
        workflow: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        clean_job_id = (job_id or "").strip()
        clean_workflow = (workflow or "").strip() or "unspecified"
        clean_status = (status or "").strip() or "running"
        if not clean_job_id:
            return

        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)

        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            conn.execute(
                """
                INSERT INTO workflow_checkpoints(job_id, workflow, status, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    workflow=excluded.workflow,
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (clean_job_id, clean_workflow, clean_status, payload_json, now),
            )

    async def get_checkpoint(self, job_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_checkpoint_sync, job_id)

    def _get_checkpoint_sync(self, job_id: str) -> dict[str, Any] | None:
        clean_job_id = (job_id or "").strip()
        if not clean_job_id:
            return None

        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            row = conn.execute(
                """
                SELECT job_id, workflow, status, payload_json, updated_at
                FROM workflow_checkpoints
                WHERE job_id = ?
                """,
                (clean_job_id,),
            ).fetchone()

        if row is None:
            return None

        payload: dict[str, Any]
        try:
            payload = json.loads(str(row[3]))
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        return {
            "job_id": str(row[0]),
            "workflow": str(row[1]),
            "status": str(row[2]),
            "payload": payload,
            "updated_at": str(row[4]),
        }

    async def list_checkpoints(
        self,
        workflow: str,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_checkpoints_sync, workflow, status, limit)

    def _list_checkpoints_sync(
        self,
        workflow: str,
        status: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clean_workflow = (workflow or "").strip() or "unspecified"
        clean_status = (status or "").strip() or None
        fetch_limit = max(1, min(int(limit), 200))

        query = (
            "SELECT job_id, workflow, status, payload_json, updated_at "
            "FROM workflow_checkpoints "
            "WHERE workflow = ?"
        )
        params: list[Any] = [clean_workflow]
        if clean_status is not None:
            query += " AND status = ?"
            params.append(clean_status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(fetch_limit)

        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row[3]))
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            results.append(
                {
                    "job_id": str(row[0]),
                    "workflow": str(row[1]),
                    "status": str(row[2]),
                    "payload": payload,
                    "updated_at": str(row[4]),
                }
            )
        return results

    async def delete_checkpoint(self, job_id: str) -> int:
        return await asyncio.to_thread(self._delete_checkpoint_sync, job_id)

    def _delete_checkpoint_sync(self, job_id: str) -> int:
        clean_job_id = (job_id or "").strip()
        if not clean_job_id:
            return 0
        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            cur = conn.execute(
                "DELETE FROM workflow_checkpoints WHERE job_id = ?",
                (clean_job_id,),
            )
            return int(cur.rowcount or 0)


class ChannelMemoryStore:
    """ChromaDB-backed conversation memory with channel and guild-wide indexes."""

    def __init__(self, persist_dir: str, top_k: int = 4, embedding_dim: int = 32) -> None:
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._top_k = top_k
        self._embedding_dim = embedding_dim
        self._persona_collection_name = os.getenv("PERSONA_MEMORY_COLLECTION", "persona_profiles").strip() or "persona_profiles"

    @staticmethod
    def _normalize_collection_name(guild_id: int | None, channel_id: int) -> str:
        guild_part = str(guild_id) if guild_id is not None else "dm"
        raw = f"mem_g{guild_part}_c{channel_id}"
        return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)

    @staticmethod
    def _normalize_guild_collection_name(guild_id: int | None) -> str:
        guild_part = str(guild_id) if guild_id is not None else "dm"
        raw = f"mem_g{guild_part}_all"
        return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)

    @staticmethod
    def _normalize_persona_collection_name(name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", name)

    @staticmethod
    def _profile_fact_id(user_id: int, key: str) -> str:
        return f"u{user_id}:{key.strip().lower()}"

    async def set_user_profile_fact(
        self,
        user_id: int,
        key: str,
        value: str,
        source: str = "manual",
        confirmed: bool = True,
    ) -> None:
        await asyncio.to_thread(
            self._set_user_profile_fact_sync,
            user_id,
            key,
            value,
            source,
            confirmed,
        )

    def _set_user_profile_fact_sync(
        self,
        user_id: int,
        key: str,
        value: str,
        source: str,
        confirmed: bool,
    ) -> None:
        clean_key = (key or "").strip().lower()
        clean_value = (value or "").strip()
        if not clean_key or not clean_value:
            return

        col_name = self._normalize_persona_collection_name(self._persona_collection_name)
        collection = self._client.get_or_create_collection(name=col_name)
        fact_id = self._profile_fact_id(user_id, clean_key)
        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            "user_id": str(user_id),
            "key": clean_key,
            "source": (source or "manual").strip() or "manual",
            "confirmed": bool(confirmed),
            "updated_at": now,
        }
        doc = f"{clean_key}: {clean_value}"
        collection.upsert(
            ids=[fact_id],
            documents=[doc],
            metadatas=[metadata],
            embeddings=[self._embed(doc)],
        )

    async def get_user_profile_facts(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_user_profile_facts_sync, user_id, limit)

    def _get_user_profile_facts_sync(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        col_name = self._normalize_persona_collection_name(self._persona_collection_name)
        try:
            collection = self._client.get_collection(name=col_name)
        except Exception:
            return []

        fetch_limit = max(int(limit), 1)
        try:
            result = collection.get(
                where={"user_id": str(user_id)},
                include=["documents", "metadatas"],
                limit=fetch_limit,
            )
        except Exception:
            logger.exception("Failed to get user profile facts: user_id=%s", user_id)
            return []

        docs = result.get("documents", []) or []
        mds = result.get("metadatas", []) or []
        facts: list[dict[str, Any]] = []
        for idx, doc in enumerate(docs):
            md = mds[idx] if idx < len(mds) else {}
            key = str(md.get("key", "")).strip()
            value = str(doc).split(":", 1)[1].strip() if ":" in str(doc) else str(doc).strip()
            facts.append(
                {
                    "key": key,
                    "value": value,
                    "source": str(md.get("source", "")),
                    "confirmed": bool(md.get("confirmed", False)),
                    "updated_at": str(md.get("updated_at", "")),
                }
            )

        facts.sort(key=lambda x: str(x.get("updated_at", "")), reverse=True)
        return facts

    async def forget_user_profile_fact(self, user_id: int, key: str | None = None) -> int:
        return await asyncio.to_thread(self._forget_user_profile_fact_sync, user_id, key)

    def _forget_user_profile_fact_sync(self, user_id: int, key: str | None) -> int:
        col_name = self._normalize_persona_collection_name(self._persona_collection_name)
        try:
            collection = self._client.get_collection(name=col_name)
        except Exception:
            return 0

        if key is not None and key.strip():
            fact_id = self._profile_fact_id(user_id, key)
            try:
                collection.delete(ids=[fact_id])
                return 1
            except Exception:
                logger.exception("Failed to delete profile fact: user_id=%s key=%s", user_id, key)
                return 0

        facts = self._get_user_profile_facts_sync(user_id, limit=1000)
        if not facts:
            return 0

        deleted = 0
        for fact in facts:
            fact_key = str(fact.get("key", "")).strip()
            if not fact_key:
                continue
            fact_id = self._profile_fact_id(user_id, fact_key)
            try:
                collection.delete(ids=[fact_id])
                deleted += 1
            except Exception:
                continue
        return deleted

    def _embed(self, text: str) -> list[float]:
        if not text:
            return [0.0] * self._embedding_dim

        values: list[float] = []
        seed = text.encode("utf-8")
        while len(values) < self._embedding_dim:
            seed = hashlib.sha256(seed).digest()
            for i in range(0, len(seed), 2):
                pair = seed[i : i + 2]
                if len(pair) < 2:
                    continue
                number = int.from_bytes(pair, "big")
                values.append((number / 65535.0) * 2.0 - 1.0)
                if len(values) >= self._embedding_dim:
                    break

        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:
            return values
        return [v / norm for v in values]

    async def add_message(
        self,
        guild_id: int | None,
        channel_id: int,
        role: str,
        content: str,
        user_id: int,
        message_id: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not content.strip():
            return
        await asyncio.to_thread(
            self._add_message_sync,
            guild_id,
            channel_id,
            role,
            content,
            user_id,
            message_id,
            metadata,
        )

    def _add_message_sync(
        self,
        guild_id: int | None,
        channel_id: int,
        role: str,
        content: str,
        user_id: int,
        message_id: int | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        channel_collection_name = self._normalize_collection_name(guild_id, channel_id)
        channel_collection = self._client.get_or_create_collection(name=channel_collection_name)
        guild_collection_name = self._normalize_guild_collection_name(guild_id)
        guild_collection = self._client.get_or_create_collection(name=guild_collection_name)

        now = datetime.now(timezone.utc).isoformat()
        source_timestamp = ""
        if metadata is not None:
            raw_ts = metadata.get("timestamp")
            if isinstance(raw_ts, str):
                source_timestamp = raw_ts.strip()
        effective_timestamp = source_timestamp or now
        if message_id is not None:
            record_id = f"msg-{message_id}-{role}"
        else:
            record_id = f"{int(time.time() * 1000)}-{role}-{uuid4().hex[:8]}"
        merged_metadata: dict[str, Any] = {
            "role": role,
            "timestamp": effective_timestamp,
            "user_id": str(user_id),
            "message_id": str(message_id) if message_id is not None else "",
            "guild_id": str(guild_id) if guild_id is not None else "",
            "channel_id": str(channel_id),
        }
        if metadata:
            for key, value in metadata.items():
                merged_metadata[str(key)] = value if isinstance(value, (str, int, float, bool)) else str(value)

        payload = {
            "ids": [record_id],
            "documents": [content],
            "metadatas": [merged_metadata],
            "embeddings": [self._embed(content)],
        }

        channel_collection.upsert(**payload)
        guild_collection.upsert(**payload)

    async def fetch_relevant_messages(
        self,
        guild_id: int | None,
        channel_id: int,
        query_text: str,
        limit: int | None = None,
        scope: str = "channel",
    ) -> list[MemoryRecord]:
        return await asyncio.to_thread(
            self._fetch_relevant_messages_sync,
            guild_id,
            channel_id,
            query_text,
            limit or self._top_k,
            scope,
        )

    async def fetch_relevant_messages_multi_guild(
        self,
        guild_ids: list[int],
        channel_id: int,
        query_text: str,
        limit: int | None = None,
    ) -> list[MemoryRecord]:
        return await asyncio.to_thread(
            self._fetch_relevant_messages_multi_guild_sync,
            guild_ids,
            channel_id,
            query_text,
            limit or self._top_k,
        )

    async def get_guild_memory_stats(self, guild_id: int | None) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_guild_memory_stats_sync, guild_id)

    def _get_guild_memory_stats_sync(self, guild_id: int | None) -> dict[str, Any]:
        guild_part = str(guild_id) if guild_id is not None else "dm"
        prefix = f"mem_g{guild_part}_"
        collections = self._client.list_collections()
        names: list[str] = []
        for col in collections:
            name = getattr(col, "name", "")
            if isinstance(name, str) and name.startswith(prefix):
                names.append(name)

        stats: list[dict[str, Any]] = []
        total = 0
        for name in sorted(names):
            try:
                cnt = self._client.get_collection(name=name).count()
            except Exception:
                cnt = 0
            total += int(cnt)
            stats.append({"name": name, "count": int(cnt)})

        return {
            "guild_id": guild_id,
            "collection_count": len(stats),
            "total_records": total,
            "collections": stats,
        }

    def _fetch_relevant_messages_sync(
        self,
        guild_id: int | None,
        channel_id: int,
        query_text: str,
        limit: int,
        scope: str,
    ) -> list[MemoryRecord]:
        normalized_scope = (scope or "channel").strip().lower()
        if normalized_scope == "guild":
            collection_name = self._normalize_guild_collection_name(guild_id)
        else:
            collection_name = self._normalize_collection_name(guild_id, channel_id)

        try:
            collection = self._client.get_collection(name=collection_name)
        except Exception:
            return []

        candidate_limit = max(limit * 25, 200)
        try:
            result = collection.query(
                query_embeddings=[self._embed(query_text)],
                n_results=candidate_limit,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            logger.exception("Failed to query memory collection: %s", collection_name)
            return []

        docs_nested = result.get("documents", [[]])
        metadatas_nested = result.get("metadatas", [[]])
        distances_nested = result.get("distances", [[]])
        docs = docs_nested[0] if docs_nested else []
        metadatas = metadatas_nested[0] if metadatas_nested else []
        distances = distances_nested[0] if distances_nested else []
        if not docs:
            return []

        query_tokens = self._tokenize(query_text)
        # 固有語の取りこぼしを減らすため、where_document検索の候補も併用
        docs, metadatas, distances = self._merge_lexical_candidates(
            collection=collection,
            query_tokens=query_tokens,
            docs=docs,
            metadatas=metadatas,
            distances=distances,
        )

        ranked: list[tuple[int, float, float, MemoryRecord]] = []
        now_ts = datetime.now(timezone.utc).timestamp()
        for idx, content in enumerate(docs):
            md = metadatas[idx] if metadatas and idx < len(metadatas) else {}
            distance = float(distances[idx]) if distances and idx < len(distances) else 0.0
            record = MemoryRecord(
                role=str(md.get("role", "unknown")),
                content=content,
                timestamp=str(md.get("timestamp", "")),
                user_id=str(md.get("user_id", "")),
                message_id=str(md.get("message_id", "")) or None,
                metadata=md,
            )
            overlap = self._overlap_score(query_tokens, self._tokenize(content))
            recency_bonus = self._recency_score(record.timestamp, now_ts)
            similarity = 1.0 / (1.0 + max(distance, 0.0))
            similarity = max(similarity - self._assistant_fallback_penalty(record), 0.0)
            similarity = max(similarity - self._content_quality_penalty(record), 0.0)
            ranked.append((overlap, similarity, recency_bonus, record))

        ranked.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        selected = ranked[:limit]

        if selected and selected[0][0] == 0:
            recent_ranked = sorted(ranked, key=lambda x: (x[1], x[2]), reverse=True)
            selected = recent_ranked[:limit]

        records: list[MemoryRecord] = []
        seen: set[str] = set()
        for _, __, ___, record in selected:
            md = record.metadata or {}
            key = f"{md.get('channel_id','')}:{record.role}:{(record.content or '').strip()}"
            if key in seen:
                continue
            seen.add(key)
            records.append(
                MemoryRecord(
                    role=record.role,
                    content=record.content,
                    timestamp=record.timestamp,
                    user_id=record.user_id,
                    message_id=record.message_id,
                    metadata=record.metadata,
                )
            )
            if len(records) >= limit:
                break

        return records

    def _fetch_relevant_messages_multi_guild_sync(
        self,
        guild_ids: list[int],
        channel_id: int,
        query_text: str,
        limit: int,
    ) -> list[MemoryRecord]:
        if not guild_ids:
            return []

        query_tokens = self._tokenize(query_text)
        now_ts = datetime.now(timezone.utc).timestamp()
        candidates: list[tuple[int, float, float, MemoryRecord]] = []

        # Per-guildで広めに拾ってから全体で再ランクする。
        per_guild_limit = max(limit * 3, 12)
        for gid in guild_ids:
            records = self._fetch_relevant_messages_sync(
                guild_id=gid,
                channel_id=channel_id,
                query_text=query_text,
                limit=per_guild_limit,
                scope="guild",
            )
            for record in records:
                overlap = self._overlap_score(query_tokens, self._tokenize(record.content))
                recency_bonus = self._recency_score(record.timestamp, now_ts)
                similarity = max(0.0, 1.0 - self._content_quality_penalty(record))
                candidates.append((overlap, similarity, recency_bonus, record))

        if not candidates:
            return []

        candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)

        records: list[MemoryRecord] = []
        seen: set[str] = set()
        for _, __, ___, record in candidates:
            md = record.metadata or {}
            key = f"{md.get('guild_id','')}:{md.get('channel_id','')}:{record.role}:{(record.content or '').strip()}"
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
            if len(records) >= limit:
                break

        return records

    def _merge_lexical_candidates(
        self,
        collection: Any,
        query_tokens: set[str],
        docs: list[str],
        metadatas: list[dict[str, Any]],
        distances: list[float],
    ) -> tuple[list[str], list[dict[str, Any]], list[float]]:
        time_tokens = [
            t for t in query_tokens
            if re.search(r"\d", t) or ("分" in t) or ("秒" in t)
        ]
        alnum_tokens = [
            t for t in sorted(query_tokens, key=len, reverse=True)
            if t not in time_tokens and re.fullmatch(r"[a-z0-9_\-]{2,32}", t)
        ]
        other_tokens = [
            t for t in sorted(query_tokens, key=len, reverse=True)
            if t not in time_tokens and t not in alnum_tokens
        ]
        token_candidates: list[str] = []
        for token in sorted(time_tokens, key=len, reverse=True)[:3] + alnum_tokens[:4] + other_tokens[:6]:
            if token and token not in token_candidates:
                token_candidates.append(token)
        if not token_candidates:
            return docs, metadatas, distances

        existing_keys: set[str] = set()
        for i, doc in enumerate(docs):
            md = metadatas[i] if i < len(metadatas) else {}
            existing_keys.add(self._candidate_key(doc, md))

        merged_docs = list(docs)
        merged_mds = list(metadatas)
        merged_distances = list(distances)

        for token in token_candidates:
            variants = [token]
            if re.fullmatch(r"[a-z0-9_\-]{2,32}", token):
                upper = token.upper()
                if upper not in variants:
                    variants.append(upper)

            for variant in variants:
                try:
                    res = collection.get(
                        where_document={"$contains": variant},
                        include=["documents", "metadatas"],
                        limit=40,
                    )
                except Exception:
                    continue

                lex_docs = res.get("documents", []) or []
                lex_mds = res.get("metadatas", []) or []
                for idx, content in enumerate(lex_docs):
                    md = lex_mds[idx] if idx < len(lex_mds) else {}
                    key = self._candidate_key(content, md)
                    if key in existing_keys:
                        continue
                    existing_keys.add(key)
                    merged_docs.append(content)
                    merged_mds.append(md)
                    # 文字列一致候補は距離0相当として強めに優先
                    merged_distances.append(0.0)

        return merged_docs, merged_mds, merged_distances

    @staticmethod
    def _candidate_key(content: str, metadata: dict[str, Any]) -> str:
        message_id = str(metadata.get("message_id", "")) if metadata else ""
        if message_id:
            return f"msg:{message_id}"
        return f"content:{hash(content)}"

    @staticmethod
    def _assistant_fallback_penalty(record: MemoryRecord) -> float:
        if record.role != "assistant":
            return 0.0
        text = (record.content or "").lower()
        patterns = [
            "discordの過去の投稿履歴を直接参照する権限を持っていない",
            "過去の投稿履歴を直接参照する権限",
            "手元にログが残っている場合は",
            "ここに貼り付けていただければ",
        ]
        return 0.35 if any(p in text for p in patterns) else 0.0

    @staticmethod
    def _content_quality_penalty(record: MemoryRecord) -> float:
        text = (record.content or "").strip()
        if not text:
            return 0.6

        # URLのみ投稿は文脈情報が弱いことが多い
        if re.fullmatch(r"https?://\S+", text):
            return 0.22

        # 極端に短い定型文は想起ノイズになりやすい
        if len(text) <= 8:
            return 0.18

        # 区切り線などは情報密度が低い
        if re.fullmatch(r"[-_=#\s]{8,}", text):
            return 0.28

        return 0.0

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        if not text:
            return set()
        digit_map = str.maketrans("０１２３４５６７８９", "0123456789")
        normalized = text.translate(digit_map).lower()

        parts = re.findall(
            r"[a-zA-Z0-9_\-]+|[一-龥]{2,}|[ぁ-ん]{2,}|[ァ-ンー]{2,}",
            normalized,
        )
        time_parts = re.findall(r"\d{1,4}\s*(?:秒|分)(?:間)?", normalized)
        stopwords = {
            "について",
            "これ",
            "それ",
            "あれ",
            "こと",
            "もの",
            "です",
            "ます",
            "した",
            "して",
            "ある",
            "いる",
            "どこ",
            "なに",
            "何",
            "過去",
            "最新",
        }
        tokens = {p for p in parts if len(p) >= 2 and p not in stopwords}
        for t in time_parts:
            compact = re.sub(r"\s+", "", t)
            if len(compact) >= 2:
                tokens.add(compact)
        return tokens

    @staticmethod
    def _overlap_score(query_tokens: set[str], doc_tokens: set[str]) -> int:
        if not query_tokens or not doc_tokens:
            return 0
        return len(query_tokens & doc_tokens)

    @staticmethod
    def _recency_score(timestamp_text: str, now_ts: float) -> float:
        if not timestamp_text:
            return 0.0
        try:
            ts = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
        age = max(now_ts - ts, 0.0)
        # 直近ほど1.0に近く、古いほど緩やかに減衰
        return 1.0 / (1.0 + (age / 86400.0))
