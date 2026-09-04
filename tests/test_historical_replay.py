"""Historical replay harness. Everything here is synthetic: no data/ read,
no cache read, no network. `load_cached_usage` is deliberately untested
against real files for that reason — it is a thin cache read, and the
three replay functions it feeds are pure."""
from __future__ import annotations

import datetime as dt

from sleeper_tool import decision_delta as dd
from sleeper_tool import market_velocity as mv
from sleeper_tool import role_trends as rt
from sleeper_tool.decision_ledger import (
    ACQUIRED_BY_ANOTHER,
    COMPLETED,
    OBSERVATION_WINDOW_DAYS,
    STILL_AVAILABLE,
    Ledger,
    LedgerEntry,
)
from sleeper_tool.historical_replay import (
    FORWARD_MOVE_THRESHOLD,
    MAX_EXAMPLES,
    MISSED_BREAKOUT_MIN_CHANGE,
    NOT_MEASURABLE,
    NO_GAMES,
    SHARE_DOWN,
    SHARE_HELD,
    SHARE_UP,
    RoleCase,
    build_result,
    classify_forward,
    false_breakouts,
    false_collapses,
    insufficient_share_by_week,
    missed_breakouts,
    render_backtest_markdown,
    replay_outcomes,
    replay_role_signals,
    replay_snapshots,
    snapshot_series,
    truncate_usage,
)
from usage_fixtures import make_player_week, make_team_week, make_usage


# -- builders ------------------------------------------------------------------


def flat_rows(weeks, *, gsis_id="g1", targets=5.0, snap_pct=0.60, team="KC"):
    """One steady player-week per week: 5 of the team's 50 opportunities,
    so his opportunity share is a clean 0.10."""
    return [make_player_week(gsis_id, w, team=team, targets=targets, snap_pct=snap_pct) for w in weeks]


def make_case(**kwargs) -> RoleCase:
    defaults = dict(
        week=5,
        gsis_id="g1",
        name="Player One",
        position="WR",
        team="KC",
        label=rt.STABLE,
        games=5,
        share_at=0.10,
        forward_share=0.10,
        forward_change=0.0,
        forward_games=3,
        forward_scheduled_weeks=(6, 7, 8),
        snap_at=0.6,
        forward_snap=0.6,
        opportunities_at=5.0,
        forward_opportunities=5.0,
        outcome=SHARE_HELD,
    )
    defaults.update(kwargs)
    return RoleCase(**defaults)


def make_entry(fingerprint="fp1", **kwargs) -> LedgerEntry:
    defaults = dict(
        fingerprint=fingerprint,
        run_id="2026-08-01T00:00:00+00:00",
        last_seen="2026-08-01T00:00:00+00:00",
        league_id="L1",
        league_name="Test League",
        action="waiver",
        player_ids=("p1",),
        player_names=("Player One",),
        receive_ids=("p1",),
        tier="Strong Add",
        valuation_snapshot={"p1": 12.5},
        replacement_context={"WR": "Scarce"},
        projected_lineup_delta=1.5,
        currency="redraft",
        team_status="contender",
        faab_pct=15,
    )
    defaults.update(kwargs)
    return LedgerEntry(**defaults)


def make_snapshot(date: str, values: dict[str, float], *, league_id="L1", league_name="Test League") -> dict:
    return {
        "schema": dd.SNAPSHOT_SCHEMA,
        "generated_at": f"{date}T12:00:00+00:00",
        "current_week": 5,
        "leagues": {
            league_id: {
                "name": league_name,
                "team_status": "contender",
                "trade_targets": {},
                "waiver_targets": {},
                "roster": {pid: {"name": f"Player {pid}", "value": v} for pid, v in values.items()},
                "tracked": {},
            }
        },
        "best_moves": [],
    }


NOW = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)


# -- mode 1: truncation and leakage --------------------------------------------


def test_truncate_usage_drops_future_rows_and_recomputes_latest_week():
    usage = make_usage(flat_rows(range(1, 8)), weeks=tuple(range(1, 8)))
    truncated = truncate_usage(usage, 4)

    assert max(r.week for r in truncated.player_weeks) == 4
    assert max(t.week for t in truncated.team_weeks) == 4
    assert truncated.latest_week == 4
    assert usage.latest_week == 7  # the original is untouched


