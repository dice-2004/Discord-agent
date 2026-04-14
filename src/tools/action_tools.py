from __future__ import annotations

import json
import os
import csv
import re
import shutil
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _safe_int(env_key: str, default_value: int) -> int:
    raw = os.getenv(env_key, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def _allowed_actions() -> set[str]:
    raw = os.getenv("INTERNAL_ALLOWED_ACTIONS", "").strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _required_fields_map() -> dict[str, list[str]]:
    raw = os.getenv("INTERNAL_ACTION_REQUIRED_FIELDS", "").strip()
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


def _normalize_action_name(action: str) -> str:
    key = (action or "").strip()
    aliases = {
        "calendar_add_event": "add_calendar_event",
        "calendar_get_events": "get_calendar_events",
        "create_calendar_event": "add_calendar_event",
        "list_calendar_events": "get_calendar_events",
        "task_update": "update_task",
        "task_delete": "delete_task",
    }
    return aliases.get(key, key)


def _normalize_add_calendar_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    if str(normalized.get("title", "")).strip():
        return normalized

    title_aliases = [
        "summary",
        "subject",
        "name",
        "task",
        "event",
        "item",
        "content",
        "件名",
        "内容",
        "タイトル",
    ]
    for key in title_aliases:
        candidate = str(normalized.get(key, "")).strip()
        if candidate:
            normalized["title"] = candidate
            break

    return normalized


def _truncate_text(text: str, limit: int = 1200) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "y"}


def _as_json_line(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _parse_iso8601(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    default_tz = timezone(timedelta(hours=9))
    if (os.getenv("TZ", "Asia/Tokyo").strip() or "Asia/Tokyo").lower() not in {"asia/tokyo", "jst"}:
        default_tz = timezone.utc

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        return parsed
    except Exception:
        pass

    patterns = [
        r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s+(\d{1,2})(?::(\d{2}))?)?$",
        r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[ T](\d{1,2})(?::(\d{2}))?)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text)
        if not match:
            continue
        try:
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3))
            hour = int(match.group(4) or "0")
            minute = int(match.group(5) or "0")
            return datetime(year, month, day, hour, minute, tzinfo=default_tz)
        except Exception:
            continue

    return None


def _parse_date_only(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    patterns = [
        r"^(\d{4})-(\d{1,2})-(\d{1,2})$",
        r"^(\d{4})/(\d{1,2})/(\d{1,2})$",
        r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text)
        if not match:
            continue
        try:
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3))
            parsed = datetime(year, month, day)
            return parsed.strftime("%Y-%m-%d")
        except Exception:
            continue
    return None


def _normalize_mmdd_to_date(token: str, default_year: int) -> str | None:
    text = (token or "").strip()
    if not text:
        return None
    parts = text.split("/")
    if len(parts) != 2:
        return None
    try:
        month = int(parts[0])
        day = int(parts[1])
        parsed = datetime(default_year, month, day)
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None


def _extract_date_tokens(raw_value: object) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        tokens: list[str] = []
        for item in raw_value:
            text = str(item or "").strip()
            if not text:
                continue
            tokens.extend([part.strip() for part in re.split(r"[,\s]+", text) if part.strip()])
        return tokens

    text = str(raw_value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,\s]+", text) if part.strip()]


def _resolve_target_dates(payload: dict[str, object], *, key: str = "dates") -> tuple[list[str], str | None]:
    default_year = datetime.now().year
    year_raw = str(payload.get("year", "")).strip()
    if year_raw:
        try:
            default_year = int(year_raw)
        except Exception:
            return [], "year は4桁の数値で指定してください。"

    raw = payload.get(key)
    if raw is None:
        raw = payload.get("date_list")
    if raw is None:
        raw = payload.get("target_dates")

    tokens = _extract_date_tokens(raw)
    if not tokens:
        return [], "dates が空です。"

    resolved: list[str] = []
    seen: set[str] = set()
    month_context: int | None = None
    for token in tokens:
        iso_date = _parse_date_only(token)
        if not iso_date:
            iso_date = _normalize_mmdd_to_date(token, default_year)
            if iso_date:
                try:
                    month_context = int(token.split("/")[0])
                except Exception:
                    month_context = None
        # 例: "4/3,4,5,7" のように月が省略された day-only トークンを許可する。
        if not iso_date and month_context is not None and re.fullmatch(r"\d{1,2}", token):
            try:
                day = int(token)
                parsed = datetime(default_year, month_context, day)
                iso_date = parsed.strftime("%Y-%m-%d")
            except Exception:
                iso_date = None
        if not iso_date:
            return [], f"日付形式が不正です: {token}"
        if iso_date in seen:
            continue
        seen.add(iso_date)
        resolved.append(iso_date)
    return resolved, None


def _calendar_provider() -> str:
    return (os.getenv("CALENDAR_PROVIDER", "google").strip().lower() or "google")


def _google_calendar_auth_url() -> str:
    return os.getenv(
        "GOOGLE_CALENDAR_AUTH_URL",
        "https://console.cloud.google.com/apis/credentials",
    ).strip()


def _google_tasks_auth_url() -> str:
    return os.getenv(
        "GOOGLE_TASKS_AUTH_URL",
        "https://console.cloud.google.com/apis/credentials",
    ).strip()

def _append_local_calendar_event_record(event: dict[str, object]) -> None:
    storage_path = _calendar_storage_path()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    with storage_path.open("a", encoding="utf-8") as wf:
        wf.write(json.dumps(event, ensure_ascii=False) + "\n")

def _load_local_calendar_events(min_dt: datetime, max_dt: datetime) -> tuple[list[dict[str, object]], str | None]:
    storage_path = _calendar_storage_path()
    if not storage_path.exists():
        return [], None

    events: list[dict[str, object]] = []
    try:
        with storage_path.open("r", encoding="utf-8") as rf:
            for line in rf:
                text = line.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue

                start_dt = _parse_iso8601(event.get("start_time"))
                end_dt = _parse_iso8601(event.get("end_time"))
                if start_dt is None and event.get("start_date"):
                    start_dt = _parse_iso8601(f"{_parse_date_only(event.get('start_date')) or str(event.get('start_date')).strip()}T00:00:00")
                if end_dt is None and event.get("end_date_exclusive"):
                    end_dt = _parse_iso8601(f"{_parse_date_only(event.get('end_date_exclusive')) or str(event.get('end_date_exclusive')).strip()}T00:00:00")

                if start_dt is None or end_dt is None:
                    continue
                if end_dt > min_dt and start_dt < max_dt:
                    events.append(
                        {
                            "id": str(event.get("id", "")),
                            "title": str(event.get("title", "")),
                            "start_time": start_dt.isoformat(),
                            "end_time": end_dt.isoformat(),
                            "description": str(event.get("description", "")),
                        }
                    )
    except Exception as exc:
        return [], _truncate_text(str(exc))

    return events, None


