"""Stash Board — dynasty/keeper only: unrostered developmental players
worth a roster spot for what they might become, never for this week.

A candidate must be early-career (years_exp <= STASH_MAX_YEARS_EXP),
young for his position (team_status.young_max_age), and carry a
meaningful dynasty value (pool-wide dynasty percentile). Veteran-age
players, near-zero values and pre-draft leagues are out — pre-draft the
"free agents" are the draft pool. Each candidate is labelled:

  Priority Stash   value >= PRIORITY_MIN_PERCENTILE and a roster spot is
                   available (an open slot, or a roster clog to cut)
  Watch            value >= WATCH_MIN_PERCENTILE, or a priority-value
                   player with no clean spot for him

At most STASH_MAX, best value first. Every line says "developmental hold"
— a stash is not immediate lineup help and is never described as such.
Replacement scarcity at his position is a reason, not a requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.formatting import ordinal
from sleeper_tool.replacement_value import SCARCE, VERY_SCARCE, ReplacementMarket
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.roster_clog import RosterClog
from sleeper_tool.team_status import veteran_min_age, young_max_age
from sleeper_tool.valuation import CORE_SKILL_POSITIONS

STASH_MAX = 5
STASH_MAX_YEARS_EXP = 2
PRIORITY_MIN_PERCENTILE = 60.0
WATCH_MIN_PERCENTILE = 40.0
STASH_KINDS = ("dynasty", "keeper")

PRIORITY_STASH = "Priority Stash"
WATCH = "Watch"


@dataclass
class StashCandidate:
    entry: RosterEntry
    label: str
    percentile: float
    reasons: list[str] = field(default_factory=list)
    drop: RosterEntry | None = None  # the clog to cut for him, when the roster is full

    def describe(self) -> str:
        spot = f"; cut {self.drop.name} for the spot" if self.drop else ""
        return f"{self.entry.name} ({self.entry.position or '?'}, {self.entry.team or '-'}): {'; '.join(self.reasons)}{spot} — developmental hold, not lineup help"


def _is_developmental(entry: RosterEntry) -> bool:
    if entry.position not in CORE_SKILL_POSITIONS:
        return False
    if entry.years_exp is None or entry.years_exp > STASH_MAX_YEARS_EXP:
        return False
    if entry.age is not None and (entry.age > young_max_age(entry.position) or entry.age >= veteran_min_age(entry.position)):
        return False
    return True


def build_stash_board(
    my_roster: ValuedRoster,
    pool: list[RosterEntry],
    *,
    league_kind: str,
    pre_draft: bool,
    open_spots: int,
    clogs: list[RosterClog],
    market: ReplacementMarket | None = None,
) -> list[StashCandidate]:
    if league_kind not in STASH_KINDS or pre_draft or not pool:
        return []
    ranked = sorted(
        (e for e in pool if _is_developmental(e) and (e.value.dynasty_value_percentile or 0) >= WATCH_MIN_PERCENTILE),
        key=lambda e: (-(e.value.dynasty_value_percentile or 0), e.name),
    )
    available_clogs = [c.entry for c in clogs]
    board: list[StashCandidate] = []
    for e in ranked[:STASH_MAX]:
        pctl = e.value.dynasty_value_percentile or 0.0
        reasons = [
            f"{ordinal(round(pctl))} percentile dynasty value",
            f"{'rookie' if not e.years_exp else f'{e.years_exp} season(s) in'}" + (f", age {e.age:.0f}" if e.age is not None else ""),
        ]
        scarcity = market.scarcity_of(e.position) if market is not None else None
        if scarcity in (SCARCE, VERY_SCARCE):
            reasons.append(f"{e.position} replacements are {scarcity} here")
        drop = None
        has_spot = False
        if pctl >= PRIORITY_MIN_PERCENTILE:
            # Each Priority Stash consumes one spot: an open slot first,
            # then a clog to cut. Watches consume nothing.
            if open_spots > 0:
                open_spots -= 1
                has_spot = True
            elif available_clogs:
                drop = available_clogs.pop(0)
                has_spot = True
        if pctl >= PRIORITY_MIN_PERCENTILE and has_spot:
            label = PRIORITY_STASH
        else:
            label = WATCH
            if pctl >= PRIORITY_MIN_PERCENTILE:
                reasons.append("no roster spot without cutting a real player")
        board.append(StashCandidate(e, label, pctl, reasons, drop))
    return board
