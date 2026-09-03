"""Action priority: the lexicographic categorical ordering that decides
what to do first across every league and every kind of move."""
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.action_priority import (
    ALERT,
    DEFENSIVE_ADD,
    DIMENSIONS,
    DROP,
    HIGH_FAAB_PCT,
    HIGH_IRREVERSIBLE,
    IMMEDIATE,
    LIKELY_TO_DISAPPEAR,
    LONG_HORIZON,
    LOW_REVERSIBLE,
    MAJOR,
    MAJOR_WEEKLY_POINTS,
    MARGINAL,
    MEANINGFUL,
    MEANINGFUL_WEEKLY_POINTS,
    MIXED,
    MODERATE,
    MONITOR,
    MULTIPLE_AGREE,
    NEUTRAL,
    POOR,
    SINGLE,
    STASH,
    STREAMER,
    STRONG,
    THIS_WEEK,
    TIME_SENSITIVE,
    Action,
    PriorityKey,
    classify,
    explain_order,
    priority_line,
    rank_actions,
)
from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.move_impact import MoveImpact, RosterSnapshot
from sleeper_tool.opponent_blocker import DefensiveAdd
from sleeper_tool.playoff_leverage import BUBBLE, PlayoffLeverage
from sleeper_tool.recommendation_conflicts import TRADE, WAIVER, Conflict
from sleeper_tool.recommendation_provenance import FOR, MARKET, Provenance, Reason, ROSTER
from sleeper_tool.report_data import LeagueReportData, WeeklyReportData
from sleeper_tool.stash_board import PRIORITY_STASH, StashCandidate
from sleeper_tool.streamer_planner import ADD, StreamOption, StreamPlan, WeekLine
from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD, TeamStatusResult
from sleeper_tool.trade_engine import DropCandidate, TradeProposal
from sleeper_tool.trade_opportunity_cost import MAJOR_LINEUP_COST, MOSTLY_NEUTRAL, ROUGHLY_EVEN, TradeEconomics
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget


def _p(pid, pos="WR", proj=200.0, *, age=25.0):
    return make_entry(player_id=pid, name=pid, position=pos, age=age, value=make_value(name=pid, position=pos, proj_points=proj))


def _proposal(give=(), receive=(), *, receive_picks=()):
    return TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="Rival",
        give=list(give), receive=list(receive), my_value_total=1000, their_value_total=1000,
        rationale_for_me=[], rationale_for_them=[], caveats=[], receive_picks=list(receive_picks),
        acceptance_rating="High", confidence="High",
    )


