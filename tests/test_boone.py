from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import requests

from sleeper_tool.rankings import boone, cache
from sleeper_tool.rankings.boone import (
    FULL_PPR,
    HALF_PPR,
    BooneBoard,
    BooneFetchError,
    BooneRow,
    load_boone_board,
    parse_boone_page,
    scoring_for_ppr,
)

FIXTURES = Path(__file__).parent / "fixtures" / "boone"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, tmp_path):
    def _refuse(*args, **kwargs):
        raise AssertionError("test tried to reach the network")

    monkeypatch.setattr(requests, "get", _refuse)
    monkeypatch.setattr(boone.requests, "get", _refuse)
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "rankings_cache")
    monkeypatch.setattr(boone, "_REQUEST_SPACING", 0)


# --- scoring mapping -------------------------------------------------------


@pytest.mark.parametrize(
    "ppr, expected",
    [
        (0, None),
        (0.24, None),
        (0.25, HALF_PPR),
        (0.5, HALF_PPR),
        (0.74, HALF_PPR),
        (0.75, FULL_PPR),
        (1.0, FULL_PPR),
        (1.5, FULL_PPR),
    ],
)
def test_scoring_for_ppr_boundaries(ppr, expected):
    assert scoring_for_ppr(ppr) == expected


# --- parsing real (trimmed) responses --------------------------------------


def test_parse_rb_list():
    parsed = parse_boone_page(_fixture("rb_half_week2.json"), url="u")
    assert parsed["week"] == 2
    assert parsed["position_list"] == "RB"
    assert parsed["problems"] == []
    top = parsed["rows"][:3]
    assert [(r["rank"], r["name"], r["team"], r["position"]) for r in top] == [
        (1, "Jahmyr Gibbs", "DET", "RB"),
        (2, "Bijan Robinson", "ATL", "RB"),
        (3, "Kenneth Walker III", "KC", "RB"),
    ]
    assert len(parsed["rows"]) == 12
    json.dumps(parsed)  # must stay cacheable


def test_parse_flex_list_keeps_each_players_own_position():
    parsed = parse_boone_page(_fixture("flex_half_week2.json"))
    assert parsed["position_list"] == "FLEX"
    by_rank = {r["rank"]: r for r in parsed["rows"]}
    assert by_rank[4]["name"] == "Puka Nacua"
    assert by_rank[4]["position"] == "WR"
    assert by_rank[1]["position"] == "RB"
    assert {r["position_list"] for r in parsed["rows"]} == {"FLEX"}


def test_parse_dst_list_normalizes_team_codes():
    parsed = parse_boone_page(_fixture("dst_half_week2.json"))
    assert parsed["rows"][0]["name"] == "Philadelphia Eagles"
    assert parsed["rows"][0]["team"] == "PHI"


def test_parse_rejects_preseason_draft_board():
    # Omitting `week` from the request returns this: rows exist, but it is
    # not a weekly opinion and must not be treated as one.
    with pytest.raises(BooneFetchError, match="not weekly"):
        parse_boone_page(_fixture("qb_draft_week0.json"))


def test_parse_zero_rows_raises():
    data = json.loads(_fixture("rb_half_week2.json"))
    data["players"] = []
    with pytest.raises(BooneFetchError, match="0 rows"):
        parse_boone_page(json.dumps(data))


def test_parse_non_json_raises():
    with pytest.raises(BooneFetchError):
        parse_boone_page("<html>Access denied</html>")


def test_parse_rejects_other_expert():
    data = json.loads(_fixture("rb_half_week2.json"))
    data["expert_names"] = {"317": "Someone Else"}
    with pytest.raises(BooneFetchError, match="Justin Boone"):
        parse_boone_page(json.dumps(data))


def test_parse_accepts_jsonp_wrapper():
    parsed = parse_boone_page("FPW.rankingsCB(" + _fixture("rb_half_week2.json") + ");")
    assert parsed["rows"][0]["name"] == "Jahmyr Gibbs"


# --- board lookup ------------------------------------------------------------


def _board(rows):
    return BooneBoard(scoring=HALF_PPR, week=2, fetched_at=None, rows=rows, urls=[], problems=[])


def test_positional_rank_normalizes_names_and_ignores_flex():
    board = _board(
        [
            BooneRow("FLEX", 30, "Deebo Samuel Sr.", "WAS", "WR"),
            BooneRow("WR", 18, "Deebo Samuel Sr.", "WAS", "WR"),
            BooneRow("DST", 1, "Philadelphia Eagles", "PHI", "DST"),
            BooneRow("DST", 12, "Jacksonville Jaguars", "JAX", "DST"),
        ]
    )
    assert board.positional_rank("Deebo Samuel", "WR") == 18
    assert board.positional_rank("deebo samuel sr", "wr") == 18
    assert board.positional_rank("Deebo Samuel", "RB") is None
    assert board.positional_rank("Nobody", "WR") is None
    # Sleeper calls it DEF and names it by team code.
    assert board.positional_rank("PHI", "DEF") == 1
    assert board.positional_rank("JAC", "DEF") == 12


