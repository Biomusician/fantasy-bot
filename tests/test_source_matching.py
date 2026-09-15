from __future__ import annotations

from sleeper_tool.rankings.source_matching import (
    SourceName,
    build_sleeper_name_index,
    match_source_names,
)


def _p(full_name, position, team, status="Active"):
    return {"full_name": full_name, "position": position, "team": team, "status": status}


PLAYERS = {
    "100": _p("Jalen Coker", "WR", "CAR"),
    "5872": _p("Deebo Samuel", "WR", "SF"),
    "200": _p("Juwan Johnson", "TE", "NO"),
    # Two active players with the same name on different teams.
    "301": _p("Mike Williams", "WR", "NYJ"),
    "302": _p("Mike Williams", "WR", "PIT"),
    # Same name, same team, both active: nothing can break the tie.
    "401": _p("Josh Allen", "QB", "BUF"),
    "402": _p("Josh Allen", "QB", "BUF"),
    # Same name, one retired free agent.
    "501": _p("Chris Brooks", "RB", "GB"),
    "502": _p("Chris Brooks", "RB", None, status="Inactive"),
    "600": _p("Brenton Strange", "TE", "JAX"),
    "700": _p("Kyren Williams", "RB", "LAR"),
    "800": _p("Terry McLaurin", "WR", "WAS"),
    "900": _p("Some Lineman", "OT", "SF"),
    "DEN": {"full_name": None, "first_name": "Denver", "last_name": "Broncos", "position": "DEF", "team": "DEN"},
    "LAR": {"first_name": "Los Angeles", "last_name": "Rams", "position": "DEF", "team": "LAR"},
    "SF": {"first_name": "San Francisco", "last_name": "49ers", "position": "DEF", "team": "SF"},
    "910": {"first_name": "Taysom", "last_name": "Hill", "position": "TE", "team": "NO", "status": "Active"},
}


def test_index_only_fantasy_positions_and_first_last_fallback():
    index = build_sleeper_name_index(PLAYERS)
    assert "some lineman" not in index
    assert index["denver broncos"] == ["DEN"]
    assert index["taysom hill"] == ["910"]
    assert index["mike williams"] == ["301", "302"]


def test_exact_match():
    result = match_source_names([SourceName("Jalen Coker", "CAR")], PLAYERS)
    assert result.matched == {0: "100"}
    assert not result.unmatched and not result.ambiguous and not result.team_mismatches
    assert result.describe() == "1/1 matched"


def test_suffix_variant():
    result = match_source_names([SourceName("Deebo Samuel Sr.", "SF", "WR")], PLAYERS)
    assert result.matched == {0: "5872"}


def test_team_mismatch_still_matched_and_recorded():
    row = SourceName("Juwan Johnson", "MIA")
    result = match_source_names([row], PLAYERS)
    assert result.matched == {0: "200"}
    assert result.team_mismatches == [(row, "200", "NO")]
    assert "1 team mismatch (Juwan Johnson: CSV MIA, Sleeper NO)" in result.describe()


def test_team_mismatch_describes_free_agent():
    players = {"1": _p("Free Guy", "WR", None)}
    result = match_source_names([SourceName("Free Guy", "NO")], players)
    assert result.describe() == "1/1 matched; 1 team mismatch (Free Guy: CSV NO, Sleeper FA)"


def test_same_name_broken_by_team():
    result = match_source_names([SourceName("Mike Williams", "PIT"), SourceName("Mike Williams", "NYJ")], PLAYERS)
    assert result.matched == {0: "302", 1: "301"}


def test_same_name_broken_by_active_status():
    result = match_source_names([SourceName("Chris Brooks")], PLAYERS)
    assert result.matched == {0: "501"}


def test_ambiguous_unresolvable():
    row = SourceName("Josh Allen", "BUF", "QB")
    result = match_source_names([row], PLAYERS)
    assert result.matched == {}
    assert result.ambiguous == [row]
    # A team the source gives that matches neither also can't break the tie.
    assert match_source_names([SourceName("Mike Williams", "KC")], PLAYERS).ambiguous


def test_unmatched():
    row = SourceName("Nobody Real", "SF")
    result = match_source_names([row, SourceName("Jalen Coker", "CAR")], PLAYERS)
    assert result.unmatched == [row]
    assert result.describe() == "1/2 matched; 1 unmatched (Nobody Real)"


def test_position_filter_falls_back_when_label_disagrees():
    # A source listing Taysom Hill as QB should still find the Sleeper TE.
    result = match_source_names([SourceName("Taysom Hill", "NO", "QB")], PLAYERS)
    assert result.matched == {0: "910"}


def test_def_by_full_team_name():
    result = match_source_names([SourceName("Denver Broncos")], PLAYERS)
    assert result.matched == {0: "DEN"}


def test_def_by_code_with_dst_suffix():
    rows = [
        SourceName("DEN D/ST"),
        SourceName("Broncos DST"),
        SourceName("Denver Defense"),
        SourceName("Rams D/ST", "LA"),
        SourceName("San Francisco", "SF", "DST"),
        SourceName("Defense", "DEN", "DEF"),
    ]
    result = match_source_names(rows, PLAYERS)
    assert result.matched == {0: "DEN", 1: "DEN", 2: "DEN", 3: "LAR", 4: "SF", 5: "DEN"}


def test_def_unknown_team_is_unmatched():
    row = SourceName("Bad Team D/ST")
    result = match_source_names([row, SourceName("Chicago Bears")], PLAYERS)
    # Chicago is a real team but Sleeper's dict here has no CHI defense.
    assert result.matched == {}
    assert len(result.unmatched) == 2


def test_team_code_aliases():
    rows = [
        SourceName("Brenton Strange", "JAC"),
        SourceName("Kyren Williams", "LA"),
        SourceName("Terry McLaurin", "WSH"),
    ]
    result = match_source_names(rows, PLAYERS)
    assert result.matched == {0: "600", 1: "700", 2: "800"}
    assert result.team_mismatches == []


def test_prebuilt_index_is_used():
    index = build_sleeper_name_index(PLAYERS)
    result = match_source_names([SourceName("Jalen Coker")], PLAYERS, index=index)
    assert result.matched == {0: "100"}
