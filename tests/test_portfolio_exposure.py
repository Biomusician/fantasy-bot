from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.portfolio_exposure import (
    HIGH,
    VERY_HIGH,
    acquisition_exposure_note,
    build_portfolio_exposure,
)


def _roster(name, player_ids, *, qb_ids=(), positions=("QB", "RB", "BN", "BN")):
    entries = [
        make_entry(player_id=pid, name=pid, position="QB" if pid in qb_ids else "RB", is_starter=False,
                   value=make_value(name=pid, position="QB" if pid in qb_ids else "RB", proj_points=100))
        for pid in player_ids
    ]
    r = make_roster(entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(name=name))
    return (name, r, optimize_lineup(r))


def test_counts_leagues_per_player_and_levels_them():
    holdings = [_roster(f"L{i}", ["shared", f"solo{i}"]) for i in range(6)]
    exposure = build_portfolio_exposure(holdings)
    assert exposure.total_leagues == 6
    assert exposure.players[0].player_id == "shared"
    assert exposure.players[0].count == 6
    assert exposure.players[0].level == VERY_HIGH
    assert exposure.leagues_holding("solo0") == 1
    assert [p.player_id for p in exposure.players] == ["shared"]  # single-league players aren't "concentration"


def test_high_exposure_starts_at_four_leagues():
    exposure = build_portfolio_exposure([_roster(f"L{i}", ["x"]) for i in range(4)])
    assert exposure.players[0].level == HIGH
    exposure = build_portfolio_exposure([_roster(f"L{i}", ["x"]) for i in range(3)])
    assert exposure.players[0].level is None


def test_starting_qb_exposure_is_flagged_separately_at_three_leagues():
    holdings = [_roster(f"L{i}", ["qb", "rb"], qb_ids={"qb"}) for i in range(3)]
    exposure = build_portfolio_exposure(holdings)
    qb = next(p for p in exposure.players if p.player_id == "qb")
    assert qb.started_in == ["L0", "L1", "L2"]
    assert qb.qb_start_flag is True
    assert qb.level is None  # 3 leagues isn't High Exposure on the general scale


def test_acquisition_note_only_fires_when_a_threshold_would_be_crossed():
    exposure = build_portfolio_exposure([_roster(f"L{i}", ["x"]) for i in range(3)])
    note = acquisition_exposure_note(exposure, "x", position="RB")
    assert note is not None and "4 of your 3 rosters" in note and HIGH in note
    # Already at 4: adding a 5th crosses nothing new.
    exposure = build_portfolio_exposure([_roster(f"L{i}", ["x"]) for i in range(4)])
    assert acquisition_exposure_note(exposure, "x", position="RB") is None
    # Never seen before: no note.
    assert acquisition_exposure_note(exposure, "brand_new", position="RB") is None


def test_acquisition_note_flags_a_third_starting_qb_league():
    holdings = [_roster(f"L{i}", ["qb", "rb"], qb_ids={"qb"}) for i in range(2)]
    exposure = build_portfolio_exposure(holdings)
    note = acquisition_exposure_note(exposure, "qb", position="QB")
    assert note is not None and "starting QB in 3 leagues" in note
    assert acquisition_exposure_note(exposure, "qb", position="RB") is None  # position gate


def test_listing_is_capped_and_ordered_most_concentrated_first():
    holdings = [_roster(f"L{i}", [f"p{j}" for j in range(12) if j <= i]) for i in range(12)]
    exposure = build_portfolio_exposure(holdings)
    assert len(exposure.players) == 10
    assert [p.count for p in exposure.players] == sorted([p.count for p in exposure.players], reverse=True)
    assert exposure.players[0].player_id == "p0"
