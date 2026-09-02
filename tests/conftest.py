"""Shared test builders. These construct minimal-but-valid instances of the
dataclasses used across the valuation/trade-matching logic, so individual
tests can focus on the one field that matters for what they're checking
instead of re-deriving a whole player/roster every time.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.config import LeagueInfo
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.valuation import LeagueFormat, PlayerValue


def make_league_info(*, kind: str = "dynasty", name: str = "Test League", league_id: str = "1") -> LeagueInfo:
    return LeagueInfo(
        name=name,
        league_id=league_id,
        kind=kind,
        sleeper_type={"dynasty": 2, "keeper": 1, "redraft": 0}[kind],
        my_team_name="My Team",
    )


def make_format(
    *,
    qb_format: str = "1QB",
    ppr: float = 1.0,
    te_premium_bonus: float = 0.0,
    rush_100_bonus: float = 0.0,
    pass_td_pts: float = 4.0,
    roster_positions: tuple[str, ...] = (),
) -> LeagueFormat:
    return LeagueFormat(
        qb_format=qb_format,
        ppr=ppr,
        te_premium_bonus=te_premium_bonus,
        rush_100_bonus=rush_100_bonus,
        pass_td_pts=pass_td_pts,
        roster_positions=roster_positions,
    )


def make_value(
    *,
    name: str = "Test Player",
    position: str | None = "WR",
    dynasty_value: int | None = 5000,
    dynasty_rank: int | None = 50,
    dynasty_positional_rank: int | None = 10,
    dynasty_ecr_rank: int | None = 50,
    redraft_ecr_rank: int | None = 50,
    proj_points: float | None = 200.0,
    sources: list[str] | None = None,
    dynasty_value_percentile: float | None = 80.0,
    dynasty_ecr_percentile: float | None = 80.0,
    redraft_ecr_percentile: float | None = 80.0,
    dynasty_positional_percentile: float | None = None,
    cross_source_agreement: str = "agree",
    trend: str | None = "no change",
    te_premium_caveat: str | None = None,
    bye_week: int | None = None,
) -> PlayerValue:
    return PlayerValue(
        player_name=name,
        position=position,
        dynasty_value=dynasty_value,
        dynasty_rank=dynasty_rank,
        dynasty_positional_rank=dynasty_positional_rank,
        dynasty_ecr_rank=dynasty_ecr_rank,
        redraft_ecr_rank=redraft_ecr_rank,
        proj_points=proj_points,
        ff_dynasty_rank=None,
        sources_used=sources if sources is not None else ["ktc", "fantasypros_dynasty"],
        dynasty_value_percentile=dynasty_value_percentile,
        dynasty_ecr_percentile=dynasty_ecr_percentile,
        redraft_ecr_percentile=redraft_ecr_percentile,
        dynasty_positional_percentile=dynasty_positional_percentile,
        cross_source_agreement=cross_source_agreement,
        trend=trend,
        te_premium_caveat=te_premium_caveat,
        bye_week=bye_week,
    )


def make_entry(
    *,
    player_id: str = "p1",
    name: str = "Test Player",
    position: str | None = "WR",
    team: str | None = "KC",
    age: float | None = 25.0,
    is_starter: bool = True,
    is_taxi: bool = False,
    is_reserve: bool = False,
    value: PlayerValue | None = None,
    injury_status: str | None = None,
    status: str | None = "Active",
) -> RosterEntry:
    return RosterEntry(
        player_id=player_id,
        name=name,
        position=position,
        team=team,
        age=age,
        years_exp=3,
        injury_status=injury_status,
        status=status,
        is_starter=is_starter,
        is_taxi=is_taxi,
        is_reserve=is_reserve,
        value=value if value is not None else make_value(name=name, position=position),
    )


def make_roster(
    *,
    roster_id: int = 1,
    owner_id: str | None = "owner1",
    owner_username: str | None = "owner1",
    team_name: str | None = "Team One",
    league: LeagueInfo | None = None,
    fmt: LeagueFormat | None = None,
    entries: list[RosterEntry] | None = None,
    wins: int = 0,
    losses: int = 0,
    ties: int = 0,
) -> ValuedRoster:
    return ValuedRoster(
        league=league if league is not None else make_league_info(),
        roster_id=roster_id,
        owner_id=owner_id,
        owner_username=owner_username,
        team_name=team_name,
        fmt=fmt if fmt is not None else make_format(),
        entries=entries if entries is not None else [],
        wins=wins,
        losses=losses,
        ties=ties,
    )