def _impact(delta):
    before = RosterSnapshot(lineup=None, weekly_points=100.0, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    after = RosterSnapshot(lineup=None, weekly_points=100.0 + delta, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    return MoveImpact("x", before, after)


def _target(pid="w1", *, tier="Must Add", reason="fills a need", drop=None, trend=5, faab=None):
    return WaiverTarget(
        player_id=pid, name=pid.upper(), position="WR", team="KC", trend_count=trend, value=make_value(),
        fills_need=True, need_rank=0, reason=reason, priority_tier=tier, drop_candidate=drop, suggested_faab_pct=faab,
    )


def _ld(entries=None, *, name="L", status=None, **kw):
    entries = entries if entries is not None else [_p("qb", "QB", 340), _p("rb", "RB")]
    roster = make_roster(entries=entries, fmt=make_format(roster_positions=("QB", "RB", "BN")), league=make_league_info(kind="dynasty"))
    defaults = dict(league=make_league_info(name=name), drafted=True, roster=roster, currency="dynasty", lineup=optimize_lineup(roster))
    if status is not None:
        defaults["team_status"] = TeamStatusResult(status=status, strength_percentile=50.0, win_pct=None, games_played=0, reason="r")
    defaults.update(kw)
    return LeagueReportData(**defaults)


def _report(current_week=5):
    return WeeklyReportData(generated_at=None, current_week=current_week, source_freshness={}, ff_status="ok", leagues=[])


def _deadline(label=BUBBLE, week=11, window=True):
    return PlayoffLeverage(
        label=label, wins=3, losses=3, ties=0, games_remaining=6, seed=6, playoff_teams=6, cut_wins=3,
        deadline_window=window, trade_deadline_week=week, reason="3-3",
    )


def _provenance(fors=0, againsts=0):
    prov = Provenance(kind=TRADE, key="0", subject="s")
    prov.reasons_for = [Reason(ROSTER, FOR, f"for {i}", "m") for i in range(fors)]
    prov.reasons_against = [Reason(MARKET, "AGAINST", f"against {i}", "m") for i in range(againsts)]
    return prov


# -- urgency -----------------------------------------------------------------
def test_urgency_rules_at_their_boundaries():
    alert_high = TimeSensitiveNote("qb", "out for the season", severity="high")
    alert_low = TimeSensitiveNote("rb", "questionable", severity="medium")
    ld = _ld(time_sensitive=[alert_high, alert_low])
    assert classify(ALERT, ld, _report(), key="qb").urgency == IMMEDIATE
    assert classify(ALERT, ld, _report(), key="rb").urgency == THIS_WEEK

    ld = _ld(waiver_targets=[_target("must"), _target("strong", tier="Strong Add"), _target("mod", tier="Moderate")])
    assert classify(WAIVER, ld, _report(), key="must").urgency == IMMEDIATE
    # A Strong Add with no measured gain is Marginal, and a Marginal claim is not this week's business.
    assert classify(WAIVER, ld, _report(), key="strong").urgency == MONITOR
    assert classify(WAIVER, ld, _report(), key="mod").urgency == MONITOR

    ordinary = _ld(proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None])
    urgent = _ld(proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None], playoff=_deadline())
    assert classify(TRADE, ordinary, _report(), key="0").urgency == MONITOR
    assert classify(TRADE, urgent, _report(), key="0").urgency == THIS_WEEK

    stash = StashCandidate(entry=_p("rookie", "RB", age=22.0), label=PRIORITY_STASH, percentile=72.0, reasons=[])
    assert classify(STASH, _ld(stash=[stash]), _report(), key="rookie").urgency == LONG_HORIZON


def test_a_next_week_bye_hole_fill_is_immediate_even_below_must_add():
    from sleeper_tool.bye_collision import ByeCollision, ByeHole

    wr = _p("wr", "WR")  # the target is a WR: cover is by position, not by the sentence the annotator wrote
    target = _target("cover", tier="Moderate", reason="depth")
    bye = ByeCollision(
        week=6, holes=[ByeHole(week=6, slot="WR", normal_starter=wr, normal_projection=10.0, replacement=None, replacement_projection=0.0)],
        starters_on_bye=[wr], weeks_scanned=[5, 6],
    )
    ld = _ld(waiver_targets=[target], bye_collision=bye)
    assert classify(WAIVER, ld, _report(current_week=5), key="cover").urgency == IMMEDIATE
    # The same target with the hole two weeks out drops back to its tier.
    bye.week = 7
    assert classify(WAIVER, ld, _report(current_week=5), key="cover").urgency == MONITOR


# -- materiality -------------------------------------------------------------
def test_materiality_sits_exactly_on_its_named_cutoffs():
    ld = _ld(
        proposals=[_proposal(receive=[_p("a")]), _proposal(receive=[_p("b")]), _proposal(receive=[_p("c")])],
        trade_economics=[None, None, None],
        trade_impacts=[_impact(MAJOR_WEEKLY_POINTS), _impact(MEANINGFUL_WEEKLY_POINTS), _impact(MEANINGFUL_WEEKLY_POINTS - 0.1)],
    )
    assert [classify(TRADE, ld, _report(), key=str(i)).materiality for i in range(3)] == [MAJOR, MEANINGFUL, MARGINAL]
    # A loss is not a gain: materiality is what a move ADDS; the cost rides on the Risk reason.
    losing = _ld(proposals=[_proposal(give=[_p("a")])], trade_economics=[None], trade_impacts=[_impact(-MAJOR_WEEKLY_POINTS)])
    assert classify(TRADE, losing, _report(), key="0").materiality == MARGINAL