def _google_calendar_creds() -> dict[str, str]:
    return {
        "client_id": os.getenv("GOOGLE_CALENDAR_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET", "").strip(),
        "refresh_token": os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "").strip(),
        "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary",
    }


def _google_access_token_from_creds(creds: dict[str, str], provider: str) -> tuple[str | None, str | None]:
    required = ["client_id", "client_secret", "refresh_token"]
    missing = [key for key in required if not creds.get(key)]
    if missing:
        return None, f"missing_credentials:{provider}:{','.join(missing)}"

    body = urlencode(
        {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        token = str(payload.get("access_token", "")).strip()
        if not token:
            return None, "token_response_missing_access_token"
        return token, None
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        lowered = raw.lower()
        if "invalid_grant" in lowered:
            hint = (
                f"invalid_grant:{provider}:refresh tokenが無効です。"
                " refresh tokenを発行したOAuthクライアントID/Secretの組み合わせと現在値が一致しているか確認してください。"
            )
            return None, _truncate_text(hint)
        return None, _truncate_text(raw)
    except Exception as exc:
        return None, _truncate_text(str(exc))


def _google_access_token() -> tuple[str | None, str | None]:
    return _google_access_token_from_creds(_google_calendar_creds(), "google_oauth2")


def _google_calendar_insert_event(
    title: str,
    description: str,
    start_dt: datetime | None,
    end_dt: datetime | None,
    all_day_start_date: str | None = None,
    all_day_end_date_exclusive: str | None = None,
    calendar_id_override: str | None = None,
) -> str:
    token, err = _google_access_token()
    if not token:
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "add_calendar_event",
                "detail": f"Google Calendar認証が未設定または無効です: {err or 'unknown'}",
                "auth_url": _google_calendar_auth_url(),
            }
        )

    creds = _google_calendar_creds()
    calendar_id = (calendar_id_override or "").strip() or creds["calendar_id"]
    tz_name = os.getenv("TZ", "Asia/Tokyo").strip() or "Asia/Tokyo"
    body: dict[str, object] = {"summary": title, "description": description}
    if all_day_start_date and all_day_end_date_exclusive:
        body["start"] = {"date": all_day_start_date}
        body["end"] = {"date": all_day_end_date_exclusive}
    elif start_dt is not None and end_dt is not None:
        body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": tz_name}
        body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": tz_name}
    else:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_time_range",
                "action": "add_calendar_event",
                "detail": "timed event または all-day event の時刻/日付が不足しています。",
            }
        )
    req = Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "discord-ai-agent/1.0",
        },
    )

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            status = int(getattr(res, "status", 200))
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        if status not in (200, 201):
            return _as_json_line(
                {
                    "status": "error",
                    "code": "add_calendar_event_failed",
                    "action": "add_calendar_event",
                    "status_code": status,
                    "detail": _truncate_text(str(payload)),
                }
            )
        try:
            mirror_event = {
                "id": str(payload.get("id", "")) or f"cal-{uuid4().hex[:12]}",
                "title": title,
                "description": description,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if all_day_start_date and all_day_end_date_exclusive:
                mirror_event["all_day"] = True
                mirror_event["start_date"] = all_day_start_date
                mirror_event["end_date_exclusive"] = all_day_end_date_exclusive
            else:
                mirror_event["start_time"] = start_dt.isoformat() if start_dt is not None else ""
                mirror_event["end_time"] = end_dt.isoformat() if end_dt is not None else ""
            _append_local_calendar_event_record(mirror_event)
        except Exception:
            pass
        return _as_json_line(
            {
                "status": "ok",
                "action": "add_calendar_event",
                "event_id": payload.get("id"),
                "event_url": payload.get("htmlLink"),
                "calendar_id": calendar_id,
                "title": title,
                "all_day": bool(all_day_start_date and all_day_end_date_exclusive),
                "start_time": start_dt.isoformat() if start_dt is not None else None,
                "end_time": end_dt.isoformat() if end_dt is not None else None,
                "start_date": all_day_start_date,
                "end_date_exclusive": all_day_end_date_exclusive,
            }
        )
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        return _as_json_line(
            {
                "status": "error",
                "code": "add_calendar_event_failed",
                "action": "add_calendar_event",
                "status_code": int(getattr(exc, "code", 500)),
                "detail": _truncate_text(raw),
            }
        )
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "add_calendar_event_failed",
                "action": "add_calendar_event",
                "detail": _truncate_text(str(exc)),
            }
        )


def _google_tasks_insert_task(title: str, due_date: str | None = None, notes: str = "") -> str:
    """Insert a task into Google Tasks via API (default task list)."""
    token, err = _google_access_token()
    if not token:
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "add_task",
                "detail": f"Google Tasks認証が未設定または無効です: {err or 'unknown'}",
            }
        )

    body: dict[str, object] = {"title": title}
    if due_date:
        body["due"] = f"{due_date}T00:00:00.000Z"
    if notes:
        body["notes"] = notes

    req = Request(
        "https://www.googleapis.com/tasks/v1/lists/@default/tasks",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "discord-ai-agent/1.0",
        },
    )

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            status = int(getattr(res, "status", 200))
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        if status not in (200, 201):
            return _as_json_line(
                {
                    "status": "error",
                    "code": "add_task_failed",
                    "action": "add_task",
                    "status_code": status,
                    "detail": _truncate_text(str(payload)),
                }
            )
        return _as_json_line(
            {
                "status": "ok",
                "action": "add_task",
                "task_id": payload.get("id"),
                "title": title,
                "due_date": due_date,
                "web_link": f"https://tasks.google.com",
            }
        )
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        code = int(getattr(exc, "code", 500))
        detail = _truncate_text(raw)
        if code == 403 and "insufficient" in raw.lower():
            detail = (
                "Google Tasks の権限スコープ不足です。"
                " refresh token を tasks スコープ付きで再発行してください"
                " (https://www.googleapis.com/auth/tasks)。"
            )
        return _as_json_line(
            {
                "status": "error",
                "code": "add_task_failed",
                "action": "add_task",
                "status_code": code,
                "detail": detail,
                "auth_url": _google_tasks_auth_url(),
            }
        )
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "add_task_failed",
                "action": "add_task",
                "detail": _truncate_text(str(exc)),
            }
        )


