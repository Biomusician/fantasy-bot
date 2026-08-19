"""Manual entry point for Phase 2 validation: sync all known leagues and
print a summary. Run from the project root:

    .venv/Scripts/python.exe scripts/pull_data.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.client import SleeperClient
from sleeper_tool.config import LEAGUES
from sleeper_tool.console import ensure_utf8_stdout
from sleeper_tool.storage import Storage
from sleeper_tool.sync import sync_leagues

ensure_utf8_stdout()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    client = SleeperClient()
    with Storage() as storage:
        results = sync_leagues(client, storage, LEAGUES, weeks_back=2)

        print("\n=== Sync summary ===")
        for r in results:
            if r.ok:
                print(
                    f"OK   {r.league.name:35s} rosters={r.rosters:2d} users={r.users:2d} "
                    f"weeks={r.weeks_synced}"
                )
            else:
                print(f"FAIL {r.league.name:35s} error={r.error}")

        print(f"\nPlayers cached: {storage.player_count()}")


if __name__ == "__main__":
    main()
