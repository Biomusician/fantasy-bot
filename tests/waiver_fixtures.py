"""Builders shared by the waiver-decision test files.

Everything is synthetic and network-free. `current_week=17` is used by most
of the waiver tests on purpose: games_remaining(17) == 1, so a projection
IS a per-week projection and every named ratio can be checked against an
exact number instead of a division artefact.
"""
from __future__ import annotations

from conftest import make_entry, make_league_info, make_roster, make_value

from sleeper_tool.replacement_value import NORMAL, PositionMarket, ReplacementMarket
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.valuation import LeagueFormat, derive_league_format

STANDARD = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN")
WEEK = 17  # games_remaining == 1: projections are per-week


def fmt_for(positions: tuple[str | None, ...] = STANDARD, *, ppr: float = 1.0) -> LeagueFormat:
    """A LeagueFormat with the real `starter_slots` demand derived from the
    slot list (waiver_drops.startable_depth reads it)."""
    return derive_league_format({"roster_positions": [p for p in positions], "scoring_settings": {"rec": ppr}})


def player(
    pid: str,
    pos: str | None,
    proj: float | None,
    *,
    name: str | None = None,
    bye_week: int | None = None,
    dynasty_value: int | None = 5000,
    redraft_ecr_rank: int | None = 50,
    **kw,
) -> RosterEntry:
    kw.setdefault("is_starter", False)
    kw.setdefault("years_exp", 6)
    kw.setdefault("age", 27.0)
    return make_entry(
        player_id=pid, name=name or pid, position=pos,
        value=make_value(
            name=name or pid, position=pos, proj_points=proj, bye_week=bye_week,
            dynasty_value=dynasty_value, redraft_ecr_rank=redraft_ecr_rank,
        ),
        **kw,
    )


def roster(
    entries: list[RosterEntry],
    *,
    positions: tuple[str | None, ...] = STANDARD,
    kind: str = "redraft",
    ppr: float = 1.0,
    roster_id: int = 1,
) -> ValuedRoster:
    return make_roster(
        roster_id=roster_id, entries=entries, fmt=fmt_for(positions, ppr=ppr), league=make_league_info(kind=kind),
    )


def market(scarcity: dict[str, str] | None = None, **starter_replacement: float | None) -> ReplacementMarket:
    """A ReplacementMarket carrying only what the waiver modules read:
    each position's starter-replacement level (per week) and its scarcity."""
    scarcity = scarcity or {}
    positions = {
        pos: PositionMarket(
            position=pos, waiver_replacement=None, waiver_replacement_projection=None,
            starter_replacement=None, starter_replacement_projection=level,
            scarcity=scarcity.get(pos, NORMAL), gap=None,
        )
        for pos, level in starter_replacement.items()
    }
    for pos, label in scarcity.items():
        if pos not in positions:
            positions[pos] = PositionMarket(pos, None, None, None, None, label, None)
    return ReplacementMarket(positions=positions, players={})
