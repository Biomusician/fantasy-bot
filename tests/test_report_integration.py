"""The shared-layer seams added in the intelligence tranche: FAAB advice
from Sleeper payloads, role trends through the crosswalk, the role/market
cross, the usage layer's "no games yet" note, and that both renderers show
the same FAAB / health / diagnostics facts."""
import datetime as dt

from conftest import make_entry, make_format, make_league_info, make_roster, make_value
from usage_fixtures import make_player_week, make_team_week, make_usage

from sleeper_tool.faab_strategy import AGGRESSIVE, AFFORDABILITY_NOTE, PRIORITY_SPEND, FaabAdvice, FaabContext, bid_cell, bid_detail, context_from_sleeper
from sleeper_tool.html_report import _league_panel, _overview_panel
from sleeper_tool.market_velocity import RISING as VELOCITY_RISING
from sleeper_tool.market_velocity import Velocity
from sleeper_tool.player_ids import PlayerIds
from sleeper_tool.replacement_value import ABUNDANT, VERY_SCARCE, PositionMarket, ReplacementMarket
from sleeper_tool.report import render_league_section, render_weekly_report
from sleeper_tool.report_data import (
    LeagueReportData,
    WeeklyReportData,
    _annotate_with_roles,
    _build_faab_advice,
    _load_usage_layer,
)
from sleeper_tool.role_trends import CONFIRM, NO_HISTORY_NOTE, RISING, ROLE_AHEAD, STABLE, RoleTrend, trends_for
from sleeper_tool.signal_health import FRESH, STALE, SignalHealth, SignalHealthReport
from sleeper_tool.source_disagreement import MARKET_ABOVE_PROJECTION, SourceView
from sleeper_tool.stash_board import StashCandidate
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.waiver_engine import INSURANCE, MODERATE, MUST_ADD, STRONG_ADD, WaiverTarget


# -- FAAB -----------------------------------------------------------------------


def _league_payload(**settings):
    base = {"waiver_type": 2, "waiver_budget": 100, "playoff_week_start": 15, "trade_deadline": 11}
    base.update(settings)
    return {"settings": base, "status": "in_season", "season": "2026"}


def test_context_from_sleeper_reads_settings_rosters_and_winning_bids():
    rosters = [
        {"roster_id": 1, "settings": {"waiver_budget_used": -5}},  # FAAB acquired by trade
        {"roster_id": 2, "settings": {"waiver_budget_used": 40}},
        {"roster_id": 3, "settings": {}},  # no settings at all
    ]
    txs = [
        {"type": "waiver", "status": "complete", "settings": {"waiver_bid": 11}},
        {"type": "waiver", "status": "failed", "settings": {"waiver_bid": 50}},  # a failed claim is not a price
        {"type": "free_agent", "status": "complete", "settings": {"waiver_bid": 0}},
        {"type": "waiver", "status": "complete", "settings": {}},
        {"type": "waiver", "status": "complete", "settings": {"waiver_bid": "3"}},
    ]
    ctx = context_from_sleeper(_league_payload(), rosters, txs, 1, current_week=3, pre_draft=False)
    assert ctx.is_faab and ctx.budget == 100 and ctx.my_used == -5 and ctx.remaining == 105
    assert ctx.others_used == [40, 0] and ctx.league_bids == [3, 11]
    assert ctx.playoff_week_start == 15 and ctx.trade_deadline == 11 and ctx.weeks_to_playoffs == 12


def test_context_from_sleeper_tolerates_missing_and_malformed_settings():
    ctx = context_from_sleeper({"settings": {"waiver_type": "x", "waiver_budget": None}}, [], [], 1, current_week=None, pre_draft=True)
    assert not ctx.is_faab and ctx.budget is None and ctx.pre_draft and ctx.league_bids == []


def _target(pid, pos, tier, *, pctl=60.0, fills_need=False, faab_pct=None):
    return WaiverTarget(
        player_id=pid, name=pid, position=pos, team="KC", trend_count=1,
        value=make_value(position=pos, proj_points=100, dynasty_value_percentile=pctl, redraft_ecr_percentile=pctl),
        fills_need=fills_need, need_rank=None, reason="r", priority_tier=tier, suggested_faab_pct=faab_pct,
    )


def _fa(pid, pos, pctl):
    return make_entry(player_id=pid, name=pid, position=pos, value=make_value(position=pos, proj_points=90, dynasty_value_percentile=pctl))


def _market():
    return ReplacementMarket(
        positions={
            "RB": PositionMarket("RB", None, 5.0, None, 12.0, VERY_SCARCE, 0.6),
            "WR": PositionMarket("WR", None, 9.0, None, 9.5, ABUNDANT, 0.05),
        },
        players={},
    )


