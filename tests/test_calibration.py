"""Calibration lab: that eligible is counted before any list cap, that each
diagnostic label lands exactly on its threshold, that the cross-signal and
contradiction checks catch a fact stated twice or pulled two ways on one
card, that the drop-protection monitor counts what every rule protects, and
that the dependency map follows RuleSpec.inputs."""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections import Counter

from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool import calibration as cal
from sleeper_tool.calibration import (
    FORMAT_BIASED,
    HEALTHY,
    INSUFFICIENT_SAMPLE,
    LEAGUE_CONCENTRATED,
    MIN_SAMPLE,
    NEARLY_ALWAYS_FIRES,
    NEARLY_UNIVERSAL,
    NEVER_FIRES,
    NORMAL,
    OVERACTIVE,
    POSITION_BIASED,
    POTENTIAL_DOUBLE_COUNT,
    RARE,
    Observation,
    RuleSpec,
    calibrate,
    diagnose,
    render_calibration_markdown,
)
from sleeper_tool.replacement_value import ABUNDANT
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
from sleeper_tool.trade_opportunity_cost import FAVORABLE, MAJOR_LINEUP_COST, MOSTLY_NEUTRAL, ROUGHLY_EVEN, TradeEconomics
from sleeper_tool.waiver_engine import INSURANCE, MUST_ADD, MONITOR, STRONG_ADD, WaiverTarget

NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def _entry(pid, pos="WR", proj=200.0):
    return make_entry(player_id=pid, name=pid, position=pos, value=make_value(name=pid, position=pos, proj_points=proj))


def _ld(name="L1", *, qb_format="1QB", kind="dynasty", league_id="1", **kw):
    roster = make_roster(
        entries=kw.pop("entries", []), fmt=make_format(qb_format=qb_format, roster_positions=("QB", "RB", "BN"))
    )
    defaults = dict(
        league=make_league_info(name=name, kind=kind, league_id=league_id), drafted=True, currency="dynasty", roster=roster
    )
    defaults.update(kw)
    return LeagueReportData(**defaults)


def _report(leagues, **kw):
    defaults = dict(generated_at=NOW, current_week=1, source_freshness={}, ff_status="ok", leagues=list(leagues))
    defaults.update(kw)
    return WeeklyReportData(**defaults)


