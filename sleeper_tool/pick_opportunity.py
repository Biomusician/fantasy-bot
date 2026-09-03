"""Pick Opportunity Cost — what a dynasty draft pick means to THIS roster,
as opposed to its KTC price. A "fair" trade can casually spend the one
pick that's a rebuilding team's future, or a contender's only realistic
route to replacing an aging position group.

Dynasty leagues, first- and second-round picks only. For each of
QB/RB/WR/TE the position unit is measured from the shared optimized
starting lineup:
  - the team's starter-group average age, vs the league-wide median of
    the same number — and vs the position's own veteran threshold, so a
    31-year-old QB unit (young for QB) is never "aging" just for being
    older than the median
  - the team's positional strength (mean within-position percentile of
    those starters, the same reconciled metric the trade engine's need
    detection uses), ranked in-league; bottom BOTTOM_UNITS is "weak"
A unit is WEAK-AGING when it's both bottom-three AND older than the
league median. A unit with no eligible starters at all counts as
bottom-three strength but can't trigger the age test (no ages to
average) — so missing depth alone can make a 2nd Useful, never a
contender's 1st Strategic.

Pick classification:
  1st  rebuilder                         -> Strategic
  1st  contender/middling, weak-aging unit -> Strategic
  1st  otherwise                         -> Useful
  2nd  any bottom-three unit             -> Useful
  2nd  otherwise                         -> Spendable
  2nd  is never Strategic
The label annotates trade recommendations that would spend the pick. It
is never a prohibition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from sleeper_tool.asset_value import DYNASTY_CURRENCY, value_currency
from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.formatting import ordinal
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup
from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.team_status import REBUILD, avg_percentile, veteran_min_age
from sleeper_tool.draft_picks import pick_key
from sleeper_tool.valuation import CORE_SKILL_POSITIONS

BOTTOM_UNITS = 3
STRATEGIC = "Strategic"
USEFUL = "Useful"
SPENDABLE = "Spendable"
ASSESSED_ROUNDS = (1, 2)


@dataclass
class PositionUnit:
    position: str
    starters: int
    avg_age: float | None
    league_median_age: float | None
    strength: float | None  # mean within-position percentile of the unit's starters
    strength_rank: int  # 1 = strongest in league; teams with no unit rank last
    teams: int

    @property
    def bottom_three(self) -> bool:
        return self.strength_rank > self.teams - BOTTOM_UNITS

    @property
    def weak_aging(self) -> bool:
        """Bottom-three AND older than the league median AND actually old for
        the position (team_status.veteran_min_age: QB 32, RB 27, WR/TE 29).
        The median alone would call a 31-year-old QB unit "aging" — QBs
        aren't — while a 27-year-old RB unit genuinely is."""
        return (
            self.bottom_three
            and self.avg_age is not None
            and self.league_median_age is not None
            and self.avg_age > self.league_median_age
            and self.avg_age >= veteran_min_age(self.position)
        )

    def describe(self) -> str:
        if self.starters == 0:
            return f"{self.position}: no eligible starter"
        age = f"avg age {self.avg_age:.1f} vs league {self.league_median_age:.1f}" if self.avg_age is not None and self.league_median_age is not None else "age n/a"
        return f"{self.position}: {ordinal(self.strength_rank)} of {self.teams} in strength, {age}"


@dataclass
class PickAssessment:
    pick: OwnedPick
    classification: str
    reason: str
    origin: str = ""  # "" for my own pick, else "via <original team>" — two "2026 Late 1st"s are different picks

    @property
    def display_name(self) -> str:
        return f"{self.pick.name} ({self.origin})" if self.origin else self.pick.name


@dataclass
class PickOpportunity:
    units: list[PositionUnit]
    assessments: list[PickAssessment]
    weak_aging_positions: list[str] = field(default_factory=list)

    def assessment_for(self, pick: OwnedPick) -> PickAssessment | None:
        return next((a for a in self.assessments if pick_key(a.pick) == pick_key(pick)), None)

    def classification_for(self, pick: OwnedPick) -> str | None:
        a = self.assessment_for(pick)
        return a.classification if a else None