def test_truncate_usage_before_any_game_has_no_latest_week():
    usage = make_usage(flat_rows(range(3, 8)), weeks=tuple(range(1, 8)))
    assert truncate_usage(usage, 2).latest_week is None


def test_a_future_share_jump_cannot_reach_the_label_at_w():
    """The leakage test. Flat through week 6, five times the volume in
    week 7: the week-6 label must be what a season ending at week 6 says,
    while the forward window still sees the jump."""
    rows = flat_rows(range(1, 7)) + [make_player_week("g1", 7, targets=25.0)]
    usage = make_usage(rows, weeks=tuple(range(1, 8)))

    assert rt.role_trend(usage, "g1").label == rt.SURGING  # with week 7 visible

    replay = replay_role_signals(usage, first_week=6, last_week=6, forward_weeks=1)
    case = next(c for c in replay.cases if c.gsis_id == "g1")
    assert case.label == rt.STABLE
    assert case.label == rt.role_trend(truncate_usage(usage, 6), "g1").label
    # ...and the forward read, taken after the label was fixed, still sees it.
    assert case.outcome == SHARE_UP
    assert case.forward_share > case.share_at


def test_replay_population_is_everyone_who_has_played():
    """Including the sub-MIN_GAMES_FOR_TREND players — otherwise the
    Insufficient share is unmeasurable and every breakout the rules were
    silent through disappears with them."""
    rows = flat_rows(range(1, 6)) + [make_player_week("rookie", 5, targets=3.0)]
    usage = make_usage(rows, weeks=tuple(range(1, 9)))

    replay = replay_role_signals(usage, first_week=5, last_week=5)
    labels = {c.gsis_id: c.label for c in replay.cases}
    assert labels["rookie"] == rt.INSUFFICIENT
    assert labels["g1"] == rt.STABLE
    assert insufficient_share_by_week(replay)[5] == (1, 2)


# -- mode 1: forward window ----------------------------------------------------


def test_a_bye_inside_the_forward_window_is_not_a_disappearance():
    rows = flat_rows(range(1, 6)) + flat_rows([7, 8])
    played_weeks = (1, 2, 3, 4, 5, 7, 8)  # week 6 is the bye: no team row either
    usage = make_usage(rows, team_weeks=[make_team_week("KC", w) for w in played_weeks])

    replay = replay_role_signals(usage, first_week=5, last_week=5, forward_weeks=3)
    case = replay.cases[0]
    assert case.forward_scheduled_weeks == (7, 8)
    assert case.forward_games == 2
    assert case.outcome == SHARE_HELD


def test_a_forward_window_that_is_all_bye_is_not_measurable():
    rows = flat_rows(range(1, 6)) + flat_rows([9])
    usage = make_usage(rows, team_weeks=[make_team_week("KC", w) for w in (1, 2, 3, 4, 5, 9)])

    case = replay_role_signals(usage, first_week=5, last_week=5, forward_weeks=3).cases[0]
    assert case.forward_scheduled_weeks == ()
    assert case.outcome == NOT_MEASURABLE


def test_his_team_played_and_he_did_not_appear_is_no_games():
    usage = make_usage(flat_rows(range(1, 6)), weeks=tuple(range(1, 9)))

    case = replay_role_signals(usage, first_week=5, last_week=5, forward_weeks=3).cases[0]
    assert case.forward_scheduled_weeks == (6, 7, 8)
    assert case.forward_games == 0
    assert case.outcome == NO_GAMES


def test_classify_forward_boundaries_are_inclusive():
    scheduled = (6, 7, 8)
    at = 0.10
    exactly = at + FORWARD_MOVE_THRESHOLD
    assert classify_forward(at, exactly, forward_games=3, scheduled_weeks=scheduled) == SHARE_UP
    assert classify_forward(at, exactly - 0.001, forward_games=3, scheduled_weeks=scheduled) == SHARE_HELD
    assert classify_forward(at, at - FORWARD_MOVE_THRESHOLD, forward_games=3, scheduled_weeks=scheduled) == SHARE_DOWN
    assert classify_forward(at, at - FORWARD_MOVE_THRESHOLD + 0.001, forward_games=3, scheduled_weeks=scheduled) == SHARE_HELD


