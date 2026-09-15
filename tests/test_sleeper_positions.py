from __future__ import annotations

import pytest

from sleeper_tool.sleeper_positions import (
    FANTASY_POSITIONS,
    fantasy_position,
    fantasy_positions,
    normalize_position,
)


def player(position=None, fantasy=None) -> dict:
    return {"position": position, "fantasy_positions": fantasy}


def test_a_plain_player_is_the_position_he_is_listed_at():
    assert fantasy_position(player("WR", ["WR"])) == "WR"
    assert fantasy_positions(player("WR", ["WR"])) == {"WR"}


def test_a_two_way_player_is_eligible_at_the_fantasy_position_not_his_nfl_listing():
    # Travis Hunter is position "DB" — reading that alone dropped him out of
    # the name index, the free-agent universe and every depth calculation.
    hunter = player("DB", ["DB", "WR"])
    assert fantasy_position(hunter) == "WR"
    assert fantasy_positions(hunter) == {"WR"}


def test_a_fullback_is_a_running_back():
    assert fantasy_position(player("FB", ["RB"])) == "RB"


def test_a_player_with_no_fantasy_eligibility_at_all_is_none():
    assert fantasy_position(player("LB", ["LB"])) is None
    assert fantasy_positions(player("LB", ["LB"])) == set()
    assert fantasy_position(player(None, None)) is None
    assert fantasy_position({}) is None


def test_a_missing_fantasy_positions_list_falls_back_to_the_primary_listing():
    # Sleeper can return the key present but explicitly null.
    assert fantasy_position({"position": "TE", "fantasy_positions": None}) == "TE"
    assert fantasy_position({"position": "TE"}) == "TE"


@pytest.mark.parametrize(
    "raw,expected",
    [("DST", "DEF"), ("D/ST", "DEF"), ("PK", "K"), ("  wr ", "WR"), ("DEF", "DEF"), ("", None), (None, None)],
)
def test_source_spellings_normalize_to_sleepers_own_values(raw, expected):
    assert normalize_position(raw) == expected


def test_multi_eligibility_resolves_by_how_much_of_a_roster_the_position_occupies():
    assert fantasy_position(player("RB", ["RB", "WR"])) == "RB"
    assert fantasy_position(player("TE", ["TE", "WR"])) == "WR"
    assert fantasy_position(player("QB", ["QB", "TE"])) == "QB"


def test_every_preference_entry_is_a_real_fantasy_position():
    for pos in FANTASY_POSITIONS:
        assert fantasy_position(player(pos, [pos])) == pos