# --- loader ------------------------------------------------------------------


def test_loader_returns_none_when_fetch_fails_and_no_cache(monkeypatch):
    def _boom(*args, **kwargs):
        raise BooneFetchError("endpoint down")

    monkeypatch.setattr(boone, "fetch_boone_list", _boom)
    assert load_boone_board(HALF_PPR, week=2, allow_fetch=True) is None


def test_loader_never_raises_on_unexpected_errors(monkeypatch):
    def _boom(*args, **kwargs):
        raise ValueError("something odd")

    monkeypatch.setattr(boone, "load_snapshot", _boom)
    assert load_boone_board(HALF_PPR, week=2, allow_fetch=True) is None


def test_loader_without_week_cannot_fetch():
    # The real requests.get is patched to fail the test if reached.
    assert load_boone_board(HALF_PPR, allow_fetch=True) is None


def test_loader_no_fetch_and_empty_cache_returns_none(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(boone, "fetch_boone_payload", _boom)
    assert load_boone_board(HALF_PPR, week=2, allow_fetch=False) is None


def test_loader_rejects_unsupported_scoring():
    assert load_boone_board("standard", week=2, allow_fetch=True) is None


def _cache_board(week=2, rows=None):
    cache.save_snapshot(
        f"boone_{HALF_PPR}",
        {
            "scoring": HALF_PPR,
            "week": week,
            "season": 2026,
            "rows": rows or [{"position_list": "RB", "rank": 1, "name": "Jahmyr Gibbs", "team": "DET", "position": "RB"}],
            "urls": ["https://example.invalid/rb"],
            "problems": [],
        },
    )


def test_loader_serves_fresh_cache_without_fetching(monkeypatch):
    _cache_board()

    def _boom(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(boone, "fetch_boone_payload", _boom)
    monkeypatch.setattr(boone, "fetch_boone_list", _boom)
    board = load_boone_board(HALF_PPR, week=2, allow_fetch=True)
    assert board is not None
    assert board.week == 2
    assert board.positional_rank("Jahmyr Gibbs", "RB") == 1
    assert board.fetched_at is not None
    # And without fetching allowed at all.
    assert load_boone_board(HALF_PPR, week=2, allow_fetch=False) is not None


def test_loader_refuses_cached_board_from_another_week(monkeypatch):
    _cache_board(week=1)

    def _boom(*args, **kwargs):
        raise BooneFetchError("endpoint down")

    monkeypatch.setattr(boone, "fetch_boone_list", _boom)
    assert load_boone_board(HALF_PPR, week=2, allow_fetch=True) is None
    assert load_boone_board(HALF_PPR, week=2, allow_fetch=False) is None


def test_loader_fetches_every_list_and_keeps_partial_failures(monkeypatch):
    calls = []

    def _fake_list(position_list, scoring, week, season):
        calls.append((position_list, scoring, week, season))
        if position_list == "TE":
            raise BooneFetchError("tight ends down")
        return f"https://example.invalid/{position_list}", _fixture("rb_half_week2.json")

    monkeypatch.setattr(boone, "fetch_boone_list", _fake_list)
    board = load_boone_board(HALF_PPR, week=2, season=2026, allow_fetch=True)
    assert board is not None
    # Only the skill-position lists the waiver evidence reads are requested.
    assert [c[0] for c in calls] == list(boone.FETCHED_POSITIONS)
    assert all(c[1:] == (HALF_PPR, 2, 2026) for c in calls)
    assert any(p.startswith("TE:") for p in board.problems)
    assert board.positional_rank("Jahmyr Gibbs", "RB") == 1
    assert len(board.urls) == len(boone.FETCHED_POSITIONS) - 1
    # A second call inside max_age comes from the cache.
    calls.clear()
    assert load_boone_board(HALF_PPR, week=2, allow_fetch=True) is not None
    assert calls == []


def test_fetch_payload_drops_list_for_wrong_week(monkeypatch):
    def _fake_list(position_list, scoring, week, season):
        return "u", _fixture("rb_half_week2.json")

    monkeypatch.setattr(boone, "fetch_boone_list", _fake_list)
    with pytest.raises(BooneFetchError, match="No Boone"):
        boone.fetch_boone_payload(HALF_PPR, 3, 2026)


def test_list_url_pins_expert_week_and_scoring():
    url = boone._list_url("FLEX", FULL_PPR, 2, 2026)
    assert "position=FLX" in url and "scoring=PPR" in url and "week=2" in url
    assert "filters=317" in url and "year=2026" in url


def test_default_season_rolls_back_in_january():
    assert boone._default_season(dt.date(2027, 1, 10)) == 2026
    assert boone._default_season(dt.date(2026, 9, 15)) == 2026
