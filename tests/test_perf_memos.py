"""Guards on the caches added for speed. Each one is only allowed to exist
because it is invisible: these tests pin the ways it could stop being.
"""
import datetime as dt
import json
import os
from dataclasses import replace

import pytest
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool import lineup_optimizer
from sleeper_tool.lineup_optimizer import _slot_tables, optimize_lineup, slot_eligibility
from sleeper_tool.rankings import cache as cache_module
from sleeper_tool.storage import Storage

STANDARD = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN", "IR", "TAXI")


@pytest.fixture(autouse=True)
def _empty_lineup_memo():
    lineup_optimizer._lineup_memo.clear()
    yield
    lineup_optimizer._lineup_memo.clear()


def _player(pid, pos, proj, **kw):
    value_kw = {k: kw.pop(k) for k in list(kw) if k in ("bye_week", "dynasty_rank", "dynasty_ecr_rank", "redraft_ecr_rank")}
    return make_entry(
        player_id=pid, name=pid, position=pos, is_starter=False,
        value=make_value(name=pid, position=pos, proj_points=proj, **value_kw),
        **kw,
    )


def _roster(entries, positions=STANDARD, kind="redraft"):
    return make_roster(entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(kind=kind))


def _squad():
    return [
        _player("qb1", "QB", 300), _player("qb2", "QB", 250),
        _player("rb1", "RB", 200), _player("rb2", "RB", 180), _player("rb3", "RB", 120),
        _player("wr1", "WR", 190), _player("wr2", "WR", 170), _player("wr3", "WR", 150, bye_week=7),
        _player("te1", "TE", 100), _player("k1", "K", None), _player("def1", "DEF", None),
    ]


# -- slot tables -------------------------------------------------------------


@pytest.mark.parametrize("positions", [
    ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"),
    ("QB", "RB", "WR", "SUPER_FLEX", "WRRB_FLEX", "REC_FLEX"),
])
def test_the_mask_cost_table_matches_summing_the_filled_slots_by_hand(positions):
    """The final tie-break prefers the lineup using the more restrictive
    slots. That used to be re-derived per mask; the table has to agree with
    it for every mask, not just the ones a test happens to reach."""
    eligibility, cost, _ = _slot_tables(positions)
    assert eligibility == tuple(slot_eligibility(s) for s in positions)
    for mask in range(1 << len(positions)):
        by_hand = sum(len(eligibility[i]) for i in range(len(positions)) if mask & (1 << i))
        assert cost[mask] == by_hand


def test_slot_candidates_are_most_restrictive_first_then_slot_order():
    _e, _c, by_position = _slot_tables(("FLEX", "RB", "SUPER_FLEX", "RB"))
    assert by_position["RB"] == (1, 3, 0, 2)  # both RB slots, then FLEX, then SUPER_FLEX
    assert by_position["QB"] == (2,)
    assert "K" not in by_position  # nothing this slot list can start


# -- the optimize_lineup memo ------------------------------------------------


def test_a_repeated_call_returns_an_equal_but_independent_result():
    roster = _roster(_squad())
    first = optimize_lineup(roster, nfl_week=3)
    second = optimize_lineup(roster, nfl_week=3)
    assert first == second
    assert first is not second
    assert first.assignments is not second.assignments
    assert first.unavailable is not second.unavailable

    first.assignments.clear()
    first.bench_player_ids.append("bogus")
    first.unavailable["bogus"] = "bogus"
    third = optimize_lineup(roster, nfl_week=3)
    assert third == second


@pytest.mark.parametrize("kwargs", [
    {"nfl_week": 7},
    {"exclude_game_day_out": True},
    {"excluded_player_ids": ("wr1",)},
])
def test_arguments_that_change_the_answer_are_all_in_the_memo_key(kwargs):
    roster = _roster(_squad() + [_player("wr4", "WR", 195, injury_status="Out")])
    baseline = optimize_lineup(roster)
    assert optimize_lineup(roster, **kwargs) != baseline


def test_a_changed_roster_is_not_served_the_previous_roster_s_lineup():
    entries = _squad()
    roster = _roster(entries)
    before = optimize_lineup(roster)
    assert before.slot_by_player["wr3"] == "FLEX"

    # Same players, same ids, one projection moved past the flex incumbent.
    promoted = [replace(e, value=replace(e.value, proj_points=160.0)) if e.player_id == "rb3" else e for e in entries]
    after = optimize_lineup(_roster(promoted))
    assert after.slot_by_player["rb3"] == "FLEX"
    assert "wr3" in after.bench_player_ids


def test_a_renamed_player_is_not_served_the_old_name():
    entries = _squad()
    assert optimize_lineup(_roster(entries)).assignment_for("qb1").name == "qb1"
    renamed = [replace(e, name="Renamed") if e.player_id == "qb1" else e for e in entries]
    assert optimize_lineup(_roster(renamed)).assignment_for("qb1").name == "Renamed"


def test_an_entry_reordering_changes_the_unavailable_order_and_is_not_memoized_away():
    """`unavailable` is reported in roster order, so entry order is part of
    the output and therefore part of the memo key."""
    entries = _squad() + [
        _player("hurt1", "WR", 50, injury_status="IR"),
        _player("hurt2", "WR", 40, injury_status="PUP"),
    ]
    forward = optimize_lineup(_roster(entries))
    backward = optimize_lineup(_roster(list(reversed(entries))))
    assert list(forward.unavailable) == ["hurt1", "hurt2"]
    assert list(backward.unavailable) == ["hurt2", "hurt1"]


