import datetime as dt
import json

from conftest import make_entry, make_league_info, make_roster, make_value

from sleeper_tool import decision_ledger as dl
from sleeper_tool.decision_ledger import (
    ACQUIRED_BY_ANOTHER,
    COMPLETED,
    DEFENSIVE_ADD,
    DROP,
    LEDGER_MAX_ENTRIES,
    NO_OBSERVED_ACTION,
    OBSERVATION_WINDOW_DAYS,
    OPEN,
    PARTIALLY_MATCHED,
    RESOLVED,
    STILL_AVAILABLE,
    TRADE,
    UNABLE_TO_DETERMINE,
    WAIVER,
    Ledger,
    LedgerEntry,
    build_entries,
    describe_entry,
    fingerprint,
    load_ledger,
    ledger_path,
    merge_entries,
    observe,
    save_ledger,
    summary,
)

RUN = "2026-09-01T12:00:00+00:00"
LATER_RUN = "2026-09-04T12:00:00+00:00"
NOW = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.timezone.utc)


def _ms(day: int, hour: int = 12) -> int:
    return int(dt.datetime(2026, 9, day, hour, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _entry(**kwargs) -> LedgerEntry:
    base = dict(
        fingerprint=kwargs.pop("fingerprint", "fp1"),
        run_id=RUN,
        last_seen=RUN,
        league_id="L1",
        league_name="Test League",
        action=WAIVER,
    )
    base.update(kwargs)
    return LedgerEntry(**base)


def _ledger(*entries) -> Ledger:
    return Ledger(updated_at=RUN, entries={e.fingerprint: e for e in entries})


# -- fingerprint ----------------------------------------------------------------


def test_fingerprint_is_order_independent_but_side_and_counterparty_aware():
    a = fingerprint(league_id="L1", action=TRADE, give_ids=["p1", "p2"], receive_ids=["p9"], counterparty="7")
    b = fingerprint(league_id="L1", action=TRADE, give_ids=["p2", "p1"], receive_ids=["p9"], counterparty="7")
    assert a == b  # the order the engine built its lists in is not part of the decision
    assert fingerprint(league_id="L1", action=TRADE, give_ids=["p1", "p2"], receive_ids=["p9"], counterparty="8") != a
    # Swapping the sides is a different offer entirely.
    assert fingerprint(league_id="L1", action=TRADE, give_ids=["p9"], receive_ids=["p1", "p2"], counterparty="7") != a
    # Same assets, different league.
    assert fingerprint(league_id="L2", action=TRADE, give_ids=["p1", "p2"], receive_ids=["p9"], counterparty="7") != a


def test_fingerprint_covers_picks_in_either_order():
    picks = [("2027", 1, 3), ("2026", 2, 5)]
    a = fingerprint(league_id="L1", action=TRADE, give_picks=picks, counterparty="7")
    b = fingerprint(league_id="L1", action=TRADE, give_picks=list(reversed(picks)), counterparty="7")
    assert a == b
    assert fingerprint(league_id="L1", action=TRADE, give_picks=[("2027", 1, 4)], counterparty="7") != a
    # A pick given is not a pick received.
    assert fingerprint(league_id="L1", action=TRADE, receive_picks=picks, counterparty="7") != a


# -- merge / persistence ---------------------------------------------------------


def test_same_day_rerun_refreshes_instead_of_duplicating():
    ledger = Ledger()
    first = _entry(valuation_snapshot={"p1": 5000.0})
    assert merge_entries(ledger, [first], RUN) == (1, 0)
    again = _entry(valuation_snapshot={"p1": 4000.0})
    assert merge_entries(ledger, [again], LATER_RUN) == (0, 1)
    assert len(ledger.entries) == 1
    stored = ledger.entries["fp1"]
    assert stored.run_id == RUN  # first_seen never moves
    assert stored.last_seen == LATER_RUN
    assert stored.valuation_snapshot == {"p1": 5000.0}  # the decision as it was made
    assert stored.latest_valuation == {"p1": 4000.0}
    assert ledger.updated_at == LATER_RUN


def test_role_signal_is_filled_in_later_but_never_overwritten():
    ledger = Ledger()
    merge_entries(ledger, [_entry()], RUN)
    merge_entries(ledger, [_entry(role_signal="Ascending")], LATER_RUN)
    assert ledger.entries["fp1"].role_signal == "Ascending"
    merge_entries(ledger, [_entry(role_signal="Something Else")], LATER_RUN)
    assert ledger.entries["fp1"].role_signal == "Ascending"


def test_round_trip_save_load(tmp_path):
    ledger = _ledger(
        _entry(
            fingerprint="fp1",
            action=TRADE,
            player_ids=("p1", "p9"),
            player_names=("Give Guy", "Get Guy"),
            give_ids=("p1",),
            receive_ids=("p9",),
            give_picks=(("2027", 1, 3),),
            receive_picks=(("2026", 2, 5),),
            counterparty_roster_id=7,
            reason_labels=("trade_type:sell_high",),
            valuation_snapshot={"p1": 5000.0, "p9": 4800.0},
            replacement_context={"WR": "Scarce"},
            projected_lineup_delta=1.5,
        )
    )
    path = save_ledger(ledger, tmp_path)
    assert path == ledger_path(tmp_path)
    back = load_ledger(tmp_path)
    got = back.entries["fp1"]
    assert got.give_picks == (("2027", 1, 3),)
    assert got.receive_picks == (("2026", 2, 5),)
    assert got.player_ids == ("p1", "p9")
    assert got.valuation_snapshot == {"p1": 5000.0, "p9": 4800.0}
    assert got.replacement_context == {"WR": "Scarce"}
    assert back.updated_at == RUN


def test_missing_and_corrupt_files_are_tolerated(tmp_path):
    assert load_ledger(tmp_path).entries == {}
    ledger_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    ledger_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert load_ledger(tmp_path).entries == {}
    ledger_path(tmp_path).write_text(json.dumps({"schema": 99, "entries": {"x": {}}}), encoding="utf-8")
    assert load_ledger(tmp_path).entries == {}
    # A single malformed entry doesn't take the rest of the file with it.
    good = _entry_dict()
    ledger_path(tmp_path).write_text(
        json.dumps({"schema": dl.LEDGER_SCHEMA, "updated_at": RUN, "entries": {"fp1": good, "bad": {"nope": 1}}}),
        encoding="utf-8",
    )
    assert list(load_ledger(tmp_path).entries) == ["fp1"]


def _entry_dict() -> dict:
    return dl._entry_to_dict(_entry())


def test_retention_drops_oldest_resolved_first():
    ledger = Ledger()
    entries = []
    for i in range(LEDGER_MAX_ENTRIES + 5):
        day = 1 + (i % 20)
        entries.append(
            _entry(
                fingerprint=f"fp{i:05d}",
                run_id=f"2026-09-{day:02d}T12:00:00+00:00",
                last_seen=f"2026-09-{day:02d}T12:00:00+00:00",
                status=RESOLVED if i < 10 else OPEN,
            )
        )
    merge_entries(ledger, entries, RUN)
    assert len(ledger.entries) == LEDGER_MAX_ENTRIES
    survivors = ledger.entries
    resolved_left = [e for e in survivors.values() if e.status == RESOLVED]
    assert len(resolved_left) == 5  # 5 of the 10 resolved entries were the ones purged
    assert all(e.status == OPEN for e in list(survivors.values()) if e.fingerprint == "fp00010")


# -- observation -----------------------------------------------------------------


def _roster(rid: int, players: list[str]) -> dict:
    return {"roster_id": rid, "players": players}


def _observe(entries, txs, rosters, *, now=NOW, my_roster=4, league="L1"):
    ledger = _ledger(*entries)
    counts = observe(
        ledger,
        transactions_by_league={league: txs} if txs is not None else {},
        rosters_by_league={league: rosters} if rosters is not None else {},
        my_roster_ids={league: my_roster},
        now=now,
    )
    return ledger, counts


def test_waiver_completed_records_the_paid_bid():
    entry = _entry(player_ids=("7562",), player_names=("Wire Guy",), receive_ids=("7562",), faab_pct=12)
    tx = {
        "type": "waiver",
        "status": "complete",
        "created": _ms(2),
        "adds": {"7562": 4},
        "drops": {"999": 4},
        "settings": {"waiver_bid": 9, "seq": 1},
        "roster_ids": [4],
        "transaction_id": "t1",
    }
    ledger, counts = _observe([entry], [tx], [_roster(4, ["7562"])])
    got = ledger.entries["fp1"]
    assert got.outcome == COMPLETED and got.status == RESOLVED
    assert got.paid_bid == 9 and got.faab_pct == 12  # suggested and paid are separate numbers
    assert "for $9 FAAB" in got.outcome_detail
    assert counts == {COMPLETED: 1}


def test_transaction_before_first_seen_does_not_count():
    entry = _entry(receive_ids=("7562",), player_names=("Wire Guy",))
    tx = {
        "type": "free_agent",
        "status": "complete",
        "created": _ms(1, hour=6),  # six hours BEFORE the run that recommended him
        "adds": {"7562": 4},
        "roster_ids": [4],
        "transaction_id": "t1",
    }
    ledger, _ = _observe([entry], [tx], [_roster(4, ["7562"])])
    got = ledger.entries["fp1"]
    assert got.outcome != COMPLETED
    assert got.status == OPEN


def test_defense_team_code_ids_match():
    entry = _entry(action=DEFENSIVE_ADD, receive_ids=("LAC",), player_names=("Chargers D/ST",))
    tx = {"type": "free_agent", "status": "complete", "created": _ms(2), "adds": {"LAC": 4}, "roster_ids": [4], "transaction_id": "t1"}
    ledger, _ = _observe([entry], [tx], [_roster(4, ["LAC"])])
    assert ledger.entries["fp1"].outcome == COMPLETED


def test_acquired_by_another_manager():
    entry = _entry(receive_ids=("7562",), player_names=("Wire Guy",))
    tx = {"type": "waiver", "status": "complete", "created": _ms(2), "adds": {"7562": 9}, "roster_ids": [9], "transaction_id": "t1"}
    ledger, _ = _observe([entry], [tx], [_roster(4, []), _roster(9, ["7562"])])
    got = ledger.entries["fp1"]
    assert got.outcome == ACQUIRED_BY_ANOTHER and got.status == RESOLVED
    assert "roster 9" in got.outcome_detail


def test_still_available_stays_open_until_the_window_closes():
    entry = _entry(receive_ids=("7562",), player_names=("Wire Guy",))
    ledger, _ = _observe([entry], [{"type": "free_agent", "status": "complete", "created": _ms(2), "adds": {"1": 4}, "roster_ids": [4]}], [_roster(4, [])])
    got = ledger.entries["fp1"]
    assert got.outcome == STILL_AVAILABLE and got.status == OPEN
    # Same facts, past the window: the same label, now closed.
    late = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=OBSERVATION_WINDOW_DAYS + 1)
    ledger, _ = _observe([_entry(receive_ids=("7562",))], [{"type": "free_agent", "status": "complete", "created": _ms(2), "adds": {"1": 4}, "roster_ids": [4]}], [_roster(4, [])], now=late)
    assert ledger.entries["fp1"].outcome == STILL_AVAILABLE
    assert ledger.entries["fp1"].status == RESOLVED


def test_no_observed_action_only_after_the_window():
    # He is rostered elsewhere but no transaction after first_seen explains it.
    entry = _entry(receive_ids=("7562",))
    txs = [{"type": "free_agent", "status": "complete", "created": _ms(2), "adds": {"1": 4}, "roster_ids": [4]}]
    rosters = [_roster(4, []), _roster(9, ["7562"])]
    ledger, _ = _observe([entry], txs, rosters)
    assert ledger.entries["fp1"].outcome is None and ledger.entries["fp1"].status == OPEN
    late = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
    ledger, counts = _observe([_entry(receive_ids=("7562",))], txs, rosters, now=late)
    assert ledger.entries["fp1"].outcome == NO_OBSERVED_ACTION
    assert ledger.entries["fp1"].status == RESOLVED
    assert counts == {NO_OBSERVED_ACTION: 1}


def test_failed_claim_records_intent_without_being_an_outcome():
    entry = _entry(receive_ids=("7562",))
    txs = [
        {"type": "waiver", "status": "failed", "created": _ms(2), "adds": {"7562": 4}, "settings": {"waiver_bid": 3}, "roster_ids": [4], "transaction_id": "t1"},
    ]
    ledger, _ = _observe([entry], txs, [_roster(4, [])])
    got = ledger.entries["fp1"]
    assert got.failed_claim is True
    assert got.outcome == STILL_AVAILABLE  # a failed claim is not an acquisition
    assert got.paid_bid is None
    assert "waiver claim of yours for him failed" in describe_entry(got)


def test_someone_elses_failed_claim_is_not_my_intent():
    entry = _entry(receive_ids=("7562",))
    txs = [{"type": "waiver", "status": "failed", "created": _ms(2), "adds": {"7562": 11}, "roster_ids": [11], "transaction_id": "t1"}]
    ledger, _ = _observe([entry], txs, [_roster(4, [])])
    assert ledger.entries["fp1"].failed_claim is False


def test_drop_completed():
    entry = _entry(action=DROP, give_ids=("111",), player_ids=("111",), player_names=("Dead Weight",))
    txs = [{"type": "free_agent", "status": "complete", "created": _ms(2), "adds": {"222": 4}, "drops": {"111": 4}, "roster_ids": [4], "transaction_id": "t1"}]
    ledger, _ = _observe([entry], txs, [_roster(4, ["222"])])
    assert ledger.entries["fp1"].outcome == COMPLETED
    # Someone else's drop of the same player is not mine.
    other = [{"type": "free_agent", "status": "complete", "created": _ms(2), "drops": {"111": 9}, "roster_ids": [9], "transaction_id": "t1"}]
    ledger, _ = _observe([_entry(action=DROP, give_ids=("111",))], other, [_roster(4, ["111"])])
    assert ledger.entries["fp1"].outcome is None


def _trade_entry(**kwargs) -> LedgerEntry:
    base = dict(
        action=TRADE,
        player_ids=("p1", "p9"),
        player_names=("Give Guy", "Get Guy"),
        give_ids=("p1",),
        receive_ids=("p9",),
        give_picks=(("2027", 1, 4),),
        receive_picks=(("2026", 2, 3),),
        counterparty_roster_id=3,
    )
    base.update(kwargs)
    return _entry(**base)


def _trade_tx(*, adds, drops, picks, roster_ids, created=None, status="complete") -> dict:
    return {
        "type": "trade",
        "status": status,
        "created": created if created is not None else _ms(2),
        "adds": adds,
        "drops": drops,
        "draft_picks": picks,
        "roster_ids": roster_ids,
        "transaction_id": "t1",
    }


def test_trade_exact_match_players_and_picks_both_ways():
    tx = _trade_tx(
        adds={"p1": 3, "p9": 4},
        drops={"p1": 4, "p9": 3},
        picks=[
            {"season": "2027", "round": 1, "roster_id": 4, "owner_id": 3, "previous_owner_id": 4},
            {"season": "2026", "round": 2, "roster_id": 3, "owner_id": 4, "previous_owner_id": 3},
        ],
        roster_ids=[3, 4],
    )
    ledger, _ = _observe([_trade_entry()], [tx], [_roster(4, ["p9"]), _roster(3, ["p1"])])
    got = ledger.entries["fp1"]
    assert got.outcome == COMPLETED and got.status == RESOLVED
    assert "roster 3" in got.outcome_detail


def test_trade_short_one_pick_on_the_return_is_not_completed():
    # Everything proposed went out, but one of the two picks never came
    # back: the pieces moved, the deal didn't.
    tx = _trade_tx(
        adds={"p1": 3, "p9": 4},
        drops={"p1": 4, "p9": 3},
        picks=[{"season": "2027", "round": 1, "roster_id": 4, "owner_id": 3, "previous_owner_id": 4}],
        roster_ids=[3, 4],
    )
    ledger, _ = _observe([_trade_entry()], [tx], [_roster(4, ["p9"]), _roster(3, ["p1"])])
    got = ledger.entries["fp1"]
    assert got.outcome == PARTIALLY_MATCHED and "different return" in got.outcome_detail


def test_an_unrelated_trade_is_not_a_match_at_all():
    tx = _trade_tx(adds={"zz": 4}, drops={"zz": 3}, picks=[], roster_ids=[3, 4])
    ledger, _ = _observe([_trade_entry()], [tx], [_roster(4, ["zz", "p1"]), _roster(3, [])])
    assert ledger.entries["fp1"].outcome is None


def test_trade_superset_is_partially_matched():
    tx = _trade_tx(
        adds={"p1": 3, "p2": 3, "p9": 4},
        drops={"p1": 4, "p2": 4, "p9": 3},
        picks=[
            {"season": "2027", "round": 1, "roster_id": 4, "owner_id": 3, "previous_owner_id": 4},
            {"season": "2026", "round": 2, "roster_id": 3, "owner_id": 4, "previous_owner_id": 3},
        ],
        roster_ids=[3, 4],
    )
    ledger, _ = _observe([_trade_entry()], [tx], [_roster(4, ["p9"]), _roster(3, ["p1", "p2"])])
    got = ledger.entries["fp1"]
    assert got.outcome == PARTIALLY_MATCHED and got.status == RESOLVED
    assert "extra assets" in got.outcome_detail


def test_trade_with_a_different_counterparty_is_partially_matched():
    tx = _trade_tx(
        adds={"p1": 8, "p9": 4},
        drops={"p1": 4, "p9": 8},
        picks=[
            {"season": "2027", "round": 1, "roster_id": 4, "owner_id": 8, "previous_owner_id": 4},
            {"season": "2026", "round": 2, "roster_id": 3, "owner_id": 4, "previous_owner_id": 8},
        ],
        roster_ids=[4, 8],
    )
    ledger, _ = _observe([_trade_entry()], [tx], [_roster(4, ["p9"]), _roster(8, ["p1"])])
    got = ledger.entries["fp1"]
    assert got.outcome == PARTIALLY_MATCHED
    assert "roster 8" in got.outcome_detail


def test_same_pieces_sent_for_a_different_return_is_partially_matched():
    entry = _trade_entry(give_picks=(), receive_picks=())
    tx = _trade_tx(adds={"p1": 3, "pX": 4}, drops={"p1": 4, "pX": 3}, picks=[], roster_ids=[3, 4])
    ledger, _ = _observe([entry], [tx], [_roster(4, ["pX"]), _roster(3, ["p1"])])
    got = ledger.entries["fp1"]
    assert got.outcome == PARTIALLY_MATCHED
    assert "different return" in got.outcome_detail


def test_failed_trade_status_is_ignored():
    entry = _trade_entry(give_picks=(), receive_picks=())
    tx = _trade_tx(adds={"p1": 3, "p9": 4}, drops={"p1": 4, "p9": 3}, picks=[], roster_ids=[3, 4], status="failed")
    ledger, _ = _observe([entry], [tx], [_roster(4, []), _roster(3, [])])
    assert ledger.entries["fp1"].outcome is None


def test_missing_transactions_or_rosters_are_unable_to_determine():
    ledger, counts = _observe([_entry(receive_ids=("7562",))], None, [_roster(4, [])])
    got = ledger.entries["fp1"]
    assert got.outcome == UNABLE_TO_DETERMINE and got.status == OPEN
    assert "no cached transactions" in got.outcome_detail
    assert counts == {UNABLE_TO_DETERMINE: 1}
    ledger, _ = _observe([_entry(receive_ids=("7562",))], [], None)
    assert "no cached rosters" in ledger.entries["fp1"].outcome_detail
    # Past the window with still no data, it closes rather than lingering forever.
    late = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
    ledger, _ = _observe([_entry(receive_ids=("7562",))], None, None, now=late)
    assert ledger.entries["fp1"].status == RESOLVED


def test_resolved_entries_are_not_re_observed():
    entry = _entry(receive_ids=("7562",), status=RESOLVED, outcome=COMPLETED)
    tx = {"type": "waiver", "status": "complete", "created": _ms(2), "adds": {"7562": 9}, "roster_ids": [9]}
    ledger, counts = _observe([entry], [tx], [_roster(9, ["7562"])])
    assert ledger.entries["fp1"].outcome == COMPLETED
    assert counts == {}


def test_summary_and_describe_are_deterministic():
    ledger = _ledger(
        _entry(fingerprint="a", action=WAIVER, outcome=COMPLETED, player_names=("A",), tier="Must Add"),
        _entry(fingerprint="b", action=WAIVER, outcome=STILL_AVAILABLE, player_names=("B",)),
        _entry(fingerprint="c", action=TRADE, player_names=("C",), counterparty_name="Them"),
    )
    assert summary(ledger) == {
        TRADE: {"(open)": 1},
        WAIVER: {COMPLETED: 1, STILL_AVAILABLE: 1},
    }
    assert list(summary(ledger)) == [TRADE, WAIVER]
    assert [e.fingerprint for e in ledger.ordered()] == ["a", "b", "c"]
    line = describe_entry(ledger.entries["a"])
    assert line.startswith("[Test League] waiver: A (Must Add)")
    assert "Completed" in line


# -- build_entries from a report -------------------------------------------------


class _Report:
    def __init__(self, leagues, generated_at=RUN, current_week=3):
        self.leagues = leagues
        self.generated_at = dt.datetime.fromisoformat(generated_at)
        self.current_week = current_week


class _LD:
    def __init__(self, **kwargs):
        self.league = kwargs.pop("league", make_league_info(league_id="L1", name="Test League"))
        self.error = None
        self.drafted = True
        self.currency = "dynasty"
        self.roster = kwargs.pop("roster", make_roster(roster_id=4))
        self.team_status = kwargs.pop("team_status", None)
        self.proposals = []
        self.waiver_targets = []
        self.drop_candidates = []
        self.trade_impacts = []
        self.trade_economics = []
        self.waiver_impacts = {}
        self.conflicts = []
        self.replacement = None
        self.league_economy = None
        self.defensive_add = None
        self.streamers = []
        self.stash = []
        for k, v in kwargs.items():
            setattr(self, k, v)


def _proposal(**kwargs):
    from sleeper_tool.trade_engine import TradeProposal

    base = dict(
        league_name="Test League",
        currency="dynasty",
        target_username="rival",
        target_team_name="Rival Team",
        give=[make_entry(player_id="p1", name="Give Guy", value=make_value(dynasty_value=5000))],
        receive=[make_entry(player_id="p9", name="Get Guy", value=make_value(dynasty_value=4800))],
        my_value_total=5000,
        their_value_total=4800,
        rationale_for_me=[],
        rationale_for_them=[],
        caveats=[],
    )
    base.update(kwargs)
    return TradeProposal(**base)


def _target(**kwargs):
    from sleeper_tool.waiver_engine import WaiverTarget

    base = dict(
        player_id="7562",
        name="Wire Guy",
        position="WR",
        team="KC",
        trend_count=100,
        value=make_value(dynasty_value=1200),
        fills_need=True,
        need_rank=0,
        reason="trending",
        suggested_faab_pct=8,
    )
    base.update(kwargs)
    return WaiverTarget(**base)


def test_build_entries_covers_every_recommendation_kind():
    from sleeper_tool.recommendation_conflicts import Conflict
    from sleeper_tool.trade_engine import DropCandidate

    ld = _LD(
        proposals=[_proposal(trade_type="sell_high", acceptance_rating="High"), _proposal(trade_type="consolidation")],
        trade_economics=[None, None],
        trade_impacts=[None, None],
        waiver_targets=[_target()],
        drop_candidates=[DropCandidate(entry=make_entry(player_id="d1", name="Cut Me"), priority="Strong Drop", reasons=[])],
        conflicts=[Conflict(kind="trade", key="0", subject="Give Guy", reasons_for=["x"], reasons_against=["y"])],
    )
    entries = build_entries(_Report([ld]))
    by_action = {}
    for e in entries:
        by_action.setdefault(e.action, []).append(e)
    assert sorted(by_action) == ["consolidation", "drop", "trade", "waiver"]
    trade = by_action["trade"][0]
    assert trade.tier == "High"
    assert "trade_type:sell_high" in trade.reason_labels
    assert "conflict" in trade.reason_labels
    assert trade.give_ids == ("p1",) and trade.receive_ids == ("p9",)
    assert trade.valuation_snapshot == {"p1": 5000, "p9": 4800}
    assert trade.counterparty_name == "Rival Team"
    waiver = by_action["waiver"][0]
    assert waiver.tier == "Moderate" and waiver.faab_pct == 8
    assert waiver.receive_ids == ("7562",) and waiver.player_names == ("Wire Guy",)
    assert by_action["drop"][0].give_ids == ("d1",)
    # Every entry is fingerprinted and dated by the run.
    assert all(e.run_id == RUN and e.last_seen == RUN and len(e.fingerprint) == 40 for e in entries)


def test_build_entries_resolves_the_counterparty_roster_id_from_league_economy():
    from sleeper_tool.league_economy import LeagueEconomy, ManagerEconomy

    economy = LeagueEconomy(
        total_completed_trades=3,
        limited_sample=False,
        managers={
            7: ManagerEconomy(roster_id=7, username="rival", team_name="Rival Team", completed_trades=1, net_future_picks=0),
            9: ManagerEconomy(roster_id=9, username="other", team_name="Other", completed_trades=2, net_future_picks=0),
        },
    )
    ld = _LD(proposals=[_proposal()], trade_economics=[None], trade_impacts=[None], league_economy=economy)
    entry = build_entries(_Report([ld]))[0]
    assert entry.counterparty_roster_id == 7


def test_build_entries_skips_errored_and_undrafted_leagues():
    bad = _LD(proposals=[_proposal()], trade_economics=[None], trade_impacts=[None])
    bad.error = "sync failed"
    predraft = _LD(proposals=[], waivers_note="Pre-draft")
    predraft.drafted = False
    assert build_entries(_Report([bad, predraft])) == []


def test_build_entries_deduplicates_within_a_run():
    ld = _LD(waiver_targets=[_target(), _target(priority_tier="Must Add")])
    entries = build_entries(_Report([ld]))
    assert len(entries) == 1  # same league, same player: one decision
    assert entries[0].tier == "Moderate"  # the first one wins


def test_end_to_end_report_to_ledger_to_observation(tmp_path):
    ld = _LD(waiver_targets=[_target()], proposals=[], trade_economics=[], trade_impacts=[])
    ledger = load_ledger(tmp_path)
    added, refreshed = merge_entries(ledger, build_entries(_Report([ld])), RUN)
    assert (added, refreshed) == (1, 0)
    save_ledger(ledger, tmp_path)
    reloaded = load_ledger(tmp_path)
    tx = {"type": "waiver", "status": "complete", "created": _ms(2), "adds": {"7562": 4}, "settings": {"waiver_bid": 5}, "roster_ids": [4]}
    counts = observe(
        reloaded,
        transactions_by_league={"L1": [tx]},
        rosters_by_league={"L1": [_roster(4, ["7562"])]},
        my_roster_ids={"L1": 4},
        now=NOW,
    )
    assert counts == {COMPLETED: 1}
    save_ledger(reloaded, tmp_path)
    assert load_ledger(tmp_path).ordered()[0].paid_bid == 5
