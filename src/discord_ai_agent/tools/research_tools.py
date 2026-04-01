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
    wait_enabled = str(wait or "true").strip().lower() not in {"false", "0", "no", "off"}

    poll_interval = max(1, _safe_int("RESEARCH_AGENT_POLL_INTERVAL_SEC", 2))
    wait_timeout = max(10, _safe_int("RESEARCH_AGENT_WAIT_TIMEOUT_SEC", 90))
    if str(timeout_sec or "").strip():
        try:
            wait_timeout = max(10, int(str(timeout_sec).strip()))
        except ValueError:
            pass

    req_body = json.dumps(
        {
            "topic": clean_topic,
            "source": clean_source,
            "mode": clean_mode,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Research-Token": _research_token(),
        "User-Agent": "discord-ai-agent/1.0",
    }

    http_timeout = max(5, _safe_int("RESEARCH_AGENT_HTTP_TIMEOUT_SEC", 10))
    create_req = Request(
        f"{_research_base_url()}/v1/jobs",
        data=req_body,
        method="POST",
        headers=headers,
    )

    try:
        with urlopen(create_req, timeout=http_timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
        created = json.loads(raw) if raw else {}
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return json.dumps(
            {
                "status": "error",
                "code": "research_agent_http_error",
                "action": "dispatch_research_job",
                "status_code": int(getattr(exc, "code", 500)),
                "detail": detail[:1200],
            },
            ensure_ascii=False,
        )
    except URLError as exc:
        return json.dumps(
            {
                "status": "error",
                "code": "research_agent_unreachable",
                "action": "dispatch_research_job",
                "detail": str(exc),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "code": "research_agent_request_failed",
                "action": "dispatch_research_job",
                "detail": str(exc)[:1200],
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
                "detail": "Research Agent にジョブを投入しました。",
            },
            ensure_ascii=False,
        )

    started = time.time()
    while (time.time() - started) <= wait_timeout:
        get_req = Request(
            f"{_research_base_url()}/v1/jobs/{quote(job_id)}",
            method="GET",
            headers={
                "Accept": "application/json",
                "X-Research-Token": _research_token(),
                "User-Agent": "discord-ai-agent/1.0",
            },
        )
        try:
            with urlopen(get_req, timeout=http_timeout) as res:
                raw = res.read().decode("utf-8", errors="replace")
            snapshot = json.loads(raw) if raw else {}
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "code": "research_agent_poll_failed",
                    "action": "dispatch_research_job",
                    "job_id": job_id,
                    "detail": str(exc)[:1200],
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