def test_two_leagues_with_the_same_players_but_different_slots_do_not_share():
    entries = _squad()
    one_flex = optimize_lineup(_roster(entries))
    superflex = optimize_lineup(_roster(entries, positions=("QB", "RB", "WR", "SUPER_FLEX", "BN")))
    assert "qb2" not in one_flex.starter_ids
    assert superflex.slot_by_player["qb2"] == "SUPER_FLEX"


def test_the_memo_is_bounded_and_still_answers_correctly_once_it_has_been_cleared(monkeypatch):
    monkeypatch.setattr(lineup_optimizer, "_MEMO_LIMIT", 4)
    roster = _roster(_squad())
    expected = optimize_lineup(roster)
    for week in range(1, 12):
        optimize_lineup(roster, nfl_week=week)
        assert len(lineup_optimizer._lineup_memo) <= 4
    assert optimize_lineup(roster) == expected


def test_an_unfillable_slot_list_still_raises_after_a_successful_call():
    roster = _roster(_squad())
    optimize_lineup(roster)
    broken = _roster(_squad(), positions=("QB", "PUNTER"))
    with pytest.raises(lineup_optimizer.UnsupportedSlotError):
        optimize_lineup(broken)


# -- Storage.get_league memo -------------------------------------------------


def test_get_league_is_memoized_and_save_league_invalidates_it(tmp_path):
    with Storage(tmp_path / "s.sqlite3") as storage:
        storage.save_league("L1", {"name": "One", "season": "2026", "total_rosters": 12})
        first = storage.get_league("L1")
        assert first is storage.get_league("L1")  # not re-parsed

        storage.save_league("L1", {"name": "One", "season": "2026", "total_rosters": 10})
        assert storage.get_league("L1")["total_rosters"] == 10


def test_a_missing_league_is_remembered_as_missing_and_still_appears_when_saved(tmp_path):
    with Storage(tmp_path / "s.sqlite3") as storage:
        assert storage.get_league("nope") is None
        assert storage.get_league("nope") is None
        storage.save_league("nope", {"name": "Late", "season": "2026"})
        assert storage.get_league("nope")["name"] == "Late"


# -- rankings cache read memo ------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    cache_module._parsed_cache.clear()
    yield tmp_path
    cache_module._parsed_cache.clear()


def _settle(path):
    """Backdate the file past the memo's settling window, which otherwise
    (correctly) refuses to trust a just-written file's mtime."""
    old = dt.datetime.now().timestamp() - 3600
    os.utime(path, (old, old))


def test_a_settled_file_is_parsed_once_and_served_from_the_memo(cache_dir):
    cache_module.save_snapshot("src", {"rows": [1, 2, 3]})
    path = cache_module._cache_path("src")
    _settle(path)

    first = cache_module.load_snapshot("src")
    second = cache_module.load_snapshot("src")
    assert first.payload is second.payload  # the expensive part was reused
    assert first is not second  # but never the same snapshot object


def test_a_memoized_snapshot_does_not_inherit_a_previous_caller_s_fallback_flag(cache_dir):
    cache_module.save_snapshot("src", {"rows": [1]})
    _settle(cache_module._cache_path("src"))
    first = cache_module.load_snapshot("src")
    first.served_from_fallback = True
    assert cache_module.load_snapshot("src").served_from_fallback is False


def test_saving_over_a_memoized_snapshot_serves_the_new_payload(cache_dir):
    cache_module.save_snapshot("src", {"rows": [1]})
    _settle(cache_module._cache_path("src"))
    assert cache_module.load_snapshot("src").payload == {"rows": [1]}
    cache_module.save_snapshot("src", {"rows": [2]})
    assert cache_module.load_snapshot("src").payload == {"rows": [2]}


def test_a_just_written_file_is_never_served_from_the_memo(cache_dir):
    """The case a plain mtime key gets wrong on Windows, whose file times
    come from a ~15ms clock: a test helper rewrites a cached snapshot with a
    different date but an identical byte count, milliseconds after it was
    read, and both writes land on the same mtime. The mtimes are pinned
    equal here so the outcome doesn't depend on this machine's timer
    resolution — only on the memo refusing to trust a file this young."""
    cache_module.save_snapshot("src", {"rows": [1]})
    path = cache_module._cache_path("src")
    recent = dt.datetime.now().timestamp() - 0.2
    os.utime(path, (recent, recent))
    before = cache_module.load_snapshot("src")

    swapped = json.loads(path.read_text(encoding="utf-8"))
    swapped["fetched_at"] = (before.fetched_at - dt.timedelta(days=30)).isoformat()
    text = json.dumps(swapped)
    assert len(text) == len(path.read_text(encoding="utf-8"))  # size can't give it away either
    path.write_text(text, encoding="utf-8")
    os.utime(path, (recent, recent))

    assert cache_module.load_snapshot("src").fetched_at != before.fetched_at


def test_a_missing_or_corrupt_file_still_reads_as_no_snapshot(cache_dir):
    assert cache_module.load_snapshot("never_written") is None
    path = cache_module._cache_path("bad")
    path.write_text("{not json", encoding="utf-8")
    _settle(path)
    assert cache_module.load_snapshot("bad") is None
    assert cache_module.load_snapshot("bad") is None


def test_the_read_memo_is_bounded(cache_dir, monkeypatch):
    monkeypatch.setattr(cache_module, "_PARSED_LIMIT", 3)
    for i in range(8):
        cache_module.save_snapshot(f"src{i}", {"i": i})
        _settle(cache_module._cache_path(f"src{i}"))
        assert cache_module.load_snapshot(f"src{i}").payload == {"i": i}
        assert len(cache_module._parsed_cache) <= 3