def _target(pid, tier=MONITOR, position="WR", reason="r", **kw):
    return WaiverTarget(
        player_id=pid, name=pid, position=position, team="KC", trend_count=1, value=make_value(),
        fills_need=False, need_rank=None, reason=reason, priority_tier=tier, **kw
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


def test_nearly_universal_boundary_is_inclusive():
    assert diagnose(10, 6, Counter({"L1": 6}), 1) == NEARLY_UNIVERSAL  # 0.60 exactly
    assert diagnose(10, 5, Counter({"L1": 5}), 1) == OVERACTIVE  # 0.50 is the band below
    assert cal.NEARLY_ALWAYS_FIRES_MIN_RATE == 0.60
    assert NEARLY_ALWAYS_FIRES == NEARLY_UNIVERSAL and NORMAL == HEALTHY  # the v1 names still resolve


def test_overactive_band_is_forty_to_sixty_exclusive_of_the_top():
    assert cal.OVERACTIVE_MIN_RATE == 0.40
    assert diagnose(100, 40, Counter({"L1": 40}), 1) == OVERACTIVE  # 0.40 exactly
    assert diagnose(100, 39, Counter({"L1": 39}), 1) == HEALTHY  # one below
    assert diagnose(100, 59, Counter({"L1": 59}), 1) == OVERACTIVE  # top of the band
    assert diagnose(100, 60, Counter({"L1": 60}), 1) == NEARLY_UNIVERSAL  # hands over at 0.60


def test_rare_boundary_is_strictly_below_five_percent():
    assert cal.RARE_MAX_RATE == 0.05
    assert diagnose(100, 4, Counter({"L1": 4}), 1) == RARE  # 0.04
    assert diagnose(100, 5, Counter({"L1": 5}), 1) == HEALTHY  # 0.05 exactly is not rare
    assert diagnose(MIN_SAMPLE, 1, Counter({"L1": 1}), 1) == HEALTHY  # 10% at the smallest judged sample


def test_never_fires_needs_the_minimum_eligible_count_and_reads_rare_below_it():
    assert diagnose(cal.NEVER_FIRES_MIN_ELIGIBLE, 0, Counter(), 1) == NEVER_FIRES
    assert diagnose(cal.NEVER_FIRES_MIN_ELIGIBLE - 1, 0, Counter(), 1) == RARE  # zero of 24 is rare, not dead


def test_min_sample_boundary():
    assert diagnose(MIN_SAMPLE - 1, 0, Counter(), 1) == INSUFFICIENT_SAMPLE
    assert diagnose(MIN_SAMPLE - 1, 9, Counter({"L1": 9}), 1) == INSUFFICIENT_SAMPLE
    assert diagnose(MIN_SAMPLE, 1, Counter({"L1": 1}), 1) == HEALTHY


def test_league_concentration_at_the_exact_share():
    at = Counter({"L1": 6, "L2": 1, "L3": 1})  # 6/8 = 0.75
    below = Counter({"L1": 5, "L2": 2, "L3": 1})  # 5/8 = 0.625
    assert diagnose(30, 8, at, 3) == LEAGUE_CONCENTRATED
    assert diagnose(30, 8, below, 3) == HEALTHY
    # ... and it needs enough triggers and enough leagues to mean anything.
    assert diagnose(30, 4, Counter({"L1": 4}), 3) == HEALTHY
    assert diagnose(30, 8, at, 2) == HEALTHY


def test_labels_stack_and_the_where_label_leads():
    """8 of 20 from one league is both Overactive and League-Concentrated;
    the concentration is the more specific thing to look at, so it is the
    primary and the band is kept as a secondary."""
    labels = cal.diagnose_all(20, 8, Counter({"L1": 8}), 3)
    assert labels == [LEAGUE_CONCENTRATED, OVERACTIVE]
    assert diagnose(20, 8, Counter({"L1": 8}), 3) == LEAGUE_CONCENTRATED
    assert cal.diagnose_all(20, 1, Counter({"L1": 1}), 3) == [HEALTHY]  # 5% exactly, one league: nothing to say
    assert cal.diagnose_all(5, 5, Counter({"L1": 5}), 1) == [INSUFFICIENT_SAMPLE]  # silences everything else


def test_league_concentration_needs_exactly_five_triggers():
    """LEAGUE_CONCENTRATION_MIN_TRIGGERS pinned by value and AT the bar —
    the existing test only checks four, one below it, so a change from 5 to
    6 would not be caught."""
    assert cal.LEAGUE_CONCENTRATION_MIN_TRIGGERS == 5
    assert cal.LEAGUE_CONCENTRATION_MIN_LEAGUES == 3
    assert diagnose(20, 5, Counter({"L1": 5}), 3) == LEAGUE_CONCENTRATED  # exactly 5
    assert diagnose(20, 4, Counter({"L1": 4}), 3) == HEALTHY  # one short
    assert diagnose(20, 5, Counter({"L1": 5}), 2) == HEALTHY  # 5 triggers, too few leagues


def test_thresholds_end_to_end_over_real_waiver_rules():
    def report_with(must_adds, others):
        targets = [_target(f"m{i}", MUST_ADD) for i in range(must_adds)]
        targets += [_target(f"o{i}", MONITOR) for i in range(others)]
        return _report([_ld(waiver_targets=targets)])

    assert _only(report_with(6, 4), f"Priority tier: {MUST_ADD}").diagnostic == NEARLY_UNIVERSAL
    assert _only(report_with(5, 5), f"Priority tier: {MUST_ADD}").diagnostic == OVERACTIVE
    assert _only(report_with(3, 7), f"Priority tier: {MUST_ADD}").diagnostic == HEALTHY
    assert _only(report_with(4, 5), f"Priority tier: {MUST_ADD}").diagnostic == INSUFFICIENT_SAMPLE
    assert _only(report_with(25, 0), f"Priority tier: {MONITOR}").diagnostic == NEVER_FIRES
    assert _only(report_with(1, 24), f"Priority tier: {MUST_ADD}").diagnostic == RARE


def test_league_concentration_end_to_end():
    def league(name, must_adds):
        targets = [_target(f"{name}m{i}", MUST_ADD) for i in range(must_adds)]
        targets += [_target(f"{name}o{i}", MONITOR) for i in range(2)]
        return _ld(name=name, waiver_targets=targets)

    concentrated = _report([league("L1", 6), league("L2", 1), league("L3", 1)])
    r = _only(concentrated, f"Priority tier: {MUST_ADD}")
    assert (r.eligible, r.triggered, r.leagues_eligible) == (14, 8, 3)
    assert r.diagnostic == LEAGUE_CONCENTRATED
    assert r.diagnostics == [LEAGUE_CONCENTRATED, OVERACTIVE]  # 8 of 14 is also a coin flip
    assert r.leagues_triggered == ["L1", "L2", "L3"]

    spread = _report([league("L1", 5), league("L2", 2), league("L3", 1)])
    r = _only(spread, f"Priority tier: {MUST_ADD}")
    assert r.diagnostic == OVERACTIVE and LEAGUE_CONCENTRATED not in r.diagnostics


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
    assert f"{len(flagged)} of {len(result.rules)} rules are not {HEALTHY}." in text


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


# --------------------------------------------------------------------------
# format and position bias
# --------------------------------------------------------------------------


def _fmt_counts(**by):
    return Counter(by)


def test_format_bias_needs_the_trigger_share_and_a_smaller_eligible_share():
    assert cal.FORMAT_SHARE == 0.80 and cal.BIAS_MIN_TRIGGERS == 5
    elig = _fmt_counts(**{"SF dynasty": 50, "1QB dynasty": 50})
    at = _fmt_counts(**{"SF dynasty": 8, "1QB dynasty": 2})  # 0.80 exactly
    below = _fmt_counts(**{"SF dynasty": 7, "1QB dynasty": 3})  # 0.70
    assert cal.format_bias(10, at, elig) == ("SF", 8, 0.5)
    assert cal.format_bias(10, below, elig) is None
    # A format that IS 80%+ of the eligible set explains its own share.
    lopsided = _fmt_counts(**{"SF dynasty": 80, "1QB dynasty": 20})
    assert cal.format_bias(10, at, lopsided) is None
    # Too few triggers, or only one format eligible, says nothing.
    assert cal.format_bias(4, _fmt_counts(**{"SF dynasty": 4}), elig) is None
    assert cal.format_bias(10, _fmt_counts(**{"SF dynasty": 10}), _fmt_counts(**{"SF dynasty": 50})) is None


def test_format_bias_reads_the_kind_axis_too():
    elig = _fmt_counts(**{"1QB dynasty": 30, "1QB keeper": 30, "1QB redraft": 30})
    trig = _fmt_counts(**{"1QB redraft": 9, "1QB dynasty": 1})
    assert cal.format_bias(10, trig, elig) == ("redraft", 9, 30 / 90)


def test_format_bias_end_to_end_reads_league_kind_and_superflex():
    def league(name, qb_format, n_must):
        targets = [_target(f"{name}m{i}", MUST_ADD) for i in range(n_must)]
        targets += [_target(f"{name}o{i}", MONITOR) for i in range(10 - n_must)]
        return _ld(name=name, qb_format=qb_format, waiver_targets=targets)

    report = _report([league("SF1", "SF", 4), league("SF2", "SF", 4), league("Q1", "1QB", 1), league("Q2", "1QB", 0)])
    r = _only(report, f"Priority tier: {MUST_ADD}")
    assert (r.eligible, r.triggered) == (40, 9)
    assert r.by_format == {"SF dynasty": 8, "1QB dynasty": 1}
    assert r.diagnostic == FORMAT_BIASED
    assert "8 of 9 triggers from SF" in r.bias_detail
    assert "8 of 9 triggers from SF" in cal.interpret(r)


def test_position_bias_boundaries():
    assert cal.POSITION_SHARE == 0.80 and cal.POSITION_BIAS_MIN_POSITIONS == 3
    elig = Counter(QB=20, RB=20, WR=20)
    assert cal.position_bias(10, Counter(QB=8, RB=2), elig) == ("QB", 8, 20 / 60)  # 0.80 exactly
    assert cal.position_bias(10, Counter(QB=7, RB=3), elig) is None
    assert cal.position_bias(10, Counter(QB=8, RB=2), Counter(QB=20, RB=20)) is None  # only two positions eligible
    assert cal.position_bias(10, Counter(QB=8, RB=2), Counter(QB=50, RB=5, WR=5)) is None  # QB is 83% of eligible
    assert cal.position_bias(4, Counter(QB=4), elig) is None


def test_position_bias_end_to_end_over_a_player_level_rule():
    targets = [_target(f"q{i}", MUST_ADD, position="QB") for i in range(8)]
    targets += [_target(f"r{i}", MUST_ADD if i == 0 else MONITOR, position="RB") for i in range(8)]
    targets += [_target(f"w{i}", MONITOR, position="WR") for i in range(8)]
    r = _only(_report([_ld(waiver_targets=targets)]), f"Priority tier: {MUST_ADD}")
    assert (r.eligible, r.triggered) == (24, 9)
    assert r.positions_eligible == {"QB": 8, "RB": 8, "WR": 8}
    assert r.diagnostic == POSITION_BIASED
    assert OVERACTIVE not in r.diagnostics  # 9 of 24 is 37.5%
    assert "8 of 9 triggers at QB" in r.bias_detail


def test_league_level_rules_carry_no_position_and_are_never_position_biased():
    r = _only(_report([_ld()]), "Playoff leverage available")
    assert r.positions_eligible == {} and r.by_position == {}


# --------------------------------------------------------------------------
# potential double counts
# --------------------------------------------------------------------------


def _spec(name, subjects, eligible=20, *, module="synthetic"):
    """A rule that fired on exactly `subjects` out of `eligible` chances."""
    fired = [Observation("L1", True, subject=s) for s in subjects]
    quiet = [Observation("L1", False, subject=f"quiet{i}") for i in range(eligible - len(subjects))]
    return RuleSpec(module=module, name=name, constants=(("K", 1),), observe=lambda report, obs=fired + quiet: list(obs))


def test_double_count_overlap_share_is_measured_against_the_smaller_set():
    assert cal.OVERLAP_SHARE == 0.80 and cal.OVERLAP_MIN_TRIGGERS == 5
    a = _spec("A", [f"p{i}" for i in range(5)])  # 5 triggers
    b_at = _spec("B", [f"p{i}" for i in range(4)] + ["x1", "x2"])  # 4 of A's 5 = 0.80
    b_below = _spec("B", [f"p{i}" for i in range(3)] + ["x1", "x2", "x3"])  # 3 of 5 = 0.60
    result = calibrate(_report([]), rules=[a, b_at])
    assert [(d.rule_a, d.rule_b, d.overlap, d.smaller, d.share) for d in result.double_counts] == [
        ("synthetic.A", "synthetic.B", 4, 5, 0.8)
    ]
    assert all(r.diagnostic == POTENTIAL_DOUBLE_COUNT for r in result.rules)
    assert not calibrate(_report([]), rules=[a, b_below]).double_counts


def test_double_count_needs_the_minimum_triggers_on_both_sides():
    a = _spec("A", [f"p{i}" for i in range(4)])  # one short
    b = _spec("B", [f"p{i}" for i in range(4)] + ["x"])
    assert not calibrate(_report([]), rules=[a, b]).double_counts


def test_double_count_ignores_a_larger_rule_that_fires_on_nearly_everything():
    a = _spec("A", [f"p{i}" for i in range(5)], eligible=20)
    everything = _spec("Everything", [f"p{i}" for i in range(16)], eligible=20)  # 0.80 of its eligible set
    assert not calibrate(_report([]), rules=[a, everything]).double_counts
    most = _spec("Most", [f"p{i}" for i in range(15)], eligible=20)  # 0.75: still a real containment
    assert len(calibrate(_report([]), rules=[a, most]).double_counts) == 1


def test_double_count_label_is_secondary_to_a_rate_label():
    a = _spec("A", [f"p{i}" for i in range(12)], eligible=20)  # 60%: Nearly Universal
    b = _spec("B", [f"p{i}" for i in range(10)], eligible=40)  # 25%, contained in A
    result = calibrate(_report([]), rules=[a, b])
    ra = next(r for r in result.rules if r.name == "A")
    rb = next(r for r in result.rules if r.name == "B")
    assert ra.diagnostics == [NEARLY_UNIVERSAL, POTENTIAL_DOUBLE_COUNT]
    assert rb.diagnostics == [POTENTIAL_DOUBLE_COUNT]
    assert "also: Potential Double Count" in cal.interpret(ra)
    text = render_calibration_markdown(result)
    assert "### Potential double counts" in text and "| synthetic.A | synthetic.B | 10 | 10 | 100% |" in text


def test_subjects_use_one_convention_per_kind_across_the_real_inventory():
    """The same player under a waiver rule and a drop rule must compare
    equal, and a trade under economics and acceptance must too."""
    from sleeper_tool.trade_types import DropCandidate

    e = _entry("p1")
    ld = _ld(
        waiver_targets=[_target("p1", MUST_ADD)],
        drop_candidates=[DropCandidate(e, "Strong Drop", ["r"])],
        proposals=[_proposal(give=[e])],
        trade_economics=[TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, 0.0, False)],
    )
    report = _report([ld])
    assert _only(report, f"Priority tier: {MUST_ADD}").subjects == {"L1|player:p1"}
    assert _only(report, "Drop priority: Strong Drop").subjects == {"L1|player:p1"}
    assert _only(report, "Acceptance: Moderate").subjects == {"L1|trade:0"}
    assert _only(report, f"Asset economics: {ROUGHLY_EVEN}").subjects == {"L1|trade:0"}


