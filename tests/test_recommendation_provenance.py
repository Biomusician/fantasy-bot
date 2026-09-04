"""Recommendation provenance: the ranked For/Against/Context ledger built
from evidence objects the report layer already produced."""
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.move_impact import MoveImpact, RosterSnapshot
from sleeper_tool.opponent_blocker import DefensiveAdd
from sleeper_tool.playoff_leverage import BUBBLE, PlayoffLeverage
from sleeper_tool.portfolio_exposure import VERY_HIGH, PortfolioExposure
from sleeper_tool.recommendation_conflicts import TRADE, WAIVER, Conflict
from sleeper_tool.recommendation_provenance import (
    AGAINST,
    ALERT,
    CONTEXT,
    DEFENSIVE_ADD,
    DROP,
    FOR,
    LEAGUE_ECONOMY,
    MARKET,
    MAX_AGAINST,
    MAX_CONTEXT,
    MAX_FOR,
    OPPONENT,
    PORTFOLIO,
    PROJECTION,
    REPLACEMENT_MARKET,
    RISK,
    ROLE,
    ROSTER,
    SCHEDULE,
    STASH,
    STREAMER,
    TIMING,
    Provenance,
    Reason,
    build_provenance,
    conflict_reasons,
    select,
)
from sleeper_tool.replacement_value import PositionMarket, ReplacementMarket
from sleeper_tool.report_data import LeagueReportData, WeeklyReportData
from sleeper_tool.stash_board import PRIORITY_STASH, StashCandidate
from sleeper_tool.trade_engine import DropCandidate, TradeProposal
from sleeper_tool.trade_opportunity_cost import (
    COSTS_LINEUP,
    FAVORABLE,
    IMPROVES_LINEUP,
    MAJOR_LINEUP_COST,
    MOSTLY_NEUTRAL,
    ROUGHLY_EVEN,
    TradeEconomics,
)
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget


def _p(pid, pos="WR", proj=200.0, *, age=25.0):
    return make_entry(player_id=pid, name=pid, position=pos, age=age, value=make_value(name=pid, position=pos, proj_points=proj))


def _proposal(give=(), receive=(), *, for_me=(), caveats=(), for_them=(), trade_type="buy_low", username="rival"):
    return TradeProposal(
        league_name="L", currency="dynasty", target_username=username, target_team_name="Rival",
        give=list(give), receive=list(receive), my_value_total=1000, their_value_total=1200,
        rationale_for_me=list(for_me), rationale_for_them=list(for_them), caveats=list(caveats),
        trade_type=trade_type, acceptance_rating="High", confidence="High",
    )


