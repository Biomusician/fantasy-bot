"""Roster Needs — how badly each position group of MY roster needs help,
read off the optimized lineup rather than raw roster counts.

A roster with five bad WRs is not strong at WR, and a roster with one
elite TE and nobody behind him is not weak at TE until that TE is on bye.
So every label here compares projected points per week — my weakest
starter in the group against this league's own starters — and then asks
whether the week being claimed for opens a hole.

Groups: QB, RB, WR, TE and FLEX. A SUPER_FLEX slot is QB demand (the
same reading valuation.derive_league_format uses), so it belongs to the
QB group: a Superflex roster starting a WR there has a QB problem, not a
flex problem. FLEX / WRRB_FLEX / REC_FLEX form the FLEX group.

Labels, checked in this order (named thresholds below):

  Critical Need  an unfilled starting slot in the group; or my weakest
                 starter projects under CRITICAL_BELOW_REPLACEMENT_RATIO of
                 the league's starter-replacement level; or a starter is
                 unavailable in the claim week and what replaces him
                 projects under CRITICAL_COVER_RATIO of him (or nothing can)
  Weak           my weakest starter projects under WEAK_BELOW_MEDIAN_RATIO
                 of the league's median weakest starter in the group, or
                 under WEAK_BELOW_REPLACEMENT_RATIO of the replacement
                 level; or the claim-week
                 cover for an unavailable starter is under
                 WEAK_COVER_RATIO of him
  Strong         my weakest starter projects at least
                 STRONG_ABOVE_MEDIAN_RATIO of that median AND at least one
                 bench player covers him (DEPTH_COVER_RATIO)
  Surplus        Strong with SURPLUS_MIN_DEPTH or more covering bench players
  Adequate       everything else

"Improvable" is recorded separately: whether any free agent eligible for
the group projects above my weakest starter. A Weak group the wire can't
fix is still Weak — it simply won't produce a claim.

Nothing here fetches, bids, or recommends. Plain values in, labels out.
"""
from __future__ import annotations

import statistics
from collections.abc import Collection
from dataclasses import dataclass, field

from sleeper_tool.lineup_optimizer import LineupResult, SlotAssignment, optimize_lineup, slot_label
from sleeper_tool.replacement_value import ReplacementMarket
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.valuation import games_remaining

CRITICAL_NEED = "Critical Need"
WEAK = "Weak"
ADEQUATE = "Adequate"
STRONG = "Strong"
SURPLUS = "Surplus"
NEED_ORDER = {CRITICAL_NEED: 0, WEAK: 1, ADEQUATE: 2, STRONG: 3, SURPLUS: 4}

CRITICAL_BELOW_REPLACEMENT_RATIO = 0.75
# The replacement level is itself a league starter, so a weakest starter a
# tenth of a point under it is the league's typical last starter, not a weak
# one. Weak begins a real margin below.
WEAK_BELOW_REPLACEMENT_RATIO = 0.92
CRITICAL_COVER_RATIO = 0.50
WEAK_BELOW_MEDIAN_RATIO = 0.85
WEAK_COVER_RATIO = 0.70  # bye_collision.BYE_HOLE_REPLACEMENT_RATIO: the same bar for "a weak fill"
STRONG_ABOVE_MEDIAN_RATIO = 1.10
DEPTH_COVER_RATIO = 0.75  # a bench player projecting at least this share of the weakest starter is real cover
SURPLUS_MIN_DEPTH = 2

QB_GROUP_SLOTS = frozenset({"QB", "SUPER_FLEX"})
FLEX_GROUP_SLOTS = frozenset({"FLEX", "WRRB_FLEX", "REC_FLEX"})
GROUPS = ("QB", "RB", "WR", "TE", "FLEX")
_GROUP_POSITIONS = {"QB": frozenset({"QB"}), "RB": frozenset({"RB"}), "WR": frozenset({"WR"}), "TE": frozenset({"TE"}),
                    "FLEX": frozenset({"RB", "WR", "TE"})}


def group_of_slot(slot: str | None) -> str | None:
    if slot in QB_GROUP_SLOTS:
        return "QB"
    if slot in FLEX_GROUP_SLOTS:
        return "FLEX"
    if slot in ("RB", "WR", "TE"):
        return slot
    return None


@dataclass
class PositionNeed:
    group: str
    label: str
    reasons: list[str] = field(default_factory=list)
    weakest_starter: str | None = None  # name
    weakest_projection: float | None = None  # per week
    league_median_projection: float | None = None  # median across rosters of each roster's weakest starter in the group
    replacement_projection: float | None = None  # league starter-replacement level, per week
    best_free_agent: str | None = None
    best_free_agent_projection: float | None = None
    depth: int = 0  # bench players covering the weakest starter
    claim_week_hole: bool = False
    improvable: bool = False

    def describe(self) -> str:
        lead = f"{self.group}: {self.label}"
        return f"{lead} — {self.reasons[0]}" if self.reasons else lead


