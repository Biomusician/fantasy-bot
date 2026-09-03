"""Playoff Leverage — where each of my teams stands relative to the
playoff cut, so the urgency of a move reflects the calendar and not just
the value math. The same trade means something different in week 3 and
the week before the deadline on a team one win outside the line.

Four labels, from standings alone — no Monte Carlo, no invented playoff
probability:
  Comfortable  at least COMFORTABLE_MARGIN_WINS wins above the cut
  Bubble       within BUBBLE_MARGIN_WINS of the cut, either side
  Long Shot    2+ wins below the cut but not mathematically eliminated
  Out          mathematically eliminated: at least playoff_teams OTHER
               teams already have strictly more wins than this team can
               still reach (wins + remaining games). A possible tie at
               season's end does NOT eliminate — future points-for isn't
               projected, so a tie can't be called either way.
The cut line is the playoff_teams-th team when the league is sorted by
wins, then points-for (Sleeper's default tiebreak). Ties on wins at the
line are resolved by points-for for the label, not for elimination.

Nothing is labelled until MIN_GAMES_FOR_LABEL games have been played —
at 0-0 every team is "at the cut", which is true and useless.

Deadline Window: the league's trade deadline is DEADLINE_WINDOW_WEEKS or
fewer weeks away. When a Bubble / Long Shot team is inside it, the
cross-league action list favours that league's existing trade
recommendations and says so; nothing new is generated and valuations are
untouched.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.roster_analysis import ValuedRoster

COMFORTABLE_MARGIN_WINS = 2
BUBBLE_MARGIN_WINS = 1
MIN_GAMES_FOR_LABEL = 3
DEADLINE_WINDOW_WEEKS = 2  # "within 14 days" of a weekly-cadence deadline

COMFORTABLE = "Comfortable"
BUBBLE = "Bubble"
LONG_SHOT = "Long Shot"
OUT = "Out"
_URGENT_LABELS = frozenset({BUBBLE, LONG_SHOT})


@dataclass
class PlayoffLeverage:
    label: str
    wins: int
    losses: int
    ties: int
    games_remaining: int
    seed: int  # 1 = top of the standings
    playoff_teams: int
    cut_wins: int  # wins held by the last team currently inside the line
    deadline_window: bool
    trade_deadline_week: int | None
    reason: str

    @property
    def urgent(self) -> bool:
        """A team that should act inside the deadline window."""
        return self.deadline_window and self.label in _URGENT_LABELS


def _standings(rosters: dict[int, ValuedRoster]) -> list[ValuedRoster]:
    return sorted(rosters.values(), key=lambda r: (-r.wins, -r.points_for, r.roster_id))


def is_eliminated(target: ValuedRoster, rosters: dict[int, ValuedRoster], *, playoff_teams: int, games_remaining: int) -> bool:
    max_wins = target.wins + games_remaining
    ahead_for_good = sum(1 for r in rosters.values() if r.roster_id != target.roster_id and r.wins > max_wins)
    return ahead_for_good >= playoff_teams


def classify_playoff_leverage(
    target_roster_id: int,
    rosters: dict[int, ValuedRoster],
    *,
    playoff_teams: int | None,
    playoff_week_start: int | None,
    trade_deadline: int | None,
    current_week: int | None,
) -> PlayoffLeverage | None:
    """None when the league's format isn't exposed or too few games have
    been played to say anything."""
    if not playoff_teams or not playoff_week_start or target_roster_id not in rosters:
        return None
    target = rosters[target_roster_id]
    if target.games_played < MIN_GAMES_FOR_LABEL:
        return None
    regular_season_games = playoff_week_start - 1
    games_remaining = max(0, regular_season_games - target.games_played)

    standings = _standings(rosters)
    seed = next(i for i, r in enumerate(standings, start=1) if r.roster_id == target_roster_id)
    cut_index = min(playoff_teams, len(standings)) - 1
    cut_wins = standings[cut_index].wins
    margin = target.wins - cut_wins

    if is_eliminated(target, rosters, playoff_teams=playoff_teams, games_remaining=games_remaining):
        label = OUT
    elif margin >= COMFORTABLE_MARGIN_WINS:
        label = COMFORTABLE
    elif abs(margin) <= BUBBLE_MARGIN_WINS:
        label = BUBBLE
    else:
        label = LONG_SHOT

    # Sleeper reports trade_deadline as an int, but the raw settings dict is
    # untyped and report_data already coerces the same field elsewhere.
    try:
        deadline_week = int(trade_deadline) if trade_deadline is not None else None
    except (TypeError, ValueError):
        deadline_week = None
    deadline_window = (
        deadline_week is not None and current_week is not None and 0 <= deadline_week - current_week <= DEADLINE_WINDOW_WEEKS
    )
    record = f"{target.wins}-{target.losses}" + (f"-{target.ties}" if target.ties else "")
    inside = seed <= playoff_teams
    reason = (
        f"{record}, seed {seed} of {len(standings)} with {playoff_teams} playoff spots; "
        f"{'inside' if inside else 'outside'} the line by {abs(margin)} win{'s' if abs(margin) != 1 else ''}"
        f"{' (points-for tiebreak)' if margin == 0 else ''}, {games_remaining} game{'s' if games_remaining != 1 else ''} left"
    )
    if label == OUT:
        reason += " — cannot catch enough teams even by winning out"
    if deadline_window:
        reason += f"; trade deadline is week {trade_deadline}"
    return PlayoffLeverage(
        label=label, wins=target.wins, losses=target.losses, ties=target.ties, games_remaining=games_remaining,
        seed=seed, playoff_teams=playoff_teams, cut_wins=cut_wins, deadline_window=deadline_window,
        trade_deadline_week=deadline_week, reason=reason,
    )
