"""Bye Collision Planner — looks ahead LOOKAHEAD_WEEKS for weeks where NFL
byes leave a starting slot empty or badly covered, so the fix can be a
cheap waiver move now rather than a scramble that week.

Bye weeks come from the already-cached PlayerValue.bye_week (FantasyPros/
RotoBaller). A missing bye_week means "no known bye in the cached data",
never an inferred one — this module fetches nothing and predicts no
schedule changes.

For each look-ahead week the shared optimizer builds the best legal
lineup with that week's bye players excluded. The players who ENTER the
lineup that week (weren't normal starters) are the replacements; they're
paired best-to-best against the displaced starters. A displaced starter
with no replacement is an unfilled slot; one whose replacement projects
under BYE_HOLE_REPLACEMENT_RATIO of his own projection is a weak fill.
Either is a BYE_HOLE, reported against the displaced starter's slot —
the position that needs cover. Using whole-lineup re-optimization (not
"who backs up this one guy") means a cascade — the FLEX sliding into RB
so a weak WR3 is what actually enters — is measured by what enters.

Only the EARLIEST affected week is reported, with every affected slot in
that week. The current week isn't included: waiver_engine already flags
this week's starters on bye, and the point here is the weeks you can
still plan for.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.lineup_optimizer import DEDICATED_POSITIONS, LineupResult, SlotAssignment, optimize_lineup, slot_label
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.valuation import NFL_REGULAR_SEASON_WEEKS

LOOKAHEAD_WEEKS = 4
BYE_HOLE_REPLACEMENT_RATIO = 0.70
BYE_HOLE = "Bye Hole"


@dataclass
class ByeHole:
    week: int
    slot: str
    normal_starter: RosterEntry
    normal_projection: float
    replacement: RosterEntry | None  # None = the slot can't be legally filled that week
    replacement_projection: float

    @property
    def ratio(self) -> float:
        return self.replacement_projection / self.normal_projection if self.normal_projection else 0.0


@dataclass
class ByeCollision:
    week: int
    holes: list[ByeHole]  # in slot order
    starters_on_bye: list[RosterEntry]  # normal starters unavailable that week
    weeks_scanned: list[int]

    @property
    def label(self) -> str:
        return BYE_HOLE


def _holes_for_week(
    roster: ValuedRoster, baseline: LineupResult, week: int, by_id: dict[str, RosterEntry]
) -> tuple[list[ByeHole], list[RosterEntry]]:
    displaced: list[SlotAssignment] = [a for a in baseline.assignments if by_id[a.player_id].value.bye_week == week]
    if not displaced:
        return [], []
    weekly = optimize_lineup(roster, nfl_week=week)
    baseline_ids = baseline.starter_ids
    entrants = sorted((a for a in weekly.assignments if a.player_id not in baseline_ids), key=lambda a: -a.projection)
    displaced.sort(key=lambda a: -a.projection)  # best replacement covers the best displaced starter
    holes: list[ByeHole] = []
    for i, a in enumerate(displaced):
        if a.projection <= 0:
            continue  # no projection on the normal starter — nothing measurable to lose
        starter = by_id[a.player_id]
        if i >= len(entrants):
            holes.append(ByeHole(week, a.slot, starter, a.projection, None, 0.0))
            continue
        filler = entrants[i]
        if filler.projection < BYE_HOLE_REPLACEMENT_RATIO * a.projection:
            holes.append(ByeHole(week, a.slot, starter, a.projection, by_id[filler.player_id], filler.projection))
    slot_index = {a.player_id: a.slot_index for a in baseline.assignments}
    holes.sort(key=lambda h: slot_index[h.normal_starter.player_id])  # the league's slot order
    return holes, [by_id[a.player_id] for a in displaced]


def describe_bye_collision(plan: ByeCollision) -> str:
    """One alert-sized sentence per affected slot, joined for the report's
    time-sensitive list."""
    bits = []
    for h in plan.holes:
        if h.replacement is None:
            bits.append(f"{slot_label(h.slot)}: {h.normal_starter.name} on bye and no legal fill on the roster")
        else:
            bits.append(
                f"{slot_label(h.slot)}: {h.normal_starter.name} on bye, best fill is {h.replacement.name} at {h.ratio:.0%} of his projection"
            )
    on_bye = len(plan.starters_on_bye)
    lead = f"{on_bye} starter{'s' if on_bye != 1 else ''} on bye"
    return f"{lead} — {'; '.join(bits)} — a cheap waiver add now beats a same-week scramble"


def positions_covering(plan: ByeCollision) -> set[str]:
    """Player positions that would genuinely cover a hole — used to
    annotate waiver targets that double as bye cover. A dedicated slot
    needs its own position; a FLEX/SUPER_FLEX hole needs the DISPLACED
    starter's position (tagging every flex-eligible add as "bye cover" for
    a Superflex QB hole was true but useless)."""
    covering: set[str] = set()
    for h in plan.holes:
        if h.slot in DEDICATED_POSITIONS:
            covering.add(h.slot)
        elif h.normal_starter.position:
            covering.add(h.normal_starter.position)
    return covering


def plan_bye_collisions(
    roster: ValuedRoster, *, current_week: int | None, lineup: LineupResult | None = None
) -> ByeCollision | None:
    """The earliest look-ahead week with a Bye Hole, or None if the bench
    covers every bye in the window (or no bye data exists)."""
    if not roster.entries or current_week is None:
        return None
    baseline = lineup if lineup is not None else optimize_lineup(roster)
    by_id = {e.player_id: e for e in roster.entries}
    weeks = [w for w in range(current_week + 1, current_week + 1 + LOOKAHEAD_WEEKS) if w <= NFL_REGULAR_SEASON_WEEKS]
    for week in weeks:
        holes, on_bye = _holes_for_week(roster, baseline, week, by_id)
        if holes:
            return ByeCollision(week=week, holes=holes, starters_on_bye=on_bye, weeks_scanned=weeks)
    return None
