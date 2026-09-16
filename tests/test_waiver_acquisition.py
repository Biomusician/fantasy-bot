from __future__ import annotations

import pytest
from waiver_fixtures import WEEK, player, roster

from sleeper_tool.lineup_optimizer import optimize_lineup, optimize_lineup_after_moves
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, SCARCE
from sleeper_tool.roster_needs import (
    ADEQUATE,
    CRITICAL_NEED,
    STRONG,
    SURPLUS,
    WEAK,
    PositionNeed,
    RosterNeeds,
    assess_roster_needs,
)
from sleeper_tool.waiver_acquisition import (
    BYE_COVER,
    DEPTH_ADD,
    FLEX_DEPTH,
    IMMEDIATE_STARTER,
    MAJOR_GAIN,
    NO_DROP_REASONS,
    PASS,
    PRIORITY_ADD,
    SPECULATIVE_ADD,
    SPECULATIVE_STASH,
    STREAMER,
    STRONG_ADD,
    UPSIDE_BENCH,
    LeagueWaiverContext,
    _connected_floor,
    _gain,
    _single_starter_position,
    assess_all,
    assess_candidate,
    candidate_universe,
    drop_rejection,
    pick_drop,
)
from sleeper_tool.waiver_drops import build_drop_board
from sleeper_tool.waiver_evidence import (
    DEEPER_LEAGUES_ONLY,
    FANTASYPROS_HIGHER,
    SUPERFLEX_ONLY,
    SUPPORT_AGREE,
    SUPPORT_BROAD,
    SUPPORT_NONE,
    SUPPORT_SINGLE,
    WaiverEvidence,
)

POSITIONS = ("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN", "BN")


def starters():
    """QB 30 / RB 25, 20 / WR 23, 18, 12 / TE 14 / FLEX rb3 10."""
    return [
        player("qb1", "QB", 30), player("rb1", "RB", 25), player("rb2", "RB", 20),
        player("wr1", "WR", 23), player("wr2", "WR", 18), player("wr3", "WR", 12),
        player("te1", "TE", 14), player("rb3", "RB", 10),
    ]


def evidence(
    entry, *, rank=None, support=SUPPORT_NONE, scarcity=None, labels=(), note=None, projected=True,
    fp_ros=None, boone=None, startable=None, rosterable=None,
):
    """`startable`/`rosterable` are this league's depth thresholds as
    waiver_evidence computes them (teams x starting demand, and 1.75x that);
    a test that sets a ROS rank has to set the depth it is judged against."""
    return WaiverEvidence(
        player_id=entry.player_id, name=entry.name, position=entry.position, team=entry.team,
        weekly_projection=entry.value.proj_points if projected else None,
        ballers_rank=rank, support=support, scarcity=scarcity, labels=list(labels), rotoballer_note=note,
        fp_ros_pos_rank=fp_ros, boone_pos_rank=boone, startable_depth=startable, rosterable_depth=rosterable,
    )


def make_ctx(
    entries=None,
    *,
    positions=POSITIONS,
    kind="redraft",
    claim_week=None,
    free_agents=(),
    ranks=None,
    mkt=None,
    num_teams=10,
    trade_pieces=(),
    open_spots=0,
    drops_see_week_lineup=True,
    needs=None,
):
    entries = starters() if entries is None else entries
    r = roster(entries, positions=positions, kind=kind)
    ranks = ranks or {}

    def ros_pos_rank(e):
        return ranks.get(e.player_id)

    lineup = optimize_lineup(r)
    week_lineup = optimize_lineup(r, nfl_week=claim_week) if claim_week is not None else None
    if needs is None:
        needs = assess_roster_needs(
            r, lineup=lineup, market=mkt, free_agents=free_agents, current_week=WEEK, claim_week=claim_week,
        )
    drops = build_drop_board(
        r, lineup=lineup, week_lineup=week_lineup if drops_see_week_lineup else None, num_teams=num_teams,
        ros_pos_rank=ros_pos_rank, trade_piece_ids=trade_pieces, current_week=WEEK, open_spots=open_spots,
    )
    return LeagueWaiverContext(
        roster=r, lineup=lineup, week_lineup=week_lineup, needs=needs, drops=drops, free_agents=list(free_agents),
        current_week=WEEK, claim_week=claim_week, ros_pos_rank=ros_pos_rank,
    )


def call_for(entry, ctx, *, open_spot=True, **ev_kw):
    return assess_candidate(entry, evidence(entry, **ev_kw), ctx, open_spot_available=open_spot)


def needs_of(**labels):
    """A RosterNeeds with hand-set labels, so a strength test isn't also a
    roster_needs test. weakest_projection is the WR3 of the shared roster."""
    groups = {
        g: PositionNeed(group=g, label=labels.get(g, ADEQUATE), weakest_projection=12.0, weakest_starter="wr3")
        for g in ("QB", "RB", "WR", "TE", "FLEX")
    }
    return RosterNeeds(groups=groups)


# -- candidate universe ------------------------------------------------------------------


