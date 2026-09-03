"""The trending fetch must not be able to erase yesterday's list.

storage.save_trending REPLACES the whole table (see test_storage_trending),
which is right for a ranked top-N snapshot but means an empty response —
Sleeper throttling, a deploy, a momentary blip — would clear a signal the
waiver engine reads and leave nothing behind until the next daily run.
"""
from __future__ import annotations

import pytest

from sleeper_tool.storage import Storage
from sleeper_tool.sync import save_trending_if_nonempty


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "test.sqlite3") as s:
        yield s


def _rows(*player_ids):
    return [{"player_id": pid, "count": 100 - i} for i, pid in enumerate(player_ids)]


def test_a_nonempty_list_is_saved(storage):
    assert save_trending_if_nonempty(storage, "add", _rows("a", "b")) is True
    assert [r["player_id"] for r in storage.get_trending("add")] == ["a", "b"]


def test_an_empty_list_keeps_the_previous_one(storage):
    save_trending_if_nonempty(storage, "add", _rows("a", "b"))
    assert save_trending_if_nonempty(storage, "add", []) is False
    assert [r["player_id"] for r in storage.get_trending("add")] == ["a", "b"]


def test_an_empty_first_fetch_leaves_the_table_empty_rather_than_erroring(storage):
    assert save_trending_if_nonempty(storage, "drop", []) is False
    assert storage.get_trending("drop") == []
