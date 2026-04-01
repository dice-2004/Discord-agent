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
from urllib.parse import urlencode
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


def _google_calendar_creds() -> dict[str, str]:
    return {
        "client_id": os.getenv("GOOGLE_CALENDAR_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET", "").strip(),
        "refresh_token": os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "").strip(),
        "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary",
    }


def _google_access_token() -> tuple[str | None, str | None]:
    creds = _google_calendar_creds()
    required = ["client_id", "client_secret", "refresh_token"]
    missing = [key for key in required if not creds.get(key)]
    if missing:
        return None, f"missing_credentials:{','.join(missing)}"

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
        return None, _truncate_text(raw)
    except Exception as exc:
        return None, _truncate_text(str(exc))


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


def _google_calendar_list_events(min_dt: datetime, max_dt: datetime, calendar_id_override: str | None = None) -> str:
    token, err = _google_access_token()
    if not token:
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
        return _as_json_line(
            {
                "status": "error",
                "code": "get_calendar_events_failed",
                "action": "get_calendar_events",
                "detail": _truncate_text(str(exc)),
            }
        )


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
            storage_path.parent.mkdir(parents=True, exist_ok=True)
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
                with storage_path.open("a", encoding="utf-8") as wf:
                    wf.write(json.dumps(event, ensure_ascii=False) + "\n")
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
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "id": f"cal-{uuid4().hex[:12]}",
            "title": title,
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with storage_path.open("a", encoding="utf-8") as wf:
                wf.write(json.dumps(event, ensure_ascii=False) + "\n")
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
        if not storage_path.exists():
            return _as_json_line(
                {
                    "status": "ok",
                    "action": "get_calendar_events",
                    "events": [],
                    "count": 0,
                    "storage_path": str(storage_path),
                }
            )

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
            return _as_json_line(
                {
                    "status": "error",
                    "code": "get_calendar_events_failed",
                    "action": "get_calendar_events",
                    "detail": _truncate_text(str(exc)),
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
    if not isinstance(raw_columns, dict) or not raw_columns:
        return _as_json_line(
            {
                "status": "error",
                "code": "invalid_column_data",
                "action": "append_sheet_row",
                "detail": "column_data は空でないJSONオブジェクトで指定してください。",
            }
        )

    column_data = {str(k): str(v) for k, v in raw_columns.items()}
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

    if clean_action == "send_email":
        return _handle_send_email(payload)

    if clean_action == "append_sheet_row":
        return _handle_append_sheet_row(payload)

    if clean_action == "add_notion_memo":
        return _handle_add_notion_memo(payload)

    if clean_action == "backup_server_data":
        return _handle_backup_server_data(payload)

    return _as_json_line(
        {
            "status": "error",
            "code": "not_implemented_action",
            "action": clean_action,
            "detail": "このactionはまだコード内実装されていません。",
        }
    )
