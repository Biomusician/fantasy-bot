"""Waiver Mode — whether today's report should lead with waivers, and which
NFL week the claims being decided are for.

Tuesday and Wednesday (in the user's own timezone) are when waiver claims
are submitted and processed, so the report's hierarchy flips to put the
Waiver Command Center first for redraft and keeper leagues. Nothing in the
fantasy logic reads this: a claim plan built on a Friday is the same plan,
it just isn't the first thing on the page.

The claim week is not Sleeper's `current_week`. On a Tuesday Sleeper still
reports the week whose games just finished, while every claim is for the
next one. With the NFL schedule in hand the claim week is the earliest
regular-season week whose last game is today or later; without it, the
week after Sleeper's current week on Tuesday/Wednesday and Sleeper's
current week otherwise.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCAL_TIMEZONE = "America/New_York"
TUESDAY = 1  # datetime.weekday(): Monday is 0
WEDNESDAY = 2
WAIVER_DAYS = (TUESDAY, WEDNESDAY)

HEADER = "Waiver Command Center"
CLAIMS_PENDING = "claims pending"
FINAL_REVIEW = "final waiver review"
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class WaiverMode:
    active: bool
    local_date: dt.date
    weekday: int
    claim_week: int | None

    @property
    def day_name(self) -> str:
        return _DAY_NAMES[self.weekday]

    @property
    def subheader(self) -> str:
        """"Tuesday — claims pending" / "Wednesday — final waiver review".
        A league whose waivers already processed says so itself
        (waiver_review); this is the run-level default."""
        if self.weekday == TUESDAY:
            return f"{self.day_name} — {CLAIMS_PENDING}"
        if self.weekday == WEDNESDAY:
            return f"{self.day_name} — {FINAL_REVIEW}"
        return self.day_name


def local_now(now: dt.datetime, timezone: str = LOCAL_TIMEZONE) -> dt.datetime:
    """`now` in the user's timezone. A machine with no tz database (Windows
    without the tzdata package) falls back to UTC-4, which is Eastern
    Daylight Time — right for the whole regular season except the last few
    weeks after the November switch, where it is an hour early and can only
    move a Tuesday midnight boundary."""
    aware = now if now.tzinfo is not None else now.replace(tzinfo=dt.timezone.utc)
    try:
        return aware.astimezone(ZoneInfo(timezone))
    except ZoneInfoNotFoundError:
        return aware.astimezone(dt.timezone(dt.timedelta(hours=-4)))


def claim_week_for(local_date: dt.date, *, schedule=None, current_week: int | None = None) -> int | None:
    if schedule is not None:
        last_game: dict[int, dt.date] = {}
        for g in getattr(schedule, "games", ()):
            if g.game_type != "REG" or not g.gameday:
                continue
            try:
                day = dt.date.fromisoformat(g.gameday)
            except ValueError:
                continue
            if g.week not in last_game or day > last_game[g.week]:
                last_game[g.week] = day
        upcoming = [w for w, day in last_game.items() if day >= local_date]
        if upcoming:
            return min(upcoming)
        if last_game:
            return None  # the regular season is over
    if current_week is None:
        return None
    return current_week + 1 if local_date.weekday() in WAIVER_DAYS else current_week


def waiver_mode_for(now: dt.datetime, *, schedule=None, current_week: int | None = None) -> WaiverMode:
    local = local_now(now)
    day = local.date()
    return WaiverMode(
        active=day.weekday() in WAIVER_DAYS,
        local_date=day,
        weekday=day.weekday(),
        claim_week=claim_week_for(day, schedule=schedule, current_week=current_week),
    )
