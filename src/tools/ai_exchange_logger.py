from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _enabled() -> bool:
    return os.getenv("AI_EXCHANGE_LOG_ENABLED", "true").strip().lower() == "true"


def _max_chars() -> int:
    return max(200, _safe_int("AI_EXCHANGE_LOG_MAX_CHARS", 6000))


def _truncate(text: str) -> str:
    if len(text) <= _max_chars():
        return text
    return text[: _max_chars()] + "..."


def _log_path() -> Path:
    raw = os.getenv("AI_EXCHANGE_LOG_PATH", "./data/audit/ai_exchange.log").strip() or "./data/audit/ai_exchange.log"
    return Path(raw)


def log_ai_exchange(
    *,
    component: str,
    model: str,
    prompt: str = "",
    response: str = "",
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if not _enabled():
        return

    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "component": (component or "unknown").strip() or "unknown",
        "model": (model or "unknown").strip() or "unknown",
        "prompt": _truncate(prompt or ""),
        "response": _truncate(response or ""),
        "metadata": metadata or {},
    }
    if error:
        entry["error"] = _truncate(error)

    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            with path.open("a", encoding="utf-8") as wf:
                wf.write(line + "\n")
    except Exception:
        return
