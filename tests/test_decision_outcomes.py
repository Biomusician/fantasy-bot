import datetime as dt

from conftest import make_entry, make_league_info, make_roster

from sleeper_tool.decision_ledger import DROP, STASH, TRADE, WAIVER, Ledger, LedgerEntry
from sleeper_tool.decision_outcomes import (
    AGAINST_IMPLIED,
    AS_IMPLIED,
    INSUFFICIENT_HISTORY,
    OBSERVED,
    OUTCOME_WINDOWS_WEEKS,
    PENDING,
    build_outcome_facts,
    outcomes_summary,
)
from sleeper_tool.lineup_optimizer import LineupResult, SlotAssignment

FIRST_SEEN = "2026-09-01T12:00:00+00:00"


def _entry(**kwargs) -> LedgerEntry:
    base = dict(
        fingerprint="fp1",
        run_id=FIRST_SEEN,
        last_seen=FIRST_SEEN,
        league_id="L1",
        league_name="Test League",
        action=WAIVER,
        player_ids=("p1",),
        player_names=("Wire Guy",),
        receive_ids=("p1",),
        currency="dynasty",
    )
    base.update(kwargs)
    return LedgerEntry(**base)


def _ledger(*entries) -> Ledger:
    return Ledger(entries={e.fingerprint: e for e in entries})


def _snapshot(date: str, values: dict[str, float], *, league_id="L1", bucket="tracked") -> dict:
    return {
        "schema": 2,
        "generated_at": f"{date}T12:00:00+00:00",
        "current_week": 1,
        "leagues": {league_id: {"name": "Test League", "roster": {}, "tracked": {}, **{bucket: {pid: {"name": pid, "value": v} for pid, v in values.items()}}}},
    }


class _LD:
    def __init__(self, *, starters=(), rostered=(), team_status=None, league_id="L1"):
        self.league = make_league_info(league_id=league_id, name="Test League")
        self.error = None
        self.roster = make_roster(entries=[make_entry(player_id=p, name=p) for p in rostered])
        self.lineup = LineupResult(
            assignments=[SlotAssignment(slot="WR", slot_index=i, player_id=p, name=p, position="WR", projection=10.0) for i, p in enumerate(starters)],
            total_projected_points=0.0,
            unfilled_slots=[],
            bench_player_ids=[],
            unavailable={},
        )
        self.team_status = _Status(team_status) if team_status else None


class _Status:
    def __init__(self, status):
        self.status = status


class _Report:
    def __init__(self, leagues):
        self.leagues = leagues


def _facts(ledger, snapshots, *, now, **kwargs):
    return build_outcome_facts(ledger, snapshots, now=now, **kwargs)


# -- windows ---------------------------------------------------------------------


def test_windows_are_pending_until_their_days_have_elapsed():
    ledger = _ledger(_entry())
    now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)  # 9 days in
    facts = _facts(ledger, [], now=now)
    assert [f.window_weeks for f in facts] == list(OUTCOME_WINDOWS_WEEKS)
    one, three, six = facts
    assert one.state != PENDING  # 7 days elapsed
    assert three.state == PENDING and six.state == PENDING
    assert "9 of 21 days" in three.facts[0]
    assert three.describe().endswith("(9 of 21 days since the recommendation)")


def test_reachable_window_without_snapshots_is_insufficient_history():
    ledger = _ledger(_entry())
    facts = _facts(ledger, [], now=dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc))
    assert {f.state for f in facts} == {INSUFFICIENT_HISTORY}
    assert all(f.value_move is None for f in facts)
    assert INSUFFICIENT_HISTORY in facts[0].facts[0]


def test_value_move_uses_the_entry_snapshot_as_the_baseline():
    ledger = _ledger(_entry(valuation_snapshot={"p1": 1000.0}))
    snapshots = [_snapshot("2026-09-05", {"p1": 1100.0}), _snapshot("2026-09-08", {"p1": 1200.0})]
    one = _facts(ledger, snapshots, now=dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc))[0]
    assert one.state == OBSERVED
    assert one.value_move == 0.2  # last observation inside the 7-day window (2026-09-08)
    assert "reconciled value +20% over the window" in one.facts


def test_value_move_falls_back_to_the_first_stored_observation():
    ledger = _ledger(_entry(valuation_snapshot={}))
    snapshots = [_snapshot("2026-09-01", {"p1": 1000.0}), _snapshot("2026-09-06", {"p1": 900.0})]
    one = _facts(ledger, snapshots, now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc))[0]
    assert one.value_move == -0.1


