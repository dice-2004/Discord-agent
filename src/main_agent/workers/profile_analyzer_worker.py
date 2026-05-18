from __future__ import annotations

import asyncio
import logging
import os

from main_agent.agents.profile_analyzer_agent import (
    UserProfileAnalyzerAgent,
    load_user_profile_analyzer_config_from_env,
)
from main_agent.core.memory import ChannelMemoryStore
from main_agent.core.orchestrator import load_orchestrator_config_from_env


logger = logging.getLogger(__name__)


def _safe_int_env(name: str, default_value: int) -> int:
    raw = os.getenv(name, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


async def _wait_for_initial_messages(agent: UserProfileAnalyzerAgent, attempts: int = 6, delay_sec: int = 20) -> dict[str, object]:
    last_summary: dict[str, object] = {}
    for attempt in range(1, max(1, attempts) + 1):
        last_summary = await agent.analyze_once()
        status = str(last_summary.get("status", "") or "")
        if status != "no_messages":
            return last_summary
        logger.info("Profile worker bootstrap waiting for messages: attempt=%s/%s", attempt, attempts)
        await asyncio.sleep(max(5, delay_sec))
    return last_summary


async def main() -> None:
    config = load_user_profile_analyzer_config_from_env()
    if not config.enabled:
        logger.info("Profile analyzer worker disabled")
        return

    orchestrator_config = load_orchestrator_config_from_env()
    top_k = max(1, _safe_int_env("MEMORY_TOP_K", 8))
    memory_store = ChannelMemoryStore(persist_dir=orchestrator_config.chromadb_path, top_k=top_k)
    agent = UserProfileAnalyzerAgent(config, memory_store)

    bootstrap_summary = await _wait_for_initial_messages(agent)
    logger.info(
        "Profile worker bootstrap completed: status=%s sample_message_count=%s",
        bootstrap_summary.get("status", ""),
        bootstrap_summary.get("sample_message_count", 0),
    )

    while True:
        await asyncio.sleep(config.analyze_interval_sec)
        summary = await agent.analyze_once()
        logger.info(
            "Profile worker update completed: status=%s sample_message_count=%s",
            summary.get("status", ""),
            summary.get("sample_message_count", 0),
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(main())
