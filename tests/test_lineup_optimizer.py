import pytest
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.lineup_optimizer import (
    UnsupportedSlotError,
    optimize_lineup,
    optimize_lineup_after_moves,
    starter_slots_for,
)

STANDARD = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN", "IR", "TAXI")


def _player(pid, pos, proj, **kw):
    value_kw = {k: kw.pop(k) for k in list(kw) if k in ("bye_week", "dynasty_rank", "dynasty_ecr_rank", "redraft_ecr_rank")}
    return make_entry(
        player_id=pid, name=pid, position=pos, is_starter=False,
        value=make_value(name=pid, position=pos, proj_points=proj, **value_kw),
        **kw,
    )


def _roster(entries, positions=STANDARD, kind="redraft"):
    return make_roster(
        entries=entries,
        fmt=make_format(roster_positions=positions),
        league=make_league_info(kind=kind),
    )


def test_fills_dedicated_slots_by_projection_and_flex_from_the_remainder():
    r = _roster([
        _player("qb1", "QB", 300), _player("qb2", "QB", 250),
        _player("rb1", "RB", 200), _player("rb2", "RB", 180), _player("rb3", "RB", 120),
        _player("wr1", "WR", 190), _player("wr2", "WR", 170), _player("wr3", "WR", 150),
        _player("te1", "TE", 100), _player("k1", "K", None), _player("def1", "DEF", None),
    ])
    result = optimize_lineup(r)
    assert result.slot_by_player == {
        "qb1": "QB", "rb1": "RB", "rb2": "RB", "wr1": "WR", "wr2": "WR", "te1": "TE",
        "wr3": "FLEX",  # 150 beats rb3's 120 for the flex
        "k1": "K", "def1": "DEF",
    }
    assert result.unfilled_slots == []
    assert set(result.bench_player_ids) == {"qb2", "rb3"}
    assert result.total_projected_points == pytest.approx(300 + 200 + 180 + 190 + 170 + 100 + 150)


def test_superflex_takes_the_second_qb_when_he_outprojects_the_flex_alternatives():
    r = _roster(
        [_player("qb1", "QB", 300), _player("qb2", "QB", 240), _player("rb1", "RB", 200), _player("wr1", "WR", 150)],
        positions=("QB", "RB", "SUPER_FLEX", "BN"),
    )
    result = optimize_lineup(r)
    assert result.slot_by_player == {"qb1": "QB", "rb1": "RB", "qb2": "SUPER_FLEX"}


def test_exact_matching_beats_greedy_on_partially_overlapping_flex_slots():
    # Greedy (best player -> most restrictive open slot): WR A(10) takes
    # WRRB_FLEX, RB B(9) then has nowhere to go, TE C(8) takes REC_FLEX = 18.
    # Optimal: B -> WRRB_FLEX, A -> REC_FLEX = 19.
    r = _roster(
        [_player("A", "WR", 10), _player("B", "RB", 9), _player("C", "TE", 8)],
        positions=("WRRB_FLEX", "REC_FLEX"),
    )
    result = optimize_lineup(r)
    assert result.total_projected_points == pytest.approx(19)
    assert result.slot_by_player == {"B": "WRRB_FLEX", "A": "REC_FLEX"}


def test_bye_week_exclusion_only_applies_when_a_week_is_given():
    r = _roster(
        [_player("rb1", "RB", 200, bye_week=7), _player("rb2", "RB", 100)],
        positions=("RB", "BN"),
    )
    assert optimize_lineup(r).slot_by_player == {"rb1": "RB"}
    on_bye = optimize_lineup(r, nfl_week=7)
    assert on_bye.slot_by_player == {"rb2": "RB"}
    assert on_bye.unavailable == {"rb1": "on bye week 7"}
    assert optimize_lineup(r, nfl_week=8).slot_by_player == {"rb1": "RB"}


def test_ir_taxi_inactive_and_suspended_players_are_unavailable_but_questionable_is_not():
    r = _roster(
        [
            _player("ir", "RB", 300, is_reserve=True),
            _player("taxi", "RB", 290, is_taxi=True),
            _player("sus", "RB", 270, injury_status="Sus"),
            _player("nfi", "RB", 260, status="Non Football Injury"),
            _player("holdout", "RB", 250, status="Inactive"),
            _player("q", "RB", 100, injury_status="Questionable"),
        ],
        positions=("RB", "BN"),
    )
    result = optimize_lineup(r)
    assert result.slot_by_player == {"q": "RB"}
    assert result.unavailable == {
        "ir": "in an IR/reserve slot",
        "taxi": "on the taxi squad",
        "sus": "injury status Sus",
        "nfi": "roster status Non Football Injury",
        "holdout": "roster status Inactive",
    }