def fa_pool():
    return [player(f"p{i}", pos, proj) for i, (pos, proj) in enumerate(
        [("WR", 100), ("WR", 90), ("RB", 80), ("RB", 70), ("TE", 60), ("QB", 50)], start=1
    )]


def test_universe_orders_expert_top_then_projection_then_deeper_boards_then_trending():
    pool = fa_pool()
    pool.append(player("top", "WR", 1))       # a board's #2, no projection edge
    pool.append(player("listed", "WR", 1))    # a board's #20
    pool.append(player("deep", "WR", 1))      # a board's #40
    pool.append(player("trend", "WR", 1))     # Sleeper trending only
    universe = candidate_universe(
        pool, expert_rank={"top": 2, "listed": 20, "deep": 40}, trending_ids=["trend"], per_position=1,
    )
    # The per-position picks follow the `positions` order: QB, RB, WR, TE.
    assert [e.player_id for e in universe] == ["top", "p6", "p3", "p1", "p5", "listed", "trend", "deep"]


def test_a_board_ranked_player_never_vanishes_because_the_cap_is_small():
    pool = fa_pool() + [player("top", "WR", 1)]
    universe = candidate_universe(pool, expert_rank={"top": 1}, cap=2, per_position=6)
    assert [e.player_id for e in universe] == ["top", "p6"]


def test_universe_dedupes_and_only_keeps_the_positions_asked_for():
    pool = fa_pool() + [player("k1", "K", 200)]
    universe = candidate_universe(pool, expert_rank={"p1": 3}, trending_ids=["p1"], per_position=2)
    ids = [e.player_id for e in universe]
    assert ids.count("p1") == 1 and "k1" not in ids


def test_kickers_and_defenses_are_excluded_on_purpose_even_when_a_board_ranks_them():
    # Deliberate, documented in the module docstring: this module has no
    # depth model for K/DEF and streamer_planner owns that decision.
    pool = fa_pool() + [player("k1", "K", 200), player("def1", "DEF", 200)]
    universe = candidate_universe(pool, expert_rank={"k1": 1, "def1": 2}, trending_ids=["k1", "def1"], per_position=6)
    ids = [e.player_id for e in universe]
    assert "k1" not in ids and "def1" not in ids


def test_expert_ranks_for_players_who_are_not_free_agents_are_ignored():
    universe = candidate_universe(fa_pool(), expert_rank={"rostered": 1}, per_position=1)
    assert "rostered" not in [e.player_id for e in universe]


def test_unprojected_free_agents_only_arrive_through_a_board_or_trending():
    pool = [player("ghost", "WR", None)]
    assert candidate_universe(pool, expert_rank={}) == []
    assert [e.player_id for e in candidate_universe(pool, expert_rank={"ghost": 40})] == ["ghost"]
    assert [e.player_id for e in candidate_universe(pool, expert_rank={}, trending_ids=["ghost"])] == ["ghost"]


# -- classes -------------------------------------------------------------------------------


def test_immediate_starter_when_he_enters_the_structural_lineup():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 20), ctx)
    assert call.cls == IMMEDIATE_STARTER
    assert call.structural_gain == 10.0  # he starts at WR and pushes wr3 down to FLEX
    assert call.entered_slot == "WR"
    assert call.replaces == "rb3"
    assert call.problem_key == "upgrade:rb3" and call.problem == "FLEX upgrade (over rb3)"


def test_the_starter_gain_boundary_separates_immediate_starter_from_a_depth_upgrade():
    ctx = make_ctx()
    # STARTER_MIN_GAIN is 1.0/wk, allowed DROP_TOLERANCE (0.05) of float slack.
    assert call_for(player("add", "WR", 11.0), ctx).cls == IMMEDIATE_STARTER
    at = call_for(player("add", "WR", 10.96), ctx)
    below = call_for(player("add", "WR", 10.9), ctx)
    assert (at.cls, at.structural_gain) == (IMMEDIATE_STARTER, 0.96)
    assert (below.cls, below.structural_gain) == (FLEX_DEPTH, 0.9)


def test_no_lineup_gain_at_all_is_not_a_flex_depth_upgrade():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 5.0), ctx)
    assert call.cls is None
    assert call.structural_gain == 0.0
    assert call.pass_reason == "no path onto this lineup and no waiver board recommends him"
    assert call.strength == PASS


def test_the_pass_reason_names_the_season_rank_instead_of_claiming_no_source_lists_him():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 5.0), ctx, fp_ros=62, startable=12, rosterable=21)
    assert call.cls is None
    assert call.pass_reason == (
        "no path onto this lineup, and FantasyPros ROS WR62 is depth here rather than an upgrade"
    )


def test_the_pass_reason_never_says_no_board_recommends_a_player_a_board_ranks():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 5.0), ctx, rank=38)
    assert call.cls is None
    assert "no waiver board recommends him" not in call.pass_reason
    assert call.pass_reason == "no path onto this lineup, and no waiver board has him inside its first 30 (best #38)"


