"""report_data's annotation seams for the replacement market, source
disagreement and trade economics — and that both renderers show them."""
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.html_report import _league_panel
from sleeper_tool.move_impact import MoveImpact, RosterSnapshot
from sleeper_tool.negotiation_ladder import LadderStep, NegotiationLadder
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, VERY_SCARCE, PlayerReplacementContext, PositionMarket, ReplacementMarket
from sleeper_tool.report import render_league_section
from sleeper_tool.report_data import (
    LeagueReportData,
    _annotate_clogs_with_replacement,
    _annotate_ladders_with_sources,
    _annotate_proposals_with_replacement,
    _annotate_proposals_with_sources,
    _annotate_waivers_with_replacement,
    _economics_note,
    _source_note_for,
    build_priority_actions,
)
from sleeper_tool.roster_clog import RosterClog
from sleeper_tool.source_disagreement import MARKET_ABOVE_PROJECTION, SOURCE_DISAGREEMENT, STRONG_CONSENSUS, SourceView
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.trade_opportunity_cost import FAVORABLE, MAJOR_LINEUP_COST, MOSTLY_NEUTRAL, ROUGHLY_EVEN, TradeEconomics, analyze_trade
from sleeper_tool.waiver_engine import WaiverTarget


def _p(pid, pos, proj, *, rank=50):
    return make_entry(
        player_id=pid, name=pid, position=pos, is_starter=False,
        value=make_value(name=pid, position=pos, proj_points=proj, dynasty_value=3000, dynasty_rank=rank, dynasty_ecr_rank=rank),
    )


def _proposal(give=(), receive=(), *, mine=100, theirs=100, rating="High", confidence="High"):
    return TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="Rival",
        give=list(give), receive=list(receive), my_value_total=mine, their_value_total=theirs,
        rationale_for_me=[], rationale_for_them=[], caveats=[], acceptance_rating=rating, confidence=confidence,
    )


def _market():
    fa_qb, fa_te, fa_rb = _p("fa_qb", "QB", 136), _p("fa_te", "TE", 153), _p("fa_rb", "RB", 100)
    return ReplacementMarket(
        positions={
            "QB": PositionMarket("QB", fa_qb, 8.0, None, 20.0, VERY_SCARCE, 0.6),
            "TE": PositionMarket("TE", fa_te, 9.0, None, 9.3, ABUNDANT, 0.03),
            "RB": PositionMarket("RB", fa_rb, 5.9, None, 8.0, NORMAL, 0.26),
        },
        players={},
    )


def _view(name, pos, consensus, direction=None, gap=None):
    return SourceView(
        name=name, position=pos, consensus=consensus, consensus_gap=gap, consensus_pair=("KTC", "FantasyPros dynasty"),
        direction=direction, market_rank=10, projection_rank=40, expert_note=None, labels=[x for x in (consensus, direction) if x],
    )


def test_replacement_annotations_on_trade_pieces():
    qb = _p("qb", "QB", 340)  # 20/wk, +12 over a Very Scarce wire
    te_in = _p("te_in", "TE", 170)  # 10/wk, +1.0 over an Abundant wire
    qb_in = _p("qb_in", "QB", 340)
    sell = _proposal(give=[qb], receive=[te_in])
    buy = _proposal(receive=[qb_in])
    _annotate_proposals_with_replacement([sell, buy], _market(), "dynasty", 17)
    assert sell.caveats == [
        "Replacement context: qb is +12.0/wk over the best free-agent QB (Very Scarce market); replacing him from this wire would cost that much.",
        "Replacement context: te_in is only +1.0/wk over the best free-agent TE (Abundant market) — waivers offer nearly the same production here.",
    ]
    assert sell.rationale_for_me == []
    assert buy.rationale_for_me == ["Replacement context: qb_in arrives +12.0/wk over the best free-agent QB (Very Scarce market)."]


def test_cheap_to_replace_give_piece_is_a_point_in_favour_and_unstarted_positions_are_ignored():
    te = _p("te", "TE", 160)  # 9.4/wk, +0.4 over the wire
    k = _p("k", "K", 150)  # no K market
    p = _proposal(give=[te, k])
    _annotate_proposals_with_replacement([p], _market(), "dynasty", 17)
    assert p.rationale_for_me == ["Replacement context: te is +0.4/wk over the best free-agent TE (Abundant market) — cheap to replace from this league's wire."]
    assert p.caveats == []


def test_waiver_and_clog_notes_follow_scarcity_only():
    def target(pid, pos):
        return WaiverTarget(player_id=pid, name=pid, position=pos, team="KC", trend_count=1, value=make_value(position=pos, proj_points=100),
                            fills_need=False, need_rank=None, reason="r")

    qb, te, rb = target("q", "QB"), target("t", "TE"), target("r", "RB")
    _annotate_waivers_with_replacement([qb, te, rb], _market(), "dynasty", 17, {})
    assert qb.notes == ["QB market is Very Scarce here: an add at this position matters more than his rank alone suggests"]
    assert te.notes == ["TE market is Abundant here: comparable production is usually on waivers, so don't overspend"]
    assert rb.notes == []
    clogs = [RosterClog(_p("c1", "TE", 50), ["buried"], 200.0), RosterClog(_p("c2", "QB", 50), ["buried"], 200.0), RosterClog(_p("c3", "RB", 50), ["buried"], 200.0)]
    _annotate_clogs_with_replacement(clogs, _market())
    assert clogs[0].reasons == ["buried", "TE replacements are Abundant on this wire"]
    assert clogs[1].reasons == ["buried", "but QB replacements are Very Scarce here — keep unless the spot is needed"]
    assert clogs[2].reasons == ["buried"]


