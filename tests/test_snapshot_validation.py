"""The gate that stops a bad refresh destroying a good cache.

Every source here can fail without raising: a renamed column, a truncated
download, one position list 502ing, a CDN 404 that the fetcher turns into an
"absent" marker. Each parses cleanly to a fraction of a board. These tests
are about the one question that matters — does the previous snapshot survive?
"""
from __future__ import annotations

import datetime as dt

import pytest

from sleeper_tool.rankings import cache as cache_module
from sleeper_tool.rankings.cache import RankingSnapshot, get_or_fetch, load_snapshot, save_snapshot
from sleeper_tool.rankings.snapshot_validation import (
    MIN_SHARE_OF_PREVIOUS,
    absent_marker_validator,
    by_row_count,
    rows_of,
)


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache_module, "last_fetch_outcome", {})
    monkeypatch.setattr(cache_module, "last_fetch_error", {})
    cache_module._parsed_cache.clear()


def snapshot_of(payload) -> RankingSnapshot:
    return RankingSnapshot(source="s", fetched_at=dt.datetime.now(dt.timezone.utc), payload=payload)


def rows(n: int) -> list[dict]:
    return [{"name": f"p{i}", "rank_ecr": i + 1} for i in range(n)]


# -- counting ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ([1, 2, 3], 3),
        ((1, 2), 2),
        ({"rows": [1, 2, 3, 4]}, 4),
        ({"rows": []}, 0),
        ({"absent": True}, None),
        ({}, None),
        (None, None),
        ("a string", None),
        (7, None),
    ],
)
def test_rows_of_counts_the_shapes_the_sources_actually_cache(payload, expected):
    assert rows_of(payload) == expected


# -- the relative rule ------------------------------------------------------------------------


def test_a_refresh_that_collapses_against_the_cached_one_is_refused():
    validate = by_row_count(label="Src", floor=0)  # the relative rule alone
    verdict = validate(rows(6), snapshot_of(rows(500)))
    assert "6 rows against 500" in verdict
    assert "a parse failure, not a smaller list" in verdict


def test_a_list_that_merely_shrank_is_still_accepted():
    # Ranking lists get trimmed week to week; the failures this catches are
    # order-of-magnitude, not marginal.
    validate = by_row_count(label="Src", floor=10)
    assert validate(rows(460), snapshot_of(rows(500))) is True
    just_over = int(500 * MIN_SHARE_OF_PREVIOUS) + 1
    assert validate(rows(just_over), snapshot_of(rows(500))) is True


def test_an_absolute_floor_catches_a_collapse_that_the_share_alone_would_allow():
    validate = by_row_count(label="Src", floor=100)
    assert "fewer than the 100" in validate(rows(5), snapshot_of(rows(8)))


def test_a_payload_with_nothing_to_count_is_refused_when_there_is_something_to_lose():
    validate = by_row_count(label="Src", floor=10)
    assert "with no rows to count" in validate("not a board", snapshot_of(rows(500)))
    assert "with no rows to count" in validate(None, snapshot_of(rows(500)))


def test_the_gate_does_not_second_guess_a_first_fetch():
    # Its contract is "never replace a good snapshot with a worse one". With
    # nothing cached, refusing would leave the source with nothing at all.
    validate = by_row_count(label="Src", floor=100)
    assert validate(rows(3), None) is True
    assert validate(rows(3), snapshot_of([])) is True


def test_a_content_check_runs_on_top_of_the_counts():
    def no_blanks(payload):
        return "a row has no name" if any(not r["name"] for r in payload) else None

    validate = by_row_count(label="Src", floor=0, extra=no_blanks)
    good, bad = rows(500), rows(500)
    bad[3]["name"] = ""
    assert validate(good, snapshot_of(rows(500))) is True
    assert validate(bad, snapshot_of(rows(500))) == "a row has no name"


# -- the absent marker ------------------------------------------------------------------------


def test_an_absent_marker_may_not_replace_a_season_of_data():
    # One transient 404 in week 10 used to wipe the usage file — and because
    # the health layer treats an absent file as expected, it rendered as
    # normal preseason behaviour rather than as a fault.
    validate = absent_marker_validator(label="nflverse_stats_player_2026")
    verdict = validate({"season": 2026, "absent": True}, snapshot_of({"rows": rows(600), "absent": False}))
    assert "came back absent while the cached one holds 600 rows" in verdict


def test_an_absent_marker_is_fine_when_there_is_nothing_to_lose():
    validate = absent_marker_validator(label="nflverse_stats_player_2026")
    assert validate({"season": 2026, "absent": True}, None) is True
    assert validate({"season": 2026, "absent": True}, snapshot_of({"rows": [], "absent": True})) is True


def test_a_real_payload_still_goes_through_the_ordinary_rules():
    validate = absent_marker_validator(label="usage")
    previous = snapshot_of({"rows": rows(600), "absent": False})
    assert validate({"rows": rows(590), "absent": False}, previous) is True
    assert "against 600" in validate({"rows": rows(20), "absent": False}, previous)


# -- end to end through the cache ---------------------------------------------------------------


def test_the_previous_snapshot_survives_every_shape_of_bad_refresh():
    validate = by_row_count(label="Src", floor=50)
    for bad in ([], rows(3), {"rows": []}, "junk", None, {}):
        cache_module._parsed_cache.clear()
        save_snapshot("src", rows(500))
        snapshot = get_or_fetch(
            "src", lambda b=bad: b, max_age=dt.timedelta(0),
            ceiling=dt.timedelta(days=7), validate=validate,
        )
        assert len(snapshot.payload) == 500, bad
        assert snapshot.served_from_fallback is True, bad
        assert cache_module.last_fetch_outcome["src"] == "rejected", bad
        assert load_snapshot("src").payload == rows(500), bad


def test_a_genuine_refresh_still_lands():
    save_snapshot("src", rows(500))
    snapshot = get_or_fetch(
        "src", lambda: rows(520), max_age=dt.timedelta(0),
        ceiling=dt.timedelta(days=7), validate=by_row_count(label="Src", floor=50),
    )
    assert len(snapshot.payload) == 520
    assert cache_module.last_fetch_outcome["src"] == "fresh"
    assert len(load_snapshot("src").payload) == 520
