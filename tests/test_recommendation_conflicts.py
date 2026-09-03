from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.pick_opportunity import STRATEGIC, PickAssessment, PickOpportunity
from sleeper_tool.portfolio_exposure import VERY_HIGH
from sleeper_tool.recommendation_conflicts import CONFLICTED, TRADE, WAIVER, conflict_for, detect_conflicts
from sleeper_tool.replacement_value import PositionMarket, ReplacementMarket
from sleeper_tool.report_data import LeagueReportData, build_priority_actions
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.trade_opportunity_cost import COSTS_LINEUP, FAVORABLE, MAJOR_LINEUP_COST, MOSTLY_NEUTRAL, ROUGHLY_EVEN, TradeEconomics
from sleeper_tool.waiver_engine import WaiverTarget


def _p(pid, pos, proj=200.0, *, years_exp=3, age=25.0):
    e = make_entry(player_id=pid, name=pid, position=pos, age=age, value=make_value(name=pid, position=pos, proj_points=proj))
    e.years_exp = years_exp
    return e


def _proposal(give, receive=(), *, trade_type="sell_high", picks=(), caveats=(), username="rival", rating="High", confidence="High"):
    return TradeProposal(
        league_name="L", currency="dynasty", target_username=username, target_team_name="Rival", give=list(give), receive=list(receive),
        my_value_total=100, their_value_total=100, rationale_for_me=["value play"], rationale_for_them=[], caveats=list(caveats),
        give_picks=list(picks), trade_type=trade_type, acceptance_rating=rating, confidence=confidence,
    )


def _ld(entries, **kw):
    roster = make_roster(entries=entries, fmt=make_format(roster_positions=("QB", "RB", "BN")), league=make_league_info(kind="dynasty"))
    defaults = dict(league=make_league_info(name="L"), drafted=True, roster=roster, currency="dynasty", lineup=optimize_lineup(roster))
    defaults.update(kw)
    return LeagueReportData(**defaults)


def test_sell_high_of_a_lineup_critical_starter_in_a_very_scarce_market():
    qb = _p("qb", "QB", 340)
    p = _proposal([qb])
    market = ReplacementMarket(positions={"QB": PositionMarket("QB", None, None, None, None, "Very Scarce", None)}, players={})
    ld = _ld([qb, _p("rb", "RB")], proposals=[p], trade_economics=[TradeEconomics(FAVORABLE, COSTS_LINEUP, -3.0, True)], replacement=market)
    conflicts = detect_conflicts(ld)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.kind == TRADE and c.key == "0" and c.subject == p.summary_line()
    assert c.reasons_against == [
        "sells a starter the lineup relies on (-3.0/wk)",
        "QB replacement market is Very Scarce: no waiver replacement for what you'd send",
    ]
    assert c.reasons_for == ["assets favorable", "value play"]
    assert c.describe().startswith(CONFLICTED + ": for — assets favorable; value play | against — sells a starter")
    # The proposal is neither removed nor re-rated.
    assert ld.proposals == [p] and p.acceptance_rating == "High"


def test_major_lineup_cost_strategic_pick_and_exposure_conflicts():
    qb = _p("qb", "QB", 340)
    pick = OwnedPick(season="2027", round=1, original_roster_id=1, tier="Mid", name="2027 Mid 1st", value=3000)
    opp = PickOpportunity(assessments=[PickAssessment(pick=pick, classification=STRATEGIC, reason="r")], units=[])
    major = _proposal([qb], trade_type="buy_low")
    strategic = _proposal([], receive=[_p("wr", "WR")], trade_type="buy_low", picks=[pick])
    exposed = _proposal([], receive=[_p("te", "TE")], trade_type="buy_low", caveats=[f"Adding him would put you at {VERY_HIGH} exposure (5 of 8 leagues)."])
    clean = _proposal([], receive=[_p("rb2", "RB")], trade_type="buy_low")
    ld = _ld(
        [qb, _p("rb", "RB")], proposals=[major, strategic, exposed, clean], pick_opportunity=opp,
        trade_economics=[
            TradeEconomics(ROUGHLY_EVEN, MAJOR_LINEUP_COST, -9.0, False),
            TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, 0.5, False),
            TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, 1.0, False),
            TradeEconomics(FAVORABLE, MOSTLY_NEUTRAL, 1.0, False),
        ],
    )
    conflicts = detect_conflicts(ld)
    assert [c.key for c in conflicts] == ["0", "1", "2"]
    assert conflicts[0].reasons_against == ["Major Lineup Cost (-9.0/wk)"]
    assert conflicts[1].reasons_against == ["spends a Strategic pick (2027 Mid 1st) for a Mostly Neutral lineup effect"]
    assert conflicts[2].reasons_against == ["the acquisition would push cross-league exposure to Very High"]
    assert conflict_for(conflicts, TRADE, "3") is None


