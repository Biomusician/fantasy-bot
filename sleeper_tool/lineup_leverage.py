"""Lineup Leverage — where a roster's value actually turns into weekly
points, and where it doesn't.

Two outputs, both computed against the shared lineup optimizer's best
legal lineup rather than whatever lineup happens to be set on Sleeper:

  Start/sit decisions — for every starting slot, the best benched player
  who could legally take it, labelled by how close the call is:
    Toss-Up     projections within TOSS_UP_RATIO of each other — matchup
                or roster construction should decide, not the projection
    Lean Start  within LEAN_START_RATIO
    Clear Start everything else (or no eligible alternative at all)
  Bench surplus — "expensive bench points": a benched player projecting at
  least BENCH_SURPLUS_RATIO of the lowest-projected starter at a slot he's
  eligible for. Value trapped behind a slightly better starter is the raw
  material for a depth-for-starter trade, so the highest-VALUE surplus
  players (not the highest-projected) are surfaced, capped at
  MAX_SURPLUS_LISTED.

No decimal confidence scores — the labels are the whole output, on
purpose. Slots where neither player has a projection (K/DEF here) are
skipped rather than labelled off a 0-vs-0 comparison.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, projection_of, slot_eligibility
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.trade_engine import percentile_for_currency, value_currency
from sleeper_tool.valuation import games_remaining

TOSS_UP_RATIO = 0.05  # abs(a - b) / max(a, b) at or under this is a Toss-Up
LEAN_START_RATIO = 0.15  # ... under this is a Lean Start; above is a Clear Start
BENCH_SURPLUS_RATIO = 0.90  # bench projection / lowest eligible starter projection
MAX_SURPLUS_LISTED = 3

CLEAR_START = "Clear Start"
LEAN_START = "Lean Start"
TOSS_UP = "Toss-Up"


@dataclass
class StartSitDecision:
    slot: str
    starter: RosterEntry
    starter_projection: float
    alternative: RosterEntry | None
    alternative_projection: float
    label: str
    gap_ratio: float | None  # (starter - alternative) / starter; None when there's no alternative


@dataclass
class BenchSurplus:
    entry: RosterEntry
    projection: float
    displaced_slot: str
    displaced_starter: RosterEntry
    displaced_projection: float
    ratio: float  # projection / displaced starter projection
    value_percentile: float | None


@dataclass
class LineupLeverage:
    lineup: LineupResult
    decisions: list[StartSitDecision]  # every projected slot, in slot order
    bench_surplus: list[BenchSurplus]  # highest value first, capped
    weekly_starter_points: float
    games_left: int  # divisor renderers use to show any rest-of-season projection per week

    @property
    def close_calls(self) -> list[StartSitDecision]:
        return [d for d in self.decisions if d.label != CLEAR_START]


def decision_label(starter_projection: float, alternative_projection: float) -> str:
    top = max(starter_projection, alternative_projection)
    if top <= 0:
        return CLEAR_START
    gap = abs(starter_projection - alternative_projection) / top
    if gap <= TOSS_UP_RATIO:
        return TOSS_UP
    if gap <= LEAN_START_RATIO:
        return LEAN_START
    return CLEAR_START


def build_lineup_leverage(
    roster: ValuedRoster, *, lineup: LineupResult | None = None, current_week: int | None = None
) -> LineupLeverage | None:
    if not roster.entries:
        return None
    lineup = lineup if lineup is not None else optimize_lineup(roster)
    by_id = {e.player_id: e for e in roster.entries}
    currency = value_currency(roster)
    bench = [by_id[pid] for pid in lineup.bench_player_ids]

    decisions: list[StartSitDecision] = []
    for a in lineup.assignments:
        starter = by_id[a.player_id]
        eligible = slot_eligibility(a.slot)
        alternatives = [e for e in bench if e.position in eligible]
        if a.projection <= 0 and not any(projection_of(e) > 0 for e in alternatives):
            continue  # no projection data on either side — nothing honest to say
        if not alternatives:
            decisions.append(StartSitDecision(a.slot, starter, a.projection, None, 0.0, CLEAR_START, None))
            continue
        best_alt = max(alternatives, key=projection_of)
        alt_proj = projection_of(best_alt)
        gap = (a.projection - alt_proj) / a.projection if a.projection > 0 else None
        decisions.append(
            StartSitDecision(a.slot, starter, a.projection, best_alt, alt_proj, decision_label(a.projection, alt_proj), gap)
        )

    surplus: list[BenchSurplus] = []
    for e in bench:
        proj = projection_of(e)
        if proj <= 0:
            continue
        eligible_slots = [a for a in lineup.assignments if e.position in slot_eligibility(a.slot)]
        if not eligible_slots:
            continue
        weakest = min(eligible_slots, key=lambda a: a.projection)
        if weakest.projection <= 0:
            continue
        ratio = proj / weakest.projection
        if ratio >= BENCH_SURPLUS_RATIO:
            surplus.append(
                BenchSurplus(
                    entry=e,
                    projection=proj,
                    displaced_slot=weakest.slot,
                    displaced_starter=by_id[weakest.player_id],
                    displaced_projection=weakest.projection,
                    ratio=ratio,
                    value_percentile=percentile_for_currency(e.value, currency),
                )
            )
    surplus.sort(key=lambda s: (-(s.value_percentile or 0), -s.ratio))

    games_left = games_remaining(current_week)
    return LineupLeverage(
        lineup=lineup,
        decisions=decisions,
        bench_surplus=surplus[:MAX_SURPLUS_LISTED],
        weekly_starter_points=lineup.total_projected_points / games_left,
        games_left=games_left,
    )
