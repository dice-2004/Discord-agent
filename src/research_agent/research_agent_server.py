from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.deep_dive_tools import source_deep_dive
from research_agent.core.orchestrator import build_research_orchestrator

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
                    engine TEXT NOT NULL,
                    report TEXT NOT NULL,
                    decision_log TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(research_jobs)").fetchall()
                if isinstance(row, tuple) and len(row) > 1
            }
            if "engine" not in cols:
                conn.execute("ALTER TABLE research_jobs ADD COLUMN engine TEXT NOT NULL DEFAULT ''")
            if "decision_log" not in cols:
                conn.execute("ALTER TABLE research_jobs ADD COLUMN decision_log TEXT NOT NULL DEFAULT '[]'")

    def create_job(self, job_id: str, topic: str, source: str, mode: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO research_jobs(job_id, topic, source, mode, status, engine, report, decision_log, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'queued', '', '', '[]', '', ?, ?)
                    """,
                    (job_id, topic, source, mode, now, now),
                )

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        report: str = "",
        error: str = "",
        engine: str | None = None,
        decision_log: list[dict[str, Any]] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                log_json = json.dumps(decision_log or [], ensure_ascii=False)
                if engine is None:
                    conn.execute(
                        """
                        UPDATE research_jobs
                        SET status = ?, report = ?, error = ?, decision_log = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (status, report, error, log_json, now, job_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE research_jobs
                        SET status = ?, engine = ?, report = ?, error = ?, decision_log = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (status, engine, report, error, log_json, now, job_id),
                    )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                row = conn.execute(
                    """
                    SELECT job_id, topic, source, mode, status, engine, report, decision_log, error, created_at, updated_at
                    FROM research_jobs
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
        if row is None:
            return None
        try:
            decision_log = json.loads(str(row[7]) if row[7] else "[]")
        except Exception:
            decision_log = []
        return {
            "job_id": str(row[0]),
            "topic": str(row[1]),
            "source": str(row[2]),
            "mode": str(row[3]),
            "status": str(row[4]),
            "engine": str(row[5]),
            "report": str(row[6]),
            "decision_log": decision_log,
            "error": str(row[8]),
            "created_at": str(row[9]),
            "updated_at": str(row[10]),
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
    model = os.getenv("RESEARCH_AGENT_GEMINI_MODEL", "gemini-2.5-flash").strip()
    timeout = max(30, _safe_int("RESEARCH_AGENT_GEMINI_TIMEOUT_SEC", 240))
    prompt = (
        "あなたは調査エージェントです。以下のトピックについて日本語で要点をまとめてください。\n"
        f"topic: {topic}\n"
        f"source_hint: {source}\n"
        "出力: 結論→根拠→次の確認ポイント の順で簡潔に。"
    )

    argv = shlex.split(cmd)
    if not argv:
        return "", "gemini_cli_command_empty"
    if model:
        argv.extend(["--model", model])
    argv.extend(["--prompt", prompt])
    logger.info(
        "[research-agent][gemini-cli] exec command=%s model=%s topic=%s source=%s",
        argv[0],
        model or "(default)",
        topic,
        source,
    )

    try:
        completed = subprocess.run(
            argv,
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


def _run_research(topic: str, source: str, mode: str) -> tuple[str, str | None, str, list[dict[str, Any]]]:
    clean_mode = (mode or "auto").strip().lower() or "auto"
    gemini_enabled = os.getenv("RESEARCH_AGENT_USE_GEMINI_CLI", "false").strip().lower() == "true"
    orchestrator_enabled = bool(os.getenv("RESEARCH_GEMINI_API_KEY", "").strip())
    initial_report = ""
    logger.info(
        "[research-agent][run] start topic=%s source=%s mode=%s gemini_cli=%s orchestrator=%s",
        topic,
        source,
        clean_mode,
        gemini_enabled,
        orchestrator_enabled,
    )

    if clean_mode == "gemini_cli":
        if not gemini_enabled:
            return "", "gemini_cli_disabled", "gemini_cli", []

        report, err = _run_gemini_cli(topic, source)
        if err:
            logger.warning("[research-agent][run] gemini_cli mode failed err=%s", err)
            return "", err, "gemini_cli", []

        if orchestrator_enabled and _check_need_orchestrator(report):
            try:
                orch_report, orch_decision_log = asyncio.run(_run_orchestrator_deepdive(topic, source, report))
                if orch_report:
                    logger.info("[research-agent][run] route=gemini_cli+orchestrator")
                    combined = f"[Gemini CLI Initial]\n{report}\n\n[Management AI Deepdive]\n{orch_report}"
                    return combined[:12000], None, "gemini_cli+orchestrator", orch_decision_log
            except Exception as exc:
                logger.exception("Orchestrator failed in gemini_cli mode: %s", exc)
        return report, None, "gemini_cli", []

    if clean_mode in {"gemini_cli", "auto"} and gemini_enabled:
        report, err = _run_gemini_cli(topic, source)
        if not err and report:
            initial_report = report
            try:
                if orchestrator_enabled and _check_need_orchestrator(report):
                    orch_report, orch_decision_log = asyncio.run(_run_orchestrator_deepdive(topic, source, report))
                    if orch_report:
                        logger.info("[research-agent][run] route=gemini_cli+orchestrator")
                        combined = f"[Gemini CLI Initial]\n{report}\n\n[Management AI Deepdive]\n{orch_report}"
                        return combined[:12000], None, "gemini_cli+orchestrator", orch_decision_log
                logger.info("[research-agent][run] route=gemini_cli")
                return report, None, "gemini_cli", []
            except Exception as exc:
                logger.exception("Orchestrator failed: %s", exc)
                return report, None, "gemini_cli", []

        logger.warning("Gemini CLI failed; fallback to deep dive. err=%s", err)
        if clean_mode == "gemini_cli":
            return "", err, "gemini_cli", []

    if clean_mode in {"auto", "fallback"} and orchestrator_enabled:
        try:
            orch_report, orch_decision_log = asyncio.run(
                _run_orchestrator_deepdive(topic, source, initial_report)
            )
            if orch_report:
                if initial_report:
                    logger.info("[research-agent][run] route=gemini_cli+orchestrator")
                    combined = f"[Gemini CLI Initial]\n{initial_report}\n\n[Management AI Deepdive]\n{orch_report}"
                    return combined[:12000], None, "gemini_cli+orchestrator", orch_decision_log
                logger.info("[research-agent][run] route=orchestrator")
                return orch_report[:12000], None, "orchestrator", orch_decision_log
            logger.warning("Orchestrator returned empty report; fallback to deep dive")
        except Exception as exc:
            logger.exception("Orchestrator fallback failed: %s", exc)

    try:
        logger.info("[research-agent][run] route=deep_dive")
        return source_deep_dive(topic=topic, source=source), None, "deep_dive", []
    except Exception as exc:
        return "", str(exc), "deep_dive", []


def _check_need_orchestrator(report: str) -> bool:
    """Check if orchestrator (management AI) is needed for deeper research."""
    if not report:
        return False
    lower = report.lower()
    if any(
        marker in lower
        for marker in [
            "需要",
            "必要",
            "深掘り",
            "詳細",
            "more_search",
            "orchestrator",
        ]
    ):
        return True
    return False


async def _run_orchestrator_deepdive(
    topic: str, source: str, initial_report: str
) -> tuple[str, list[dict[str, Any]]]:
    """Run management AI (orchestrator) for deeper research."""
    try:
        logger.info("[research-agent][orchestrator] start topic=%s source=%s", topic, source)
        orchestrator = await build_research_orchestrator()
        question = f"{topic}\n\n[Initial Gemini CLI report]\n{initial_report}"
        answer, decision_log = await orchestrator.answer(topic=question, source=source)
        logger.info("[research-agent][orchestrator] done decision_log_len=%s", len(decision_log))
        return answer, decision_log
    except Exception as exc:
        logger.exception("Orchestrator deepdive failed: %s", exc)
        return "", []


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
            job_timeout = max(30, _safe_int("RESEARCH_AGENT_JOB_TIMEOUT_SEC", 600))
            start_time = time.time()

            logger.info("[research-agent][job] start job_id=%s topic=%s source=%s mode=%s", job_id, topic, source, mode)
            self.store.update_job(job_id, status="running", engine="")
            try:
                report, err, engine, decision_log = _run_research(topic=topic, source=source, mode=mode)
                elapsed = time.time() - start_time
                if elapsed > job_timeout:
                    self.store.update_job(
                        job_id,
                        status="failed",
                        error=f"Job timeout exceeded: {elapsed:.1f}s > {job_timeout}s",
                        engine="timeout",
                        decision_log=decision_log,
                    )
                    return
                if err:
                    logger.warning(
                        "[research-agent][job] failed job_id=%s engine=%s elapsed_sec=%.1f err=%s",
                        job_id,
                        engine,
                        elapsed,
                        err[:300],
                    )
                    self.store.update_job(
                        job_id,
                        status="failed",
                        error=err[:1200],
                        engine=engine,
                        decision_log=decision_log,
                    )
                    return
                decorated_report = f"[Research Engine] {engine}\n{report}" if report else f"[Research Engine] {engine}"
                self.store.update_job(
                    job_id,
                    status="done",
                    report=decorated_report[:12000],
                    engine=engine,
                    decision_log=decision_log,
                )
                logger.info(
                    "[research-agent][job] done job_id=%s engine=%s elapsed_sec=%.1f report_chars=%s decision_log_len=%s",
                    job_id,
                    engine,
                    elapsed,
                    len(report),
                    len(decision_log),
                )
            except Exception as exc:
                logger.exception("Job worker error: %s", exc)
                self.store.update_job(job_id, status="failed", error=str(exc)[:1200], engine="error")

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
