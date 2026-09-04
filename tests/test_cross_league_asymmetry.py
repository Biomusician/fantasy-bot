from conftest import make_entry, make_league_info

from sleeper_tool.cross_league_asymmetry import CHEAP_EDGE_MAX, DEAR_EDGE_MIN, MAX_NOTES, MIN_LEAGUES, build_asymmetries
from sleeper_tool.portfolio_exposure import PortfolioExposure
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, SCARCE, VERY_SCARCE, PlayerReplacementContext, ReplacementMarket
from sleeper_tool.report_data import LeagueReportData


def _ld(name, players):
    """players: {pid: (scarcity, edge)}"""
    market = ReplacementMarket(positions={}, players={
        pid: PlayerReplacementContext(make_entry(player_id=pid, name=pid.upper(), position="WR"), 10.0, edge, None, None, scarcity)
        for pid, (scarcity, edge) in players.items()
    })
    return LeagueReportData(league=make_league_info(name=name), drafted=True, replacement=market)


def _portfolio(counts):
    return PortfolioExposure(total_leagues=9, players=[], counts_by_player_id=counts)


def test_cheapest_and_dearest_league_are_named_for_a_widely_held_player():
    leagues = [
        _ld("A", {"x": (ABUNDANT, 0.4)}),
        _ld("B", {"x": (VERY_SCARCE, 6.0)}),
        _ld("C", {"x": (NORMAL, 0.9)}),
        _ld("D", {"x": (SCARCE, 3.0)}),  # scarce but under the dear bar
    ]
    notes = build_asymmetries(_portfolio({"x": MIN_LEAGUES}), leagues)
    assert len(notes) == 1
    n = notes[0]
    assert n.cheapest.league == "A" and n.dearest.league == "B" and n.leagues_held == MIN_LEAGUES
    assert n.describe() == "X (WR), held in 4 leagues: cheapest to move in A (Abundant +0.4/wk over the wire); costliest in B (Very Scarce +6.0/wk over the wire)"


def test_boundaries_and_the_exposure_floor():
    at_cheap = [_ld("A", {"x": (ABUNDANT, CHEAP_EDGE_MAX)}), _ld("B", {"x": (SCARCE, DEAR_EDGE_MIN)})]
    notes = build_asymmetries(_portfolio({"x": MIN_LEAGUES}), at_cheap)
    assert notes and notes[0].cheapest.edge == CHEAP_EDGE_MAX and notes[0].dearest.edge == DEAR_EDGE_MIN
    over_cheap = [_ld("A", {"x": (ABUNDANT, CHEAP_EDGE_MAX + 0.1)})]
    assert build_asymmetries(_portfolio({"x": MIN_LEAGUES}), over_cheap) == []
    under_dear = [_ld("A", {"x": (ABUNDANT, 0.0)}), _ld("B", {"x": (SCARCE, DEAR_EDGE_MIN - 0.1)})]
    assert build_asymmetries(_portfolio({"x": MIN_LEAGUES}), under_dear)[0].dearest is None
    # Held in fewer leagues than the floor: not a portfolio fact.
    assert build_asymmetries(_portfolio({"x": MIN_LEAGUES - 1}), at_cheap) == []
    # A scarce market is never "cheap", however small the edge.
    assert build_asymmetries(_portfolio({"x": MIN_LEAGUES}), [_ld("A", {"x": (VERY_SCARCE, 0.0)})]) == []


def test_ordering_cap_and_skipped_leagues():
    leagues = [_ld("A", {f"p{i}": (ABUNDANT, 0.0) for i in range(MAX_NOTES + 2)})]
    leagues.append(LeagueReportData(league=make_league_info(name="Broken"), error="boom"))
    leagues.append(LeagueReportData(league=make_league_info(name="Predraft"), drafted=False))
    counts = {f"p{i}": MIN_LEAGUES + (i % 2) for i in range(MAX_NOTES + 2)}
    notes = build_asymmetries(_portfolio(counts), leagues)
    assert len(notes) == MAX_NOTES
    held = [n.leagues_held for n in notes]
    assert held == sorted(held, reverse=True)
    assert build_asymmetries(None, leagues) == []