def _google_tasks_list_all_tasks() -> tuple[list[dict[str, object]], str | None, int | None]:
    token, err = _google_access_token()
    if not token:
        return [], err or "auth_required", None

    tasks: list[dict[str, object]] = []
    page_token: str | None = None
    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)

    try:
        while True:
            query_params: dict[str, str] = {
                "showCompleted": "true",
                "showHidden": "true",
                "maxResults": "100",
            }
            if page_token:
                query_params["pageToken"] = page_token

            req = Request(
                f"https://www.googleapis.com/tasks/v1/lists/@default/tasks?{urlencode(query_params)}",
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "discord-ai-agent/1.0",
                },
            )

            with urlopen(req, timeout=timeout_sec) as res:
                status = int(getattr(res, "status", 200))
                raw = res.read().decode("utf-8", errors="replace")

            payload = json.loads(raw) if raw else {}
            if status != 200:
                return [], _truncate_text(str(payload)), status

            items = payload.get("items", []) if isinstance(payload, dict) else []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    tasks.append(item)

            page_token = str(payload.get("nextPageToken", "")).strip() if isinstance(payload, dict) else ""
            if not page_token:
                break

        return tasks, None, 200
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        return [], _truncate_text(raw), int(getattr(exc, "code", 500))
    except Exception as exc:
        return [], _truncate_text(str(exc)), None


def _google_tasks_update_task(
    task_id: str,
    *,
    title: str | None = None,
    due_date: str | None = None,
    notes: str | None = None,
    completed: bool | None = None,
) -> str:
    token, err = _google_access_token()
    if not token:
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "update_task",
                "detail": f"Google Tasks認証が未設定または無効です: {err or 'unknown'}",
            }
        )

    body: dict[str, object] = {}
    if title is not None:
        body["title"] = title
    if due_date:
        body["due"] = f"{due_date}T00:00:00.000Z"
    if notes is not None:
        body["notes"] = notes
    if completed is not None:
        body["status"] = "completed" if completed else "needsAction"

    if not body:
        return _as_json_line(
            {
                "status": "error",
                "code": "missing_required_fields",
                "action": "update_task",
                "detail": "更新内容が空です。",
            }
        )

    req = Request(
        f"https://www.googleapis.com/tasks/v1/lists/@default/tasks/{quote(task_id, safe='')}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "discord-ai-agent/1.0",
        },
    )

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            status = int(getattr(res, "status", 200))
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        if status != 200:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "update_task_failed",
                    "action": "update_task",
                    "status_code": status,
                    "detail": _truncate_text(str(payload)),
                }
            )
        return _as_json_line(
            {
                "status": "ok",
                "action": "update_task",
                "task_id": payload.get("id", task_id),
                "title": payload.get("title", title),
                "due_date": str(payload.get("due", due_date or ""))[:10] if payload.get("due") or due_date else None,
                "web_link": payload.get("selfLink") or "https://tasks.google.com",
            }
        )
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        return _as_json_line(
            {
                "status": "error",
                "code": "update_task_failed",
                "action": "update_task",
                "status_code": int(getattr(exc, "code", 500)),
                "detail": _truncate_text(raw),
            }
        )
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "update_task_failed",
                "action": "update_task",
                "detail": _truncate_text(str(exc)),
            }
        )


def _google_tasks_delete_task(task_id: str) -> str:
    token, err = _google_access_token()
    if not token:
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "delete_task",
                "detail": f"Google Tasks認証が未設定または無効です: {err or 'unknown'}",
            }
        )

    req = Request(
        f"https://www.googleapis.com/tasks/v1/lists/@default/tasks/{quote(task_id, safe='')}",
        method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "discord-ai-agent/1.0",
        },
    )

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            status = int(getattr(res, "status", 200))
            raw = res.read().decode("utf-8", errors="replace") if hasattr(res, "read") else ""
        if status not in (200, 204):
            return _as_json_line(
                {
                    "status": "error",
                    "code": "delete_task_failed",
                    "action": "delete_task",
                    "status_code": status,
                    "detail": _truncate_text(raw),
                }
            )
        return _as_json_line(
            {
                "status": "ok",
                "action": "delete_task",
                "task_id": task_id,
                "web_link": "https://tasks.google.com",
            }
        )
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        return _as_json_line(
            {
                "status": "error",
                "code": "delete_task_failed",
                "action": "delete_task",
                "status_code": int(getattr(exc, "code", 500)),
                "detail": _truncate_text(raw),
            }
        )
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "delete_task_failed",
                "action": "delete_task",
                "detail": _truncate_text(str(exc)),
            }
        )


def _google_calendar_list_events(min_dt: datetime, max_dt: datetime, calendar_id_override: str | None = None) -> str:
    token, err = _google_access_token()
    if not token:
        local_events, local_err = _load_local_calendar_events(min_dt, max_dt)
        if local_err is None:
            return _as_json_line(
                {
                    "status": "ok",
                    "action": "get_calendar_events",
                    "calendar_id": (calendar_id_override or "").strip() or _google_calendar_creds()["calendar_id"],
                    "events": local_events,
                    "count": len(local_events),
                    "source": "local_cache",
                }
            )
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "get_calendar_events",
                "detail": f"Google Calendar認証が未設定または無効です: {err or 'unknown'}",
                "auth_url": _google_calendar_auth_url(),
            }
        )

    creds = _google_calendar_creds()
    calendar_id = (calendar_id_override or "").strip() or creds["calendar_id"]
    limit = max(1, min(_safe_int("CALENDAR_EVENTS_LIST_LIMIT", 250), 2000))

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        events: list[dict[str, object]] = []
        next_page_token: str | None = None
        while True:
            page_size = max(1, min(limit - len(events), 2500))
            if page_size <= 0:
                break
            query_params = {
                "timeMin": min_dt.isoformat(),
                "timeMax": max_dt.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": str(page_size),
            }
            if next_page_token:
                query_params["pageToken"] = next_page_token

            query = urlencode(query_params)
            req = Request(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events?{query}",
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "discord-ai-agent/1.0",
                },
            )

            with urlopen(req, timeout=timeout_sec) as res:
                status = int(getattr(res, "status", 200))
                raw = res.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            if status != 200:
                return _as_json_line(
                    {
                        "status": "error",
                        "code": "get_calendar_events_failed",
                        "action": "get_calendar_events",
                        "status_code": status,
                        "detail": _truncate_text(str(payload)),
                    }
                )

            items = payload.get("items", []) if isinstance(payload, dict) else []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                start_obj = item.get("start", {})
                end_obj = item.get("end", {})
                if not isinstance(start_obj, dict) or not isinstance(end_obj, dict):
                    continue
                start_text = str(start_obj.get("dateTime") or start_obj.get("date") or "")
                end_text = str(end_obj.get("dateTime") or end_obj.get("date") or "")
                events.append(
                    {
                        "id": str(item.get("id", "")),
                        "title": str(item.get("summary", "")),
                        "start_time": start_text,
                        "end_time": end_text,
                        "description": str(item.get("description", "")),
                        "event_url": str(item.get("htmlLink", "")),
                    }
                )

            next_page_token = str(payload.get("nextPageToken", "")).strip() if isinstance(payload, dict) else ""
            if not next_page_token or len(events) >= limit:
                break

        return _as_json_line(
            {
                "status": "ok",
                "action": "get_calendar_events",
                "calendar_id": calendar_id,
                "events": events,
                "count": len(events),
            }
        )
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        local_events, local_err = _load_local_calendar_events(min_dt, max_dt)
        if local_err is None:
            return _as_json_line(
                {
                    "status": "ok",
                    "action": "get_calendar_events",
                    "calendar_id": calendar_id,
                    "events": local_events,
                    "count": len(local_events),
                    "source": "local_cache",
                    "fallback_detail": _truncate_text(raw),
                }
            )
        return _as_json_line(
            {
                "status": "error",
                "code": "get_calendar_events_failed",
                "action": "get_calendar_events",
                "status_code": int(getattr(exc, "code", 500)),
                "detail": _truncate_text(raw),
            }
        )
    except Exception as exc:
        local_events, local_err = _load_local_calendar_events(min_dt, max_dt)
        if local_err is None:
            return _as_json_line(
                {
                    "status": "ok",
                    "action": "get_calendar_events",
                    "calendar_id": calendar_id,
                    "events": local_events,
                    "count": len(local_events),
                    "source": "local_cache",
                    "fallback_detail": _truncate_text(str(exc)),
                }
            )
        return _as_json_line(
            {
                "status": "error",
                "code": "get_calendar_events_failed",
                "action": "get_calendar_events",
                "detail": _truncate_text(str(exc)),
            }
        )


