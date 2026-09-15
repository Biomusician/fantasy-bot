from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from sleeper_tool.rankings import boone as boone_module
from sleeper_tool.rankings import fantasyfootballers as ballers_module
from sleeper_tool.rankings import rotoballer_waivers as roto_module
from sleeper_tool.rankings.boone import BooneBoard, BooneRow
from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.rankings.fantasyfootballers import BallersBoard, BallersRow
from sleeper_tool.rankings.rotoballer_waivers import WaiverBoard, WaiverBoardRow
from sleeper_tool.waiver_sources import (
    BALLERS,
    BOONE,
    FP_ROS,
    FP_ROS_FULL,
    FP_ROS_HALF,
    LOADED,
    NOT_PROVIDED,
    ROTOBALLER,
    UNAVAILABLE,
    WRONG_WEEK,
    fp_ros_page_for,
    load_waiver_sources,
)

NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)

ALL_PLAYERS = {
    "1": {"full_name": "Jalen Coker", "position": "WR", "team": "CAR", "status": "Active"},
    "2": {"full_name": "Brenton Strange", "position": "TE", "team": "JAX", "status": "Active"},
    "3": {"full_name": "Kyren Williams", "position": "RB", "team": "LAR", "status": "Active"},
}

FP_FULL_PAGE = "FantasyPros ROS (full-ppr)"
FP_HALF_PAGE = "FantasyPros ROS (half-ppr)"
BOONE_FULL = f"{BOONE} (full PPR)"


def fp_rows():
    return [
        {"name": "Jalen Coker", "team": "CAR", "position": "WR", "pos_rank": "WR41", "rank_ecr": 120, "rank_std": 4.5,
         "rank_min": 100, "rank_max": 140},
        {"name": "Nobody At All", "team": "FA", "position": "WR", "pos_rank": "WR99", "rank_ecr": 300, "rank_std": 1.0,
         "rank_min": 290, "rank_max": 310},
    ]


def engine_with(pages=(FP_ROS_FULL, FP_ROS_HALF)):
    snaps = {p: RankingSnapshot(source=p, fetched_at=NOW, payload=fp_rows()) for p in pages}
    return SimpleNamespace(fp_snapshots=snaps)


def ballers_board(week=2):
    return BallersBoard(
        week=week, source_file="ballers_week2.csv", loaded_at=NOW, file_mtime=NOW,
        rows=[
            BallersRow(name="Jalen Coker", team="CAR", rank=1, andy=1, jason=2, mike=3),
            BallersRow(name="Ghost Player", team="FA", rank=2, andy=4, jason=5, mike=6),
        ],
        problems=[], status="ok",
    )


def roto_board(week=2):
    return WaiverBoard(
        source="rotoballer_waivers", week=week, url="http://example.invalid", fetched_at=NOW,
        rows=[WaiverBoardRow(rank=1, name="Brenton Strange", team="JAX", position="TE",
                             league_size_note="12+ Team PPR Leagues", min_league_size=12)],
        problems=[],
    )


def boone_board(week=2):
    return BooneBoard(
        scoring="ppr", week=week, fetched_at=NOW,
        rows=[
            BooneRow(position_list="RB", rank=7, name="Kyren Williams", team="LAR", position="RB"),
            BooneRow(position_list="FLEX", rank=1, name="Kyren Williams", team="LAR", position="RB"),
            BooneRow(position_list="K", rank=1, name="Some Kicker", team="LAR", position="K"),
        ],
    )


@pytest.fixture
def loaders(monkeypatch):
    """Every external loader replaced with a synthetic one. Tests mutate
    `state` to say what each source should return (or raise)."""
    state = {
        "ballers": ballers_board(),
        "ballers_reason": "",
        "roto": roto_board(),
        "boone": boone_board(),
    }

    def find_waiver_csv(directory, *, target_week=None, now=None):
        return (None, state["ballers_reason"])

    def load_ballers_board(directory, *, target_week=None, now=None):
        value = state["ballers"]
        if isinstance(value, Exception):
            raise value
        return value

    def load_rotoballer_waiver_board(**kwargs):
        value = state["roto"]
        if isinstance(value, Exception):
            raise value
        return value

    def load_boone_board(scoring, *, week=None, allow_fetch=None, **kwargs):
        value = state["boone"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ballers_module, "find_waiver_csv", find_waiver_csv)
    monkeypatch.setattr(ballers_module, "load_ballers_board", load_ballers_board)
    monkeypatch.setattr(roto_module, "load_rotoballer_waiver_board", load_rotoballer_waiver_board)
    monkeypatch.setattr(boone_module, "load_boone_board", load_boone_board)
    return state