def test_the_materiality_cutoffs_are_seven_and_two_points_a_week():
    """Pinned by value and at literal point deltas, including the step
    below Major — the case the existing cutoff test leaves untested."""
    assert MAJOR_WEEKLY_POINTS == 7.0 and MEANINGFUL_WEEKLY_POINTS == 2.0
    deltas = [7.0, 6.9, 2.0, 1.9]
    ld = _ld(
        proposals=[_proposal(receive=[_p(f"p{i}")]) for i in range(len(deltas))],
        trade_economics=[None] * len(deltas),
        trade_impacts=[_impact(d) for d in deltas],
    )
    got = [classify(TRADE, ld, _report(), key=str(i)).materiality for i in range(len(deltas))]
    assert got == [MAJOR, MEANINGFUL, MEANINGFUL, MARGINAL]


def test_a_claim_stops_being_cheap_at_twenty_percent_of_the_budget():
    """HIGH_FAAB_PCT pinned by value and at literal percentages."""
    assert HIGH_FAAB_PCT == 20
    bench = _p("spare", "WR", 10.0)
    ld = _ld(
        [_p("qb", "QB", 340), _p("rb", "RB"), bench],
        waiver_targets=[_target(f"f{pct}", drop=bench, faab=pct) for pct in (19, 20, 21)],
    )
    assert classify(WAIVER, ld, _report(), key="f19").cost == LOW_REVERSIBLE
    assert classify(WAIVER, ld, _report(), key="f20").cost == MODERATE
    assert classify(WAIVER, ld, _report(), key="f21").cost == MODERATE


def test_materiality_without_a_preview_falls_back_to_the_tier_or_the_economics():
    ld = _ld(
        proposals=[_proposal(receive=[_p("a")])],
        trade_economics=[TradeEconomics(ROUGHLY_EVEN, MAJOR_LINEUP_COST, None, False)], trade_impacts=[None],
        waiver_targets=[_target("must"), _target("mod", tier="Moderate")],
    )
    assert classify(TRADE, ld, _report(), key="0").materiality == MARGINAL  # a Major Lineup Cost is a cost, not a gain
    assert classify(WAIVER, ld, _report(), key="must").materiality == MEANINGFUL
    assert classify(WAIVER, ld, _report(), key="mod").materiality == MARGINAL


def test_a_defensive_block_is_measured_on_the_opponents_gain():
    def add(gain):
        return DefensiveAdd(target=_p("blocker", "TE"), opponent_name="Rival", opponent_gain=gain,
                            hole="an unfilled TE slot this week", drop=None, my_gain=0.0, week=5)

    assert classify(DEFENSIVE_ADD, _ld(defensive_add=add(8.0)), _report(), key="blocker").materiality == MAJOR
    assert classify(DEFENSIVE_ADD, _ld(defensive_add=add(4.0)), _report(), key="blocker").materiality == MEANINGFUL


def test_a_streamer_is_measured_per_week_over_the_window():
    def option(name, total):
        return StreamOption(entry=_p(name, "TE"), rostered=name == "held", weeks=[WeekLine(5, total / 3)] * 3, total=total)

    plan = StreamPlan(position="TE", weeks=[5, 6, 7], current=option("held", 12.0), single=option("free", 33.0),
                      sequence=None, recommendation=ADD, note="n", candidates=[])
    ld = _ld(streamers=[plan])
    assert classify(STREAMER, ld, _report(), key="TE").materiality == MAJOR
    plan.single = option("free", 21.0)  # +3.0/wk
    assert classify(STREAMER, ld, _report(), key="TE").materiality == MEANINGFUL