# --------------------------------------------------------------------------
# contradictions
# --------------------------------------------------------------------------


def _lineup(*starter_ids):
    from sleeper_tool.lineup_optimizer import LineupResult, SlotAssignment

    return LineupResult(
        assignments=[SlotAssignment(slot="WR", slot_index=i, player_id=p, name=p, position="WR", projection=10.0)
                     for i, p in enumerate(starter_ids)],
        total_projected_points=10.0 * len(starter_ids), unfilled_slots=[], bench_player_ids=[], unavailable={},
    )


def _leverage(lineup, surplus_entries):
    from sleeper_tool.lineup_leverage import BenchSurplus, LineupLeverage

    surplus = [BenchSurplus(e, 100.0, "WR", e, 90.0, 1.1, 50.0) for e in surplus_entries]
    return LineupLeverage(lineup=lineup, decisions=[], bench_surplus=surplus, weekly_starter_points=10.0, games_left=17)


def _contra(report, name):
    return next(c for c in cal.contradictions(report) if c.name == name)


def test_drop_candidate_who_is_bench_surplus_is_an_invariant_breach():
    from sleeper_tool.trade_types import DropCandidate

    a, b = _entry("a"), _entry("b")
    ld = _ld(
        entries=[a, b], lineup=_lineup(), lineup_leverage=_leverage(_lineup(), [a]),
        drop_candidates=[DropCandidate(a, "Consider Dropping", ["r"]), DropCandidate(b, "Strong Drop", ["r"])],
    )
    c = _contra(_report([ld]), "Drop candidate who is also bench surplus")
    assert (c.count, c.of, c.share, c.should_be_zero) == (1, 2, 0.5, True)
    assert c.examples == ["L1 — a is a Consider Dropping and bench surplus"]


