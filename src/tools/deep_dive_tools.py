from __future__ import annotations

import os

from tools.search_tools import web_search


def _safe_int(env_key: str, default_value: int) -> int:
    raw = os.getenv(env_key, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def source_deep_dive(topic: str, source: str = "auto") -> str:
    """特定ソースを深掘りするための検索クエリ束を生成して実行する。"""
    clean_topic = (topic or "").strip()
    clean_source = (source or "auto").strip().lower()
    if not clean_topic:
        return "deep dive対象のtopicが空です。"

    query_plan = _build_query_plan(clean_topic, clean_source)
    max_queries = _safe_int("DEEP_DIVE_MAX_QUERIES", 3)
    query_plan = _dedupe_queries(query_plan)[:max_queries]

    outputs: list[str] = []
    for idx, query in enumerate(query_plan, start=1):
        result = web_search(query)
        outputs.append(f"[DeepDive Query {idx}] {query}\n{result}")
        if "レート制限" in result:
            outputs.append("[DeepDive] レート制限を検知したため、残りクエリはスキップしました。")
            break

    merged = "\n\n".join(outputs)
    if len(merged) > 7000:
        merged = merged[:7000] + "..."
    return merged


def _build_query_plan(topic: str, source: str) -> list[str]:
    if source == "github":
        return [
            f"site:github.com {topic} issue",
            f"site:github.com {topic} release notes",
            f"site:github.com {topic} discussion",
        ]
    if source == "reddit":
        return [
            f"site:reddit.com {topic}",
            f"site:reddit.com {topic} latest",
            f"site:reddit.com {topic} troubleshooting",
        ]
    if source == "youtube":
        return [
            f"site:youtube.com {topic}",
            f"site:youtube.com {topic} review",
            f"site:youtube.com {topic} tutorial",
        ]
    if source == "x":
        return [
            f"site:x.com {topic}",
            f"site:x.com {topic} latest",
            f"site:x.com {topic} opinion",
        ]

    return [
        topic,
        f"{topic} official",
        f"{topic} latest updates",
    ]


def _dedupe_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        normalized = " ".join((query or "").strip().lower().split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(query)
    return unique