def bye_ctx(bye_on="wr1", **kw):
    entries = [e for e in starters() if e.player_id != bye_on]
    original = next(e for e in starters() if e.player_id == bye_on)
    entries.append(player(bye_on, original.position, original.value.proj_points, bye_week=5))
    return make_ctx(entries, claim_week=5, **kw)


def test_injury_bye_cover_helps_the_claim_week_only():
    ctx = bye_ctx()
    call = call_for(player("add", "WR", 9.0), ctx)
    assert call.cls == BYE_COVER
    assert (call.structural_gain, call.week_gain) == (0.0, 9.0)
    assert call.problem_key == "cover:5:WR" and call.problem == "Week 5 WR cover"
    assert call.why[0] == "starts at WR in week 5 for +9.0 pts"


def streamer_ctx():
    """rb1 is on bye in the claim week, so his FLEX-playing backup moves up
    and the FLEX slot — a group with no hole of its own — opens."""
    entries = [
        player("qb1", "QB", 30), player("rb1", "RB", 25, bye_week=5), player("rb2", "RB", 20),
        player("wr1", "WR", 23), player("wr2", "WR", 18), player("te1", "TE", 14), player("rb3", "RB", 10),
    ]
    return make_ctx(entries, positions=("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN"), claim_week=5)


def test_a_qb_or_te_filling_a_claim_week_slot_with_no_hole_in_that_group_is_a_streamer():
    ctx = streamer_ctx()
    assert ctx.needs.groups["FLEX"].claim_week_hole is False
    call = call_for(player("add", "TE", 10.5), ctx)
    assert call.cls == STREAMER
    assert (call.structural_gain, call.week_gain) == (0.5, 10.5)
    assert call.problem_key == "stream:TE" and call.problem == "TE streamer"


def test_the_same_claim_week_slot_for_a_non_streaming_position_is_a_cover():
    ctx = streamer_ctx()
    call = call_for(player("add", "RB", 10.5), ctx)
    assert call.cls == BYE_COVER


def test_becoming_the_best_cover_at_a_position_is_a_depth_upgrade():
    ctx = make_ctx(starters() + [player("wr4", "WR", 8.0)])
    # 9.5 is below the lineup floor (no gain) but 1.5/wk better cover than wr4
    # and a credible fill for wr3 (12 x 0.75 = 9).
    call = call_for(player("add", "WR", 9.5), ctx)
    assert call.cls == FLEX_DEPTH
    assert (call.structural_gain, call.week_gain) == (0.0, 0.0)
    assert call.why[0] == "becomes your best WR cover"
    assert call.problem_key == "depth:WR"


def test_an_abundant_market_is_its_own_depth_so_a_pure_cover_add_is_not_one():
    ctx = make_ctx(starters() + [player("wr4", "WR", 8.0)])
    call = call_for(player("add", "WR", 9.5), ctx, scarcity=ABUNDANT, rank=5)
    assert call.cls == UPSIDE_BENCH
    assert call.cls != FLEX_DEPTH


def test_a_backup_quarterback_in_a_one_qb_league_is_never_cover():
    ctx = make_ctx()
    call = call_for(player("add", "QB", 25.0), ctx, rank=5)
    assert call.cls == UPSIDE_BENCH  # not FLEX_DEPTH, however well he projects


def test_upside_bench_and_speculative_stash_split_at_waiver_top_and_waiver_listed():
    ctx = make_ctx()
    add = player("add", "WR", 1.0)
    assert call_for(add, ctx, rank=12).cls == UPSIDE_BENCH
    assert call_for(add, ctx, rank=13).cls == SPECULATIVE_STASH
    assert call_for(add, ctx, rank=30).cls == SPECULATIVE_STASH
    assert call_for(add, ctx, rank=31).cls is None


def test_upside_and_speculative_adds_at_the_same_position_share_one_bench_spot_problem():
    ctx = make_ctx()
    upside = call_for(player("a", "WR", 1.0), ctx, rank=3)
    stash = call_for(player("b", "WR", 1.0), ctx, rank=25)
    assert upside.problem_key == stash.problem_key == "upside:WR"
    assert (upside.problem, stash.problem) == ("Upside bench add", "Speculative stash")


def test_speculative_adds_at_different_positions_are_different_problems():
    # A QB stash and a WR stash are not substitutes, so they must not share a
    # key — a shared key makes the second one a backup that inherits the
    # first's drop and can never clear alongside it.
    ctx = make_ctx()
    wr = call_for(player("a", "WR", 1.0), ctx, rank=3)
    rb = call_for(player("b", "RB", 1.0), ctx, rank=25)
    qb = call_for(player("c", "QB", 1.0), ctx, rank=3)
    assert len({wr.problem_key, rb.problem_key, qb.problem_key}) == 3
    assert (wr.problem_key, rb.problem_key, qb.problem_key) == ("upside:WR", "upside:RB", "upside:QB")


# -- the FantasyPros/Boone depth rung --------------------------------------------------------