def test_faab_advice_is_built_per_target_and_the_budget_plan_flags_unaffordable_rows():
    ctx = FaabContext(waiver_type=2, budget=100, my_used=70, others_used=[0, 0], current_week=3, playoff_week_start=15)
    targets = [
        _target("rb1", "RB", MUST_ADD, fills_need=True, faab_pct=25),
        _target("rb2", "RB", STRONG_ADD, faab_pct=15),
        _target("wr1", "WR", MODERATE, faab_pct=3),
    ]
    free_agents = [_fa("x1", "WR", 58.0), _fa("x2", "WR", 62.0), _fa("x3", "WR", 30.0)]
    advice = _build_faab_advice(ctx, targets, free_agents, _market(), "dynasty", {}, set())
    assert set(advice) == {"rb1", "rb2", "wr1"}
    # Must Add, Very Scarce market, no comparable free agent, urgent need: the top posture.
    assert advice["rb1"].posture == PRIORITY_SPEND and advice["rb1"].tier == MUST_ADD
    assert advice["rb1"].share_of_remaining_text.startswith("Suggested bid uses approximately")
    # $30 left: the Must Add takes most of it, the Strong Add behind it cannot also clear.
    assert AFFORDABILITY_NOTE in advice["rb2"].notes and AFFORDABILITY_NOTE not in advice["rb1"].notes
    assert AFFORDABILITY_NOTE not in advice["wr1"].notes  # Moderate rows are outside the plan


def test_faab_advice_is_empty_pre_draft_and_in_non_faab_leagues():
    targets = [_target("rb1", "RB", MUST_ADD, faab_pct=25)]
    assert _build_faab_advice(FaabContext(waiver_type=2, budget=100, pre_draft=True), targets, [], None, "dynasty", {}, set()) == {}
    assert _build_faab_advice(FaabContext(waiver_type=1, budget=None), targets, [], None, "dynasty", {}, set()) == {}
    assert _build_faab_advice(FaabContext(waiver_type=2, budget=100), [], [], None, "dynasty", {}, set()) == {}


def test_urgent_ids_and_insurance_rows_count_as_urgent_need():
    ctx = FaabContext(waiver_type=2, budget=100, my_used=0, others_used=[0], current_week=3, playoff_week_start=15)
    bye_cover = _target("wr1", "WR", STRONG_ADD, faab_pct=10)
    insurance = _target("wr2", "WR", INSURANCE, faab_pct=5)
    plain = _target("wr3", "WR", STRONG_ADD, faab_pct=10)
    advice = _build_faab_advice(ctx, [bye_cover, insurance, plain], [], _market(), "dynasty", {}, {"wr1"})
    # An Abundant market is not aggressive on its own; the bye-hole cover is
    # what lifts wr1 over the otherwise identical wr3. The insurance row gets
    # advice too (its tier is outside the Must/Strong Add posture rules).
    assert advice["wr1"].posture == AGGRESSIVE and advice["wr3"].posture != AGGRESSIVE
    assert "wr2" in advice


def test_bid_cell_and_detail_wording():
    adv = FaabAdvice(player_id="p", posture=AGGRESSIVE, suggested_pct=25, suggested_dollars=20, remaining=80,
                     share_of_remaining_text="Suggested bid uses approximately 25% of remaining budget ($20 of $80)",
                     leverage_text="Only two managers can outbid 20", anchor_text=None, notes=["n1"], name="p", tier=MUST_ADD)
    assert bid_cell(adv, 25) == "$20 · Aggressive"
    assert bid_cell(None, 25) == "25%" and bid_cell(None, None) == "—"
    assert bid_detail(adv) == "Suggested bid uses approximately 25% of remaining budget ($20 of $80); Only two managers can outbid 20; n1"
    adv.tier = MODERATE
    assert bid_detail(adv) is None


# -- roles ------------------------------------------------------------------------


def _usage_rising():
    rows = {1: {"targets": 3.0, "snap_pct": 0.40}, 2: {"targets": 3.0, "snap_pct": 0.40}, 3: {"targets": 8.0, "snap_pct": 0.75}, 4: {"targets": 8.0, "snap_pct": 0.75}}
    return make_usage(
        [make_player_week("g1", w, position="WR", **rows[w]) for w in rows],
        team_weeks=[make_team_week("KC", w, targets=30.0, carries=20.0) for w in rows],
    )