def test_observations_after_the_window_do_not_leak_into_it():
    ledger = _ledger(_entry(valuation_snapshot={"p1": 1000.0}))
    snapshots = [_snapshot("2026-09-05", {"p1": 1100.0}), _snapshot("2026-09-20", {"p1": 3000.0})]
    facts = _facts(ledger, snapshots, now=dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
    assert facts[0].value_move == 0.1  # 1-week window stops at 2026-09-08
    assert facts[1].value_move == 2.0  # 3-week window reaches 2026-09-22


def test_lineup_and_roster_facts_come_from_the_current_report():
    ledger = _ledger(_entry(valuation_snapshot={"p1": 1000.0}))
    snapshots = [_snapshot("2026-09-06", {"p1": 1050.0})]
    now = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)
    report = _Report([_LD(starters=["p1"], rostered=["p1"])])
    one = _facts(ledger, snapshots, now=now, report=report)[0]
    assert one.entered_lineup is True and one.still_rostered is True
    assert "has since reached your optimized lineup" in one.facts
    assert "on your roster now" in one.facts
    # No report at all: the facts are absent, not guessed.
    bare = _facts(ledger, snapshots, now=now)[0]
    assert bare.entered_lineup is None and bare.still_rostered is None


def test_drop_entries_report_whether_the_player_is_still_here():
    ledger = _ledger(_entry(action=DROP, give_ids=("p1",), receive_ids=(), valuation_snapshot={"p1": 500.0}))
    snapshots = [_snapshot("2026-09-06", {"p1": 400.0}, bucket="roster")]
    report = _Report([_LD(rostered=["p1"])])
    one = _facts(ledger, snapshots, now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc), report=report)[0]
    assert one.still_rostered is True
    assert "still on your roster" in one.facts
    assert one.value_move == -0.2


def test_role_and_points_sources_are_optional_and_never_invented():
    ledger = _ledger(_entry(valuation_snapshot={"p1": 1000.0}))
    snapshots = [_snapshot("2026-09-06", {"p1": 1000.0})]
    now = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)
    plain = _facts(ledger, snapshots, now=now)[0]
    assert plain.role_movement is None and plain.points_total is None
    rich = _facts(ledger, snapshots, now=now, role_labels={"p1": "Ascending"}, points_by_player_week={"p1": {2: 12.5, 3: 8.0}})[0]
    assert rich.role_movement == "Ascending"
    assert rich.points_total == 20.5 and rich.points_weeks == 2
    assert "role: Ascending" in rich.facts
    assert "20.5 fantasy points recorded across 2 player-week(s)" in rich.facts


def test_entry_role_signal_wins_over_the_lookup():
    ledger = _ledger(_entry(valuation_snapshot={"p1": 1000.0}, role_signal="Stored Label"))
    one = _facts(ledger, [_snapshot("2026-09-06", {"p1": 1000.0})], now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc), role_labels={"p1": "Other"})[0]
    assert one.role_movement == "Stored Label"


# -- trades ----------------------------------------------------------------------


def _trade(**kwargs) -> LedgerEntry:
    base = dict(
        action=TRADE,
        player_ids=("g1", "r1"),
        player_names=("Give Guy", "Get Guy"),
        give_ids=("g1",),
        receive_ids=("r1",),
        reason_labels=("trade_type:sell_high",),
        valuation_snapshot={"g1": 5000.0, "r1": 4000.0},
        team_status="contender",
        projected_lineup_delta=1.5,
    )
    base.update(kwargs)
    return _entry(**base)


def test_sell_high_thesis_reports_direction_not_correctness():
    ledger = _ledger(_trade())
    now = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)
    down = _facts(ledger, [_snapshot("2026-09-06", {"g1": 4000.0, "r1": 4400.0})], now=now)[0]
    assert down.give_move == -0.2 and down.receive_move == 0.1
    assert down.thesis_direction == AS_IMPLIED
    assert any("sell-high read" in f and AS_IMPLIED in f for f in down.facts)
    up = _facts(_ledger(_trade()), [_snapshot("2026-09-06", {"g1": 6000.0, "r1": 4000.0})], now=now)[0]
    assert up.thesis_direction == AGAINST_IMPLIED
    # Nothing anywhere claims the recommendation was right or wrong.
    joined = " ".join(down.facts + up.facts).lower()
    assert not any(word in joined for word in ("correct", "wrong", "won", "lost", "accurate", "beat"))


