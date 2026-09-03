"""Calibration lab: that eligible is counted before any list cap, that each
diagnostic label lands exactly on its threshold, and that the cross-signal
checks catch a fact stated twice on one card."""
from __future__ import annotations

import datetime as dt
from collections import Counter

from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool import calibration as cal
from sleeper_tool.calibration import (
    INSUFFICIENT_SAMPLE,
    LEAGUE_CONCENTRATED,
    MIN_SAMPLE,
    NEARLY_ALWAYS_FIRES,
    NEVER_FIRES,
    NORMAL,
    Observation,
    RuleSpec,
    calibrate,
    diagnose,
    render_calibration_markdown,
)
from sleeper_tool.replacement_value import NORMAL as NORMAL_SCARCITY
from sleeper_tool.replacement_value import PlayerReplacementContext, PositionMarket, ReplacementMarket, VERY_SCARCE
from sleeper_tool.report_data import LeagueReportData, WeeklyReportData
from sleeper_tool.source_disagreement import (
    HIGH_DISAGREEMENT,
    MARKET_ABOVE_PROJECTION,
    STRONG_CONSENSUS,
    SourceView,
)
from sleeper_tool.trade_engine import TradeProposal
from sleeper_tool.trade_opportunity_cost import MOSTLY_NEUTRAL, ROUGHLY_EVEN, TradeEconomics
from sleeper_tool.waiver_engine import MUST_ADD, MONITOR, WaiverTarget

NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def _entry(pid, pos="WR", proj=200.0):
    return make_entry(player_id=pid, name=pid, position=pos, value=make_value(name=pid, position=pos, proj_points=proj))


def _ld(name="L1", **kw):
    roster = make_roster(entries=kw.pop("entries", []), fmt=make_format(roster_positions=("QB", "RB", "BN")))
    defaults = dict(league=make_league_info(name=name), drafted=True, currency="dynasty", roster=roster)
    defaults.update(kw)
    return LeagueReportData(**defaults)


def _report(leagues, **kw):
    defaults = dict(generated_at=NOW, current_week=1, source_freshness={}, ff_status="ok", leagues=list(leagues))
    defaults.update(kw)
    return WeeklyReportData(**defaults)


def _target(pid, tier=MONITOR, **kw):
    return WaiverTarget(
        player_id=pid, name=pid, position="WR", team="KC", trend_count=1, value=make_value(),
        fills_need=False, need_rank=None, reason="r", priority_tier=tier, **kw
    )


def _proposal(*, give=(), receive=(), caveats=(), trade_type="buy_low"):
    return TradeProposal(
        league_name="L1", currency="dynasty", target_username="rival", target_team_name="Rival",
        give=list(give), receive=list(receive), my_value_total=100, their_value_total=100,
        rationale_for_me=[], rationale_for_them=[], caveats=list(caveats), trade_type=trade_type,
    )


def _result_for(result, name):
    return next(r for r in result.rules if r.name == name)


def _only(report, name, **kw):
    """Run the single named rule from the real inventory."""
    spec = next(s for s in cal.build_rules() if s.name == name)
    return _result_for(calibrate(report, rules=[spec], **kw), name)


# --------------------------------------------------------------------------
# eligible counting, caps, missing data
# --------------------------------------------------------------------------


def test_eligible_is_counted_before_the_highlight_cap():
    # Eight measurable players; the module highlights at most MAX_HIGHLIGHTED.
    contexts = {}
    for i in range(8):
        e = _entry(f"p{i}")
        contexts[e.player_id] = PlayerReplacementContext(e, 12.0, 3.0 + i, 1.0, 100.0, NORMAL_SCARCITY)
    highlighted = [contexts[f"p{i}"] for i in (7, 6, 5)]
    market = ReplacementMarket(positions={}, players=contexts, understated=highlighted, overstated=[])
    r = _only(_report([_ld(replacement=market)]), "Rank understates replacement edge")

    assert (r.eligible, r.triggered) == (8, 3)
    assert r.rate == 3 / 8
    assert "MAX_HIGHLIGHTED" in (r.note or "")
    assert r.examples[0].startswith("L1 — p7")  # ordered by the largest edge


def test_players_the_market_cannot_measure_are_not_eligible():
    measurable = PlayerReplacementContext(_entry("a"), 12.0, 3.0, 1.0, 100.0, NORMAL_SCARCITY)
    unmeasurable = PlayerReplacementContext(_entry("b"), None, None, None, None, VERY_SCARCE)
    market = ReplacementMarket(
        positions={}, players={"a": measurable, "b": unmeasurable}, understated=[measurable], overstated=[]
    )
    r = _only(_report([_ld(replacement=market)]), "Rank understates replacement edge")
    assert (r.eligible, r.triggered) == (1, 1)