def test_trends_for_goes_through_the_crosswalk_and_skips_unplaced_players():
    crosswalk = {"s1": PlayerIds("s1", "g1", None, "dynastyprocess"), "s2": PlayerIds("s2", None, None, "ambiguous")}
    trends = trends_for(_usage_rising(), crosswalk, ["s1", "s2", "s3"])
    assert set(trends) == {"s1"} and trends["s1"].rising
    assert trends_for(None, crosswalk, ["s1"]) == {}


def _proposal(give=(), receive=()):
    return TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="Rival",
        give=list(give), receive=list(receive), my_value_total=100, their_value_total=100,
        rationale_for_me=[], rationale_for_them=[], caveats=[],
    )


def _trend(label, games=4):
    return RoleTrend(gsis_id="g", label=label, components=[], games=games)


def test_role_annotations_are_sparse_and_land_on_the_side_they_argue_for():
    give_up = make_entry(player_id="a", name="A", position="WR")
    give_down = make_entry(player_id="b", name="B", position="WR")
    recv_up = make_entry(player_id="c", name="C", position="RB")
    quiet = make_entry(player_id="d", name="D", position="RB")
    p = _proposal(give=[give_up, give_down, quiet], receive=[recv_up])
    target = _target("a", "WR", MODERATE)
    stash = StashCandidate(entry=make_entry(player_id="e", name="E", position="TE"), label="Watch", percentile=50.0, reasons=["young"])
    ld = LeagueReportData(
        league=make_league_info(), drafted=True, proposals=[p], waiver_targets=[target], stash=[stash],
        role_trends={"a": _trend(RISING), "b": _trend("Role Falling"), "c": _trend("Role Surging"), "d": _trend(STABLE), "e": _trend(RISING)},
        velocity={"a": Velocity(VELOCITY_RISING, 3, 0.1, "2026-09-01", "2026-09-03")},
        source_views={"c": SourceView("C", "RB", None, None, ("KTC", "FantasyPros dynasty"), MARKET_ABOVE_PROJECTION, 10, 40, None, [MARKET_ABOVE_PROJECTION])},
    )
    _annotate_with_roles(ld)
    assert ld.role_market == {"a": CONFIRM, "c": CONFIRM}  # b, d: no market label; e: nothing to cross
    assert [c for c in p.caveats if c.startswith("Role:")] == [
        f"Role: Role Rising (4 games) — {CONFIRM} — A's role is growing, which argues against selling now."
    ]
    assert p.rationale_for_me == [
        "Role: Role Falling (4 games) — B's role is shrinking, which favours selling.",
        f"Role: Role Surging (4 games) — {CONFIRM} — C's role is growing, which favours buying.",
    ]
    assert target.notes == [f"Role: Role Rising (4 games) — {CONFIRM}"]
    assert stash.reasons == ["young", "Role: Role Rising (4 games)"]
    # A stable role never writes anything.
    assert not any("D" in c for c in p.caveats + p.rationale_for_me)


def test_role_ahead_of_market_when_the_role_moved_and_the_price_did_not():
    target = _target("a", "WR", MODERATE)
    ld = LeagueReportData(
        league=make_league_info(), drafted=True, waiver_targets=[target],
        role_trends={"a": _trend(RISING)}, velocity={"a": Velocity("Stable", 3, 0.0, "2026-09-01", "2026-09-03")},
    )
    _annotate_with_roles(ld)
    assert ld.role_market == {"a": ROLE_AHEAD}


class _Storage:
    def __init__(self, rosters, trending=(), players=None):
        self._rosters, self._trending, self._players = rosters, list(trending), players or {}

    def get_rosters(self, league_id):
        return self._rosters

    def get_trending(self, kind):
        return self._trending

    def get_all_players(self):
        return self._players


def test_usage_layer_says_once_that_role_data_begins_after_games(monkeypatch):
    import sleeper_tool.report_data as rd

    monkeypatch.setattr(rd, "load_usage", lambda season: None)
    usage, crosswalk, note, xnote = _load_usage_layer(_Storage([]), [make_league_info()], 2026)
    assert usage is None and crosswalk is None and note == NO_HISTORY_NOTE and xnote is None
    assert _load_usage_layer(_Storage([]), [make_league_info()], None) == (None, None, None, None)