def load(loaders_state=None, *, claim_week=2, scoring_needed=None, engine=None):
    return load_waiver_sources(
        ALL_PLAYERS, engine if engine is not None else engine_with(), claim_week=claim_week,
        scoring_needed=scoring_needed, allow_fetch=False, now=NOW,
    )


# -- FantasyPros ROS ------------------------------------------------------------------------


def test_fantasypros_pages_are_read_off_the_engine_and_never_refetched(loaders):
    sources = load(loaders)
    assert sources.status(FP_FULL_PAGE).label == LOADED
    assert sources.status(FP_FULL_PAGE).rows == 2
    assert sources.status(FP_FULL_PAGE).matched == 1  # only the real Sleeper player pins
    view = sources.fp_ros_for(1.0)["1"]
    assert (view.pos_rank, view.overall_rank, view.rank_std, view.page) == (41, 120, 4.5, FP_ROS_FULL)


def test_a_page_the_engine_did_not_load_is_recorded_unavailable(loaders):
    sources = load(loaders, engine=engine_with(pages=(FP_ROS_FULL,)))
    assert sources.status(FP_HALF_PAGE).label == UNAVAILABLE
    assert sources.status(FP_HALF_PAGE).detail == "not loaded this run"
    assert sources.fp_ros_for(0.5) == {}


def test_an_engine_with_no_snapshots_at_all_does_not_fail_the_run(loaders):
    sources = load(loaders, engine=SimpleNamespace())
    assert sources.status(FP_FULL_PAGE).label == UNAVAILABLE


@pytest.mark.parametrize("ppr,page", [(1.0, FP_ROS_FULL), (0.75, FP_ROS_FULL), (0.74, FP_ROS_HALF), (0.5, FP_ROS_HALF), (0.0, FP_ROS_HALF)])
def test_which_fantasypros_page_a_league_reads(ppr, page):
    assert fp_ros_page_for(ppr) == page


def test_fp_ros_for_picks_the_page_that_matches_the_leagues_ppr(loaders):
    sources = load(loaders)
    assert sources.fp_ros_for(1.0)["1"].page == FP_ROS_FULL
    assert sources.fp_ros_for(0.5)["1"].page == FP_ROS_HALF


def test_a_row_with_an_unparseable_positional_rank_keeps_the_rest(loaders):
    engine = engine_with(pages=(FP_ROS_FULL,))
    engine.fp_snapshots[FP_ROS_FULL].payload[0]["pos_rank"] = "—"
    sources = load(loaders, engine=engine)
    assert sources.fp_ros_for(1.0)["1"].pos_rank is None
    assert sources.fp_ros_for(1.0)["1"].overall_rank == 120


# -- the Fantasy Footballers CSV ------------------------------------------------------------------


def test_the_ballers_board_is_pinned_to_sleeper_ids(loaders):
    sources = load(loaders)
    assert sources.has(BALLERS) is True
    assert sources.ballers_by_id["1"].rank == 1
    status = sources.status(BALLERS)
    assert (status.label, status.week, status.rows, status.matched) == (LOADED, 2, 2, 1)
    assert sources.unmatched[BALLERS] == ["Ghost Player"]
    assert status.describe() == "Fantasy Footballers · Loaded · week 2 · 1/2 players matched · ballers_week2.csv"


def test_no_csv_at_all_reads_not_provided(loaders):
    loaders["ballers"] = None
    loaders["ballers_reason"] = "no Fantasy Footballers waiver CSV in data/manual"
    sources = load(loaders)
    assert sources.status(BALLERS).label == NOT_PROVIDED
    assert sources.has(BALLERS) is False


def test_a_csv_for_another_week_reads_wrong_week(loaders):
    loaders["ballers"] = None
    loaders["ballers_reason"] = "newest Ballers board is for week 1, not week 2"
    sources = load(loaders)
    assert sources.status(BALLERS).label == WRONG_WEEK
    assert "not week 2" in sources.status(BALLERS).detail


