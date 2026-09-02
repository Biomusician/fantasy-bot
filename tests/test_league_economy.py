from conftest import make_entry, make_roster

from sleeper_tool.league_economy import (
    FREQUENT_TRADER,
    INACTIVE_TRADER,
    PICK_ACCUMULATOR,
    PICK_SELLER,
    POSITION_HEAVY,
    build_league_economy,
    net_future_picks_by_roster,
)


def _trade(*roster_ids, status="complete"):
    return {"type": "trade", "status": status, "roster_ids": list(roster_ids)}


def _roster(rid, positions):
    entries = [make_entry(player_id=f"{rid}-{i}", position=pos) for i, pos in enumerate(positions)]
    return make_roster(roster_id=rid, owner_username=f"user{rid}", entries=entries)


def _league(counts_by_rid):
    return {rid: _roster(rid, ["RB"] * n) for rid, n in counts_by_rid.items()}


def test_trader_activity_labels_need_a_real_league_sample():
    rosters = _league({1: 3, 2: 3, 3: 3, 4: 3})
    quiet = build_league_economy(rosters, [_trade(1, 2), _trade(1, 3)], [], season="2026")
    assert quiet.limited_sample is True
    assert all(FREQUENT_TRADER not in m.labels and INACTIVE_TRADER not in m.labels for m in quiet.managers.values())

    busy = build_league_economy(rosters, [_trade(1, 2), _trade(1, 3), _trade(1, 4), _trade(2, 3, status="pending")], [], season="2026")
    assert busy.limited_sample is False and busy.total_completed_trades == 3
    assert FREQUENT_TRADER in busy.managers[1].labels and busy.managers[1].completed_trades == 3
    assert busy.managers[4].labels == []  # one trade: neither frequent nor inactive
    assert busy.managers[2].labels == [] and busy.managers[3].labels == []  # the pending trade doesn't count


def test_inactive_trader_is_zero_trades_once_the_league_has_a_sample():
    rosters = _league({1: 3, 2: 3, 3: 3, 4: 3})
    eco = build_league_economy(rosters, [_trade(1, 2), _trade(1, 3), _trade(2, 3)], [], season="2026")
    assert INACTIVE_TRADER in eco.managers[4].labels
    assert "0 completed trades" in eco.managers[4].describe()


def test_net_future_picks_credit_the_owner_and_debit_the_original_team():
    picks = [
        {"round": 1, "season": "2027", "roster_id": 2, "owner_id": 1},
        {"round": 2, "season": "2027", "roster_id": 3, "owner_id": 1},
        {"round": 1, "season": "2025", "roster_id": 1, "owner_id": 2},  # past season, ignored
        {"round": 3, "season": "2026", "roster_id": 4, "owner_id": 4},  # back with its original team
    ]
    assert net_future_picks_by_roster(picks, season="2026") == {1: 2, 2: -1, 3: -1}
    eco = build_league_economy(_league({1: 3, 2: 3, 3: 3, 4: 3}), [], picks, season="2026")
    assert PICK_ACCUMULATOR in eco.managers[1].labels and "net +2 future picks" in eco.managers[1].describe()
    assert PICK_SELLER in eco.managers[2].labels and "net -1 future pick)" in eco.managers[2].describe()
    assert PICK_SELLER in eco.managers[3].labels
    assert PICK_SELLER not in eco.managers[4].labels


def test_position_heavy_needs_both_the_ratio_and_two_players_above_median():
    # Median RB count is 4 (five rosters, so the median is a real value):
    # 6 RBs is 1.5x AND +2 -> heavy; 5 is neither.
    rosters = {
        1: _roster(1, ["RB"] * 6), 2: _roster(2, ["RB"] * 4), 3: _roster(3, ["RB"] * 4),
        4: _roster(4, ["RB"] * 5), 5: _roster(5, ["RB"] * 4),
    }
    eco = build_league_economy(rosters, [], [], season="2026")
    assert POSITION_HEAVY in eco.managers[1].labels and eco.managers[1].heavy_positions == ["RB"]
    assert POSITION_HEAVY not in eco.managers[4].labels
    # Median 2 -> 3 is 1.5x but only +1 above median -> not heavy.
    rosters = {1: _roster(1, ["TE"] * 3), 2: _roster(2, ["TE"] * 2), 3: _roster(3, ["TE"] * 2)}
    assert POSITION_HEAVY not in build_league_economy(rosters, [], [], season="2026").managers[1].labels


def test_labelled_lists_only_managers_with_something_to_say():
    eco = build_league_economy(_league({1: 3, 2: 3}), [], [{"round": 1, "season": "2027", "roster_id": 2, "owner_id": 1}], season="2026")
    assert {m.roster_id for m in eco.labelled()} == {1, 2}
    assert build_league_economy(_league({1: 3, 2: 3}), [], [], season="2026").labelled() == []
