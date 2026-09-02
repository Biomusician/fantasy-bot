from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.bye_collision import LOOKAHEAD_WEEKS, describe_bye_collision, plan_bye_collisions, positions_covering


def _p(pid, pos, proj, bye=None):
    return make_entry(player_id=pid, name=pid, position=pos, is_starter=False,
                      value=make_value(name=pid, position=pos, proj_points=proj, bye_week=bye))


def _roster(entries, positions=("RB", "RB", "WR", "FLEX", "BN", "BN")):
    return make_roster(entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(kind="redraft"))


def test_reports_the_earliest_week_where_a_slot_cannot_be_filled():
    r = _roster([_p("rb1", "RB", 200, bye=9), _p("rb2", "RB", 150, bye=7), _p("wr1", "WR", 180), _p("wr2", "WR", 100)])
    # Week 7: rb2 out, no RB backup -> wr2 can't play RB -> RB slot unfilled.
    plan = plan_bye_collisions(r, current_week=5)
    assert plan.week == 7
    assert [h.slot for h in plan.holes] == ["RB"]
    assert plan.holes[0].replacement is None
    assert [e.player_id for e in plan.starters_on_bye] == ["rb2"]
    assert plan.weeks_scanned == [6, 7, 8, 9]


def test_a_weak_replacement_under_70_percent_is_a_hole_but_an_adequate_one_is_not():
    positions = ("RB", "RB", "WR", "BN", "BN")
    r = _roster([_p("rb1", "RB", 200, bye=8), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("rb3", "RB", 130)], positions)
    # Week 8: rb1 out; rb3 (130) is what enters -> 65% of 200 -> hole.
    plan = plan_bye_collisions(r, current_week=5)
    assert plan.week == 8
    assert plan.holes[0].slot == "RB" and plan.holes[0].replacement.player_id == "rb3"
    assert round(plan.holes[0].ratio, 2) == 0.65

    covered = _roster([_p("rb1", "RB", 200, bye=8), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("rb3", "RB", 145)], positions)
    assert plan_bye_collisions(covered, current_week=5) is None  # 72.5% is fine


def test_two_byes_in_one_week_pair_the_best_replacement_with_the_best_displaced_starter():
    r = _roster(
        [_p("rb1", "RB", 200, bye=8), _p("rb2", "RB", 150, bye=8), _p("wr1", "WR", 180), _p("rb3", "RB", 145), _p("rb4", "RB", 40)],
        ("RB", "RB", "WR", "BN", "BN"),
    )
    plan = plan_bye_collisions(r, current_week=5)
    assert plan.week == 8
    assert [e.player_id for e in plan.starters_on_bye] == ["rb1", "rb2"]
    # rb3 (145) covers rb1 (72.5%, fine); rb4 (40) is left for rb2 (27%) -> one hole.
    assert [(h.normal_starter.player_id, h.replacement.player_id) for h in plan.holes] == [("rb2", "rb4")]


def test_cascade_lands_where_it_actually_hurts():
    # rb1 on bye; wr2 was in FLEX; rb3 slides into RB and wr2 stays — but the
    # only benched WR is weak, so if wr2 moved to RB... he can't. The hole is
    # RB (filled by rb3 at 40%), not FLEX.
    r = _roster([_p("rb1", "RB", 200, bye=6), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("wr2", "WR", 100), _p("rb3", "RB", 80)])
    plan = plan_bye_collisions(r, current_week=5)
    assert [(h.slot, h.replacement.player_id) for h in plan.holes] == [("RB", "rb3")]


def test_no_bye_data_means_no_holes_and_the_current_week_is_not_scanned():
    r = _roster([_p("rb1", "RB", 200, bye=5), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("wr2", "WR", 100)])
    assert plan_bye_collisions(r, current_week=5) is None  # week 5 is this week: waiver_engine's job
    assert plan_bye_collisions(_roster([_p("rb1", "RB", 200), _p("rb2", "RB", 150)]), current_week=5) is None
    assert plan_bye_collisions(r, current_week=None) is None


def test_description_and_covering_positions():
    r = _roster([_p("rb1", "RB", 200, bye=9), _p("rb2", "RB", 150, bye=7), _p("wr1", "WR", 180), _p("wr2", "WR", 100)])
    plan = plan_bye_collisions(r, current_week=5)
    text = describe_bye_collision(plan)
    assert text.startswith("1 starter on bye — RB: rb2 on bye and no legal fill")
    assert positions_covering(plan) == {"RB"}
    flex_plan = plan_bye_collisions(
        _roster([_p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("wr2", "WR", 100, bye=6)]), current_week=5
    )
    assert flex_plan.holes[0].slot == "FLEX"
    assert positions_covering(flex_plan) == {"WR"}  # the displaced starter's position, not everything FLEX-eligible


def test_window_stops_at_the_end_of_the_regular_season():
    r = _roster([_p("rb1", "RB", 200, bye=17), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("wr2", "WR", 100)])
    plan = plan_bye_collisions(r, current_week=15)
    assert plan.weeks_scanned == [16, 17]
    assert plan.week == 17
    assert len(plan.weeks_scanned) < LOOKAHEAD_WEEKS