def test_classify_forward_needs_a_share_and_a_scheduled_week():
    assert classify_forward(None, 0.4, forward_games=3, scheduled_weeks=(6,)) == NOT_MEASURABLE
    assert classify_forward(0.1, 0.4, forward_games=3, scheduled_weeks=()) == NOT_MEASURABLE
    assert classify_forward(0.1, None, forward_games=2, scheduled_weeks=(6,)) == NOT_MEASURABLE
    assert classify_forward(0.1, None, forward_games=0, scheduled_weeks=(6,)) == NO_GAMES


# -- mode 1: false / missed breakout classification ----------------------------


def test_false_breakout_is_a_rising_label_whose_share_fell():
    cases = [
        make_case(gsis_id="surge", label=rt.SURGING, outcome=SHARE_DOWN, forward_change=-0.20),
        make_case(gsis_id="rise", label=rt.RISING, outcome=SHARE_DOWN, forward_change=-0.08),
        make_case(gsis_id="held", label=rt.SURGING, outcome=SHARE_HELD, forward_change=-0.01),
        make_case(gsis_id="stable", label=rt.STABLE, outcome=SHARE_DOWN, forward_change=-0.30),
    ]
    assert [c.gsis_id for c in false_breakouts(cases)] == ["surge", "rise"]


def test_false_collapse_is_a_falling_label_whose_share_rose():
    cases = [
        make_case(gsis_id="collapse", label=rt.COLLAPSING, outcome=SHARE_UP, forward_change=0.19),
        make_case(gsis_id="fall", label=rt.FALLING, outcome=SHARE_UP, forward_change=0.07),
        make_case(gsis_id="rise", label=rt.RISING, outcome=SHARE_UP, forward_change=0.40),
    ]
    assert [c.gsis_id for c in false_collapses(cases)] == ["collapse", "fall"]


def test_missed_breakout_boundary_sits_on_the_named_constant():
    cases = [
        make_case(gsis_id="exactly", label=rt.STABLE, forward_change=MISSED_BREAKOUT_MIN_CHANGE),
        make_case(gsis_id="just_under", label=rt.STABLE, forward_change=MISSED_BREAKOUT_MIN_CHANGE - 0.001),
        make_case(gsis_id="insufficient", label=rt.INSUFFICIENT, forward_change=0.15),
        make_case(gsis_id="not_quiet", label=rt.RISING, forward_change=0.40),
        make_case(gsis_id="no_change", label=rt.STABLE, forward_change=None),
    ]
    assert [c.gsis_id for c in missed_breakouts(cases)] == ["insufficient", "exactly"]


def test_missed_breakout_threshold_is_a_parameter():
    cases = [make_case(gsis_id="small", label=rt.STABLE, forward_change=0.07)]
    assert missed_breakouts(cases) == []
    assert [c.gsis_id for c in missed_breakouts(cases, min_change=0.05)] == ["small"]


# -- mode 1: determinism -------------------------------------------------------


def test_replay_is_deterministic():
    rows = flat_rows(range(1, 9)) + flat_rows(range(1, 9), gsis_id="g2", targets=8.0)
    usage = make_usage(rows, weeks=tuple(range(1, 9)))

    first = replay_role_signals(usage, first_week=3, last_week=6)
    second = replay_role_signals(usage, first_week=3, last_week=6)
    assert first.cases == second.cases
    assert [(c.week, c.gsis_id) for c in first.cases] == sorted((c.week, c.gsis_id) for c in first.cases)


def test_example_ordering_breaks_ties_on_week_then_id():
    cases = [
        make_case(week=7, gsis_id="b", label=rt.SURGING, outcome=SHARE_DOWN, forward_change=-0.10),
        make_case(week=7, gsis_id="a", label=rt.SURGING, outcome=SHARE_DOWN, forward_change=-0.10),
        make_case(week=4, gsis_id="z", label=rt.SURGING, outcome=SHARE_DOWN, forward_change=-0.10),
        make_case(week=3, gsis_id="worst", label=rt.SURGING, outcome=SHARE_DOWN, forward_change=-0.25),
    ]
    assert [c.gsis_id for c in false_breakouts(cases)] == ["worst", "z", "a", "b"]