def test_drop_who_is_an_optimized_starter_counts_the_list_and_the_paired_waiver_drop():
    from sleeper_tool.trade_types import DropCandidate

    s, bench = _entry("s"), _entry("bench")
    ld = _ld(
        entries=[s, bench], lineup=_lineup("s"),
        drop_candidates=[DropCandidate(s, "Strong Drop", ["r"])],
        waiver_targets=[_target("w1", drop_candidate=s), _target("w2", drop_candidate=bench), _target("w3")],
    )
    c = _contra(_report([ld]), "Drop (list or paired waiver drop) who is an optimized starter")
    assert (c.count, c.of) == (2, 3)  # the list entry and the w1 pairing; w3 has no drop so is not a chance


def test_clog_and_drop_candidate_overlap_is_an_invariant_breach():
    from sleeper_tool.roster_clog import RosterClog
    from sleeper_tool.trade_types import DropCandidate

    a, b = _entry("a"), _entry("b")
    ld = _ld(
        drop_candidates=[DropCandidate(a, "Strong Drop", ["r"])],
        roster_clogs=[RosterClog(a, ["r"], 1.0), RosterClog(b, ["r"], 2.0)],
    )
    c = _contra(_report([ld]), "Roster clog who is also a drop candidate")
    assert (c.count, c.of, c.should_be_zero) == (1, 2, True)


