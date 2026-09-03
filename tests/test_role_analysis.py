from sleeper_tool.nfl_schedule import schedule_from_rows
from sleeper_tool.role_analysis import (
    BYE,
    DID_NOT_PLAY,
    UNKNOWN_ABSENCE,
    missing_week_reasons,
    player_role_windows,
    role_window_for_weeks,
    team_opportunity_leaders,
)
from usage_fixtures import make_player_week, make_team_week, make_usage


def _game(week, home, away):
    return {"season": 2025, "week": week, "game_type": "REG", "home": home, "away": away, "gameday": None}


def test_windows_average_played_games_and_use_team_denominators():
    usage = make_usage([
        make_player_week("g1", w, targets=float(3 * w), snap_pct=0.3 + 0.1 * w)
        for w in (1, 2, 3, 4)
    ])
    windows = player_role_windows(usage, "g1")

    assert windows.games == 4 and windows.played_weeks == [1, 2, 3, 4]
    assert windows.latest.weeks == [4] and windows.latest.targets == 12.0
    assert windows.last2.weeks == [3, 4] and windows.last3.weeks == [2, 3, 4]
    assert windows.season.targets == 7.5
    # target share is per-week targets / that week's team targets (30), averaged
    assert windows.season.target_share == 0.25
    assert windows.last3.target_share == 0.30
    assert round(windows.season.snap_pct, 10) == 0.55
    assert not windows.traded


def test_a_bye_is_not_a_zero_usage_game():
    weeks = (1, 2, 4, 5)  # no week 3 row at all
    usage = make_usage([make_player_week("g1", w, targets=6.0) for w in weeks], weeks=(1, 2, 3, 4, 5))
    windows = player_role_windows(usage, "g1")

    assert windows.games == 4 and windows.played_weeks == [1, 2, 4, 5]
    assert windows.last3.weeks == [2, 4, 5]  # three games, spanning four weeks
    assert windows.season.targets == 6.0  # a bye would have dragged this to 4.8


def test_a_row_with_no_snaps_and_no_opportunity_is_not_a_played_game():
    usage = make_usage([
        make_player_week("g1", 1, targets=5.0),
        make_player_week("g1", 2, targets=0.0, receptions=0.0, snaps=0, snap_pct=0.0),
    ])
    assert player_role_windows(usage, "g1").played_weeks == [1]


def test_a_missing_snap_row_leaves_snap_pct_none_without_losing_the_other_stats():
    usage = make_usage([
        make_player_week("g1", 1, snaps=None, snap_pct=None, targets=4.0),
        make_player_week("g1", 2, snaps=40, snap_pct=0.8, targets=6.0),
    ])
    windows = player_role_windows(usage, "g1")
    assert windows.season.snap_pct == 0.8  # averaged over the week it exists for
    assert windows.season.targets == 5.0

    no_snaps_at_all = make_usage([make_player_week("g2", 1, snaps=None, snap_pct=None, targets=4.0)])
    assert player_role_windows(no_snaps_at_all, "g2").season.snap_pct is None


def test_a_zero_or_missing_denominator_gives_none_not_zero():
    usage = make_usage(
        [make_player_week("g1", 1, targets=4.0, carries=2.0), make_player_week("g1", 2, targets=4.0, carries=2.0)],
        team_weeks=[make_team_week("KC", 1, targets=0.0, carries=20.0)],  # week 1 has no targets, week 2 has no row
    )
    window = player_role_windows(usage, "g1").season
    assert window.target_share is None
    assert window.carry_share == 0.1  # week 1 only, and it still works
    assert window.targets == 4.0


def test_a_traded_player_is_measured_against_the_team_he_played_for():
    usage = make_usage(
        [make_player_week("g1", w, team="KC" if w <= 2 else "LAR", targets=4.0) for w in (1, 2, 3, 4)],
        team_weeks=[make_team_week("KC", w, targets=40.0) for w in (1, 2)] + [make_team_week("LAR", w, targets=10.0) for w in (3, 4)],
    )
    windows = player_role_windows(usage, "g1")
    assert windows.traded and windows.teams == ["KC", "LAR"]
    assert role_window_for_weeks(usage, "g1", [1, 2]).target_share == 0.1
    assert windows.last2.target_share == 0.4  # same four targets, smaller offense


def test_no_rows_and_no_season_are_different_answers():
    usage = make_usage([make_player_week("someone_else", 1)])
    never_played = player_role_windows(usage, "g1")
    assert never_played.games == 0 and never_played.history_available

    assert player_role_windows(None, "g1").history_available is False
    assert player_role_windows(make_usage([]), "g1").history_available is False


def test_missing_weeks_are_only_called_byes_when_the_schedule_says_so():
    usage = make_usage([make_player_week("g1", w) for w in (1, 2, 4, 5)], weeks=(1, 2, 3, 4, 5))
    schedule = schedule_from_rows(
        [_game(w, "KC", "LAR") for w in (1, 2, 4, 5)] + [_game(3, "DAL", "LAR")],
        2025,
    )
    assert missing_week_reasons(usage, "g1", schedule=schedule) == [(3, BYE)]
    assert missing_week_reasons(usage, "g1") == [(3, UNKNOWN_ABSENCE)]

    hurt = make_usage([make_player_week("g1", w) for w in (1, 2, 5)], weeks=(1, 2, 3, 4, 5))
    reasons = dict(missing_week_reasons(hurt, "g1", schedule=schedule))
    assert reasons == {3: BYE, 4: DID_NOT_PLAY}


def test_team_opportunity_leaders_are_ordered_and_filterable():
    usage = make_usage([
        make_player_week("wr_big", w, position="WR", targets=9.0) for w in (1, 2)
    ] + [
        make_player_week("wr_small", w, position="WR", targets=3.0) for w in (1, 2)
    ] + [
        make_player_week("rb", w, position="RB", targets=1.0, carries=15.0) for w in (1, 2)
    ])
    leaders = team_opportunity_leaders(usage, "KC")
    assert [l.gsis_id for l in leaders] == ["rb", "wr_big", "wr_small"]
    assert round(leaders[1].opportunity_share, 4) == round(9.0 / 50.0, 4)

    wrs = team_opportunity_leaders(usage, "KC", position="WR", weeks=[2])
    assert [l.gsis_id for l in wrs] == ["wr_big", "wr_small"] and all(l.games == 1 for l in wrs)
    assert team_opportunity_leaders(usage, None) == [] and team_opportunity_leaders(None, "KC") == []


def test_window_shares_never_exceed_one():
    usage = make_usage([make_player_week("g1", 1, targets=30.0, carries=20.0)])
    window = player_role_windows(usage, "g1").season
    assert window.target_share == 1.0 and window.opportunity_share == 1.0
