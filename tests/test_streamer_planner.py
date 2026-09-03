from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.nfl_schedule import parse_schedule_csv, schedule_from_rows
from sleeper_tool.streamer_planner import ADD, HOLD, MIN_GAIN_OVER_HOLD, SEQUENCE, SINGLE_PREFERENCE_TOLERANCE, plan_streams

# Weeks 1-3; LAR bye week 3, PHI bye week 3, KC and DAL play every week.
CSV = """game_id,season,game_type,week,gameday,away_team,home_team
a,2026,REG,1,2026-09-10,KC,LA
b,2026,REG,1,2026-09-13,DAL,PHI
c,2026,REG,2,2026-09-20,LA,DAL
d,2026,REG,2,2026-09-20,PHI,KC
e,2026,REG,3,2026-09-27,KC,DAL
"""
SCHED = schedule_from_rows(parse_schedule_csv(CSV, 2026), 2026)


def _p(pid, pos, team, proj, *, starter=False, bye=None):
    # current_week=1 -> games_remaining 17 -> per-game = proj / 17
    return make_entry(
        player_id=pid, name=pid, position=pos, team=team, is_starter=starter,
        value=make_value(name=pid, position=pos, proj_points=proj, bye_week=bye),
    )


def _roster(entries, positions=("QB", "TE", "K", "DEF", "BN", "BN")):
    return make_roster(roster_id=1, owner_id="me", owner_username="me", entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(kind="redraft"))


def _plans(entries, fas, *, schedule=SCHED, positions=("QB", "TE", "K", "DEF", "BN", "BN"), week=1):
    r = _roster(entries, positions)
    return plan_streams(r, fas, schedule=schedule, current_week=week, lineup=optimize_lineup(r))


def test_bye_week_makes_a_two_player_sequence_worth_it():
    # My TE (LAR) sits week 3; a KC free agent projects nearly as well and
    # plays all three. Holding = 2 games; single add = 3 slightly smaller
    # games; the best sequence keeps mine for weeks 1-2 then switches.
    mine = _p("my_te", "TE", "LAR", 170, starter=True)  # 10.0/wk -> 20.0 over the window
    fa = _p("fa_te", "TE", "KC", 153)  # 9.0/wk -> 27.0
    plans = _plans([mine], [fa])
    te = next(p for p in plans if p.position == "TE")
    assert te.current.entry.player_id == "my_te" and te.current.total == 20.0
    assert [w.note for w in te.current.weeks] == [None, None, "bye"]
    assert te.sequence is not None and te.sequence.switch_week == 3 and te.sequence.total == 29.0
    assert te.single.entry.player_id == "fa_te" and te.single.total == 27.0
    # 27.0 < 29.0 * 0.92 = 26.68? No -> single is within tolerance -> ADD, not SEQUENCE.
    assert te.recommendation == ADD
    assert "a two-player sequence would only reach 29.0" in te.note


def test_sequence_recommended_when_clearly_better_than_any_single():
    # Two free agents, each on bye inside the window: neither alone beats
    # the pair by 8%.
    mine = _p("my_k", "K", "PHI", 51, starter=True)  # 3/wk, bye wk3 -> 6.0
    a = _p("fa_a", "K", "LAR", 170)  # 10/wk, bye wk3 -> 20.0
    b = _p("fa_b", "K", "PHI", 170)  # 10/wk, bye wk3 -> 20.0
    c = _p("fa_c", "K", "KC", 119)  # 7/wk all three -> 21.0
    k = next(p for p in _plans([mine], [a, b, c]) if p.position == "K")
    assert k.recommendation == SEQUENCE
    assert k.sequence.first.entry.player_id == "fa_a" and k.sequence.second.entry.player_id == "fa_c" and k.sequence.switch_week == 3
    assert k.sequence.total == 27.0 and k.single.total == 21.0
    assert 21.0 < 27.0 * (1 - SINGLE_PREFERENCE_TOLERANCE)