def test_sell_high_from_a_very_scarce_position_counts_only_pieces_that_play():
    qb_starter, qb_bench, wr = _entry("qb1", "QB"), _entry("qb2", "QB"), _entry("wr", "WR")
    market = ReplacementMarket(
        positions={
            "QB": PositionMarket("QB", None, None, None, None, VERY_SCARCE, None),
            "WR": PositionMarket("WR", None, None, None, None, ABUNDANT, 0.05),
        },
        players={},
    )
    ld = _ld(
        lineup=_lineup("qb1"), replacement=market,
        proposals=[
            _proposal(give=[qb_starter], trade_type="sell_high"),
            _proposal(give=[qb_bench], trade_type="sell_high"),  # surplus sale: allowed
            _proposal(give=[wr], trade_type="sell_high"),
            _proposal(give=[qb_starter], trade_type="buy_low"),  # not a sell-high
        ],
    )
    c = _contra(_report([ld]), "Sell-high piece who starts at a Very Scarce position")
    assert (c.count, c.of, c.should_be_zero) == (1, 3, False)
    assert "qb1's QB market is Very Scarce" in c.examples[0]


def test_paid_tier_with_not_an_immediate_upgrade_is_split_by_tier():
    depth = "would be depth behind your current starting WR, X (90th percentile), not an immediate upgrade"
    targets = [
        _target("m1", MUST_ADD, reason=depth), _target("m2", MUST_ADD, reason="fills your worst slot"),
        _target("s1", STRONG_ADD, reason=depth), _target("s2", STRONG_ADD, reason=depth), _target("s3", STRONG_ADD),
        _target("o1", MONITOR, reason=depth),
    ]
    report = _report([_ld(waiver_targets=targets)])
    must = _contra(report, f"{MUST_ADD} whose row says 'not an immediate upgrade'")
    strong = _contra(report, f"{STRONG_ADD} whose row says 'not an immediate upgrade'")
    assert (must.count, must.of, must.should_be_zero) == (1, 2, True)
    assert (strong.count, strong.of, strong.should_be_zero) == (2, 3, False)


