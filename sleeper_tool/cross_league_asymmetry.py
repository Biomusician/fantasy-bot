"""Cross-league opportunity asymmetry — where a player I hold in many
leagues is cheap to move in one of them and dear in another.

Portfolio exposure says "you hold X in six leagues". This says which of
those six is the one where selling him costs the least: the league whose
replacement market at his position is Abundant and where his edge over
the best free agent is small, against the league where the market is
Scarce and his edge is large. It reads the replacement contexts
report_data already built (`LeagueReportData.replacement.players`) and
never re-derives value; the output is a fact about the portfolio, never a
sell instruction — the trade engine decides whether a trade exists.

  MIN_LEAGUES               he must be held in at least this many leagues
  CHEAP_EDGE_MAX            per-week edge over the wire at or under this,
                            in an Abundant/Normal market, is "cheap to move"
  DEAR_EDGE_MIN             per-week edge at or over this, in a Scarce/Very
                            Scarce market, is "costly to move"
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.portfolio_exposure import HIGH_EXPOSURE_LEAGUES
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, SCARCE, VERY_SCARCE

MIN_LEAGUES = HIGH_EXPOSURE_LEAGUES
CHEAP_EDGE_MAX = 1.0  # projected weekly points over the best free agent
DEAR_EDGE_MIN = 4.0
MAX_NOTES = 6


@dataclass
class LeagueSide:
    league: str
    scarcity: str
    edge: float  # projection over the best free agent, per week

    def describe(self) -> str:
        return f"{self.league} ({self.scarcity} {'+' if self.edge >= 0 else ''}{self.edge:.1f}/wk over the wire)"


@dataclass
class Asymmetry:
    player_id: str
    name: str
    position: str | None
    leagues_held: int
    cheapest: LeagueSide  # the league where moving him costs least
    dearest: LeagueSide | None  # the league where it costs most, when one qualifies

    def describe(self) -> str:
        head = f"{self.name} ({self.position or '?'}), held in {self.leagues_held} leagues: cheapest to move in {self.cheapest.describe()}"
        if self.dearest is not None:
            return f"{head}; costliest in {self.dearest.describe()}"
        return head


def build_asymmetries(portfolio, leagues) -> list[Asymmetry]:
    """One note per widely-held player with a genuinely cheap league. Sorted
    by leagues held (most first), then name; capped at MAX_NOTES."""
    if portfolio is None:
        return []
    sides: dict[str, list[tuple[str, str | None, LeagueSide]]] = {}
    for ld in leagues:
        if getattr(ld, "error", None) or not getattr(ld, "drafted", False):
            continue
        market = getattr(ld, "replacement", None)
        if market is None:
            continue
        for pid, ctx in market.players.items():
            if ctx.projection_over_waiver is None:
                continue
            side = LeagueSide(ld.league.name, ctx.scarcity, ctx.projection_over_waiver)
            sides.setdefault(pid, []).append((ctx.entry.name, ctx.entry.position, side))

    out: list[Asymmetry] = []
    for pid, rows in sides.items():
        held = portfolio.leagues_holding(pid)
        if held < MIN_LEAGUES:
            continue
        cheap = [s for _, _, s in rows if s.scarcity in (ABUNDANT, NORMAL) and s.edge <= CHEAP_EDGE_MAX]
        if not cheap:
            continue
        cheapest = min(cheap, key=lambda s: (s.edge, s.league))
        dear = [s for _, _, s in rows if s.scarcity in (SCARCE, VERY_SCARCE) and s.edge >= DEAR_EDGE_MIN]
        dearest = max(dear, key=lambda s: (s.edge, s.league)) if dear else None
        name, position, _ = rows[0]
        out.append(Asymmetry(pid, name, position, held, cheapest, dearest))
    out.sort(key=lambda a: (-a.leagues_held, a.name))
    return out[:MAX_NOTES]