def _unit_stats(roster: ValuedRoster, lineup: LineupResult, position: str, currency: str) -> tuple[int, float | None, float | None]:
    starters = [e for e in roster.entries if e.position == position and e.player_id in lineup.starter_ids]
    ages = [e.age for e in starters if e.age is not None]
    # Same mean-of-within-position-percentile that team_status ranks
    # roster strength on, so "bottom-three unit" and "weak roster" agree.
    return len(starters), (sum(ages) / len(ages) if ages else None), avg_percentile(starters, currency)


def position_units(
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    *,
    my_lineup: LineupResult | None = None,
    lineups: dict[int, LineupResult] | None = None,
) -> list[PositionUnit]:
    """`lineups` may carry already-optimized structural lineups by roster_id
    (the report builds one map per league); the rest are optimized here.
    `my_lineup` still wins for my own roster."""
    currency = value_currency(my_roster)
    lineups = dict(lineups or {})  # copied: the loop below fills it in
    if my_lineup is not None:
        lineups[my_roster.roster_id] = my_lineup
    units: list[PositionUnit] = []
    for pos in CORE_SKILL_POSITIONS:
        stats: dict[int, tuple[int, float | None, float | None]] = {}
        for rid, r in rosters.items():
            if not r.entries:
                continue
            lineup = lineups.get(rid)
            if lineup is None:
                lineup = lineups[rid] = optimize_lineup(r)
            stats[rid] = _unit_stats(r, lineup, pos, currency)
        ages = [a for _, a, _ in stats.values() if a is not None]
        league_median_age = median(ages) if ages else None
        # Strength ranking: real units by strength desc; unit-less teams last.
        ranked = sorted(stats.items(), key=lambda kv: (kv[1][2] is None, -(kv[1][2] or 0)))
        rank = next(i for i, (rid, _) in enumerate(ranked, start=1) if rid == my_roster.roster_id)
        n, avg_age, strength = stats[my_roster.roster_id]
        units.append(PositionUnit(pos, n, avg_age, league_median_age, strength, rank, len(stats)))
    return units


def assess_picks(
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    my_picks: list[OwnedPick],
    *,
    team_status: str,
    my_lineup: LineupResult | None = None,
    lineups: dict[int, LineupResult] | None = None,
) -> PickOpportunity | None:
    if value_currency(my_roster) != DYNASTY_CURRENCY or not my_picks:
        return None
    units = position_units(my_roster, rosters, my_lineup=my_lineup, lineups=lineups)
    weak_aging = [u.position for u in units if u.weak_aging]
    bottom = [u.position for u in units if u.bottom_three]
    assessments: list[PickAssessment] = []
    for pick in sorted(my_picks, key=lambda p: (p.season, p.round, p.original_roster_id)):
        if pick.round not in ASSESSED_ROUNDS:
            continue
        origin = ""
        if pick.original_roster_id != my_roster.roster_id:
            src = rosters.get(pick.original_roster_id)
            # A roster with no user row (abandoned team, or a users sync that
            # returned nothing) has neither a team name nor a username.
            named = (src.team_name or src.owner_username) if src is not None else None
            origin = "via " + (named or f"roster {pick.original_roster_id}")
        if pick.round == 1:
            if team_status == REBUILD:
                cls, why = STRATEGIC, "a rebuilding roster's first-round picks are its future starters"
            elif weak_aging:
                cls, why = STRATEGIC, f"your {'/'.join(weak_aging)} unit is bottom-three in the league and older than the league median — this pick is the realistic replacement path"
            else:
                cls, why = USEFUL, "no weak-aging position unit to replace; valuable but not load-bearing"
        else:
            if bottom:
                cls, why = USEFUL, f"a bottom-three {'/'.join(bottom)} unit gives a 2nd-round swing real use"
            else:
                cls, why = SPENDABLE, "no bottom-three position unit — market value only"
        assessments.append(PickAssessment(pick, cls, why, origin))
    return PickOpportunity(units=units, assessments=assessments, weak_aging_positions=weak_aging)