def _google_calendar_delete_event(event_id: str, calendar_id_override: str | None = None) -> str:
    token, err = _google_access_token()
    if not token:
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "delete_calendar_event",
                "detail": f"Google Calendar認証が未設定または無効です: {err or 'unknown'}",
                "auth_url": _google_calendar_auth_url(),
            }
        )

    creds = _google_calendar_creds()
    calendar_id = (calendar_id_override or "").strip() or creds["calendar_id"]
    req = Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{quote(event_id, safe='')}",
        method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "discord-ai-agent/1.0",
        },
    )

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            status = int(getattr(res, "status", 200))
            raw = res.read().decode("utf-8", errors="replace") if hasattr(res, "read") else ""
        if status not in (200, 204):
            return _as_json_line(
                {
                    "status": "error",
                    "code": "delete_calendar_event_failed",
                    "action": "delete_calendar_event",
                    "event_id": event_id,
                    "status_code": status,
                    "detail": _truncate_text(raw),
                }
            )
        return _as_json_line(
            {
                "status": "ok",
                "action": "delete_calendar_event",
                "event_id": event_id,
                "calendar_id": calendar_id,
            }
        )
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        return _as_json_line(
            {
                "status": "error",
                "code": "delete_calendar_event_failed",
                "action": "delete_calendar_event",
                "event_id": event_id,
                "status_code": int(getattr(exc, "code", 500)),
                "detail": _truncate_text(raw),
            }
        )
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "delete_calendar_event_failed",
                "action": "delete_calendar_event",
                "event_id": event_id,
                "detail": _truncate_text(str(exc)),
            }
        )


def _iso_day_of(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    if len(text) >= 10:
        direct = _parse_date_only(text[:10])
        if direct:
            return direct

    parsed = _parse_iso8601(text)
    if parsed is not None:
        return parsed.date().strftime("%Y-%m-%d")

    return _parse_date_only(text)


def _parse_allowed_roots() -> list[Path]:
    raw = os.getenv("BACKUP_ALLOWED_ROOTS", "./data,./src,./docs").strip()
    roots: list[Path] = []
    for item in raw.split(","):
        part = item.strip()
        if not part:
            continue
        roots.append(Path(part).expanduser().resolve())
    return roots


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _handle_backup_server_data(payload: dict[str, object]) -> str:
    target = str(payload.get("target", "")).strip()
    if not target:
        return _as_json_line(
            {
                "status": "error",
                "code": "missing_required_fields",
                "action": "backup_server_data",
                "missing": ["target"],
                "required": ["target"],
            }
        )

    target_path = Path(target).expanduser().resolve()
    if not target_path.exists():
        return _as_json_line(
            {
                "status": "error",
                "code": "target_not_found",
                "action": "backup_server_data",
                "target": str(target_path),
            }
        )

    allowed_roots = _parse_allowed_roots()
    if allowed_roots and not any(_is_under_root(target_path, root) for root in allowed_roots):
        return _as_json_line(
            {
                "status": "error",
                "code": "forbidden_target",
                "action": "backup_server_data",
                "target": str(target_path),
                "allowed_roots": [str(root) for root in allowed_roots],
            }
        )

    output_dir = Path(os.getenv("BACKUP_OUTPUT_DIR", "./data/runtime/backups")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = target_path.name or "target"
    archive_base = output_dir / f"{slug}-{stamp}"
    try:
        archive_path = Path(
            shutil.make_archive(
                str(archive_base),
                "gztar",
                root_dir=str(target_path.parent),
                base_dir=target_path.name,
            )
        )
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "backup_failed",
                "action": "backup_server_data",
                "target": str(target_path),
                "detail": _truncate_text(str(exc)),
            }
        )

    try:
        size_bytes = archive_path.stat().st_size
    except Exception:
        size_bytes = 0

    return _as_json_line(
        {
            "status": "ok",
            "action": "backup_server_data",
            "target": str(target_path),
            "archive_path": str(archive_path),
            "archive_size_bytes": int(size_bytes),
        }
    )


def _calendar_storage_path() -> Path:
    return Path(
        os.getenv("CALENDAR_EVENTS_STORAGE_PATH", "./data/runtime/calendar_events.jsonl")
    ).expanduser().resolve()


