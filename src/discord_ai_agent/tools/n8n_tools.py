from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _safe_int(env_key: str, default_value: int) -> int:
    raw = os.getenv(env_key, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def _allowed_actions() -> set[str]:
    raw = os.getenv("N8N_ALLOWED_ACTIONS", "").strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _truncate_text(text: str, limit: int = 1500) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _required_fields_map() -> dict[str, list[str]]:
    """Parse N8N_ACTION_REQUIRED_FIELDS.

    Format example:
    calendar_add:title,start_at;email_send:to,subject,body
    """
    raw = os.getenv("N8N_ACTION_REQUIRED_FIELDS", "").strip()
    if not raw:
        return {}

    mapping: dict[str, list[str]] = {}
    for block in raw.split(";"):
        part = block.strip()
        if not part or ":" not in part:
            continue
        action, fields_raw = part.split(":", 1)
        key = action.strip()
        if not key:
            continue
        fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
        if fields:
            mapping[key] = fields
    return mapping


def trigger_n8n_webhook(action: str, payload_json: str = "{}") -> str:
    """Trigger an allowed n8n webhook action with a JSON payload."""
    clean_action = (action or "").strip()
    if not clean_action:
        return "action が空です。"

    allowed = _allowed_actions()
    if clean_action not in allowed:
        allowed_list = ", ".join(sorted(allowed)) if allowed else "(未設定)"
        return f"このactionは許可されていません: {clean_action}\n許可action: {allowed_list}"

    try:
        payload = json.loads(payload_json or "{}")
    except Exception:
        return "payload_json はJSON形式で指定してください。"

    if not isinstance(payload, dict):
        return "payload_json はJSONオブジェクトで指定してください。"

    required_fields = _required_fields_map().get(clean_action, [])
    missing = [name for name in required_fields if name not in payload]
    if missing:
        return (
            f"action={clean_action} で必須キーが不足しています: {', '.join(missing)}\n"
            f"required: {', '.join(required_fields)}"
        )

    base_url = os.getenv("N8N_WEBHOOK_BASE_URL", "").strip()
    if not base_url:
        return "n8n webhook base URL が未設定です。N8N_WEBHOOK_BASE_URL を設定してください。"

    target_url = base_url.rstrip("/")
    token = os.getenv("N8N_WEBHOOK_TOKEN", "").strip()
    timeout_sec = _safe_int("N8N_TIMEOUT_SEC", 12)
    retry_count = max(0, _safe_int("N8N_RETRY_COUNT", 1))
    retry_backoff_sec = max(0, _safe_int("N8N_RETRY_BACKOFF_SEC", 1))

    request_payload = {
        "action": clean_action,
        "parameters": payload,
        "token": token,
    }
    body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "discord-ai-agent/1.0",
    }
    if token:
        headers["X-Webhook-Token"] = token

    req = Request(target_url, data=body, method="POST", headers=headers)
    attempts = retry_count + 1

    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=timeout_sec) as res:
                resp_body = res.read().decode("utf-8", errors="replace")
                status = int(getattr(res, "status", 200))
            compact = _truncate_text(resp_body)
            return f"[status={status}] action={clean_action}\n{compact or '(レスポンス本文なし)'}"
        except HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""

            is_retryable = int(getattr(exc, "code", 0)) >= 500
            if is_retryable and attempt < attempts:
                time.sleep(retry_backoff_sec)
                continue

            compact = _truncate_text(error_body)
            return (
                f"n8n webhook がHTTPエラーを返しました: status={exc.code}, action={clean_action}\n"
                f"{compact or str(exc)}"
            )
        except URLError as exc:
            if attempt < attempts:
                time.sleep(retry_backoff_sec)
                continue
            return f"n8n webhook に接続できませんでした: {exc}"
        except Exception as exc:
            if attempt < attempts:
                time.sleep(retry_backoff_sec)
                continue
            return f"n8n webhook 呼び出しに失敗しました: {exc}"

    return "n8n webhook 呼び出しに失敗しました: retries exhausted"