def _impact(delta, deltas_from=100.0):
    before = RosterSnapshot(lineup=None, weekly_points=deltas_from, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    after = RosterSnapshot(lineup=None, weekly_points=deltas_from + delta, depth_needs=[], status=None, strength_percentile=None, roster_value=0, avg_starter_age=None)
    return MoveImpact("x", before, after)


def _target(pid="w1", *, tier="Must Add", reason="fills your worst need at WR", notes=(), drop=None, trend=5, faab=None):
    return WaiverTarget(
        player_id=pid, name=pid.upper(), position="WR", team="KC", trend_count=trend, value=make_value(),
        fills_need=True, need_rank=0, reason=reason, priority_tier=tier, drop_candidate=drop,
        suggested_faab_pct=faab, notes=list(notes),
    )


def _ld(entries=None, **kw):
    entries = entries if entries is not None else [_p("qb", "QB", 340), _p("rb", "RB")]
    roster = make_roster(entries=entries, fmt=make_format(roster_positions=("QB", "RB", "BN")), league=make_league_info(kind="dynasty"))
    defaults = dict(league=make_league_info(name="L"), drafted=True, roster=roster, currency="dynasty", lineup=optimize_lineup(roster))
    defaults.update(kw)
    return LeagueReportData(**defaults)


def _report(portfolio=None, current_week=5):
    return WeeklyReportData(
        generated_at=None, current_week=current_week, source_freshness={}, ff_status="ok", leagues=[], portfolio=portfolio
    )


def _texts(reasons):
    return [r.text for r in reasons]


def _categories(reasons):
    return [r.category for r in reasons]


# -- caps and ordering -------------------------------------------------------
def test_caps_are_enforced_on_every_direction():
    p = _proposal(
        receive=[_p("in", "WR")],
        for_me=[f"Buy low on piece {i}" for i in range(6)],
        caveats=[f"He is risky in way {i}" for i in range(6)],
        for_them=[f"They want him because {i}" for i in range(6)],
    )
    ld = _ld(proposals=[p], trade_economics=[TradeEconomics(ROUGHLY_EVEN, MOSTLY_NEUTRAL, 0.0, False)], trade_impacts=[None])
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    assert len(prov.reasons_for) == MAX_FOR
    assert len(prov.reasons_against) == MAX_AGAINST
    assert len(prov.context) == MAX_CONTEXT
    assert len(prov.describe()) == MAX_FOR + MAX_AGAINST + MAX_CONTEXT


def test_category_priority_orders_the_for_reasons_and_drops_the_lowest():
    """Roster (a measured lineup gain) outranks Replacement Market, which
    outranks Market, which outranks Projection — so the Projection reason
    is the one the cap discards."""
    p = _proposal(
        receive=[_p("in", "WR")],
        for_me=[
            "Sources on in: Projection Above Market (WR10 market vs WR40 projection) — favours buying.",
            "Replacement context: in arrives +6.0/wk over the best free-agent WR (Scarce market).",
        ],
    )
    ld = _ld(proposals=[p], trade_economics=[TradeEconomics(FAVORABLE, IMPROVES_LINEUP, 5.0, False)], trade_impacts=[_impact(5.0)])
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    assert _categories(prov.reasons_for) == [ROSTER, REPLACEMENT_MARKET, MARKET]
    assert prov.reasons_for[0].text.startswith("Move Impact:")
    assert prov.reasons_for[0].source == "move_impact"
    assert all(r.direction == FOR for r in prov.reasons_for)


def test_select_is_a_pure_categorical_sort_with_deterministic_tiebreaks():
    reasons = [
        Reason(MARKET, FOR, "b market", "zz_source"),
        Reason(MARKET, FOR, "a market", "zz_source"),
        Reason(ROSTER, FOR, "roster", "move_impact"),
        Reason(PROJECTION, FOR, "projection", "source_disagreement"),
    ]
    assert _texts(select(reasons, FOR)) == ["roster", "a market", "b market"]
    against = [Reason(MARKET, AGAINST, "market", "m"), Reason(RISK, AGAINST, "conflict", "recommendation_conflicts")]
    assert _texts(select(against, AGAINST)) == ["conflict", "market"]


def test_conflicts_against_reasons_come_first_and_survive_the_cap():
    p = _proposal(
        give=[_p("qb", "QB", 340)],
        caveats=["Portfolio exposure: would put him on 6 of your 8 rosters (Very High Exposure).", "He is old."],
    )
    conflict = Conflict(TRADE, "0", "s", ["value play"], [f"{MAJOR_LINEUP_COST} (-9.0/wk)", "QB replacement market is Very Scarce"])
    ld = _ld(
        proposals=[p], conflicts=[conflict],
        trade_economics=[TradeEconomics(ROUGHLY_EVEN, MAJOR_LINEUP_COST, -9.0, False)], trade_impacts=[_impact(-9.0)],
    )
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    assert _categories(prov.reasons_against) == [RISK, RISK]
    assert _texts(prov.reasons_against) == [f"{MAJOR_LINEUP_COST} (-9.0/wk)", "QB replacement market is Very Scarce"]
    assert conflict_reasons(prov)[1] == _texts(prov.reasons_against)


# -- dedupe ------------------------------------------------------------------
def test_scarcity_exposure_and_per_piece_source_facts_are_each_said_once():
    p = _proposal(
        give=[_p("a", "QB", 300), _p("b", "QB", 280)],
        receive=[_p("c", "WR"), _p("d", "WR")],
        caveats=[
            "Replacement context: a is +12.0/wk over the best free-agent QB (Very Scarce market); replacing him would cost that much.",
            "Replacement context: b is +9.0/wk over the best free-agent QB (Very Scarce market); replacing him would cost that much.",
            f"Portfolio exposure: would put him on 6 of your 8 rosters ({VERY_HIGH}).",
            f"Portfolio exposure: would put him on 6 of your 8 rosters ({VERY_HIGH}).",
        ],
        for_me=[
            "Sources on c: High Disagreement: KTC vs FantasyPros dynasty differ by 44 WR places.",
            "Sources on c: High Disagreement: KTC vs FantasyPros dynasty differ by 44 WR places — again.",
            "Sources on d: Source Disagreement: KTC vs FantasyPros dynasty differ by 25 WR places.",
        ],
    )
    ld = _ld(proposals=[p], trade_economics=[None], trade_impacts=[None])
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    against = _texts(prov.reasons_against)
    assert sum(1 for t in against if "market" in t and "Very Scarce" in t) == 1
    assert sum(1 for t in against if t.startswith("Portfolio exposure:")) == 1
    # One source-disagreement reason per named piece, not per sentence.
    sources = [r for r in prov.reasons_for if r.category == PROJECTION]
    assert sorted(t.split(":")[0] for t in _texts(sources)) == ["Sources on c", "Sources on d"]


def test_the_exposure_note_is_not_repeated_from_the_portfolio_object_when_a_caveat_already_says_it():
    receive = _p("star", "WR")
    portfolio = PortfolioExposure(total_leagues=8, players=[], counts_by_player_id={"star": 5}, qb_starts_by_player_id={})
    p = _proposal(receive=[receive], caveats=[f"Portfolio exposure: would put him on 6 of your 8 rosters ({VERY_HIGH})."])
    ld = _ld(proposals=[p], trade_economics=[None], trade_impacts=[None])
    prov = build_provenance(ld, _report(portfolio=portfolio))[(TRADE, "0")]
    assert len([r for r in prov.reasons_against if r.category == PORTFOLIO]) == 1


def test_the_portfolio_object_supplies_the_exposure_reason_when_no_annotation_did():
    receive = _p("star", "WR")
    portfolio = PortfolioExposure(total_leagues=8, players=[], counts_by_player_id={"star": 5}, qb_starts_by_player_id={})
    ld = _ld(proposals=[_proposal(receive=[receive])], trade_economics=[None], trade_impacts=[None])
    prov = build_provenance(ld, _report(portfolio=portfolio))[(TRADE, "0")]
    exposure = [r for r in prov.reasons_against if r.category == PORTFOLIO]
    assert len(exposure) == 1 and VERY_HIGH in exposure[0].text
    assert exposure[0].source == "portfolio_exposure"


# -- missing objects ---------------------------------------------------------
def test_missing_evidence_objects_yield_no_reasons_rather_than_invented_ones():
    ld = _ld(proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None])
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    assert prov.reasons_for == [] and prov.reasons_against == [] and prov.context == []
    assert prov.describe() == []
    assert conflict_reasons(prov) == ([], [])