def test_game_day_out_only_excludes_from_a_this_week_lineup():
    # "Out" is a one-week tag: the structural lineup (what bye planning,
    # insurance and leverage build on) keeps him; a this-week lineup drops him.
    r = _roster([_player("out", "RB", 280, injury_status="Out"), _player("rb2", "RB", 100)], positions=("RB", "BN"))
    assert optimize_lineup(r).slot_by_player == {"out": "RB"}
    this_week = optimize_lineup(r, exclude_game_day_out=True)
    assert this_week.slot_by_player == {"rb2": "RB"}
    assert this_week.unavailable == {"out": "ruled out this week"}


def test_empty_roster_positions_is_refused_not_silently_a_zero_slot_lineup():
    r = _roster([_player("rb1", "RB", 200)], positions=())
    with pytest.raises(UnsupportedSlotError):
        optimize_lineup(r)
    assert starter_slots_for(_roster([], positions=("RB", None, "BN"))) == ["RB"]  # a null entry is skipped


def test_unprojected_player_fills_a_required_slot_but_loses_to_any_projected_player():
    r = _roster([_player("k_a", "K", None), _player("k_b", "K", 3.0)], positions=("K", "BN"))
    result = optimize_lineup(r)
    assert result.slot_by_player == {"k_b": "K"}

    only_unprojected = _roster([_player("k_a", "K", None)], positions=("K", "BN"))
    result = optimize_lineup(only_unprojected)
    assert result.slot_by_player == {"k_a": "K"}
    assert result.unfilled_slots == []
    assert result.total_projected_points == 0.0


def test_unfillable_slots_are_reported_not_faked():
    r = _roster([_player("rb1", "RB", 200)], positions=("QB", "RB", "TE"))
    result = optimize_lineup(r)
    assert result.slot_by_player == {"rb1": "RB"}
    assert result.unfilled_slots == ["QB", "TE"]


def test_unknown_starter_slot_raises_instead_of_guessing():
    r = _roster([_player("rb1", "RB", 200)], positions=("RB", "MYSTERY_SLOT"))
    with pytest.raises(UnsupportedSlotError):
        optimize_lineup(r)


def test_bench_ir_and_taxi_slots_are_not_starter_slots():
    r = _roster([], positions=STANDARD)
    assert starter_slots_for(r) == ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"]


def test_equal_projections_start_the_better_ranked_player_deterministically():
    r = _roster(
        [
            _player("worse", "RB", 100, redraft_ecr_rank=80),
            _player("better", "RB", 100, redraft_ecr_rank=20),
        ],
        positions=("RB", "BN"),
    )
    for _ in range(3):
        assert optimize_lineup(r).slot_by_player == {"better": "RB"}


def test_a_player_prefers_his_dedicated_slot_over_a_flex_when_it_changes_nothing():
    r = _roster([_player("rb1", "RB", 100)], positions=("FLEX", "RB"))
    result = optimize_lineup(r)
    assert result.slot_by_player == {"rb1": "RB"}
    assert result.unfilled_slots == ["FLEX"]


def test_excluded_player_ids_are_treated_as_hypothetically_unavailable():
    r = _roster([_player("rb1", "RB", 200), _player("rb2", "RB", 100)], positions=("RB", "BN"))
    result = optimize_lineup(r, excluded_player_ids={"rb1"})
    assert result.slot_by_player == {"rb2": "RB"}
    assert result.unavailable == {"rb1": "excluded"}


def test_after_moves_swaps_players_without_mutating_the_original_roster():
    original = _roster([_player("rb1", "RB", 200), _player("rb2", "RB", 100)], positions=("RB", "RB", "BN"))
    incoming = _player("rb_new", "RB", 300, is_reserve=True)  # slot flags from his old team must not carry over
    result = optimize_lineup_after_moves(original, add_entries=[incoming], remove_player_ids={"rb2"})
    assert result.slot_by_player == {"rb_new": "RB", "rb1": "RB"}
    assert [e.player_id for e in original.entries] == ["rb1", "rb2"]
    assert incoming.is_reserve is True  # the caller's entry object was not edited either


def test_players_with_no_eligible_slot_sit_on_the_bench():
    r = _roster([_player("rb1", "RB", 200), _player("mystery", None, 500)], positions=("RB", "BN"))
    result = optimize_lineup(r)
    assert result.slot_by_player == {"rb1": "RB"}
    assert result.bench_player_ids == ["mystery"]