def test_a_season_rank_inside_startable_depth_is_an_upside_bench_add_even_with_no_waiver_board():
    ctx = make_ctx()
    # The best free-agent TE in a league whose own format ranks him a starter.
    call = call_for(player("hock", "TE", 1.0), ctx, fp_ros=9, startable=12, rosterable=21)
    assert call.cls == UPSIDE_BENCH
    assert call.pass_reason is None and call.strength != PASS


def test_boone_alone_reaches_the_depth_rung_the_same_way_fantasypros_does():
    ctx = make_ctx()
    call = call_for(player("add", "RB", 1.0), ctx, boone=9, startable=12, rosterable=21)
    assert call.cls == UPSIDE_BENCH


@pytest.mark.parametrize("rank,expected", [
    (11, UPSIDE_BENCH),       # inside startable depth
    (12, UPSIDE_BENCH),       # exactly at it
    (13, SPECULATIVE_STASH),  # past it, inside rosterable depth
    (21, SPECULATIVE_STASH),  # exactly at rosterable depth
    (22, None),               # past both: not a claim
])
def test_the_depth_rung_splits_at_startable_then_rosterable_depth(rank, expected):
    ctx = make_ctx()
    call = call_for(player("add", "WR", 1.0), ctx, fp_ros=rank, startable=12, rosterable=21)
    assert call.cls == expected


def test_the_better_of_the_two_season_ranks_decides_the_rung():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 1.0), ctx, fp_ros=30, boone=8, startable=12, rosterable=21)
    assert call.cls == UPSIDE_BENCH


def test_a_season_rank_with_no_league_depth_to_judge_it_against_reaches_no_rung():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 1.0), ctx, fp_ros=3)
    assert call.cls is None


def test_a_waiver_board_rank_still_outranks_a_season_rank_on_the_ladder():
    ctx = make_ctx()
    # Ballers #5 classifies him before the depth rung is consulted at all...
    top = call_for(player("add", "WR", 1.0), ctx, rank=5, fp_ros=40, startable=12, rosterable=21)
    assert top.cls == UPSIDE_BENCH
    # ...and a board listing him deeper still answers first, keeping the
    # more conservative class.
    listed = call_for(player("add", "WR", 1.0), ctx, rank=25, fp_ros=3, startable=12, rosterable=21)
    assert listed.cls == SPECULATIVE_STASH


def test_an_add_that_fills_an_empty_slot_names_the_slot_it_fills():
    ctx = make_ctx([e for e in starters() if e.position != "TE"])
    call = call_for(player("add", "TE", 20.0), ctx)
    assert call.cls == IMMEDIATE_STARTER
    assert call.problem_key == "fill:TE" and call.problem == "Fill TE"


# -- strength ------------------------------------------------------------------------------


@pytest.mark.parametrize("need,expected", [(CRITICAL_NEED, PRIORITY_ADD), (WEAK, PRIORITY_ADD), (ADEQUATE, STRONG_ADD)])
def test_an_immediate_starter_is_a_priority_add_when_the_group_needs_help(need, expected):
    ctx = make_ctx(needs=needs_of(WR=need, FLEX=need))
    call = call_for(player("add", "WR", 11.5), ctx)  # +1.5/wk: a real but not major gain
    assert (call.cls, call.strength) == (IMMEDIATE_STARTER, expected)


def test_a_major_gain_is_a_priority_add_whatever_the_need_label_says():
    ctx = make_ctx(needs=needs_of(WR=SURPLUS, FLEX=SURPLUS))
    call = call_for(player("add", "WR", 10.0 + MAJOR_GAIN), ctx)
    assert call.structural_gain == MAJOR_GAIN
    assert call.strength == PRIORITY_ADD


def test_a_cover_is_a_strong_add_only_for_a_critical_group():
    critical = call_for(player("add", "WR", 9.0), bye_ctx(needs=needs_of(WR=CRITICAL_NEED)))
    assert critical.strength == STRONG_ADD
    ordinary = call_for(player("add", "WR", 9.0), bye_ctx(needs=needs_of(WR=WEAK)))
    assert ordinary.strength == DEPTH_ADD


def test_a_depth_upgrade_is_strong_only_with_broad_or_agreeing_waiver_boards():
    ctx = make_ctx()
    add = player("add", "WR", 10.5)
    assert call_for(add, ctx, support=SUPPORT_BROAD).strength == STRONG_ADD
    assert call_for(add, ctx, support=SUPPORT_AGREE).strength == STRONG_ADD
    assert call_for(add, ctx, support=SUPPORT_SINGLE).strength == DEPTH_ADD


def test_a_depth_upgrade_into_a_strong_room_stays_a_depth_add():
    ctx = make_ctx(needs=needs_of(WR=STRONG, FLEX=STRONG))
    assert call_for(player("add", "WR", 10.5), ctx, support=SUPPORT_BROAD).strength == DEPTH_ADD


def test_upside_bench_strength_follows_the_outside_support():
    ctx = make_ctx()
    add = player("add", "WR", 1.0)
    assert call_for(add, ctx, rank=3, support=SUPPORT_BROAD).strength == DEPTH_ADD
    assert call_for(add, ctx, rank=3, support=SUPPORT_AGREE).strength == DEPTH_ADD
    assert call_for(add, ctx, rank=3, support=SUPPORT_SINGLE).strength == SPECULATIVE_ADD


