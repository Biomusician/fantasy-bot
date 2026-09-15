from __future__ import annotations

import dataclasses
import datetime as dt
import os
from pathlib import Path

import pytest

from sleeper_tool.rankings import fantasyfootballers as ff
from sleeper_tool.rankings.fantasyfootballers import (
    BALLERS_SPLIT,
    MAX_UNDATED_AGE,
    MODERATE_AGREEMENT,
    STRONG_AGREEMENT,
    BallersRow,
    find_waiver_csv,
    is_ballers_waiver_header,
    load_ballers_board,
    parse_ballers_csv,
    week_from_filename,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "ballers"
    / "Week 2 Fantasy Football Waivers - Fantasy Footballers Podcast.csv"
)
NOW = dt.datetime(2026, 9, 15, 18, 0, tzinfo=dt.timezone.utc)
HEADER = '"Name","Team","Rank","Andy","Jason","Mike","FAAB","WK 2","WK 3","WK 4","WK 5"'
GATED = ('"Suggested FAAB values are available to FootClan membersJoin to Unlock",'
         + ",".join(['"Upcoming matchup data is available to FootClan membersJoin to Unlock"'] * 4))
DFS_HEADER = '"Name","Team","Pos","Salary","Proj","Value"'


def _board_csv(rows: list[str], header: str = HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


def _write(directory: Path, name: str, text: str, *, age: dt.timedelta = dt.timedelta(hours=2), bom: bool = False) -> Path:
    path = directory / name
    path.write_text(("﻿" if bom else "") + text, encoding="utf-8")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _simple_board(n: int = 3) -> str:
    return _board_csv([f'"Player {chr(65 + i)}","SF","{i + 1}","{i + 1}","{i + 2}","{i + 1}",{GATED}' for i in range(n)])


def _row(andy=None, jason=None, mike=None) -> BallersRow:
    return BallersRow(name="X", team="SF", rank=1, andy=andy, jason=jason, mike=mike)


# -- filename / header ------------------------------------------------------

def test_week_from_filename():
    assert week_from_filename("Week 2 Fantasy Football Waivers - Fantasy Footballers Podcast.csv") == 2
    assert week_from_filename("WEEK12 waivers.csv") == 12
    assert week_from_filename("waivers week  7.csv") == 7
    assert week_from_filename("Fantasy Football Waivers.csv") is None


def test_header_detection():
    assert is_ballers_waiver_header(["Name", "Team", "Rank", "Andy", "Jason", "Mike", "FAAB"])
    assert is_ballers_waiver_header(["﻿Name", " Team ", "Rank", "Andy", "Jason", "Mike", " FAAB"])
    assert not is_ballers_waiver_header(["Name", "Team", "Rank", "Andy", "Jason", "FAAB"])
    assert not is_ballers_waiver_header(None)
    assert not is_ballers_waiver_header([])


def test_dynasty_and_rookie_exports_are_not_waiver_boards():
    # Real Fantasy Footballers export headers that carry every required column.
    dynasty = ["Rank", "Name", "Team", "Pos", "Age", "Andy", "Jason", "Mike"]
    rookie = ["Rank", "Name", "Team", "Bye", "Pos", "Age", "Height", "Weight", "Andy", "Jason", "Mike"]
    assert not is_ballers_waiver_header(dynasty)
    assert not is_ballers_waiver_header(rookie)


def test_dynasty_csv_in_folder_never_qualifies(tmp_path):
    header = '"Rank","Name","Team","Pos","Age","Andy","Jason","Mike"'
    _write(tmp_path, "Week 2 Dynasty Startup Rankings - Fantasy Footballers Podcast.csv",
           _board_csv(['"1","Puka Nacua","LAR","WR","25.3","1","1","2"'], header=header))
    assert find_waiver_csv(tmp_path, target_week=2, now=NOW)[0] is None
    assert load_ballers_board(tmp_path, now=NOW) is None


def test_header_detection_rejects_empty():
    assert not is_ballers_waiver_header(None)
    assert not is_ballers_waiver_header([])


# -- parsing ----------------------------------------------------------------

def test_parse_real_fixture():
    board = parse_ballers_csv(FIXTURE, now=NOW)
    assert board.week == 2
    assert board.source_file == FIXTURE.name
    assert len(board.rows) == 12
    first = board.rows[0]
    assert (first.name, first.team, first.rank, first.andy, first.jason, first.mike) == ("Jalen Coker", "CAR", 1, 1, 2, 1)
    assert first.spread == 1 and first.agreement == STRONG_AGREEMENT
    tyler = next(r for r in board.rows if r.name == "Tyler Shough")
    # Published rank stays authoritative even though the hosts disagree wildly.
    assert tyler.rank == 12 and tyler.spread == 31 and tyler.agreement == BALLERS_SPLIT
    assert board.loaded_at == NOW
    assert board.status.startswith("Week 2 board · 12 players")


def test_gated_columns_ignored_and_noted():
    board = parse_ballers_csv(FIXTURE, now=NOW)
    field_names = {f.name for f in dataclasses.fields(BallersRow)}
    assert field_names == {"name", "team", "rank", "andy", "jason", "mike"}
    for row in board.rows:
        for value in dataclasses.astuple(row):
            assert "FootClan" not in str(value)
    gated = [p for p in board.problems if "gated" in p]
    assert len(gated) == 1
    assert "5 gated columns" in gated[0]
    assert "FootClan" not in gated[0]


def test_duplicate_player_keeps_better_rank(tmp_path):
    path = _write(tmp_path, "Week 3 waivers.csv", _board_csv([
        f'"Deebo Samuel Sr.","SF","9","9","9","9",{GATED}',
        f'"Deebo Samuel","SF","3","3","4","2",{GATED}',
        f'"Other Guy","NO","4","4","4","4",{GATED}',
    ]))
    board = parse_ballers_csv(path, now=NOW)
    deebo = [r for r in board.rows if "Deebo" in r.name]
    assert len(deebo) == 1 and deebo[0].rank == 3
    assert any("duplicate player" in p and "kept rank 3" in p for p in board.problems)


def test_blank_or_garbage_host_rank_becomes_none(tmp_path):
    path = _write(tmp_path, "Week 3 waivers.csv", _board_csv([
        f'"A","SF","1","","n/a","4",{GATED}',
        f'"B","sf","2"," 7 ","0","9",{GATED}',
    ]))
    board = parse_ballers_csv(path, now=NOW)
    a, b = board.rows
    assert (a.andy, a.jason, a.mike) == (None, None, 4)
    assert a.spread is None and a.agreement is None
    assert (b.andy, b.jason, b.mike) == (7, None, 9)
    assert b.team == "SF"


def test_non_integer_rank_and_blank_name_rows_skipped(tmp_path):
    path = _write(tmp_path, "Week 3 waivers.csv", _board_csv([
        f'"A","SF","1","1","1","1",{GATED}',
        f'"B","SF","two","2","2","2",{GATED}',
        f'"","SF","3","3","3","3",{GATED}',
        f'"C","SF","","4","4","4",{GATED}',
        f'"D","SF","5","5","5","5",{GATED}',
    ]))
    board = parse_ballers_csv(path, now=NOW)
    assert [r.name for r in board.rows] == ["A", "D"]
    assert sum("row skipped" in p for p in board.problems) == 3


def test_missing_required_column_parses_to_no_rows(tmp_path):
    header = '"Name","Team","Rank","Andy","Jason","FAAB"'
    path = _write(tmp_path, "Week 3 waivers.csv", _board_csv(['"A","SF","1","1","1","x"'], header=header))
    board = parse_ballers_csv(path, now=NOW)
    assert board.rows == []
    assert load_ballers_board(tmp_path, now=NOW) is None


def test_week_mismatch_noted(tmp_path):
    path = _write(tmp_path, "Week 3 waivers.csv", _simple_board())
    board = parse_ballers_csv(path, week=4, now=NOW)
    assert board.week == 3
    assert any("week 3" in p and "week 4" in p for p in board.problems)


def test_bom_header(tmp_path):
    path = _write(tmp_path, "Week 3 waivers.csv", _simple_board(), bom=True)
    assert len(parse_ballers_csv(path, now=NOW).rows) == 3
    assert find_waiver_csv(tmp_path, target_week=3, now=NOW)[0] == path


# -- agreement boundaries ---------------------------------------------------

@pytest.mark.parametrize("ranks, spread, label", [
    ((10, 15, 12), 5, STRONG_AGREEMENT),
    ((10, 16, 12), 6, MODERATE_AGREEMENT),
    ((10, 22, 12), 12, MODERATE_AGREEMENT),
    ((10, 23, 12), 13, BALLERS_SPLIT),
    ((10, None, None), None, None),
    ((None, None, None), None, None),
    ((None, 3, 16), 13, BALLERS_SPLIT),
    ((4, None, 9), 5, STRONG_AGREEMENT),
])
def test_spread_and_agreement(ranks, spread, label):
    row = _row(*ranks)
    assert row.spread == spread
    assert row.agreement == label


# -- discovery --------------------------------------------------------------

def test_no_valid_file(tmp_path):
    _write(tmp_path, "Week 2 DFS Rankings - Fantasy Footballers.csv", _board_csv(['"A","SF","QB","5000","20","4"'], header=DFS_HEADER))
    _write(tmp_path, "notes.txt", "hello")
    path, reason = find_waiver_csv(tmp_path, target_week=2, now=NOW)
    assert path is None
    assert "no Fantasy Footballers waiver CSV" in reason
    assert load_ballers_board(tmp_path, target_week=2, now=NOW) is None


def test_default_reason_names_manual_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ff, "MANUAL_DIR", tmp_path)
    assert find_waiver_csv(tmp_path, now=NOW) == (None, "no Fantasy Footballers waiver CSV in data/manual")