# -- perishability, strategic fit, evidence, cost ----------------------------
def test_perishability_by_kind():
    ld = _ld(
        waiver_targets=[_target("trending", tier="Strong Add", trend=9), _target("quiet", tier="Moderate", trend=0), _target("mod_trend", tier="Moderate", trend=9)],
        proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None],
    )
    assert classify(WAIVER, ld, _report(), key="trending").perishability == LIKELY_TO_DISAPPEAR
    # Every trending row has a count; only a paid tier that is also trending is gone by Wednesday.
    assert classify(WAIVER, ld, _report(), key="mod_trend").perishability == TIME_SENSITIVE
    assert classify(WAIVER, ld, _report(), key="quiet").perishability == TIME_SENSITIVE
    assert classify(TRADE, ld, _report(), key="0").perishability == "Durable"
    urgent = _ld(proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None], playoff=_deadline())
    assert classify(TRADE, urgent, _report(), key="0").perishability == TIME_SENSITIVE


def test_strategic_fit_uses_the_position_specific_age_thresholds():
    veteran_rb = _p("vet_rb", "RB", age=28.0)  # RB veteran_min_age is 27
    young_rb = _p("young_rb", "RB", age=22.0)
    qb_at_28 = _p("qb28", "QB", age=28.0)  # neither young (26) nor veteran (32) for a QB

    def fit(status, piece):
        ld = _ld(status=status, proposals=[_proposal(receive=[piece])], trade_economics=[None], trade_impacts=[None])
        return classify(TRADE, ld, _report(), key="0").strategic_fit

    assert fit(CONTENDER, veteran_rb) == STRONG
    assert fit(CONTENDER, young_rb) == POOR
    assert fit(REBUILD, young_rb) == STRONG
    assert fit(REBUILD, veteran_rb) == POOR
    assert fit(CONTENDER, qb_at_28) == NEUTRAL
    assert fit(MIDDLING, veteran_rb) == NEUTRAL


def test_incoming_picks_count_as_a_rebuild_fit():
    ld = _ld(status=REBUILD, proposals=[_proposal(give=[_p("vet", "RB", age=29.0)], receive_picks=[object()])],
             trade_economics=[None], trade_impacts=[None])
    assert classify(TRADE, ld, _report(), key="0").strategic_fit == STRONG


def test_evidence_counts_provenance_reasons_and_a_conflict_is_always_mixed():
    ld = _ld(proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None])
    # Agreement is between sources: two reasons from one module are one voice.
    from sleeper_tool.recommendation_provenance import FOR, Provenance, Reason
    two_sources = Provenance(kind="trade", key="0", subject="s", reasons_for=[Reason("Roster", FOR, "a", "move_impact"), Reason("Market", FOR, "b", "market_velocity")])
    assert classify(TRADE, ld, _report(), key="0", provenance=two_sources).evidence == MULTIPLE_AGREE
    one_source = Provenance(kind="trade", key="0", subject="s", reasons_for=[Reason("Roster", FOR, "a", "trade_engine"), Reason("Roster", FOR, "b", "trade_engine")])
    assert classify(TRADE, ld, _report(), key="0", provenance=one_source).evidence == SINGLE
    assert classify(TRADE, ld, _report(), key="0", provenance=_provenance(fors=1)).evidence == SINGLE
    assert classify(TRADE, ld, _report(), key="0", provenance=_provenance(fors=3, againsts=1)).evidence == MIXED
    assert classify(TRADE, ld, _report(), key="0").evidence == SINGLE
    ld.conflicts = [Conflict(TRADE, "0", "s", [], ["it costs the lineup"])]
    assert classify(TRADE, ld, _report(), key="0", provenance=_provenance(fors=3)).evidence == MIXED


def test_cost_is_low_until_it_costs_a_starter_the_budget_or_a_trade():
    starter = _p("qb", "QB", 340)
    bench = _p("spare", "WR", 10.0)
    ld = _ld(
        [starter, _p("rb", "RB"), bench],
        waiver_targets=[
            _target("free"), _target("cheap", drop=bench, faab=HIGH_FAAB_PCT - 1),
            _target("pricey", drop=bench, faab=HIGH_FAAB_PCT), _target("costly", drop=starter),
        ],
        proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None],
        drop_candidates=[DropCandidate(entry=bench, priority="Strong Drop", reasons=["buried"])],
    )
    assert classify(WAIVER, ld, _report(), key="free").cost == LOW_REVERSIBLE
    assert classify(WAIVER, ld, _report(), key="cheap").cost == LOW_REVERSIBLE
    assert classify(WAIVER, ld, _report(), key="pricey").cost == MODERATE
    assert classify(WAIVER, ld, _report(), key="costly").cost == MODERATE
    assert classify(TRADE, ld, _report(), key="0").cost == HIGH_IRREVERSIBLE
    assert classify(DROP, ld, _report(), key="spare").cost == MODERATE


