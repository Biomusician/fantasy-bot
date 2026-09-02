"""Portfolio Exposure — treats every league as one portfolio and counts how
many of my teams ride on the same NFL player, so a move that's rational
inside one league doesn't quietly stack a fourth or sixth copy of a
player whose injury would sink several teams at once.

Descriptive only. Exposure is a risk flag and a tie-breaker that annotates
existing trade/waiver recommendations; it never recommends selling a good
player merely because I own him in many places. Thresholds are league
counts, not probabilities:
  HIGH_EXPOSURE_LEAGUES       — rostered in this many leagues or more
  VERY_HIGH_EXPOSURE_LEAGUES  — rostered in this many or more
  QB_START_EXPOSURE_LEAGUES   — a QB in my optimized starting lineup in
                                this many leagues or more (flagged
                                separately: one QB's bad Sunday hits every
                                lineup he starts in, and QB is the slot
                                with the fewest substitutes)
Sleeper player_ids are global across leagues, so aggregation is by id —
no name matching involved.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sleeper_tool.lineup_optimizer import LineupResult
from sleeper_tool.roster_analysis import ValuedRoster

HIGH_EXPOSURE_LEAGUES = 4
VERY_HIGH_EXPOSURE_LEAGUES = 6
QB_START_EXPOSURE_LEAGUES = 3
MAX_LISTED_PLAYERS = 10

HIGH = "High Exposure"
VERY_HIGH = "Very High Exposure"


@dataclass
class PlayerExposure:
    player_id: str
    name: str
    position: str | None
    team: str | None
    league_names: list[str]
    started_in: list[str]  # leagues where he's in my optimized lineup

    @property
    def count(self) -> int:
        return len(self.league_names)

    @property
    def level(self) -> str | None:
        return exposure_level(self.count)

    @property
    def qb_start_flag(self) -> bool:
        return self.position == "QB" and len(self.started_in) >= QB_START_EXPOSURE_LEAGUES


@dataclass
class PortfolioExposure:
    total_leagues: int
    players: list[PlayerExposure]  # most concentrated first, capped
    counts_by_player_id: dict[str, int] = field(default_factory=dict)  # every rostered player, for annotations
    qb_starts_by_player_id: dict[str, int] = field(default_factory=dict)

    def leagues_holding(self, player_id: str) -> int:
        return self.counts_by_player_id.get(player_id, 0)


def exposure_level(league_count: int) -> str | None:
    if league_count >= VERY_HIGH_EXPOSURE_LEAGUES:
        return VERY_HIGH
    if league_count >= HIGH_EXPOSURE_LEAGUES:
        return HIGH
    return None


def build_portfolio_exposure(
    holdings: Iterable[tuple[str, ValuedRoster, LineupResult | None]],
) -> PortfolioExposure:
    """`holdings`: (league name, my roster in that league, my optimized
    lineup there or None) for every league with a drafted roster.
    """
    by_id: dict[str, PlayerExposure] = {}
    total = 0
    for league_name, roster, lineup in holdings:
        total += 1
        starters = lineup.starter_ids if lineup is not None else frozenset()
        for entry in roster.entries:
            px = by_id.get(entry.player_id)
            if px is None:
                px = PlayerExposure(
                    player_id=entry.player_id, name=entry.name, position=entry.position, team=entry.team,
                    league_names=[], started_in=[],
                )
                by_id[entry.player_id] = px
            px.league_names.append(league_name)
            if entry.player_id in starters:
                px.started_in.append(league_name)

    ranked = sorted(by_id.values(), key=lambda p: (-p.count, -len(p.started_in), p.name))
    # Only players that actually mean something: multi-league holdings, or
    # a flagged QB. A list of 1-league players would be the roster, not a
    # concentration view.
    listed = [p for p in ranked if p.count >= 2 or p.qb_start_flag][:MAX_LISTED_PLAYERS]
    return PortfolioExposure(
        total_leagues=total,
        players=listed,
        counts_by_player_id={pid: p.count for pid, p in by_id.items()},
        qb_starts_by_player_id={pid: len(p.started_in) for pid, p in by_id.items() if p.position == "QB"},
    )


def acquisition_exposure_note(
    exposure: PortfolioExposure, player_id: str, *, position: str | None, compact: bool = False
) -> str | None:
    """The annotation for a recommendation that would ADD this player to
    one more league — only when that addition would cross a threshold
    (the point at which it becomes worth saying), never as routine
    decoration on every row. `compact` gives a lowercase clause for an
    inline, semicolon-joined reason string.
    """
    after = exposure.leagues_holding(player_id) + 1
    level = exposure_level(after)
    crossed_general = level is not None and exposure_level(after - 1) != level
    qb_after = exposure.qb_starts_by_player_id.get(player_id, 0) + 1
    crossed_qb = position == "QB" and qb_after == QB_START_EXPOSURE_LEAGUES
    if not crossed_general and not crossed_qb:
        return None
    bits = []
    if crossed_general:
        bits.append(f"would put him on {after} of your {exposure.total_leagues} rosters ({level})")
    if crossed_qb:
        bits.append(f"would make him your starting QB in {qb_after} leagues")
    if compact:
        return "portfolio exposure: " + "; ".join(bits)
    return "Portfolio exposure: " + "; ".join(bits) + " — one injury hits all of them at once. A tie-breaker, not a veto."
