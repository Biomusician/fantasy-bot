import datetime as dt

import pytest

from sleeper_tool import nfl_usage
from sleeper_tool.nfl_usage import (
    SEASON_MAX_AGE,
    STALE_AFTER,
    cached_health,
    load_crosswalk_rows,
    load_usage,
    parse_player_weeks,
    parse_snap_counts,
    parse_team_weeks,
    read_csv_rows,
)
from sleeper_tool.rankings import cache as cache_mod
from usage_fixtures import FIXTURE_SEASON, absent_fetch, fake_fetch, fixture_text


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path)


def _counting(fetch):
    calls = []

    def wrapped(url):
        calls.append(url)
        return fetch(url)

    return wrapped, calls


def test_parse_player_weeks_filters_and_counts_malformed():
    rows = read_csv_rows(fixture_text("stats_player_week.csv").encode("utf-8"), gzipped=False)
    parsed, malformed = parse_player_weeks(rows, FIXTURE_SEASON)
    # 3 bad rows: no week, no player id, no team. The POST row, the 2024
    # row and the defensive lineman are filtered, not malformed.
    assert malformed == 3
    assert {r["gsis_id"] for r in parsed} == {"00-0000001", "00-0000002", "00-0000003", "00-0000004"}
    assert all(r["week"] in (1, 2) for r in parsed)
    assert {r["team"] for r in parsed} == {"KC", "LAR"}  # nflverse "LA" -> Sleeper "LAR"


def test_parse_team_and_snap_rows_filter_to_the_regular_season():
    team_rows, team_bad = parse_team_weeks(read_csv_rows(fixture_text("stats_team_week.csv").encode("utf-8"), gzipped=False), FIXTURE_SEASON)
    assert team_bad == 1 and len(team_rows) == 4
    assert {(r["team"], r["week"]) for r in team_rows} == {("KC", 1), ("KC", 2), ("LAR", 1), ("LAR", 2)}

    snap_rows, snap_bad = parse_snap_counts(read_csv_rows(fixture_text("snap_counts.csv").encode("utf-8"), gzipped=False), FIXTURE_SEASON)
    assert snap_bad == 1  # the row with no pfr id
    assert all(r["week"] in (1, 2) for r in snap_rows)  # the WC round is dropped


def test_load_usage_joins_snaps_and_survives_a_missing_snap_row():
    fetch, calls = _counting(fake_fetch)
    usage = load_usage(FIXTURE_SEASON, fetch=fetch)
    assert usage is not None and usage.latest_week == 2
    assert len(calls) == 5  # player, team, snaps, dynastyprocess, nflverse players

    rise = {r.week: r for r in usage.weeks_for("00-0000001")}
    assert rise[1].snap_pct == 0.75 and rise[2].snaps == 60
    assert rise[2].targets == 10.0 and rise[2].team == "KC"

    no_snaps = usage.weeks_for("00-0000004")[0]
    assert no_snaps.snap_pct is None and no_snaps.snaps is None
    assert no_snaps.targets == 6.0 and no_snaps.played  # opportunity without a snap row still counts

    traded = usage.weeks_for("00-0000003")
    assert [r.team for r in traded] == ["KC", "LAR"]
    assert usage.team_week("LA", 2).targets == 30.0  # normalized on the way in and on lookup


def test_load_usage_uses_the_daily_cache_on_the_second_call():
    fetch, calls = _counting(fake_fetch)
    first = load_usage(FIXTURE_SEASON, fetch=fetch)
    again = load_usage(FIXTURE_SEASON, fetch=fetch)
    assert len(calls) == 5 and again is not None
    assert [r.gsis_id for r in again.player_weeks] == [r.gsis_id for r in first.player_weeks]


def test_a_season_that_does_not_exist_is_cached_as_absent_and_not_re_requested():
    fetch, calls = _counting(absent_fetch)
    assert load_usage(2026, fetch=fetch) is None
    assert len(calls) == 1  # gave up after the player file 404'd
    assert load_usage(2026, fetch=fetch) is None
    assert len(calls) == 1  # the absent marker is honoured for the full max age

    health = cached_health(2026)
    assert health.absent and health.rows == 0 and health.latest_week is None
    assert "no data published yet" in health.describe()


def test_an_expired_absent_marker_is_re_checked():
    fetch, calls = _counting(absent_fetch)
    load_usage(2026, fetch=fetch)
    source = nfl_usage.STATS_PLAYER_SOURCE.format(season=2026)
    stale = cache_mod.load_snapshot(source)
    stale.fetched_at = dt.datetime.now(dt.timezone.utc) - SEASON_MAX_AGE - dt.timedelta(hours=1)
    cache_mod._cache_path(source).write_text(__import__("json").dumps(stale.to_json()), encoding="utf-8")
    load_usage(2026, fetch=fetch)
    assert len(calls) == 2


def test_health_reports_freshness_and_staleness():
    usage = load_usage(FIXTURE_SEASON, fetch=fake_fetch)
    health = usage.health()
    assert health.latest_week == 2 and health.rows == 6 and not health.absent
    usage.fetched_at = dt.datetime.now(dt.timezone.utc) - STALE_AFTER - dt.timedelta(days=1)
    assert usage.health().stale


def test_load_crosswalk_rows_returns_both_id_files():
    ff_rows, nfl_rows = load_crosswalk_rows(fetch=fake_fetch)
    assert {r["sleeper_id"] for r in ff_rows} == {"s1", "s2", "s3", "s4"}  # the row with no sleeper id is dropped
    assert any(r["gsis_id"] == "00-0000001" and r["pfr_id"] == "RiseR00" for r in nfl_rows)


def test_a_dead_source_with_no_cache_is_none_not_an_exception():
    def boom(url):
        raise ConnectionError("offline")

    assert load_usage(FIXTURE_SEASON, fetch=boom) is None
