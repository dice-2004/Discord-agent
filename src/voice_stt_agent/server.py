from __future__ import annotations

import json
import logging
import os
import threading
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
_stt_model_lock = threading.Lock()
_stt_model: object | None = None


def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _call_ollama_intent(text: str) -> tuple[str, float, str]:
    use_ollama = os.getenv("MUSIC_INTENT_USE_OLLAMA", "false").strip().lower() == "true"
    if not use_ollama:
        lowered = text.lower()
        if any(k in lowered for k in ("流して", "再生", "spotify", "曲", "かけて", "jam")):
            return "add_to_jam", 0.65, "rule_only_mode_music_keyword"
        if any(k in lowered for k in ("天気", "weather", "雨", "晴れ")):
            return "weather_recommend", 0.6, "rule_only_mode_weather_keyword"
        return "ignore", 0.5, "rule_only_mode_default"

    base = (os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").strip() or "http://ollama:11434").rstrip("/")
    model = os.getenv("MUSIC_INTENT_OLLAMA_MODEL", os.getenv("OLLAMA_MODEL", "gemma4:e2b")).strip() or "gemma4:e2b"
    timeout_sec = max(5, _safe_int("MUSIC_INTENT_OLLAMA_TIMEOUT_SEC", 30))

    prompt = (
        "Classify the user utterance for Discord voice assistant. Return JSON only with keys: "
        "intent (add_to_jam|weather_recommend|ignore), confidence (0..1), reason. "
        f"Utterance: {text}"
    )
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
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
        data = json.loads(llm_text) if llm_text else {}
        intent = str(data.get("intent", "ignore")).strip() or "ignore"
        confidence = float(data.get("confidence", 0.0) or 0.0)
        reason = str(data.get("reason", ""))[:200]
        return intent, max(0.0, min(confidence, 1.0)), reason
    except Exception as exc:
        logger.debug("ollama intent failed, fallback to rules: %s", exc)

    lowered = text.lower()
    if any(k in lowered for k in ("流して", "再生", "spotify", "曲", "かけて", "jam")):
        return "add_to_jam", 0.65, "rule_fallback_music_keyword"
    if any(k in lowered for k in ("天気", "weather", "雨", "晴れ")):
        return "weather_recommend", 0.6, "rule_fallback_weather_keyword"
    return "ignore", 0.5, "rule_fallback_default"


def _spotify_search_track_uri(query: str) -> tuple[str | None, str | None]:
    token = os.getenv("SPOTIFY_ACCESS_TOKEN", "").strip()
    if not token:
        return None, "spotify_access_token_missing"

    timeout_sec = max(5, _safe_int("MUSIC_INTENT_SPOTIFY_TIMEOUT_SEC", 15))
    url = f"https://api.spotify.com/v1/search?q={quote(query)}&type=track&limit=1"
    req = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        items = (((payload.get("tracks") or {}).get("items")) or [])
        if not items:
            return None, "track_not_found"
        return str(items[0].get("uri", "")).strip() or None, None
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return None, f"spotify_search_http_error:{int(getattr(exc, 'code', 500))}:{detail[:200]}"
    except URLError as exc:
        return None, f"spotify_search_url_error:{exc}"
    except Exception as exc:
        return None, f"spotify_search_error:{exc}"


def _spotify_add_to_queue(track_uri: str) -> str | None:
    token = os.getenv("SPOTIFY_ACCESS_TOKEN", "").strip()
    if not token:
        return "spotify_access_token_missing"

    timeout_sec = max(5, _safe_int("MUSIC_INTENT_SPOTIFY_TIMEOUT_SEC", 15))
    device_id = os.getenv("SPOTIFY_DEVICE_ID", "").strip()
    url = f"https://api.spotify.com/v1/me/player/queue?uri={quote(track_uri)}"
    if device_id:
        url += f"&device_id={quote(device_id)}"
    req = Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout_sec):
            pass
        return None
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return f"spotify_queue_http_error:{int(getattr(exc, 'code', 500))}:{detail[:200]}"
    except URLError as exc:
        return f"spotify_queue_url_error:{exc}"
    except Exception as exc:
        return f"spotify_queue_error:{exc}"


def _process_transcript(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", "")).strip()
    if not text:
        return {"status": "error", "code": "invalid_text"}

    started = time.time()
    intent, confidence, reason = _call_ollama_intent(text)
    result: dict[str, Any] = {
        "status": "ok",
        "intent": intent,
        "confidence": confidence,
        "reason": reason,
        "text": text,
        "guild_id": int(payload.get("guild_id", 0) or 0),
        "channel_id": int(payload.get("channel_id", 0) or 0),
        "user_id": int(payload.get("user_id", 0) or 0),
        "received_at": _now_iso(),
    }

    if intent == "add_to_jam":
        track_uri, err = _spotify_search_track_uri(text)
        if err is not None:
            result.update({"action": "noop", "detail": err})
        elif track_uri is None:
            result.update({"action": "noop", "detail": "track_uri_missing"})
        else:
            queue_err = _spotify_add_to_queue(track_uri)
            if queue_err is not None:
                result.update({"action": "noop", "detail": queue_err, "track_uri": track_uri})
            else:
                result.update({"action": "spotify_queue_add", "track_uri": track_uri})
    elif intent == "weather_recommend":
        result.update({"action": "weather_recommend_todo"})
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
        return bool(sent and expected and sent == expected)

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
    VoiceSttHandler.shared_token = os.getenv("VOICE_STT_SHARED_TOKEN", "change_me").strip() or "change_me"

    server = build_http_server(host=host, port=port)
    logger.info("Voice STT Agent started at %s:%s", host, port)
    server.serve_forever()


def build_http_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), VoiceSttHandler)


def start_http_server_in_thread(host: str, port: int) -> ThreadingHTTPServer:
    server = build_http_server(host=host, port=port)
    t = threading.Thread(target=server.serve_forever, name="voice-stt-http", daemon=True)
    t.start()
    logger.info("Voice STT HTTP endpoint started at %s:%s", host, port)
    return server


if __name__ == "__main__":
    main()