def test_source_annotations_support_the_trade_direction_or_become_caveats():
    sell_piece, buy_piece, quiet = _p("s", "WR", 100), _p("b", "WR", 100), _p("q", "WR", 100)
    views = {
        "s": _view("s", "WR", STRONG_CONSENSUS, MARKET_ABOVE_PROJECTION),
        "b": _view("b", "WR", SOURCE_DISAGREEMENT, gap=25),
        "q": _view("q", "WR", STRONG_CONSENSUS),
    }
    p = _proposal(give=[sell_piece, quiet], receive=[buy_piece])
    _annotate_proposals_with_sources([p], views)
    assert p.rationale_for_me == [
        "Sources on s: Strong Consensus; Market Above Projection (WR10 market vs WR40 projection) — the market pays more than the projection supports, which favours selling."
    ]
    assert p.caveats == ["Sources on b: Source Disagreement: KTC vs FantasyPros dynasty differ by 25 WR places."]
    assert _source_note_for(views, [sell_piece, buy_piece, quiet]) == " Sources disagree on b."
    assert _source_note_for(views, [quiet]) == ""
    step = LadderStep(name="x", players=[buy_piece, quiet], picks=[], outgoing_value=100, ratio=1.0, acceptance="Good", reasons=[])
    ladder = NegotiationLadder(baseline_value=100, opening=step, fallback=None, walk_away=None, opening_message="")
    _annotate_ladders_with_sources({0: ladder}, views)
    assert step.source_note == "sources split on b (Source Disagreement: KTC vs FantasyPros dynasty differ by 25 WR places)"


def test_economics_note_names_only_tradeoffs_and_major_costs():
    assert _economics_note(None) == ""
    assert _economics_note(TradeEconomics(FAVORABLE, MOSTLY_NEUTRAL, 0.5, False)) == ""
    assert _economics_note(TradeEconomics(FAVORABLE, MAJOR_LINEUP_COST, -10.7, True)) == " Strategic Tradeoff: assets favorable, lineup major lineup cost (-10.7/wk)."
    assert _economics_note(TradeEconomics(ROUGHLY_EVEN, MAJOR_LINEUP_COST, -8.0, False)) == " Major Lineup Cost (-8.0/wk)."


def test_priority_actions_carry_economics_and_source_notes():
    piece = _p("s", "WR", 100)
    p = _proposal(give=[piece])
    ld = LeagueReportData(
        league=make_league_info(name="L"), drafted=True, proposals=[p],
        trade_economics=[TradeEconomics(FAVORABLE, MAJOR_LINEUP_COST, -10.7, True)],
        source_views={"s": _view("s", "WR", SOURCE_DISAGREEMENT, gap=30)},
    )
    actions = build_priority_actions([ld])
    assert actions[0].detail.endswith("Strategic Tradeoff: assets favorable, lineup major lineup cost (-10.7/wk). Sources disagree on s.")


def _impact(delta):
    before = RosterSnapshot(lineup=None, weekly_points=100.0, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    after = RosterSnapshot(lineup=None, weekly_points=100.0 + delta, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    return MoveImpact("x", before, after)


def test_both_renderers_show_the_new_context():
    qb = _p("qb", "QB", 340)
    roster = make_roster(
        roster_id=1, owner_id="me", owner_username="me", entries=[qb, _p("te", "TE", 100)],
        fmt=make_format(roster_positions=("QB", "TE", "BN")), league=make_league_info(kind="dynasty"),
    )
    market = _market()
    market.understated = [PlayerReplacementContext(qb, 20.0, 12.0, 0.0, 1000, VERY_SCARCE)]
    p = _proposal(give=[qb], mine=80, theirs=100)
    econ = analyze_trade(p, _impact(-10.7), market)
    target = WaiverTarget(player_id="w", name="Wire Guy", position="TE", team="KC", trend_count=1, value=make_value(position="TE", proj_points=100),
                          fills_need=False, need_rank=None, reason="r", notes=["TE market is Abundant here: don't overspend"])
    ld = LeagueReportData(
        league=make_league_info(name="L"), drafted=True, roster=roster, currency="dynasty", proposals=[p],
        trade_economics=[econ], replacement=market, waiver_targets=[target],
        replacement_clauses={"qb": "+12.0/wk over the best free-agent QB (Very Scarce market)"},
    )
    md = "\n".join(render_league_section(ld))
    assert "*Economics: Assets: Favorable · Lineup: Major Lineup Cost (-10.7/wk) · Strategic Tradeoff · QB replacement market is Very Scarce — waivers won't repair this*" in md
    assert "### Replacement market" in md
    assert "- **QB: Very Scarce — best free agent fa_qb projects 8.0/wk vs no current starter league-wide**" in md
    assert "Rank understates their edge here: qb (+12.0/wk over the best free-agent QB (Very Scarce market))" in md
    assert "TE market is Abundant here: don't overspend" in md
    html = _league_panel(ld)
    assert "Assets: Favorable" in html and "Lineup: Major Lineup Cost (-10.7/wk)" in html and "Strategic Tradeoff" in html
    assert "Replacement market" in html and "Rank understates their edge here" in html
    assert "TE market is Abundant here: don&#x27;t overspend" in html or "TE market is Abundant here: don't overspend" in html
    # An undrafted or errored league renders nothing new.
    assert "Replacement market" not in "\n".join(render_league_section(LeagueReportData(league=make_league_info(), drafted=False)))