def test_a_major_lineup_cost_makes_even_a_non_trade_high():
    ld = _ld(proposals=[_proposal(give=[_p("qb", "QB", 340)])],
             trade_economics=[TradeEconomics(ROUGHLY_EVEN, MAJOR_LINEUP_COST, -9.0, False)], trade_impacts=[_impact(-9.0)])
    key = classify(TRADE, ld, _report(), key="0")
    assert key.cost == HIGH_IRREVERSIBLE and key.materiality == MARGINAL  # the cost is on Cost and Risk, never Materiality


# -- the key itself ----------------------------------------------------------
def test_sort_key_is_lexicographic_over_the_six_ranks():
    key = PriorityKey(IMMEDIATE, MAJOR, LIKELY_TO_DISAPPEAR, STRONG, MULTIPLE_AGREE, LOW_REVERSIBLE)
    assert key.sort_key() == (0, 0, 0, 0, 0, 0)
    worst = PriorityKey(LONG_HORIZON, MARGINAL, "Durable", POOR, SINGLE, HIGH_IRREVERSIBLE)
    assert worst.sort_key() == (3, 2, 2, 2, 2, 2)
    assert priority_line(key) == "Immediate · Major · Likely to disappear"
    assert key.describe().startswith("Urgency Immediate · Materiality Major")
    assert DIMENSIONS == ("urgency", "materiality", "perishability", "strategic_fit", "evidence", "cost")


# -- ordering ----------------------------------------------------------------
def _action(kind, key, ld, priority_key, headline, detail="d"):
    return Action(kind, key, ld, priority_key, headline, detail)


def test_a_speculative_stash_never_outranks_an_urgent_starter_replacement():
    ld = _ld(name="Dynasty")
    stash = classify(STASH, _ld(stash=[StashCandidate(entry=_p("rookie", "RB", age=22.0), label=PRIORITY_STASH, percentile=72.0, reasons=[])]), _report(), key="rookie")
    starter_ld = _ld(waiver_targets=[_target("must")], waiver_impacts={"must": _impact(6.0)})
    replacement = classify(WAIVER, starter_ld, _report(), key="must")
    ordered = rank_actions([
        _action(STASH, "rookie", ld, stash, "Priority Stash: rookie"),
        _action(WAIVER, "must", starter_ld, replacement, "Add MUST"),
    ])
    assert [a.kind for a in ordered] == [WAIVER, STASH]
    assert explain_order(ordered[0], ordered[1]).startswith("urgency: Immediate before Long Horizon")


def test_a_durable_trade_ranks_below_an_expiring_must_add_of_similar_materiality():
    trade_ld = _ld(name="A", proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[_impact(8.0)])
    waiver_ld = _ld(name="B", waiver_targets=[_target("must")], waiver_impacts={"must": _impact(8.0)})
    trade = classify(TRADE, trade_ld, _report(), key="0")
    waiver = classify(WAIVER, waiver_ld, _report(), key="must")
    assert trade.materiality == waiver.materiality == MAJOR
    ordered = rank_actions([
        _action(TRADE, "0", trade_ld, trade, "Send x for in"),
        _action(WAIVER, "must", waiver_ld, waiver, "Add MUST"),
    ])
    assert [a.kind for a in ordered] == [WAIVER, TRADE]
    assert explain_order(*ordered).startswith("urgency:")


