from __future__ import annotations

import asyncio
import logging

from main_agent.agents.twitter_scout_agent import TwitterScoutAgent, load_twitter_scout_config_from_env


logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_twitter_scout_config_from_env()
    if not config.enabled:
        logger.info("Twitter scout worker disabled")
        return

    agent = TwitterScoutAgent(config)
    await agent.start()
    logger.info("Twitter scout worker started")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(main())
