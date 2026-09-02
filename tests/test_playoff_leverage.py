from conftest import make_roster

from sleeper_tool.playoff_leverage import BUBBLE, COMFORTABLE, LONG_SHOT, OUT, classify_playoff_leverage, is_eliminated


def _league(records):
    """records: {roster_id: (wins, losses, points_for)}"""
    return {rid: make_roster(roster_id=rid, wins=w, losses=l, points_for=pf) for rid, (w, l, pf) in records.items()}


def _classify(rosters, rid, **kw):
    defaults = dict(playoff_teams=4, playoff_week_start=15, trade_deadline=11, current_week=9)
    defaults.update(kw)
    return classify_playoff_leverage(rid, rosters, **defaults)


def test_labels_relative_to_the_playoff_cut():
    # 8 games played; cut is the 4th-best team.
    rosters = _league({1: (7, 1, 900), 2: (6, 2, 880), 3: (5, 3, 870), 4: (4, 4, 860), 5: (4, 4, 850), 6: (2, 6, 700), 7: (1, 7, 600), 8: (0, 8, 500)})
    assert _classify(rosters, 1).label == COMFORTABLE  # 7 vs cut 4 -> +3
    assert _classify(rosters, 3).label == BUBBLE  # 5 vs 4 -> +1
    assert _classify(rosters, 5).label == BUBBLE  # 4 vs 4, outside on points-for
    assert _classify(rosters, 6).label == LONG_SHOT  # 2 vs 4, 6 games left
    assert _classify(rosters, 5).reason.startswith("4-4, seed 5 of 8 with 4 playoff spots; outside the line by 0 wins (points-for tiebreak)")


def test_mathematical_elimination_needs_playoff_teams_worth_of_uncatchable_teams():
    # 12 games played, 2 left: max wins for a 2-10 team is 4.
    rosters = _league({1: (10, 2, 1), 2: (9, 3, 1), 3: (8, 4, 1), 4: (7, 5, 1), 5: (5, 7, 1), 6: (2, 10, 1)})
    assert is_eliminated(rosters[6], rosters, playoff_teams=4, games_remaining=2) is True
    assert _classify(rosters, 6, current_week=13).label == OUT
    # Team 5 (max 7) can still tie team 4 at 7 — a possible tie never eliminates.
    assert is_eliminated(rosters[5], rosters, playoff_teams=4, games_remaining=2) is False
    assert _classify(rosters, 5, current_week=13).label == LONG_SHOT


def test_no_label_before_enough_games():
    rosters = _league({1: (2, 0, 1), 2: (0, 2, 1)})
    assert _classify(rosters, 2) is None
    assert _classify(_league({1: (3, 0, 1), 2: (0, 3, 1)}), 2) is not None


def test_no_label_without_playoff_format_data():
    rosters = _league({1: (5, 3, 1), 2: (3, 5, 1)})
    assert _classify(rosters, 1, playoff_teams=None) is None
    assert _classify(rosters, 1, playoff_week_start=None) is None


def test_deadline_window_marks_bubble_and_long_shot_teams_urgent():
    rosters = _league({1: (7, 1, 900), 2: (6, 2, 880), 3: (5, 3, 870), 4: (4, 4, 860), 5: (4, 4, 850), 6: (2, 6, 700)})
    bubble = _classify(rosters, 5, trade_deadline=11, current_week=9)
    assert bubble.deadline_window and bubble.urgent and "trade deadline is week 11" in bubble.reason
    comfortable = _classify(rosters, 1, trade_deadline=11, current_week=9)
    assert comfortable.deadline_window and not comfortable.urgent
    assert not _classify(rosters, 5, trade_deadline=11, current_week=8).deadline_window  # 3 weeks out
    assert not _classify(rosters, 5, trade_deadline=11, current_week=12).deadline_window  # already passed
    assert not _classify(rosters, 5, trade_deadline=None, current_week=9).deadline_window
