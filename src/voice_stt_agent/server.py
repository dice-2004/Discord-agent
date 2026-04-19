from __future__ import annotations

import json
import logging
import os
import threading
import tempfile
import time
import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from tools.ai_exchange_logger import log_ai_exchange

logger = logging.getLogger(__name__)
_stt_model_lock = threading.Lock()
_stt_model: object | None = None
_spotify_token_cache: dict[str, object] = {"access_token": "", "expires_at": 0}


def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _now_iso() -> str:
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).isoformat()


def _rule_based_intent(text: str) -> tuple[str, float, str, str]:
    lowered = (text or "").lower()
    if any(k in lowered for k in ("流して", "再生", "spotify", "曲", "かけて", "jam", "キュー", "queue")):
        query = text
        for kw in ("流して", "再生して", "再生", "かけて", "を", "という曲", "の曲", "spotifyに", "に", "入れて", "追加して", "追加"):
            query = query.replace(kw, "")
        return "add_to_jam", 0.65, query.strip(), "rule_music_keyword"
    if any(k in lowered for k in ("天気", "weather", "雨", "晴れ")):
        return "weather_recommend", 0.6, "", "rule_weather_keyword"
    return "ignore", 0.5, "", "rule_default"


# Spotify API handling moved to src/tools/music_tools.py


def _call_ollama_intent(text: str) -> tuple[str, float, str, str]:
    use_ollama = os.getenv("MUSIC_INTENT_USE_OLLAMA", "false").strip().lower() == "true"
    if not use_ollama:
        intent, confidence, rule_q, reason = _rule_based_intent(text)
        return intent, confidence, rule_q, f"rule_only_mode:{reason}"

    base = (os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").strip() or "http://ollama:11434").rstrip("/")
    model = os.getenv("MUSIC_INTENT_OLLAMA_MODEL", os.getenv("OLLAMA_MODEL", "gemma4:e2b")).strip() or "gemma4:e2b"
    timeout_sec = max(5, _safe_int("MUSIC_INTENT_OLLAMA_TIMEOUT_SEC", 30))

    prompt = (
        "You are an intent classifier for a Discord voice assistant. "
        "Return JSON only with keys: intent, confidence, extracted_query, reason. "
        "Allowed intents are strictly: add_to_jam, weather_recommend, ignore. "
        "If the utterance asks to play/add a song, artist, or music (Japanese examples: '流して', 'かけて', '再生', '曲', 'キュー'), "
        "intent MUST be add_to_jam. "
        "If intent is add_to_jam, extracted_query MUST be ONLY the requested song name or artist. "
        "If weather-based music suggestion is requested, intent MUST be weather_recommend. "
        "Otherwise use ignore. "
        f"Utterance: {text}"
    )
    keep_alive_raw = os.getenv("OLLAMA_KEEP_ALIVE", "5m").strip() or "5m"
    # Convert to int if it looks like a number (including negative strings like "-1")
    try:
        if keep_alive_raw.lstrip('-').isdigit():
            keep_alive: int | str = int(keep_alive_raw)
        else:
            keep_alive = keep_alive_raw
    except (ValueError, TypeError):
        keep_alive = keep_alive_raw

    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "keep_alive": keep_alive,
        "options": {
            "temperature": 0.1,
            "num_predict": 96,
        },
    }
    req = Request(
        f"{base}/api/generate",
        method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )

    try:
        with urlopen(req, timeout=timeout_sec) as res:
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        llm_text = str(payload.get("response", "")).strip()
        log_ai_exchange(
            component="voice-stt-agent",
            model=model,
            prompt=prompt,
            response=llm_text,
            metadata={
                "phase": "ollama_intent",
                "base": base,
            },
        )
        data = json.loads(llm_text) if llm_text else {}
        intent = str(data.get("intent", "ignore")).strip() or "ignore"
        confidence = float(data.get("confidence", 0.0) or 0.0)
        reason = str(data.get("reason", ""))[:200]
        query = str(data.get("extracted_query", ""))
        intent = intent if intent in {"add_to_jam", "weather_recommend", "ignore"} else "ignore"

        # Guardrail: when LLM misses obvious music/weather keywords, rule-based result takes precedence.
        rule_intent, rule_conf, rule_query, rule_reason = _rule_based_intent(text)
        if intent == "ignore" and rule_intent != "ignore":
            return rule_intent, max(rule_conf, confidence), rule_query, f"llm_override_by_rule:{rule_reason}"

        return intent, max(0.0, min(confidence, 1.0)), query, reason
    except Exception as exc:
        log_ai_exchange(
            component="voice-stt-agent",
            model=model,
            prompt=prompt,
            response="",
            metadata={
                "phase": "ollama_intent",
                "base": base,
            },
            error=str(exc),
        )
        logger.debug("ollama intent failed, fallback to rules: %s", exc)

    return _rule_based_intent(text)


