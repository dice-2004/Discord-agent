from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from discord_ai_agent.tools.deep_dive_tools import source_deep_dive

logger = logging.getLogger(__name__)


class ResearchJobStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path, timeout=5.0) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_jobs (
                    job_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    source TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create_job(self, job_id: str, topic: str, source: str, mode: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO research_jobs(job_id, topic, source, mode, status, report, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'queued', '', '', ?, ?)
                    """,
                    (job_id, topic, source, mode, now, now),
                )

    def update_job(self, job_id: str, *, status: str, report: str = "", error: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                conn.execute(
                    """
                    UPDATE research_jobs
                    SET status = ?, report = ?, error = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, report, error, now, job_id),
                )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                row = conn.execute(
                    """
                    SELECT job_id, topic, source, mode, status, report, error, created_at, updated_at
                    FROM research_jobs
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
        if row is None:
            return None
        return {
            "job_id": str(row[0]),
            "topic": str(row[1]),
            "source": str(row[2]),
            "mode": str(row[3]),
            "status": str(row[4]),
            "report": str(row[5]),
            "error": str(row[6]),
            "created_at": str(row[7]),
            "updated_at": str(row[8]),
        }


def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _build_job_id() -> str:
    return f"rj-{int(time.time() * 1000)}-{os.getpid()}-{threading.get_ident()}"


def _run_gemini_cli(topic: str, source: str) -> tuple[str, str | None]:
    cmd = os.getenv("RESEARCH_AGENT_GEMINI_COMMAND", "gemini").strip() or "gemini"
    timeout = max(30, _safe_int("RESEARCH_AGENT_GEMINI_TIMEOUT_SEC", 240))
    prompt = (
        "あなたは調査エージェントです。以下のトピックについて日本語で要点をまとめてください。\n"
        f"topic: {topic}\n"
        f"source_hint: {source}\n"
        "出力: 結論→根拠→次の確認ポイント の順で簡潔に。"
    )

    try:
        # NOTE: CLIの引数仕様差異に備え、最初は標準入力を使う呼び出しを採用。
        completed = subprocess.run(
            [cmd],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return "", "gemini_cli_not_found"
    except Exception as exc:
        return "", f"gemini_cli_exec_failed:{exc}"

    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "gemini_cli_non_zero_exit").strip()
        return "", err[:1200]

    out = (completed.stdout or "").strip()
    if not out:
        return "", "gemini_cli_empty_output"
    return out, None


def _run_research(topic: str, source: str, mode: str) -> tuple[str, str | None]:
    clean_mode = (mode or "auto").strip().lower() or "auto"
    gemini_enabled = os.getenv("RESEARCH_AGENT_USE_GEMINI_CLI", "false").strip().lower() == "true"

    if clean_mode in {"gemini_cli", "auto"} and gemini_enabled:
        report, err = _run_gemini_cli(topic, source)
        if not err and report:
            return report, None
        logger.warning("Gemini CLI failed; fallback to deep dive. err=%s", err)
        if clean_mode == "gemini_cli":
            return "", err

    try:
        return source_deep_dive(topic=topic, source=source), None
    except Exception as exc:
        return "", str(exc)


class ResearchHandler(BaseHTTPRequestHandler):
    store: ResearchJobStore | None = None
    shared_token: str = "change_me"

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.headers.get("X-Research-Token", "").strip()
        return token == self.shared_token

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"status": "ok", "service": "research-agent"})
            return

        if not self._authorized():
            self._send_json(403, {"status": "error", "code": "forbidden"})
            return

        if parsed.path.startswith("/v1/jobs/"):
            if self.store is None:
                self._send_json(500, {"status": "error", "code": "store_unavailable"})
                return
            job_id = parsed.path.split("/v1/jobs/", 1)[1].strip()
            if not job_id:
                self._send_json(400, {"status": "error", "code": "invalid_job_id"})
                return
            snapshot = self.store.get_job(job_id)
            if snapshot is None:
                self._send_json(404, {"status": "error", "code": "job_not_found", "job_id": job_id})
                return
            self._send_json(200, snapshot)
            return

        self._send_json(404, {"status": "error", "code": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/v1/jobs":
            self._send_json(404, {"status": "error", "code": "not_found"})
            return

        if not self._authorized():
            self._send_json(403, {"status": "error", "code": "forbidden"})
            return

        content_len = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_len).decode("utf-8", errors="replace") if content_len > 0 else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            self._send_json(400, {"status": "error", "code": "invalid_json"})
            return

        topic = str(payload.get("topic", "")).strip()
        source = str(payload.get("source", "auto")).strip().lower() or "auto"
        mode = str(payload.get("mode", "auto")).strip().lower() or "auto"
        if not topic:
            self._send_json(400, {"status": "error", "code": "invalid_topic"})
            return

        if self.store is None:
            self._send_json(500, {"status": "error", "code": "store_unavailable"})
            return

        job_id = _build_job_id()
        self.store.create_job(job_id=job_id, topic=topic, source=source, mode=mode)

        def _worker() -> None:
            assert self.store is not None
            self.store.update_job(job_id, status="running")
            report, err = _run_research(topic=topic, source=source, mode=mode)
            if err:
                self.store.update_job(job_id, status="failed", error=err[:1200])
                return
            self.store.update_job(job_id, status="done", report=(report or "")[:12000])

        threading.Thread(target=_worker, name=f"research-job-{job_id}", daemon=True).start()

        self._send_json(
            202,
            {
                "status": "queued",
                "job_id": job_id,
                "topic": topic,
                "source": source,
                "mode": mode,
            },
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.info("ResearchAgent %s - %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, (os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    host = os.getenv("RESEARCH_AGENT_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _safe_int("RESEARCH_AGENT_PORT", 8091)
    token = os.getenv("RESEARCH_AGENT_SHARED_TOKEN", "change_me").strip() or "change_me"
    db_path = os.getenv("RESEARCH_AGENT_DB_PATH", "./data/runtime/research_jobs.sqlite3").strip()

    ResearchHandler.store = ResearchJobStore(db_path=db_path)
    ResearchHandler.shared_token = token

    server = ThreadingHTTPServer((host, port), ResearchHandler)
    logger.info("Research Agent started at %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
