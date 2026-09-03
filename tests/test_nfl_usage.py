import datetime as dt

import pytest

from sleeper_tool import nfl_usage
from sleeper_tool.nfl_usage import (
    CROSSWALK_MAX_AGE,
    SEASON_MAX_AGE,
    STALE_AFTER,
    cached_health,
    load_crosswalk_rows,
    load_usage,
    parse_player_weeks,
    parse_snap_counts,
    parse_team_weeks,
    read_csv_rows,
    usage_from_payloads,
)
from sleeper_tool.rankings import cache as cache_mod
from sleeper_tool.role_analysis import window_from_rows
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
    # row and the defensive lineman are filtered, not malformed — and so is
    # 00-0000007, whose NA/NULL shares and garbage carry count are missing
    # data, not a broken row (see the _MISSING test below).
    assert malformed == 3
    assert {r["gsis_id"] for r in parsed} == {
        "00-0000001", "00-0000002", "00-0000003", "00-0000004", "00-0000007",
    }
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
    assert health.latest_week == 2 and health.rows == 7 and not health.absent
    usage.fetched_at = dt.datetime.now(dt.timezone.utc) - STALE_AFTER - dt.timedelta(days=1)
    assert usage.health().stale


def test_load_crosswalk_rows_returns_both_id_files():
    ff_rows, nfl_rows = load_crosswalk_rows(fetch=fake_fetch)
    assert {r["sleeper_id"] for r in ff_rows} == {"s1", "s2", "s3", "s4"}  # the row with no sleeper id is dropped
    assert any(r["gsis_id"] == "00-0000001" and r["pfr_id"] == "RiseR00" for r in nfl_rows)


def test_missing_shares_stay_none_while_missing_counting_stats_become_zero():
    """`_MISSING` ("", NA, N/A, NULL, NONE) and an unparseable number both
    mean "no value". A SHARE must stay None — a 0.0 target share is a claim
    that the offense threw nothing his way — while a counting stat goes
    through `_num0` to 0.0, which is what "no receiving air yards recorded"
    actually means. Neither is a malformed row."""
    rows = read_csv_rows(fixture_text("stats_player_week.csv").encode("utf-8"), gzipped=False)
    parsed, malformed = parse_player_weeks(rows, FIXTURE_SEASON)
    row = next(r for r in parsed if r["gsis_id"] == "00-0000007")

    assert row["target_share"] is None      # "NA"
    assert row["air_yards_share"] is None   # "N/A"
    assert row["air_yards"] == 0.0          # "NULL" through _num0
    assert row["carries"] == 0.0            # "twelve" is unparseable, not fatal
    assert row["targets"] == 4.0            # the readable columns are untouched
    assert malformed == 3                   # unchanged: an NA is not a broken row

    # ... and the same distinction survives the typed-row build.
    usage = load_usage(FIXTURE_SEASON, fetch=fake_fetch)
    week = usage.weeks_for("00-0000007")[0]
    assert week.target_share is None and week.air_yards_share is None
    assert week.air_yards == 0.0 and week.carries == 0.0
    assert week.played  # four targets is a game played, whatever the shares say


def test_the_documented_cache_ages_are_twenty_four_hours_and_seven_days():
    """A season file goes stale after a day; the identity crosswalks only
    after a week. Exercised at absolute ages either side of SEASON_MAX_AGE,
    not at ages derived from it."""
    assert SEASON_MAX_AGE == dt.timedelta(hours=24)
    assert CROSSWALK_MAX_AGE == dt.timedelta(days=7)
    assert STALE_AFTER == dt.timedelta(days=8)

    fetch, calls = _counting(fake_fetch)
    load_usage(FIXTURE_SEASON, fetch=fetch)
    assert len(calls) == 5
    _age_every_cache_entry(dt.timedelta(hours=25))
    load_usage(FIXTURE_SEASON, fetch=fetch)
    # The three season files re-fetched at 25h; the two crosswalks did not.
    refetched = calls[5:]
    assert len(refetched) == 3
    assert all("players.csv" not in u and "db_playerids" not in u for u in refetched)

    # Past a week, the crosswalks go too.
    _age_every_cache_entry(dt.timedelta(days=7, hours=1))
    load_usage(FIXTURE_SEASON, fetch=fetch)
    assert len(calls[8:]) == 5


def _age_every_cache_entry(age: dt.timedelta) -> None:
    """Backdate every cached snapshot so the next load has to decide on age
    alone. Writes only to the tmp_path CACHE_DIR the autouse fixture set."""
    import json

    for path in cache_mod.CACHE_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fetched_at"] = (dt.datetime.now(dt.timezone.utc) - age).isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.xfail(strict=True, reason="duplicate player-week rows are counted twice")
def test_a_repeated_player_week_counts_once():
    """nflverse has shipped duplicate rows before (a release rebuilt while a
    game was being corrected). Two identical (gsis_id, week) rows are one
    game, not two — `UsageData.__post_init__` appends both to `_by_player`
    with no key on (gsis_id, week), so every played-games count, window
    split and per-game mean downstream sees a phantom extra game."""
    usage = usage_from_payloads(
        FIXTURE_SEASON,
        [_player_row("00-0000001", 1, targets=5.0), _player_row("00-0000001", 1, targets=5.0)],
        [_team_row("KC", 1)],
        [],
        {},
    )
    assert len(usage.weeks_for("00-0000001")) == 1


def test_a_repeated_team_week_does_not_change_the_denominator():
    """A duplicated team-week must not double the offense's opportunities;
    a share computed against 2x the real denominator would halve every
    player. `_by_team_week` is keyed on (team, week), so the lookup every
    share goes through sees one row however many the file shipped."""
    dup = usage_from_payloads(
        FIXTURE_SEASON,
        [_player_row("00-0000001", 1, targets=6.0)],
        [_team_row("KC", 1, targets=30.0), _team_row("KC", 1, targets=30.0)],
        [],
        {},
    )
    assert dup.team_week("KC", 1).targets == 30.0
    once = usage_from_payloads(
        FIXTURE_SEASON, [_player_row("00-0000001", 1, targets=6.0)], [_team_row("KC", 1, targets=30.0)], [], {},
    )
    assert window_from_rows(dup, dup.weeks_for("00-0000001")).target_share == pytest.approx(0.2)
    assert (
        window_from_rows(dup, dup.weeks_for("00-0000001")).target_share
        == window_from_rows(once, once.weeks_for("00-0000001")).target_share
    )


def _player_row(gsis: str, week: int, **kw) -> dict:
    base = {
        "gsis_id": gsis, "week": week, "team": "KC", "position": "WR", "name": gsis,
        "targets": 0.0, "receptions": 0.0, "rec_yards": 0.0, "air_yards": 0.0,
        "carries": 0.0, "rush_yards": 0.0, "pass_attempts": 0.0,
        "target_share": None, "air_yards_share": None,
    }
    base.update(kw)
    return base


def _team_row(team: str, week: int, *, targets: float = 30.0, carries: float = 20.0) -> dict:
    return {"team": team, "week": week, "targets": targets, "carries": carries, "attempts": 30.0}


def test_a_dead_source_with_no_cache_is_none_not_an_exception():
    def boom(url):
        raise ConnectionError("offline")

    assert load_usage(FIXTURE_SEASON, fetch=boom) is None
