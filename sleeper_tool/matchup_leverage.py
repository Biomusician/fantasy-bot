"""Matchup Leverage — my projected this-week lineup against my actual
this-week opponent's, both from the shared optimizer with the current
week's byes and game-day outs applied (the same current-week semantics
Move Impact and the insurance feature already use; nothing structural).

The opponent is whoever shares my matchup_id in Sleeper's cached
matchups for the week. No matchup row (a bye week in the fantasy
playoffs, an unsynced week, the off-season) means no leverage — never a
guess.

  gap = my projected points - opponent's projected points
  Strong Edge      gap >= STRONG_EDGE_MIN
  Modest Edge      MODEST_EDGE_MIN <= gap < STRONG_EDGE_MIN
  Near Even        -MODEST_EDGE_MIN < gap < MODEST_EDGE_MIN
  Modest Deficit   -STRONG_EDGE_MIN < gap <= -MODEST_EDGE_MIN
  Large Deficit    gap <= -STRONG_EDGE_MIN

Annotation only: recommendations gain a clause relating their projected
gain to the gap. No valuation, acceptance or priority changes.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup
from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.valuation import games_remaining

STRONG_EDGE_MIN = 12.0
MODEST_EDGE_MIN = 4.0

STRONG_EDGE = "Strong Edge"
MODEST_EDGE = "Modest Edge"
NEAR_EVEN = "Near Even"
MODEST_DEFICIT = "Modest Deficit"
LARGE_DEFICIT = "Large Deficit"


@dataclass
class MatchupLeverage:
    week: int
    opponent_roster_id: int
    opponent_name: str
    my_points: float
    opponent_points: float
    gap: float
    label: str
    my_lineup: LineupResult
    opponent_lineup: LineupResult

    def describe(self) -> str:
        return (
            f"vs {self.opponent_name} (week {self.week}): {self.label} — you project {self.my_points:.1f}, "
            f"they project {self.opponent_points:.1f} ({self.gap:+.1f})"
        )

    def effect_clause(self, weekly_delta: float) -> str:
        """How a move's projected weekly gain relates to this week's gap."""
        if self.gap <= -MODEST_EDGE_MIN:
            standing = f"current matchup deficit is {-self.gap:.1f}"
        elif self.gap >= MODEST_EDGE_MIN:
            standing = f"current matchup edge is {self.gap:.1f}"
        else:
            standing = f"the matchup is near even ({self.gap:+.1f})"
        return f"Adds {weekly_delta:+.1f} projected points per week; {standing}."


def gap_label(gap: float) -> str:
    if gap >= STRONG_EDGE_MIN:
        return STRONG_EDGE
    if gap >= MODEST_EDGE_MIN:
        return MODEST_EDGE
    if gap <= -STRONG_EDGE_MIN:
        return LARGE_DEFICIT
    if gap <= -MODEST_EDGE_MIN:
        return MODEST_DEFICIT
    return NEAR_EVEN


def find_opponent_roster_id(matchups: list[dict], my_roster_id: int) -> int | None:
    mine = next((m for m in matchups if m.get("roster_id") == my_roster_id), None)
    if mine is None or mine.get("matchup_id") is None:
        return None
    for m in matchups:
        if m.get("matchup_id") == mine["matchup_id"] and m.get("roster_id") != my_roster_id:
            return m.get("roster_id")
    return None


def build_matchup_leverage(
    my_roster: ValuedRoster, rosters: dict[int, ValuedRoster], matchups: list[dict], *, current_week: int | None
) -> MatchupLeverage | None:
    if not current_week or not matchups:
        return None
    opp_id = find_opponent_roster_id(matchups, my_roster.roster_id)
    opponent = rosters.get(opp_id) if opp_id is not None else None
    if opponent is None or not opponent.entries or not my_roster.entries:
        return None
    mine = optimize_lineup(my_roster, nfl_week=current_week, exclude_game_day_out=True)
    theirs = optimize_lineup(opponent, nfl_week=current_week, exclude_game_day_out=True)
    # Optimizer totals are rest-of-season projections; the matchup is one week.
    per_week = games_remaining(current_week)
    my_points = round(mine.total_projected_points / per_week, 1)
    their_points = round(theirs.total_projected_points / per_week, 1)
    gap = round(my_points - their_points, 1)
    return MatchupLeverage(
        week=current_week, opponent_roster_id=opponent.roster_id,
        opponent_name=opponent.team_name or opponent.owner_username or f"roster {opponent.roster_id}",
        my_points=my_points, opponent_points=their_points,
        gap=gap, label=gap_label(gap), my_lineup=mine, opponent_lineup=theirs,
    )