@dataclass
class RosterNeeds:
    groups: dict[str, PositionNeed]
    claim_week: int | None = None

    def label_for(self, group: str | None) -> str | None:
        need = self.groups.get(group or "")
        return need.label if need else None

    def for_position(self, position: str | None) -> PositionNeed | None:
        """The dedicated group for a player's own position (QB/RB/WR/TE)."""
        return self.groups.get(position or "")

    def most_urgent(self, groups: Collection[str]) -> PositionNeed | None:
        present = [self.groups[g] for g in groups if g in self.groups]
        return min(present, key=lambda n: NEED_ORDER[n.label]) if present else None


def _groups_in_league(slots: Collection[str]) -> list[str]:
    present = {group_of_slot(s) for s in slots}
    return [g for g in GROUPS if g in present]


def _group_assignments(lineup: LineupResult, group: str) -> list[SlotAssignment]:
    if group == "QB":
        # A non-QB in SUPER_FLEX is the absence of a second QB, not a QB starter.
        return [a for a in lineup.assignments if a.slot in QB_GROUP_SLOTS and a.position == "QB"]
    if group == "FLEX":
        return [a for a in lineup.assignments if a.slot in FLEX_GROUP_SLOTS]
    return [a for a in lineup.assignments if a.slot == group]


def _group_slot_count(slots: Collection[str], group: str) -> int:
    return sum(1 for s in slots if group_of_slot(s) == group)


def _weakest(assignments: list[SlotAssignment]) -> SlotAssignment | None:
    return min(assignments, key=lambda a: (a.projection, a.name)) if assignments else None


def _starter_slots(roster: ValuedRoster) -> list[str]:
    return [s for s in roster.fmt.roster_positions if s and s not in ("BN", "IR", "TAXI")]


def assess_roster_needs(
    my_roster: ValuedRoster,
    *,
    lineup: LineupResult,
    lineups: dict[int, LineupResult] | None = None,
    market: ReplacementMarket | None = None,
    free_agents: Collection[RosterEntry] = (),
    current_week: int | None = None,
    claim_week: int | None = None,
) -> RosterNeeds:
    """`lineup` is my structural lineup; `lineups` every roster's (for the
    league median); `claim_week` the NFL week the claims being decided are
    for — its byes and long-term injuries open holes."""
    per_week = games_remaining(current_week)
    slots = _starter_slots(my_roster)
    by_id = {e.player_id: e for e in my_roster.entries}
    week_lineup = optimize_lineup(my_roster, nfl_week=claim_week) if claim_week is not None else None
    others = [lu for lu in (lineups or {}).values() if lu is not None]

    groups: dict[str, PositionNeed] = {}
    for group in _groups_in_league(slots):
        eligible = _GROUP_POSITIONS[group]
        mine = _group_assignments(lineup, group)
        weakest = _weakest(mine)
        weakest_pw = weakest.projection / per_week if weakest is not None else None

        weakest_by_roster = [w.projection / per_week for lu in others if (w := _weakest(_group_assignments(lu, group))) is not None]
        median = statistics.median(weakest_by_roster) if weakest_by_roster else None
        replacement = _replacement_level(market, group, per_week, others)

        fas = [fa for fa in free_agents if fa.position in eligible and fa.value.proj_points is not None]
        best_fa = max(fas, key=lambda e: (e.value.proj_points, e.name), default=None)
        best_fa_pw = best_fa.value.proj_points / per_week if best_fa is not None else None

        depth = 0
        if weakest is not None and weakest.projection > 0:
            bench = [by_id[pid] for pid in lineup.bench_player_ids if pid in by_id and by_id[pid].position in eligible]
            depth = sum(1 for e in bench if (e.value.proj_points or 0) >= DEPTH_COVER_RATIO * weakest.projection)

        need = PositionNeed(
            group=group, label=ADEQUATE, weakest_starter=weakest.name if weakest is not None else None,
            weakest_projection=weakest_pw, league_median_projection=median, replacement_projection=replacement,
            best_free_agent=best_fa.name if best_fa is not None else None, best_free_agent_projection=best_fa_pw,
            depth=depth,
            improvable=best_fa_pw is not None and (weakest_pw is None or best_fa_pw > weakest_pw),
        )

        unfilled = [s for s in lineup.unfilled_slots if group_of_slot(s) == group]
        missing = _group_slot_count(slots, group) - len(mine)
        cover = _claim_week_cover(lineup, week_lineup, group, by_id) if week_lineup is not None else None

        critical: list[str] = []
        weak: list[str] = []
        if unfilled or missing > 0:
            what = ", ".join(slot_label(s) for s in unfilled) if unfilled else f"{missing} {group} slot(s)"
            critical.append(f"no one to start at {what}" if unfilled else f"no {group} to start in {what}")
        unprojected = weakest is not None and by_id.get(weakest.player_id) is not None and by_id[weakest.player_id].value.proj_points is None
        if weakest_pw is not None and replacement is not None and replacement > 0:
            if weakest_pw < CRITICAL_BELOW_REPLACEMENT_RATIO * replacement:
                # A starter with no projection at all is started because
                # nobody projected is available — the hole is real, but the
                # sentence must not claim he projects zero.
                critical.append(
                    f"weakest starter {weakest.name} has no projection in this league's sources and nobody projected can replace him"
                    if unprojected else
                    f"weakest starter {weakest.name} projects {weakest_pw:.1f}/wk, far under the league's "
                    f"starter-replacement level ({replacement:.1f}/wk)"
                )
            elif weakest_pw < WEAK_BELOW_REPLACEMENT_RATIO * replacement:
                weak.append(f"weakest starter {weakest.name} ({weakest_pw:.1f}/wk) is under the league's starter-replacement level ({replacement:.1f}/wk)")
        if weakest_pw is not None and median is not None and median > 0 and weakest_pw < WEAK_BELOW_MEDIAN_RATIO * median:
            weak.append(f"weakest starter {weakest.name} ({weakest_pw:.1f}/wk) trails the league's typical {group} starter ({median:.1f}/wk)")
        if cover is not None:
            starter, ratio, fill = cover
            need.claim_week_hole = True
            if ratio < CRITICAL_COVER_RATIO:
                critical.append(
                    f"{starter.name} is out for week {claim_week}"
                    + (f" and the best fill, {fill.name}, projects {ratio:.0%} of him" if fill is not None else " and nothing on the roster can fill his slot")
                )
            elif ratio < WEAK_COVER_RATIO:
                weak.append(f"{starter.name} is out for week {claim_week}; the best fill, {fill.name}, projects {ratio:.0%} of him")

        if critical:
            need.label, need.reasons = CRITICAL_NEED, critical + weak
        elif weak:
            need.label, need.reasons = WEAK, weak
        elif weakest_pw is not None and median is not None and weakest_pw >= STRONG_ABOVE_MEDIAN_RATIO * median and depth >= 1:
            need.label = SURPLUS if depth >= SURPLUS_MIN_DEPTH else STRONG
            need.reasons = [
                f"weakest starter {weakest.name} ({weakest_pw:.1f}/wk) is ahead of the league's typical {group} starter "
                f"({median:.1f}/wk), with {depth} bench player{'s' if depth != 1 else ''} who can cover"
            ]
        else:
            need.reasons = [f"weakest starter {weakest.name} projects {weakest_pw:.1f}/wk"] if weakest is not None and weakest_pw is not None else []
        groups[group] = need
    return RosterNeeds(groups=groups, claim_week=claim_week)


