from __future__ import annotations

from waiver_fixtures import WEEK, fmt_for, player, roster

from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.waiver_drops import (
    MATERIAL_COVER_POINTS,
    PROTECT_DEVELOPMENTAL,
    PROTECT_IR_ELIGIBLE,
    PROTECT_RESERVE,
    PROTECT_STARTER,
    PROTECT_TRADE_PIECE,
    PROTECT_WEEK_STARTER,
    build_drop_board,
    startable_depth,
)

# One RB/WR/TE FLEX on top of the dedicated slots, plus bench. IR capacity is
# NOT a roster_positions entry in Sleeper: it is settings.reserve_slots, which
# build_drop_board takes as `reserve_slots`.
POSITIONS = ("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN", "BN", "BN")
ONE_IR_SLOT = 1

STARTERS = [
    player("qb_s", "QB", 30), player("rb_s1", "RB", 25), player("rb_s2", "RB", 24),
    player("wr_s1", "WR", 23), player("wr_s2", "WR", 22), player("wr_s3", "WR", 21),
    player("te_s", "TE", 20), player("rb_s3", "RB", 19),  # rb_s3 takes the FLEX
]


def board_for(bench, *, positions=POSITIONS, kind="redraft", num_teams=10, ranks=None, **kw):
    r = roster(STARTERS + list(bench), positions=positions, kind=kind)
    ranks = ranks or {}
    return build_drop_board(
        r, lineup=optimize_lineup(r), num_teams=num_teams, current_week=WEEK,
        ros_pos_rank=lambda e: ranks.get(e.player_id), **kw,
    )


# -- startable depth ------------------------------------------------------------------


def test_startable_depth_is_teams_times_this_leagues_own_demand():
    r = roster([], positions=("QB", "RB", "RB", "WR", "WR", "WR", "TE", "BN"))
    assert startable_depth(r, 10) == {"QB": 10, "RB": 20, "WR": 30, "TE": 10}