def test_an_errored_or_undrafted_league_produces_no_cards():
    assert build_provenance(_ld(error="boom"), _report()) == {}
    assert build_provenance(_ld(drafted=False), _report()) == {}


# -- waiver cards ------------------------------------------------------------
def test_waiver_card_covers_tier_impact_scarcity_sources_velocity_schedule_and_faab():
    drop = _p("bench", "WR")
    target = _target(
        drop=drop, faab=25,
        reason="fills your worst need at WR; portfolio exposure: would put him on 6 of your 8 rosters",
        notes=[
            "WR market is Very Scarce here: an add at this position matters more than his rank alone suggests",
            "Market velocity: Rapidly Rising (+22% over 5 observations since 2026-08-28)",
            "Schedule: bye week 6 inside the next 3 (this week included)",
        ],
    )
    ld = _ld(waiver_targets=[target], waiver_impacts={"w1": _impact(4.0)})
    prov = build_provenance(ld, _report())[(WAIVER, "w1")]
    assert prov.subject == "Add W1, drop bench"
    assert _categories(prov.reasons_for) == [ROSTER, ROSTER, REPLACEMENT_MARKET]
    # Within a category the tiebreak is the source module's name, so the
    # measured Move Impact leads the tier line it shares a category with.
    assert prov.reasons_for[0].text.startswith("Move Impact: projected starter points +4.0/wk")
    assert prov.reasons_for[1].text.startswith("Must Add (Streamer): fills your worst need at WR")
    assert any(r.category == SCHEDULE for r in prov.reasons_against)
    assert any(r.category == PORTFOLIO for r in prov.reasons_against)
    context = {r.category: r.text for r in prov.context}
    assert TIMING in context and "5 adds" in context[TIMING]