def test_missing_data_rate_counts_views_without_a_consensus():
    def _view(name, consensus, gap):
        return SourceView(
            name=name, position="WR", consensus=consensus, consensus_gap=gap, consensus_pair=("KTC", "FP"),
            direction=MARKET_ABOVE_PROJECTION if consensus else None, market_rank=10, projection_rank=40,
            expert_note=None,
        )

    views = {f"m{i}": _view(f"m{i}", STRONG_CONSENSUS, 2) for i in range(6)}
    views.update({f"x{i}": _view(f"x{i}", None, None) for i in range(4)})
    r = _only(_report([_ld(source_views=views)]), f"Consensus: {STRONG_CONSENSUS}")

    assert (r.eligible, r.triggered, r.missing) == (6, 6, 4)
    assert r.missing_data_rate == 0.4
    # The rule fired on everything it could measure; that is the flag.
    assert r.diagnostic == INSUFFICIENT_SAMPLE  # 6 eligible is below MIN_SAMPLE


def test_a_bucket_no_view_landed_in_is_counted_but_never_fires():
    def _view(name):
        return SourceView(
            name=name, position="WR", consensus=STRONG_CONSENSUS, consensus_gap=1, consensus_pair=("KTC", "FP"),
            direction=None, market_rank=10, projection_rank=11, expert_note=None,
        )

    views = {f"m{i}": _view(f"m{i}") for i in range(cal.NEVER_FIRES_MIN_ELIGIBLE)}
    r = _only(_report([_ld(source_views=views)]), f"Consensus: {HIGH_DISAGREEMENT}")
    assert (r.eligible, r.triggered, r.diagnostic) == (cal.NEVER_FIRES_MIN_ELIGIBLE, 0, NEVER_FIRES)


# --------------------------------------------------------------------------
# diagnostic labels, exactly at their thresholds
# --------------------------------------------------------------------------


def test_nearly_always_fires_boundary_is_inclusive():
    assert diagnose(10, 6, Counter({"L1": 6}), 1) == NEARLY_ALWAYS_FIRES  # 0.60 exactly
    assert diagnose(10, 5, Counter({"L1": 5}), 1) == NORMAL  # 0.50
    assert cal.NEARLY_ALWAYS_FIRES_MIN_RATE == 0.60


def test_never_fires_needs_the_minimum_eligible_count():
    assert diagnose(cal.NEVER_FIRES_MIN_ELIGIBLE, 0, Counter(), 1) == NEVER_FIRES
    assert diagnose(cal.NEVER_FIRES_MIN_ELIGIBLE - 1, 0, Counter(), 1) == NORMAL


def test_min_sample_boundary():
    assert diagnose(MIN_SAMPLE - 1, 0, Counter(), 1) == INSUFFICIENT_SAMPLE
    assert diagnose(MIN_SAMPLE - 1, 9, Counter({"L1": 9}), 1) == INSUFFICIENT_SAMPLE
    assert diagnose(MIN_SAMPLE, 1, Counter({"L1": 1}), 1) == NORMAL


def test_league_concentration_at_the_exact_share():
    at = Counter({"L1": 6, "L2": 1, "L3": 1})  # 6/8 = 0.75
    below = Counter({"L1": 5, "L2": 2, "L3": 1})  # 5/8 = 0.625
    assert diagnose(20, 8, at, 3) == LEAGUE_CONCENTRATED
    assert diagnose(20, 8, below, 3) == NORMAL
    # ... and it needs enough triggers and enough leagues to mean anything.
    assert diagnose(20, 4, Counter({"L1": 4}), 3) == NORMAL
    assert diagnose(20, 8, at, 2) == NORMAL


def test_thresholds_end_to_end_over_real_waiver_rules():
    def report_with(must_adds, others):
        targets = [_target(f"m{i}", MUST_ADD) for i in range(must_adds)]
        targets += [_target(f"o{i}", MONITOR) for i in range(others)]
        return _report([_ld(waiver_targets=targets)])

    assert _only(report_with(6, 4), f"Priority tier: {MUST_ADD}").diagnostic == NEARLY_ALWAYS_FIRES
    assert _only(report_with(5, 5), f"Priority tier: {MUST_ADD}").diagnostic == NORMAL
    assert _only(report_with(4, 5), f"Priority tier: {MUST_ADD}").diagnostic == INSUFFICIENT_SAMPLE
    assert _only(report_with(25, 0), f"Priority tier: {MONITOR}").diagnostic == NEVER_FIRES


