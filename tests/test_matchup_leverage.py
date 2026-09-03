from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.matchup_leverage import (
    LARGE_DEFICIT,
    MODEST_DEFICIT,
    MODEST_EDGE,
    MODEST_EDGE_MIN,
    NEAR_EVEN,
    STRONG_EDGE,
    STRONG_EDGE_MIN,
    build_matchup_leverage,
    find_opponent_roster_id,
    gap_label,
)


def _p(pid, pos, proj, *, bye=None):
    return make_entry(player_id=pid, name=pid, position=pos, value=make_value(name=pid, position=pos, proj_points=proj, bye_week=bye))


def _roster(rid, entries, owner):
    return make_roster(roster_id=rid, owner_id=owner, owner_username=owner, team_name=f"Team {owner}", entries=entries,
                       fmt=make_format(roster_positions=("QB", "RB", "BN")), league=make_league_info())


def test_gap_label_boundaries_and_ties():
    assert gap_label(STRONG_EDGE_MIN) == STRONG_EDGE
    assert gap_label(STRONG_EDGE_MIN - 0.1) == MODEST_EDGE
    assert gap_label(MODEST_EDGE_MIN) == MODEST_EDGE
    assert gap_label(MODEST_EDGE_MIN - 0.1) == NEAR_EVEN
    assert gap_label(0.0) == NEAR_EVEN
    assert gap_label(-MODEST_EDGE_MIN + 0.1) == NEAR_EVEN
    assert gap_label(-MODEST_EDGE_MIN) == MODEST_DEFICIT
    assert gap_label(-STRONG_EDGE_MIN + 0.1) == MODEST_DEFICIT
    assert gap_label(-STRONG_EDGE_MIN) == LARGE_DEFICIT


def test_opponent_is_the_other_roster_sharing_my_matchup_id():
    rows = [{"roster_id": 1, "matchup_id": 7}, {"roster_id": 2, "matchup_id": 7}, {"roster_id": 3, "matchup_id": 8}]
    assert find_opponent_roster_id(rows, 1) == 2 and find_opponent_roster_id(rows, 2) == 1
    assert find_opponent_roster_id(rows, 3) is None  # nobody else in matchup 8 (bye)
    assert find_opponent_roster_id(rows, 9) is None  # not in this week's rows
    assert find_opponent_roster_id([{"roster_id": 1, "matchup_id": None}, {"roster_id": 2, "matchup_id": None}], 1) is None


def test_leverage_uses_this_weeks_lineups_and_names_the_opponent():
    mine = _roster(1, [_p("qb1", "QB", 340), _p("rb1", "RB", 170)], "me")  # 20 + 10 = 30/wk
    theirs = _roster(2, [_p("qb2", "QB", 340), _p("rb2", "RB", 170, bye=1), _p("rb3", "RB", 85)], "them")  # rb2 sits: 20 + 5 = 25
    rosters = {1: mine, 2: theirs}
    rows = [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}]
    lev = build_matchup_leverage(mine, rosters, rows, current_week=1)
    assert lev is not None and lev.opponent_name == "Team them" and lev.week == 1
    assert (lev.my_points, lev.opponent_points, lev.gap, lev.label) == (30.0, 25.0, 5.0, MODEST_EDGE)
    assert lev.describe() == "vs Team them (week 1): Modest Edge — you project 30.0, they project 25.0 (+5.0)"
    assert lev.effect_clause(2.8) == "Adds +2.8 projected points per week; current matchup edge is 5.0."
    # Week 2: rb2 plays, the gap closes to 0 -> Near Even (a tie is Near Even, not an edge).
    lev2 = build_matchup_leverage(mine, rosters, rows, current_week=2)
    assert lev2.gap == 0.0 and lev2.label == NEAR_EVEN
    assert lev2.effect_clause(-1.5) == "Adds -1.5 projected points per week; the matchup is near even (+0.0)."
    deficit = build_matchup_leverage(theirs, rosters, rows, current_week=1)
    assert deficit.label == MODEST_DEFICIT and deficit.effect_clause(2.8) == "Adds +2.8 projected points per week; current matchup deficit is 5.0."


def test_no_matchup_no_week_or_unknown_opponent_means_none():
    mine = _roster(1, [_p("qb1", "QB", 340)], "me")
    rosters = {1: mine}
    assert build_matchup_leverage(mine, rosters, [], current_week=1) is None
    assert build_matchup_leverage(mine, rosters, [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}], current_week=None) is None
    # Opponent roster id not among valued rosters (unsynced) -> None, never a guess.
    assert build_matchup_leverage(mine, rosters, [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}], current_week=1) is None