# -- mode 2: snapshots ---------------------------------------------------------


def test_empty_snapshot_directory_replays_nothing_and_says_so(tmp_path):
    snapshots = dd.load_snapshots(tmp_path / "run_snapshots")
    assert snapshots == []

    replay = replay_snapshots(snapshots)
    assert replay.snapshots == 0
    assert replay.series_count == 0
    assert replay.velocity_labels == {}
    assert replay.delta_pairs == []
    assert any("No snapshots" in note for note in replay.notes)


def test_one_snapshot_is_insufficient_history_for_every_series():
    replay = replay_snapshots([make_snapshot("2026-09-03", {"p1": 100.0, "p2": 50.0})])
    assert replay.series_count == 2
    assert replay.velocity_labels == {mv.INSUFFICIENT_HISTORY: 2}
    assert replay.delta_pairs == []
    assert any(str(mv.MIN_OBSERVATIONS) in note for note in replay.notes)
    assert any("two snapshots" in note for note in replay.notes)


def test_two_snapshots_diff_but_still_cannot_classify_velocity():
    snapshots = [
        make_snapshot("2026-09-01", {"p1": 100.0}),
        make_snapshot("2026-09-02", {"p1": 150.0}),
    ]
    replay = replay_snapshots(snapshots)
    assert replay.velocity_labels == {mv.INSUFFICIENT_HISTORY: 1}
    assert len(replay.delta_pairs) == 1
    previous, current, counts = replay.delta_pairs[0]
    assert (previous, current) == ("2026-09-01", "2026-09-02")
    assert counts == {dd.VALUATION: 1}
    assert replay.delta_examples and "Player p1" in replay.delta_examples[0]


def test_three_snapshots_reach_a_velocity_label():
    snapshots = [make_snapshot(f"2026-09-0{d}", {"p1": v}) for d, v in ((1, 100.0), (2, 110.0), (3, 125.0))]
    replay = replay_snapshots(snapshots)
    assert replay.velocity_labels == {mv.RAPIDLY_RISING: 1}
    assert len(replay.delta_pairs) == 2


def test_snapshot_series_are_sorted_and_keyed_by_league_and_player():
    snapshots = [make_snapshot("2026-09-02", {"p2": 5.0}), make_snapshot("2026-09-01", {"p1": 1.0, "p2": 4.0})]
    series = snapshot_series(snapshots)
    assert list(series) == [("L1", "p1"), ("L1", "p2")]
    assert series[("L1", "p2")] == [("2026-09-01", 4.0), ("2026-09-02", 5.0)]


# -- mode 3: ledger outcomes ---------------------------------------------------


def test_ledger_with_no_outcomes_reports_the_wait_not_a_result():
    ledger = Ledger(entries={
        "fp1": make_entry("fp1"),
        "fp2": make_entry("fp2", action="trade", tier="Moderate"),
    })
    replay = replay_outcomes(ledger, now=NOW)

    assert replay.entries == 2
    assert replay.with_outcome == 0
    assert replay.terminal == []
    assert replay.resolved == []
    assert replay.by_action_outcome == {"trade": {"(open)": 1}, "waiver": {"(open)": 1}}
    assert any("still open" in note for note in replay.notes)


def test_empty_ledger_says_nothing_has_been_recorded():
    replay = replay_outcomes(Ledger(), now=NOW)
    assert replay.entries == 0
    assert replay.horizons == {}
    assert any("empty" in note for note in replay.notes)


def test_terminal_outcomes_carry_the_recorded_prior_state():
    ledger = Ledger(entries={
        "fp1": make_entry("fp1", outcome=COMPLETED, outcome_detail="added via waiver for $3 FAAB"),
        "fp2": make_entry("fp2", outcome=STILL_AVAILABLE),
        "fp3": make_entry("fp3", outcome=ACQUIRED_BY_ANOTHER, outcome_detail="added by roster 4"),
    })
    replay = replay_outcomes(ledger, now=NOW)

    assert replay.with_outcome == 3
    assert [c.fingerprint for c in replay.terminal] == ["fp1", "fp3"]
    described = replay.terminal[0].describe()
    assert "Strong Add" in described  # tier
    assert "12.5" in described  # recorded value, as recommended
    assert "+1.5/wk" in described  # previewed lineup delta
    assert "WR Scarce" in described  # scarcity
    assert COMPLETED in described


