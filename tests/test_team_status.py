from conftest import make_entry, make_league_info, make_roster, make_value

from sleeper_tool.team_status import (
    CONTENDER,
    MIDDLING,
    REBUILD,
    _record_weight_for_games,
    classify_team_status,
)


def _roster_with_strength(roster_id, strength_pctl, **kwargs):
    entry = make_entry(value=make_value(dynasty_value_percentile=strength_pctl), is_starter=True)
    return make_roster(roster_id=roster_id, entries=[entry], **kwargs)


def test_classify_strongest_roster_in_league_is_contender_preseason():
    rosters = {
        1: _roster_with_strength(1, 95.0),
        2: _roster_with_strength(2, 50.0),
        3: _roster_with_strength(3, 10.0),
    }
    result = classify_team_status(1, rosters, "dynasty")
    assert result.status == CONTENDER
    assert result.win_pct is None  # no games played yet


def test_classify_weakest_roster_in_league_is_rebuild_preseason():
    rosters = {
        1: _roster_with_strength(1, 95.0),
        2: _roster_with_strength(2, 50.0),
        3: _roster_with_strength(3, 10.0),
    }
    result = classify_team_status(3, rosters, "dynasty")
    assert result.status == REBUILD


def test_classify_middle_roster_is_middling():
    rosters = {i: _roster_with_strength(i, pctl) for i, pctl in enumerate([90, 70, 50, 30, 10], start=1)}
    result = classify_team_status(3, rosters, "dynasty")  # 50th percentile, middle of 5 teams
    assert result.status == MIDDLING


def test_classify_uses_win_pct_once_enough_games_played():
    # Weak roster (would be REBUILD on strength alone) but a strong record
    # with enough games to trust it should pull the classification up.
    rosters = {
        1: _roster_with_strength(1, 20.0, wins=8, losses=1, ties=0),
        2: _roster_with_strength(2, 80.0, wins=1, losses=8, ties=0),
    }
    result = classify_team_status(1, rosters, "dynasty")
    assert abs(result.win_pct - 8 / 9) < 1e-6
    assert result.status == CONTENDER  # record at full ramp weight overrides weak roster strength


def test_record_weight_ramps_gradually_not_a_step_function():
    # Below the minimum sample, record shouldn't count at all.
    assert _record_weight_for_games(3) == 0.0
    assert _record_weight_for_games(4) == 0.0
    # At the minimum, weight should just be starting to ramp in, not full weight.
    low = _record_weight_for_games(5)
    assert 0 < low < 0.65
    # It should strictly increase as more games accumulate...
    mid = _record_weight_for_games(7)
    assert low < mid < 0.65
    # ...and cap at RECORD_WEIGHT once fully ramped in.
    assert _record_weight_for_games(20) == 0.65


def test_three_game_record_no_longer_dominates_classification():
    # This is the exact scenario the red team flagged: a top roster that's
    # 0-3 by variance shouldn't get dragged to REBUILD off 3 games alone.
    rosters = {
        1: _roster_with_strength(1, 90.0, wins=0, losses=3, ties=0),
        2: _roster_with_strength(2, 10.0, wins=3, losses=0, ties=0),
    }
    result = classify_team_status(1, rosters, "dynasty")
    assert result.win_pct is None  # 3 games is below MIN_GAMES_FOR_RECORD_BLEND now
    assert result.status == CONTENDER  # falls back to roster strength alone