# Spotify tracking logics moved to src/tools/music_tools.py


def _process_transcript(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", "")).strip()
    if not text:
        return {"status": "error", "code": "invalid_text"}

    started = time.time()
    intent, confidence, query, reason = _call_ollama_intent(text)
    
    if not query:
        query = text

    result: dict[str, Any] = {
        "status": "ok",
        "intent": intent,
        "confidence": confidence,
        "query": query,
        "reason": reason,
        "text": text,
        "guild_id": int(payload.get("guild_id", 0) or 0),
        "channel_id": int(payload.get("channel_id", 0) or 0),
        "user_id": int(payload.get("user_id", 0) or 0),
        "received_at": _now_iso(),
    }

    if intent == "add_to_jam":
        from tools.music_tools import add_to_jam
        err = add_to_jam(query)
        if err is not None:
            result.update({"action": "noop", "detail": err})
        else:
            result.update({"action": "spotify_queue_add", "detail": "added dynamically"})
    elif intent == "weather_recommend":
        from tools.music_tools import weather_recommend
        err = weather_recommend()
        if err is not None:
            result.update({"action": "noop", "detail": err})
        else:
            result.update({"action": "weather_recommend_added"})
    else:
        result.update({"action": "noop"})

    result["elapsed_ms"] = int((time.time() - started) * 1000)
    logger.info(
        "voice-stt unified processed: intent=%s confidence=%.2f action=%s elapsed_ms=%s",
        result.get("intent"),
        float(result.get("confidence", 0.0) or 0.0),
        result.get("action"),
        result.get("elapsed_ms"),
    )
    return result


def _audio_dump_enabled() -> bool:
    return os.getenv("VOICE_STT_AUDIO_DUMP_ENABLED", "false").strip().lower() == "true"


def _write_audio_dump(payload: bytes, *, guild_id: int, channel_id: int, user_id: int, ext: str) -> str:
    root = Path(os.getenv("VOICE_STT_AUDIO_DUMP_DIR", "./data/runtime/voice_audio").strip() or "./data/runtime/voice_audio")
    root.mkdir(parents=True, exist_ok=True)
    safe_ext = (ext or "bin").strip().lower()
    if not safe_ext.isalnum() or len(safe_ext) > 8:
        safe_ext = "bin"
    ts_ms = int(time.time() * 1000)
    name = f"g{guild_id}_c{channel_id}_u{user_id}_{ts_ms}.{safe_ext}"
    path = root / name
    path.write_bytes(payload)
    return str(path)


def _stt_enabled() -> bool:
    return os.getenv("VOICE_STT_TRANSCRIBE_ENABLED", "true").strip().lower() == "true"


def _get_stt_model() -> object:
    global _stt_model
    if _stt_model is not None:
        return _stt_model
    with _stt_model_lock:
        if _stt_model is not None:
            return _stt_model
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError(f"faster_whisper_unavailable:{exc}") from exc

        model_name = os.getenv("VOICE_STT_WHISPER_MODEL", "small").strip() or "small"
        device = os.getenv("VOICE_STT_WHISPER_DEVICE", "cpu").strip() or "cpu"
        compute_type = os.getenv("VOICE_STT_WHISPER_COMPUTE_TYPE", "int8").strip() or "int8"
        _stt_model = WhisperModel(model_name, device=device, compute_type=compute_type)
        logger.info("Loaded faster-whisper model=%s device=%s compute_type=%s", model_name, device, compute_type)
        return _stt_model


def _transcribe_audio_bytes(payload: bytes, ext: str) -> tuple[str, str | None]:
    if not payload:
        return "", "audio_empty"
    if not _stt_enabled():
        return "", "stt_disabled"

    model = _get_stt_model()
    language = os.getenv("VOICE_STT_LANGUAGE", "ja").strip() or "ja"
    beam_size = max(1, _safe_int("VOICE_STT_BEAM_SIZE", 1))
    vad_filter = os.getenv("VOICE_STT_VAD_FILTER", "true").strip().lower() == "true"

    suffix = f".{(ext or 'wav').strip().lower()}"
    if not suffix.startswith("."):
        suffix = ".wav"

    with tempfile.NamedTemporaryFile(prefix="voice_chunk_", suffix=suffix, delete=True) as tmp:
        tmp.write(payload)
        tmp.flush()
        try:
            segments, _ = model.transcribe(
                tmp.name,
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
            )
            text = " ".join(str(seg.text).strip() for seg in segments if str(seg.text).strip()).strip()
            if not text:
                return "", "stt_no_speech"
            return text, None
        except Exception as exc:
            return "", f"stt_failed:{exc}"


def _forward_transcript(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    result = _process_transcript(payload)
    if result.get("status") != "ok":
        return 400, str(result.get("code", "invalid_request"))
    return 200, None


class VoiceSttHandler(BaseHTTPRequestHandler):
    shared_token = "change_me"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        sent = self.headers.get("X-Voice-Token", "").strip()
        expected = self.shared_token.strip() if self.shared_token else ""
        if not expected:
            return True
        return bool(sent and sent == expected)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"status": "ok", "service": "voice-stt-agent", "mode": "unified", "ts": _now_iso()})
            return
        self._json(404, {"status": "error", "code": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/v1/transcripts", "/v1/transcripts/mock", "/v1/audio/chunks"):
            self._json(404, {"status": "error", "code": "not_found"})
            return

        if not self._authorized():
            self._json(403, {"status": "error", "code": "forbidden"})
            return

        if self.path == "/v1/audio/chunks":
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            audio_payload = self.rfile.read(content_len) if content_len > 0 else b""
            if not audio_payload:
                self._json(400, {"status": "error", "code": "invalid_audio_payload"})
                return

            guild_id = int(self.headers.get("X-Guild-Id", "0") or "0")
            channel_id = int(self.headers.get("X-Channel-Id", "0") or "0")
            user_id = int(self.headers.get("X-User-Id", "0") or "0")
            ext = self.headers.get("X-Audio-Ext", "wav")

            dumped = False
            dump_path = ""
            if _audio_dump_enabled():
                try:
                    dump_path = _write_audio_dump(
                        audio_payload,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        ext=ext,
                    )
                    dumped = True
                except Exception as exc:
                    logger.warning("audio dump failed: %s", exc)

            started = time.time()
            text, stt_err = _transcribe_audio_bytes(audio_payload, ext)
            if stt_err is not None:
                status = 202 if stt_err in {"stt_no_speech", "stt_disabled"} else 500
                self._json(
                    status,
                    {
                        "status": "accepted" if status == 202 else "error",
                        "bytes": len(audio_payload),
                        "dumped": dumped,
                        "dump_path": dump_path,
                        "detail": stt_err,
                        "elapsed_ms": int((time.time() - started) * 1000),
                    },
                )
                return

            transcript_event = {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "text": text,
                "started_at": _now_iso(),
                "ended_at": _now_iso(),
                "source": "audio_chunk",
                "created_at": _now_iso(),
            }
            result = _process_transcript(transcript_event)
            if result.get("status") != "ok":
                self._json(400, result)
                return
            result.update(
                {
                    "bytes": len(audio_payload),
                    "dumped": dumped,
                    "dump_path": dump_path,
                    "stt_text": text,
                }
            )
            result["elapsed_ms_total"] = int((time.time() - started) * 1000)
            self._json(200, result)
            return

        content_len = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_len).decode("utf-8", errors="replace") if content_len > 0 else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            self._json(400, {"status": "error", "code": "invalid_json"})
            return

        text = str(payload.get("text", "")).strip()
        guild_id = int(payload.get("guild_id", 0) or 0)
        channel_id = int(payload.get("channel_id", 0) or 0)
        user_id = int(payload.get("user_id", 0) or 0)

        if not text:
            self._json(400, {"status": "error", "code": "invalid_text"})
            return

        transcript_event = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "text": text,
            "started_at": str(payload.get("started_at") or _now_iso()),
            "ended_at": str(payload.get("ended_at") or _now_iso()),
            "source": str(payload.get("source") or "mock"),
            "created_at": _now_iso(),
        }
        result = _process_transcript(transcript_event)
        if result.get("status") != "ok":
            self._json(400, result)
            return
        self._json(200, result)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("VoiceSttAgent %s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, (os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    host = os.getenv("VOICE_STT_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _safe_int("VOICE_STT_PORT", 8095)
    VoiceSttHandler.shared_token = os.getenv("VOICE_STT_SHARED_TOKEN", "").strip()

    server = build_http_server(host=host, port=port)
    logger.info("Voice STT Agent started at %s:%s", host, port)
    server.serve_forever()


def build_http_server(host: str, port: int) -> ThreadingHTTPServer:
    VoiceSttHandler.shared_token = os.getenv("VOICE_STT_SHARED_TOKEN", "").strip()
    return ThreadingHTTPServer((host, port), VoiceSttHandler)


def start_http_server_in_thread(host: str, port: int) -> ThreadingHTTPServer:
    server = build_http_server(host=host, port=port)
    t = threading.Thread(target=server.serve_forever, name="voice-stt-http", daemon=True)
    t.start()
    logger.info("Voice STT HTTP endpoint started at %s:%s", host, port)
    return server


if __name__ == "__main__":
    main()
