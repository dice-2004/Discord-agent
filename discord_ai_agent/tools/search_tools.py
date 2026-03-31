from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from duckduckgo_search import DDGS
from langchain_core.tools import tool


def _safe_int(env_key: str, default_value: int) -> int:
    raw = os.getenv(env_key, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


@tool("web_search")
def web_search(query: str) -> str:
    """DuckDuckGoで一般Web検索を行い、重複ドメインを除いた上位結果を返す。"""
    clean_query = (query or "").strip()
    if not clean_query:
        return "検索クエリが空です。質問内容を具体的にしてください。"

    max_results = _safe_int("SEARCH_MAX_RESULTS", 5)
    timeout_sec = _safe_int("SEARCH_TIMEOUT_SEC", 10)
    retries = 2

    last_error: Exception | None = None
    raw_results: list[dict] = []

    for attempt in range(retries + 1):
        try:
            with DDGS(timeout=timeout_sec) as ddgs:
                raw_results = list(ddgs.text(clean_query, max_results=max_results * 3))
            break
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)

    if not raw_results:
        if last_error is not None:
            return "検索に失敗しました。時間をおいて再試行してください。"
        return "検索結果が見つかりませんでした。"

    filtered: list[dict[str, str]] = []
    seen_domains: set[str] = set()
    for item in raw_results:
        title = str(item.get("title", "(no title)")).strip()
        url = str(item.get("href") or item.get("url") or "").strip()
        body = str(item.get("body", "")).strip()
        if not url:
            continue

        domain = _domain(url)
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        if len(body) > 180:
            body = body[:177] + "..."

        filtered.append(
            {
                "title": title,
                "url": url,
                "body": body,
            }
        )
        if len(filtered) >= max_results:
            break

    if not filtered:
        return "検索結果が見つかりませんでした。"

    lines: list[str] = ["【Web検索結果】"]
    for index, result in enumerate(filtered, start=1):
        lines.append(f"{index}. {result['title']}")
        lines.append(f"URL: {result['url']}")
        lines.append(f"概要: {result['body'] or '概要なし'}")
        lines.append("")

    output = "\n".join(lines).strip()
    if len(output) > 2500:
        output = output[:2497] + "..."
    return output
