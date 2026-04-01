from __future__ import annotations

import os
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


def _safe_int(env_key: str, default_value: int) -> int:
    raw = os.getenv(env_key, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def read_url_markdown(url: str) -> str:
    """Jina Reader経由でURL本文をMarkdownとして取得する。"""
    target = (url or "").strip()
    if not target:
        return "URLが空です。読み取り対象のURLを指定してください。"

    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"}:
        return "URLはhttp:// または https:// 形式で指定してください。"

    timeout_sec = _safe_int("READER_TIMEOUT_SEC", 12)
    max_chars = _safe_int("READER_MAX_CHARS", 5000)

    reader_url = f"https://r.jina.ai/http://{quote(target, safe=':/?&=#%')}"
    req = Request(reader_url, headers={"User-Agent": "discord-ai-agent/1.0"})

    try:
        with urlopen(req, timeout=timeout_sec) as res:
            body = res.read().decode("utf-8", errors="replace")
    except Exception:
        return "ページ本文の取得に失敗しました。時間をおいて再試行してください。"

    body = body.strip()
    if not body:
        return "本文を取得できませんでした。"

    if len(body) > max_chars:
        body = body[:max_chars] + "..."

    return f"【URL本文要約素材】\nURL: {target}\n\n{body}"