def test_league_concentration_end_to_end():
    def league(name, must_adds):
        targets = [_target(f"{name}m{i}", MUST_ADD) for i in range(must_adds)]
        targets += [_target(f"{name}o{i}", MONITOR) for i in range(2)]
        return _ld(name=name, waiver_targets=targets)

    concentrated = _report([league("L1", 6), league("L2", 1), league("L3", 1)])
    r = _only(concentrated, f"Priority tier: {MUST_ADD}")
    assert (r.eligible, r.triggered, r.leagues_eligible) == (14, 8, 3)
    assert r.diagnostic == LEAGUE_CONCENTRATED
    assert r.leagues_triggered == ["L1", "L2", "L3"]

    spread = _report([league("L1", 5), league("L2", 2), league("L3", 1)])
    assert _only(spread, f"Priority tier: {MUST_ADD}").diagnostic == NORMAL


def test_a_rule_that_needs_more_leagues_than_the_report_has_is_insufficient():
    report = _report([_ld()], portfolio=_portfolio({"a": 9}))
    r = _only(report, "Exposure: High Exposure")
    assert r.diagnostic == INSUFFICIENT_SAMPLE  # one league can't evidence a cross-league threshold


def _portfolio(counts):
    from sleeper_tool.portfolio_exposure import PortfolioExposure

    return PortfolioExposure(total_leagues=len(counts), players=[], counts_by_player_id=dict(counts))


def test_errored_and_undrafted_leagues_are_not_eligible():
    good = _ld(name="ok", waiver_targets=[_target("a", MUST_ADD)])
    broken = LeagueReportData(league=make_league_info(name="broken"), error="boom", waiver_targets=[_target("b", MUST_ADD)])
    undrafted = LeagueReportData(league=make_league_info(name="pre"), drafted=False, waiver_targets=[_target("c", MUST_ADD)])
    r = _only(_report([good, broken, undrafted]), f"Priority tier: {MUST_ADD}")
    assert (r.eligible, r.triggered, r.leagues_triggered) == (1, 1, ["ok"])


# --------------------------------------------------------------------------
# custom specs — the plumbing itself
# --------------------------------------------------------------------------


def test_a_custom_spec_reports_examples_by_magnitude():
    spec = RuleSpec(
        module="synthetic", name="Synthetic", constants=(("K", 1),),
        observe=lambda report: [
            Observation("L1", True, example=f"ex{i}", magnitude=float(i)) for i in range(5)
        ],
    )
    r = _result_for(calibrate(_report([]), rules=[spec]), "Synthetic")
    assert r.examples == ["ex4", "ex3", "ex2"]  # biggest magnitude first, capped at MAX_EXAMPLES
    assert r.constants_text == "K=1"


def test_time_gated_rules_carry_their_explanation_not_a_suppression():
    from sleeper_tool.market_velocity import INSUFFICIENT_HISTORY, Velocity

    velocities = {f"p{i}": Velocity(INSUFFICIENT_HISTORY, 1, None, None, None) for i in range(30)}
    r = _only(_report([_ld(velocity=velocities)]), f"Velocity: {INSUFFICIENT_HISTORY}")
    assert r.diagnostic == NEARLY_ALWAYS_FIRES  # still counted and still flagged
    assert r.time_gated and "snapshot" in r.time_gated
    assert "expected early" in cal.interpret(r)


# --------------------------------------------------------------------------
# cross-signal findings
# --------------------------------------------------------------------------


def test_scarcity_stated_twice_on_one_card_is_detected():
    twice = _proposal(caveats=["Replacement context: A is Very Scarce here; nothing on waivers replaces him."])
    once = _proposal(caveats=["Sources on B: split."])
    ld = _ld(
        proposals=[twice, once],
        trade_economics=[
            TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, -1.0, False, scarcity_note="QB market is Scarce"),
            TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, 1.0, False),
        ],
    )
    finding = _cross(_report([ld]), "Trade cards stating scarcity twice or more")
    assert (finding.count, finding.of, finding.share) == (1, 2, 0.5)


