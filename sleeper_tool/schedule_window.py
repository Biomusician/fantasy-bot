"""Schedule Windows — three horizons over the cached NFL schedule:

  next          the next NEXT_GAMES_WINDOW regular-season weeks
  remaining     the rest of the regular season (through the schedule's
                last regular-season week)
  playoffs      this league's fantasy playoff weeks, derived from the
                Sleeper settings Sleeper reports at runtime
                (playoff_week_start, playoff_teams, playoff_round_type) —
                never a hardcoded 15-17

Per NFL team: how many games it plays in each window and where its byes
fall. That is all the schedule contributes. No strength-of-schedule is
manufactured (nothing here rates opponents), and the schedule only ever
breaks a tie: two players whose values sit within TIEBREAK_MAX_VALUE_GAP
of each other may be ordered by games in the relevant window; further
apart, value decides and the schedule is silent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sleeper_tool.nfl_schedule import Schedule, normalize_team

NEXT_GAMES_WINDOW = 3
TIEBREAK_MAX_VALUE_GAP = 0.10
# Sleeper playoff_round_type: 0 = one week per round, 1 = one week per round
# with a two-week championship, 2 = two weeks per round.
_ROUND_TYPE_ONE_WEEK, _ROUND_TYPE_TWO_WEEK_FINAL, _ROUND_TYPE_TWO_WEEKS = 0, 1, 2


@dataclass
class ScheduleWindows:
    current_week: int
    next_weeks: list[int]
    remaining_weeks: list[int]
    playoff_weeks: list[int] | None  # None when the league settings don't say
    playoff_teams: int | None

    def describe(self) -> str:
        nxt = f"weeks {self.next_weeks[0]}-{self.next_weeks[-1]}" if len(self.next_weeks) > 1 else (f"week {self.next_weeks[0]}" if self.next_weeks else "none")
        rem = f"through week {self.remaining_weeks[-1]}" if self.remaining_weeks else "over"
        if self.playoff_weeks:
            po = f"weeks {self.playoff_weeks[0]}-{self.playoff_weeks[-1]}" if len(self.playoff_weeks) > 1 else f"week {self.playoff_weeks[0]}"
            po += f" ({self.playoff_teams} teams)" if self.playoff_teams else ""
        else:
            po = "not set in league settings"
        return f"next {NEXT_GAMES_WINDOW} (from this week): {nxt} · regular season {rem} · fantasy playoffs {po}"


@dataclass
class TeamWindow:
    team: str
    next_games: int
    next_byes: list[int]
    remaining_games: int
    remaining_byes: list[int]
    playoff_games: int | None
    playoff_byes: list[int] | None

    def note(self) -> str | None:
        """Only the notable facts: a bye inside the next window or inside
        the fantasy playoffs. A full slate says nothing."""
        bits = []
        if self.next_byes:
            bits.append(f"bye week {', '.join(map(str, self.next_byes))} inside the next {NEXT_GAMES_WINDOW} (this week included)")
        if self.playoff_byes:
            bits.append(f"bye in the fantasy playoffs (week {', '.join(map(str, self.playoff_byes))})")
        return "; ".join(bits) if bits else None


def playoff_weeks(settings: dict, schedule: Schedule | None) -> list[int] | None:
    start = settings.get("playoff_week_start")
    teams = settings.get("playoff_teams")
    try:
        start, teams = int(start), int(teams)
    except (TypeError, ValueError):
        return None
    if start <= 0 or teams <= 1:
        return None
    rounds = math.ceil(math.log2(teams))
    round_type = settings.get("playoff_round_type") or _ROUND_TYPE_ONE_WEEK
    if round_type == _ROUND_TYPE_TWO_WEEKS:
        weeks = rounds * 2
    elif round_type == _ROUND_TYPE_TWO_WEEK_FINAL:
        weeks = rounds + 1
    else:
        weeks = rounds
    out = list(range(start, start + weeks))
    last = schedule.last_regular_week() if schedule is not None else None
    if last is not None:
        out = [w for w in out if w <= last]
    return out or None


def build_windows(schedule: Schedule | None, settings: dict, current_week: int | None) -> ScheduleWindows | None:
    if schedule is None or not current_week:
        return None
    weeks = schedule.regular_weeks()
    remaining = [w for w in weeks if w >= current_week]
    return ScheduleWindows(
        current_week=current_week,
        next_weeks=remaining[:NEXT_GAMES_WINDOW],
        remaining_weeks=remaining,
        playoff_weeks=playoff_weeks(settings, schedule),
        playoff_teams=settings.get("playoff_teams"),
    )


def team_window(schedule: Schedule, team: str | None, windows: ScheduleWindows) -> TeamWindow | None:
    code = normalize_team(team)
    if code is None or code not in schedule.teams():
        return None

    def games_and_byes(weeks):
        byes = [w for w in weeks if schedule.is_bye(code, w)]
        return len(weeks) - len(byes), byes

    next_games, next_byes = games_and_byes(windows.next_weeks)
    rem_games, rem_byes = games_and_byes(windows.remaining_weeks)
    po_games, po_byes = games_and_byes(windows.playoff_weeks) if windows.playoff_weeks else (None, None)
    return TeamWindow(code, next_games, next_byes, rem_games, rem_byes, po_games, po_byes)


def schedule_tiebreak(
    a_name: str, a_team: str | None, a_value: float | None,
    b_name: str, b_team: str | None, b_value: float | None,
    schedule: Schedule, windows: ScheduleWindows, *, horizon: str = "next",
) -> str | None:
    """A one-line schedule preference between two near-equal players, or
    None when value already separates them (gap > TIEBREAK_MAX_VALUE_GAP)
    or the schedule doesn't."""
    if a_value is None or b_value is None or max(a_value, b_value) <= 0:
        return None
    if abs(a_value - b_value) / max(a_value, b_value) > TIEBREAK_MAX_VALUE_GAP:
        return None
    wa, wb = team_window(schedule, a_team, windows), team_window(schedule, b_team, windows)
    if wa is None or wb is None:
        return None
    if horizon == "playoffs":
        if wa.playoff_games is None or wb.playoff_games is None or wa.playoff_games == wb.playoff_games:
            return None
        label, ga, gb, total = "fantasy playoff weeks", wa.playoff_games, wb.playoff_games, len(windows.playoff_weeks or [])
        label = "fantasy playoff weeks"
    else:
        if wa.next_games == wb.next_games:
            return None
        label, ga, gb, total = "upcoming weeks", wa.next_games, wb.next_games, len(windows.next_weeks)
    lead, trail = (a_name, b_name) if ga > gb else (b_name, a_name)
    lead_g, trail_g = max(ga, gb), min(ga, gb)
    return f"schedule tiebreak (values within {TIEBREAK_MAX_VALUE_GAP:.0%}): {lead} plays {lead_g} of the {total} {label}, {trail} plays {trail_g}"