def test_missing_directory(tmp_path):
    assert find_waiver_csv(tmp_path / "nope", now=NOW)[0] is None
    assert load_ballers_board(tmp_path / "nope", now=NOW) is None


def test_dfs_csv_never_qualifies_even_when_newer(tmp_path):
    board = _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(days=1))
    _write(tmp_path, "Week 2 Dynasty Rankings.csv", _board_csv(['"A","SF","QB","5000","20","4"'], header=DFS_HEADER), age=dt.timedelta(minutes=5))
    _write(tmp_path, "Fantasy Football DFS.csv", _board_csv(['"A","SF","QB","5000","20","4"'], header=DFS_HEADER), age=dt.timedelta(minutes=5))
    assert find_waiver_csv(tmp_path, target_week=2, now=NOW)[0] == board
    assert find_waiver_csv(tmp_path, now=NOW)[0] == board


def test_wrong_week_not_used(tmp_path):
    _write(tmp_path, "Week 1 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(hours=1))
    path, reason = find_waiver_csv(tmp_path, target_week=2, now=NOW)
    assert path is None
    assert reason == "newest Ballers board is for week 1, not week 2 — download this week's CSV"
    assert load_ballers_board(tmp_path, target_week=2, now=NOW) is None


def test_week_match_preferred_over_newer_other_week(tmp_path):
    wk2 = _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(days=3))
    _write(tmp_path, "Week 3 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(hours=1))
    path, reason = find_waiver_csv(tmp_path, target_week=2, now=NOW)
    assert path == wk2
    assert "week 2" in reason


def test_newest_of_two_same_week_files(tmp_path):
    _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(days=1))
    newer = _write(tmp_path, "Week 2 Fantasy Football Waivers (1).csv", _simple_board(5), age=dt.timedelta(hours=1))
    assert find_waiver_csv(tmp_path, target_week=2, now=NOW)[0] == newer
    board = load_ballers_board(tmp_path, target_week=2, now=NOW)
    assert board is not None and len(board.rows) == 5 and board.week == 2


