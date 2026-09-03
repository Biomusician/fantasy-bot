"""The trending table is a replace, not an accumulate.

Sleeper's /players/nfl/trending endpoint returns a ranked top-N snapshot of
who is being added or dropped right now. save_trending used to upsert into
whatever was already there, which turned the table into an append-only union
of every list ever fetched — ordered by a raw add count that isn't
comparable across days. On the live database that meant 94 `add` rows for a
50-row fetch, with a twelve-day-old count sitting at rank 4 of what the
waiver engine treated as today's list.
"""
from __future__ import annotations

import datetime as dt

import pytest

from sleeper_tool import storage as storage_mod
from sleeper_tool.storage import Storage


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "test.sqlite3") as s:
        yield s


def _trending(*pairs):
    return [{"player_id": pid, "count": count} for pid, count in pairs]


def test_a_player_who_falls_off_the_list_disappears(storage):
    storage.save_trending("add", _trending(("10218", 231630), ("4034", 900)))
    assert {r["player_id"] for r in storage.get_trending("add")} == {"10218", "4034"}

    # Today's fetch: yesterday's runaway leader isn't on it any more.
    storage.save_trending("add", _trending(("6786", 5000), ("4034", 1200)))

    rows = storage.get_trending("add")
    assert [r["player_id"] for r in rows] == ["6786", "4034"]
    assert "10218" not in {r["player_id"] for r in rows}


def test_a_stale_high_count_cannot_outrank_todays_list(storage):
    # The exact live bug: an old huge count kept its position at the top of
    # a count-ordered list forever.
    storage.save_trending("add", _trending(("stale_star", 231630)))
    storage.save_trending("add", _trending(("todays_riser", 4200), ("todays_second", 3100)))

    rows = storage.get_trending("add")
    assert [r["player_id"] for r in rows] == ["todays_riser", "todays_second"]


def test_replacing_one_trend_type_leaves_the_other_alone(storage):
    storage.save_trending("add", _trending(("a1", 10)))
    storage.save_trending("drop", _trending(("d1", 20)))

    storage.save_trending("add", _trending(("a2", 30)))

    assert [r["player_id"] for r in storage.get_trending("add")] == ["a2"]
    assert [r["player_id"] for r in storage.get_trending("drop")] == ["d1"]


def test_an_empty_fetch_clears_the_list_rather_than_freezing_it(storage):
    storage.save_trending("add", _trending(("a1", 10)))
    storage.save_trending("add", [])
    assert storage.get_trending("add") == []


def test_get_trending_still_orders_by_count_descending(storage):
    storage.save_trending("add", _trending(("low", 1), ("high", 999), ("mid", 50)))
    assert [r["player_id"] for r in storage.get_trending("add")] == ["high", "mid", "low"]


def test_fetched_at_is_rewritten_on_every_save(storage, monkeypatch):
    """Strictly later, not merely not-earlier. `utcnow_iso` has one-second
    resolution, so two saves in the same second would satisfy a `>=` even if
    save_trending carried the old stamp forward — the clock is controlled
    here so the assertion can be a real one, without sleeping."""
    clock = iter(["2026-09-01T12:00:00+00:00", "2026-09-01T12:00:05+00:00"])
    monkeypatch.setattr(storage_mod, "utcnow_iso", lambda: next(clock))

    storage.save_trending("add", _trending(("a1", 10)))
    first = storage.get_trending("add")[0]["fetched_at"]
    storage.save_trending("add", _trending(("a1", 11)))
    second = storage.get_trending("add")[0]["fetched_at"]

    assert dt.datetime.fromisoformat(second) > dt.datetime.fromisoformat(first)
    assert storage.get_trending("add")[0]["count"] == 11


# -- the read-only freshness helpers the health layer needs ------------------


def test_table_last_fetched_and_row_count_track_writes(storage):
    assert storage.table_last_fetched("trending") is None
    assert storage.row_count("trending") == 0

    storage.save_trending("add", _trending(("a1", 10), ("a2", 5)))
    assert storage.row_count("trending") == 2
    assert isinstance(storage.table_last_fetched("trending"), dt.datetime)


def test_latest_fetched_at_takes_the_newest_of_several_tables(storage):
    storage.save_trending("add", _trending(("a1", 10)))
    assert storage.latest_fetched_at("matchups", "transactions") is None
    assert storage.latest_fetched_at("matchups", "trending") == storage.table_last_fetched("trending")


def test_unknown_table_names_are_rejected_rather_than_interpolated(storage):
    with pytest.raises(ValueError):
        storage.table_last_fetched("trending; DROP TABLE players")
    with pytest.raises(ValueError):
        storage.row_count("players")  # tracked via meta, not a fetched_at column
