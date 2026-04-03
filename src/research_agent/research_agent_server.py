from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
from urllib.request import Request, urlopen

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
    logger.info("[route] research-agent -> gemini-cli command=%s model=%s", argv[0], model or "(default)")

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


def _safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _contains_deep_intent(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    markers = (
        "網羅",
        "徹底",
        "全部",
        "比較表",
        "じっくり",
        "deep",
        "thorough",
        "comprehensive",
    )
    return any(marker in lowered for marker in markers)


def _is_high_precision_exception_task(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    patterns = (
        r"規約|利用規約|terms|policy",
        r"学術|論文|paper|survey",
        r"矛盾|contradiction|conflict",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _should_trigger_gemma_stage(topic: str, timeout_sec: int, time_specified: bool) -> bool:
    gemma_enabled = os.getenv("RESEARCH_AGENT_GEMMA_ENABLED", "false").strip().lower() == "true"
    if not gemma_enabled:
        return False

    min_minutes = max(1, _safe_int("RESEARCH_AGENT_GEMMA_TRIGGER_MIN_MINUTES", 10))
    min_seconds = min_minutes * 60
    allow_exception = os.getenv("RESEARCH_AGENT_GEMMA_ALLOW_EXCEPTION_TRIGGER", "false").strip().lower() == "true"
    has_deep_intent = _contains_deep_intent(topic)

    if time_specified and timeout_sec >= min_seconds:
        return True
    if time_specified and has_deep_intent:
        return True
    if allow_exception and _is_high_precision_exception_task(topic):
        return True
    return False


def _run_gemini_runner(topic: str, source: str, timeout_sec: int) -> tuple[str, str, list[dict[str, Any]], str | None]:
    try:
        orchestrator = asyncio.run(build_research_orchestrator())
        report, decision_log = asyncio.run(orchestrator.answer(topic=topic, source=source, timeout_sec=timeout_sec))
        transcript = getattr(orchestrator, "last_transcript", "") or report
        logger.info(
            "[route] research-agent -> gemini-api topic=%s source=%s timeout_sec=%s report_chars=%s transcript_chars=%s",
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


def _run_gemma_worker(topic: str, source: str, initial_report: str, timeout_sec: int) -> tuple[str, str, list[dict[str, Any]], str | None]:
    endpoint = (
        os.getenv("RESEARCH_AGENT_GEMMA_ENDPOINT", "http://gemma-worker:8093/v1/research/analyze").strip()
        or "http://gemma-worker:8093/v1/research/analyze"
    )
    call_timeout = max(10, _safe_int("RESEARCH_AGENT_GEMMA_TIMEOUT_SEC", 180))
    # Keep transport timeout at least research budget + buffer.
    # Using min() here causes premature abort for long jobs (e.g. 600s -> 180s).
    call_timeout = max(call_timeout, max(15, timeout_sec + 60))

    payload = {
        "topic": topic,
        "source": source,
        "initial_report": initial_report,
        "timeout_sec": timeout_sec,
        "output_format": "evidence_summary",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "discord-ai-agent-research/1.0",
        },
    )
    try:
        logger.info(
            "[route] research-agent -> gemma-worker endpoint=%s topic=%s source=%s timeout_sec=%s",
            endpoint,
            topic[:160],
            source,
            call_timeout,
        )
        with urlopen(req, timeout=call_timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return "", "", [], f"gemma_worker_http_error:{exc}"

    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {"report": raw}

    report = ""
    if isinstance(parsed, dict):
        for key in ("report", "result", "output", "text"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                report = value.strip()
                break
        status = str(parsed.get("status", "ok")).strip().lower()
        if status in {"error", "failed"} and not report:
            detail = str(parsed.get("detail") or parsed.get("error") or "gemma_worker_failed")
            return "", "", [], f"gemma_worker_failed:{detail[:800]}"
    elif isinstance(parsed, str):
        report = parsed.strip()

    if not report:
        return "", "", [], "gemma_worker_empty_output"
    transcript = ""
    decision_log: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        transcript = str(parsed.get("transcript", "") or "").strip()
        candidate_log = parsed.get("decision_log", [])
        if isinstance(candidate_log, list):
            decision_log = [item for item in candidate_log if isinstance(item, dict)]
    return report[:12000], transcript[:24000] or report[:12000], decision_log, None


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

    gemma_enabled = os.getenv("RESEARCH_AGENT_GEMMA_ENABLED", "false").strip().lower() == "true"
    min_minutes = max(1, _safe_int("RESEARCH_AGENT_GEMMA_TRIGGER_MIN_MINUTES", 10))
    allow_exception = os.getenv("RESEARCH_AGENT_GEMMA_ALLOW_EXCEPTION_TRIGGER", "false").strip().lower() == "true"
    has_deep_intent = _contains_deep_intent(topic)
    is_exception_task = _is_high_precision_exception_task(topic)
    gemma_triggered = _should_trigger_gemma_stage(topic=topic, timeout_sec=timeout_sec, time_specified=time_specified)
    logger.info(
        "[research-agent][gemma] decision enabled=%s triggered=%s time_specified=%s timeout_sec=%s min_minutes=%s deep_intent=%s exception_task=%s allow_exception=%s auto_prefer_orchestrator=%s",
        gemma_enabled,
        gemma_triggered,
        time_specified,
        timeout_sec,
        min_minutes,
        has_deep_intent,
        is_exception_task,
        allow_exception,
        "n/a",
    )
    decision_log.append(
        {
            "stage": "gemma_trigger_decision",
            "triggered": gemma_triggered,
            "time_specified": time_specified,
            "timeout_sec": timeout_sec,
            "gemma_enabled": gemma_enabled,
            "min_minutes": min_minutes,
            "has_deep_intent": has_deep_intent,
            "is_exception_task": is_exception_task,
            "allow_exception": allow_exception,
            "auto_prefer_orchestrator": False,
        }
    )

    transcript = ""
    if clean_mode == "gemini_cli":
        logger.info("[route] research-agent mode=gemini_cli path=gemini-api")
        report, transcript, runner_log, err = _coerce_runner_result(
            _run_gemini_runner(topic=topic, source=source, timeout_sec=timeout_sec)
        )
        engine = "gemini_api"
    elif clean_mode == "fallback":
        logger.info("[route] research-agent mode=fallback path=gemma-worker")
        report, transcript, runner_log, err = _coerce_runner_result(
            _run_gemma_worker(
                topic=topic,
                source=source,
                initial_report="",
                timeout_sec=timeout_sec,
            )
        )
        engine = "gemma4"
    elif gemma_triggered:
        logger.info("[route] research-agent mode=auto path=gemma-worker gemma_triggered=%s", gemma_triggered)
        report, transcript, runner_log, err = _coerce_runner_result(
            _run_gemma_worker(
                topic=topic,
                source=source,
                initial_report="",
                timeout_sec=timeout_sec,
            )
        )
        engine = "gemma4"
    else:
        logger.info("[route] research-agent mode=auto path=gemini-api gemma_triggered=%s", gemma_triggered)
        report, transcript, runner_log, err = _coerce_runner_result(
            _run_gemini_runner(topic=topic, source=source, timeout_sec=timeout_sec)
        )
        engine = "gemini_api"

    decision_log.extend(runner_log)
    if err:
        logger.warning("[research-agent][run] runner failed engine=%s err=%s", engine, err)
        return "", err, engine, decision_log, transcript

    if not _report_is_returnable(report):
        logger.warning("[research-agent][run] report looks weak engine=%s report_chars=%s", engine, len(report))
        decision_log.append({"stage": "review", "status": "weak_report", "engine": engine, "report_chars": len(report)})
        if not report.strip():
            return "", "empty_report", engine, decision_log, transcript

    return report[:12000], None, engine, decision_log, transcript


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
            logger.info("[route] research-agent -> job_status_lookup job_id=%s", job_id)
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
