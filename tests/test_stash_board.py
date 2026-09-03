from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.replacement_value import PositionMarket, ReplacementMarket
from sleeper_tool.roster_clog import RosterClog
from sleeper_tool.stash_board import PRIORITY_STASH, STASH_MAX, WATCH, build_stash_board


def _fa(pid, pos, pctl, *, age=22.0, years_exp=0):
    e = make_entry(player_id=pid, name=pid, position=pos, age=age, is_starter=False, value=make_value(name=pid, position=pos, dynasty_value_percentile=pctl, proj_points=None))
    e.years_exp = years_exp
    return e


def _roster(n_entries=3, positions=("QB", "RB", "BN")):
    entries = [make_entry(player_id=f"r{i}", name=f"r{i}") for i in range(n_entries)]
    return make_roster(entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(kind="dynasty"))


def _board(pool, **kw):
    defaults = dict(league_kind="dynasty", pre_draft=False, roster_full=False, clogs=[])
    defaults.update(kw)
    return build_stash_board(_roster(), pool, **defaults)


def test_priority_and_watch_labels_and_exemptions():
    pool = [
        _fa("rookie_wr", "WR", 75.0),
        _fa("second_year_rb", "RB", 55.0, age=23.0, years_exp=1),
        _fa("old_rookie", "WR", 80.0, age=27.0),  # too old for the position
        _fa("vet", "QB", 90.0, age=33.0, years_exp=10),  # veteran
        _fa("fourth_year", "TE", 70.0, age=24.0, years_exp=3),  # too experienced
        _fa("near_zero", "RB", 15.0),
        _fa("kicker", "K", 99.0),
    ]
    board = _board(pool)
    assert [(c.entry.player_id, c.label) for c in board] == [("rookie_wr", PRIORITY_STASH), ("second_year_rb", WATCH)]
    assert board[0].describe().endswith("— developmental hold, not lineup help")
    assert "immediate lineup help" not in board[0].describe()
    assert "75th percentile dynasty value" in board[0].reasons and "rookie, age 22" in board[0].reasons
    assert "1 season(s) in, age 23" in board[1].reasons


def test_full_roster_needs_a_clog_to_cut_for_priority_status():
    pool = [_fa("a", "WR", 80.0), _fa("b", "WR", 70.0), _fa("c", "RB", 65.0)]
    no_spot = _board(pool, roster_full=True)
    assert [c.label for c in no_spot] == [WATCH, WATCH, WATCH]
    assert "no roster spot without cutting a real player" in no_spot[0].reasons
    clog = RosterClog(make_entry(player_id="clog", name="Clog"), ["dead"], 250.0)
    with_clog = _board(pool, roster_full=True, clogs=[clog])
    assert [(c.label, c.drop.player_id if c.drop else None) for c in with_clog] == [(PRIORITY_STASH, "clog"), (WATCH, None), (WATCH, None)]
    assert with_clog[0].describe().endswith("cut Clog for the spot — developmental hold, not lineup help")


def test_redraft_and_pre_draft_are_suppressed_and_the_board_is_capped():
    pool = [_fa(f"p{i}", "WR", 90.0 - i) for i in range(8)]
    assert _board(pool, league_kind="redraft") == []
    assert _board(pool, pre_draft=True) == []
    assert len(_board(pool, league_kind="keeper")) == STASH_MAX
    assert [c.entry.player_id for c in _board(pool)] == ["p0", "p1", "p2", "p3", "p4"]  # value order, deterministic


def test_scarcity_is_a_reason_not_a_requirement():
    market = ReplacementMarket(positions={"TE": PositionMarket("TE", None, None, None, None, "Very Scarce", None)}, players={})
    board = _board([_fa("te", "TE", 65.0), _fa("wr", "WR", 65.0)], market=market)
    te = next(c for c in board if c.entry.player_id == "te")
    wr = next(c for c in board if c.entry.player_id == "wr")
    assert "TE replacements are Very Scarce here" in te.reasons and not any("replacements" in r for r in wr.reasons)
    assert te.label == wr.label == PRIORITY_STASH