def test_flex_demand_is_already_distributed_and_a_position_never_started_is_absent():
    r = roster([], positions=("QB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "BN"))
    depth = startable_depth(r, 12)
    assert depth["RB"] == 22  # (1 + 2 x 0.40) x 12, rounded up
    assert depth["QB"] == 12
    assert "K" not in depth


def test_depth_never_drops_below_one():
    fmt = fmt_for(("QB", "BN"))
    r = roster([], positions=("QB", "BN"))
    assert fmt.starter_slots == {"QB": 1.0}
    assert startable_depth(r, 0) == {"QB": 1}


# -- protections ----------------------------------------------------------------------


def test_every_protection_is_recorded_with_its_own_reason():
    bench = [
        player("res", "WR", 18, is_reserve=True),
        player("taxi", "WR", 17, is_taxi=True),
        player("trade", "WR", 9),
        player("hurt", "WR", 2, injury_status="Out"),
        player("plain", "WR", 1),
    ]
    board = board_for(bench, trade_piece_ids=["trade"], reserve_slots=ONE_IR_SLOT)
    assert board.protected["qb_s"] == PROTECT_STARTER
    assert board.protected["res"] == PROTECT_RESERVE
    assert board.protected["taxi"] == PROTECT_RESERVE
    assert board.protected["trade"] == PROTECT_TRADE_PIECE
    # One IR slot, one player already in it: no open IR slot, so the injured
    # bench player is a normal drop candidate.
    assert "hurt" not in board.protected
    assert {o.entry.player_id for o in board.options} == {"hurt", "plain"}


def test_an_open_ir_slot_protects_an_ir_eligible_player_instead_of_dropping_him():
    board = board_for([player("hurt", "WR", 2, injury_status="Doubtful"), player("plain", "WR", 1)], reserve_slots=ONE_IR_SLOT)
    assert board.protected["hurt"] == PROTECT_IR_ELIGIBLE
    assert [o.entry.player_id for o in board.options] == ["plain"]


def test_a_healthy_player_is_never_ir_protected():
    board = board_for([player("fit", "WR", 2, injury_status="Questionable")], reserve_slots=ONE_IR_SLOT)


def test_without_ir_capacity_an_injured_bench_player_is_droppable():
    """A league whose settings report no reserve slots cannot stash him."""
    board = board_for([player("hurt", "WR", 2, injury_status="Out"), player("plain", "WR", 1)])
    assert "hurt" not in board.protected
    assert "fit" not in board.protected


def test_a_claim_week_starter_is_protected_even_though_he_is_not_a_structural_starter():
    bench = [player("byefill", "WR", 10), player("plain", "WR", 1)]
    r = roster(
        [*[e for e in STARTERS if e.player_id != "wr_s3"], player("wr_s3", "WR", 21, bye_week=5), *bench],
        positions=POSITIONS,
    )
    board = build_drop_board(
        r, lineup=optimize_lineup(r), week_lineup=optimize_lineup(r, nfl_week=5), num_teams=10, current_week=WEEK,
    )
    assert board.protected["byefill"] == PROTECT_WEEK_STARTER
    assert [o.entry.player_id for o in board.options] == ["plain"]


def test_dynasty_developmental_players_are_held_but_only_in_a_dynasty_league():
    bench = [player("rookie", "WR", 2, years_exp=0, age=22.0), player("plain", "WR", 1, years_exp=8, age=30.0)]
    dynasty = board_for(bench, kind="dynasty")
    assert dynasty.protected["rookie"] == PROTECT_DEVELOPMENTAL
    assert [o.entry.player_id for o in dynasty.options] == ["plain"]
    assert "rookie" not in board_for(bench, kind="redraft").protected


def test_protection_order_puts_the_reserve_slot_ahead_of_the_trade_piece():
    board = board_for([player("both", "WR", 9, is_reserve=True)], trade_piece_ids=["both"])
    assert board.protected["both"] == PROTECT_RESERVE


# -- ordering --------------------------------------------------------------------------


def ordering_bench():
    return [
        player("no_proj", "TE", None, status="Practice Squad"),
        player("wr_low", "WR", 1.6),
        player("wr_mid", "WR", 2.0),
        player("rb_cover", "RB", 9.0),
        player("qb_calibre", "QB", 12.0),
    ]


def test_cheapest_first_is_calibre_then_cover_then_projection():
    board = board_for(ordering_bench(), ranks={"qb_calibre": 5, "rb_cover": 999, "wr_mid": 999})
    # no_proj is LAST: his blank projection is explained by a Sleeper status,
    # which is a data gap rather than a reason to cut him.
    assert [o.entry.player_id for o in board.options] == ["wr_low", "wr_mid", "rb_cover", "no_proj", "qb_calibre"]
    assert board.option_for("qb_calibre").ros_starter_calibre is True
    assert board.option_for("rb_cover").cover_value == 9.0
    assert board.option_for("wr_mid").cover_value == 0.4
    assert board.option_for("no_proj").weekly_projection is None
    assert board.option_for("no_proj").status_caution


def test_a_worse_ros_rank_drops_first_when_everything_else_ties():
    bench = [player("wr_a", "WR", 5.0), player("wr_b", "WR", 5.0)]
    board = board_for(bench, ranks={"wr_a": 200, "wr_b": 400})
    assert [o.entry.player_id for o in board.options] == ["wr_b", "wr_a"]


def test_starter_calibre_is_the_ros_rank_inside_this_leagues_own_depth():
    depth = startable_depth(roster([], positions=POSITIONS), 10)
    assert depth["WR"] == 36  # (3 + 0.55) x 10, rounded up
    inside = board_for([player("wr_x", "WR", 5.0)], ranks={"wr_x": 36})
    assert inside.option_for("wr_x").ros_starter_calibre is True
    outside = board_for([player("wr_x", "WR", 5.0)], ranks={"wr_x": 37})
    assert outside.option_for("wr_x").ros_starter_calibre is False
    unranked = board_for([player("wr_x", "WR", 5.0)])
    assert unranked.option_for("wr_x").ros_starter_calibre is False


def test_an_injured_starter_with_no_weekly_projection_still_has_starter_calibre():
    board = board_for([player("wr_x", "WR", None, injury_status="Out")], ranks={"wr_x": 4}, positions=POSITIONS)
    opt = board.option_for("wr_x")
    assert (opt.weekly_projection, opt.ros_starter_calibre, opt.is_dead_spot) == (None, True, False)


# -- cover value ------------------------------------------------------------------------


def test_cover_is_measured_against_the_whole_bench_including_protected_players():
    bench = [player("wr_best", "WR", 9.0), player("wr_dev", "WR", 8.0, years_exp=0, age=22.0)]
    board = board_for(bench, kind="dynasty")
    assert board.protected["wr_dev"] == PROTECT_DEVELOPMENTAL
    # The held rookie still covers the position, so dropping wr_best costs 1.0/wk.
    assert board.option_for("wr_best").cover_value == 1.0
    assert board.option_for("wr_best").is_dead_spot is True


def test_material_cover_boundary():
    at = board_for([player("wr_best", "WR", 3.5), player("wr_next", "WR", 2.0)])
    assert at.option_for("wr_best").cover_value == MATERIAL_COVER_POINTS
    assert at.option_for("wr_best").is_dead_spot is False
    assert at.option_for("wr_best").reasons == ["your best WR cover by 1.5/wk"]

    below = board_for([player("wr_best", "WR", 3.49), player("wr_next", "WR", 2.0)])
    assert below.option_for("wr_best").is_dead_spot is True
    assert below.option_for("wr_best").reasons == ["3.5/wk, and another bench player covers his position as well"]


def test_a_position_the_league_never_starts_covers_nothing():
    board = board_for([player("k1", "K", 8.0)])
    opt = board.option_for("k1")
    assert (opt.cover_value, opt.ros_starter_calibre, opt.is_dead_spot) == (0.0, False, True)
    assert opt.reasons == ["8.0/wk"]


# -- reasons ----------------------------------------------------------------------------


def test_reasons_name_the_calibre_and_the_cover_in_order():
    board = board_for([player("wr_x", "WR", 9.0)], ranks={"wr_x": 12})
    assert board.option_for("wr_x").reasons == [
        "ROS WR12 — starter calibre in a 10-team league",
        "your best WR cover by 9.0/wk",
    ]
    assert board.option_for("wr_x").describe() == (
        "wr_x (WR) — ROS WR12 — starter calibre in a 10-team league; your best WR cover by 9.0/wk"
    )


def test_an_unexplained_blank_projection_still_sorts_first():
    """Nothing about him says he is unavailable, so the blank is just a blank."""
    board = board_for([player("no_proj", "WR", None), player("wr_low", "WR", 1.6)])
    assert [o.entry.player_id for o in board.options] == ["no_proj", "wr_low"]
    assert board.option_for("no_proj").status_caution is None


def test_a_player_with_no_projection_says_what_sleeper_says_about_him():
    board = board_for([player("wr_x", "WR", None, injury_status="Out")], positions=POSITIONS)
    assert board.option_for("wr_x").reasons == [
        "no projection in this league's sources (Sleeper: Out) — confirm he isn't returning soon"
    ]
    plain = board_for([player("wr_y", "WR", None)])
    assert plain.option_for("wr_y").reasons == ["no projection in this league's sources"]


def test_a_non_active_sleeper_status_is_used_when_there_is_no_injury_tag():
    board = board_for([player("wr_x", "WR", None, status="Practice Squad")])
    assert "Sleeper: Practice Squad" in board.option_for("wr_x").reasons[0]


# -- board plumbing ----------------------------------------------------------------------


def test_open_spots_are_carried_and_never_negative():
    assert board_for([player("plain", "WR", 1)], open_spots=2).open_spots == 2
    assert board_for([player("plain", "WR", 1)], open_spots=-3).open_spots == 0


def test_option_for_an_unknown_or_protected_player_is_none():
    board = board_for([player("plain", "WR", 1)])
    assert board.option_for("qb_s") is None
    assert board.option_for("nobody") is None
