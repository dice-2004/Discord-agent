from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.request import Request, urlopen

from tools.research_loop import run_model_research_loop

logger = logging.getLogger(__name__)


def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _ollama_base_url() -> str:
    return (os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").strip() or "http://ollama:11434").rstrip("/")


def _ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "gemma4:e2b").strip() or "gemma4:e2b"


def _call_ollama(prompt: str, timeout_sec: int) -> tuple[str, str | None]:
    num_predict = max(64, _safe_int("GEMMA_WORKER_NUM_PREDICT", 128))
    temperature = float(os.getenv("GEMMA_WORKER_TEMPERATURE", "0.2").strip() or "0.2")
    logger.info(
        "Gemma call start model=%s timeout_sec=%s num_predict=%s temperature=%s prompt_chars=%s",
        _ollama_model(),
        timeout_sec,
        num_predict,
        temperature,
        len(prompt),
    )
    payload = {
        "model": _ollama_model(),
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    req = Request(
        f"{_ollama_base_url()}/api/generate",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "gemma-worker/1.0",
        },
    )
    try:
        logger.info("[route] gemma-worker -> ollama path=/api/generate model=%s timeout_sec=%s", _ollama_model(), max(5, timeout_sec))
        with urlopen(req, timeout=max(5, timeout_sec)) as res:
            raw = res.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return "", f"ollama_http_error:{exc}"

    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        return "", "ollama_invalid_json"

    if not isinstance(parsed, dict):
        return "", "ollama_invalid_payload"
    text = str(parsed.get("response", "") or "").strip()
    if not text:
        # Some ollama responses return done=true with empty text under load.
        # Retry once with a compact prompt to recover a non-empty response.
        compact_prompt = (
            "次の指示に短く答えてください。出力は1行以上、空文字は禁止。\n\n"
            f"{prompt[:1800]}"
        )
        retry_payload = {
            "model": _ollama_model(),
            "prompt": compact_prompt,
            "stream": False,
            "options": {
                "num_predict": max(96, num_predict),
                "temperature": min(temperature, 0.15),
            },
        }
        retry_req = Request(
            f"{_ollama_base_url()}/api/generate",
            data=json.dumps(retry_payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "gemma-worker/1.0",
            },
        )
        try:
            with urlopen(retry_req, timeout=max(5, timeout_sec)) as retry_res:
                retry_raw = retry_res.read().decode("utf-8", errors="replace")
            retry_parsed = json.loads(retry_raw) if retry_raw else {}
            if isinstance(retry_parsed, dict):
                retry_text = str(retry_parsed.get("response", "") or "").strip()
                if retry_text:
                    logger.info("Gemma compact-retry succeeded model=%s response_chars=%s", _ollama_model(), len(retry_text))
                    return retry_text, None
        except Exception:
            pass
        return "", "ollama_empty_response"
    logger.info("Gemma call done model=%s response_chars=%s", _ollama_model(), len(text))
    return text, None


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"[a-zA-Z0-9_\-]+|[一-龥]{2,}|[ぁ-ん]{2,}|[ァ-ンー]{2,}", text.lower()))


def _recency_score(timestamp_text: str) -> float:
    if not timestamp_text:
        return 0.15
    try:
        ts = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = max((now - ts.astimezone(timezone.utc)).total_seconds() / 86400.0, 0.0)
        if days <= 1:
            return 1.0
        if days <= 7:
            return 0.82
        if days <= 30:
            return 0.56
        if days <= 90:
            return 0.34
        return 0.18
    except Exception:
        return 0.15


def _local_rerank(query: str, candidates: list[dict[str, Any]]) -> list[int]:
    q_tokens = _tokenize(query)
    weighted: list[tuple[float, int]] = []
    for idx, row in enumerate(candidates):
        content = str(row.get("content", "") or "")
        doc_tokens = _tokenize(content)
        overlap = 0.0
        if q_tokens and doc_tokens:
            overlap = len(q_tokens.intersection(doc_tokens)) / max(1, len(q_tokens))
        recency = _recency_score(str(row.get("timestamp", "") or ""))
        score = (0.75 * overlap) + (0.25 * recency)
        weighted.append((score, idx))
    weighted.sort(key=lambda item: item[0], reverse=True)
    return [idx for _, idx in weighted]


def _extract_json_array(text: str) -> list[int]:
    candidate = (text or "").strip()
    if not candidate:
        return []
    # Try direct parse
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return [int(x) for x in parsed if str(x).isdigit()]
    except Exception:
        pass

    # Try fenced code block extraction
    m = re.search(r"\[[\s\S]*\]", candidate)
    if not m:
        return []
    try:
        parsed = json.loads(m.group(0))
        if isinstance(parsed, list):
            return [int(x) for x in parsed if str(x).isdigit()]
    except Exception:
        return []
    return []