def _handle_add_calendar_event(payload: dict[str, object]) -> str:
    title = str(payload.get("title", "")).strip()
    start_time_text = str(payload.get("start_time", "")).strip()
    end_time_text = str(payload.get("end_time", "")).strip()
    description = str(payload.get("description", "")).strip()
    calendar_id_override = str(payload.get("calendar_id", "")).strip() or None
    all_day = _as_bool(payload.get("all_day", False))

    all_day_start = _parse_date_only(payload.get("date") or payload.get("start_date"))
    all_day_end_inclusive = _parse_date_only(payload.get("end_date"))

    if all_day:
        if all_day_start is None:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "invalid_date_format",
                    "action": "add_calendar_event",
                    "detail": "終日予定は date（または start_date）を YYYY-MM-DD 形式で指定してください。",
                }
            )
        try:
            start_date_obj = datetime.fromisoformat(all_day_start).date()
            end_inclusive_obj = (
                datetime.fromisoformat(all_day_end_inclusive).date()
                if all_day_end_inclusive
                else start_date_obj
            )
            if end_inclusive_obj < start_date_obj:
                return _as_json_line(
                    {
                        "status": "error",
                        "code": "invalid_time_range",
                        "action": "add_calendar_event",
                        "detail": "end_date は date/start_date 以降である必要があります。",
                    }
                )
            end_exclusive_obj = end_inclusive_obj + timedelta(days=1)
            all_day_end_exclusive = end_exclusive_obj.strftime("%Y-%m-%d")
        except Exception:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "invalid_date_format",
                    "action": "add_calendar_event",
                    "detail": "終日予定の日付形式が不正です。",
                }
            )

        if _calendar_provider() == "local":
            storage_path = _calendar_storage_path()
            event = {
                "id": f"cal-{uuid4().hex[:12]}",
                "title": title,
                "all_day": True,
                "start_date": all_day_start,
                "end_date_exclusive": all_day_end_exclusive,
                "description": description,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                    _append_local_calendar_event_record(event)
            except Exception as exc:
                return _as_json_line(
                    {
                        "status": "error",
                        "code": "add_calendar_event_failed",
                        "action": "add_calendar_event",
                        "detail": _truncate_text(str(exc)),
                    }
                )
            return _as_json_line(
                {
                    "status": "ok",
                    "action": "add_calendar_event",
                    "event_id": event["id"],
                    "title": title,
                    "all_day": True,
                    "start_date": all_day_start,
                    "end_date_exclusive": all_day_end_exclusive,
                    "storage_path": str(storage_path),
                }
            )

        return _google_calendar_insert_event(
            title=title,
            description=description,
            start_dt=None,
            end_dt=None,
            all_day_start_date=all_day_start,
            all_day_end_date_exclusive=all_day_end_exclusive,
            calendar_id_override=calendar_id_override,
        )

    start_dt = _parse_iso8601(start_time_text)
    end_dt = _parse_iso8601(end_time_text)
    if start_dt is None or end_dt is None:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_datetime_format",
                "action": "add_calendar_event",
                "detail": "start_time / end_time はISO8601形式で指定してください。",
            }
        )
    if end_dt <= start_dt:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_time_range",
                "action": "add_calendar_event",
                "detail": "end_time は start_time より後である必要があります。",
            }
        )

    if _calendar_provider() == "local":
        storage_path = _calendar_storage_path()
        event = {
            "id": f"cal-{uuid4().hex[:12]}",
            "title": title,
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _append_local_calendar_event_record(event)
        except Exception as exc:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "add_calendar_event_failed",
                    "action": "add_calendar_event",
                    "detail": _truncate_text(str(exc)),
                }
            )
        return _as_json_line(
            {
                "status": "ok",
                "action": "add_calendar_event",
                "event_id": event["id"],
                "title": title,
                "start_time": event["start_time"],
                "end_time": event["end_time"],
                "storage_path": str(storage_path),
            }
        )

    return _google_calendar_insert_event(
        title=title,
        description=description,
        start_dt=start_dt,
        end_dt=end_dt,
        all_day_start_date=None,
        all_day_end_date_exclusive=None,
        calendar_id_override=calendar_id_override,
    )


def _handle_add_task(payload: dict[str, object]) -> str:
    """Add a task to Google Tasks."""
    title = str(payload.get("title", "")).strip()
    if not title:
        return _as_json_line(
            {
                "status": "error",
                "code": "missing_title",
                "action": "add_task",
                "detail": "タスクのタイトルは必須です。",
            }
        )

    due_date_text = str(payload.get("due_date", "")).strip() or str(payload.get("date", "")).strip()
    due_date = None
    if due_date_text:
        due_date = _parse_date_only(due_date_text)
        if not due_date:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "invalid_date_format",
                    "action": "add_task",
                    "detail": f"期限の日付形式が不正です: {due_date_text}",
                }
            )

    notes = str(payload.get("notes", "")).strip() or str(payload.get("description", "")).strip()
    return _google_tasks_insert_task(title=title, due_date=due_date, notes=notes)


def _handle_update_task(payload: dict[str, object]) -> str:
    search_title = str(
        payload.get("title", "")
        or payload.get("task_title", "")
        or payload.get("old_title", "")
        or payload.get("query", "")
    ).strip()
    task_id = str(payload.get("task_id", "")).strip()
    new_title = str(payload.get("new_title", "")).strip() or None
    due_date_text = str(payload.get("due_date", "")).strip() or str(payload.get("date", "")).strip()
    notes = str(payload.get("notes", "")).strip() or str(payload.get("description", "")).strip() or None
    completed = payload.get("completed")

    due_date = None
    if due_date_text:
        due_date = _parse_date_only(due_date_text)
        if not due_date:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "invalid_date_format",
                    "action": "update_task",
                    "detail": f"期限の日付形式が不正です: {due_date_text}",
                }
            )

    if not task_id:
        if not search_title:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": "update_task",
                    "required": ["title or task_id", "due_date"],
                }
            )
        tasks, err, status_code = _google_tasks_list_all_tasks()
        if err is not None:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "update_task_failed",
                    "action": "update_task",
                    "status_code": status_code,
                    "detail": err,
                }
            )

        matches = [task for task in tasks if str(task.get("title", "")).strip() == search_title]
        if not matches:
            matches = [task for task in tasks if search_title and search_title in str(task.get("title", ""))]
        if not matches:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "task_not_found",
                    "action": "update_task",
                    "title": search_title,
                }
            )
        task_id = str(matches[0].get("id", "")).strip()

    if not due_date and new_title is None and notes is None and completed is None:
        return _as_json_line(
            {
                "status": "error",
                "code": "missing_required_fields",
                "action": "update_task",
                "required": ["due_date"],
            }
        )

    return _google_tasks_update_task(
        task_id=task_id,
        title=new_title,
        due_date=due_date,
        notes=notes,
        completed=completed if isinstance(completed, bool) else None,
    )


