"""Combines a Sleeper roster with player metadata and valuation into a
single queryable structure. Both the trade engine and waiver engine build
on this rather than re-joining Sleeper/valuation data themselves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sleeper_tool.config import LeagueInfo
from sleeper_tool.storage import Storage
from sleeper_tool.valuation import LeagueFormat, PlayerValue, ValuationEngine, derive_league_format

logger = logging.getLogger(__name__)

SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}


@dataclass
class RosterEntry:
    player_id: str
    name: str
    position: str | None
    team: str | None
    age: float | None
    years_exp: int | None
    injury_status: str | None
    status: str | None  # Active/Inactive/etc from Sleeper
    is_starter: bool
    is_taxi: bool
    is_reserve: bool
    value: PlayerValue

    @property
    def is_bench(self) -> bool:
        return not (self.is_starter or self.is_taxi or self.is_reserve)


@dataclass
class ValuedRoster:
    league: LeagueInfo
    roster_id: int
    owner_id: str | None
    owner_username: str | None
    team_name: str | None
    fmt: LeagueFormat
    entries: list[RosterEntry]
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0  # season points scored (Sleeper's fpts + fpts_decimal/100) — the default standings tiebreak
    waiver_budget_used: int = 0  # FAAB spent so far this season, from Sleeper roster.settings
    skipped_player_count: int = 0  # roster player_ids not found in the player cache this run (see build_valued_roster)

    def starters(self) -> list[RosterEntry]:
        return [e for e in self.entries if e.is_starter]

    def bench(self) -> list[RosterEntry]:
        return [e for e in self.entries if e.is_bench]

    def by_position(self, position: str) -> list[RosterEntry]:
        return [e for e in self.entries if e.position == position]

    def total_dynasty_value(self, *, starters_only: bool = False) -> int:
        pool = self.starters() if starters_only else self.entries
        return sum(e.value.dynasty_value or 0 for e in pool)

    @property
    def games_played(self) -> int:
        return self.wins + self.losses + self.ties


def player_name(player_data: dict) -> str:
    return player_data.get("full_name") or " ".join(
        filter(None, [player_data.get("first_name"), player_data.get("last_name")])
    ) or "Unknown Player"


def build_valued_roster(
    storage: Storage,
    engine: ValuationEngine,
    league: LeagueInfo,
    roster: dict,
    league_data: dict,
    users_by_id: dict[str, dict],
    all_players: dict[str, dict],
) -> ValuedRoster:
    fmt = derive_league_format(league_data)
    starters = set(p for p in (roster.get("starters") or []) if p and p != "0")
    taxi = set(roster.get("taxi") or [])
    reserve = set(roster.get("reserve") or [])

    entries = []
    skipped_count = 0
    for pid in roster.get("players") or []:
        pdata = all_players.get(pid)
        if pdata is None:
            # Sleeper occasionally references a player_id not present in the
            # daily cache (e.g. a just-added practice-squad player). Skip
            # rather than crash; a re-sync will pick it up. Counted (not
            # just silently dropped) so need/status classification — which
            # runs purely over `entries` — can be understood as "over a
            # roster missing N players" rather than reading as complete.
            skipped_count += 1
            continue
        name = player_name(pdata)
        value = engine.value_player(name, fmt, pdata.get("position"))
        entries.append(
            RosterEntry(
                player_id=pid,
                name=name,
                position=pdata.get("position"),
                team=pdata.get("team"),
                age=pdata.get("age"),
                years_exp=pdata.get("years_exp"),
                injury_status=pdata.get("injury_status"),
                status=pdata.get("status"),
                is_starter=pid in starters,
                is_taxi=pid in taxi,
                is_reserve=pid in reserve,
                value=value,
            )
        )

    if skipped_count:
        logger.warning(
            "%s / %s: %d roster player_id(s) missing from the player cache, skipped",
            league.name, roster.get("roster_id"), skipped_count,
        )

    owner_id = roster.get("owner_id")
    user = users_by_id.get(owner_id, {})
    settings = roster.get("settings") or {}
    return ValuedRoster(
        league=league,
        roster_id=roster["roster_id"],
        owner_id=owner_id,
        owner_username=user.get("display_name"),
        team_name=((user.get("metadata") or {}).get("team_name") or "").strip() or None,
        fmt=fmt,
        entries=entries,
        skipped_player_count=skipped_count,
        waiver_budget_used=settings.get("waiver_budget_used", 0) or 0,
        wins=settings.get("wins", 0) or 0,
        losses=settings.get("losses", 0) or 0,
        ties=settings.get("ties", 0) or 0,
        points_for=(settings.get("fpts", 0) or 0) + (settings.get("fpts_decimal", 0) or 0) / 100,
    )


def build_all_valued_rosters(
    storage: Storage, engine: ValuationEngine, league: LeagueInfo
) -> dict[int, ValuedRoster]:
    league_data = storage.get_league(league.league_id)
    if league_data is None:
        raise ValueError(f"No cached league data for {league.name} — run sync first")
    rosters = storage.get_rosters(league.league_id)
    users = storage.get_league_users(league.league_id)
    users_by_id = {u["user_id"]: u for u in users}
    all_players = storage.get_all_players()

    result = {}
    for roster in rosters:
        vr = build_valued_roster(storage, engine, league, roster, league_data, users_by_id, all_players)
        result[vr.roster_id] = vr
    return result