def test_a_waiver_conflict_replaces_the_drop_context_and_makes_the_card_mixed():
    drop = _p("starter", "WR")
    target = _target(drop=drop)
    conflict = Conflict(WAIVER, "w1", "Add W1", ["Must Add"], ["the drop, starter, is a current optimized starter"])
    ld = _ld(waiver_targets=[target], conflicts=[conflict])
    prov = build_provenance(ld, _report())[(WAIVER, "w1")]
    assert _texts(prov.reasons_against) == ["the drop, starter, is a current optimized starter"]
    assert prov.reasons_against[0].category == RISK
    assert not any("roster spot" in r.text for r in prov.context)


def test_an_abundant_market_note_argues_against_the_add():
    target = _target(tier="Moderate", notes=["WR market is Abundant here: comparable production is usually on waivers, so don't overspend"])
    prov = build_provenance(_ld(waiver_targets=[target]), _report())[(WAIVER, "w1")]
    assert [r.category for r in prov.reasons_against] == [REPLACEMENT_MARKET]


# -- other recommendation kinds ---------------------------------------------
def test_drop_defensive_add_streamer_stash_and_alert_cards_are_all_built():
    drop_entry = _p("cut", "WR")
    candidate = DropCandidate(entry=drop_entry, priority="Strong Drop", reasons=["buried on the depth chart", "market velocity Rising (+12%) — a rising player is worth a second look before cutting"])
    add = DefensiveAdd(
        target=_p("blocker", "TE"), opponent_name="Rival", opponent_gain=6.0,
        hole="an unfilled TE slot this week", drop=_p("spare", "WR"), my_gain=0.4, week=5,
    )
    stash = StashCandidate(entry=_p("rookie", "RB", age=22.0), label=PRIORITY_STASH, percentile=72.0,
                           reasons=["72nd percentile dynasty value", "rookie, age 22"], drop=None)
    alert = TimeSensitiveNote("qb", "placed on IR — season-ending", severity="high")
    ld = _ld(drop_candidates=[candidate], defensive_add=add, stash=[stash], time_sensitive=[alert])
    cards = build_provenance(ld, _report())

    assert _texts(cards[(DROP, "cut")].reasons_for) == ["buried on the depth chart"]
    assert cards[(DROP, "cut")].reasons_against[0].category == MARKET

    blocker = cards[(DEFENSIVE_ADD, "blocker")]
    assert blocker.reasons_for[0].category == OPPONENT and "+6.0" in blocker.reasons_for[0].text
    assert any(r.category == ROSTER for r in blocker.reasons_against)  # the drop it costs

    assert cards[(STASH, "rookie")].reasons_for[0].category == MARKET
    assert cards[(STASH, "rookie")].context[0].category == TIMING

    assert cards[(ALERT, "qb")].reasons_for[0].text == "qb: placed on IR — season-ending"