def test_insurance_for_an_abundant_position_counts_rows_and_insurance_tier_targets():
    from sleeper_tool.contender_insurance import InsuranceRecommendation

    rb, wr, fa = _entry("rb", "RB"), _entry("wr", "WR"), _entry("fa", "RB")
    market = ReplacementMarket(
        positions={
            "RB": PositionMarket("RB", None, None, None, None, ABUNDANT, 0.05),
            "WR": PositionMarket("WR", None, None, None, None, VERY_SCARCE, None),
        },
        players={},
    )
    ld = _ld(
        replacement=market,
        insurance=[
            InsuranceRecommendation(rb, "RB", 10.0, 5.0, fa, 8.0),
            InsuranceRecommendation(wr, "WR", 10.0, 5.0, fa, 8.0),
        ],
        waiver_targets=[_target("i1", INSURANCE, position="RB"), _target("i2", INSURANCE, position="WR")],
    )
    c = _contra(_report([ld]), "Insurance row for an Abundant position")
    assert (c.count, c.of, c.should_be_zero) == (2, 4, True)


def test_favorable_assets_with_a_major_lineup_cost_is_counted_as_a_tradeoff():
    ld = _ld(
        proposals=[_proposal(), _proposal(), _proposal()],
        trade_economics=[
            TradeEconomics(FAVORABLE, MAJOR_LINEUP_COST, -8.0, True),
            TradeEconomics(FAVORABLE, MOSTLY_NEUTRAL, 0.5, False),
            None,  # below the preview bar: not a chance
        ],
    )
    c = _contra(_report([ld]), "Trade Favorable by assets and a Major Lineup Cost")
    assert (c.count, c.of, c.should_be_zero) == (1, 2, False)


def test_contradictions_on_an_empty_report_are_all_zero_of_zero():
    for c in cal.contradictions(_report([_ld()])):
        assert (c.count, c.of, c.share) == (0, 0, None)


