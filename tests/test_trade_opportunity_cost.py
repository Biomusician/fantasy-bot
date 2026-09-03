from conftest import make_entry, make_value

from sleeper_tool.move_impact import MoveImpact, RosterSnapshot
from sleeper_tool.replacement_value import PositionMarket, ReplacementMarket
from sleeper_tool.trade_engine import TradeProposal
from sleeper_tool.trade_opportunity_cost import (
    COSTS_LINEUP,
    COSTS_MAX,
    FAVORABLE,
    IMPROVES_LINEUP,
    IMPROVES_MIN,
    MAJOR_COST_MAX,
    MAJOR_LINEUP_COST,
    MOSTLY_NEUTRAL,
    ROUGHLY_EVEN,
    STRATEGIC_TRADEOFF,
    UNFAVORABLE,
    analyze_trade,
    asset_economics,
    is_strategic_tradeoff,
    roster_economics,
)


def _proposal(mine, theirs, give=None):
    return TradeProposal(
        league_name="L", currency="dynasty", target_username="r", target_team_name="r",
        give=give or [], receive=[], my_value_total=mine, their_value_total=theirs,
        rationale_for_me=[], rationale_for_them=[], caveats=[],
    )


def _impact(delta):
    snap = RosterSnapshot(lineup=None, weekly_points=100.0, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    after = RosterSnapshot(lineup=None, weekly_points=100.0 + delta, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    return MoveImpact("x", snap, after)


def test_roster_economics_boundaries():
    assert roster_economics(IMPROVES_MIN) == IMPROVES_LINEUP
    assert roster_economics(IMPROVES_MIN - 0.01) == MOSTLY_NEUTRAL
    assert roster_economics(COSTS_MAX + 0.01) == MOSTLY_NEUTRAL
    assert roster_economics(COSTS_MAX) == COSTS_LINEUP
    assert roster_economics(MAJOR_COST_MAX + 0.01) == COSTS_LINEUP
    assert roster_economics(MAJOR_COST_MAX) == MAJOR_LINEUP_COST


def test_asset_economics_follows_the_engine_balance_label():
    assert asset_economics(_proposal(80, 100)) == FAVORABLE
    assert asset_economics(_proposal(100, 100)) == ROUGHLY_EVEN
    assert asset_economics(_proposal(113, 100)) == UNFAVORABLE
    assert asset_economics(_proposal(130, 100)) == UNFAVORABLE


def test_strategic_tradeoff_is_opposite_directions_only():
    assert is_strategic_tradeoff(FAVORABLE, MAJOR_LINEUP_COST)
    assert is_strategic_tradeoff(FAVORABLE, COSTS_LINEUP)
    assert is_strategic_tradeoff(UNFAVORABLE, IMPROVES_LINEUP)
    assert not is_strategic_tradeoff(FAVORABLE, IMPROVES_LINEUP)
    assert not is_strategic_tradeoff(ROUGHLY_EVEN, MAJOR_LINEUP_COST)
    assert not is_strategic_tradeoff(FAVORABLE, None)


def test_the_stroud_shape_surfaces_as_a_strategic_tradeoff_with_a_scarcity_note():
    qb = make_entry(player_id="qb", name="QB Star", position="QB", value=make_value(position="QB"))
    p = _proposal(80, 100, give=[qb])  # value favors me
    market = ReplacementMarket(positions={"QB": PositionMarket("QB", None, None, None, None, "Very Scarce", None)}, players={})
    econ = analyze_trade(p, _impact(-10.7), market)
    assert econ.asset_economics == FAVORABLE and econ.roster_economics == MAJOR_LINEUP_COST
    assert econ.strategic_tradeoff
    assert econ.scarcity_note == "QB replacement market is Very Scarce — waivers won't repair this"
    text = econ.describe()
    assert "Assets: Favorable" in text and "Lineup: Major Lineup Cost (-10.7/wk)" in text and STRATEGIC_TRADEOFF in text


def test_no_preview_means_asset_economics_only_and_never_a_tradeoff():
    econ = analyze_trade(_proposal(80, 100), None)
    assert econ.roster_economics is None and econ.weekly_delta is None and not econ.strategic_tradeoff
    assert econ.describe() == "Assets: Favorable"


def test_abundant_market_adds_no_scarcity_note():
    rb = make_entry(player_id="rb", name="RB", position="RB")
    market = ReplacementMarket(positions={"RB": PositionMarket("RB", None, None, None, None, "Abundant", 0.0)}, players={})
    assert analyze_trade(_proposal(80, 100, give=[rb]), _impact(-3.0), market).scarcity_note is None