def test_only_high_severity_alerts_and_priority_stashes_get_cards():
    ld = _ld(
        time_sensitive=[TimeSensitiveNote("x", "questionable", severity="medium")],
        stash=[StashCandidate(entry=_p("watch", "RB"), label="Watch", percentile=45.0, reasons=["45th percentile dynasty value"])],
    )
    assert build_provenance(ld, _report()) == {}


# -- optional role / faab fields --------------------------------------------
class _Component:
    def __init__(self, name, direction, magnitude_text):
        self.name, self.direction, self.magnitude_text = name, direction, magnitude_text


class _Trend:
    def __init__(self, label, components=(), note=""):
        self.label, self.components, self.note = label, list(components), note


class _Faab:
    def __init__(self, posture, share_of_remaining_text, suggested_dollars=12):
        self.posture, self.share_of_remaining_text, self.suggested_dollars = posture, share_of_remaining_text, suggested_dollars


def test_role_trends_and_role_market_are_used_when_the_orchestrator_supplies_them():
    incoming, outgoing = _p("in", "WR"), _p("out", "RB")
    ld = _ld(proposals=[_proposal(give=[outgoing], receive=[incoming])], trade_economics=[None], trade_impacts=[None])
    ld.role_trends = {
        "in": _Trend("Role Rising", [_Component("routes run", "up", "+8 per game")]),
        "out": _Trend("Role Collapsing", note="snap share halved"),
    }
    ld.role_market = {"in": "Role Ahead of Market", "out": "Market Ahead of Role"}
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    role_for = [r for r in prov.reasons_for if r.category == ROLE]
    # No trailing slice: a `[: len(role_for)]` would let an empty list — or
    # any prefix of the expected reasons — pass silently.
    assert _texts(role_for) == [
        "in: Role Rising — routes run up +8 per game",
        "out: Role Collapsing — snap share halved",
        "in: Role Ahead of Market",
    ]
    assert all(r.source == "role_trends" for r in role_for)
    # A falling incoming role would argue against instead.
    ld.role_trends = {"in": _Trend("Role Falling", note="down to 40% of snaps")}
    ld.role_market = {}
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    assert [r.category for r in prov.reasons_against] == [ROLE]


def test_faab_posture_replaces_the_plain_suggested_bid_when_present():
    target = _target(faab=12)
    ld = _ld(waiver_targets=[target])
    plain = build_provenance(ld, _report())[(WAIVER, "w1")]
    assert any("Suggested FAAB: 12%" in r.text for r in plain.context)
    ld.faab = {"w1": _Faab("Bid to win", "18-24% of your remaining budget")}
    posture = build_provenance(ld, _report())[(WAIVER, "w1")]
    faab_reasons = [r for r in posture.context if r.category == LEAGUE_ECONOMY]
    assert _texts(faab_reasons) == ["FAAB: Bid to win — 18-24% of your remaining budget"]
    assert faab_reasons[0].source == "faab_strategy"


def test_freshness_labels_are_attached_only_where_the_caller_supplies_them():
    p = _proposal(receive=[_p("in")], for_me=["Market velocity: in is Rising (+9%) — the market is moving toward you."])
    ld = _ld(proposals=[p], trade_economics=[None], trade_impacts=[None])
    prov = build_provenance(ld, _report(), freshness_by_source={"market_velocity": "28 days of history"})[(TRADE, "0")]
    assert prov.reasons_for[0].freshness == "28 days of history"
    assert prov.reasons_for[0].describe().endswith("[28 days of history]")
    plain = build_provenance(ld, _report())[(TRADE, "0")]
    assert plain.reasons_for[0].freshness is None