def test_waiver_conflicts_developmental_drop_and_exposure():
    rookie = _p("rookie", "WR", years_exp=0, age=22.0)
    vet = _p("vet", "WR", years_exp=8, age=30.0)
    t1 = WaiverTarget(player_id="w1", name="W1", position="WR", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                      reason="fills a need; hype", priority_tier="Must Add", drop_candidate=rookie)
    t2 = WaiverTarget(player_id="w2", name="W2", position="WR", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                      reason="fills a need", priority_tier="Strong Add", drop_candidate=vet, notes=[f"would put you at {VERY_HIGH} exposure"])
    t3 = WaiverTarget(player_id="w3", name="W3", position="WR", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                      reason="fills a need", priority_tier="Moderate", drop_candidate=vet)
    ld = _ld([_p("qb", "QB"), _p("rb", "RB")], waiver_targets=[t1, t2, t3])
    conflicts = detect_conflicts(ld)
    assert [(c.kind, c.key) for c in conflicts] == [(WAIVER, "w1"), (WAIVER, "w2")]
    assert conflicts[0].reasons_against == ["the drop, rookie, is a developmental hold worth keeping (80th percentile dynasty value)"]
    assert conflicts[0].reasons_for == ["Must Add — fills a need"]
    assert conflicts[1].reasons_against == ["the add would push cross-league exposure to Very High"]


def test_drops_the_tool_itself_recommends_or_that_have_no_value_are_not_conflicts():
    from sleeper_tool.trade_types import DropCandidate

    rookie = _p("rookie", "WR", years_exp=0, age=22.0)
    nobody = _p("nobody", "WR", years_exp=0, age=22.0)
    nobody.value.dynasty_value_percentile = 12.0
    t1 = WaiverTarget(player_id="w1", name="W1", position="WR", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                      reason="r", priority_tier="Must Add", drop_candidate=rookie)
    t2 = WaiverTarget(player_id="w2", name="W2", position="WR", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                      reason="r", priority_tier="Must Add", drop_candidate=nobody)
    ld = _ld([_p("qb", "QB"), _p("rb", "RB")], waiver_targets=[t1, t2], drop_candidates=[DropCandidate(entry=rookie, priority="Consider Dropping", reasons=["buried"])])
    assert detect_conflicts(ld) == []


def test_dropping_a_current_starter_or_the_bye_fill_is_a_conflict():
    from sleeper_tool.bye_collision import ByeCollision, ByeHole

    qb, rb, fill = _p("qb", "QB", 340), _p("rb", "RB"), _p("fill", "RB", 90, years_exp=6, age=28.0)
    t1 = WaiverTarget(player_id="w1", name="W1", position="RB", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                      reason="r", priority_tier="Must Add", drop_candidate=rb)
    t2 = WaiverTarget(player_id="w2", name="W2", position="RB", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                      reason="r", priority_tier="Strong Add", drop_candidate=fill)
    bye = ByeCollision(week=5, holes=[ByeHole(week=5, slot="RB", normal_starter=rb, normal_projection=10.0, replacement=fill, replacement_projection=5.0)],
                       starters_on_bye=[rb], weeks_scanned=[2, 3, 4, 5])
    ld = _ld([qb, rb, fill], waiver_targets=[t1, t2], bye_collision=bye)
    conflicts = detect_conflicts(ld)
    assert [c.reasons_against for c in conflicts] == [
        ["the drop, rb, is a current optimized starter"],
        ["the drop, fill, is the named fill for your week 5 bye hole"],
    ]


def test_sell_high_out_of_a_very_scarce_market_is_only_a_conflict_when_the_piece_plays():
    starter, bench = _p("qb", "QB", 340), _p("qb2", "QB", 100)
    market = ReplacementMarket(positions={"QB": PositionMarket("QB", None, None, None, None, "Very Scarce", None)}, players={})
    sell_bench = _proposal([bench])
    sell_starter = _proposal([starter])
    ld = _ld([starter, bench, _p("rb", "RB")], proposals=[sell_bench, sell_starter], replacement=market,
             trade_economics=[TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, 0.0, False), TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, -1.0, False)])
    conflicts = detect_conflicts(ld)
    assert [c.key for c in conflicts] == ["1"]
    assert conflicts[0].reasons_for == ["value play"]  # a Roughly Even asset verdict is not a reason for


def test_best_moves_carry_the_conflict_label_without_dropping_the_move():
    qb = _p("qb", "QB", 340)
    p = _proposal([qb])
    t = WaiverTarget(player_id="w1", name="W1", position="WR", team="KC", trend_count=1, value=make_value(), fills_need=True, need_rank=0,
                     reason="fills a need", priority_tier="Must Add", drop_candidate=_p("rookie", "WR", years_exp=0, age=22.0))
    ld = _ld([qb, _p("rb", "RB")], proposals=[p], waiver_targets=[t], trade_economics=[TradeEconomics(ROUGHLY_EVEN, MAJOR_LINEUP_COST, -9.0, False)])
    ld.conflicts = detect_conflicts(ld)
    actions = build_priority_actions([ld])
    # A Must Add is Immediate; a trade is Monitor — the priority key, not the kind, orders them.
    assert [a.kind for a in actions] == ["waiver", "trade"]
    assert all(a.detail.startswith(CONFLICTED + ": ") for a in actions)
    assert "Major Lineup Cost (-9.0/wk)" in actions[1].detail and "developmental hold" in actions[0].detail