@pytest.mark.parametrize("room", [STRONG, SURPLUS])
def test_a_full_room_passes_on_an_upside_add_unless_the_support_is_broad(room):
    ctx = make_ctx(needs=needs_of(WR=room, FLEX=room))
    passed = call_for(player("add", "WR", 1.0), ctx, rank=3, support=SUPPORT_AGREE)
    assert passed.strength == PASS
    assert passed.pass_reason == f"your WR room is {room} and he has no path onto this lineup"
    kept = call_for(player("add", "WR", 1.0), ctx, rank=3, support=SUPPORT_BROAD)
    assert kept.strength == DEPTH_ADD


@pytest.mark.parametrize("room", [STRONG, SURPLUS])
def test_a_full_room_always_passes_on_a_deep_stash(room):
    ctx = make_ctx(needs=needs_of(WR=room, FLEX=room))
    call = call_for(player("add", "WR", 1.0), ctx, rank=25, support=SUPPORT_BROAD)
    assert call.strength == PASS
    assert call.pass_reason == f"your WR room is {room} and he is a deep stash with no path onto this lineup"


def test_a_deeper_leagues_tag_demotes_a_speculative_add_one_step():
    ctx = make_ctx()
    add = player("add", "WR", 1.0)
    demoted = call_for(add, ctx, rank=3, support=SUPPORT_BROAD, labels=[DEEPER_LEAGUES_ONLY], note="14+ Team Leagues")
    assert demoted.strength == SPECULATIVE_ADD  # Depth Add, demoted once

    to_pass = call_for(add, ctx, rank=25, labels=[DEEPER_LEAGUES_ONLY], note="14+ Team Leagues")
    assert to_pass.strength == PASS
    assert to_pass.pass_reason == (
        "RotoBaller tags him for deeper leagues (14+ Team Leagues) and he has no path onto this lineup"
    )


def test_a_deeper_leagues_tag_never_touches_an_add_with_a_lineup_path():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 20.0), ctx, labels=[DEEPER_LEAGUES_ONLY], note="14+ Team Leagues")
    assert call.strength == PRIORITY_ADD


def test_a_known_quantity_who_is_only_roster_depth_is_a_pass_that_names_the_rank():
    ctx = make_ctx()
    call = call_for(
        player("add", "WR", 1.0), ctx, rank=3, support=SUPPORT_BROAD, labels=[FANTASYPROS_HIGHER],
        fp_ros=18, startable=12, rosterable=21,
    )
    assert call.strength == PASS
    assert call.pass_reason == (
        "FantasyPros ROS WR18 is roster depth in this league, not a starter, and he has no path onto this lineup"
    )


def test_a_known_quantity_the_season_ranks_as_a_starter_here_survives_the_pass():
    # The narrowed rule: being a boring known quantity is only a reason to
    # pass when the season ranks him as depth, not when it ranks him a
    # starter this league somehow left on the wire.
    ctx = make_ctx()
    call = call_for(
        player("add", "WR", 1.0), ctx, rank=3, support=SUPPORT_BROAD, labels=[FANTASYPROS_HIGHER],
        fp_ros=6, startable=12, rosterable=21,
    )
    assert call.cls == UPSIDE_BENCH
    assert call.strength != PASS and call.pass_reason is None


def test_the_known_quantity_pass_keeps_its_old_wording_when_no_source_named_a_rank():
    ctx = make_ctx()
    call = call_for(player("add", "WR", 1.0), ctx, rank=3, support=SUPPORT_BROAD, labels=[FANTASYPROS_HIGHER])
    assert call.strength == PASS and call.pass_reason == "a known quantity with no path onto this lineup"


def test_a_bench_quarterback_in_an_abundant_one_qb_market_is_a_pass():
    ctx = make_ctx()
    call = call_for(player("add", "QB", 1.0), ctx, rank=3, support=SUPPORT_BROAD, scarcity=ABUNDANT)
    assert call.strength == PASS
    assert call.pass_reason == "a bench QB in a league that starts one, with comparable QBs on waivers every week"


def test_the_same_bench_quarterback_survives_in_superflex_and_in_a_scarce_market():
    superflex = make_ctx(positions=("QB", "SUPER_FLEX", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "BN", "BN"))
    call = call_for(player("add", "QB", 1.0), superflex, rank=3, support=SUPPORT_BROAD, scarcity=ABUNDANT)
    assert call.strength != PASS
    scarce = make_ctx()
    assert call_for(player("add", "QB", 1.0), scarce, rank=3, support=SUPPORT_BROAD, scarcity=SCARCE).strength != PASS


def test_a_bench_te_in_a_one_te_league_with_no_flex_is_a_pass_too():
    ctx = make_ctx(positions=("QB", "RB", "RB", "WR", "WR", "WR", "TE", "BN", "BN", "BN", "BN"))
    call = call_for(player("add", "TE", 1.0), ctx, rank=3, support=SUPPORT_BROAD, scarcity=ABUNDANT)
    assert call.strength == PASS and "bench TE" in call.pass_reason


