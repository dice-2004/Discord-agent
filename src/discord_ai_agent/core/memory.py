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
    """ChromaDB-backed conversation memory isolated by guild/channel."""

    def __init__(self, persist_dir: str, top_k: int = 4, embedding_dim: int = 32) -> None:
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._top_k = top_k
        self._embedding_dim = embedding_dim

    @staticmethod
    def _normalize_collection_name(guild_id: int | None, channel_id: int) -> str:
        guild_part = str(guild_id) if guild_id is not None else "dm"
        raw = f"mem_g{guild_part}_c{channel_id}"
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
        collection_name = self._normalize_collection_name(guild_id, channel_id)
        collection = self._client.get_or_create_collection(name=collection_name)

        now = datetime.now(timezone.utc).isoformat()
        record_id = f"{int(time.time() * 1000)}-{role}-{uuid4().hex[:8]}"
        merged_metadata: dict[str, Any] = {
            "role": role,
            "timestamp": now,
            "user_id": str(user_id),
            "message_id": str(message_id) if message_id is not None else "",
            "guild_id": str(guild_id) if guild_id is not None else "",
            "channel_id": str(channel_id),
        }
        if metadata:
            for key, value in metadata.items():
                merged_metadata[str(key)] = value if isinstance(value, (str, int, float, bool)) else str(value)

        collection.add(
            ids=[record_id],
            documents=[content],
            metadatas=[merged_metadata],
            embeddings=[self._embed(content)],
        )

    async def fetch_relevant_messages(
        self,
        guild_id: int | None,
        channel_id: int,
        query_text: str,
        limit: int | None = None,
    ) -> list[MemoryRecord]:
        return await asyncio.to_thread(
            self._fetch_relevant_messages_sync,
            guild_id,
            channel_id,
            query_text,
            limit or self._top_k,
        )

    def _fetch_relevant_messages_sync(
        self,
        guild_id: int | None,
        channel_id: int,
        query_text: str,
        limit: int,
    ) -> list[MemoryRecord]:
        collection_name = self._normalize_collection_name(guild_id, channel_id)
        try:
            collection = self._client.get_collection(name=collection_name)
        except Exception:
            return []

        try:
            result = collection.query(
                query_embeddings=[self._embed(query_text)],
                n_results=limit,
                include=["documents", "metadatas"],
            )
        except Exception:
            logger.exception("Failed to query memory collection: %s", collection_name)
            return []

        docs = result.get("documents", [[]])
        metadatas = result.get("metadatas", [[]])
        if not docs or not docs[0]:
            return []

        records: list[MemoryRecord] = []
        for idx, content in enumerate(docs[0]):
            md = metadatas[0][idx] if metadatas and metadatas[0] and idx < len(metadatas[0]) else {}
            records.append(
                MemoryRecord(
                    role=str(md.get("role", "unknown")),
                    content=content,
                    timestamp=str(md.get("timestamp", "")),
                    user_id=str(md.get("user_id", "")),
                    message_id=str(md.get("message_id", "")) or None,
                    metadata=md,
                )
            )

        return records