def test_contradictions_render_with_kind_and_examples():
    from sleeper_tool.trade_types import DropCandidate

    a = _entry("a")
    ld = _ld(
        entries=[a], lineup=_lineup(), lineup_leverage=_leverage(_lineup(), [a]),
        drop_candidates=[DropCandidate(a, "Strong Drop", ["r"])],
    )
    text = render_calibration_markdown(calibrate(_report([ld])))
    assert "## Contradictions" in text
    assert "| Drop candidate who is also bench surplus | 1 | 1 | 100% | invariant |" in text
    assert "- **Drop candidate who is also bench surplus** (1 of 1; should be zero)" in text
    assert "  - e.g. L1 — a is a Strong Drop and bench surplus" in text


# --------------------------------------------------------------------------
# drop protection
# --------------------------------------------------------------------------


def _watchlist(*items):
    from sleeper_tool.watchlist import WatchItem, Watchlist

    made = {}
    for league_id, pid, state in items:
        made[f"{league_id}{pid}"] = WatchItem(
            item_id=f"{league_id}{pid}", league_id=league_id, league_name="L", kind="k", player_id=pid, player_name=pid,
            reason="r", first_seen="2026-09-01", last_seen="2026-09-01", trigger_state=state,
        )
    return Watchlist(items=made)


def test_drop_protection_counts_every_reason_and_the_droppable_remainder(monkeypatch):
    from sleeper_tool.watchlist import RESOLVED, STILL_WATCHING

    monkeypatch.setattr(cal.ra, "untouchable_ids", lambda roster, currency, n: {"u"})
    names = ("starter", "surplus", "dev", "watched", "scarce", "u", "piece", "free1", "free2")
    starter, surplus, dev, watched, scarce, untouchable, piece, free1, free2 = (
        _entry(p, "QB" if p == "scarce" else "WR") for p in names
    )
    dev = dataclasses.replace(dev, years_exp=0)
    market = ReplacementMarket(positions={"QB": PositionMarket("QB", None, None, None, None, VERY_SCARCE, None)}, players={})
    entries = [starter, surplus, dev, watched, scarce, untouchable, piece, free1, free2]
    ld = _ld(
        entries=entries, lineup=_lineup("starter"), lineup_leverage=_leverage(_lineup("starter"), [surplus]),
        replacement=market, proposals=[_proposal(give=[piece])],
    )
    watchlist = _watchlist(("1", "watched", STILL_WATCHING), ("1", "free1", RESOLVED), ("2", "free2", STILL_WATCHING))
    [d] = cal.drop_protection(_report([ld], watchlist=watchlist))
    # Only the four rules the drop path enforces make a player undroppable;
    # the watchlist, scarcity and trade-untouchable rules are about other
    # decisions and are counted separately, or the monitor reports an
    # immunity the engine does not actually have.
    assert (d.rostered, d.protected, d.droppable, d.advisory_only) == (9, 4, 5, 3)
    assert d.by_reason == {
        cal.PROTECT_STARTER: 1, cal.PROTECT_SURPLUS: 1, cal.PROTECT_DEVELOPMENTAL: 1, cal.PROTECT_WATCHLIST: 1,
        cal.PROTECT_VERY_SCARCE: 1, cal.PROTECT_UNTOUCHABLE: 1, cal.PROTECT_TRADE_PIECE: 1,
    }
    # A resolved thesis and another league's thesis protect nobody either way.
    assert d.droppable_names == ["watched", "scarce", "u", "free1", "free2"]
    assert d.flagged is False


def test_drop_protection_flags_a_roster_below_min_droppable(monkeypatch):
    monkeypatch.setattr(cal.ra, "untouchable_ids", lambda roster, currency, n: set())
    assert cal.MIN_DROPPABLE == 2
    a, b, c = _entry("a"), _entry("b"), _entry("c")
    flagged = _ld(name="tight", entries=[a, b, c], lineup=_lineup("a", "b"))
    fine = _ld(name="fine", entries=[a, b, c], lineup=_lineup("a"))
    empty = _ld(name="empty")
    result = cal.drop_protection(_report([flagged, fine, empty]))
    assert [(d.league, d.droppable, d.flagged) for d in result] == [("tight", 1, True), ("fine", 2, False)]
    text = render_calibration_markdown(calibrate(_report([flagged, fine])))
    assert "## Drop protection" in text
    assert "| tight | 3 | 2 | 1 |" in text and "**roster with nobody droppable**" in text
    assert "- tight: droppable = c" in text


