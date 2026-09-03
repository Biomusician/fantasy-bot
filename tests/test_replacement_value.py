from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.replacement_value import (
    ABUNDANT,
    ABUNDANT_MAX_GAP,
    NORMAL,
    NORMAL_MAX_GAP,
    SCARCE,
    SCARCE_MAX_GAP,
    VERY_SCARCE,
    build_replacement_market,
    scarcity_label,
)


def _p(pid, pos, proj, *, dyn=3000, rank=50):
    return make_entry(
        player_id=pid, name=pid, position=pos, is_starter=False,
        value=make_value(name=pid, position=pos, proj_points=proj, dynasty_value=dyn, dynasty_rank=rank, dynasty_ecr_rank=rank),
    )


def _roster(rid, entries, positions, owner="me"):
    return make_roster(
        roster_id=rid, owner_id=owner, owner_username=owner, entries=entries,
        fmt=make_format(roster_positions=positions), league=make_league_info(kind="dynasty"),
    )


def test_scarcity_label_boundaries():
    assert scarcity_label(None) == VERY_SCARCE
    assert scarcity_label(ABUNDANT_MAX_GAP) == ABUNDANT
    assert scarcity_label(ABUNDANT_MAX_GAP + 0.001) == NORMAL
    assert scarcity_label(NORMAL_MAX_GAP) == NORMAL
    assert scarcity_label(SCARCE_MAX_GAP) == SCARCE
    assert scarcity_label(SCARCE_MAX_GAP + 0.001) == VERY_SCARCE
    assert scarcity_label(-0.2) == ABUNDANT  # a free agent better than the worst starter


def test_superflex_makes_qb_scarce_without_any_position_rule():
    # Same players, same free agents; only the slot list differs.
    def league(positions):
        mine = _roster(1, [_p("qb1", "QB", 340), _p("rb1", "RB", 200)], positions)
        other = _roster(2, [_p("qb2", "QB", 300), _p("qb3", "QB", 170), _p("rb2", "RB", 190)], positions, owner="x")
        return mine, {1: mine, 2: other}

    fas = [_p("fa_qb", "QB", 150), _p("fa_rb", "RB", 170)]
    one_qb, rosters = league(("QB", "RB", "BN", "BN"))
    m = build_replacement_market(one_qb, rosters, fas, current_week=1)
    # 1QB: worst starting QB is qb2 at 300; free agent at 150 -> gap 0.5 -> Scarce (boundary inclusive).
    assert m.positions["QB"].scarcity == SCARCE
    sf, rosters = league(("QB", "RB", "SUPER_FLEX", "BN"))
    m = build_replacement_market(sf, rosters, fas, current_week=1)
    # SF: qb3 (170) now starts in the other team's SUPER_FLEX -> worst starter 170, FA 150 -> gap 0.12 -> Normal.
    # (The second QB slot pulls the worst starter DOWN toward the free agent; scarcity is about that gap.)
    assert m.positions["QB"].starter_replacement.player_id == "qb3"
    assert m.positions["QB"].scarcity == NORMAL


def test_no_free_agent_is_very_scarce_with_unavailable_not_zero_replacement():
    mine = _roster(1, [_p("te1", "TE", 120), _p("qb1", "QB", 300)], ("QB", "TE", "BN"))
    m = build_replacement_market(mine, {1: mine}, [_p("fa_qb", "QB", 100)], current_week=1)
    te = m.positions["TE"]
    assert te.scarcity == VERY_SCARCE
    assert te.waiver_replacement is None and te.waiver_replacement_projection is None
    assert m.players["te1"].projection_over_waiver is None
    assert m.players["te1"].projection_over_starter_replacement == 0.0  # he IS the worst (only) starter
    assert "no startable free agent" in te.describe()
    assert m.players["te1"].clause() == "TE market is Very Scarce"


def test_per_player_context_and_value_over_waiver():
    mine = _roster(1, [_p("rb1", "RB", 340, dyn=6000), _p("rb2", "RB", 170, dyn=2000)], ("RB", "RB", "BN"))
    fa = _p("fa_rb", "RB", 170, dyn=1500)
    m = build_replacement_market(mine, {1: mine}, [fa], current_week=1)
    assert m.players["rb1"].projection_over_waiver == 10.0  # (340-170)/17
    assert m.players["rb1"].value_over_waiver == 4500
    assert m.players["rb2"].projection_over_waiver == 0.0
    assert m.positions["RB"].scarcity == ABUNDANT  # worst starter 170 == free agent
    assert m.players["rb1"].clause().startswith("+10.0/wk over the best free-agent RB")


def test_positions_the_league_does_not_start_are_skipped():
    mine = _roster(1, [_p("qb1", "QB", 300), _p("te1", "TE", 100)], ("QB", "BN"))
    m = build_replacement_market(mine, {1: mine}, [], current_week=1)
    assert set(m.positions) == {"QB"}
    assert "te1" not in m.players


def test_rank_divergence_flags_understated_and_overstated_players():
    # rb_star is the best generic rank on the roster but a free agent nearly
    # matches him (cheap replacement); te1 is the worst generic rank but
    # nothing on waivers comes close.
    mine = _roster(
        1,
        [_p("rb_star", "RB", 220, rank=5), _p("wr1", "WR", 200, rank=60), _p("qb1", "QB", 300, rank=70), _p("te1", "TE", 230, rank=90)],
        ("QB", "RB", "WR", "TE", "BN"),
    )
    fas = [_p("fa_rb", "RB", 210), _p("fa_wr", "WR", 100), _p("fa_qb", "QB", 150), _p("fa_te", "TE", 30)]
    m = build_replacement_market(mine, {1: mine}, fas, current_week=1)
    assert [p.entry.player_id for p in m.understated] == ["te1"]  # generic 4th, over-waiver 1st
    assert [p.entry.player_id for p in m.overstated] == ["rb_star"]  # generic 1st, over-waiver 4th at +0.6/wk
    assert m.scarcest()[0].position == "TE"


def test_divergence_requires_a_real_advantage_not_just_a_relative_one():
    # Everyone is below replacement: nobody is "understated", however the
    # orderings differ.
    mine = _roster(
        1,
        [_p("a", "RB", 100, rank=5), _p("b", "WR", 90, rank=60), _p("c", "QB", 120, rank=70), _p("d", "TE", 80, rank=90)],
        ("QB", "RB", "WR", "TE", "BN"),
    )
    fas = [_p("fa_rb", "RB", 300), _p("fa_wr", "WR", 100), _p("fa_qb", "QB", 150), _p("fa_te", "TE", 200)]
    m = build_replacement_market(mine, {1: mine}, fas, current_week=1)
    assert m.understated == []


def test_missing_projections_and_lineups_are_handled():
    mine = _roster(1, [_p("qb1", "QB", None), _p("rb1", "RB", 200)], ("QB", "RB", "BN"))
    m = build_replacement_market(mine, {1: mine}, [], current_week=None)
    assert m.players["qb1"].weekly_projection is None and m.players["qb1"].projection_over_waiver is None
    assert m.positions["RB"].starter_replacement.player_id == "rb1"
    assert m.positions["QB"].starter_replacement is None  # no projected starter to measure