def test_exposure_stated_in_both_a_note_and_a_conflict_is_detected():
    from sleeper_tool.recommendation_conflicts import Conflict, WAIVER

    both = _target("w1", MUST_ADD)
    both.notes.append("portfolio exposure: would put him on 5 of your 8 rosters (Very High Exposure)")
    note_only = _target("w2", MUST_ADD)
    note_only.notes.append("portfolio exposure: would put him on 4 of your 8 rosters (High Exposure)")
    conflict = Conflict(WAIVER, "w1", "Add w1", [], ["the add would push cross-league exposure to Very High"])
    finding = _cross(_report([_ld(waiver_targets=[both, note_only], conflicts=[conflict])]),
                     "Recommendations stating exposure in both a note and a conflict")
    assert (finding.count, finding.of) == (1, 2)


def test_very_scarce_sell_high_conflict_share():
    from sleeper_tool.recommendation_conflicts import Conflict, TRADE

    qb = _entry("qb", "QB", 340)
    market = ReplacementMarket(
        positions={"QB": PositionMarket("QB", None, None, None, None, VERY_SCARCE, None)}, players={}
    )
    conflicted = _proposal(give=[qb], trade_type="sell_high")
    clean = _proposal(give=[qb], trade_type="sell_high")
    ld = _ld(
        proposals=[conflicted, clean], replacement=market,
        conflicts=[Conflict(TRADE, "0", "s", [], ["QB replacement market is Very Scarce"])],
    )
    finding = _cross(_report([ld]), "Very Scarce sell-high proposals that are also Conflicted")
    assert (finding.count, finding.of, finding.share) == (1, 2, 0.5)


def test_role_trend_comparison_is_skipped_without_labels_and_runs_with_them():
    from sleeper_tool.market_velocity import FALLING, RISING, Velocity

    ld = _ld(velocity={"a": Velocity(RISING, 4, 0.2, "d1", "d4"), "b": Velocity(FALLING, 4, -0.2, "d1", "d4")})
    names = {f.name for f in cal.cross_signals(_report([ld]))}
    assert not any("Role trend" in n for n in names)

    findings = cal.cross_signals(_report([ld]), role_labels={"a": "Role rising", "b": "Role rising"})
    agree = next(f for f in findings if f.name.endswith("agree"))
    disagree = next(f for f in findings if f.name.endswith("disagree"))
    assert (agree.count, agree.of) == (1, 2)
    assert (disagree.count, disagree.of) == (1, 2)


def test_developmental_drop_overlap_should_be_empty_by_construction():
    finding = _cross(_report([_ld()]), "Developmental-drop conflicts whose drop is also a drop candidate")
    assert (finding.count, finding.of, finding.share) == (0, 0, None)


def _cross(report, name):
    return next(f for f in cal.cross_signals(report) if f.name == name)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_render_is_deterministic_and_groups_by_module_in_inventory_order():
    report = _report([_ld(waiver_targets=[_target(f"m{i}", MUST_ADD) for i in range(30)])])
    first = render_calibration_markdown(calibrate(report))
    second = render_calibration_markdown(calibrate(report))
    assert first == second

    modules = [line[4:] for line in first.splitlines() if line.startswith("### ") and "." not in line]
    inventory: list[str] = []
    for spec in cal.build_rules():
        if spec.module not in inventory:
            inventory.append(spec.module)
    assert [m for m in modules if m in inventory] == inventory

    assert "# Calibration report" in first
    assert "## Flags" in first and "## Cross-signal findings" in first
    assert f"Priority tier: {MUST_ADD}" in first


def test_flags_section_names_every_non_normal_rule_with_an_interpretation():
    report = _report([_ld(waiver_targets=[_target(f"m{i}", MUST_ADD) for i in range(30)])])
    result = calibrate(report)
    text = render_calibration_markdown(result)
    flagged = result.flagged()
    assert flagged
    for r in flagged:
        assert f"**{r.module}.{r.name}**" in text
    assert f"{len(flagged)} of {len(result.rules)} rules are not Normal." in text


def test_an_empty_report_renders_without_error():
    result = calibrate(_report([]))
    text = render_calibration_markdown(result)
    assert result.leagues == []
    assert all(r.eligible == 0 and r.triggered == 0 for r in result.rules)
    assert all(r.diagnostic == INSUFFICIENT_SAMPLE for r in result.rules)
    assert "Leagues analysed: 0" in text
    assert "## Cross-signal findings" in text


def test_every_rule_names_its_module_and_at_least_one_constant():
    for spec in cal.build_rules():
        assert spec.module and spec.name
        assert spec.constants, f"{spec.name} states no constant"
