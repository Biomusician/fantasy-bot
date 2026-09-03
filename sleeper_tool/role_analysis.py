"""What share of his offense did this player actually get, and over which
games? Pure functions over `nfl_usage.UsageData` — no fetching, no labels,
no judgement. `role_trends.py` turns these numbers into a signal.

Three rules do most of the work here:

* **Only played games count.** A player-week with no row is a bye, an
  inactive, or a week he wasn't on an NFL roster. Treating it as a
  zero-usage game would manufacture a role collapse out of a bye. A window
  of "last 3" therefore means the last 3 games he played, which may span
  five calendar weeks.
* **A share needs a denominator.** Every share is computed per week from
  that week's team totals and averaged over the weeks where the
  denominator was above zero. No team row (the file lags, the team was on
  bye) means no share for that week, not a zero.
* **A traded player's share follows him.** Each week uses the team on that
  week's own row, so a receiver who moves in week 9 is measured against his
  new offense from week 9 on without any special case.

What is deliberately *not* here: route participation. The routes-run data
is not in these files and a snap share is not a route share; calling it one
would be the exact kind of false precision the report avoids. Red-zone
share would need play-by-play and is a future extension.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean
from typing import Sequence

from sleeper_tool.nfl_schedule import Schedule, normalize_team
from sleeper_tool.nfl_usage import PlayerWeek, UsageData

LATEST_WINDOW = 1
SHORT_WINDOW = 2
MEDIUM_WINDOW = 3

BYE = "bye"
DID_NOT_PLAY = "did not play"
UNKNOWN_ABSENCE = "no row"


@dataclass
class RoleWindow:
    """Averages over `games` played games. Counting stats are per game;
    shares are 0-1 fractions, or None when nothing could be measured."""
    games: int
    snap_pct: float | None
    target_share: float | None
    carry_share: float | None
    opportunity_share: float | None
    targets: float | None
    carries: float | None
    air_yards: float | None
    weeks: list[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.games == 0


@dataclass
class RoleWindows:
    gsis_id: str
    latest: RoleWindow
    last2: RoleWindow
    last3: RoleWindow
    season: RoleWindow
    played_weeks: list[int] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    history_available: bool = True

    @property
    def games(self) -> int:
        return self.season.games

    @property
    def traded(self) -> bool:
        return len(self.teams) > 1


@dataclass(frozen=True)
class TeamLeader:
    gsis_id: str
    name: str | None
    position: str | None
    games: int
    opportunity_share: float | None
    targets: float | None
    carries: float | None


def _empty_window() -> RoleWindow:
    return RoleWindow(games=0, snap_pct=None, target_share=None, carry_share=None, opportunity_share=None, targets=None, carries=None, air_yards=None, weeks=[])


def _mean_or_none(values: list[float]) -> float | None:
    return fmean(values) if values else None


def played_rows(usage: UsageData | None, gsis_id: str | None) -> list[PlayerWeek]:
    """The player's played games, oldest first."""
    if usage is None or not gsis_id:
        return []
    return sorted((r for r in usage.weeks_for(gsis_id) if r.played), key=lambda r: r.week)


def window_from_rows(usage: UsageData | None, rows: Sequence[PlayerWeek]) -> RoleWindow:
    if not rows:
        return _empty_window()
    snap_pcts = [r.snap_pct for r in rows if r.snap_pct is not None]
    target_shares: list[float] = []
    carry_shares: list[float] = []
    opportunity_shares: list[float] = []
    for r in rows:
        tw = usage.team_week(r.team, r.week) if usage is not None else None
        if tw is None:
            continue
        if tw.targets > 0:
            target_shares.append(r.targets / tw.targets)
        if tw.carries > 0:
            carry_shares.append(r.carries / tw.carries)
        if tw.opportunities > 0:
            opportunity_shares.append(r.opportunities / tw.opportunities)
    return RoleWindow(
        games=len(rows),
        snap_pct=_mean_or_none([float(p) for p in snap_pcts]),
        target_share=_mean_or_none(target_shares),
        carry_share=_mean_or_none(carry_shares),
        opportunity_share=_mean_or_none(opportunity_shares),
        targets=fmean([r.targets for r in rows]),
        carries=fmean([r.carries for r in rows]),
        air_yards=fmean([r.air_yards for r in rows]),
        weeks=[r.week for r in rows],
    )


