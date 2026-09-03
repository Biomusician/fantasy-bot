"""Pulls rosters/users/matchups/transactions for a set of leagues and persists
them locally, so weekly runs don't need to refetch everything from scratch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sleeper_tool.client import SleeperClient, SleeperAPIError
from sleeper_tool.config import LeagueInfo
from sleeper_tool.players_cache import ensure_players_cached
from sleeper_tool.storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class LeagueSyncResult:
    league: LeagueInfo
    ok: bool
    rosters: int = 0
    users: int = 0
    weeks_synced: list[int] = field(default_factory=list)
    error: str | None = None


def current_week(client: SleeperClient) -> int:
    state = client.get_nfl_state()
    week = state.get("display_week") or state.get("week") or 1
    return max(1, int(week))


def sync_league(
    client: SleeperClient,
    storage: Storage,
    league: LeagueInfo,
    *,
    weeks_back: int = 1,
    week_override: int | None = None,
) -> LeagueSyncResult:
    """Pull one league's settings/rosters/users, plus matchups+transactions
    for the current week and `weeks_back - 1` prior weeks (best effort —
    missing weeks, e.g. before the league existed, are skipped, not fatal).
    """
    try:
        league_data = client.get_league(league.league_id)
        if league_data is None:
            return LeagueSyncResult(league, ok=False, error="league not found (404)")
        storage.save_league(league.league_id, league_data)

        rosters = client.get_rosters(league.league_id)
        storage.save_rosters(league.league_id, rosters)

        users = client.get_league_users(league.league_id)
        storage.save_league_users(league.league_id, users)

        traded_picks = client.get_traded_picks(league.league_id)
        storage.save_traded_picks(league.league_id, traded_picks)

        week = week_override if week_override is not None else current_week(client)
        weeks_synced: list[int] = []
        for w in range(max(1, week - weeks_back + 1), week + 1):
            try:
                matchups = client.get_matchups(league.league_id, w)
                storage.save_matchups(league.league_id, w, matchups)
                transactions = client.get_transactions(league.league_id, w)
                storage.save_transactions(league.league_id, w, transactions)
                weeks_synced.append(w)
            except SleeperAPIError as exc:
                logger.warning("Skipping week %d for %s: %s", w, league.name, exc)

        return LeagueSyncResult(
            league, ok=True, rosters=len(rosters), users=len(users), weeks_synced=weeks_synced
        )
    except SleeperAPIError as exc:
        logger.error("Failed syncing %s: %s", league.name, exc)
        return LeagueSyncResult(league, ok=False, error=str(exc))


def save_trending_if_nonempty(storage: Storage, trend_type: str, rows: list[dict]) -> bool:
    """Persist a trending list only if it has rows. save_trending REPLACES
    the table, so a momentary empty response from Sleeper (a blip, a
    throttle, a deploy) would otherwise wipe a signal the waiver engine
    reads and leave nothing until tomorrow's run. Yesterday's list is stale
    but real; an empty one is just missing. Returns whether it saved.
    """
    if not rows:
        logger.warning(
            "Sleeper returned no trending %s players; keeping the previously stored list rather than clearing it",
            trend_type,
        )
        return False
    storage.save_trending(trend_type, rows)
    return True


def sync_leagues(
    client: SleeperClient,
    storage: Storage,
    leagues: list[LeagueInfo],
    *,
    weeks_back: int = 1,
    refresh_players: bool = True,
) -> list[LeagueSyncResult]:
    if refresh_players:
        ensure_players_cached(client, storage)

    for trend_type in ("add", "drop"):
        save_trending_if_nonempty(storage, trend_type, client.get_trending_players(trend_type=trend_type, limit=50))

    week = current_week(client)
    storage.set_meta("current_week", str(week))

    results = []
    for league in leagues:
        logger.info("Syncing %s (%s)...", league.name, league.league_id)
        results.append(sync_league(client, storage, league, weeks_back=weeks_back, week_override=week))
    return results
