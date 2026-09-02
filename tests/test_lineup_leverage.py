import pytest
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.lineup_leverage import (
    CLEAR_START,
    LEAN_START,
    TOSS_UP,
    build_lineup_leverage,
    decision_label,
)


def _p(pid, pos, proj, *, pctl=50.0):
    return make_entry(
        player_id=pid, name=pid, position=pos, is_starter=False,
        value=make_value(name=pid, position=pos, proj_points=proj, dynasty_value_percentile=pctl, redraft_ecr_percentile=pctl),
    )


def _roster(entries, positions):
    return make_roster(entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(kind="redraft"))


def test_decision_labels_use_the_higher_projection_as_the_denominator():
    assert decision_label(100, 96) == TOSS_UP  # 4%
    assert decision_label(100, 95) == TOSS_UP  # exactly 5%
    assert decision_label(100, 90) == LEAN_START
    assert decision_label(100, 84) == CLEAR_START
    assert decision_label(0, 0) == CLEAR_START


def test_close_calls_name_the_best_eligible_alternative_per_slot():
    r = _roster(
        [_p("rb1", "RB", 100), _p("rb2", "RB", 97), _p("wr1", "WR", 100), _p("wr2", "WR", 60)],
        positions=("RB", "WR", "BN", "BN"),
    )
    lev = build_lineup_leverage(r)
    by_slot = {d.slot: d for d in lev.decisions}
    assert by_slot["RB"].label == TOSS_UP and by_slot["RB"].alternative.player_id == "rb2"
    assert by_slot["WR"].label == CLEAR_START and by_slot["WR"].alternative.player_id == "wr2"
    assert [d.slot for d in lev.close_calls] == ["RB"]


def test_a_slot_with_no_eligible_alternative_is_a_clear_start():
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 100)], positions=("QB", "RB", "BN"))
    lev = build_lineup_leverage(r)
    assert all(d.label == CLEAR_START and d.alternative is None for d in lev.decisions)


def test_unprojected_slots_are_skipped_not_labelled():
    r = _roster([_p("k1", "K", None), _p("k2", "K", None), _p("rb1", "RB", 100)], positions=("K", "RB", "BN"))
    lev = build_lineup_leverage(r)
    assert [d.slot for d in lev.decisions] == ["RB"]


def test_bench_surplus_is_at_least_90_percent_of_the_weakest_eligible_starter():
    r = _roster(
        [
            _p("rb1", "RB", 100), _p("rb2", "RB", 80),
            _p("wr1", "WR", 100), _p("wr2", "WR", 90),  # flex-eligible: weakest eligible starter is rb2 at 80
            _p("bench_rb", "RB", 72, pctl=70),  # 72/80 = 0.90 -> surplus, displaces rb2
            _p("bench_wr", "WR", 71, pctl=90),  # 71/80 < 0.90 -> not surplus
        ],
        positions=("RB", "RB", "WR", "FLEX", "BN", "BN"),
    )
    lev = build_lineup_leverage(r)
    assert lev.lineup.slot_by_player == {"rb1": "RB", "rb2": "RB", "wr1": "WR", "wr2": "FLEX"}
    assert [s.entry.player_id for s in lev.bench_surplus] == ["bench_rb"]
    s = lev.bench_surplus[0]
    assert s.displaced_starter.player_id == "rb2" and s.displaced_slot == "RB"
    assert s.ratio == pytest.approx(0.9)


def test_bench_surplus_is_ordered_by_value_and_capped_at_three():
    r = _roster(
        [_p("rb1", "RB", 100)] + [_p(f"b{i}", "RB", 95, pctl=10 * i) for i in range(5)],
        positions=("RB", "BN", "BN", "BN", "BN", "BN"),
    )
    lev = build_lineup_leverage(r)
    assert [s.entry.player_id for s in lev.bench_surplus] == ["b4", "b3", "b2"]


def test_weekly_starter_points_divide_rest_of_season_totals_by_games_left():
    r = _roster([_p("rb1", "RB", 170)], positions=("RB",))
    assert build_lineup_leverage(r, current_week=1).weekly_starter_points == pytest.approx(10.0)
    assert build_lineup_leverage(r, current_week=8).weekly_starter_points == pytest.approx(17.0)


def test_empty_roster_returns_none():
    assert build_lineup_leverage(_roster([], positions=("RB",))) is None
