import pytest
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.move_impact import preview_add_drop, preview_trade, snapshot_roster
from sleeper_tool.trade_engine import TradeProposal

POSITIONS = ("QB", "RB", "RB", "WR", "BN", "BN")


def _p(pid, pos, proj, *, age=25.0, dyn=3000, pctl=60.0, is_starter=False):
    return make_entry(
        player_id=pid, name=pid, position=pos, age=age, is_starter=is_starter,
        value=make_value(name=pid, position=pos, proj_points=proj, dynasty_value=dyn, dynasty_value_percentile=pctl,
                         dynasty_positional_percentile=pctl, redraft_ecr_percentile=pctl),
    )


def _roster(rid, entries, owner="me"):
    return make_roster(
        roster_id=rid, owner_id=owner, owner_username=owner, team_name=owner, entries=entries,
        fmt=make_format(roster_positions=POSITIONS), league=make_league_info(kind="dynasty"),
    )


def _league():
    mine = _roster(1, [_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("rb2", "RB", 100), _p("wr1", "WR", 180), _p("rb3", "RB", 40)])
    rival = _roster(2, [_p("rqb", "QB", 280), _p("rrb1", "RB", 220, age=29.0), _p("rrb2", "RB", 150), _p("rwr", "WR", 170)], owner="rival")
    filler = _roster(3, [_p("fqb", "QB", 100), _p("frb", "RB", 50), _p("fwr", "WR", 60)], owner="filler")
    return mine, {1: mine, 2: rival, 3: filler}


def _proposal(give, receive, rating="Good"):
    return TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="rival",
        give=give, receive=receive, my_value_total=0, their_value_total=0,
        rationale_for_me=[], rationale_for_them=[], caveats=[], acceptance_rating=rating,
    )


def test_trade_preview_reports_lineup_change_and_weekly_points_delta():
    mine, rosters = _league()
    before = snapshot_roster(mine, rosters, current_week=1)
    rb3, rrb1 = mine.entries[4], rosters[2].entries[1]
    impact = preview_trade(_proposal([rb3], [rrb1]), mine, rosters, before, current_week=1)
    # rrb1 (220) replaces rb2 (100) in the lineup: +120 ROS points = +7.1/wk over 17 games.
    assert impact.lineup_in == ["rrb1"] and impact.lineup_out == ["rb2"]
    assert impact.weekly_points_delta == pytest.approx(120 / 17)
    deltas = impact.material_deltas()
    assert any(d.startswith("projected starter points +7.1/wk") for d in deltas)
    assert any("rrb1 enters the lineup; rb2 drops out" in d for d in deltas)
    assert any("average starter age +1.0 yrs" in d for d in deltas)  # 29-year-old replaces a 25-year-old starter
    assert impact.is_material
    assert [e.player_id for e in mine.entries] == ["qb1", "rb1", "rb2", "wr1", "rb3"]  # untouched


def test_an_immaterial_move_reports_nothing():
    mine, rosters = _league()
    before = snapshot_roster(mine, rosters, current_week=1)
    add = _p("fa", "RB", 45)  # bench-for-bench, no lineup change
    impact = preview_add_drop("Add fa", add, "rb3", mine, rosters, before, current_week=1)
    assert impact.material_deltas() == []
    assert not impact.is_material


def test_previews_are_skipped_below_the_acceptance_bar():
    mine, rosters = _league()
    before = snapshot_roster(mine, rosters, current_week=1)
    rb3, rrb1 = mine.entries[4], rosters[2].entries[1]
    assert preview_trade(_proposal([rb3], [rrb1], rating="Low"), mine, rosters, before, current_week=1) is None
    assert preview_trade(_proposal([rb3], [rrb1], rating="Moderate"), mine, rosters, before, current_week=1) is not None


def test_team_status_change_is_detected_using_the_post_trade_league():
    mine, rosters = _league()
    before = snapshot_roster(mine, rosters, current_week=1)
    # Gut my roster: send both real RBs and the QB for a single scrub. Status must drop.
    qb1, rb1, rb2 = mine.entries[0], mine.entries[1], mine.entries[2]
    scrub = _p("scrub", "RB", 30, dyn=200, pctl=5.0)
    rosters[2].entries.append(scrub)
    impact = preview_trade(_proposal([qb1, rb1, rb2], [scrub], rating="Good"), mine, rosters, before, current_week=1)
    assert impact.before.status == "contender"
    assert impact.after.status != "contender"
    assert any(d.startswith("team status contender →") for d in impact.material_deltas())
    assert any(d.startswith("total roster value -") for d in impact.material_deltas())


def test_waiver_preview_can_show_a_depth_need_being_filled():
    # No TE on the roster at all is a depth need; adding a rosterable TE
    # (even one who doesn't start — the synthetic slots have no TE spot)
    # removes it from the list.
    mine = _roster(1, [_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("wr1", "WR", 180), _p("wr2", "WR", 90)])
    rosters = {1: mine}
    before = snapshot_roster(mine, rosters, current_week=1)
    assert before.depth_needs == ["TE"]
    impact = preview_add_drop("Add fa", _p("fa", "TE", 100, pctl=70.0), "wr2", mine, rosters, before, current_week=1)
    assert impact.after.depth_needs == []
    assert "depth needs TE → none" in impact.material_deltas()
