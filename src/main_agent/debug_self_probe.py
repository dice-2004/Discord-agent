from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _bootstrap_imports() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


def main() -> None:
    _bootstrap_imports()

    from main_agent.main import _run_self_probe_once  # noqa: WPS433

    parser = argparse.ArgumentParser(description="Temporary self-probe runner for the Discord bot")
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--channel-id", type=int, required=True)
    parser.add_argument("--question", type=str, required=True)
    args = parser.parse_args()

    os.environ.setdefault("DEBUG_SELF_PROBE_ENABLED", "true")
    asyncio.run(_run_self_probe_once(args.guild_id, args.channel_id, args.question))


if __name__ == "__main__":
    main()