def role_window_for_weeks(usage: UsageData | None, gsis_id: str | None, weeks: Sequence[int]) -> RoleWindow:
    """The window over exactly these NFL weeks (played games only). Used for
    an explicit baseline — "everything before the last three games"."""
    wanted = set(weeks)
    return window_from_rows(usage, [r for r in played_rows(usage, gsis_id) if r.week in wanted])


def player_role_windows(usage: UsageData | None, gsis_id: str | None) -> RoleWindows:
    """Latest / last 2 / last 3 / season windows for one player.

    `history_available` is False when there is no usage data at all (no
    season played yet) — distinct from a player who simply hasn't played,
    who gets empty windows with history_available True.
    """
    history_available = usage is not None and bool(usage.latest_week)
    rows = played_rows(usage, gsis_id)
    if not rows:
        empty = _empty_window()
        return RoleWindows(
            gsis_id=gsis_id or "",
            latest=empty,
            last2=_empty_window(),
            last3=_empty_window(),
            season=_empty_window(),
            played_weeks=[],
            teams=[],
            history_available=history_available,
        )
    return RoleWindows(
        gsis_id=gsis_id or "",
        latest=window_from_rows(usage, rows[-LATEST_WINDOW:]),
        last2=window_from_rows(usage, rows[-SHORT_WINDOW:]),
        last3=window_from_rows(usage, rows[-MEDIUM_WINDOW:]),
        season=window_from_rows(usage, rows),
        played_weeks=[r.week for r in rows],
        teams=sorted({r.team for r in rows}),
        history_available=history_available,
    )


def team_for_week(usage: UsageData | None, gsis_id: str | None, week: int) -> str | None:
    """The team he was on that week — his own row if he played, otherwise
    the most recent earlier team, otherwise the first team he ever shows."""
    rows = played_rows(usage, gsis_id)
    if not rows:
        return None
    for r in rows:
        if r.week == week:
            return r.team
    earlier = [r for r in rows if r.week < week]
    return earlier[-1].team if earlier else rows[0].team


def missing_week_reasons(usage: UsageData | None, gsis_id: str | None, *, schedule: Schedule | None = None) -> list[tuple[int, str]]:
    """Weeks between his first and the season's latest with no played row,
    labelled bye / did not play where the schedule can tell them apart.

    Without a schedule every gap is "no row": a bye and a healthy scratch
    look identical in the stat file, and guessing between them is exactly
    the kind of invention this tool refuses.
    """
    if usage is None or not usage.latest_week:
        return []
    rows = played_rows(usage, gsis_id)
    if not rows:
        return []
    played = {r.week for r in rows}
    out: list[tuple[int, str]] = []
    for week in range(rows[0].week, usage.latest_week + 1):
        if week in played:
            continue
        team = team_for_week(usage, gsis_id, week)
        if schedule is None:
            out.append((week, UNKNOWN_ABSENCE))
        elif schedule.is_bye(team, week):
            out.append((week, BYE))
        else:
            out.append((week, DID_NOT_PLAY))
    return out


def team_opportunity_leaders(
    usage: UsageData | None,
    team: str | None,
    window: int | None = None,
    *,
    position: str | None = None,
    weeks: Sequence[int] | None = None,
) -> list[TeamLeader]:
    """Everyone who took opportunity for this team over a window, best
    first — the other half of the "a teammate took his role" question.

    `weeks` names the exact weeks; otherwise `window` means the team's last
    N played weeks (None = the whole season). Ties break on gsis id so the
    order never depends on input order.
    """
    if usage is None:
        return []
    team = normalize_team(team)
    if not team:
        return []
    if weeks is not None:
        wanted = set(weeks)
    else:
        team_weeks = usage.team_played_weeks(team)
        wanted = set(team_weeks[-window:] if window else team_weeks)

    by_player: dict[str, list[PlayerWeek]] = {}
    for row in usage.team_player_weeks(team):
        if row.week not in wanted or not row.played:
            continue
        if position and (row.position or "").upper() != position.upper():
            continue
        by_player.setdefault(row.gsis_id, []).append(row)

    leaders: list[TeamLeader] = []
    for gsis_id, rows in by_player.items():
        w = window_from_rows(usage, rows)
        leaders.append(TeamLeader(
            gsis_id=gsis_id,
            name=rows[-1].name,
            position=rows[-1].position,
            games=w.games,
            opportunity_share=w.opportunity_share,
            targets=w.targets,
            carries=w.carries,
        ))
    leaders.sort(key=lambda l: (-(l.opportunity_share or 0.0), l.gsis_id))
    return leaders