def test_a_low_value_block_ranks_below_a_major_lineup_improvement_at_the_same_urgency():
    block_ld = _ld(name="A", defensive_add=DefensiveAdd(
        target=_p("blocker", "TE"), opponent_name="Rival", opponent_gain=4.0, hole="an unfilled TE slot this week",
        drop=None, my_gain=0.2, week=5,
    ))
    trade_ld = _ld(name="B", proposals=[_proposal(receive=[_p("in")])], playoff=_deadline(),
                   trade_economics=[None], trade_impacts=[_impact(9.0)])
    block = classify(DEFENSIVE_ADD, block_ld, _report(), key="blocker")
    trade = classify(TRADE, trade_ld, _report(), key="0")
    assert block.urgency == trade.urgency == THIS_WEEK
    ordered = rank_actions([
        _action(DEFENSIVE_ADD, "blocker", block_ld, block, "Defensive add: blocker"),
        _action(TRADE, "0", trade_ld, trade, "Send x for in"),
    ])
    assert [a.kind for a in ordered] == [TRADE, DEFENSIVE_ADD]
    assert explain_order(*ordered) == "materiality: Major before Meaningful"


def test_ties_fall_through_to_kind_then_league_then_headline():
    key = PriorityKey(THIS_WEEK, MEANINGFUL, TIME_SENSITIVE, NEUTRAL, SINGLE, LOW_REVERSIBLE)
    a = _action(ALERT, "1", _ld(name="Zeta"), key, "z alert")
    b = _action(WAIVER, "2", _ld(name="Alpha"), key, "a waiver")
    c = _action(WAIVER, "3", _ld(name="Alpha"), key, "b waiver")
    d = _action(WAIVER, "4", _ld(name="Beta"), key, "a waiver")
    ordered = rank_actions([d, c, b, a])
    assert [x.headline for x in ordered] == ["z alert", "a waiver", "b waiver", "a waiver"]
    assert [x.ld.league.name for x in ordered] == ["Zeta", "Alpha", "Alpha", "Beta"]
    assert explain_order(a, b) == "kind: alert before waiver"
    assert explain_order(b, d) == "league: Alpha before Beta"
    assert explain_order(b, c) == "headline: a waiver before b waiver"
    assert explain_order(b, b) is None
    assert rank_actions([("waiver", "2", b.ld, key, "a waiver", "d")])[0] == b


def test_explain_order_names_the_first_dimension_that_differs():
    base = PriorityKey(THIS_WEEK, MEANINGFUL, TIME_SENSITIVE, NEUTRAL, SINGLE, LOW_REVERSIBLE)
    assert explain_order(base, PriorityKey(IMMEDIATE, MARGINAL, "Durable", POOR, SINGLE, HIGH_IRREVERSIBLE)) == "urgency: Immediate before This Week"
    assert explain_order(base, PriorityKey(THIS_WEEK, MAJOR, "Durable", POOR, SINGLE, HIGH_IRREVERSIBLE)) == "materiality: Major before Meaningful"
    assert explain_order(base, PriorityKey(THIS_WEEK, MEANINGFUL, LIKELY_TO_DISAPPEAR, POOR, SINGLE, HIGH_IRREVERSIBLE)).startswith("perishability:")
    assert explain_order(base, PriorityKey(THIS_WEEK, MEANINGFUL, TIME_SENSITIVE, STRONG, SINGLE, HIGH_IRREVERSIBLE)).startswith("strategic_fit:")
    assert explain_order(base, PriorityKey(THIS_WEEK, MEANINGFUL, TIME_SENSITIVE, NEUTRAL, MIXED, HIGH_IRREVERSIBLE)).startswith("evidence:")
    assert explain_order(base, PriorityKey(THIS_WEEK, MEANINGFUL, TIME_SENSITIVE, NEUTRAL, SINGLE, MODERATE)) == "cost: Low / reversible before Moderate"
    assert explain_order(base, base) is None


def test_an_unknown_key_classifies_without_a_subject_rather_than_raising():
    ld = _ld()
    key = classify(TRADE, ld, _report(), key="7")
    assert key.materiality == MARGINAL and key.urgency == MONITOR
    assert classify(WAIVER, ld, _report(), key=None).urgency == MONITOR
    assert classify(STREAMER, None, None, key="TE").urgency == MONITOR  # no measured gain: not this week's business
