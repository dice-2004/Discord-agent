from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from duckduckgo_search import DDGS


_SEARCH_CACHE: dict[str, tuple[float, str]] = {}
_RATE_LIMIT_UNTIL: float = 0.0


def _safe_int(env_key: str, default_value: int) -> int:
    raw = os.getenv(env_key, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _decode_duckduckgo_redirect(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query or "")
    uddg = query.get("uddg", [])
    if uddg:
        return unquote(uddg[0])
    return url


def _strip_markdown_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    cleaned = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", cleaned)
    cleaned = re.sub(r"\[([^\]]*)\]\([^\)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"\(https?://[^\)]+\)", "", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"\*{1,2}", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _search_via_jina_duckduckgo_html(query: str, max_results: int, timeout_sec: int) -> list[dict[str, str]]:
    target = f"https://r.jina.ai/http://html.duckduckgo.com/html/?q={quote_plus(query)}"
    req = Request(target, headers={"User-Agent": "discord-ai-agent/1.0"})
    with urlopen(req, timeout=max(8, timeout_sec + 2)) as res:
        body = res.read().decode("utf-8", errors="replace")

    pattern = re.compile(
        r"##\s+\[(?P<title>.+?)\]\((?P<url>https?://[^\)]+)\)\n(?P<section>.*?)(?=\n##\s+\[|\Z)",
        flags=re.DOTALL,
    )
    results: list[dict[str, str]] = []
    seen_domains: set[str] = set()

    for match in pattern.finditer(body):
        title = _strip_markdown_text(match.group("title"))
        raw_url = match.group("url").strip()
        url = _decode_duckduckgo_redirect(raw_url)
        if not url.startswith("http"):
            continue

        domain = _domain(url)
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        section = match.group("section") or ""
        lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
        summary = ""
        for ln in lines:
            stripped = _strip_markdown_text(ln)
            if not stripped:
                continue
            if stripped.startswith("http://") or stripped.startswith("https://"):
                continue
            lowered = stripped.lower()
            if lowered.startswith("x.com/") or lowered.startswith("youtube.com/") or lowered.startswith("www.youtube.com/"):
                continue
            if re.fullmatch(r"(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}/\S*", lowered):
                continue
            summary = stripped
            break

        if len(summary) > 180:
            summary = summary[:177] + "..."
        results.append(
            {
                "title": title or "(no title)",
                "url": url,
                "body": summary,
            }
        )
        if len(results) >= max_results:
            break

    return results


def web_search(query: str) -> str:
    """DuckDuckGoで一般Web検索を行い、重複ドメインを除いた上位結果を返す。"""
    global _RATE_LIMIT_UNTIL

    clean_query = (query or "").strip()
    if not clean_query:
        return "検索クエリが空です。質問内容を具体的にしてください。"

    max_results = _safe_int("SEARCH_MAX_RESULTS", 5)
    timeout_sec = _safe_int("SEARCH_TIMEOUT_SEC", 10)
    cache_ttl_sec = _safe_int("SEARCH_CACHE_TTL_SEC", 180)
    cooldown_sec = _safe_int("SEARCH_COOLDOWN_SEC", 45)
    retries = 2
    lower_query = clean_query.lower()
    prefer_fallback = any(k in lower_query for k in ("site:x.com", "site:twitter.com", "site:youtube.com"))

    def _format_results(title: str, entries: list[dict[str, str]]) -> str:
        lines: list[str] = [title]
        for index, result in enumerate(entries, start=1):
            lines.append(f"{index}. {result['title']}")
            lines.append(f"URL: {result['url']}")
            lines.append(f"概要: {result['body'] or '概要なし'}")
            lines.append("")
        output = "\n".join(lines).strip()
        if len(output) > 2500:
            output = output[:2497] + "..."
        return output

    now = time.time()
    cached = _SEARCH_CACHE.get(clean_query)
    if cached is not None:
        cached_at, cached_value = cached
        if now - cached_at <= cache_ttl_sec:
            return cached_value

    if prefer_fallback:
        try:
            fallback_results = _search_via_jina_duckduckgo_html(clean_query, max_results=max_results, timeout_sec=timeout_sec)
            if fallback_results:
                output = _format_results("【Web検索結果（フォールバック）】", fallback_results)
                _SEARCH_CACHE[clean_query] = (time.time(), output)
                return output
        except Exception:
            pass

    if now < _RATE_LIMIT_UNTIL:
        try:
            fallback_results = _search_via_jina_duckduckgo_html(clean_query, max_results=max_results, timeout_sec=timeout_sec)
            if fallback_results:
                output = _format_results("【Web検索結果（フォールバック）】", fallback_results)
                _SEARCH_CACHE[clean_query] = (time.time(), output)
                return output
        except Exception:
            pass
        return "検索先がレート制限中です。少し時間をおいて再試行してください。"

    last_error: Exception | None = None
    raw_results: list[dict] = []

    for attempt in range(retries + 1):
        try:
            with DDGS(timeout=timeout_sec) as ddgs:
                raw_results = list(ddgs.text(clean_query, max_results=max_results * 3))
            break
        except Exception as exc:
            last_error = exc
            lower = str(exc).lower()
            if "ratelimit" in lower or " 202" in lower:
                _RATE_LIMIT_UNTIL = time.time() + cooldown_sec
            if attempt < retries:
                time.sleep(2**attempt)

    if not raw_results:
        try:
            fallback_results = _search_via_jina_duckduckgo_html(clean_query, max_results=max_results, timeout_sec=timeout_sec)
        except Exception:
            fallback_results = []
        if fallback_results:
            output = _format_results("【Web検索結果（フォールバック）】", fallback_results)
            _SEARCH_CACHE[clean_query] = (time.time(), output)
            return output

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

    output = _format_results("【Web検索結果】", filtered)

    _SEARCH_CACHE[clean_query] = (time.time(), output)
    return output