# -- timing context and determinism -----------------------------------------
def test_the_deadline_window_is_the_timing_context_for_a_trade():
    playoff = PlayoffLeverage(
        label=BUBBLE, wins=3, losses=3, ties=0, games_remaining=6, seed=6, playoff_teams=6, cut_wins=3,
        deadline_window=True, trade_deadline_week=11, reason="3-3, seed 6 of 12",
    )
    ld = _ld(proposals=[_proposal(receive=[_p("in")])], trade_economics=[None], trade_impacts=[None], playoff=playoff)
    prov = build_provenance(ld, _report())[(TRADE, "0")]
    assert prov.context[0].category == TIMING
    assert "trade deadline is week 11" in prov.context[0].text


def test_provenance_is_deterministic_across_builds():
    market = ReplacementMarket(positions={"QB": PositionMarket("QB", None, None, None, None, "Very Scarce", None)}, players={})
    p = _proposal(
        give=[_p("qb", "QB", 340)], receive=[_p("wr", "WR")],
        for_me=["Buy low on wr: value play", "Replacement context: wr arrives +5.0/wk over the best free-agent WR (Scarce market)."],
        caveats=["Schedule: wr has a bye week 6 inside the next 3 (this week included).", "He is old."],
        for_them=["This league's transaction record: Frequent Trader (4 completed trades this season)."],
    )
    ld = _ld(
        proposals=[p], replacement=market, waiver_targets=[_target()],
        trade_economics=[TradeEconomics(FAVORABLE, COSTS_LINEUP, -3.0, True)], trade_impacts=[_impact(-3.0)],
    )
    first = {k: v.describe() for k, v in build_provenance(ld, _report()).items()}
    second = {k: v.describe() for k, v in build_provenance(ld, _report()).items()}
    assert first == second
    assert isinstance(build_provenance(ld, _report())[(TRADE, "0")], Provenance)


def test_describe_lines_name_the_direction_and_the_category():
    p = _proposal(receive=[_p("in")], for_me=["Buy low on in: he is cheap right now"])
    ld = _ld(proposals=[p], trade_economics=[None], trade_impacts=[None])
    lines = build_provenance(ld, _report())[(TRADE, "0")].describe()
    assert lines == ["FOR — Market: Buy low on in: he is cheap right now"]
    assert CONTEXT == "CONTEXT" and FOR == "FOR" and AGAINST == "AGAINST"
    assert STREAMER == "streamer"


# -- waiver explanation rows: why this drop, and what would make it wrong -----


def _source_view(consensus):
    from sleeper_tool.source_disagreement import SourceView

    return SourceView(
        name="W1", position="WR", consensus=consensus, consensus_gap=30,
        consensus_pair=("KTC", "FantasyPros dynasty"), direction=None,
        market_rank=20, projection_rank=50, expert_note=None,
    )


def test_why_this_drop_uses_the_roster_clog_reason_when_there_is_one():
    from sleeper_tool.recommendation_provenance import WHY_DROP_PREFIX
    from sleeper_tool.roster_clog import RosterClog

    drop = _p("bench", "WR")
    clog = RosterClog(entry=drop, reasons=["no path to the lineup at WR", "23rd percentile within position"], composite_rank=1.0)
    prov = build_provenance(_ld(waiver_targets=[_target(drop=drop)], roster_clogs=[clog]), _report())[(WAIVER, "w1")]
    why = prov.why_drop
    assert why is not None
    assert why.text == f"{WHY_DROP_PREFIX} bench — no path to the lineup at WR; 23rd percentile within position"
    assert (why.category, why.source, why.direction) == (ROSTER, "roster_clog", CONTEXT)