def test_drop_protection_survives_a_roster_the_untouchable_helper_cannot_value(monkeypatch):
    def boom(roster, currency, n):
        raise ValueError("no values")

    monkeypatch.setattr(cal.ra, "untouchable_ids", boom)
    [d] = cal.drop_protection(_report([_ld(entries=[_entry("a"), _entry("b"), _entry("c")])]))
    assert d.by_reason[cal.PROTECT_UNTOUCHABLE] == 0 and d.droppable == 3


# --------------------------------------------------------------------------
# dependency map
# --------------------------------------------------------------------------


def test_dependency_map_lists_every_tracked_fact_and_follows_rule_inputs():
    specs = [
        RuleSpec("m1", "R1", (("K", 1),), lambda r: [], inputs=(cal.FACT_SCARCITY,)),
        RuleSpec("m1", "R2", (("K", 1),), lambda r: [], inputs=(cal.FACT_SCARCITY, cal.FACT_MOVE_DELTA)),
        RuleSpec("m2", "R3", (("K", 1),), lambda r: [], inputs=(cal.FACT_SCARCITY,)),
        RuleSpec("m3", "R4", (("K", 1),), lambda r: [], inputs=("something else",)),
    ]
    entries = {e.fact: e for e in cal.dependency_map(specs)}
    assert list(entries) == [*cal.TRACKED_FACTS, "something else"]
    scarcity = entries[cal.FACT_SCARCITY]
    assert scarcity.rules == ["m1.R1", "m1.R2", "m2.R3"]
    assert scarcity.modules == ["m1", "m2"]
    assert scarcity.votes == 2 + len(cal.MODULE_CONSUMERS[cal.FACT_SCARCITY])
    assert entries[cal.FACT_ROLE].rules == [] and entries[cal.FACT_ROLE].modules == []
    assert entries["something else"].other_consumers == ()


def test_real_inventory_declares_the_known_scarcity_and_trending_consumers():
    entries = {e.fact: e for e in cal.dependency_map(cal.build_rules())}
    scarcity_modules = entries[cal.FACT_SCARCITY].modules
    for module in ("trade_opportunity_cost", "stash_board", "buyer_board", "recommendation_conflicts", "contender_insurance"):
        assert module in scarcity_modules, module
    assert "waiver_engine" in entries[cal.FACT_TRENDING].modules and "roster_clog" in entries[cal.FACT_TRENDING].modules
    assert "move_impact" in entries[cal.FACT_MOVE_DELTA].modules
    assert "role_trends" in entries[cal.FACT_ROLE].modules
    # Every rule's inputs name a tracked fact, so a typo can't silently vanish from the map.
    for spec in cal.build_rules():
        assert set(spec.inputs) <= set(cal.TRACKED_FACTS), spec.name


def test_dependency_map_renders_votes_per_fact():
    text = render_calibration_markdown(calibrate(_report([])))
    assert "## Dependency map" in text
    for fact in cal.TRACKED_FACTS:
        assert f"### {fact} — " in text
    assert "- also consumed by: faab_strategy" in text


# --------------------------------------------------------------------------
# role rules (the role fact now has rules of its own)
# --------------------------------------------------------------------------


def test_role_trend_labels_are_bucketed_and_role_vs_market_reads_the_fact():
    from sleeper_tool.role_trends import CONFIRM, RISING, ROLE_AHEAD, STABLE, RoleTrend

    trends = {f"p{i}": RoleTrend(gsis_id=f"g{i}", label=RISING if i < 3 else STABLE, games=4) for i in range(12)}
    market = {"p0": ROLE_AHEAD, "p1": CONFIRM}
    report = _report([_ld(role_trends=trends, role_market=market)])
    r = _only(report, f"Role: {RISING}")
    assert (r.eligible, r.triggered, r.diagnostic) == (12, 3, HEALTHY)
    assert r.subjects == {"L1|player:p0", "L1|player:p1", "L1|player:p2"}
    m = _only(report, f"Role vs market: {ROLE_AHEAD}")
    assert (m.eligible, m.triggered, m.inputs) == (2, 1, (cal.FACT_ROLE,))
