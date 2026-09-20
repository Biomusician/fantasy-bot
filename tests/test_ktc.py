"""KTC page parsing against trimmed copies of the real page, in every shape
KTC has served, plus the ways a page can be served and still not be a board.

`dynasty_rankings_script_tag.html` is the September 2026 layout (players in a
`<script type="application/json" id="ktc-players">` tag); the
`dynasty_rankings_players_array.html` one is the earlier inline
`var playersArray = [...]` literal; `dynasty_rankings_rendered_markup.html`
is the visible ranking rows. Each holds three records with the history
arrays emptied to keep the files small.

Two layers are tested separately on purpose. `parse_ktc` answers "what shape
is this page, and what is in it" and is happy with three players — that is
what the diagnostics command needs. `parse_ktc_players` is the production
path and additionally demands a whole board, which three players are not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sleeper_tool.rankings import ktc, ktc_parser
from sleeper_tool.rankings.ktc import KTCFetchError, parse_ktc_players
from sleeper_tool.rankings.ktc_parser import (
    EMBEDDED_JSON_V1,
    EMBEDDED_JSON_V2,
    MIN_PLAYERS,
    RENDERED_MARKUP,
    KTCParseError,
    KtcParse,
    parse_ktc,
    validate_parse,
)

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


def board(rows: int = MIN_PLAYERS, *, strategy: str = EMBEDDED_JSON_V2, complete: bool = True) -> KtcParse:
    """A synthetic board that passes validation, for tests that want to break
    exactly one thing about it."""
    positions = ("QB", "RB", "WR", "TE")
    players = [
        {
            "name": f"Player {i}",
            "position": positions[i % len(positions)],
            "team": "KC",
            "age": 25.0,
            "is_rookie": False,
            **{key: {"value": 5000 - i, "rank": i + 1, "positional_rank": i + 1} for key in FORMAT_KEYS},
        }
        for i in range(rows)
    ]
    return KtcParse(players=players, strategy=strategy, complete=complete)


# -- the shapes KTC has served -------------------------------------------------------------


def test_script_tag_page_parses_into_the_payload_shape():
    parse = parse_ktc(_fixture("dynasty_rankings_script_tag.html"))
    assert parse.strategy == EMBEDDED_JSON_V2 and parse.complete is True
    players = parse.players
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
    parse = parse_ktc(_fixture("dynasty_rankings_players_array.html"))
    assert parse.strategy == EMBEDDED_JSON_V1 and parse.complete is True
    by_name = _by_name(parse.players)
    assert set(by_name) == {"Josh Allen", "Brock Bowers", "Ja'Marr Chase"}
    assert by_name["Josh Allen"]["superflex"] == {"value": 9958, "rank": 4, "positional_rank": 1}
    assert by_name["Brock Bowers"]["superflex_tepp"] == {"value": 9999, "rank": 1, "positional_rank": 1}


def test_the_live_shape_wins_over_a_players_array_literal():
    both = _fixture("dynasty_rankings_script_tag.html") + _fixture("dynasty_rankings_players_array.html")
    parse = parse_ktc(both)
    assert parse.strategy == EMBEDDED_JSON_V2
    assert _by_name(parse.players)["Josh Allen"]["superflex"]["value"] == 9992


def test_a_json_parse_reaching_for_the_literal_survives_the_new_json_parse_call():
    # This is the break that took the source down: KTC replaced the literal
    # with `var playersArray = JSON.parse(document.getElementById(...))`, so
    # a parser matching only the literal finds nothing on a healthy page.
    page = _fixture("dynasty_rankings_script_tag.html").replace(
        "</body>", "<script>var playersArray = JSON.parse(document.getElementById('ktc-players').textContent);</script></body>"
    )
    assert ktc_parser._PLAYERS_ARRAY_RE.search(page) is None
    assert parse_ktc(page).strategy == EMBEDDED_JSON_V2


# -- the rendered markup is a fallback that is never a snapshot -----------------------------


def test_rendered_markup_parses_but_is_marked_incomplete():
    parse = parse_ktc(_fixture("dynasty_rankings_rendered_markup.html"))
    assert parse.strategy == RENDERED_MARKUP and parse.complete is False
    assert [p["name"] for p in parse.players] == ["First Example", "Second Example", "Third Example"]
    first = parse.players[0]
    assert (first["position"], first["team"]) == ("QB", "BUF")
    assert first["superflex"] == {"value": 9999, "rank": 1, "positional_rank": 1}
    assert parse.notes and "diagnostic only" in parse.notes[0]


def test_an_incomplete_parse_is_refused_however_many_rows_it_has():
    # The markup shows one page of rows and one format. Caching it would not
    # read as a failure downstream; it would read as a league where most
    # rostered players are suddenly worthless.
    with pytest.raises(KTCParseError, match="cannot produce a full board"):
        validate_parse(board(rows=MIN_PLAYERS * 2, strategy=RENDERED_MARKUP, complete=False))


def test_the_production_path_refuses_the_markup_fallback():
    with pytest.raises(KTCFetchError, match="cannot produce a full board"):
        parse_ktc_players(_fixture("dynasty_rankings_rendered_markup.html"))


# -- pages that are not boards ---------------------------------------------------------------


def test_a_challenge_page_is_named_as_one_rather_than_read_as_an_empty_board():
    page = "<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing</body></html>"
    with pytest.raises(KTCParseError) as exc:
        parse_ktc(page)
    assert "challenge page" in str(exc.value)
    assert "Just a moment..." in str(exc.value)


@pytest.mark.parametrize(
    "marker",
    ["Just a moment", "Checking your browser", "cf-browser-verification", "Access Denied", "Attention Required"],
)
def test_every_known_wall_marker_is_recognised(marker):
    assert ktc_parser.looks_like_challenge(f"<html><body>{marker}</body></html>") is True


def test_a_page_with_no_board_at_all_says_what_was_served():
    page = "<html><head><title>KeepTradeCut</title></head><body><p>Maintenance</p></body></html>"
    with pytest.raises(KTCParseError) as exc:
        parse_ktc(page)
    message = str(exc.value)
    assert "No KTC player data found by any strategy" in message
    assert "KeepTradeCut" in message and f"{len(page)} chars" in message


def test_an_empty_body_raises_rather_than_returning_nothing():
    with pytest.raises(KTCParseError, match="No KTC player data"):
        parse_ktc("")


def test_an_empty_script_tag_falls_through_to_the_next_strategy():
    page = '<script type="application/json" id="ktc-players">  </script>'
    with pytest.raises(KTCParseError, match="No KTC player data"):
        parse_ktc(page)


def test_malformed_embedded_json_is_a_parse_failure_not_an_empty_board():
    page = '<script type="application/json" id="ktc-players">[{"playerName": </script>'
    with pytest.raises(KTCParseError, match="not valid JSON"):
        parse_ktc(page)


def test_embedded_json_that_is_not_a_list_is_rejected():
    page = '<script type="application/json" id="ktc-players">{"players": []}</script>'
    with pytest.raises(KTCParseError, match="is dict, not a list"):
        parse_ktc(page)


def test_records_without_value_blocks_are_dropped_and_counted():
    payload = json.dumps([{"playerName": "Nobody"}, {"playerName": "Nobody Else"}])
    parse = parse_ktc(f'<script type="application/json" id="ktc-players">{payload}</script>')
    assert parse.rows == 0
    assert parse.notes == ["2 of 2 records carried no value blocks"]
    with pytest.raises(KTCParseError, match="yielded 0 players"):
        validate_parse(parse)


def test_a_partial_response_is_a_parse_failure_either_way():
    # Cut short mid-download the closing tag is gone, so no strategy matches
    # at all; cut short inside a closed tag the JSON itself is broken.
    full = _fixture("dynasty_rankings_script_tag.html")
    with pytest.raises(KTCParseError, match="No KTC player data"):
        parse_ktc(full[: len(full) // 2])

    opening = full[: full.index(">", full.index("id=\"ktc-players\"")) + 1]
    body = full[len(opening):]
    with pytest.raises(KTCParseError, match="not valid JSON"):
        parse_ktc(opening + body[: len(body) // 2] + "</script>")


# -- validation: the ways a parse can succeed and still be wrong ------------------------------


def test_a_whole_board_passes_validation():
    validate_parse(board())


def test_a_board_smaller_than_the_floor_is_a_parse_failure_not_a_smaller_board():
    with pytest.raises(KTCParseError) as exc:
        validate_parse(board(rows=MIN_PLAYERS - 1))
    assert "a parse failure, not a smaller board" in str(exc.value)
    assert str(MIN_PLAYERS) in str(exc.value)


def test_a_board_missing_a_whole_position_parsed_the_wrong_object():
    parse = board()
    kept = [p for p in parse.players if p["position"] != "TE"]
    with pytest.raises(KTCParseError, match="has no TE"):
        validate_parse(KtcParse(players=kept + kept[: len(parse.players) - len(kept)], strategy=EMBEDDED_JSON_V2, complete=True))


def test_nameless_rows_are_rejected():
    parse = board()
    parse.players[3]["name"] = ""
    with pytest.raises(KTCParseError, match="1 nameless rows"):
        validate_parse(parse)


def test_a_board_that_is_mostly_one_repeated_row_is_rejected():
    parse = board()
    for p in parse.players:
        p["name"] = "Same Guy"
    with pytest.raises(KTCParseError, match="duplicate names"):
        validate_parse(parse)


def test_a_handful_of_duplicate_names_is_tolerated():
    parse = board()
    parse.players[1]["name"] = parse.players[0]["name"]
    validate_parse(parse)


def test_a_board_of_zeroes_found_the_right_keys_in_the_wrong_objects():
    parse = board()
    for p in parse.players:
        p["superflex"] = {"value": 0, "rank": 0, "positional_rank": 0}
    with pytest.raises(KTCParseError, match="with a value above zero"):
        validate_parse(parse)


def test_values_outside_ktcs_own_scale_are_rejected():
    parse = board()
    parse.players[0]["superflex"]["value"] = 99999
    with pytest.raises(KTCParseError, match="outside 0-9999"):
        validate_parse(parse)


# -- the cache-write gate ----------------------------------------------------------------------


def test_the_cache_gate_accepts_a_whole_board_and_refuses_anything_less():
    # True accepts; anything else is the reason it was refused, which is
    # what gets recorded and read the next morning.
    assert ktc.valid_ktc_payload(board().players) is True
    assert "fewer than the" in ktc.valid_ktc_payload(board(rows=3).players)
    assert "yielded 0 players" in ktc.valid_ktc_payload([])
    assert "NoneType, not a list" in ktc.valid_ktc_payload(None)
    assert "dict, not a list" in ktc.valid_ktc_payload({"players": []})
    assert ktc.valid_ktc_payload([{"name": "no value blocks"}]) is not True


# -- the production entry point ------------------------------------------------------------------


def test_fetch_and_parse_surfaces_the_layout_error_as_a_fetch_error(monkeypatch):
    monkeypatch.setattr(ktc, "fetch_ktc_html", lambda: "<html><title>KeepTradeCut</title></html>")
    with pytest.raises(KTCFetchError, match="No KTC player data found by any strategy"):
        ktc._fetch_and_parse()


def test_diagnostics_reports_a_bad_page_instead_of_raising():
    parse, reason = ktc.parse_for_diagnostics("<html><title>KeepTradeCut</title></html>")
    assert parse is None and "No KTC player data" in reason

    parse, reason = ktc.parse_for_diagnostics(_fixture("dynasty_rankings_script_tag.html"))
    assert reason is None and parse.strategy == EMBEDDED_JSON_V2 and parse.rows == 3
