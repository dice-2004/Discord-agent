from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
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
                    artifact_path TEXT NOT NULL,
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
            if "artifact_path" not in cols:
                conn.execute("ALTER TABLE research_jobs ADD COLUMN artifact_path TEXT NOT NULL DEFAULT ''")
            if "decision_log" not in cols:
                conn.execute("ALTER TABLE research_jobs ADD COLUMN decision_log TEXT NOT NULL DEFAULT '[]'")

    def create_job(self, job_id: str, topic: str, source: str, mode: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO research_jobs(job_id, topic, source, mode, status, engine, report, artifact_path, decision_log, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'queued', '', '', '', '[]', '', ?, ?)
                    """,
                    (job_id, topic, source, mode, now, now),
                )

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        report: str = "",
        artifact_path: str = "",
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
                        SET status = ?, report = ?, artifact_path = ?, error = ?, decision_log = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (status, report, artifact_path, error, log_json, now, job_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE research_jobs
                        SET status = ?, engine = ?, report = ?, artifact_path = ?, error = ?, decision_log = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (status, engine, report, artifact_path, error, log_json, now, job_id),
                    )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                row = conn.execute(
                    """
                    SELECT job_id, topic, source, mode, status, engine, report, artifact_path, decision_log, error, created_at, updated_at
                    FROM research_jobs
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
        if row is None:
            return None
        try:
            decision_log = json.loads(str(row[8]) if row[8] else "[]")
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
            "artifact_path": str(row[7]),
            "decision_log": decision_log,
            "error": str(row[9]),
            "created_at": str(row[10]),
            "updated_at": str(row[11]),
        }


def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _build_job_id() -> str:
    return f"rj-{int(time.time() * 1000)}-{os.getpid()}-{threading.get_ident()}"


def _safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _run_gemini_runner(
    topic: str,
    source: str,
    timeout_sec: int,
    *,
    time_specified: bool,
) -> tuple[str, str, list[dict[str, Any]], str | None]:
    try:
        orchestrator = asyncio.run(build_research_orchestrator())
        report, decision_log = asyncio.run(
            orchestrator.answer(
                topic=topic,
                source=source,
                timeout_sec=timeout_sec,
                time_specified=time_specified,
            )
        )
        transcript = getattr(orchestrator, "last_transcript", "") or report
        logger.info(
            "[route] research-agent -> gemini-cli topic=%s source=%s timeout_sec=%s report_chars=%s transcript_chars=%s",
            topic[:160],
            source,
            timeout_sec,
            len(report),
            len(transcript),
        )
        return report, transcript, decision_log, None
    except Exception as exc:
        logger.exception("Gemini runner failed: %s", exc)
        return "", "", [], f"gemini_runner_failed:{exc}"


def _build_research_artifact(job_id: str, engine: str, report: str, transcript: str, decision_log: list[dict[str, Any]]) -> str:
    artifact_dir = Path(os.getenv("RESEARCH_AGENT_ARTIFACT_DIR", "./data/runtime/research_artifacts").strip() or "./data/runtime/research_artifacts")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    safe_engine = re.sub(r"[^a-zA-Z0-9_.-]+", "_", engine or "unknown")
    artifact_path = artifact_dir / f"{job_id}-{safe_engine}.txt"
    body = [
        f"job_id: {job_id}",
        f"engine: {engine}",
        "",
        "[summary]",
        report.strip() or "(empty)",
        "",
        "[decision_log]",
        json.dumps(decision_log or [], ensure_ascii=False, indent=2),
        "",
        "[raw_transcript]",
        transcript.strip() or "(empty)",
    ]
    artifact_path.write_text("\n".join(body), encoding="utf-8")
    return str(artifact_path)


def _report_is_returnable(report: str) -> bool:
    body = (report or "").strip()
    if not body:
        return False
    placeholders = {
        "(タイムアウト)",
        "(解析エラー)",
        "(レスポンス生成失敗)",
        "(調査結果の生成に失敗しました)",
    }
    return body not in placeholders and len(body) >= 40


