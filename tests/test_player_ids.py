from sleeper_tool.nfl_usage import load_crosswalk_rows
from sleeper_tool.player_ids import (
    SOURCE_DEF_TEAM,
    SOURCE_DYNASTYPROCESS,
    SOURCE_NFLVERSE_NAME,
    SOURCE_SLEEPER_GSIS,
    build_crosswalk,
    by_gsis,
)
from sleeper_tool.rankings import cache as cache_mod
from usage_fixtures import fake_fetch

import pytest

SLEEPER_PLAYERS = {
    # Sleeper's own gsis id, with the leading space the real cache carries.
    "s1": {"player_id": "s1", "full_name": "Rise Receiver", "position": "WR", "team": "KC", "gsis_id": " 00-0000001"},
    "s2": {"player_id": "s2", "full_name": "Bye Back", "position": "RB", "team": "KC"},
    "s3": {"player_id": "s3", "first_name": "Traded", "last_name": "Wideout", "position": "WR", "team": "LAR"},
    # No gsis id anywhere in db_playerids: has to fall to name+position+team.
    "s4": {"player_id": "s4", "full_name": "No Snap Tight End", "position": "TE", "team": "LA"},
    "s7": {"player_id": "s7", "full_name": "Name Twin", "position": "WR", "team": "DAL"},
    "s10": {"player_id": "s10", "full_name": "Cut Player", "position": "WR", "team": "NYJ"},
    # Right name, wrong team: name alone must never be enough.
    "s11": {"player_id": "s11", "full_name": "Active Namematch", "position": "TE", "team": "KC"},
    "s99": {"player_id": "s99", "full_name": "Ghost Player", "position": "WR", "team": "SEA"},
    "HOU": {"player_id": "HOU", "full_name": "Houston Texans", "position": "DEF", "team": "HOU"},
}


@pytest.fixture
def rows(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path)
    return load_crosswalk_rows(fetch=fake_fetch)


def _build(rows, only_ids=None):
    ff_rows, nfl_rows = rows
    return build_crosswalk(SLEEPER_PLAYERS, ff_rows=ff_rows, nfl_rows=nfl_rows, only_ids=only_ids)


def test_the_ladder_runs_strongest_source_first(rows):
    crosswalk, report = _build(rows)

    # 1. Sleeper's own gsis id wins over the (deliberately wrong) db_playerids row.
    assert crosswalk["s1"].gsis_id == "00-0000001" and crosswalk["s1"].source == SOURCE_SLEEPER_GSIS
    assert crosswalk["s1"].pfr_id == "RiseR00"  # filled in from nflverse by gsis

    # 2. db_playerids by sleeper_id, which carries pfr too.
    assert crosswalk["s2"].source == SOURCE_DYNASTYPROCESS and crosswalk["s2"].pfr_id == "ByeBa00"

    # 3. name + position + team, ACT only.
    assert crosswalk["s4"].source == SOURCE_NFLVERSE_NAME and crosswalk["s4"].gsis_id == "00-0000004"

    assert report.matched_by_source[SOURCE_SLEEPER_GSIS] == 1
    assert report.matched_by_source[SOURCE_DYNASTYPROCESS] == 2  # s2 and s3


def test_a_db_playerids_row_without_a_gsis_id_does_not_short_circuit_the_ladder(rows):
    crosswalk, _ = _build(rows)
    # s4's db_playerids row has a pfr id but no gsis id; the name rung had to run.
    assert crosswalk["s4"].source == SOURCE_NFLVERSE_NAME


def test_ambiguous_name_hits_are_surfaced_not_guessed(rows):
    crosswalk, report = _build(rows)
    assert crosswalk["s7"].gsis_id is None
    assert crosswalk["s7"].candidates == ("00-0000007", "00-0000008")
    assert [a["sleeper_id"] for a in report.ambiguous] == ["s7"]
    assert any(u["reason"] == "ambiguous" for u in report.unmatched)


def test_name_alone_and_non_active_rows_never_match(rows):
    _, report = _build(rows)
    unmatched = {u["sleeper_id"] for u in report.unmatched}
    assert "s11" in unmatched  # name matches an ACT player, team does not
    assert "s10" in unmatched  # exact name+position+team, but status CUT
    assert "s99" in unmatched  # in no source at all


def test_team_defenses_map_to_themselves(rows):
    crosswalk, report = _build(rows)
    assert crosswalk["HOU"].source == SOURCE_DEF_TEAM and crosswalk["HOU"].gsis_id is None
    assert crosswalk["HOU"].matched and "HOU" in crosswalk["HOU"].note
    assert report.matched_by_source[SOURCE_DEF_TEAM] == 1


def test_only_ids_restricts_the_work_and_the_report(rows):
    crosswalk, report = _build(rows, only_ids={"s1", "s2"})
    assert set(crosswalk) == {"s1", "s2"} and report.total == 2 and not report.unmatched


def test_result_is_deterministic_and_reversible(rows):
    ff_rows, nfl_rows = rows
    first, _ = build_crosswalk(SLEEPER_PLAYERS, ff_rows=ff_rows, nfl_rows=nfl_rows)
    shuffled = dict(reversed(list(SLEEPER_PLAYERS.items())))
    second, _ = build_crosswalk(shuffled, ff_rows=list(reversed(ff_rows)), nfl_rows=list(reversed(nfl_rows)), only_ids=set(SLEEPER_PLAYERS))
    assert first == second
    assert by_gsis(first)["00-0000001"] == "s1"


def test_an_unknown_sleeper_id_is_unmatched_not_an_error(rows):
    ff_rows, nfl_rows = rows
    crosswalk, report = build_crosswalk({}, ff_rows=ff_rows, nfl_rows=nfl_rows, only_ids={"nobody"})
    assert crosswalk == {} and report.unmatched[0]["sleeper_id"] == "nobody"


def test_describe_summarises_without_naming_every_player(rows):
    _, report = _build(rows)
    text = report.describe()
    assert "matched" in text and "unmatched" in text and "Ghost Player" not in text