def test_hold_when_no_free_agent_clears_the_bar_or_my_own_player_is_best():
    mine = _p("my_qb", "QB", "KC", 340, starter=True)  # 20/wk
    weak = _p("fa_qb", "QB", "DAL", 170)
    qb = next(p for p in _plans([mine], [weak]) if p.position == "QB")
    assert qb.recommendation == HOLD and "my_qb projects best" in qb.note
    # A marginal free agent below MIN_GAIN_OVER_HOLD is also a hold.
    close = _p("fa_close", "QB", "DAL", 340 + (MIN_GAIN_OVER_HOLD - 0.3) * 17 / 3)
    qb = next(p for p in _plans([mine], [close]) if p.position == "QB")
    assert qb.recommendation == HOLD
    # Two starting QBs (Superflex) that both out-project the wire: a hold
    # that must NOT tell me to "start" a player who already starts.
    qb2 = _p("my_qb2", "QB", "DAL", 300, starter=True)
    plans = _plans([mine, qb2], [weak], positions=("QB", "SUPER_FLEX", "BN"))
    qb = next(p for p in plans if p.position == "QB")
    assert qb.recommendation == HOLD and "start him over" not in qb.note and "your starters already project best" in qb.note


def test_my_own_bench_player_beating_my_starter_is_a_lineup_note_not_a_stream():
    starter = _p("st", "TE", "LAR", 100, starter=True)
    bench = _p("bn", "TE", "KC", 90)  # plays week 3 while the starter sits -> more over the window
    fa = _p("fa", "TE", "DAL", 34)
    te = next(p for p in _plans([starter, bench], [fa]) if p.position == "TE")
    # Optimizer picks the higher season projection (st) as the structural starter;
    # over this window the bench player totals more (3 games vs 2).
    assert te.recommendation == HOLD and "start him over st" in te.note


def test_positions_the_league_does_not_start_and_pre_draft_pools_are_skipped():
    mine = _p("my_qb", "QB", "KC", 340, starter=True)
    fa_def = _p("fa_def", "DEF", "DAL", 200)
    fa_qb = _p("fa_qb", "QB", "DAL", 400)
    plans = _plans([mine], [fa_def, fa_qb], positions=("QB", "BN"))
    assert [p.position for p in plans] == ["QB"]
    assert _plans([mine], []) == []  # pre-draft: report_data hands over no free agents


def test_without_a_schedule_the_sources_bye_week_is_used_and_no_game_weeks_are_dropped():
    mine = _p("my_te", "TE", "LAR", 170, starter=True, bye=2)
    fa = _p("fa_te", "TE", "KC", 153)
    te = next(p for p in _plans([mine], [fa], schedule=None) if p.position == "TE")
    assert [w.note for w in te.current.weeks] == [None, "bye", None]
    # With a schedule, a window running past the last regular-season week
    # is truncated to real weeks.
    late = _plans([mine], [fa], week=3)
    assert late and late[0].weeks == [3]


def test_superflex_streams_the_weaker_qb_and_is_deterministic():
    qb1 = _p("qb1", "QB", "KC", 400, starter=True)
    qb2 = _p("qb2", "QB", "DAL", 200, starter=True)
    fa = _p("fa_qb", "QB", "PHI", 300)  # bye wk3: 2 games of 17.6 = 35.3 vs qb2's 35.3? no: qb2 11.8*3 = 35.3
    plans = _plans([qb1, qb2], [fa], positions=("QB", "SUPER_FLEX", "BN"))
    qb = next(p for p in plans if p.position == "QB")
    assert qb.current.entry.player_id == "qb2"
    twice = _plans([qb1, qb2], [fa], positions=("QB", "SUPER_FLEX", "BN"))
    assert [(p.position, p.recommendation, p.single.entry.player_id) for p in plans] == [(p.position, p.recommendation, p.single.entry.player_id) for p in twice]
