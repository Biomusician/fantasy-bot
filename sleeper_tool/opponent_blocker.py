"""Opponent Blocking — at most ONE "Defensive Add" per league per week,
and only when every one of these holds:

  1. this week's opponent is identifiable (matchup_leverage);
  2. that opponent has a real hole this week — an unfilled starting slot,
     or a structural starter their optimized this-week lineup can't use
     (bye, ruled out, long-term status);
  3. the free agent would improve the opponent's optimized this-week
     lineup by at least OPPONENT_GAIN_MIN points;
  4. the player is actually available (the league's free-agent pool);
  5. adding him costs me nothing I value: the drop (if my roster is
     full) reuses the waiver engine's own drop-candidate logic and may
     not be a current optimized starter, a bench-surplus asset, a
     clog-exempt developmental player, or a piece in a live trade
     proposal. No acceptable drop means no Defensive Add.

The point is to deny a specific opponent a specific fix this week, so
the add is judged on THEIR gain; my own gain is reported alongside but
is not required. Pre-draft leagues never reach here (no free-agent pool).
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sleeper_tool.asset_value import value_currency
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, optimize_lineup_after_moves, starter_slots_for
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.trade_engine import identify_needs
from sleeper_tool.valuation import games_remaining
from sleeper_tool.waiver_engine import _find_drop_candidate

OPPONENT_GAIN_MIN = 4.0  # projected points to the opponent's this-week lineup
MAX_CANDIDATES = 12  # free agents tried, best projections first


@dataclass
class DefensiveAdd:
    target: RosterEntry
    opponent_name: str
    opponent_gain: float
    hole: str
    drop: RosterEntry | None  # None when my roster has an open spot
    my_gain: float
    week: int

    def describe(self) -> str:
        drop = f", drop {self.drop.name}" if self.drop else ""
        return (
            f"Add {self.target.name} ({self.target.position or '?'}){drop} — your week-{self.week} opponent {self.opponent_name} "
            f"has {self.hole}; he would add {self.opponent_gain:+.1f} to their lineup this week (and {self.my_gain:+.1f} to yours)"
        )


def opponent_hole(opponent: ValuedRoster, week_lineup: LineupResult, structural_lineup: LineupResult) -> str | None:
    if week_lineup.unfilled_slots:
        slots = ", ".join(week_lineup.unfilled_slots)
        return f"an unfilled {slots} slot this week"
    by_id = {e.player_id: e for e in opponent.entries}
    missing = [
        f"{by_id[pid].name} {week_lineup.unavailable[pid]}"
        for pid in structural_lineup.starter_ids
        if pid in week_lineup.unavailable and pid in by_id
    ]
    if missing:
        return "a starter out this week (" + "; ".join(sorted(missing)) + ")"
    return None


def open_roster_spots(roster: ValuedRoster) -> int:
    active = [e for e in roster.entries if not e.is_reserve and not e.is_taxi]
    return max(0, len(roster.fmt.roster_positions) - len(active))


def roster_is_full(roster: ValuedRoster) -> bool:
    return open_roster_spots(roster) == 0


def find_defensive_add(
    my_roster: ValuedRoster,
    opponent: ValuedRoster,
    free_agents: list[RosterEntry],
    *,
    current_week: int,
    protected_ids: Collection[str],
    clog_ids: Collection[str] = (),
    opponent_week_lineup: LineupResult | None = None,
    my_week_lineup: LineupResult | None = None,
    opponent_structural_lineup: LineupResult | None = None,
) -> DefensiveAdd | None:
    """The three optional lineups are pure caching: the report has already
    solved all of them (the matchup's two this-week lineups and the shared
    structural map), and re-solving them here is the single most expensive
    thing this module does. The `_week_` ones must have been built for THIS
    `current_week` with exclude_game_day_out=True, the structural one with
    plain defaults — pass nothing and they're computed here."""
    if not free_agents or not opponent.entries or not starter_slots_for(opponent):
        return None
    opp_week = opponent_week_lineup if opponent_week_lineup is not None else optimize_lineup(
        opponent, nfl_week=current_week, exclude_game_day_out=True
    )
    opp_structural = opponent_structural_lineup if opponent_structural_lineup is not None else optimize_lineup(opponent)
    hole = opponent_hole(opponent, opp_week, opp_structural)
    if hole is None:
        return None
    per_week = games_remaining(current_week)  # optimizer totals are rest-of-season; the block is about one week

    candidates = sorted(
        (fa for fa in free_agents if fa.value.proj_points is not None),
        key=lambda e: (-e.value.proj_points, e.name),
    )[:MAX_CANDIDATES]
    best: tuple[float, RosterEntry] | None = None
    for fa in candidates:
        after = optimize_lineup_after_moves(opponent, add_entries=[fa], nfl_week=current_week, exclude_game_day_out=True)
        gain = round((after.total_projected_points - opp_week.total_projected_points) / per_week, 1)
        if gain >= OPPONENT_GAIN_MIN and (best is None or gain > best[0]):
            best = (gain, fa)
    if best is None:
        return None
    gain, target = best

    drop: RosterEntry | None = None
    if roster_is_full(my_roster):
        drop = _find_drop_candidate(
            my_roster, target.position, identify_needs(my_roster), value_currency(my_roster),
            exclude_ids=set(protected_ids), preferred_ids=clog_ids,
        )
        if drop is None or drop.player_id in set(protected_ids):
            return None

    my_week = my_week_lineup if my_week_lineup is not None else optimize_lineup(
        my_roster, nfl_week=current_week, exclude_game_day_out=True
    )
    my_after = optimize_lineup_after_moves(
        my_roster, add_entries=[target], remove_player_ids=[drop.player_id] if drop else (),
        nfl_week=current_week, exclude_game_day_out=True,
    )
    return DefensiveAdd(
        target=target,
        opponent_name=opponent.team_name or opponent.owner_username or f"roster {opponent.roster_id}",
        opponent_gain=gain, hole=hole, drop=drop,
        my_gain=round((my_after.total_projected_points - my_week.total_projected_points) / per_week, 1), week=current_week,
    )