def test_why_this_drop_falls_back_to_the_drop_candidate_reasons_then_to_the_engine_rule():
    from sleeper_tool.recommendation_provenance import WEAKEST_AT_POSITION, WEAKEST_BENCH_PIECE, WHY_DROP_PREFIX

    drop = _p("bench", "WR")
    candidate = DropCandidate(entry=drop, priority="Consider Dropping", reasons=["buried behind 3 better WR options"])
    with_candidate = build_provenance(_ld(waiver_targets=[_target(drop=drop)], drop_candidates=[candidate]), _report())
    assert with_candidate[(WAIVER, "w1")].why_drop.text.endswith("buried behind 3 better WR options")
    assert with_candidate[(WAIVER, "w1")].why_drop.source == "trade_engine"

    # No module judged him: the waiver engine's own two rules, said out loud.
    same_position = build_provenance(_ld(waiver_targets=[_target(drop=drop)]), _report())[(WAIVER, "w1")]
    assert same_position.why_drop.text == f"{WHY_DROP_PREFIX} bench — {WEAKEST_AT_POSITION}"
    other_position = build_provenance(_ld(waiver_targets=[_target(drop=_p("bench", "TE"))]), _report())[(WAIVER, "w1")]
    assert other_position.why_drop.text == f"{WHY_DROP_PREFIX} bench — {WEAKEST_BENCH_PIECE}"


def test_a_waiver_row_with_no_paired_drop_has_no_why_drop_row():
    prov = build_provenance(_ld(waiver_targets=[_target(drop=None)]), _report())[(WAIVER, "w1")]
    assert prov.why_drop is None


def test_the_drop_reasons_are_capped_so_the_row_stays_one_sentence():
    from sleeper_tool.recommendation_provenance import MAX_DROP_REASONS
    from sleeper_tool.roster_clog import RosterClog

    drop = _p("bench", "WR")
    clog = RosterClog(entry=drop, reasons=["one", "two", "three", "four"], composite_rank=1.0)
    prov = build_provenance(_ld(waiver_targets=[_target(drop=drop)], roster_clogs=[clog]), _report())[(WAIVER, "w1")]
    assert prov.why_drop.text.count(";") == MAX_DROP_REASONS - 1


def test_invalidation_assembles_only_the_facts_this_run_actually_has():
    from sleeper_tool.recommendation_provenance import INVALIDATION_PREFIX
    from sleeper_tool.replacement_value import ABUNDANT
    from sleeper_tool.source_disagreement import SOURCE_DISAGREEMENT
    from sleeper_tool.waiver_engine import EARLY_SEASON_CLAUSE

    target = _target(reason=f"fills your worst need at WR; 40 adds across Sleeper in the last 48h; {EARLY_SEASON_CLAUSE}", trend=40)
    market = ReplacementMarket(positions={"WR": PositionMarket("WR", None, None, None, None, ABUNDANT, None)}, players={})
    ld = _ld(waiver_targets=[target], replacement=market, source_views={"w1": _source_view(SOURCE_DISAGREEMENT)})
    prov = build_provenance(ld, _report())[(WAIVER, "w1")]
    row = prov.invalidation
    assert row is not None and row.text.startswith(INVALIDATION_PREFIX) and row.text.endswith(".")
    assert "40 adds" in row.text and "early-season sample" in row.text
    assert f"market here is {ABUNDANT}" in row.text
    assert "sources split on him" in row.text
    assert (row.category, row.direction) == (RISK, CONTEXT)


def test_invalidation_covers_a_healthy_insured_starter_and_a_thin_role_record():
    from sleeper_tool.contender_insurance import InsuranceRecommendation
    from sleeper_tool.role_trends import INSUFFICIENT, RoleTrend

    healthy = _p("starter-qb", "QB")
    row = InsuranceRecommendation(
        starter=healthy, slot="QB", starter_projection=18.0, replacement_projection=6.0,
        candidate=_p("w1", "QB"), restored_projection=12.0,
    )
    ld = _ld(
        waiver_targets=[_target()], insurance=[row],
        role_trends={"w1": RoleTrend(gsis_id="g1", label=INSUFFICIENT, games=1)},
    )
    text = build_provenance(ld, _report())[(WAIVER, "w1")].invalidation.text
    assert "starter-qb is healthy" in text
    assert INSUFFICIENT.lower() in text