def _handle_delete_task(payload: dict[str, object]) -> str:
    task_id = str(payload.get("task_id", "")).strip()
    title_hint = str(
        payload.get("title", "")
        or payload.get("task_title", "")
        or payload.get("query", "")
    ).strip()

    if not task_id:
        if not title_hint:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": "delete_task",
                    "required": ["task_id or title"],
                }
            )
        tasks, err, status_code = _google_tasks_list_all_tasks()
        if err is not None:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "delete_task_failed",
                    "action": "delete_task",
                    "status_code": status_code,
                    "detail": err,
                }
            )

        matches = [task for task in tasks if str(task.get("title", "")).strip() == title_hint]
        if not matches:
            matches = [task for task in tasks if title_hint and title_hint in str(task.get("title", ""))]
        if not matches:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "task_not_found",
                    "action": "delete_task",
                    "title": title_hint,
                }
            )
        task_id = str(matches[0].get("id", "")).strip()

    return _google_tasks_delete_task(task_id)


def _handle_bulk_update_task_due_date(payload: dict[str, object]) -> str:
    from_dates, date_err = _resolve_target_dates(payload, key="from_dates")
    if date_err is not None:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_date_format",
                "action": "bulk_update_task_due_date",
                "detail": date_err,
            }
        )

    to_date_text = str(payload.get("to_date", "")).strip()
    to_date = _parse_date_only(to_date_text)
    if not to_date:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_date_format",
                "action": "bulk_update_task_due_date",
                "detail": "to_date は YYYY-MM-DD 形式で指定してください。",
            }
        )

    tasks, err, status_code = _google_tasks_list_all_tasks()
    if err is not None:
        return _as_json_line(
            {
                "status": "error",
                "code": "bulk_update_task_due_date_failed",
                "action": "bulk_update_task_due_date",
                "status_code": status_code,
                "detail": err,
            }
        )

    targets: list[dict[str, object]] = []
    from_set = set(from_dates)
    for task in tasks:
        due_day = _iso_day_of(task.get("due"))
        if due_day and due_day in from_set:
            targets.append(task)

    updated: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for task in targets:
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            failed.append({"task_id": "", "title": str(task.get("title", "")), "detail": "missing_task_id"})
            continue

        result = json.loads(_google_tasks_update_task(task_id=task_id, due_date=to_date))
        if result.get("status") == "ok":
            updated.append({"task_id": task_id, "title": str(task.get("title", "")), "to_date": to_date})
        else:
            failed.append(
                {
                    "task_id": task_id,
                    "title": str(task.get("title", "")),
                    "detail": str(result.get("detail", result.get("code", "unknown_error"))),
                }
            )

    return _as_json_line(
        {
            "status": "ok" if not failed else "partial",
            "action": "bulk_update_task_due_date",
            "from_dates": from_dates,
            "to_date": to_date,
            "matched_count": len(targets),
            "updated_count": len(updated),
            "failed_count": len(failed),
            "updated": updated,
            "failed": failed,
        }
    )


def _handle_bulk_delete_by_dates(payload: dict[str, object]) -> str:
    dates, date_err = _resolve_target_dates(payload, key="dates")
    if date_err is not None:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_date_format",
                "action": "bulk_delete_by_dates",
                "detail": date_err,
            }
        )

    delete_tasks = _as_bool(payload.get("delete_tasks", True))
    delete_calendar = _as_bool(payload.get("delete_calendar", True))
    date_set = set(dates)

    task_deleted: list[dict[str, object]] = []
    task_failed: list[dict[str, object]] = []
    if delete_tasks:
        tasks, err, status_code = _google_tasks_list_all_tasks()
        if err is not None:
            task_failed.append(
                {
                    "scope": "tasks",
                    "detail": err,
                    "status_code": status_code,
                }
            )
        else:
            for task in tasks:
                due_day = _iso_day_of(task.get("due"))
                if not due_day or due_day not in date_set:
                    continue
                task_id = str(task.get("id", "")).strip()
                if not task_id:
                    task_failed.append({"scope": "tasks", "title": str(task.get("title", "")), "detail": "missing_task_id"})
                    continue
                result = json.loads(_google_tasks_delete_task(task_id))
                if result.get("status") == "ok":
                    task_deleted.append({"task_id": task_id, "title": str(task.get("title", "")), "due_date": due_day})
                else:
                    task_failed.append(
                        {
                            "scope": "tasks",
                            "task_id": task_id,
                            "title": str(task.get("title", "")),
                            "detail": str(result.get("detail", result.get("code", "unknown_error"))),
                        }
                    )

    event_deleted: list[dict[str, object]] = []
    event_failed: list[dict[str, object]] = []
    if delete_calendar:
        min_date = min(dates)
        max_date = max(dates)
        min_dt = _parse_iso8601(f"{min_date}T00:00:00+09:00")
        max_dt = _parse_iso8601(f"{max_date}T23:59:59+09:00")
        if min_dt is None or max_dt is None:
            event_failed.append({"scope": "calendar", "detail": "failed_to_build_time_range"})
        else:
            list_res = json.loads(_google_calendar_list_events(min_dt=min_dt, max_dt=max_dt + timedelta(seconds=1)))
            if list_res.get("status") != "ok":
                event_failed.append(
                    {
                        "scope": "calendar",
                        "detail": str(list_res.get("detail", list_res.get("code", "list_failed"))),
                    }
                )
            else:
                events = list_res.get("events", [])
                for event in events if isinstance(events, list) else []:
                    if not isinstance(event, dict):
                        continue
                    start_day = _iso_day_of(event.get("start_time"))
                    if not start_day or start_day not in date_set:
                        continue
                    event_id = str(event.get("id", "")).strip()
                    if not event_id:
                        event_failed.append({"scope": "calendar", "title": str(event.get("title", "")), "detail": "missing_event_id"})
                        continue
                    result = json.loads(_google_calendar_delete_event(event_id))
                    if result.get("status") == "ok":
                        event_deleted.append({"event_id": event_id, "title": str(event.get("title", "")), "date": start_day})
                    else:
                        event_failed.append(
                            {
                                "scope": "calendar",
                                "event_id": event_id,
                                "title": str(event.get("title", "")),
                                "detail": str(result.get("detail", result.get("code", "unknown_error"))),
                            }
                        )

    failed_count = len(task_failed) + len(event_failed)
    return _as_json_line(
        {
            "status": "ok" if failed_count == 0 else "partial",
            "action": "bulk_delete_by_dates",
            "dates": dates,
            "task_deleted_count": len(task_deleted),
            "event_deleted_count": len(event_deleted),
            "failed_count": failed_count,
            "task_deleted": task_deleted,
            "event_deleted": event_deleted,
            "failed": task_failed + event_failed,
        }
    )


