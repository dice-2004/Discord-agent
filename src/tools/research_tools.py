from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _research_base_url() -> str:
    return (os.getenv("RESEARCH_AGENT_URL", "http://research-agent:8091").strip() or "http://research-agent:8091").rstrip("/")


def _research_token() -> str:
    return os.getenv("RESEARCH_AGENT_SHARED_TOKEN", "change_me").strip() or "change_me"


def _request_json(path: str, method: str = "GET", body: dict[str, object] | None = None) -> tuple[dict[str, object], str | None, int | None]:
    headers = {
        "Accept": "application/json",
        "X-Research-Token": _research_token(),
        "User-Agent": "discord-ai-agent/1.0",
    }
    payload_bytes = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = Request(
        f"{_research_base_url()}{path}",
        data=payload_bytes,
        method=method,
        headers=headers,
    )

    http_timeout = max(5, _safe_int("RESEARCH_AGENT_HTTP_TIMEOUT_SEC", 10))
    try:
        with urlopen(req, timeout=http_timeout) as res:
            status_code = int(getattr(res, "status", 200))
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {"status": "error", "code": "invalid_payload_type", "detail": str(payload)[:1200]}
        return payload, None, status_code
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return {}, detail[:1200], int(getattr(exc, "code", 500))
    except URLError as exc:
        return {}, str(exc), None
    except Exception as exc:
        return {}, str(exc)[:1200], None


def get_research_job_status(job_id: str) -> str:
    clean_job_id = (job_id or "").strip()
    if not clean_job_id:
        return json.dumps(
            {
                "status": "error",
                "code": "invalid_job_id",
                "action": "get_research_job_status",
                "detail": "job_id が空です。",
            },
            ensure_ascii=False,
        )

    payload, err, status_code = _request_json(path=f"/v1/jobs/{quote(clean_job_id)}", method="GET")
    if err is not None:
        return json.dumps(
            {
                "status": "error",
                "code": "research_agent_http_error",
                "action": "get_research_job_status",
                "job_id": clean_job_id,
                "status_code": status_code,
                "detail": err,
            },
            ensure_ascii=False,
        )

    payload["action"] = "get_research_job_status"
    return json.dumps(payload, ensure_ascii=False)


def dispatch_research_job(
    topic: str,
    source: str = "auto",
    wait: str = "true",
    mode: str = "auto",
    timeout_sec: str = "",
) -> str:
    clean_topic = (topic or "").strip()
    if not clean_topic:
        return json.dumps(
            {
                "status": "error",
                "code": "invalid_topic",
                "action": "dispatch_research_job",
                "detail": "topic が空です。",
            },
            ensure_ascii=False,
        )

    clean_source = (source or "auto").strip().lower() or "auto"
    clean_mode = (mode or "auto").strip().lower() or "auto"
    if clean_mode not in {"auto", "gemini_cli", "fallback"}:
        return json.dumps(
            {
                "status": "error",
                "code": "invalid_mode",
                "action": "dispatch_research_job",
                "detail": "mode は auto / gemini_cli / fallback のいずれかを指定してください。",
            },
            ensure_ascii=False,
        )
    wait_enabled = str(wait or "true").strip().lower() not in {"false", "0", "no", "off"}

    poll_interval = max(1.0, _safe_int("RESEARCH_AGENT_POLL_INTERVAL_SEC", 3))

    # Research time (actual research budget, not polling timeout)
    # Default is intentionally short for normal (non time-specified) requests.
    research_time_sec = max(10, _safe_int("RESEARCH_AGENT_DEFAULT_TIMEOUT_SEC", 45))
    time_specified = bool(str(timeout_sec or "").strip())
    if time_specified:
        try:
            research_time_sec = int(str(timeout_sec).strip())
        except (ValueError, TypeError):
            pass
    research_time_sec = max(10, min(research_time_sec, 1800))  # Clamp: 10s - 30min

    # Polling timeout = research time + buffer for async cleanup
    wait_timeout = max(30, research_time_sec + 30)

    created, err, status_code = _request_json(
        path="/v1/jobs",
        method="POST",
        body={
            "topic": clean_topic,
            "source": clean_source,
            "mode": clean_mode,
            "timeout_sec": str(research_time_sec),  # Send actual research time to Agent
            "time_specified": time_specified,
        },
    )
    if err is not None:
        return json.dumps(
            {
                "status": "error",
                "code": "research_agent_http_error",
                "action": "dispatch_research_job",
                "status_code": status_code,
                "detail": err,
            },
            ensure_ascii=False,
        )

    job_id = str(created.get("job_id", "")).strip()
    if not job_id:
        return json.dumps(
            {
                "status": "error",
                "code": "research_agent_invalid_response",
                "action": "dispatch_research_job",
                "detail": str(created)[:1200],
            },
            ensure_ascii=False,
        )

    if not wait_enabled:
        return json.dumps(
            {
                "status": "queued",
                "action": "dispatch_research_job",
                "job_id": job_id,
                "mode": clean_mode,
                "timeout_sec": research_time_sec,
                "poll_timeout_sec": wait_timeout,
                "detail": "Research Agent にジョブを投入しました。",
            },
            ensure_ascii=False,
        )

    started = time.time()
    while (time.time() - started) <= wait_timeout:
        snapshot, poll_err, poll_status = _request_json(path=f"/v1/jobs/{quote(job_id)}", method="GET")
        if poll_err is not None:
            return json.dumps(
                {
                    "status": "error",
                    "code": "research_agent_poll_failed",
                    "action": "dispatch_research_job",
                    "job_id": job_id,
                    "status_code": poll_status,
                    "detail": poll_err,
                },
                ensure_ascii=False,
            )

        status = str(snapshot.get("status", "")).strip().lower()
        if status in {"done", "failed"}:
            snapshot["action"] = "dispatch_research_job"
            return json.dumps(snapshot, ensure_ascii=False)

        time.sleep(poll_interval)

    return json.dumps(
        {
            "status": "queued",
            "action": "dispatch_research_job",
            "job_id": job_id,
            "detail": "タイムアウトまでに完了しなかったため、バックグラウンド継続中です。",
        },
        ensure_ascii=False,
    )