def _replacement_level(market: ReplacementMarket | None, group: str, per_week: int, lineups: list[LineupResult]) -> float | None:
    """The league's starter-replacement level for the group. The replacement
    market already computes it for QB/RB/WR/TE; FLEX is the lowest projection
    in any league FLEX slot (a league with no flex slots has none)."""
    if group != "FLEX":
        m = market.positions.get(group) if market is not None else None
        return m.starter_replacement_projection if m is not None else None
    flex = [a.projection for lu in lineups for a in _group_assignments(lu, "FLEX") if a.projection > 0]
    return min(flex) / per_week if flex else None


def _claim_week_cover(
    structural: LineupResult, week: LineupResult, group: str, by_id: dict[str, RosterEntry]
) -> tuple[RosterEntry, float, RosterEntry | None] | None:
    """The worst-covered starter in `group` who is unavailable in the claim
    week: (starter, fill projection / his projection, fill). Entrants are
    paired best-to-best with the displaced starters, as bye_collision does,
    so a cascade is measured by what actually enters the lineup."""
    week_ids = week.starter_ids
    displaced = sorted(
        (a for a in structural.assignments if a.player_id not in week_ids and a.player_id in week.unavailable and a.projection > 0),
        key=lambda a: -a.projection,
    )
    if not displaced:
        return None
    structural_ids = structural.starter_ids
    entrants = sorted((a for a in week.assignments if a.player_id not in structural_ids), key=lambda a: -a.projection)
    worst: tuple[RosterEntry, float, RosterEntry | None] | None = None
    for i, a in enumerate(displaced):
        if group_of_slot(a.slot) != group and not (group == "QB" and a.position == "QB" and a.slot == "SUPER_FLEX"):
            continue
        fill = entrants[i] if i < len(entrants) else None
        ratio = (fill.projection / a.projection) if fill is not None else 0.0
        entry = by_id.get(a.player_id)
        if entry is None:
            continue
        if worst is None or ratio < worst[1]:
            worst = (entry, ratio, by_id.get(fill.player_id) if fill is not None else None)
    return worst
