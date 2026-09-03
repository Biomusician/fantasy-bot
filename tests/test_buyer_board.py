from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.buyer_board import (
    MAX_BUYERS,
    POOR_FIT,
    POSSIBLE_FIT,
    POSSIBLE_FIT_MIN,
    STRONG_FIT,
    STRONG_FIT_MIN,
    annotate_sell_high_proposals,
    build_buyer_boards,
    fit_label,
    score_buyer,
    sell_high_candidates,
)
from sleeper_tool.league_economy import FREQUENT_TRADER, INACTIVE_TRADER, POSITION_HEAVY, LeagueEconomy, ManagerEconomy
from sleeper_tool.replacement_value import PositionMarket, ReplacementMarket
from sleeper_tool.team_status import CONTENDER, REBUILD
from sleeper_tool.trade_engine import TradeProposal

POSITIONS = ("QB", "RB", "WR", "BN", "BN")


def _p(pid, pos, value, pctl, *, age=25.0, trend="no change", starter=True):
    return make_entry(
        player_id=pid, name=pid, position=pos, age=age, is_starter=starter,
        value=make_value(name=pid, position=pos, dynasty_value=value, dynasty_value_percentile=pctl, dynasty_positional_percentile=pctl, trend=trend),
    )


def _roster(rid, entries, owner):
    return make_roster(roster_id=rid, owner_id=owner, owner_username=owner, team_name=f"Team {owner}", entries=entries,
                       fmt=make_format(roster_positions=POSITIONS), league=make_league_info(kind="dynasty"))


def _me():
    return _roster(1, [_p("qb1", "QB", 8000, 95), _p("rb1", "RB", 6000, 90), _p("wr_hot", "WR", 3500, 65, age=29.0, trend="rising"), _p("wr2", "WR", 1500, 30, starter=False)], "me")


def test_fit_labels_and_score_components():
    assert fit_label(STRONG_FIT_MIN) == STRONG_FIT and fit_label(POSSIBLE_FIT_MIN) == POSSIBLE_FIT and fit_label(POSSIBLE_FIT_MIN - 1) == POOR_FIT
    piece = _me().entries[2]  # wr_hot, 29-year-old riser
    needy = _roster(2, [_p("qb2", "QB", 8000, 95), _p("rb2", "RB", 7000, 92), _p("wr_bad", "WR", 400, 10), _p("rb_spare", "RB", 4000, 60, starter=False)], "needy")
    fit = score_buyer(needy, piece, "dynasty", their_status=CONTENDER, economy_labels=[FREQUENT_TRADER], heavy_positions=[], scarcity="Scarce", pick_value=0)
    assert fit.label == STRONG_FIT and fit.score == 6
    assert fit.reasons == ["upgrades their WR", "WR is a top need", "fits a contender timeline", "frequent trader", "WR is Scarce on waivers"]
    # Need alone is not Strong: upgrade + top need = 3 -> Possible.
    plain = score_buyer(needy, piece, "dynasty", their_status="middling", economy_labels=[], heavy_positions=[], scarcity=None, pick_value=0)
    assert plain.label == POSSIBLE_FIT and plain.score == 3
    poor = score_buyer(needy, piece, "dynasty", their_status=REBUILD, economy_labels=[INACTIVE_TRADER, POSITION_HEAVY], heavy_positions=["WR"], scarcity="Abundant", pick_value=0)
    assert poor.score < fit.score and "cuts against a rebuild timeline" in poor.reasons and "inactive trader" in poor.reasons and "already heavy at WR" in poor.reasons
    broke = _roster(3, [_p("qb3", "QB", 8000, 95), _p("rb3", "RB", 7000, 92), _p("wr_bad3", "WR", 400, 10)], "broke")  # nothing tradeable behind the top two
    assert "little to pay with" in score_buyer(broke, piece, "dynasty", their_status=CONTENDER, economy_labels=[], heavy_positions=[], scarcity=None, pick_value=0).reasons


