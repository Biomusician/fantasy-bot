"""Daily-refresh cache for the ~5MB /v1/players/nfl dictionary.

Sleeper explicitly asks callers not to hit this endpoint more than once a
day. We track the last successful pull in the `meta` table and skip
re-fetching if it's still fresh, regardless of how often sync runs.
"""
from __future__ import annotations

import datetime as dt
import logging

from sleeper_tool.client import SleeperClient
from sleeper_tool.storage import Storage

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = dt.timedelta(hours=20)


def ensure_players_cached(
    client: SleeperClient, storage: Storage, *, force: bool = False
) -> dict[str, dict]:
    """Return the full player dict, refreshing the local cache if it's stale."""
    last_updated = storage.players_last_updated()
    is_stale = force or last_updated is None or (
        dt.datetime.now(dt.timezone.utc) - last_updated > REFRESH_INTERVAL
    )

    if not is_stale:
        logger.info("Players cache fresh as of %s (%d players)", last_updated, storage.player_count())
        return storage.get_all_players()

    logger.info("Fetching /v1/players/nfl (~5MB, this takes a few seconds)...")
    players = client.get_all_players()
    if not players:
        if storage.player_count() > 0:
            logger.warning("Players fetch returned empty; keeping stale cache instead of wiping it")
            return storage.get_all_players()
        raise RuntimeError("Players fetch returned no data and no local cache exists")

    storage.save_players(players)
    logger.info("Cached %d players", len(players))
    return players