def test_usage_layer_builds_the_crosswalk_for_rostered_trending_and_active_players(monkeypatch):
    import sleeper_tool.report_data as rd

    monkeypatch.setattr(rd, "load_usage", lambda season: _usage_rising())
    monkeypatch.setattr(rd, "load_crosswalk_rows", lambda: ([], []))
    players = {
        "s1": {"full_name": "Some Guy", "position": "WR", "team": "KC", "gsis_id": "g1"},
        "s2": {"full_name": "Trend Guy", "position": "RB", "team": "KC", "gsis_id": "g2"},
        "s3": {"full_name": "Nobody", "position": "WR", "team": "KC", "gsis_id": "g3"},
    }
    storage = _Storage([{"players": ["s1"]}], trending=[{"player_id": "s2"}], players=players)
    usage, crosswalk, note, xnote = _load_usage_layer(storage, [make_league_info()], 2026)
    assert usage is not None and note is None and xnote.startswith("Player id crosswalk:")
    # s3 is an active WR on a team: a free agent the report may name, so he is crosswalked too.
    assert set(crosswalk) == {"s1", "s2", "s3"} and crosswalk["s1"].gsis_id == "g1"


# -- renderer parity ---------------------------------------------------------------


def _health():
    return SignalHealthReport(
        signals=[
            SignalHealth("ktc", "ktc", cache_age=dt.timedelta(hours=2), label=FRESH, coverage=500),
            SignalHealth("rotoballer", "rotoballer", cache_age=dt.timedelta(days=3), label=STALE, coverage=400),
        ],
        degraded=True, notes=["RotoBaller is 3.0d old"],
    )


def test_both_renderers_show_faab_health_and_diagnostics():
    target = _target("rb1", "RB", MUST_ADD, faab_pct=25)
    adv = FaabAdvice(player_id="rb1", posture=AGGRESSIVE, suggested_pct=25, suggested_dollars=20, remaining=80,
                     share_of_remaining_text="Suggested bid uses approximately 25% of remaining budget ($20 of $80)",
                     leverage_text="Only two managers can outbid 20", anchor_text=None, notes=[], name="rb1", tier=MUST_ADD)
    roster = make_roster(entries=[make_entry(player_id="q", name="Q", position="QB")], fmt=make_format(roster_positions=("QB", "BN")))
    ld = LeagueReportData(league=make_league_info(name="L"), drafted=True, roster=roster, currency="dynasty",
                          waiver_targets=[target], faab={"rb1": adv})
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={}, ff_status="absent",
        leagues=[ld], health=_health(), suppressed={"role_trends": "requires nflverse usage, which is unavailable"},
        freshness_lines=["KTC dynasty · Fresh · 2.0h · 500 rows", "RotoBaller · Stale · 3.0d · 400 rows"],
        usage_note=NO_HISTORY_NOTE, crosswalk_note="Player id crosswalk: 391/394 matched",
        ledger_summary={"waiver": {"(open)": 3, "Completed": 1}}, watchlist_new=["L: X — promoted"], watchlist_watching=4,
    )
    md = render_weekly_report(report)
    html = _overview_panel(report) + _league_panel(ld)
    for sentinel in (
        "$20 · Aggressive", "Suggested bid uses approximately 25% of remaining budget ($20 of $80)", "Only two managers can outbid 20",
        "RotoBaller · Stale · 3.0d · 400 rows", "RotoBaller is 3.0d old", NO_HISTORY_NOTE, "391/394 matched",
        "Completed 1", "L: X — promoted", "4 more item(s) watched with nothing new to say",
    ):
        assert sentinel in md, sentinel
        # The HTML renders the same facts as chips, so check the tokens rather than the joined line.
        for token in sentinel.split(" · "):
            assert token in html or token.replace("·", "&middot;") in html, (sentinel, token)
    assert "Signal health — degraded" in md and "degraded" in html
    assert "role trends" in md and "role trends" in html  # the suppressed feature is named in both


def test_a_non_faab_league_shows_the_status_note_in_both_renderers():
    target = _target("rb1", "RB", MUST_ADD)
    roster = make_roster(entries=[make_entry(player_id="q", name="Q", position="QB")], fmt=make_format(roster_positions=("QB", "BN")))
    note = "League is not FAAB — waiver claims here run on priority order, so there is no bid to size."
    ld = LeagueReportData(league=make_league_info(name="L"), drafted=True, roster=roster, currency="dynasty",
                          waiver_targets=[target], faab_note=note)
    assert note in "\n".join(render_league_section(ld))
    assert "there is no bid to size" in _league_panel(ld)


def test_reports_without_a_health_grade_fall_back_to_bare_ages():
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1,
        source_freshness={"ktc": dt.timedelta(hours=2)}, ff_status="absent", leagues=[],
    )
    md = render_weekly_report(report)
    assert "## Data freshness" in md and "ktc:" in md and "Diagnostics" not in md
    assert "Data freshness" in _overview_panel(report)