def test_a_young_ledger_says_the_horizons_have_not_elapsed():
    ledger = Ledger(entries={"fp1": make_entry("fp1", run_id="2026-09-02T00:00:00+00:00")})
    replay = replay_outcomes(ledger, now=NOW)

    assert replay.oldest_age_days == 1
    assert replay.horizons[f"observation window ({OBSERVATION_WINDOW_DAYS}d)"] == (0, 1)
    assert any("observation window" in note for note in replay.notes)


def test_an_elapsed_observation_window_is_counted_not_flagged():
    ledger = Ledger(entries={"fp1": make_entry("fp1", run_id="2026-07-01T00:00:00+00:00")})
    replay = replay_outcomes(ledger, now=NOW)

    assert replay.oldest_age_days == 64
    assert replay.horizons[f"observation window ({OBSERVATION_WINDOW_DAYS}d)"] == (1, 1)
    assert not any("observation window" in note for note in replay.notes)
    assert any("outcome window (6w)" not in note for note in replay.notes) or not replay.notes


def test_recorded_value_falls_back_to_the_whole_snapshot_when_ids_do_not_line_up():
    entry = make_entry("fp1", player_ids=("other",), valuation_snapshot={"p1": 4.0, "p2": 6.0}, outcome=COMPLETED)
    replay = replay_outcomes(Ledger(entries={"fp1": entry}), now=NOW)
    assert replay.terminal[0].recorded_value == 10.0


# -- assembly and rendering ----------------------------------------------------


def synthetic_result():
    usage = make_usage(flat_rows(range(1, 9)), weeks=tuple(range(1, 9)))
    snapshots = [make_snapshot("2026-09-01", {"p1": 100.0}), make_snapshot("2026-09-02", {"p1": 150.0})]
    ledger = Ledger(entries={"fp1": make_entry("fp1", outcome=COMPLETED, outcome_detail="added")})
    return build_result(usage=usage, snapshots=snapshots, ledger=ledger, generated_at=NOW, now=NOW)


def test_build_result_runs_all_three_modes():
    result = synthetic_result()
    assert result.role is not None and result.role.cases
    assert result.snapshot is not None and result.snapshot.snapshots == 2
    assert result.outcome is not None and result.outcome.with_outcome == 1
    assert result.unavailable == []


def test_missing_usage_is_named_not_substituted():
    result = build_result(usage=None, snapshots=[], ledger=None, generated_at=NOW, now=NOW)
    assert result.role is None
    assert len(result.unavailable) == 2
    assert any("Mode 1" in note for note in result.unavailable)
    assert any("Mode 3" in note for note in result.unavailable)


def test_render_states_the_leakage_rules_and_claims_no_accuracy():
    text = render_backtest_markdown(synthetic_result())
    assert "not an accuracy report" in text
    assert "truncated to weeks <= W" in text
    assert "never reconstructed from today's" in text
    assert "## Summary" in text
    assert "## Mode 1 — role-signal replay" in text
    assert "## Mode 2 — snapshot replay" in text
    assert "## Mode 3 — ledger outcome replay" in text
    assert "smoke-tested" in text  # the "this is not validation" caveat


def test_render_is_stable_across_calls():
    result = synthetic_result()
    assert render_backtest_markdown(result) == render_backtest_markdown(result)


def test_render_caps_examples():
    cases = [
        make_case(week=w, gsis_id=f"g{w}", label=rt.SURGING, outcome=SHARE_DOWN, forward_change=-0.10 - w / 100)
        for w in range(3, 9)
    ]
    result = synthetic_result()
    result.role.cases = cases
    text = render_backtest_markdown(result)
    assert f"the {MAX_EXAMPLES} largest moves" in text
    assert text.count("Role Surging, 5 games") == MAX_EXAMPLES
