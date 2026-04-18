from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from main_agent.core.memory import ChannelMemoryStore


_STORE: ChannelMemoryStore | None = None


def _int_or_none(text: str) -> int | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _safe_int(text: str, default: int, min_value: int, max_value: int) -> int:
    raw = (text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(min_value, min(value, max_value))


def _store() -> ChannelMemoryStore:
    global _STORE
    if _STORE is None:
        chromadb_path = (os.getenv("CHROMADB_PATH", "./data/chromadb").strip() or "./data/chromadb")
        top_k = _safe_int(os.getenv("MEMORY_TOP_K", "8"), default=8, min_value=1, max_value=50)
        _STORE = ChannelMemoryStore(persist_dir=chromadb_path, top_k=top_k)
    return _STORE


def get_discord_conversation_status(
    guild_id: str = "",
    channel_id: str = "",
    scope: str = "guild",
    limit: str = "12",
) -> str:
    gid = _int_or_none(guild_id)
    cid = _int_or_none(channel_id) or 0
    scoped = (scope or "guild").strip().lower()
    if scoped not in {"guild", "channel"}:
        scoped = "guild"
    max_items = _safe_int(limit, default=12, min_value=1, max_value=50)

    store = _store()
    stats = asyncio.run(store.get_guild_memory_stats(gid))
    recent = asyncio.run(
        store.get_recent_messages(
            guild_id=gid,
            channel_id=cid,
            limit=max_items,
            scope=scoped,
        )
    )

    items: list[dict[str, Any]] = []
    for rec in recent:
        md = rec.metadata or {}
        items.append(
            {
                "timestamp": rec.timestamp,
                "role": rec.role,
                "user_id": rec.user_id,
                "guild_id": str(md.get("guild_id", "")),
                "channel_id": str(md.get("channel_id", "")),
                "channel_name": str(md.get("channel_name", "")),
                "content": (rec.content or "")[:220],
            }
        )

    return json.dumps(
        {
            "status": "ok",
            "tool": "get_discord_conversation_status",
            "scope": scoped,
            "guild_id": gid,
            "channel_id": cid if cid > 0 else None,
            "memory_stats": stats,
            "recent_messages": items,
        },
        ensure_ascii=False,
    )


def get_user_memory(
    user_id: str,
    guild_id: str = "",
    channel_id: str = "",
    scope: str = "guild",
    limit: str = "12",
) -> str:
    uid = _int_or_none(user_id)
    if uid is None or uid <= 0:
        return json.dumps(
            {
                "status": "error",
                "tool": "get_user_memory",
                "code": "invalid_user_id",
                "detail": "user_id は正の整数が必要です。",
            },
            ensure_ascii=False,
        )

    gid = _int_or_none(guild_id)
    cid = _int_or_none(channel_id) or 0
    scoped = (scope or "guild").strip().lower()
    if scoped not in {"guild", "channel"}:
        scoped = "guild"
    max_items = _safe_int(limit, default=12, min_value=1, max_value=50)

    store = _store()
    facts = asyncio.run(store.get_user_profile_facts(uid, limit=max_items))
    messages = asyncio.run(
        store.get_user_messages(
            user_id=uid,
            guild_id=gid,
            channel_id=cid,
            limit=max_items,
            scope=scoped,
        )
    )

    rows: list[dict[str, Any]] = []
    for rec in messages:
        md = rec.metadata or {}
        rows.append(
            {
                "timestamp": rec.timestamp,
                "role": rec.role,
                "user_id": rec.user_id,
                "guild_id": str(md.get("guild_id", "")),
                "channel_id": str(md.get("channel_id", "")),
                "channel_name": str(md.get("channel_name", "")),
                "content": (rec.content or "")[:220],
            }
        )

    return json.dumps(
        {
            "status": "ok",
            "tool": "get_user_memory",
            "user_id": uid,
            "scope": scoped,
            "guild_id": gid,
            "channel_id": cid if cid > 0 else None,
            "profile_facts": facts,
            "messages": rows,
        },
        ensure_ascii=False,
    )


def search_memory(
    query: str,
    guild_id: str = "",
    channel_id: str = "",
    scope: str = "guild",
    limit: str = "8",
) -> str:
    gid = _int_or_none(guild_id)
    cid = _int_or_none(channel_id) or 0
    scoped = (scope or "guild").strip().lower()
    if scoped not in {"guild", "channel"}:
        scoped = "guild"
    max_items = _safe_int(limit, default=8, min_value=1, max_value=25)

    store = _store()
    results = asyncio.run(
        store.fetch_relevant_messages(
            guild_id=gid,
            channel_id=cid,
            query_text=query,
            limit=max_items,
            scope=scoped,
        )
    )

    items: list[dict[str, Any]] = []
    for rec in results:
        md = rec.metadata or {}
        items.append(
            {
                "timestamp": rec.timestamp,
                "role": rec.role,
                "user_id": rec.user_id,
                "content": (rec.content or ""),
                "channel_name": str(md.get("channel_name", "")),
            }
        )

    return json.dumps(
        {
            "status": "ok",
            "tool": "search_memory",
            "query": query,
            "scope": scoped,
            "count": len(items),
            "matches": items,
        },
        ensure_ascii=False,
    )

