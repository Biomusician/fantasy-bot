"""Signal health: labels, family rollup, feature suppression, rendering.

Everything here is synthetic. `build_health` reads objects other layers
already loaded, so the fakes below only need the handful of attributes it
actually touches — which is the point of the duck typing.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from sleeper_tool import signal_health as sh
from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.rankings.freshness import MIN_COVERAGE, SOURCE_WINDOWS
from sleeper_tool.storage import Storage

NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)
WINDOWS = (dt.timedelta(hours=20), dt.timedelta(days=3), dt.timedelta(days=7))


@pytest.fixture(autouse=True)
def _clean_fetch_registry(monkeypatch):
    """`cache.last_fetch_outcome` is a process-global that accumulates for
    the life of a run — correct in production (one process, one report) but
    it means an earlier test module's fetch failure is still recorded here.
    Start every test from empty."""
    monkeypatch.setattr(sh.ranking_cache, "last_fetch_outcome", {})


@pytest.fixture(autouse=True)
def _no_ff_csv(monkeypatch):
    """The Dynasty Pass CSV is a real file on the developer's disk; pin it
    to "absent" so these tests describe the code, not the machine."""
    monkeypatch.setattr(sh, "ff_dynasty_status", lambda *a, **k: "not provided (optional)")


def _snapshot(source: str, age: dt.timedelta, rows: int = 500, fallback: bool = False):
    snap = RankingSnapshot(
        source=source, fetched_at=NOW - age, payload=[{"n": i} for i in range(rows)]
    )
    snap.served_from_fallback = fallback
    return snap


@dataclass
class FakeEngine:
    ktc_snapshot: object = None
    fp_snapshots: dict = None
    rb_snapshots: dict = None


@dataclass
class FakeUsage:
    fetched_at: dt.datetime | None = None
    latest_week: int | None = None
    rows: int = 0
    absent: bool = False
    stale: bool = False


def _healthy_engine(**overrides):
    engine = FakeEngine(
        ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(hours=3), rows=500),
        fp_snapshots={"dynasty_1qb": _snapshot("fantasypros_dynasty_1qb", dt.timedelta(hours=3), rows=400)},
        rb_snapshots={"full_ppr": _snapshot("rotoballer_full_ppr", dt.timedelta(hours=3), rows=600)},
    )
    for k, v in overrides.items():
        setattr(engine, k, v)
    return engine


def _populated_storage(tmp_path, age=dt.timedelta(hours=2)):
    storage = Storage(tmp_path / "health.sqlite3")
    storage.save_players({str(i): {"full_name": f"P{i}"} for i in range(6000)})
    storage.save_league("L1", {"name": "L", "season": "2026"})
    storage.save_rosters("L1", [{"roster_id": 1, "owner_id": "u1"}])
    storage.save_league_users("L1", [{"user_id": "u1", "display_name": "u"}])
    storage.save_traded_picks("L1", [])
    storage.save_matchups("L1", 1, [{"roster_id": 1, "matchup_id": 1, "points": 100.0}])
    storage.save_transactions("L1", 1, [{"transaction_id": "t1", "type": "waiver"}])
    storage.save_trending("add", [{"player_id": "1", "count": 10}])
    return storage


# -- label_for boundaries ---------------------------------------------------


@pytest.mark.parametrize(
    "age, expected",
    [
        (dt.timedelta(0), sh.FRESH),
        (dt.timedelta(hours=20), sh.FRESH),  # exactly the fresh window
        (dt.timedelta(hours=20, microseconds=1), sh.USABLE),
        (dt.timedelta(days=3), sh.USABLE),  # exactly the usable window
        (dt.timedelta(days=3, microseconds=1), sh.STALE),
        (dt.timedelta(days=7), sh.STALE),  # exactly the ceiling
        (dt.timedelta(days=7, microseconds=1), sh.UNAVAILABLE),
    ],
)
def test_label_for_at_every_window_boundary(age, expected):
    assert sh.label_for(age, WINDOWS) == expected


def test_no_fetched_at_is_unavailable():
    assert sh.label_for(None, WINDOWS) == sh.UNAVAILABLE


def test_unknown_source_with_no_windows_is_unavailable():
    assert sh.label_for(dt.timedelta(hours=1), None) == sh.UNAVAILABLE


def test_parse_failure_is_unavailable_however_fresh():
    assert sh.label_for(dt.timedelta(0), WINDOWS, parse_ok=False) == sh.UNAVAILABLE


def test_coverage_exactly_at_the_floor_is_not_partial():
    assert sh.label_for(dt.timedelta(hours=1), WINDOWS, coverage=400, floor=400) == sh.FRESH
    assert sh.label_for(dt.timedelta(hours=1), WINDOWS, coverage=399, floor=400) == sh.PARTIAL


def test_coverage_shortfall_downgrades_a_usable_source_too():
    assert sh.label_for(dt.timedelta(days=1), WINDOWS, coverage=10, floor=400) == sh.PARTIAL


def test_a_stale_source_stays_stale_even_when_short():
    # Age is the more serious problem and the one the reader must act on.
    assert sh.label_for(dt.timedelta(days=5), WINDOWS, coverage=10, floor=400) == sh.STALE


def test_a_fallback_served_snapshot_is_never_fresh():
    assert sh.label_for(dt.timedelta(hours=1), WINDOWS, fallback=True) == sh.USABLE
    # ...but it doesn't rescue something already worse.
    assert sh.label_for(dt.timedelta(days=5), WINDOWS, fallback=True) == sh.STALE


# -- build_health with nothing --------------------------------------------


def test_everything_missing_is_unavailable_and_degraded():
    report = sh.build_health(now=NOW)

    assert report.degraded is True
    assert all(s.label == sh.UNAVAILABLE for s in report.signals)
    assert report.unavailable_families == {
        "ktc",
        "fantasypros",
        "rotoballer",
        "nflverse_schedule",
        "nflverse_usage",
        "sleeper_players",
        "sleeper_league",
        "sleeper_weekly",
        "ff_dynasty_pass",
    }
    assert all(s.detail for s in report.signals)


def test_everything_missing_suppresses_every_feature():
    suppressed = sh.suppressed_features(sh.build_health(now=NOW))
    assert set(suppressed) == set(sh.FEATURE_REQUIREMENTS)


# -- build_health with real-shaped inputs ----------------------------------


def test_healthy_ranking_snapshots_grade_fresh():
    report = sh.build_health(engine=_healthy_engine(), now=NOW)
    labels = {s.source: s.label for s in report.signals}

    assert labels["ktc_dynasty"] == sh.FRESH
    assert labels["fantasypros_dynasty_1qb"] == sh.FRESH
    assert labels["rotoballer_full_ppr"] == sh.FRESH
    assert "ktc" not in report.unavailable_families


def test_a_short_ktc_list_grades_partial_and_says_so():
    engine = _healthy_engine(ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(hours=3), rows=12))
    report = sh.build_health(engine=engine, now=NOW)
    ktc = next(s for s in report.signals if s.source == "ktc_dynasty")

    assert ktc.label == sh.PARTIAL
    assert ktc.coverage == 12
    assert str(MIN_COVERAGE["ktc"]) in ktc.detail
    assert any("12 rows" in note for note in report.notes)


def test_a_fallback_snapshot_is_flagged_in_the_detail_and_notes():
    engine = _healthy_engine(
        ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(hours=3), rows=500, fallback=True)
    )
    report = sh.build_health(engine=engine, now=NOW)
    ktc = next(s for s in report.signals if s.source == "ktc_dynasty")

    assert ktc.fallback is True
    assert ktc.label == sh.USABLE
    assert "failed re-fetch" in ktc.detail
    assert any("failed re-fetch" in note for note in report.notes)
    # A live source being down is a degradation even though the cached
    # numbers it served are young enough to use.
    assert report.degraded is True


def test_the_outcome_registry_alone_marks_a_fallback(monkeypatch):
    # A snapshot handed around without its flag (rebuilt from JSON, say)
    # is still known to have come from a fallback this run.
    monkeypatch.setitem(sh.ranking_cache.last_fetch_outcome, "ktc_dynasty", "fallback")
    report = sh.build_health(engine=_healthy_engine(), now=NOW)
    assert next(s for s in report.signals if s.source == "ktc_dynasty").fallback is True


def test_an_expired_ranking_source_takes_its_family_down():
    engine = _healthy_engine(ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(days=9)))
    report = sh.build_health(engine=engine, now=NOW)

    assert "ktc" in report.unavailable_families
    assert report.degraded is True


def test_one_dead_fantasypros_list_does_not_kill_the_family():
    engine = _healthy_engine(
        fp_snapshots={
            "dynasty_1qb": _snapshot("fantasypros_dynasty_1qb", dt.timedelta(hours=3), rows=400),
            "ros_full_ppr": _snapshot("fantasypros_ros_full_ppr", dt.timedelta(days=99), rows=400),
        }
    )
    report = sh.build_health(engine=engine, now=NOW)

    assert "fantasypros" not in report.unavailable_families
    assert report.degraded is True


def test_an_engine_with_an_empty_payload_fails_the_parse_check():
    empty = RankingSnapshot(source="ktc_dynasty", fetched_at=NOW, payload=[])
    report = sh.build_health(engine=_healthy_engine(ktc_snapshot=empty), now=NOW)
    ktc = next(s for s in report.signals if s.source == "ktc_dynasty")

    assert ktc.parse_ok is False
    assert ktc.label == sh.UNAVAILABLE
    assert "unreadable" in ktc.detail


# -- Sleeper tables --------------------------------------------------------


def test_a_freshly_synced_storage_grades_fresh(tmp_path):
    with _populated_storage(tmp_path) as storage:
        report = sh.build_health(storage=storage, now=dt.datetime.now(dt.timezone.utc))

    labels = {s.source: s.label for s in report.signals}
    assert labels["sleeper_players"] == sh.FRESH
    assert labels["sleeper_league"] == sh.FRESH
    assert labels["sleeper_weekly"] == sh.FRESH


def test_a_thin_players_table_grades_partial(tmp_path):
    with Storage(tmp_path / "thin.sqlite3") as storage:
        storage.save_players({"1": {"full_name": "Only One"}})
        report = sh.build_health(storage=storage, now=dt.datetime.now(dt.timezone.utc))

    players = next(s for s in report.signals if s.source == "sleeper_players")
    assert players.label == sh.PARTIAL
    assert players.coverage == 1


def test_an_empty_storage_grades_every_sleeper_family_unavailable(tmp_path):
    with Storage(tmp_path / "empty.sqlite3") as storage:
        report = sh.build_health(storage=storage, now=NOW)

    assert {"sleeper_players", "sleeper_league", "sleeper_weekly"} <= report.unavailable_families


# -- schedule and usage ----------------------------------------------------


def test_the_schedule_payload_is_counted_by_its_rows():
    snapshot = RankingSnapshot(
        source="nflverse_schedule",
        fetched_at=NOW - dt.timedelta(hours=2),
        payload={"season": 2026, "rows": [{"week": w} for w in range(285)]},
    )
    report = sh.build_health(schedule_snapshot=snapshot, now=NOW)
    schedule = next(s for s in report.signals if s.family == "nflverse_schedule")

    assert schedule.coverage == 285
    assert schedule.label == sh.FRESH


def test_a_two_week_old_schedule_is_only_usable_not_stale():
    # Its window is deliberately wide — a published schedule barely moves.
    snapshot = RankingSnapshot(
        source="nflverse_schedule",
        fetched_at=NOW - dt.timedelta(days=10),
        payload={"season": 2026, "rows": [{"week": w} for w in range(285)]},
    )
    report = sh.build_health(schedule_snapshot=snapshot, now=NOW)
    assert next(s for s in report.signals if s.family == "nflverse_schedule").label == sh.USABLE


def test_usage_health_is_read_duck_typed():
    usage = FakeUsage(fetched_at=NOW - dt.timedelta(hours=4), latest_week=1, rows=900)
    report = sh.build_health(usage_health=usage, now=NOW)
    signal = next(s for s in report.signals if s.family == "nflverse_usage")

    assert signal.label == sh.FRESH
    assert signal.latest_week == 1
    assert signal.coverage == 900
    assert "week 1" in signal.detail


def test_usage_that_calls_itself_stale_is_stale_however_recently_downloaded():
    usage = FakeUsage(fetched_at=NOW, latest_week=1, rows=900, stale=True)
    report = sh.build_health(usage_health=usage, now=NOW)
    signal = next(s for s in report.signals if s.family == "nflverse_usage")

    assert signal.label == sh.STALE
    assert "behind the current week" in signal.detail


def test_absent_usage_is_unavailable():
    report = sh.build_health(usage_health=FakeUsage(absent=True), now=NOW)
    assert "nflverse_usage" in report.unavailable_families


# -- ff dynasty pass -------------------------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [
        ("fresh (3h old)", sh.FRESH),
        ("stale (9d old, ignoring)", sh.STALE),
        ("not provided (optional)", sh.UNAVAILABLE),
    ],
)
def test_ff_dynasty_status_maps_onto_the_shared_labels(status, expected, monkeypatch):
    monkeypatch.setattr(sh, "ff_dynasty_status", lambda *a, **k: status)
    report = sh.build_health(now=NOW)
    signal = next(s for s in report.signals if s.family == "ff_dynasty_pass")

    assert signal.label == expected
    assert signal.detail == status


# -- suppression -----------------------------------------------------------


def test_a_missing_ktc_suppresses_only_what_needs_ktc():
    engine = _healthy_engine(ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(days=99)))
    suppressed = sh.suppressed_features(sh.build_health(engine=engine, now=NOW))

    assert "dynasty_values" in suppressed
    assert "source_disagreement" in suppressed
    assert "redraft_currency" not in suppressed
    assert "KTC" in suppressed["dynasty_values"]


def test_a_stale_source_does_not_suppress_anything():
    engine = _healthy_engine(ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(days=5)))
    report = sh.build_health(engine=engine, now=NOW)

    assert next(s for s in report.signals if s.source == "ktc_dynasty").label == sh.STALE
    assert "dynasty_values" not in sh.suppressed_features(report)


def test_every_feature_requirement_names_a_known_family():
    known = set(SOURCE_WINDOWS)
    for feature, families in sh.FEATURE_REQUIREMENTS.items():
        assert set(families) <= known, feature


# -- rendering -------------------------------------------------------------


def test_signals_are_ordered_by_family_then_source():
    engine = _healthy_engine(
        fp_snapshots={
            "ros_full_ppr": _snapshot("fantasypros_ros_full_ppr", dt.timedelta(hours=1)),
            "dynasty_1qb": _snapshot("fantasypros_dynasty_1qb", dt.timedelta(hours=1)),
        }
    )
    report = sh.build_health(engine=engine, now=NOW)
    keys = [(s.family, s.source) for s in report.signals]

    assert keys == sorted(keys)
    assert build_twice_is_identical(engine)


def build_twice_is_identical(engine) -> bool:
    a = [(s.family, s.source, s.label) for s in sh.build_health(engine=engine, now=NOW).signals]
    b = [(s.family, s.source, s.label) for s in sh.build_health(engine=engine, now=NOW).signals]
    return a == b


def test_freshness_lines_read_as_source_label_age_rows():
    engine = _healthy_engine(
        ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(hours=2, minutes=54), rows=500)
    )
    lines = sh.freshness_lines(sh.build_health(engine=engine, now=NOW))

    assert "KTC dynasty · Fresh · 2.9h · 500 rows" in lines
    assert "FantasyPros dynasty 1qb · Fresh · 3.0h · 400 rows" in lines


def test_an_age_over_two_days_renders_in_days():
    engine = _healthy_engine(ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(days=4)))
    lines = sh.freshness_lines(sh.build_health(engine=engine, now=NOW))
    assert "KTC dynasty · Stale · 4.0d · 500 rows" in lines


def test_a_missing_source_renders_without_an_age_or_row_count():
    lines = sh.freshness_lines(sh.build_health(now=NOW))
    assert "KTC dynasty · Unavailable · no data" in lines
    assert "Sleeper weekly · Unavailable · no data" in lines


def test_describe_names_the_unavailable_families_and_carries_the_notes():
    engine = _healthy_engine(ktc_snapshot=_snapshot("ktc_dynasty", dt.timedelta(days=99)))
    text = sh.build_health(engine=engine, now=NOW).describe()

    assert text.startswith("Signal health: degraded")
    assert "ktc" in text.splitlines()[0]
    assert "KTC dynasty · Unavailable" in text


def test_describe_says_so_when_nothing_is_wrong(tmp_path):
    with _populated_storage(tmp_path) as storage:
        report = sh.build_health(
            engine=_healthy_engine(),
            storage=storage,
            schedule_snapshot=RankingSnapshot(
                source="nflverse_schedule",
                fetched_at=dt.datetime.now(dt.timezone.utc),
                payload={"season": 2026, "rows": [{"week": 1}] * 285},
            ),
            usage_health=FakeUsage(fetched_at=dt.datetime.now(dt.timezone.utc), latest_week=1, rows=900),
        )
    # ff_dynasty_pass is pinned absent by the fixture, and it is optional by
    # design — so it shows as unavailable without making the run degraded.
    assert report.unavailable_families == {"ff_dynasty_pass"}
    assert report.degraded is False
    assert report.notes == []
    assert report.describe().startswith("Signal health: all sources fresh")
    assert [s.label for s in report.signals if s.family != "ff_dynasty_pass"] == [sh.FRESH] * 8


def test_the_optional_csv_alone_never_degrades_a_run(monkeypatch):
    monkeypatch.setattr(sh, "ff_dynasty_status", lambda *a, **k: "stale (9d old, ignoring)")
    report = sh.build_health(engine=_healthy_engine(), now=NOW)

    ff = next(s for s in report.signals if s.family == "ff_dynasty_pass")
    assert ff.label == sh.STALE
    # The ranking families are all Fresh; only the Sleeper ones are missing,
    # and those are what make this degraded — never the optional CSV.
    assert sh.OPTIONAL_FAMILIES == {"ff_dynasty_pass"}
    assert not any(f in sh.OPTIONAL_FAMILIES for fams in sh.FEATURE_REQUIREMENTS.values() for f in fams)


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing_the_run():
    # This module reports on broken inputs; it must never be the thing that
    # raises. A tz-naive fetched_at is the classic way that happens.
    naive = RankingSnapshot(
        source="ktc_dynasty",
        fetched_at=dt.datetime(2026, 9, 2, 9, 0),  # no tzinfo
        payload=[{"n": i} for i in range(500)],
    )
    report = sh.build_health(engine=_healthy_engine(ktc_snapshot=naive), now=NOW)
    ktc = next(s for s in report.signals if s.source == "ktc_dynasty")

    assert ktc.cache_age == dt.timedelta(hours=3)
    assert ktc.label == sh.FRESH
