from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.opponent_blocker import OPPONENT_GAIN_MIN, find_defensive_add, opponent_hole, roster_is_full
from sleeper_tool.lineup_optimizer import optimize_lineup

POSITIONS = ("QB", "RB", "WR", "BN")


def _p(pid, pos, proj, *, bye=None, starter=True, pctl=50.0):
    return make_entry(
        player_id=pid, name=pid, position=pos, is_starter=starter,
        value=make_value(name=pid, position=pos, proj_points=proj, bye_week=bye, dynasty_value_percentile=pctl, trend="no change"),
    )


def _roster(rid, entries, owner, positions=POSITIONS):
    return make_roster(roster_id=rid, owner_id=owner, owner_username=owner, team_name=f"Team {owner}", entries=entries,
                       fmt=make_format(roster_positions=positions), league=make_league_info())


def _me(bench):
    return _roster(1, [_p("qb1", "QB", 340), _p("rb1", "RB", 170), _p("wr1", "WR", 170), bench], "me")


def test_defensive_add_when_opponent_has_a_bye_hole_and_i_have_a_cheap_drop():
    them = _roster(2, [_p("qb2", "QB", 340), _p("rb2", "RB", 170), _p("wr2", "WR", 170, bye=1)], "them")  # WR slot empty week 1
    me = _me(_p("bench", "WR", 34, starter=False, pctl=10.0))
    fa = _p("fa_wr", "WR", 170, starter=False)  # 10/wk to their empty slot
    add = find_defensive_add(me, them, [fa], current_week=1, protected_ids=set())
    assert add is not None
    assert add.target.player_id == "fa_wr" and add.opponent_gain == 10.0 and add.drop.player_id == "bench"
    assert add.hole == "an unfilled WR slot this week"
    assert add.my_gain == 0.0  # he'd sit on my bench; the point is denying THEM
    assert add.describe().startswith("Add fa_wr (WR), drop bench — your week-1 opponent Team them has an unfilled WR slot this week; he would add +10.0")


def test_starter_out_this_week_is_a_hole_even_when_a_weak_backup_fills_the_slot():
    them = _roster(2, [_p("qb2", "QB", 340), _p("rb2", "RB", 170), _p("wr2", "WR", 170, bye=1), _p("wr3", "WR", 17, starter=False)], "them")
    hole = opponent_hole(them, optimize_lineup(them, nfl_week=1), optimize_lineup(them))
    assert hole == "a starter out this week (wr2 on bye week 1)"
    me = _me(_p("bench", "WR", 34, starter=False, pctl=10.0))
    add = find_defensive_add(me, them, [_p("fa_wr", "WR", 170, starter=False)], current_week=1, protected_ids=set())
    assert add is not None and add.opponent_gain == 9.0  # 10/wk replaces the 1/wk backup


def test_opponent_with_no_need_gets_no_defensive_add():
    them = _roster(2, [_p("qb2", "QB", 340), _p("rb2", "RB", 170), _p("wr2", "WR", 170)], "them")
    me = _me(_p("bench", "WR", 34, starter=False, pctl=10.0))
    assert find_defensive_add(me, them, [_p("fa_wr", "WR", 400, starter=False)], current_week=1, protected_ids=set()) is None


def test_small_opponent_gain_is_not_worth_a_move():
    them = _roster(2, [_p("qb2", "QB", 340), _p("rb2", "RB", 170), _p("wr2", "WR", 170, bye=1)], "them")
    me = _me(_p("bench", "WR", 34, starter=False, pctl=10.0))
    fa = _p("fa_wr", "WR", (OPPONENT_GAIN_MIN - 0.5) * 17, starter=False)
    assert find_defensive_add(me, them, [fa], current_week=1, protected_ids=set()) is None


def test_block_that_needs_an_unacceptable_drop_is_suppressed():
    them = _roster(2, [_p("qb2", "QB", 340), _p("rb2", "RB", 170), _p("wr2", "WR", 170, bye=1)], "them")
    me = _me(_p("keeper", "WR", 34, starter=False, pctl=10.0))
    fa = _p("fa_wr", "WR", 170, starter=False)
    assert find_defensive_add(me, them, [fa], current_week=1, protected_ids={"keeper"}) is None
    # An open roster spot needs no drop at all.
    roomy = _roster(1, [_p("qb1", "QB", 340), _p("rb1", "RB", 170), _p("wr1", "WR", 170)], "me")
    assert not roster_is_full(roomy)
    add = find_defensive_add(roomy, them, [fa], current_week=1, protected_ids={"qb1", "rb1", "wr1"})
    assert add is not None and add.drop is None


def test_best_opponent_gain_wins_deterministically_and_pre_draft_pool_is_empty():
    them = _roster(2, [_p("qb2", "QB", 340), _p("rb2", "RB", 170), _p("wr2", "WR", 170, bye=1)], "them")
    me = _me(_p("bench", "WR", 34, starter=False, pctl=10.0))
    fas = [_p("fa_b", "WR", 136, starter=False), _p("fa_a", "WR", 170, starter=False), _p("fa_rb", "RB", 255, starter=False)]
    add = find_defensive_add(me, them, fas, current_week=1, protected_ids=set())
    assert add.target.player_id == "fa_a"  # the WR fills the hole (+10/wk); the RB would only displace rb2 (+5/wk)
    assert find_defensive_add(me, them, [], current_week=1, protected_ids=set()) is None
