from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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


class ChannelMemoryStore:
    """ChromaDB-backed conversation memory with channel and guild-wide indexes."""

    def __init__(self, persist_dir: str, top_k: int = 4, embedding_dim: int = 32) -> None:
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._top_k = top_k
        self._embedding_dim = embedding_dim

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

    def _merge_lexical_candidates(
        self,
        collection: Any,
        query_tokens: set[str],
        docs: list[str],
        metadatas: list[dict[str, Any]],
        distances: list[float],
    ) -> tuple[list[str], list[dict[str, Any]], list[float]]:
        token_candidates = sorted(query_tokens, key=len, reverse=True)[:3]
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
            try:
                res = collection.get(
                    where_document={"$contains": token},
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
        parts = re.findall(
            r"[a-zA-Z0-9_\-]+|[一-龥]{2,}|[ぁ-ん]{2,}|[ァ-ンー]{2,}",
            text.lower(),
        )
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
        return {p for p in parts if len(p) >= 2 and p not in stopwords}

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
