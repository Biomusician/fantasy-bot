"""Waiver Alternatives — how many close substitutes a waiver target has on
THIS league's wire right now.

Position scarcity (replacement_value) says how thin a whole position is;
it can't see that a normally abundant WR market holds one breakout nobody
else resembles, or that a Scarce TE market still has three near-identical
streamers. The bid should follow the second question, so it is asked
per player.

A close substitute is another available free agent who
  - can fill the same roster problem (eligible for one of the same
    positions), and
  - projects at least nearly as well per week: no more than
    max(ALT_ABS_POINTS, ALT_REL_SHARE x his projection) below him
    (better players count too — a target with better options on the wire
    is not unique), and
  - when both carry a FantasyPros ROS positional rank, sits within
    ALT_ROS_RANK_BAND ranks of him or ahead of him, so a projection
    artefact alone can't manufacture a substitute.
Role labels are deliberately not part of the test: the role heuristic is
annotation-only until it is redesigned (docs/DECISIONS.md, 2026-09-04).

  Unique Opportunity  0 substitutes
  Few Alternatives    1
  Some Alternatives   2-3
  Many Alternatives   4 or more
"""
from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from sleeper_tool.roster_analysis import RosterEntry
from sleeper_tool.valuation import games_remaining

ALT_ABS_POINTS = 1.0
ALT_REL_SHARE = 0.12
ALT_ROS_RANK_BAND = 8
FEW_MAX = 1
SOME_MAX = 3

UNIQUE = "Unique Opportunity"
FEW = "Few Alternatives"
SOME = "Some Alternatives"
MANY = "Many Alternatives"
DENSITY_ORDER = {UNIQUE: 0, FEW: 1, SOME: 2, MANY: 3}


@dataclass
class AlternativeDensity:
    player_id: str
    label: str
    count: int
    substitutes: list[str] = field(default_factory=list)  # names, best projection first

    def describe(self) -> str:
        if not self.substitutes:
            return f"{self.label}: no comparable free agent on this wire"
        shown = ", ".join(self.substitutes[:3])
        more = f" and {self.count - 3} more" if self.count > 3 else ""
        return f"{self.label}: {shown}{more}"


def density_label(count: int) -> str:
    if count <= 0:
        return UNIQUE
    if count <= FEW_MAX:
        return FEW
    if count <= SOME_MAX:
        return SOME
    return MANY


def alternative_density(
    target: RosterEntry,
    free_agents: Collection[RosterEntry],
    *,
    positions: Collection[str] | None = None,
    current_week: int | None = None,
    ros_pos_rank: Callable[[RosterEntry], int | None] = lambda e: None,
    exclude_ids: Collection[str] = (),
) -> AlternativeDensity:
    """`positions` is the roster problem's eligible positions (defaults to
    the target's own). A target with no projection has no measurable
    substitutes and reads Unique — callers must not treat that as a reason
    to pay up (waiver_acquisition never lets an unprojected player's
    density raise a bid)."""
    per_week = games_remaining(current_week)
    eligible = set(positions) if positions else {target.position}
    excluded = set(exclude_ids) | {target.player_id}
    if target.value.proj_points is None:
        return AlternativeDensity(target.player_id, UNIQUE, 0)
    mine = target.value.proj_points / per_week
    band = max(ALT_ABS_POINTS, ALT_REL_SHARE * mine)
    my_rank = ros_pos_rank(target)
    subs: list[tuple[float, str]] = []
    for fa in free_agents:
        if fa.player_id in excluded or fa.position not in eligible or fa.value.proj_points is None:
            continue
        theirs = fa.value.proj_points / per_week
        if theirs < mine - band:
            continue
        their_rank = ros_pos_rank(fa)
        if my_rank is not None and their_rank is not None and fa.position == target.position and their_rank > my_rank + ALT_ROS_RANK_BAND:
            continue
        subs.append((theirs, fa.name))
    subs.sort(key=lambda s: (-s[0], s[1]))
    return AlternativeDensity(target.player_id, density_label(len(subs)), len(subs), [name for _, name in subs])