def _rerank_with_gemma(query: str, candidates: list[dict[str, Any]], timeout_sec: int) -> list[int]:
    logger.info("Gemma rerank start query_chars=%s candidate_count=%s timeout_sec=%s", len(query), len(candidates), timeout_sec)
    prompt = (
        "あなたは検索結果再ランキング器です。"
        "以下のqueryに最も関連する順に候補indexを並べ、JSON配列だけを返してください。"
        "説明文は禁止。\n\n"
        f"query: {query}\n"
        f"candidates: {json.dumps(candidates, ensure_ascii=False)[:12000]}\n"
        "output_example: [2,0,1]"
    )
    text, err = _call_ollama(prompt, timeout_sec)
    if err:
        logger.warning("Gemma rerank failed: %s", err)
        return _local_rerank(query, candidates)

    ranking = _extract_json_array(text)
    if not ranking:
        return _local_rerank(query, candidates)

    seen: set[int] = set()
    normalized: list[int] = []
    for idx in ranking:
        if idx < 0 or idx >= len(candidates) or idx in seen:
            continue
        seen.add(idx)
        normalized.append(idx)
    for idx in range(len(candidates)):
        if idx not in seen:
            normalized.append(idx)
    logger.info("Gemma rerank done query_chars=%s candidate_count=%s ranking=%s", len(query), len(candidates), normalized[:10])
    return normalized


def _analyze_research(topic: str, source: str, initial_report: str, timeout_sec: int) -> tuple[str, str, list[dict[str, Any]]]:
    logger.info(
        "Gemma research loop start topic_chars=%s source=%s timeout_sec=%s initial_report_chars=%s",
        len(topic),
        source,
        timeout_sec,
        len(initial_report),
    )

    def _call(prompt: str) -> str:
        text, err = _call_ollama(prompt, timeout_sec)
        if err:
            logger.warning("Gemma tool loop call failed: %s", err)
            return json.dumps(
                {
                    "action": "respond",
                    "response": f"(Gemma call failed: {err})",
                },
                ensure_ascii=False,
            )
        return text

    result = run_model_research_loop(
        topic=f"{topic}\n\n[Initial context]\n{initial_report[:4000]}",
        source=source,
        timeout_sec=timeout_sec,
        model_name=_ollama_model(),
        model_call=_call,
        loop_label="gemma4-research",
        max_turns=max(4, min(16, timeout_sec // 75 + 4)),
    )
    logger.info(
        "Gemma research loop done report_chars=%s transcript_chars=%s decision_log_len=%s",
        len(result.report),
        len(result.transcript),
        len(result.decision_log),
    )
    return result.report[:12000], result.transcript[:24000], result.decision_log


class GemmaHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            logger.warning("Client disconnected before response write completed")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok", "service": "gemma-worker"})
            return
        self._send_json(404, {"status": "error", "code": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        content_len = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_len).decode("utf-8", errors="replace") if content_len > 0 else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            self._send_json(400, {"status": "error", "code": "invalid_json"})
            return

        timeout_sec = max(5, _safe_int("GEMMA_WORKER_HTTP_TIMEOUT_SEC", 120))

        if self.path == "/v1/logsearch/rerank":
            query = str(payload.get("query", "") or "").strip()
            candidates = payload.get("candidates")
            logger.info(
                "Gemma worker request path=/v1/logsearch/rerank route=research-agent->gemma-worker query_chars=%s candidate_count=%s timeout_sec=%s",
                len(query),
                len(candidates) if isinstance(candidates, list) else -1,
                timeout_sec,
            )
            if not query:
                self._send_json(400, {"status": "error", "code": "invalid_query"})
                return
            if not isinstance(candidates, list) or not candidates:
                self._send_json(400, {"status": "error", "code": "invalid_candidates"})
                return
            rows = [row for row in candidates if isinstance(row, dict)]
            if not rows:
                self._send_json(400, {"status": "error", "code": "invalid_candidates"})
                return
            ranking = _rerank_with_gemma(query=query, candidates=rows, timeout_sec=timeout_sec)
            self._send_json(200, {"status": "ok", "ranking": ranking})
            return

        if self.path == "/v1/research/analyze":
            topic = str(payload.get("topic", "") or "").strip()
            source = str(payload.get("source", "auto") or "auto").strip().lower() or "auto"
            initial_report = str(payload.get("initial_report", "") or "")
            req_timeout = payload.get("timeout_sec")
            if isinstance(req_timeout, int):
                timeout_sec = max(timeout_sec, req_timeout)
            elif isinstance(req_timeout, str) and req_timeout.strip().isdigit():
                timeout_sec = max(timeout_sec, int(req_timeout.strip()))
            timeout_sec = min(timeout_sec, 900)

            logger.info(
                "Gemma worker request path=/v1/research/analyze route=research-agent->gemma-worker topic_chars=%s source=%s timeout_sec=%s initial_report_chars=%s",
                len(topic),
                source,
                timeout_sec,
                len(initial_report),
            )

            if not topic:
                self._send_json(400, {"status": "error", "code": "invalid_topic"})
                return

            report, transcript, decision_log = _analyze_research(topic=topic, source=source, initial_report=initial_report, timeout_sec=timeout_sec)
            self._send_json(
                200,
                {
                    "status": "ok",
                    "report": report,
                    "transcript": transcript,
                    "decision_log": decision_log,
                    "model": _ollama_model(),
                    "provider": "ollama",
                },
            )
            return

        self._send_json(404, {"status": "error", "code": "not_found"})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("GemmaWorker %s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, (os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    host = os.getenv("GEMMA_WORKER_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _safe_int("GEMMA_WORKER_PORT", 8093)
    server = ThreadingHTTPServer((host, port), GemmaHandler)
    logger.info("Gemma Worker started at %s:%s model=%s ollama=%s", host, port, _ollama_model(), _ollama_base_url())
    server.serve_forever()


if __name__ == "__main__":
    main()