def test_boards_hide_poor_fits_and_cap_at_three_best_first():
    me = _me()
    buyers = {}
    for i in range(2, 8):
        buyers[i] = _roster(i, [_p(f"qb{i}", "QB", 8000, 95), _p(f"rb{i}", "RB", 7000, 92), _p(f"wr_bad{i}", "WR", 400, 10), _p(f"spare{i}", "RB", 4000 + i, 60, starter=False)], f"b{i}")
    rosters = {1: me, **buyers}
    economy = LeagueEconomy(total_completed_trades=5, limited_sample=False, managers={
        2: ManagerEconomy(2, "b2", "Team b2", 5, 0, [], [FREQUENT_TRADER]), 7: ManagerEconomy(7, "b7", "Team b7", 0, 0, [], [INACTIVE_TRADER]),
    })
    status_of = {i: CONTENDER for i in buyers}
    status_of[7] = REBUILD
    boards = build_buyer_boards(me, rosters, sell_high_candidates(me, []), status_of=status_of, economy=economy, market=None, valued_picks=None)
    assert [b.candidate.player_id for b in boards] == ["wr_hot"]
    # Kickers and defenses are never sell-high pieces.
    k = _p("k1", "K", 900, 99, trend="rising", starter=False)
    assert [e.player_id for e in sell_high_candidates(_roster(9, [*me.entries, k], "me9"), [])] == ["wr_hot"]
    board = boards[0]
    assert len(board.buyers) == MAX_BUYERS and board.buyers[0].username == "b2" and board.buyers[0].label == STRONG_FIT
    assert all(b.label != POOR_FIT for b in board.buyers)
    assert board.fit_for("b7").label == POOR_FIT and "b7" not in [b.username for b in board.buyers]  # rebuild + inactive: poor, hidden from the board
    # An inactive trader never rates Strong, however good the need.
    inactive = score_buyer(buyers[2], me.entries[2], "dynasty", their_status=CONTENDER, economy_labels=[INACTIVE_TRADER], heavy_positions=[], scarcity="Scarce", pick_value=0)
    assert inactive.score >= STRONG_FIT_MIN and inactive.label == POSSIBLE_FIT
    # A manager heavy at the position is not told the position is his need.
    heavy = score_buyer(buyers[2], me.entries[2], "dynasty", their_status=CONTENDER, economy_labels=[POSITION_HEAVY], heavy_positions=["WR"], scarcity=None, pick_value=0)
    assert "already heavy at WR" in heavy.reasons and "WR is a top need" not in heavy.reasons


def test_sell_high_proposals_get_buyer_board_context():
    me = _me()
    strong = _roster(2, [_p("qb2", "QB", 8000, 95), _p("rb2", "RB", 7000, 92), _p("wr_bad", "WR", 400, 10), _p("spare", "RB", 4000, 60, starter=False)], "strong")
    weak = _roster(3, [_p("qb3", "QB", 8000, 95), _p("rb3", "RB", 7000, 92), _p("wr_ok", "WR", 3400, 70), _p("spare3", "RB", 3000, 60, starter=False)], "weak")
    rosters = {1: me, 2: strong, 3: weak}
    piece = me.entries[2]

    def proposal(username):
        return TradeProposal(league_name="L", currency="dynasty", target_username=username, target_team_name=f"Team {username}", give=[piece], receive=[],
                             my_value_total=1, their_value_total=1, rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="sell_high")

    to_weak, to_strong = proposal("weak"), proposal("strong")
    boards = build_buyer_boards(me, rosters, sell_high_candidates(me, [to_weak, to_strong]), status_of={2: CONTENDER, 3: CONTENDER}, economy=None, market=None, valued_picks=None)
    annotate_sell_high_proposals([to_weak, to_strong], boards)
    assert to_strong.rationale_for_them and to_strong.rationale_for_them[0].startswith("Buyer board: Team strong is a Strong Fit for wr_hot")
    assert to_weak.caveats and to_weak.caveats[0].startswith("Buyer board: Team strong is a Strong Fit for wr_hot") and "Team weak rates Possible Fit" in to_weak.caveats[0]
    buy_low = TradeProposal(league_name="L", currency="dynasty", target_username="weak", target_team_name="Team weak", give=[piece], receive=[],
                            my_value_total=1, their_value_total=1, rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="buy_low")
    annotate_sell_high_proposals([buy_low], boards)
    assert buy_low.caveats == [] and buy_low.rationale_for_them == []  # only sell-high proposals are annotated
