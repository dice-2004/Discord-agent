from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import discord

from voice_stt_agent.server import _forward_transcript, _safe_int, start_http_server_in_thread

logger = logging.getLogger(__name__)

def main() -> None:
    logging.basicConfig(
        level=getattr(logging, (os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    host = os.getenv("VOICE_STT_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _safe_int("VOICE_STT_PORT", 8095)
    start_http_server_in_thread(host=host, port=port)

    # Cleaned up duplicate Discord VC bot code. This agent relies purely on HTTP hooks.
    logger.info("Voice STT Agent running in HTTP endpoint only mode.")
    asyncio.get_event_loop().run_forever()

if __name__ == "__main__":
    main()