def test_a_broken_manual_file_never_fails_the_run(loaders):
    loaders["ballers"] = ValueError("bad header")
    sources = load(loaders)
    assert sources.status(BALLERS).label == UNAVAILABLE
    assert sources.status(BALLERS).detail == "could not be read: bad header"
    assert sources.ballers_by_id == {}
    assert sources.has(ROTOBALLER) is True  # the other sources still loaded


# -- the RotoBaller board ---------------------------------------------------------------------------


def test_the_rotoballer_board_is_pinned_to_sleeper_ids(loaders):
    sources = load(loaders)
    row = sources.rotoballer_by_id["2"]
    assert (row.rank, row.min_league_size) == (1, 12)
    assert sources.status(ROTOBALLER).label == LOADED


def test_no_current_rotoballer_board_is_unavailable_not_silence(loaders):
    loaders["roto"] = None
    sources = load(loaders)
    assert sources.status(ROTOBALLER).label == UNAVAILABLE
    assert sources.status(ROTOBALLER).detail == "no current board (fetch failed or not cached)"


def test_a_rotoballer_board_for_another_week_is_refused(loaders):
    loaders["roto"] = roto_board(week=1)
    sources = load(loaders)
    status = sources.status(ROTOBALLER)
    assert status.label == WRONG_WEEK and status.week == 1
    assert status.detail == "newest board is for week 1, not week 2"
    assert sources.rotoballer_by_id == {}


def test_a_board_that_does_not_state_its_week_is_still_used(loaders):
    loaders["roto"] = roto_board(week=None)
    assert load(loaders).has(ROTOBALLER) is True


def test_a_failing_rotoballer_fetch_never_propagates(loaders):
    loaders["roto"] = RuntimeError("HTTP 503")
    sources = load(loaders)
    assert sources.status(ROTOBALLER).label == UNAVAILABLE
    assert "HTTP 503" in sources.status(ROTOBALLER).detail


# -- Justin Boone -----------------------------------------------------------------------------------


def test_boone_is_only_loaded_for_the_scoring_variants_asked_for(loaders):
    none_asked = load(loaders)
    assert none_asked.status(BOONE_FULL) is None
    assert none_asked.boone_for(1.0) is None

    asked = load(loaders, scoring_needed={"ppr"})
    assert asked.status(BOONE_FULL).label == LOADED
    assert asked.boone_for(1.0) == {"3": 7}  # the RB list rank, never the FLEX one


def test_boone_counts_only_the_four_positional_lists(loaders):
    sources = load(loaders, scoring_needed={"ppr"})
    assert sources.status(BOONE_FULL).rows == 1  # the RB row; the FLEX and K rows are not positional ranks


def test_a_standard_league_gets_no_boone_board_at_all(loaders):
    sources = load(loaders, scoring_needed={"ppr"})
    assert sources.boone_for(0.0) is None
    assert sources.boone_for(0.5) is None  # the half-PPR variant was not asked for


def test_a_missing_boone_board_is_recorded_with_the_week_it_wanted(loaders):
    loaders["boone"] = None
    sources = load(loaders, scoring_needed={"ppr"})
    assert sources.status(BOONE_FULL).label == UNAVAILABLE
    assert sources.status(BOONE_FULL).detail == "no week 2 board (fetch failed, disabled, or not cached)"


def test_a_failing_boone_fetch_never_propagates(loaders):
    loaders["boone"] = RuntimeError("timeout")
    sources = load(loaders, scoring_needed={"ppr"})
    assert sources.status(BOONE_FULL).label == UNAVAILABLE
    assert sources.has(BALLERS) is True


def test_boone_half_ppr_is_its_own_source_line(loaders):
    sources = load(loaders, scoring_needed={"half_ppr"})
    assert sources.status(f"{BOONE} (half PPR)").label == LOADED
    assert sources.boone_for(0.5) == {"3": 7}


# -- the record itself -------------------------------------------------------------------------------


def test_status_and_has_for_a_source_that_was_never_asked_about(loaders):
    sources = load(loaders)
    assert sources.status("Nothing") is None
    assert sources.has("Nothing") is False


def test_the_claim_week_is_carried_on_the_record(loaders):
    assert load(loaders, claim_week=7).claim_week == 7