def test_a_flex_slot_makes_a_one_te_league_a_two_te_league_for_this_rule():
    # The Surfeit runs two FLEX slots and started 11 TEs; counting the literal
    # "TE" token called it a league that starts one and passed on its best
    # available TE.
    one_flex = make_ctx()  # POSITIONS: one TE slot plus a FLEX
    assert call_for(player("add", "TE", 1.0), one_flex, rank=3, support=SUPPORT_BROAD, scarcity=ABUNDANT).strength != PASS
    two_flex = make_ctx(positions=("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "BN", "BN", "BN"))
    assert call_for(player("add", "TE", 1.0), two_flex, rank=3, support=SUPPORT_BROAD, scarcity=ABUNDANT).strength != PASS


@pytest.mark.parametrize("pos,positions,expected", [
    ("QB", POSITIONS, True),                                                   # 1QB, FLEX is RB/WR/TE only
    ("QB", ("QB", "SUPER_FLEX", "RB", "WR", "TE", "BN"), False),               # the superflex IS a second QB slot
    ("QB", ("QB", "QB", "RB", "WR", "TE", "BN"), False),                       # two literal QB slots
    ("TE", ("QB", "RB", "WR", "TE", "BN"), True),                              # one TE slot, no flex
    ("TE", POSITIONS, False),                                                  # a FLEX can take the second TE
    ("TE", ("QB", "RB", "WR", "TE", "REC_FLEX", "BN"), False),                 # so can a WR/TE flex
    ("TE", ("QB", "RB", "WR", "TE", "WRRB_FLEX", "BN"), True),                 # an RB/WR flex cannot
    ("WR", ("QB", "RB", "WR", "TE", "BN"), True),                              # the rule is not QB/TE-only
    ("RB", POSITIONS, False),
    (None, POSITIONS, False),
])
def test_single_starter_positions_are_counted_over_the_slots_he_could_actually_fill(pos, positions, expected):
    r = roster(starters(), positions=positions)
    assert _single_starter_position(pos, r) is expected


# -- explanation --------------------------------------------------------------------------


def test_risks_carry_the_room_the_market_the_alternatives_and_a_missing_projection():
    fas = [player(f"alt{i}", "WR", 20.0) for i in range(3)]
    ctx = make_ctx(free_agents=fas, needs=needs_of(WR=SURPLUS, FLEX=SURPLUS))
    call = assess_candidate(
        player("add", "WR", 20.0), evidence(player("add", "WR", 20.0), scarcity=ABUNDANT, projected=False),
        ctx, open_spot_available=True,
    )
    assert "your WR room is already Surplus" not in call.risks  # an Immediate Starter is exempt
    assert "WR wire is Abundant here — comparable production keeps appearing" in call.risks
    assert "no projection in this league's sources yet" in call.risks
    assert any(r.startswith("Some Alternatives") for r in call.risks)


def test_a_normal_market_is_named_as_a_risk_only_on_a_bid_worthy_add():
    ctx = make_ctx()
    strong = call_for(player("add", "WR", 20.0), ctx, scarcity=NORMAL)
    assert "WR wire is Normal, not Scarce" in strong.risks
    depth = call_for(player("add", "WR", 10.5), ctx, scarcity=NORMAL)
    assert "WR wire is Normal, not Scarce" not in depth.risks


def test_why_leads_with_the_lineup_move_then_the_need_then_the_source_facts():
    ctx = make_ctx(needs=needs_of(WR=CRITICAL_NEED, FLEX=CRITICAL_NEED))
    ctx.needs.groups["FLEX"].reasons = ["no one to start at FLEX"]
    entry = player("add", "WR", 20.0)
    ev = evidence(entry, rank=4)
    ev.for_lines = ["Ballers #4"]
    call = assess_candidate(entry, ev, ctx, open_spot_available=True)
    assert call.why == [
        "enters your lineup at WR over rb3 for +10.0 projected pts/week",
        "FLEX is Critical Need: no one to start at FLEX",
        "Ballers #4",
    ]


# -- the drop guardrail -----------------------------------------------------------------------


def junk_bench():
    return [player("junk_te", "TE", 0.5), player("junk_wr", "WR", 1.0)]


def test_the_cheapest_viable_drop_is_used_and_recorded():
    ctx = make_ctx(starters() + junk_bench())
    call = call_for(player("add", "WR", 20.0), ctx, open_spot=False)
    assert call.drop.entry.player_id == "junk_te"
    assert call.needs_drop is True and call.rejected_drops == []


def test_an_open_roster_spot_means_no_drop_is_planned():
    ctx = make_ctx(starters() + junk_bench())
    call = call_for(player("add", "WR", 20.0), ctx, open_spot=True)
    assert call.drop is None and call.needs_drop is False


def test_a_stash_only_takes_a_dead_roster_spot():
    ctx = make_ctx(starters() + [player("cover_wr", "WR", 9.0)])
    opt = ctx.drops.option_for("cover_wr")
    assert opt.is_dead_spot is False
    call = call_for(player("add", "WR", 1.0), ctx, rank=3, open_spot=True)
    assert drop_rejection(call, opt, ctx) == "cover_wr: a stash only takes a dead roster spot"


def test_a_starter_calibre_drop_needs_a_better_ranked_add_at_the_same_position():
    ctx = make_ctx(starters() + [player("good_wr", "WR", 9.0)], ranks={"good_wr": 20, "add_wr": 10, "worse_wr": 30},
                   num_teams=10)
    opt = ctx.drops.option_for("good_wr")
    assert opt.ros_starter_calibre is True

    better = call_for(player("add_wr", "WR", 20.0), ctx, open_spot=True)
    assert drop_rejection(better, opt, ctx) is None

    worse = call_for(player("worse_wr", "WR", 20.0), ctx, open_spot=True)
    assert drop_rejection(worse, opt, ctx) == "good_wr: ROS WR20 is starter calibre here"

    other_position = call_for(player("add_rb", "RB", 30.0), ctx, open_spot=True)
    assert drop_rejection(other_position, opt, ctx) == "good_wr: ROS WR20 is starter calibre here"


def test_real_cover_is_only_given_up_for_a_big_enough_gain():
    ctx = make_ctx(starters() + [player("cover_wr", "WR", 9.0)])
    opt = ctx.drops.option_for("cover_wr")
    assert opt.cover_value == 9.0

    # A QB add in a 1QB league cannot cascade, so his gain is exactly +0.5.
    small = call_for(player("add_qb", "QB", 30.5), ctx, open_spot=True)
    assert drop_rejection(small, opt, ctx) == (
        "cover_wr: your best WR cover (9.0/wk) is worth more than this add's gain (0.5/wk)"
    )
    big = call_for(player("add_qb", "QB", 40.0), ctx, open_spot=True)
    assert big.structural_gain >= opt.cover_value
    assert drop_rejection(big, opt, ctx) is None


def test_same_position_cover_replaces_cover_without_needing_the_gain():
    ctx = make_ctx(starters() + [player("cover_wr", "WR", 9.0)])
    opt = ctx.drops.option_for("cover_wr")
    same = call_for(player("add_wr", "WR", 9.0), ctx, open_spot=True)
    assert same.structural_gain < opt.cover_value
    assert drop_rejection(same, opt, ctx) is None

    worse = call_for(player("add_wr", "WR", 8.99), ctx, open_spot=True)
    assert drop_rejection(worse, opt, ctx) is not None


def test_a_drop_who_still_starts_in_the_claim_week_after_the_add_is_refused():
    # The drop board here was built without the claim-week lineup, so the
    # week's fill-in is offered as a drop and the guardrail has to catch him.
    ctx = make_ctx(
        [e for e in starters() if e.player_id != "wr1"]
        + [player("wr1", "WR", 23, bye_week=5), player("fill_wr", "WR", 9.0), player("wr_b", "WR", 8.5)],
        claim_week=5, drops_see_week_lineup=False,
    )
    opt = ctx.drops.option_for("fill_wr")
    assert opt.cover_value < 1.5  # wr_b covers him, so the cover rule is not what refuses this
    call = call_for(player("add_qb", "QB", 30.5), ctx, open_spot=True)
    assert drop_rejection(call, opt, ctx) == "fill_wr: he starts for you in week 5 even after the add"


def test_a_player_is_never_his_own_drop():
    ctx = make_ctx(starters() + junk_bench())
    add = ctx.roster.entries[-1]
    call = call_for(add, ctx, open_spot=True)
    assert drop_rejection(call, ctx.drops.option_for(add.player_id), ctx) == f"{add.name}: the same player"


def test_no_viable_drop_makes_the_add_a_pass_and_names_the_rejections():
    ctx = make_ctx(starters() + [player("cover_wr", "WR", 9.0)])
    call = call_for(player("add_qb", "QB", 30.5), ctx, open_spot=False)
    assert call.strength == PASS
    assert call.pass_reason == NO_DROP_REASONS[0]
    assert call.rejected_drops == [
        "cover_wr: your best WR cover (9.0/wk) is worth more than this add's gain (0.5/wk)"
    ]


def test_a_roster_with_nothing_droppable_says_so_differently():
    ctx = make_ctx(starters())  # every entry is an optimizer starter
    assert ctx.drops.options == []
    call = call_for(player("add", "WR", 20.0), ctx, open_spot=False)
    assert call.strength == PASS and call.pass_reason == NO_DROP_REASONS[1]


def test_pick_drop_skips_excluded_players_and_falls_through_to_the_next_option():
    ctx = make_ctx(starters() + junk_bench())
    call = call_for(player("add", "WR", 20.0), ctx, open_spot=True)
    assert pick_drop(call, ctx).entry.player_id == "junk_te"
    assert pick_drop(call, ctx, exclude_ids={"junk_te"}).entry.player_id == "junk_wr"
    assert pick_drop(call, ctx, exclude_ids={"junk_te", "junk_wr"}) is None


# -- assess_all -----------------------------------------------------------------------------------


def test_assess_all_only_rejudges_the_no_drop_passes_against_an_open_spot():
    ctx = make_ctx(starters() + [player("cover_wr", "WR", 9.0)], needs=needs_of(WR=SURPLUS, FLEX=SURPLUS, TE=SURPLUS))
    blocked = player("add_te", "TE", 14.9)     # a real add with no viable drop
    room_pass = player("stash", "WR", 1.0)     # passed on the merits, not the drop
    ev = {"add_te": evidence(blocked), "stash": evidence(room_pass, rank=25)}

    closed = assess_all([blocked, room_pass], ev, ctx)
    assert {c.player_id: c.strength for c in closed} == {"add_te": PASS, "stash": PASS}

    opened = assess_all([blocked, room_pass], ev, ctx, open_spots=1)
    by_id = {c.player_id: c for c in opened}
    assert by_id["add_te"].strength != PASS and by_id["add_te"].needs_drop is False
    assert by_id["stash"].strength == PASS  # a merits pass is never revisited


def test_assess_all_skips_candidates_with_no_evidence_and_sorts_by_order_key():
    ctx = make_ctx(starters() + junk_bench())
    starter_add = player("big", "WR", 20.0)
    depth_add = player("small", "WR", 10.5)
    unknown = player("unknown", "WR", 15.0)
    ev = {"big": evidence(starter_add), "small": evidence(depth_add)}
    calls = assess_all([unknown, depth_add, starter_add], ev, ctx)
    assert [c.player_id for c in calls] == ["big", "small"]
    assert calls[0].order_key() < calls[1].order_key()


# -- the connected-floor shortcut -------------------------------------------------------------------


def shortcut_cases():
    plain = roster(starters(), positions=POSITIONS)
    overlap = roster(
        [player("A", "WR", 10), player("B", "RB", 9), player("C", "TE", 8)],
        positions=("WRRB_FLEX", "REC_FLEX", "BN"),
    )
    superflex = roster(
        [player("qb1", "QB", 30), player("qb2", "QB", 12), player("rb1", "RB", 20), player("wr1", "WR", 18)],
        positions=("QB", "RB", "WR", "SUPER_FLEX", "BN"),
    )
    hole = roster([player("rb1", "RB", 20)], positions=("QB", "RB", "WR", "BN"))
    unprojected = roster(
        [player("rb1", "RB", None), player("wr1", "WR", 5)], positions=("RB", "WR", "FLEX", "BN"),
    )
    adds = [
        player("x_qb", "QB", 31), player("x_qb2", "QB", 1), player("x_rb", "RB", 19.5),
        player("x_wr", "WR", 9), player("x_te", "TE", 100), player("x_te2", "TE", 0.0),
        player("x_none", "WR", None), player("x_k", "K", 50),
    ]
    return [(r, a, week) for r in (plain, overlap, superflex, hole, unprojected) for a in adds for week in (None, 5)]


@pytest.mark.parametrize("r,add,week", shortcut_cases())
def test_the_connected_floor_shortcut_never_changes_the_answer(r, add, week):
    base = optimize_lineup(r, nfl_week=week)
    shortcut, _after = _gain(r, base, add, nfl_week=week)
    direct = optimize_lineup_after_moves(r, add_entries=[add], nfl_week=week)
    assert shortcut == pytest.approx(direct.total_projected_points - base.total_projected_points)


def test_the_shortcut_is_skipped_whenever_a_slot_is_unfilled():
    r = roster([player("rb1", "RB", 20)], positions=("QB", "RB", "BN"))
    assert _connected_floor(optimize_lineup(r), "QB") is None
    full = roster(starters(), positions=POSITIONS)
    assert _connected_floor(optimize_lineup(full), "WR") == 10.0  # the FLEX is reachable from WR
    assert _connected_floor(optimize_lineup(full), "QB") == 30.0  # a 1QB league's QB slot reaches nothing else
    assert _connected_floor(optimize_lineup(full), None) is None


def test_a_superflex_only_row_is_demoted_in_a_league_that_starts_one_quarterback():
    # RotoBaller's "2QB Leagues" rows are its backup quarterbacks, named for a
    # format this league does not run — at least as disqualifying as a
    # league-size tag, and previously worth nothing at all.
    ctx = make_ctx()
    add = player("add", "WR", 1.0)
    demoted = call_for(add, ctx, rank=3, support=SUPPORT_BROAD, labels=[SUPERFLEX_ONLY], note="2QB Leagues")
    assert demoted.strength == SPECULATIVE_ADD  # Depth Add, demoted once

    to_pass = call_for(add, ctx, rank=25, labels=[SUPERFLEX_ONLY], note="2QB Leagues")
    assert to_pass.strength == PASS
    assert to_pass.pass_reason == (
        "RotoBaller tags him for Superflex/2QB leagues (2QB Leagues); "
        "this league starts one quarterback and he has no path onto this lineup"
    )