def test_buy_low_thesis_reads_the_receive_side():
    ledger = _ledger(_trade(reason_labels=("trade_type:buy_low",)))
    one = _facts(ledger, [_snapshot("2026-09-06", {"g1": 5000.0, "r1": 4400.0})], now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc))[0]
    assert one.thesis_direction == AS_IMPLIED
    assert any("buy-low read" in f for f in one.facts)


def test_a_partially_priced_trade_side_is_not_reported():
    ledger = _ledger(_trade(give_ids=("g1", "g2"), valuation_snapshot={"g1": 5000.0, "r1": 4000.0}))
    one = _facts(ledger, [_snapshot("2026-09-06", {"g1": 4000.0, "r1": 4400.0})], now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc))[0]
    assert one.give_move is None  # g2 has no value on either day
    assert one.receive_move == 0.1
    assert one.thesis_direction is None


def test_trade_with_no_valued_assets_is_insufficient_history():
    ledger = _ledger(_trade(valuation_snapshot={}))
    one = _facts(ledger, [], now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc))[0]
    assert one.state == INSUFFICIENT_HISTORY
    assert "either side of this offer" in one.facts[0]


def test_picks_lineup_delta_and_team_status_are_stated_plainly():
    ledger = _ledger(_trade(give_picks=(("2027", 1, 4),)))
    report = _Report([_LD(team_status="rebuild")])
    one = _facts(
        ledger,
        [_snapshot("2026-09-06", {"g1": 4000.0, "r1": 4000.0})],
        now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc),
        report=report,
        lineup_delta_now={"fp1": -0.5},
    )[0]
    assert "draft picks in this offer carry no stored value series and are not counted" in one.facts
    assert one.lineup_delta_then == 1.5 and one.lineup_delta_now == -0.5
    assert "previewed at +1.5/wk when recommended, now -0.5/wk" in one.facts
    assert one.team_status_then == "contender" and one.team_status_now == "rebuild"
    assert "your team status went contender -> rebuild" in one.facts


def test_redraft_projection_move_is_derived_only_in_redraft():
    now = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)
    snaps = [_snapshot("2026-09-06", {"p1": 11.0})]
    dynasty = _facts(_ledger(_entry(valuation_snapshot={"p1": 10.0})), snaps, now=now)[0]
    assert dynasty.projection_move is None
    redraft = _facts(_ledger(_entry(valuation_snapshot={"p1": 10.0}, currency="redraft")), snaps, now=now)[0]
    assert redraft.projection_move is not None and round(redraft.projection_move, 3) == 0.1


# -- ordering and summary ---------------------------------------------------------


def test_ordering_is_deterministic_and_summary_groups_counts():
    ledger = _ledger(
        _entry(fingerprint="b", action=STASH, run_id=FIRST_SEEN, last_seen=FIRST_SEEN),
        _entry(fingerprint="a", action=WAIVER),
        _trade(fingerprint="c", run_id="2026-08-20T12:00:00+00:00", last_seen=FIRST_SEEN),
    )
    now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)
    facts = _facts(ledger, [], now=now)
    assert [(f.fingerprint, f.window_weeks) for f in facts] == [
        ("c", 1), ("c", 3), ("c", 6),  # oldest first_seen first
        ("a", 1), ("a", 3), ("a", 6),  # then fingerprint order within the same run
        ("b", 1), ("b", 3), ("b", 6),
    ]
    summary = outcomes_summary(facts)
    assert list(summary) == ["stash", "trade", "waiver"]
    assert summary["waiver"] == {"1w insufficient history": 1, "3w pending": 1, "6w pending": 1}
    assert summary["trade"] == {"1w insufficient history": 1, "3w insufficient history": 1, "6w pending": 1}
    assert list(summary["waiver"]) == sorted(summary["waiver"])
    # Rerunning on the same inputs produces byte-identical descriptions.
    assert [f.describe() for f in facts] == [f.describe() for f in _facts(ledger, [], now=now)]


def test_entries_with_an_unparseable_run_id_are_skipped():
    assert _facts(_ledger(_entry(run_id="not-a-date")), [], now=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)) == []