def test_an_injured_insured_starter_is_not_an_invalidation():
    from sleeper_tool.contender_insurance import InsuranceRecommendation

    hurt = make_entry(player_id="starter-qb", name="starter-qb", position="QB", injury_status="IR", value=make_value(position="QB"))
    row = InsuranceRecommendation(
        starter=hurt, slot="QB", starter_projection=18.0, replacement_projection=6.0,
        candidate=_p("w1", "QB"), restored_projection=12.0,
    )
    prov = build_provenance(_ld(waiver_targets=[_target()], insurance=[row]), _report())[(WAIVER, "w1")]
    assert prov.invalidation is None


def test_no_invalidation_row_when_nothing_in_the_inputs_undercuts_the_read():
    from sleeper_tool.source_disagreement import STRONG_CONSENSUS

    ld = _ld(waiver_targets=[_target()], source_views={"w1": _source_view(STRONG_CONSENSUS)})
    assert build_provenance(ld, _report())[(WAIVER, "w1")].invalidation is None


def test_invalidation_facts_are_capped():
    from sleeper_tool.contender_insurance import InsuranceRecommendation
    from sleeper_tool.recommendation_provenance import MAX_INVALIDATION_FACTS
    from sleeper_tool.replacement_value import ABUNDANT
    from sleeper_tool.role_trends import INSUFFICIENT, RoleTrend
    from sleeper_tool.source_disagreement import HIGH_DISAGREEMENT
    from sleeper_tool.waiver_engine import EARLY_SEASON_CLAUSE

    target = _target(reason=f"fills your worst need at WR; {EARLY_SEASON_CLAUSE}")
    ld = _ld(
        waiver_targets=[target],
        replacement=ReplacementMarket(positions={"WR": PositionMarket("WR", None, None, None, None, ABUNDANT, None)}, players={}),
        insurance=[InsuranceRecommendation(starter=_p("starter-wr", "WR"), slot="WR", starter_projection=18.0,
                                           replacement_projection=6.0, candidate=_p("w1", "WR"), restored_projection=12.0)],
        role_trends={"w1": RoleTrend(gsis_id="g1", label=INSUFFICIENT, games=1)},
        source_views={"w1": _source_view(HIGH_DISAGREEMENT)},
    )
    text = build_provenance(ld, _report())[(WAIVER, "w1")].invalidation.text
    assert text.count(";") == MAX_INVALIDATION_FACTS - 1


def test_the_explanation_rows_never_widen_the_context_cap():
    # Both rows exist on a card whose Context slots are already spoken for by
    # the timing reasons — they live on `extras` instead of pushing MAX_CONTEXT up.
    from sleeper_tool.recommendation_provenance import INVALIDATION_PREFIX, WHY_DROP_PREFIX
    from sleeper_tool.replacement_value import ABUNDANT

    drop = _p("bench", "WR")
    target = _target(drop=drop, faab=25)
    ld = _ld(
        waiver_targets=[target],
        replacement=ReplacementMarket(positions={"WR": PositionMarket("WR", None, None, None, None, ABUNDANT, None)}, players={}),
        playoff=PlayoffLeverage(
            label=BUBBLE, wins=3, losses=3, ties=0, games_remaining=6, seed=6, playoff_teams=6,
            cut_wins=3, deadline_window=True, trade_deadline_week=11, reason="3-3, seed 6 of 12",
        ),
    )
    prov = build_provenance(ld, _report())[(WAIVER, "w1")]
    assert len(prov.context) == MAX_CONTEXT
    assert {r.category for r in prov.context} == {TIMING}  # the trending count and the deadline window
    assert not any(r.text.startswith((WHY_DROP_PREFIX, INVALIDATION_PREFIX)) for r in prov.context)
    assert prov.why_drop is not None and prov.invalidation is not None
    assert [r.direction for r in prov.extras] == [CONTEXT, CONTEXT]