def _extract_used_tools(decision_log: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    tools: list[str] = []
    for entry in decision_log:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("action", "")).strip().lower() != "tool":
            continue
        name = str(entry.get("tool", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        tools.append(name)
    return tools


def _assess_report_quality(report: str, decision_log: list[dict[str, Any]]) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    body = (report or "").strip()
    tools = _extract_used_tools(decision_log)

    if not _report_is_returnable(body):
        reasons.append("weak_or_placeholder_report")
    if len(body) < 280:
        reasons.append("report_too_short")
    if "[参考URL]" not in body and "http://" not in body and "https://" not in body:
        reasons.append("missing_sources")
    if not tools:
        reasons.append("no_tool_usage")

    meta = {
        "report_chars": len(body),
        "tool_count": len(tools),
        "tools": tools,
    }
    return len(reasons) == 0, reasons, meta


def _coerce_runner_result(raw: Any) -> tuple[str, str, list[dict[str, Any]], str | None]:
    if isinstance(raw, tuple):
        if len(raw) == 4:
            report, transcript, runner_log, err = raw
            report_text = str(report or "")
            transcript_text = str(transcript or "")
            safe_log = runner_log if isinstance(runner_log, list) else []
            safe_err = None if err is None else str(err)
            return report_text, transcript_text, safe_log, safe_err
        if len(raw) == 2:
            report, err = raw
            return str(report or ""), "", [], None if err is None else str(err)
    return "", "", [], "invalid_runner_result"


def _run_research(
    topic: str,
    source: str,
    mode: str,
    timeout_sec: int = 60,
    time_specified: bool = False,
) -> tuple[str, str | None, str, list[dict[str, Any]], str]:
    clean_mode = (mode or "auto").strip().lower() or "auto"
    decision_log: list[dict[str, Any]] = []
    logger.info(
        "[research-agent][run] start topic=%s source=%s mode=%s time_specified=%s timeout_sec=%s",
        topic,
        source,
        clean_mode,
        time_specified,
        timeout_sec,
    )
    transcript = ""
    if clean_mode not in {"auto", "gemini_cli", "fallback"}:
        logger.warning("[research-agent][run] unknown mode=%s; defaulting to gemini-cli", clean_mode)
    logger.info("[route] research-agent mode=%s path=gemini-cli", clean_mode)
    engine = "gemini_cli"
    run_started = time.time()
    min_explore_sec = int(timeout_sec * 0.9) if time_specified else 0
    planned_attempts = 1 if not time_specified else max(1, min(5, timeout_sec // 120 + 1))

    best_report = ""
    best_transcript = ""
    best_quality_score = -1

    for attempt in range(1, planned_attempts + 1):
        elapsed_before = int(time.time() - run_started)
        remaining = max(0, timeout_sec - elapsed_before)
        if time_specified and remaining <= 5:
            decision_log.append(
                {
                    "stage": "budget_gate",
                    "attempt": attempt,
                    "status": "stop_no_budget",
                    "elapsed_sec": elapsed_before,
                    "remaining_sec": remaining,
                }
            )
            break

        call_timeout = max(10, remaining) if time_specified else timeout_sec
        decision_log.append(
            {
                "stage": "attempt_start",
                "attempt": attempt,
                "call_timeout_sec": call_timeout,
                "elapsed_sec": elapsed_before,
            }
        )

        report, transcript, runner_log, err = _coerce_runner_result(
            _run_gemini_runner(
                topic=topic,
                source=source,
                timeout_sec=call_timeout,
                time_specified=time_specified,
            )
        )
        decision_log.extend(runner_log)

        elapsed_after = int(time.time() - run_started)
        explored_enough = (elapsed_after >= min_explore_sec) if time_specified else True

        if err:
            decision_log.append(
                {
                    "stage": "attempt_error",
                    "attempt": attempt,
                    "elapsed_sec": elapsed_after,
                    "error": str(err)[:300],
                }
            )
            if time_specified and attempt < planned_attempts and not explored_enough:
                continue
            logger.warning("[research-agent][run] runner failed engine=%s err=%s", engine, err)
            return "", err, engine, decision_log, transcript

        if not time_specified:
            quality_ok, quality_reasons, quality_meta = _assess_report_quality(report, runner_log)
            decision_log.append(
                {
                    "stage": "quality_eval",
                    "attempt": attempt,
                    "elapsed_sec": elapsed_after,
                    "explored_enough": explored_enough,
                    "quality_ok": quality_ok,
                    "quality_reasons": quality_reasons,
                    **quality_meta,
                }
            )

            quality_score = (
                (100 if quality_ok else 0)
                + (quality_meta.get("tool_count", 0) * 10)
                + min(50, int(quality_meta.get("report_chars", 0)) // 120)
            )
            if _report_is_returnable(report) and quality_score > best_quality_score:
                best_quality_score = quality_score
                best_report = report
                best_transcript = transcript

            if quality_ok:
                return report[:12000], None, engine, decision_log, transcript
            break

        if _report_is_returnable(report):
            candidate_score = len(report) + len(transcript)
            if candidate_score > best_quality_score:
                best_quality_score = candidate_score
                best_report = report
                best_transcript = transcript

        if not explored_enough and attempt < planned_attempts:
            decision_log.append(
                {
                    "stage": "budget_gate",
                    "attempt": attempt,
                    "status": "continue_exploration_until_90pct",
                    "elapsed_sec": elapsed_after,
                    "target_sec": min_explore_sec,
                }
            )
            continue

        quality_ok, quality_reasons, quality_meta = _assess_report_quality(report, runner_log)
        decision_log.append(
            {
                "stage": "quality_eval",
                "attempt": attempt,
                "elapsed_sec": elapsed_after,
                "explored_enough": explored_enough,
                "quality_ok": quality_ok,
                "quality_reasons": quality_reasons,
                **quality_meta,
            }
        )

        quality_score = (
            (100 if quality_ok else 0)
            + (quality_meta.get("tool_count", 0) * 10)
            + min(50, int(quality_meta.get("report_chars", 0)) // 120)
        )
        if _report_is_returnable(report) and quality_score > best_quality_score:
            best_quality_score = quality_score
            best_report = report
            best_transcript = transcript

        if quality_ok:
            return report[:12000], None, engine, decision_log, transcript

        if attempt < planned_attempts:
            decision_log.append(
                {
                    "stage": "quality_gate",
                    "attempt": attempt,
                    "status": "return_to_gemini_cli",
                    "quality_reasons": quality_reasons,
                }
            )
            continue

    if _report_is_returnable(best_report):
        decision_log.append(
            {
                "stage": "finalize",
                "status": "use_best_attempt",
                "best_report_chars": len(best_report),
            }
        )
        return best_report[:12000], None, engine, decision_log, best_transcript

    logger.warning("[research-agent][run] report looks weak engine=%s report_chars=%s", engine, len(best_report))
    return "", "empty_report", engine, decision_log, best_transcript


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
    topic: str, source: str, initial_report: str, timeout_sec: int = 60
) -> tuple[str, list[dict[str, Any]]]:
    """Run management AI (orchestrator) for deeper research with time budget."""
    try:
        logger.info("[research-agent][orchestrator] start topic=%s source=%s timeout_sec=%s", topic, source, timeout_sec)
        orchestrator = await build_research_orchestrator()
        # Pass timeout_sec to orchestrator for decision loop planning
        question = f"{topic}\n\n[Initial Gemini CLI report]\n{initial_report}"
        answer, decision_log = await orchestrator.answer(topic=question, source=source, timeout_sec=timeout_sec)
        logger.info("[research-agent][orchestrator] done decision_log_len=%s timeout_sec=%s", len(decision_log), timeout_sec)
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
            logger.debug("[route] research-agent -> job_status_lookup job_id=%s", job_id)
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
        timeout_sec = max(10, _safe_int("_request_timeout_sec", _safe_int("RESEARCH_AGENT_JOB_TIMEOUT_SEC", 60)))
        time_specified = _safe_bool(payload.get("time_specified"), default=False)
        try:
            raw_timeout = str(payload.get("timeout_sec", "")).strip()
            if raw_timeout:
                timeout_sec = max(10, int(raw_timeout))
        except ValueError:
            pass
        if not topic:
            self._send_json(400, {"status": "error", "code": "invalid_topic"})
            return

        if self.store is None:
            self._send_json(500, {"status": "error", "code": "store_unavailable"})
            return

        job_id = _build_job_id()
        self.store.create_job(job_id=job_id, topic=topic, source=source, mode=mode)
        logger.info(
            "[route] main-agent -> research-agent accepted job_id=%s mode=%s source=%s timeout_sec=%s time_specified=%s",
            job_id,
            mode,
            source,
            timeout_sec,
            time_specified,
        )

        def _worker() -> None:
            assert self.store is not None
            job_timeout = timeout_sec + 30  # Add buffer for cleanup
            start_time = time.time()

            logger.info("[research-agent][job] start job_id=%s topic=%s source=%s mode=%s timeout_sec=%s", job_id, topic, source, mode, timeout_sec)
            logger.info("[route] research-agent job_started job_id=%s mode=%s source=%s timeout_sec=%s", job_id, mode, source, timeout_sec)
            self.store.update_job(job_id, status="running", engine="")
            try:
                report, err, engine, decision_log, transcript = _run_research(
                    topic=topic,
                    source=source,
                    mode=mode,
                    timeout_sec=timeout_sec,
                    time_specified=time_specified,
                )
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
                artifact_path = _build_research_artifact(
                    job_id=job_id,
                    engine=engine,
                    report=report,
                    transcript=transcript,
                    decision_log=decision_log,
                )
                decorated_report = (
                    f"[Research Engine] {engine}\n{report}\n\n[原文アーティファクト]\n{artifact_path}"
                    if report
                    else f"[Research Engine] {engine}\n[原文アーティファクト]\n{artifact_path}"
                )
                self.store.update_job(
                    job_id,
                    status="done",
                    report=decorated_report[:12000],
                    artifact_path=artifact_path,
                    engine=engine,
                    decision_log=decision_log,
                )
                logger.info("[route] research-agent job_completed job_id=%s engine=%s decision_log_len=%s", job_id, engine, len(decision_log))
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