def test_no_target_week_picks_highest_week(tmp_path):
    _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(hours=1))
    wk3 = _write(tmp_path, "Week 3 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(days=2))
    path, reason = find_waiver_csv(tmp_path, now=NOW)
    assert path == wk3
    assert "week 3" in reason


def test_undated_file_within_max_age(tmp_path):
    undated = _write(tmp_path, "Fantasy Football Waivers.csv", _simple_board(), age=MAX_UNDATED_AGE - dt.timedelta(hours=1))
    path, reason = find_waiver_csv(tmp_path, target_week=2, now=NOW)
    assert path == undated
    assert "undated" in reason
    board = load_ballers_board(tmp_path, target_week=2, now=NOW)
    assert board is not None and board.week == 2
    assert any("assumed week 2" in p for p in board.problems)


def test_undated_file_over_max_age(tmp_path):
    _write(tmp_path, "Fantasy Football Waivers.csv", _simple_board(), age=MAX_UNDATED_AGE + dt.timedelta(hours=1))
    path, reason = find_waiver_csv(tmp_path, target_week=2, now=NOW)
    assert path is None
    assert "no week in its name" in reason
    assert find_waiver_csv(tmp_path, now=NOW)[0] is None


def test_dated_week_match_beats_fresh_undated(tmp_path):
    wk2 = _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(days=2))
    _write(tmp_path, "Fantasy Football Waivers.csv", _simple_board(), age=dt.timedelta(hours=1))
    assert find_waiver_csv(tmp_path, target_week=2, now=NOW)[0] == wk2


# -- load never raises ------------------------------------------------------

def test_load_real_fixture_copy(tmp_path):
    _write(tmp_path, FIXTURE.name, FIXTURE.read_text(encoding="utf-8"), age=dt.timedelta(hours=3))
    board = load_ballers_board(tmp_path, target_week=2, now=NOW)
    assert board is not None
    assert board.status == "Week 2 board · 12 players · loaded 3h ago"
    assert board.file_mtime == NOW - dt.timedelta(hours=3)


def test_load_header_only_file_is_none(tmp_path):
    _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", HEADER + "\n")
    assert load_ballers_board(tmp_path, target_week=2, now=NOW) is None


def test_load_empty_file_is_none(tmp_path):
    _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", "")
    assert load_ballers_board(tmp_path, target_week=2, now=NOW) is None


def test_load_corrupt_file_is_none(tmp_path):
    path = _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", _simple_board())
    # Valid header, then bytes that are not UTF-8.
    path.write_bytes(HEADER.encode("utf-8") + b"\n\xff\xfe\x00garbage\x81\n")
    stamp = NOW.timestamp()
    os.utime(path, (stamp, stamp))
    assert load_ballers_board(tmp_path, target_week=2, now=NOW) is None


def test_load_survives_parse_error(monkeypatch, tmp_path):
    _write(tmp_path, "Week 2 Fantasy Football Waivers.csv", _simple_board())

    def boom(*args, **kwargs):
        raise OSError("locked by Excel")

    monkeypatch.setattr(ff, "parse_ballers_csv", boom)
    assert load_ballers_board(tmp_path, target_week=2, now=NOW) is None