# -- post-inspection guards ----------------------------------------------------------


def test_expected_absent_usage_never_grades_the_run_degraded():
    from sleeper_tool.signal_health import build_health

    class _Usage:
        absent = True
        fetched_at = None
        latest_week = None
        rows = 0
        stale = False

    report = build_health(usage_health=_Usage(), now=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc))
    usage = next(s for s in report.signals if s.family == "nflverse_usage")
    assert usage.label == "Unavailable" and usage.expected_absent
    assert "nflverse_usage" in report.unavailable_families  # still suppresses role trends
    assert not any(s.family == "nflverse_usage" and s.label != "Unavailable" for s in report.signals)
    # The only non-optional families here are all missing too, so the run IS degraded — but
    # not because of usage: remove every other signal and it isn't.
    report.signals = [usage]
    from sleeper_tool.signal_health import DEGRADED_LABELS, OPTIONAL_FAMILIES
    assert not any(s.label in DEGRADED_LABELS and not s.expected_absent for s in report.signals if s.family not in OPTIONAL_FAMILIES)
    assert any("not published for this season yet" in n for n in report.notes)


def test_abundant_market_with_many_substitutes_is_a_preserve_whatever_the_tier():
    from sleeper_tool.faab_strategy import MANY_SUBSTITUTES, PRESERVE, TargetFacts, choose_posture

    ctx = FaabContext(waiver_type=2, budget=100, my_used=0, others_used=[0], current_week=2, playoff_week_start=15)
    depth_qb = TargetFacts(player_id="q", tier=MUST_ADD, horizon="Season Starter", scarcity=ABUNDANT, substitutes=MANY_SUBSTITUTES, need_urgency=False, suggested_pct=35)
    posture, reasons = choose_posture(ctx, depth_qb)
    assert posture == PRESERVE and "don't pay a Must Add price" in reasons[0]
    # One fewer substitute, or an urgent need, and the guardrail does not apply.
    assert choose_posture(ctx, TargetFacts(player_id="q", tier=MUST_ADD, horizon="Season Starter", scarcity=ABUNDANT, substitutes=MANY_SUBSTITUTES - 1, suggested_pct=35))[0] != PRESERVE
    assert choose_posture(ctx, TargetFacts(player_id="q", tier=MUST_ADD, horizon="Season Starter", scarcity=ABUNDANT, substitutes=MANY_SUBSTITUTES, need_urgency=True, suggested_pct=35))[0] != PRESERVE


def test_only_observed_outcome_facts_are_rendered():
    from sleeper_tool.decision_outcomes import OBSERVED, PENDING, OutcomeFact

    pending = OutcomeFact("f1", "L", "waiver", "X", 1, 7, PENDING, facts=("window not reached yet (0 of 7 days)",))
    observed = OutcomeFact("f2", "L", "waiver", "Y", 1, 7, OBSERVED, facts=("still rostered", "entered the lineup"))
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={}, ff_status="absent",
        leagues=[], outcome_facts=[pending, observed], ledger_summary={"waiver": {"(open)": 1}},
    )
    md = render_weekly_report(report)
    html = _overview_panel(report)
    assert "window not reached" not in md and "window not reached" not in html
    assert "Y — 1-week window: still rostered; entered the lineup" in md and "entered the lineup" in html


class _LedgerStorage:
    def get_all_transactions(self, league_id):
        return []

    def get_rosters(self, league_id):
        return [{"roster_id": 1, "players": ["q"]}]


def test_new_ledger_entries_are_recorded_open_not_observed_in_the_same_run(monkeypatch, tmp_path):
    import sleeper_tool.report_data as rd
    from sleeper_tool.decision_ledger import Ledger

    monkeypatch.setattr(rd, "load_ledger", lambda: Ledger())
    monkeypatch.setattr(rd, "load_snapshots", lambda: [])
    roster = make_roster(roster_id=1, entries=[make_entry(player_id="q", name="Q", position="QB")], fmt=make_format(roster_positions=("QB", "BN")))
    ld = LeagueReportData(league=make_league_info(name="L"), drafted=True, roster=roster, currency="dynasty",
                          waiver_targets=[_target("rb1", "RB", MUST_ADD)])
    report = WeeklyReportData(generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={}, ff_status="absent", leagues=[ld])
    rd._attach_ledger(report, _LedgerStorage(), report.generated_at)
    assert report.ledger_new >= 1
    assert all(e.outcome is None for e in report.ledger.entries.values())
    assert report.ledger_summary.get("waiver") == {"(open)": report.ledger_new}