def _handle_get_calendar_events(payload: dict[str, object]) -> str:
    time_min_text = str(payload.get("time_min", "")).strip()
    time_max_text = str(payload.get("time_max", "")).strip()
    calendar_id_override = str(payload.get("calendar_id", "")).strip() or None

    min_dt = _parse_iso8601(time_min_text)
    max_dt = _parse_iso8601(time_max_text)
    if min_dt is None or max_dt is None:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_datetime_format",
                "action": "get_calendar_events",
                "detail": "time_min / time_max はISO8601形式で指定してください。",
            }
        )
    if max_dt <= min_dt:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_time_range",
                "action": "get_calendar_events",
                "detail": "time_max は time_min より後である必要があります。",
            }
        )

    if _calendar_provider() == "local":
        storage_path = _calendar_storage_path()
        events, err = _load_local_calendar_events(min_dt, max_dt)
        if err is not None:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "get_calendar_events_failed",
                    "action": "get_calendar_events",
                    "detail": err,
                }
            )

        events.sort(key=lambda item: str(item.get("start_time", "")))
        limit = max(1, min(_safe_int("CALENDAR_EVENTS_LIST_LIMIT", 50), 200))
        limited_events = events[:limit]

        return _as_json_line(
            {
                "status": "ok",
                "action": "get_calendar_events",
                "events": limited_events,
                "count": len(limited_events),
                "total_matched": len(events),
                "storage_path": str(storage_path),
            }
        )

    return _google_calendar_list_events(
        min_dt=min_dt,
        max_dt=max_dt,
        calendar_id_override=calendar_id_override,
    )


def _normalize_sheet_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        return ""
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in name)
    return cleaned[:120]


def _handle_append_sheet_row(payload: dict[str, object]) -> str:
    sheet_name = _normalize_sheet_name(str(payload.get("sheet_name", "")))
    raw_columns = payload.get("column_data")
    if not sheet_name:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_sheet_name",
                "action": "append_sheet_row",
                "detail": "sheet_name は英数字・_・- を含む名前で指定してください。",
            }
        )
    if isinstance(raw_columns, dict):
        if not raw_columns:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "invalid_column_data",
                    "action": "append_sheet_row",
                    "detail": "column_data は空でないJSONオブジェクトで指定してください。",
                }
            )
        column_data = {str(k): str(v) for k, v in raw_columns.items()}
    elif isinstance(raw_columns, list):
        if not raw_columns:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "invalid_column_data",
                    "action": "append_sheet_row",
                    "detail": "column_data は空でないJSON配列で指定してください。",
                }
            )
        column_data = {f"col{i + 1}": str(v) for i, v in enumerate(raw_columns)}
    else:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_column_data",
                "action": "append_sheet_row",
                "detail": "column_data はJSONオブジェクトまたはJSON配列で指定してください。",
            }
        )

    header = sorted(column_data.keys())

    storage_dir = Path(os.getenv("SHEET_STORAGE_DIR", "./data/runtime/sheets")).expanduser().resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    csv_path = storage_dir / f"{sheet_name}.csv"

    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    existing_header: list[str] = []
    if file_exists:
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as rf:
                reader = csv.reader(rf)
                existing_header = next(reader, [])
        except Exception:
            existing_header = []

    final_header = existing_header or header
    for key in header:
        if key not in final_header:
            final_header.append(key)

    row = {key: column_data.get(key, "") for key in final_header}

    try:
        rewrite_required = bool(existing_header) and (final_header != existing_header)
        if rewrite_required:
            with csv_path.open("r", encoding="utf-8", newline="") as rf:
                reader = list(csv.DictReader(rf))
            with csv_path.open("w", encoding="utf-8", newline="") as wf:
                writer = csv.DictWriter(wf, fieldnames=final_header)
                writer.writeheader()
                for old_row in reader:
                    writer.writerow({key: old_row.get(key, "") for key in final_header})
                writer.writerow(row)
        else:
            with csv_path.open("a", encoding="utf-8", newline="") as af:
                writer = csv.DictWriter(af, fieldnames=final_header)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "append_sheet_row_failed",
                "action": "append_sheet_row",
                "detail": _truncate_text(str(exc)),
            }
        )

    return _as_json_line(
        {
            "status": "ok",
            "action": "append_sheet_row",
            "sheet_name": sheet_name,
            "storage_path": str(csv_path),
            "columns": final_header,
        }
    )


def _handle_add_notion_memo(payload: dict[str, object]) -> str:
    title = str(payload.get("title", "")).strip()
    content = str(payload.get("content", "")).strip()
    category = str(payload.get("category", "")).strip()

    if not title or not content or not category:
        return _as_json_line(
            {
                "status": "error",
                "code": "missing_required_fields",
                "action": "add_notion_memo",
                "required": ["title", "content", "category"],
            }
        )

    storage_path = Path(
        os.getenv("NOTION_MEMO_STORAGE_PATH", "./data/runtime/notion_memos.jsonl")
    ).expanduser().resolve()
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    memo = {
        "id": f"memo-{uuid4().hex[:12]}",
        "title": title,
        "content": content,
        "category": category,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with storage_path.open("a", encoding="utf-8") as wf:
            wf.write(json.dumps(memo, ensure_ascii=False) + "\n")
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "add_notion_memo_failed",
                "action": "add_notion_memo",
                "detail": _truncate_text(str(exc)),
            }
        )

    return _as_json_line(
        {
            "status": "ok",
            "action": "add_notion_memo",
            "memo_id": memo["id"],
            "storage_path": str(storage_path),
            "category": category,
        }
    )


def _handle_create_github_issue(payload: dict[str, object]) -> str:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        auth_url = os.getenv("GITHUB_AUTH_URL", "https://github.com/settings/tokens")
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "create_github_issue",
                "detail": "GITHUB_TOKEN が未設定です。",
                "auth_url": auth_url,
            }
        )

    repository = str(payload.get("repository", "")).strip()
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", ""))

    req_body = json.dumps({"title": title, "body": body}, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"https://api.github.com/repos/{repository}/issues",
        data=req_body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "discord-ai-agent/1.0",
        },
    )

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            status = int(getattr(res, "status", 200))
            raw = res.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
        if status != 201:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "github_issue_create_failed",
                    "action": "create_github_issue",
                    "status_code": status,
                    "detail": _truncate_text(str(parsed)),
                }
            )
        return _as_json_line(
            {
                "status": "ok",
                "action": "create_github_issue",
                "issue_number": parsed.get("number"),
                "issue_url": parsed.get("html_url"),
                "repository": repository,
            }
        )
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        return _as_json_line(
            {
                "status": "error",
                "code": "github_issue_create_failed",
                "action": "create_github_issue",
                "status_code": int(getattr(exc, "code", 500)),
                "detail": _truncate_text(raw),
            }
        )
    except URLError as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "upstream_unreachable",
                "action": "create_github_issue",
                "detail": str(exc),
            }
        )


