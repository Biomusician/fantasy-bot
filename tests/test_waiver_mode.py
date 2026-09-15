from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfoNotFoundError

import pytest

from sleeper_tool import waiver_mode
from sleeper_tool.nfl_schedule import Game, Schedule
from sleeper_tool.waiver_mode import (
    CLAIMS_PENDING,
    FINAL_REVIEW,
    claim_week_for,
    local_now,
    waiver_mode_for,
)

UTC = dt.timezone.utc
# 2026-09-14 is a Monday; 15 Tuesday, 16 Wednesday, 17 Thursday.
MON, TUE, WED, THU = dt.date(2026, 9, 14), dt.date(2026, 9, 15), dt.date(2026, 9, 16), dt.date(2026, 9, 17)


def _schedule(*games: tuple[int, str, str]) -> Schedule:
    """(week, game_type, gameday) triples."""
    return Schedule(season=2026, games=[Game(2026, w, t, "KC", "BUF", day) for w, t, day in games])


# -- local date / active days --------------------------------------------------------


def test_utc_instant_late_tuesday_night_eastern_is_still_tuesday():
    # 03:30 UTC Wednesday = 23:30 EDT Tuesday.
    mode = waiver_mode_for(dt.datetime(2026, 9, 16, 3, 30, tzinfo=UTC))
    assert mode.local_date == TUE
    assert mode.weekday == 1
    assert mode.active is True
    assert mode.subheader == f"Tuesday · {CLAIMS_PENDING}"


def test_utc_monday_night_that_is_already_tuesday_in_utc_is_monday_locally_and_inactive():
    # 02:00 UTC Tuesday = 22:00 EDT Monday.
    mode = waiver_mode_for(dt.datetime(2026, 9, 15, 2, 0, tzinfo=UTC))
    assert mode.local_date == MON
    assert mode.active is False
    assert mode.subheader == "Monday"


def test_wednesday_is_active_with_final_review_subheader():
    mode = waiver_mode_for(dt.datetime(2026, 9, 16, 15, 0, tzinfo=UTC))
    assert mode.active is True
    assert mode.day_name == "Wednesday"
    assert mode.subheader == f"Wednesday · {FINAL_REVIEW}"


def test_thursday_is_inactive_and_subheader_is_just_the_day():
    mode = waiver_mode_for(dt.datetime(2026, 9, 17, 15, 0, tzinfo=UTC))
    assert mode.active is False
    assert mode.subheader == "Thursday"


def test_naive_datetime_is_read_as_utc():
    naive = local_now(dt.datetime(2026, 9, 16, 3, 30))
    aware = local_now(dt.datetime(2026, 9, 16, 3, 30, tzinfo=UTC))
    assert naive == aware
    assert naive.date() == TUE


def test_missing_tz_database_falls_back_to_utc_minus_four(monkeypatch):
    def _raise(_name):
        raise ZoneInfoNotFoundError("no tzdata")

    monkeypatch.setattr(waiver_mode, "ZoneInfo", _raise)
    out = local_now(dt.datetime(2026, 9, 16, 3, 30, tzinfo=UTC))
    assert out.utcoffset() == dt.timedelta(hours=-4)
    assert out.date() == TUE and out.hour == 23


# -- claim week -----------------------------------------------------------------------


def test_claim_week_with_schedule_is_earliest_week_whose_last_game_is_today_or_later():
    sched = _schedule(
        (2, "REG", "2026-09-17"), (2, "REG", "2026-09-21"),  # week 2 ends Monday 21st
        (3, "REG", "2026-09-24"), (3, "REG", "2026-09-28"),
    )
    assert claim_week_for(TUE, schedule=sched) == 2
    assert claim_week_for(dt.date(2026, 9, 21), schedule=sched) == 2  # the last game day itself still counts
    assert claim_week_for(dt.date(2026, 9, 22), schedule=sched) == 3


def test_claim_week_ignores_preseason_postseason_blank_and_bad_dates():
    sched = _schedule(
        (1, "PRE", "2026-12-31"),  # would win if not filtered
        (2, "REG", None),
        (2, "REG", "not-a-date"),
        (3, "REG", "2026-09-28"),
        (1, "POST", "2027-01-10"),
    )
    assert claim_week_for(TUE, schedule=sched) == 3


def test_season_over_returns_none_even_with_current_week():
    sched = _schedule((18, "REG", "2027-01-03"))
    assert claim_week_for(dt.date(2027, 1, 5), schedule=sched, current_week=18) is None


def test_schedule_with_no_usable_games_falls_back_to_current_week_rule():
    sched = _schedule((1, "PRE", "2026-08-20"))
    assert claim_week_for(TUE, schedule=sched, current_week=2) == 3
    assert claim_week_for(THU, schedule=sched, current_week=2) == 2


@pytest.mark.parametrize("day,expected", [(MON, 2), (TUE, 3), (WED, 3), (THU, 2)])
def test_fallback_without_schedule_is_next_week_only_on_tuesday_and_wednesday(day, expected):
    assert claim_week_for(day, current_week=2) == expected


def test_fallback_without_schedule_or_current_week_is_none():
    assert claim_week_for(TUE) is None


def test_waiver_mode_for_passes_schedule_through():
    sched = _schedule((4, "REG", "2026-10-05"))
    mode = waiver_mode_for(dt.datetime(2026, 9, 16, 3, 30, tzinfo=UTC), schedule=sched, current_week=1)
    assert mode.claim_week == 4
