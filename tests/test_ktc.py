"""KTC page parsing against trimmed copies of the real page, both shapes.

`dynasty_rankings_script_tag.html` is the September 2026 layout (players in a
`<script type="application/json" id="ktc-players">` tag); the
`dynasty_rankings_players_array.html` one is the earlier inline
`var playersArray = [...]` literal. Each holds three real records (QB, TE,
WR) with the history arrays emptied to keep the files small.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sleeper_tool.rankings import ktc
from sleeper_tool.rankings.ktc import KTCFetchError, parse_ktc_players

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ktc"
FORMAT_KEYS = (
    "one_qb", "superflex",
    "one_qb_tep", "one_qb_tepp", "one_qb_teppp",
    "superflex_tep", "superflex_tepp", "superflex_teppp",
)


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _by_name(players: list[dict]) -> dict[str, dict]:
    return {p["name"]: p for p in players}


def test_script_tag_page_parses_into_the_payload_shape():
    players = parse_ktc_players(_fixture("dynasty_rankings_script_tag.html"))
    assert [p["name"] for p in players] == ["Josh Allen", "Brock Bowers", "Ja'Marr Chase"]
    for p in players:
        assert set(p) == {"name", "position", "team", "age", "is_rookie", *FORMAT_KEYS}
        for key in FORMAT_KEYS:
            assert set(p[key]) == {"value", "rank", "positional_rank"}

    allen = _by_name(players)["Josh Allen"]
    assert (allen["position"], allen["team"], allen["age"], allen["is_rookie"]) == ("QB", "BUF", 30.3, False)
    assert allen["superflex"] == {"value": 9992, "rank": 3, "positional_rank": 1}
    assert allen["one_qb"] == {"value": 7450, "rank": 11, "positional_rank": 1}

    # TE premium is carried per block: a TE climbs with each tier.
    bowers = _by_name(players)["Brock Bowers"]
    assert bowers["superflex"]["value"] == 7965
    assert [bowers[k]["value"] for k in ("superflex_tep", "superflex_tepp", "superflex_teppp")] == [8816, 9634, 9999]
    assert [bowers[k]["value"] for k in ("one_qb_tep", "one_qb_tepp", "one_qb_teppp")] == [8974, 9807, 9999]


def test_old_players_array_page_still_parses():
    players = parse_ktc_players(_fixture("dynasty_rankings_players_array.html"))
    by_name = _by_name(players)
    assert set(by_name) == {"Josh Allen", "Brock Bowers", "Ja'Marr Chase"}
    assert by_name["Josh Allen"]["superflex"] == {"value": 9958, "rank": 4, "positional_rank": 1}
    assert by_name["Brock Bowers"]["superflex_tepp"] == {"value": 9999, "rank": 1, "positional_rank": 1}


def test_script_tag_wins_over_a_players_array_literal():
    new_page = _fixture("dynasty_rankings_script_tag.html")
    old_page = _fixture("dynasty_rankings_players_array.html")
    players = parse_ktc_players(new_page + old_page)
    assert _by_name(players)["Josh Allen"]["superflex"]["value"] == 9992


def test_page_with_neither_shape_raises_and_says_what_was_served():
    page = "<html><head><title>Just a moment...</title></head><body>checking your browser</body></html>"
    with pytest.raises(KTCFetchError) as exc:
        parse_ktc_players(page)
    message = str(exc.value)
    assert "ktc-players" in message and "playersArray" in message
    assert "Just a moment..." in message
    assert f"{len(page)} chars" in message


def test_empty_script_tag_without_a_literal_raises():
    page = '<script type="application/json" id="ktc-players">  </script>'
    with pytest.raises(KTCFetchError, match="Could not find player data"):
        parse_ktc_players(page)


def test_malformed_script_tag_json_raises():
    page = '<script type="application/json" id="ktc-players">[{"playerName": </script>'
    with pytest.raises(KTCFetchError, match="ktc-players script tag JSON"):
        parse_ktc_players(page)


def test_records_without_value_blocks_parse_to_zero_players_and_raise():
    page = '<script type="application/json" id="ktc-players">[{"playerName": "Nobody"}]</script>'
    with pytest.raises(KTCFetchError, match="Parsed 0 players"):
        parse_ktc_players(page)


def test_fetch_and_parse_surfaces_the_layout_error(monkeypatch):
    monkeypatch.setattr(ktc, "fetch_ktc_html", lambda: "<html><title>KeepTradeCut</title></html>")
    with pytest.raises(KTCFetchError, match="Could not find player data"):
        ktc._fetch_and_parse()