def _handle_send_email(payload: dict[str, object]) -> str:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = _safe_int("SMTP_PORT", 587)
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", smtp_user).strip()

    if not smtp_host or not smtp_user or not smtp_pass or not smtp_from:
        auth_url = os.getenv("SMTP_AUTH_URL", "")
        return _as_json_line(
            {
                "status": "error",
                "code": "auth_required",
                "action": "send_email",
                "detail": "SMTP設定が未完了です。",
                "auth_url": auth_url,
            }
        )

    to_address = str(payload.get("to_address", "")).strip()
    subject = str(payload.get("subject", "")).strip()
    body = str(payload.get("body", ""))

    msg = EmailMessage()
    msg["From"] = smtp_from
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)

    timeout_sec = _safe_int("INTERNAL_ACTION_TIMEOUT_SEC", 15)
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout_sec) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return _as_json_line(
            {
                "status": "ok",
                "action": "send_email",
                "to_address": to_address,
                "subject": subject,
            }
        )
    except Exception as exc:
        return _as_json_line(
            {
                "status": "error",
                "code": "send_email_failed",
                "action": "send_email",
                "detail": _truncate_text(str(exc)),
            }
        )


def execute_internal_action(action: str, payload_json: str = "{}") -> str:
    clean_action = _normalize_action_name(action)
    if not clean_action:
        return _as_json_line({"status": "error", "code": "invalid_action", "detail": "action が空です。"})

    try:
        payload = json.loads(payload_json or "{}")
    except Exception:
        return _as_json_line({"status": "error", "code": "invalid_payload_json", "detail": "payload_json はJSON形式で指定してください。"})

    if not isinstance(payload, dict):
        return _as_json_line({"status": "error", "code": "invalid_payload_type", "detail": "payload_json はJSONオブジェクトで指定してください。"})

    if clean_action == "add_calendar_event":
        payload = _normalize_add_calendar_payload(payload)

    allowed = _allowed_actions()
    allowed.add("add_to_jam")
    if clean_action not in allowed:
        return _as_json_line(
            {
                "status": "error",
                "code": "unsupported_action",
                "action": clean_action,
                "allowed_actions": sorted(allowed),
            }
        )

    if clean_action == "add_calendar_event":
        if not str(payload.get("title", "")).strip():
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": clean_action,
                    "missing": ["title"],
                    "required": ["title", "start_time+end_time OR all_day+date"],
                }
            )
        has_timed = bool(str(payload.get("start_time", "")).strip() and str(payload.get("end_time", "")).strip())
        all_day_enabled = _as_bool(payload.get("all_day", False))
        has_all_day = all_day_enabled and bool(str(payload.get("date") or payload.get("start_date") or "").strip())
        if not has_timed and not has_all_day:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": clean_action,
                    "detail": "add_calendar_event は timed(start_time,end_time) か all_day(date) のどちらかが必要です。",
                }
            )
    elif clean_action == "update_task":
        # update_task は task_id 指定、または title 指定で対象解決できればよい。
        # 汎用 required_fields では OR 条件を表現できないため個別検証する。
        has_task_id = bool(str(payload.get("task_id", "")).strip())
        has_title = bool(str(payload.get("title", "")).strip())
        if not has_task_id and not has_title:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": clean_action,
                    "required": ["task_id or title"],
                }
            )
    elif clean_action == "delete_task":
        # delete_task は task_id か title のどちらかがあれば実行できる。
        has_task_id = bool(str(payload.get("task_id", "")).strip())
        has_title = bool(str(payload.get("title", "") or payload.get("task_title", "") or payload.get("query", "")).strip())
        if not has_task_id and not has_title:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": clean_action,
                    "required": ["task_id or title"],
                }
            )
    elif clean_action == "bulk_update_task_due_date":
        from_dates, from_err = _resolve_target_dates(payload, key="from_dates")
        if from_err is not None or not from_dates:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": clean_action,
                    "required": ["from_dates", "to_date"],
                }
            )
        to_date = _parse_date_only(payload.get("to_date"))
        if not to_date:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "invalid_date_format",
                    "action": clean_action,
                    "detail": "to_date は YYYY-MM-DD 形式で指定してください。",
                }
            )
    elif clean_action == "bulk_delete_by_dates":
        dates, dates_err = _resolve_target_dates(payload, key="dates")
        if dates_err is not None or not dates:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": clean_action,
                    "required": ["dates"],
                }
            )
    else:
        required_fields = _required_fields_map().get(clean_action, [])
        missing = [name for name in required_fields if name not in payload]
        if missing:
            return _as_json_line(
                {
                    "status": "error",
                    "code": "missing_required_fields",
                    "action": clean_action,
                    "missing": missing,
                    "required": required_fields,
                }
            )

    if clean_action == "create_github_issue":
        return _handle_create_github_issue(payload)

    if clean_action == "add_calendar_event":
        return _handle_add_calendar_event(payload)

    if clean_action == "get_calendar_events":
        return _handle_get_calendar_events(payload)

    if clean_action == "add_task":
        return _handle_add_task(payload)

    if clean_action == "update_task":
        return _handle_update_task(payload)

    if clean_action == "delete_task":
        return _handle_delete_task(payload)

    if clean_action == "bulk_update_task_due_date":
        return _handle_bulk_update_task_due_date(payload)

    if clean_action == "bulk_delete_by_dates":
        return _handle_bulk_delete_by_dates(payload)

    if clean_action == "send_email":
        return _handle_send_email(payload)

    if clean_action == "append_sheet_row":
        return _handle_append_sheet_row(payload)

    if clean_action == "add_notion_memo":
        return _handle_add_notion_memo(payload)

    if clean_action == "add_to_jam":
        from tools.music_tools import add_to_jam
        query = str(payload.get("query", "")).strip() or str(payload.get("title", "")).strip()
        status_err = add_to_jam(query)
        if status_err:
            return _as_json_line({"status": "error", "code": "add_to_jam_failed", "action": "add_to_jam", "detail": status_err})
        return _as_json_line({"status": "ok", "action": "add_to_jam", "detail": "success"})

    if clean_action == "backup_server_data":
        return _handle_backup_server_data(payload)

    return _as_json_line(
        {
            "status": "error",
            "code": "invalid_action",
            "code": "not_implemented_action",
            "action": clean_action,
            "detail": "このactionはまだコード内実装されていません。",
        }
    )
