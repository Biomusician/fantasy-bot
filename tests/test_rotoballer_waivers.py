from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import requests

from sleeper_tool.rankings import cache
from sleeper_tool.rankings import rotoballer_waivers as rbw

FIXTURES = Path(__file__).parent / "fixtures" / "rotoballer_waivers"
ARTICLE_URL = "https://www.rotoballer.com/waiver-wire-rankings-fantasy-football-week-2-2026/1930758"


def _article() -> str:
    return (FIXTURES / "week2_2026_article.html").read_text(encoding="utf-8")


def _index() -> str:
    return (FIXTURES / "waiver_wire_articles_index.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.last_fetch_outcome.pop(rbw.SOURCE, None)


def _write_cache(tmp_path: Path, payload: dict, age: dt.timedelta) -> None:
    fetched = dt.datetime.now(dt.timezone.utc) - age
    (tmp_path / f"{rbw.SOURCE}.json").write_text(
        json.dumps({"source": rbw.SOURCE, "fetched_at": fetched.isoformat(), "payload": payload}),
        encoding="utf-8",
    )


# --- parsing ---------------------------------------------------------------


def test_parses_combined_board_in_rank_order():
    payload = rbw.parse_waiver_board(_article(), url=ARTICLE_URL)
    rows = payload["rows"]
    assert payload["week"] == 2
    assert payload["url"] == ARTICLE_URL
    assert payload["problems"] == []
    # The unranked TE table below the combined board must not be appended.
    assert len(rows) == 85
    assert [r["rank"] for r in rows] == list(range(1, 86))
    assert rows[0] == {
        "rank": 1,
        "name": "Jalen Coker",
        "team": None,
        "position": "WR",
        "league_size_note": "All Leagues",
        "min_league_size": None,
    }
    assert rows[1]["name"] == "Wan'Dale Robinson"
    assert rows[2]["name"] == "Devaughn Vele"  # plain text cell, no player link
    assert rows[2]["league_size_note"] == "10+ Team PPR Leagues"
    assert rows[2]["min_league_size"] == 10


def test_league_size_tags_and_positions():
    rows = {r["name"]: r for r in rbw.parse_waiver_board(_article())["rows"]}
    assert rows["Khalil Shakir"]["min_league_size"] == 12
    assert rows["Khalil Shakir"]["league_size_note"] == "12+ Team PPR Leagues"
    assert rows["T.J. Hockenson"]["min_league_size"] == 14
    assert rows["T.J. Hockenson"]["position"] == "TE"
    assert rows["Jordan Love"]["position"] == "QB"
    assert rows["Jacoby Brissett"]["league_size_note"] == "2QB Leagues"
    assert rows["Jacoby Brissett"]["min_league_size"] is None
    assert rows["Baltimore Ravens"]["position"] == "DST"
    assert all(r["team"] is None for r in rows.values())


def test_payload_round_trips_through_board_from_snapshot():
    payload = json.loads(json.dumps(rbw.parse_waiver_board(_article(), url=ARTICLE_URL)))
    snap = cache.RankingSnapshot(source=rbw.SOURCE, fetched_at=dt.datetime.now(dt.timezone.utc), payload=payload)
    board = rbw.board_from_snapshot(snap)
    assert board is not None
    assert board.week == 2 and board.url == ARTICLE_URL
    assert board.rows[14] == rbw.WaiverBoardRow(15, "Kendre Miller", None, "RB", "12+ Team PPR Leagues", 12)


def test_zero_row_page_raises():
    with pytest.raises(rbw.RotoBallerWaiverFetchError):
        rbw.parse_waiver_board("<html><title>Week 2</title><table><tr><td>Player Name</td></tr></table></html>")
    with pytest.raises(rbw.RotoBallerWaiverFetchError):
        rbw.parse_waiver_board("<html><body><p>Prose only.</p></body></html>")


def test_bad_rows_are_reported_not_fatal():
    raw = (
        "<table><tr><td>Rank</td><td>Player Name</td><td>Position</td><td>Baller Move</td></tr>"
        "<tr><td>1</td><td>A Player</td><td>WR</td><td>Add in 12+ Team Leagues</td></tr>"
        "<tr><td>x</td><td>B Player</td><td>RB</td><td></td></tr></table>"
    )
    payload = rbw.parse_waiver_board(raw)
    assert [r["name"] for r in payload["rows"]] == ["A Player"]
    assert payload["week"] is None
    assert len(payload["problems"]) == 1


def test_discovery_picks_the_weekly_article_not_variants():
    assert rbw.find_latest_article_url(_index()) == ARTICLE_URL
    links = (
        '<a href="https://www.rotoballer.com/waiver-wire-rankings-fantasy-football-week-17-2025/1800000">'
        '<a href="https://www.rotoballer.com/waiver-wire-rankings-fantasy-football-week-3-2026/1940000">'
        '<a href="https://www.rotoballer.com/updated-waiver-wire-rankings-fantasy-football-week-9-2026/1999999">'
    )
    assert rbw.find_latest_article_url(links).endswith("week-3-2026/1940000")
    assert rbw.find_latest_article_url("<html></html>") is None


# --- loader ----------------------------------------------------------------


def test_fetch_payload_uses_index_then_article(monkeypatch):
    pages = {rbw.INDEX_URL: _index(), ARTICLE_URL: _article()}
    calls: list[str] = []

    def fake_get(url):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(rbw, "_get", fake_get)
    board = rbw.load_rotoballer_waiver_board()
    assert calls == [rbw.INDEX_URL, ARTICLE_URL]
    assert board is not None and len(board.rows) == 85 and board.week == 2
    assert cache.last_fetch_outcome[rbw.SOURCE] == "fresh"


def test_fresh_cache_served_without_fetching(monkeypatch, tmp_path):
    _write_cache(tmp_path, rbw.parse_waiver_board(_article(), url=ARTICLE_URL), dt.timedelta(hours=2))

    def boom():
        raise AssertionError("fetch should not run on a fresh cache")

    monkeypatch.setattr(rbw, "_fetch_payload", boom)
    board = rbw.load_rotoballer_waiver_board()
    assert board is not None and board.rows[0].name == "Jalen Coker"
    assert cache.last_fetch_outcome[rbw.SOURCE] == "cached"


def test_fetch_failure_without_cache_returns_none(monkeypatch):
    def fail():
        raise rbw.RotoBallerWaiverFetchError("down")

    monkeypatch.setattr(rbw, "_fetch_payload", fail)
    assert rbw.load_rotoballer_waiver_board() is None


def test_real_fetch_path_failure_returns_none():
    # requests.get is patched to raise; the loader must swallow it.
    assert rbw.load_rotoballer_waiver_board(force=True) is None


def test_stale_cache_within_ceiling_is_fallback(monkeypatch, tmp_path):
    _write_cache(tmp_path, rbw.parse_waiver_board(_article()), dt.timedelta(days=2))
    monkeypatch.setattr(rbw, "_fetch_payload", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    board = rbw.load_rotoballer_waiver_board()
    assert board is not None
    assert cache.last_fetch_outcome[rbw.SOURCE] == "fallback"


def test_cache_past_ceiling_is_none(monkeypatch, tmp_path):
    _write_cache(tmp_path, rbw.parse_waiver_board(_article()), dt.timedelta(days=8))
    monkeypatch.setattr(rbw, "_fetch_payload", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert rbw.load_rotoballer_waiver_board() is None
    assert rbw.load_rotoballer_waiver_board(allow_fetch=False) is None


def test_cache_only_mode(monkeypatch, tmp_path):
    def boom():
        raise AssertionError("allow_fetch=False must not fetch")

    monkeypatch.setattr(rbw, "_fetch_payload", boom)
    assert rbw.load_rotoballer_waiver_board(allow_fetch=False) is None
    # Stale-but-within-ceiling cache is served in cache-only mode.
    _write_cache(tmp_path, rbw.parse_waiver_board(_article()), dt.timedelta(days=3))
    board = rbw.load_rotoballer_waiver_board(allow_fetch=False)
    assert board is not None and len(board.rows) == 85


def test_corrupt_cache_shape_returns_none(tmp_path):
    _write_cache(tmp_path, {"rows": [{"unexpected": 1}]}, dt.timedelta(hours=1))
    assert rbw.load_rotoballer_waiver_board(allow_fetch=False) is None
