"""Shared data-gathering layer for the weekly report. Both the Markdown
renderer (report.py) and the HTML dashboard (html_report.py) consume the
same WeeklyReportData so the two outputs can never drift out of sync with
each other or re-implement the underlying business logic twice.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sleeper_tool.action_priority import ALERT, DEFENSIVE_ADD, DROP, STASH, STREAMER, Action, PriorityKey, classify, rank_actions
from sleeper_tool.asset_value import percentile_for_currency, value_currency
from sleeper_tool.buyer_board import BuyerBoard, annotate_sell_high_proposals, build_buyer_boards, sell_high_candidates
from sleeper_tool.bye_collision import ByeCollision, describe_bye_collision, plan_bye_collisions, positions_covering
from sleeper_tool.config import LEAGUES, LeagueInfo, MY_USER_ID
from sleeper_tool.cross_league_asymmetry import Asymmetry, build_asymmetries
from sleeper_tool.contender_insurance import (
    InsuranceRecommendation,
    free_agent_candidates,
    identify_fragile_starters,
    merge_insurance_into_waiver_targets,
)
from sleeper_tool.decision_delta import DecisionDelta, build_snapshot, compute_delta, load_latest_snapshot, load_snapshots
from sleeper_tool.decision_ledger import Ledger, build_entries, load_ledger, merge_entries, observe
from sleeper_tool.decision_ledger import summary as ledger_summary
from sleeper_tool.decision_outcomes import OutcomeFact, build_outcome_facts
from sleeper_tool.faab_strategy import FaabAdvice, FaabContext, TargetFacts, budget_plan, context_from_sleeper, count_substitutes
from sleeper_tool.faab_strategy import advise as faab_advise
from sleeper_tool.faab_strategy import status_note as faab_status_note
from sleeper_tool.league_economy import LeagueEconomy, build_league_economy
from sleeper_tool.lineup_decisions import LineupDecisions, build_lineup_decisions
from sleeper_tool.lineup_leverage import LineupLeverage, build_lineup_leverage
from sleeper_tool.lineup_optimizer import LineupResult, UnsupportedSlotError, optimize_lineup, slot_label
from sleeper_tool.market_velocity import Velocity, annotate_league, build_velocities
from sleeper_tool.matchup_leverage import MatchupLeverage, build_matchup_leverage
from sleeper_tool.move_impact import (
    PREVIEWED_WAIVER_TIERS,
    MoveImpact,
    PreviewContext,
    preview_add_drop,
    preview_trade,
    snapshot_roster,
)
from sleeper_tool.negotiation_ladder import NegotiationLadder, build_ladders
from sleeper_tool.opponent_blocker import DefensiveAdd, find_defensive_add, open_roster_spots
from sleeper_tool.nfl_schedule import Schedule, load_schedule
from sleeper_tool.nfl_usage import UsageData, cached_health, load_crosswalk_rows, load_usage
from sleeper_tool.pick_opportunity import SPENDABLE, STRATEGIC, PickOpportunity, assess_picks
from sleeper_tool.player_ids import PlayerIds, build_crosswalk
from sleeper_tool.playoff_leverage import PlayoffLeverage, classify_playoff_leverage
from sleeper_tool.portfolio_exposure import PortfolioExposure, acquisition_exposure_note, build_portfolio_exposure
from sleeper_tool.rankings.cache import load_snapshot
from sleeper_tool.rankings.ff_dynasty_pass import ff_dynasty_status
from sleeper_tool.recommendation_conflicts import CONFLICTED, TRADE, WAIVER, Conflict, conflict_for, detect_conflicts
from sleeper_tool.recommendation_provenance import AGAINST, CONTEXT, FOR, Provenance, build_provenance
from sleeper_tool.replacement_value import (
    ABUNDANT,
    OVERSTATED_MAX_OVER_WAIVER,
    SCARCE,
    UNDERSTATED_MIN_OVER_WAIVER,
    VERY_SCARCE,
    ReplacementMarket,
    build_replacement_market,
    player_context,
)
from sleeper_tool.roster_analysis import SKILL_POSITIONS, RosterEntry, ValuedRoster, build_all_valued_rosters
from sleeper_tool.roster_clog import RosterClog, is_dynasty_developmental, identify_roster_clogs
from sleeper_tool.role_trends import NO_HISTORY_NOTE, RoleTrend, market_cross, trends_for
from sleeper_tool.roster_consolidation import Consolidation, find_consolidations
from sleeper_tool.schedule_window import ScheduleWindows, build_windows, schedule_tiebreak, team_window
from sleeper_tool.source_disagreement import (
    HIGH_DISAGREEMENT,
    MARKET_ABOVE_PROJECTION,
    PROJECTION_ABOVE_MARKET,
    SOURCE_DISAGREEMENT,
    SourceView,
    build_source_rank_tables,
    lookup,
    source_view,
)
from sleeper_tool.signal_health import SignalHealthReport, build_health, freshness_by_source, freshness_lines, suppressed_features
from sleeper_tool.stash_board import StashCandidate, build_stash_board
from sleeper_tool.storage import Storage
from sleeper_tool.streamer_planner import ADD as STREAM_ADD
from sleeper_tool.streamer_planner import StreamPlan, plan_streams
from sleeper_tool.team_status import CONTENDER, TeamStatusResult, classify_team_status, get_valued_picks_by_roster
from sleeper_tool.trade_engine import generate_trade_proposals, identify_drop_candidates
from sleeper_tool.trade_opportunity_cost import MAJOR_LINEUP_COST, TradeEconomics, analyze_trade
from sleeper_tool.trade_types import DropCandidate, TradeProposal
from sleeper_tool.valuation import LeagueFormat, ValuationEngine, games_remaining
from sleeper_tool.waiver_engine import INSURANCE, MUST_ADD, STRONG_ADD, TimeSensitiveNote, WaiverTarget, get_time_sensitive_notes, get_waiver_targets
from sleeper_tool.watchlist import Watchlist, load_watchlist
from sleeper_tool.watchlist import candidates as watch_candidates
from sleeper_tool.watchlist import render_lines as watchlist_lines
from sleeper_tool.watchlist import update as update_watchlist

logger = logging.getLogger(__name__)

# The one free-agent pool every decision module shares: skill positions for
# insurance/replacement, plus K and DEF for streaming.
FREE_AGENT_POSITIONS = frozenset(SKILL_POSITIONS) | {"K", "DEF"}
MATCHUP_NOTE_MIN_DELTA = 0.5  # projected weekly points a move must shift before it is related to this week's gap


def describe_format(fmt: LeagueFormat) -> str:
    bits = ["Superflex" if fmt.is_superflex else "1QB"]
    if fmt.ppr >= 0.99:
        bits.append("Full PPR")
    elif fmt.ppr >= 0.49:
        bits.append("Half PPR")
    elif fmt.ppr > 0:
        bits.append(f"{fmt.ppr:g} PPR")
    else:
        bits.append("Standard")
    if fmt.te_premium_bonus > 0:
        bits.append(f"TE Premium (+{fmt.te_premium_bonus:g}/rec)")
    if fmt.rush_100_bonus > 0:
        bits.append(f"100yd rush bonus (+{fmt.rush_100_bonus:g})")
    if fmt.pass_td_pts != 4:
        bits.append(f"{fmt.pass_td_pts:g}pt pass TD")
    return ", ".join(bits)


@dataclass
class LeagueReportData:
    league: LeagueInfo
    fmt_desc: str = ""
    currency: str = ""
    drafted: bool = False
    roster: ValuedRoster | None = None
    team_status: TeamStatusResult | None = None
    proposals: list[TradeProposal] = field(default_factory=list)
    waiver_targets: list[WaiverTarget] = field(default_factory=list)
    time_sensitive: list[TimeSensitiveNote] = field(default_factory=list)
    drop_candidates: list[DropCandidate] = field(default_factory=list)
    roster_clogs: list[RosterClog] = field(default_factory=list)  # excludes players already listed as drop candidates
    lineup: LineupResult | None = None  # my best legal lineup (structural: no bye-week exclusions)
    lineup_leverage: LineupLeverage | None = None
    lineup_decisions: LineupDecisions | None = None  # what needs deciding about THIS week's lineup
    insurance: list[InsuranceRecommendation] = field(default_factory=list)  # contenders only; also merged into waiver_targets
    bye_collision: ByeCollision | None = None  # earliest look-ahead week with a Bye Hole; also a time_sensitive note
    league_economy: LeagueEconomy | None = None  # per-manager trade/pick/position tendencies, current season
    trade_impacts: list[MoveImpact | None] = field(default_factory=list)  # parallel to proposals; None = below preview bar
    waiver_impacts: dict[str, MoveImpact] = field(default_factory=dict)  # by waiver target player_id (move_impact.PREVIEWED_WAIVER_TIERS, never Insurance)
    playoff: PlayoffLeverage | None = None  # standings position vs the playoff cut; None until 3 games are played
    pick_opportunity: PickOpportunity | None = None  # dynasty only: what my 1st/2nd-round picks mean to this roster
    ladders: dict[int, NegotiationLadder] = field(default_factory=dict)  # by proposal index; top two buy-low/pick-target trades
    waivers_note: str | None = None  # shown in place of waiver targets when they're deliberately suppressed
    replacement: ReplacementMarket | None = None  # league replacement levels by position; None pre-draft or without a lineup
    replacement_clauses: dict[str, str] = field(default_factory=dict)  # my player_id -> one-line replacement context, for renderers to attach wherever he's named
    source_views: dict[str, SourceView] = field(default_factory=dict)  # by player_id: my roster, trade pieces, waiver targets
    trade_economics: list[TradeEconomics | None] = field(default_factory=list)  # parallel to proposals: asset vs roster economics, kept separate
    streamers: list[StreamPlan] = field(default_factory=list)  # QB/TE/K/DEF plans over the next few weeks; empty pre-draft
    velocity: dict[str, Velocity] = field(default_factory=dict)  # by player_id, actionable players only (trade pieces, waiver targets, drops)
    matchup: MatchupLeverage | None = None  # this week's opponent and projected gap; None without a matchup row
    defensive_add: DefensiveAdd | None = None  # at most one per league per week
    stash: list[StashCandidate] = field(default_factory=list)  # dynasty/keeper developmental adds; empty pre-draft and in redraft
    windows: ScheduleWindows | None = None  # next-3 / remaining / playoff week windows from the NFL schedule and league settings
    consolidations: list[Consolidation] = field(default_factory=list)  # 2-for-1 proposals for contenders and strong middling teams
    buyer_boards: list[BuyerBoard] = field(default_factory=list)  # per sell-high candidate, the counterparties most likely to pay
    conflicts: list[Conflict] = field(default_factory=list)  # opposing signals on one move; set cross-league in build_weekly_report_data
    role_trends: dict[str, RoleTrend] = field(default_factory=dict)  # by player_id (my roster, trade pieces, waiver targets, stash); empty until the season has usage rows
    role_market: dict[str, str] = field(default_factory=dict)  # by player_id: Role Ahead of Market / Market Ahead of Role / Role and Market Confirm
    faab: dict[str, FaabAdvice] = field(default_factory=dict)  # by waiver target player_id; empty pre-draft and in non-FAAB leagues
    faab_note: str | None = None  # why there is no FAAB advice, when there isn't any
    faab_context: FaabContext | None = None
    provenance: dict[tuple[str, str], Provenance] = field(default_factory=dict)  # by (kind, key): the For/Against/Context evidence per recommendation
    note_directions: dict[tuple[str, str], str] = field(default_factory=dict)  # (player_id, note text) -> FOR/AGAINST/CONTEXT, stated by the annotator that wrote the note
    priorities: dict[tuple[str, str], PriorityKey] = field(default_factory=dict)  # by (kind, key): the six-dimension priority key
    error: str | None = None


@dataclass
class PriorityAction:
    """One entry in the cross-league "best moves right now" list — without
    this, a user with 10 leagues has to click into each one individually
    to find anything urgent; nothing in this module previously aggregated
    across leagues at all.
    """
    league_name: str
    kind: str  # "alert" | "trade" | "waiver" | "roster" | "defensive_add" | "streamer"
    headline: str
    detail: str
    rank: int = 0  # lower = more important WITHIN this kind — quality tiebreak after the priority key, not shown to the user
    key: str = ""  # the shared identity: proposal index as text, player_id, position, player name
    priority: PriorityKey | None = None  # action_priority's six categorical dimensions
    why_now: list[str] = field(default_factory=list)  # provenance reasons FOR (at most MAX_FOR)
    against: list[str] = field(default_factory=list)  # provenance reasons AGAINST (at most MAX_AGAINST)
    context: list[str] = field(default_factory=list)
    conflict_note: str = ""  # the Conflict's reasons against, when the move is a Conflicted Move; never prefixed into detail


@dataclass
class WeeklyReportData:
    generated_at: dt.datetime
    current_week: int | None
    source_freshness: dict[str, dt.timedelta]
    ff_status: str
    leagues: list[LeagueReportData]
    priority_actions: list[PriorityAction] = field(default_factory=list)
    portfolio: PortfolioExposure | None = None  # cross-league player concentration
    asymmetries: list[Asymmetry] = field(default_factory=list)  # widely-held players with a league where moving them is cheap
    delta: DecisionDelta | None = None  # vs the last complete run's snapshot; None on a first run
    snapshot: dict | None = None  # this run's decision snapshot, for daily_run to persist after a complete run
    health: SignalHealthReport | None = None  # every input graded Fresh/Usable/Partial/Stale/Unavailable
    suppressed: dict[str, str] = field(default_factory=dict)  # feature -> why it was suppressed this run (a required source is Unavailable)
    freshness_lines: list[str] = field(default_factory=list)  # one labelled line per source, for both renderers
    usage_note: str | None = None  # "Role data begins after games are played" until the season's first usage rows exist
    crosswalk_note: str | None = None  # how many of the players that matter resolved to an nflverse id
    watchlist: Watchlist | None = None  # this run's updated watchlist (in memory); daily_run persists it after a complete run
    watchlist_new: list[str] = field(default_factory=list)  # New Trigger lines only
    watchlist_watching: int = 0
    ledger: Ledger | None = None  # this run's merged + observed ledger (in memory); daily_run persists it after a complete run
    ledger_new: int = 0  # recommendations first recorded this run
    ledger_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    outcome_facts: list[OutcomeFact] = field(default_factory=list)


_CONFIDENCE_SORT_RANK = {"High": 0, "Medium": 1, "Low": 2}  # ascending = best first (trade_rating's own dict counts the other way)

# Renderer kind for each action_priority kind (drops render as "roster").
_RENDER_KIND = {DROP: "roster"}


def build_priority_actions(
    leagues: list[LeagueReportData], report: WeeklyReportData | None = None, *, max_actions: int = 8
) -> list[PriorityAction]:
    """The cross-league "best moves right now" list. Candidates are the
    recommendations worth a top slot: high-severity alerts, trades with a
    Good/High acceptance rating AND at least Medium valuation confidence,
    Must-Add and Insurance waiver rows, this week's defensive add, streamer
    switches, Strong-Drop cleanup. Their ORDER is action_priority's six
    categorical dimensions compared lexicographically (Urgency,
    Materiality, Perishability, Strategic fit, Evidence agreement, Cost) —
    a Must Add is Immediate and so outranks any trade; a Strong Drop is
    cheaper to reverse than any trade and so outranks an otherwise-equal
    one — then kind order, then the kind's own quality rank (acceptance
    tier and confidence for trades, percentile for waivers, reason count
    for drops), then league name and headline, so the list is stable run
    to run. No per-kind floors and no numeric boosts: the dimensions ARE
    the arbitration, and `PriorityAction.priority` says which ones decided.
    Every action carries its provenance card's For/Against reasons as
    `why_now` / `against`. An empty result is a legitimate "nothing urgent
    right now" — nothing is manufactured to fill the list.
    """
    actions: list[PriorityAction] = []
    for ld in leagues:
        if ld.error or not ld.drafted:
            continue
        for note in ld.time_sensitive:
            if note.severity == "high":
                actions.append(_priority_action(
                    ld, report, ALERT, note.player_name,
                    headline=f"{note.player_name} — {note.note}",
                    detail=f"{ld.league.name} — check before this week's lineup locks.",
                ))
        # A Bubble / Long Shot team inside its trade deadline window: the
        # trades already generated for it are the time-boxed ones — the
        # priority key's Urgency dimension puts them ahead, and the detail
        # says why. Nothing new is generated.
        deadline_urgent = ld.playoff is not None and ld.playoff.urgent
        for i, p in enumerate(ld.proposals):
            if p.acceptance_rating in ("High", "Good") and p.confidence in ("High", "Medium"):
                tier_rank = 0 if p.acceptance_rating == "High" else 1
                detail = f"{ld.league.name} — {p.acceptance_rating.lower()} acceptance likelihood, {p.trade_type.replace('_', ' ')}."
                if deadline_urgent:
                    detail = f"Deadline Window ({ld.playoff.label}, deadline week {ld.playoff.trade_deadline_week}) — {detail}"
                detail += _economics_note(ld.trade_economics[i] if i < len(ld.trade_economics) else None)
                detail += _source_note_for(ld.source_views, [*p.give, *p.receive])
                actions.append(_priority_action(
                    ld, report, TRADE, str(i), headline=p.summary_line(), detail=detail,
                    rank=tier_rank * 10 + _CONFIDENCE_SORT_RANK.get(p.confidence, 2),
                ))
        for t in ld.waiver_targets:
            if t.priority_tier in (MUST_ADD, INSURANCE):
                drop_note = f", drop {t.drop_candidate.name}" if t.drop_candidate else ""
                pctl = (t.value.dynasty_value_percentile or t.value.redraft_ecr_percentile) if t.value else None
                impact = ld.waiver_impacts.get(t.player_id)
                impact_note = f" Impact: {'; '.join(impact.material_deltas())}." if impact is not None and impact.material_deltas() else ""
                advice = ld.faab.get(t.player_id)
                faab_note = f" FAAB: ${advice.suggested_dollars} ({advice.posture})." if advice is not None else ""
                actions.append(_priority_action(
                    ld, report, WAIVER, t.player_id,
                    headline=f"Add {t.name}{drop_note}",
                    detail=f"{ld.league.name} — {t.reason}" + impact_note + faab_note + _source_note_for(ld.source_views, [t]),
                    rank=-(pctl or 0),  # higher percentile ranks first (more negative sorts earlier)
                ))
        if ld.defensive_add is not None:
            actions.append(_priority_action(
                ld, report, DEFENSIVE_ADD, ld.defensive_add.target.player_id,
                headline=f"Defensive add: {ld.defensive_add.target.name}",
                detail=f"{ld.league.name} — {ld.defensive_add.describe()}",
            ))
        for plan in ld.streamers:
            if plan.recommendation == STREAM_ADD:
                actions.append(_priority_action(
                    ld, report, STREAMER, plan.position,
                    headline=f"Stream {plan.position}: {plan.describe()}",
                    detail=f"{ld.league.name} — streamer switch for the next {len(plan.weeks)} week(s).",
                ))
        for d in ld.drop_candidates:
            if d.priority == "Strong Drop":
                actions.append(_priority_action(
                    ld, report, DROP, d.entry.player_id,
                    headline=f"{d.priority}: {d.entry.name}",
                    detail=f"{ld.league.name} — {'; '.join(d.reasons)}",
                ))  # no reason-count rank: the annotation passes append reasons that aren't independent
    # The ordering is action_priority's, not a sibling of it: the six
    # dimensions, then kind, then the kind's own quality rank, league, headline.
    by_action = {}
    for a in actions:
        kind = next((k for k, v in _RENDER_KIND.items() if v == a.kind), a.kind)
        action = Action(kind, a.key, next(ld for ld in leagues if ld.league.name == a.league_name), a.priority, a.headline, a.detail, a.rank)
        by_action[id(action)] = a
        by_action.setdefault("order", []).append(action)
    ranked = rank_actions(by_action.pop("order", []))
    return [by_action[id(action)] for action in ranked][:max_actions]


def _priority_action(
    ld: LeagueReportData, report: WeeklyReportData | None, kind: str, key: str, *, headline: str, detail: str, rank: int = 0
) -> PriorityAction:
    """One candidate with its priority key and provenance attached. The key
    is classified here (cheap, and pure over the report objects) so a
    league built without the cross-league pass — a test, a partial run —
    still gets ordered on the same dimensions."""
    prov = ld.provenance.get((kind, key))
    priority = ld.priorities.get((kind, key)) or classify(kind, ld, report, provenance=prov, key=key)
    conflict = conflict_for(ld.conflicts, kind, key) if kind in (TRADE, WAIVER) else None
    return PriorityAction(
        league_name=ld.league.name, kind=_RENDER_KIND.get(kind, kind), headline=headline, detail=detail, rank=rank,
        key=key, priority=priority, conflict_note="; ".join(conflict.reasons_against) if conflict is not None else "",
        why_now=[r.text for r in prov.reasons_for] if prov is not None else [],
        against=[r.text for r in prov.reasons_against] if prov is not None else [],
        context=[r.text for r in prov.context] if prov is not None else [],
    )



def _conflict_prefix(conflict: Conflict | None) -> str:
    """A Conflicted Move stays in Best Moves, labelled: the reader decides."""
    if conflict is None:
        return ""
    return f"{CONFLICTED}: against — {'; '.join(conflict.reasons_against)}. "


def _economics_note(econ: TradeEconomics | None) -> str:
    """The Best Moves list must not hide a lineup cost behind a good
    acceptance rating: a Strategic Tradeoff or a Major Lineup Cost is
    named right on the action."""
    if econ is None or econ.roster_economics is None:
        return ""
    if econ.strategic_tradeoff:
        return (
            f" Strategic Tradeoff: assets {econ.asset_economics.lower()}, lineup {econ.roster_economics.lower()}"
            f" ({econ.weekly_delta:+.1f}/wk)."
        )
    if econ.roster_economics == MAJOR_LINEUP_COST:
        return f" {MAJOR_LINEUP_COST} ({econ.weekly_delta:+.1f}/wk)."
    return ""


def _source_note_for(views: dict[str, SourceView], pieces) -> str:
    """One clause naming the pieces the ranking sources genuinely disagree
    on (not the milder market-vs-projection direction)."""
    split = [
        v.name for e in pieces
        if (v := views.get(e.player_id)) is not None and v.consensus in (SOURCE_DISAGREEMENT, HIGH_DISAGREEMENT)
    ]
    return f" Sources disagree on {', '.join(split)}." if split else ""


def _entry_from_target(t: WaiverTarget, all_players: dict) -> RosterEntry:
    """A waiver target as a RosterEntry, so lineup/replacement code can
    treat him like any rostered player."""
    pdata = all_players.get(t.player_id) or {}
    return RosterEntry(
        player_id=t.player_id, name=t.name, position=t.position, team=t.team, age=pdata.get("age"),
        years_exp=pdata.get("years_exp"), injury_status=pdata.get("injury_status"), status=pdata.get("status"),
        is_starter=False, is_taxi=False, is_reserve=False, value=t.value,
    )


def build_league_report_data(
    storage: Storage, engine: ValuationEngine, league: LeagueInfo, current_week: int | None,
    schedule: Schedule | None = None, *, usage: UsageData | None = None, crosswalk: dict[str, PlayerIds] | None = None,
    suppressed: dict[str, str] | None = None,
) -> LeagueReportData:
    """`usage`/`crosswalk` are the season's nflverse rows and the Sleeper->
    gsis id map, loaded once per run by build_weekly_report_data (None
    until games are played). `suppressed` is signal_health's feature ->
    reason map for this run; a feature listed there is skipped rather
    than built from a source that isn't there."""
    suppressed = suppressed or {}
    rosters = build_all_valued_rosters(storage, engine, league)
    my_roster = next((r for r in rosters.values() if r.owner_id == MY_USER_ID), None)

    if my_roster is None:
        return LeagueReportData(league=league, error="Could not find my roster in this league (sync issue?).")

    fmt_desc = describe_format(my_roster.fmt)
    if not my_roster.entries:
        return LeagueReportData(
            league=league, fmt_desc=fmt_desc, currency=value_currency(my_roster), drafted=False, roster=my_roster
        )

    currency = value_currency(my_roster)
    # Every roster in this league is classified below, and each
    # classification prices the whole league's draft picks against the same
    # KTC snapshot. Compute that once and hand it down.
    valued_picks = get_valued_picks_by_roster(rosters, currency, storage, engine)
    status_result = classify_team_status(
        my_roster.roster_id, rosters, currency, storage=storage, engine=engine, valued_picks=valued_picks
    )
    league_data = storage.get_league(league.league_id) or {}
    waiver_budget = (league_data.get("settings") or {}).get("waiver_budget")
    trending_add_ids = {row["player_id"] for row in storage.get_trending("add")}
    # Every lineup-based feature below is optional: a league whose slot
    # list this optimizer can't model (an unknown slot type, an empty
    # roster_positions payload) keeps its trades, waivers, alerts and
    # status — none of which need a lineup — and skips the rest.
    # One structural lineup per roster, solved once and shared with every
    # consumer that would otherwise re-solve the same league from scratch
    # (the replacement market, the preview context, the pick-opportunity
    # units). The optimizer is a bitmask DP and is by a wide margin the
    # most expensive thing in the run; this is the same league, so the
    # answers are identical, not approximations of each other.
    try:
        lineups: dict[int, LineupResult] = {
            rid: optimize_lineup(r) for rid, r in rosters.items() if r.entries
        }
    except UnsupportedSlotError as exc:
        logger.warning("%s: lineup-based features skipped — %s", league.name, exc)
        lineups = {}
    lineup: LineupResult | None = lineups.get(my_roster.roster_id)
    # A league still pre-draft on Sleeper (keepers rostered, draft to come)
    # has no waiver wire yet: every "free agent" is about to be drafted, so
    # adds and insurance from that pool would be fiction. Trades and lineup
    # analysis of the kept roster still stand.
    pre_draft = league_data.get("status") in ("pre_draft", "drafting")
    # One free-agent pool per league (skill positions plus K/DEF), shared by
    # insurance, the replacement market and the streamer planner. Pre-draft
    # it would be the undrafted universe, so it's empty there and every
    # consumer stays silent.
    all_players = storage.get_all_players()
    all_free_agents: list[RosterEntry] = []  # includes unprojected but dynasty-valued players (stash board)
    free_agents: list[RosterEntry] = []  # the projected subset every lineup-based consumer uses
    replacement: ReplacementMarket | None = None
    if lineup is not None and not pre_draft:
        all_free_agents = free_agent_candidates(
            storage, engine, league, my_roster, positions=FREE_AGENT_POSITIONS, require_projection=False
        )
        free_agents = [fa for fa in all_free_agents if fa.value.proj_points is not None]
        replacement = build_replacement_market(
            my_roster, rosters, free_agents, current_week=current_week, lineups=lineups
        )
    status_of = {
        r.roster_id: classify_team_status(
            r.roster_id, rosters, currency, storage=storage, engine=engine, valued_picks=valued_picks
        ).status
        for r in rosters.values()
        if r.entries
    }
    league_economy = build_league_economy(
        rosters, storage.get_all_transactions(league.league_id), storage.get_traded_picks(league.league_id),
        season=str(league_data.get("season") or ""),
    )
    # The buyer board says WHICH manager should receive a sell-high; building
    # proposals first meant a card could name one counterparty while the
    # board named a different Strong Fit two sections down. Board first, then
    # the engine prefers its fits and can see the league's own trade record.
    pre_boards = (
        build_buyer_boards(
            my_roster, rosters, sell_high_candidates(my_roster, []),
            status_of=status_of, economy=league_economy, market=replacement, valued_picks=valued_picks,
        )
        if not pre_draft else []
    )
    proposals = generate_trade_proposals(
        league, rosters, status_result=status_result, storage=storage, engine=engine,
        preferred_buyers={b.candidate.player_id: [f.username for f in b.buyers if f.username] for b in pre_boards},
        manager_labels={m.username: m.labels for m in league_economy.managers.values() if m.username},
    )
    # Exclude anyone already used as a give-piece in a live trade proposal
    # this run -- otherwise the same player could be told to both trade
    # away for value and cut for nothing in the same report.
    proposed_give_ids = frozenset(e.player_id for p in proposals for e in p.give)
    # 2-for-1 consolidations are TradeProposals like any other: appended to
    # the list so every annotation pass, preview, economics line, conflict
    # check and the Best Moves list see them. `consolidations` keeps the
    # per-trade extras (weekly gain, freed slot, fragility) for the summary.
    consolidations = (
        find_consolidations(
            league, my_roster, rosters, status_result=status_result, status_of=status_of, lineup=lineup,
            free_agents=[fa for fa in free_agents if fa.position in SKILL_POSITIONS], current_week=current_week,
            exclude_ids=proposed_give_ids,
        )
        if not pre_draft
        else []
    )
    proposals.extend(c.proposal for c in consolidations)
    proposed_give_ids = frozenset(e.player_id for p in proposals for e in p.give)
    roster_clogs = (
        identify_roster_clogs(my_roster, trending_add_ids=trending_add_ids, exclude_ids=proposed_give_ids, lineup=lineup)
        if lineup is not None
        else []
    )
    clog_ids = frozenset(c.entry.player_id for c in roster_clogs)
    waivers_note = None
    if pre_draft:
        waiver_targets: list[WaiverTarget] = []
        waivers_note = "League is still pre-draft on Sleeper — waiver and insurance targets are suppressed until the draft."
    else:
        # Role labels for the candidate pool before tiering, not after: a
        # Role Surging free agent is a different add from a trending name,
        # and the tier is where that has to land. Dormant until the season
        # publishes usage rows, like every other role reading.
        candidate_roles: dict[str, str] = {}
        if usage is not None and crosswalk and "role_trends" not in suppressed:
            candidate_roles = {
                pid: t.label for pid, t in trends_for(usage, crosswalk, sorted(trending_add_ids)).items()
            }
        waiver_targets = get_waiver_targets(
            storage, engine, league, my_roster, current_week=current_week, waiver_budget=waiver_budget, clog_ids=clog_ids,
            starter_ids=lineup.starter_ids if lineup is not None else None,
            protected_ids=lineup.starter_ids if lineup is not None else (),
            role_labels=candidate_roles or None,
        )
    insurance: list[InsuranceRecommendation] = []
    if status_result.status == CONTENDER and lineup is not None and not pre_draft:
        skill_free_agents = [fa for fa in free_agents if fa.position in SKILL_POSITIONS]
        insurance = identify_fragile_starters(my_roster, skill_free_agents, team_status=status_result.status, lineup=lineup)
        # Insurance is for a position the wire can't refill on the day: an
        # Abundant or Normal market is its own insurance.
        if replacement is not None:
            insurance = [r for r in insurance if replacement.scarcity_of(r.starter.position) in (SCARCE, VERY_SCARCE)]
        trade_deadline = (league_data.get("settings") or {}).get("trade_deadline")
        deadline_passed = bool(trade_deadline) and current_week is not None and current_week > int(trade_deadline)
        waiver_targets = merge_insurance_into_waiver_targets(
            waiver_targets, insurance, my_roster, current_week=current_week, deadline_passed=deadline_passed,
            waiver_budget=waiver_budget, clog_ids=clog_ids, protected_ids=lineup.starter_ids,
        )
    time_sensitive = get_time_sensitive_notes(storage, my_roster, current_week=current_week)
    urgent_add_ids: set[str] = set()  # waiver targets that answer a bye hole (FAAB posture reads this)
    note_directions: dict[tuple[str, str], str] = {}  # (player_id, note) -> the side the note argues for
    # Pre-draft, the roster is keepers and placeholders: a week-5 bye hole is
    # a draft-day fact, not a waiver move, and "drop" means "don't keep".
    bye_collision = plan_bye_collisions(my_roster, current_week=current_week, lineup=lineup) if lineup is not None and not pre_draft else None
    if bye_collision is not None:
        # Next week's hole is this week's waiver move; further out is a heads-up.
        severity = "medium" if bye_collision.week == (current_week or 0) + 1 else "low"
        time_sensitive.append(
            TimeSensitiveNote(f"Week {bye_collision.week} bye hole", describe_bye_collision(bye_collision), severity=severity)
        )
        covering = positions_covering(bye_collision)
        for t in waiver_targets:
            if t.position in covering:
                note = f"Would also cover your week {bye_collision.week} bye hole"
                t.notes.append(note)
                note_directions[(t.player_id, note)] = FOR
                urgent_add_ids.add(t.player_id)
    lineup_leverage = build_lineup_leverage(my_roster, lineup=lineup, current_week=current_week) if lineup is not None else None
    if lineup_leverage is not None:
        _annotate_proposals_with_bench_surplus(proposals, lineup_leverage)
    # Bench surplus is trade material by the report's own reading; the
    # same player can't also be a drop candidate two sections later.
    surplus_ids = frozenset(s.entry.player_id for s in lineup_leverage.bench_surplus) if lineup_leverage is not None else frozenset()
    # lineup_optimizer owns who starts; a player it starts is never a drop,
    # whatever Sleeper's set-lineup flag says about him.
    starter_ids = frozenset(lineup.starter_ids) if lineup is not None else frozenset()
    drop_candidates = (
        identify_drop_candidates(my_roster, status_result.status, exclude_ids=proposed_give_ids | surplus_ids | starter_ids)
        if not pre_draft else []
    )
    # A clog that's already a drop candidate is surfaced there; listing him
    # twice under two headings is noise, not extra information.
    drop_ids = {d.entry.player_id for d in drop_candidates}
    roster_clogs = [c for c in roster_clogs if c.entry.player_id not in drop_ids]

    replacement_clauses: dict[str, str] = {}
    if replacement is not None:
        per_week = games_remaining(current_week)
        replacement_clauses = {pid: c for pid, ctx in replacement.players.items() if (c := ctx.clause())}
        _annotate_waivers_with_replacement(waiver_targets, replacement, currency, per_week, all_players, note_directions)
        _annotate_clogs_with_replacement(roster_clogs, replacement)

    matchup: MatchupLeverage | None = None
    defensive_add: DefensiveAdd | None = None
    if lineup is not None and current_week:
        matchup = build_matchup_leverage(
            my_roster, rosters, storage.get_matchups(league.league_id, current_week), current_week=current_week
        )
    if matchup is not None and free_agents:
        # Nothing I value is on the table for a block: optimized starters,
        # bench surplus, clog-exempt developmental players, live trade pieces.
        protected = set(lineup.starter_ids) | set(proposed_give_ids)
        if lineup_leverage is not None:
            protected |= {s.entry.player_id for s in lineup_leverage.bench_surplus}
        protected |= {e.player_id for e in my_roster.entries if is_dynasty_developmental(e, currency)}
        defensive_add = find_defensive_add(
            my_roster, rosters[matchup.opponent_roster_id], free_agents,
            current_week=current_week, protected_ids=protected, clog_ids=clog_ids,
            # The matchup already solved both this-week lineups for the same
            # week with the same exclusions; `lineups` holds the structural one.
            opponent_week_lineup=matchup.opponent_lineup, my_week_lineup=matchup.my_lineup,
            opponent_structural_lineup=lineups.get(matchup.opponent_roster_id),
        )

    stash = build_stash_board(
        my_roster, [fa for fa in all_free_agents if fa.position in SKILL_POSITIONS],
        league_kind=league.kind, pre_draft=pre_draft, open_spots=open_roster_spots(my_roster), clogs=roster_clogs, market=replacement,
    )

    source_table = build_source_rank_tables(engine.snapshots_for(my_roster.fmt), my_roster.fmt)
    source_views = (
        _build_source_views(source_table, currency, my_roster, proposals, waiver_targets)
        if "source_disagreement" not in suppressed
        else {}
    )
    _annotate_proposals_with_sources(proposals, source_views)
    for t in waiver_targets:
        v = source_views.get(t.player_id)
        if v is not None and v.disagrees:
            note = f"Sources: {v.describe()}"
            t.notes.append(note)
            note_directions[(t.player_id, note)] = CONTEXT

    _annotate_proposals_with_league_economy(proposals, league_economy, rosters)

    settings = league_data.get("settings") or {}
    windows = build_windows(schedule, settings, current_week)
    if windows is not None and schedule is not None:
        _annotate_with_schedule_windows(schedule, windows, lineup_leverage, waiver_targets, proposals)
    playoff = classify_playoff_leverage(
        my_roster.roster_id, rosters,
        playoff_teams=settings.get("playoff_teams"), playoff_week_start=settings.get("playoff_week_start"),
        trade_deadline=settings.get("trade_deadline"), current_week=current_week,
    )

    pick_opportunity = None
    ladders = build_ladders(
        proposals, my_roster, rosters, (valued_picks or {}).get(my_roster.roster_id, []),
        my_status=status_result.status, status_of=status_of,
        my_starter_ids=lineup.starter_ids if lineup is not None else (),
    )
    buyer_boards: list[BuyerBoard] = []
    if not pre_draft:
        buyer_boards = build_buyer_boards(
            my_roster, rosters, sell_high_candidates(my_roster, proposals),
            status_of=status_of, economy=league_economy, market=replacement, valued_picks=valued_picks,
        )
        annotate_sell_high_proposals(proposals, buyer_boards)
    _annotate_ladders_with_sources(ladders, source_views)
    if valued_picks is not None and lineup is not None:
        pick_opportunity = assess_picks(
            my_roster, rosters, valued_picks.get(my_roster.roster_id, []),
            team_status=status_result.status, my_lineup=lineup, lineups=lineups,
        )
        if pick_opportunity is not None:
            _annotate_proposals_with_pick_opportunity(proposals, pick_opportunity)

    trade_impacts: list[MoveImpact | None] = [None] * len(proposals)
    waiver_impacts: dict[str, MoveImpact] = {}
    if lineup is not None:
        ctx = PreviewContext.build(rosters, current_week=current_week, storage=storage, engine=engine, lineups=lineups)
        before = snapshot_roster(my_roster, ctx, lineup=lineup, displayed_status=status_result.status)
        trade_impacts = [preview_trade(p, my_roster, before, ctx) for p in proposals]
        for t in waiver_targets:
            if t.priority_tier not in PREVIEWED_WAIVER_TIERS or t.priority_tier == INSURANCE:
                continue  # an insurance row previewed as a live swap benches the starter it insures
            add_entry = _entry_from_target(t, all_players)
            drop_id = t.drop_candidate.player_id if t.drop_candidate else None
            label = f"Add {t.name}" + (f", drop {t.drop_candidate.name}" if t.drop_candidate else "")
            waiver_impacts[t.player_id] = preview_add_drop(label, add_entry, drop_id, my_roster, before, ctx)
    trade_economics = [analyze_trade(p, impact, replacement) for p, impact in zip(proposals, trade_impacts)]
    if replacement is not None:
        # After economics, so a give-side scarcity caveat is skipped where
        # the economics line already carries the same note.
        _annotate_proposals_with_replacement(proposals, replacement, currency, games_remaining(current_week), trade_economics)
    if matchup is not None:
        for impact in (*trade_impacts, *waiver_impacts.values()):
            # "+0.0 against a 17.8 edge" is not a fact about the move.
            if impact is not None and abs(impact.weekly_points_delta) >= MATCHUP_NOTE_MIN_DELTA:
                impact.matchup_note = matchup.effect_clause(impact.weekly_points_delta)
    streamers = plan_streams(my_roster, free_agents, schedule=schedule, current_week=current_week, lineup=lineup)

    # Role trends for the players this report names, through the crosswalk.
    # Nothing here until the season has usage rows; the market cross and
    # the sparse annotations wait for velocity in build_weekly_report_data.
    role_trends: dict[str, RoleTrend] = {}
    if usage is not None and crosswalk and "role_trends" not in suppressed:
        named = {e.player_id for e in my_roster.entries}
        named |= {e.player_id for p in proposals for e in (*p.give, *p.receive)}
        named |= {t.player_id for t in waiver_targets}
        named |= {c.entry.player_id for c in stash}
        role_trends = trends_for(usage, crosswalk, sorted(named))

    # This week's lineup decisions read the week lineup the matchup already
    # solved (byes and game-day outs applied), so nothing is re-optimized.
    lineup_decisions = (
        build_lineup_decisions(
            my_roster, structural_lineup=lineup, week_lineup=matchup.my_lineup if matchup is not None else None,
            leverage=lineup_leverage, bye_collision=bye_collision, matchup=matchup, current_week=current_week,
            schedule=schedule, free_agents=[fa for fa in free_agents if fa.value.proj_points is not None],
            context_lines={pid: [t.describe()] for pid, t in role_trends.items()} or None,
        )
        if lineup is not None else None
    )

    faab_ctx = context_from_sleeper(
        league_data, storage.get_rosters(league.league_id), storage.get_all_transactions(league.league_id),
        my_roster.roster_id, current_week=current_week, pre_draft=pre_draft,
    )
    faab = _build_faab_advice(faab_ctx, waiver_targets, free_agents, replacement, currency, role_trends, urgent_add_ids)

    return LeagueReportData(
        league=league,
        fmt_desc=fmt_desc,
        currency=currency,
        drafted=True,
        roster=my_roster,
        team_status=status_result,
        proposals=proposals,
        waiver_targets=waiver_targets,
        time_sensitive=time_sensitive,
        drop_candidates=drop_candidates,
        roster_clogs=roster_clogs,
        lineup=lineup,
        lineup_leverage=lineup_leverage,
        lineup_decisions=lineup_decisions,
        insurance=insurance,
        bye_collision=bye_collision,
        league_economy=league_economy,
        trade_impacts=trade_impacts,
        waiver_impacts=waiver_impacts,
        playoff=playoff,
        pick_opportunity=pick_opportunity,
        ladders=ladders,
        waivers_note=waivers_note,
        replacement=replacement,
        replacement_clauses=replacement_clauses,
        source_views=source_views,
        trade_economics=trade_economics,
        streamers=streamers,
        matchup=matchup,
        defensive_add=defensive_add,
        stash=stash,
        windows=windows,
        consolidations=consolidations,
        buyer_boards=buyer_boards,
        role_trends=role_trends,
        note_directions=note_directions,
        faab=faab,
        faab_note=faab_status_note(faab_ctx),
        faab_context=faab_ctx,
    )


def _build_faab_advice(
    ctx: FaabContext, targets: list[WaiverTarget], free_agents: list[RosterEntry], market: ReplacementMarket | None,
    currency: str, role_trends: dict[str, RoleTrend], urgent_ids: set[str],
) -> dict[str, FaabAdvice]:
    """One FaabAdvice per waiver target from facts the report already
    holds: tier, horizon, the position's replacement scarcity, his role
    label, how many comparable free agents sit near him, and whether the
    add answers an urgent need (a need the engine found, an insurance
    row, a bye-hole cover). Then the table-level affordability pass."""
    if not targets:
        return {}

    def pctl_of(e) -> float | None:
        return percentile_for_currency(e.value, currency) if getattr(e, "value", None) is not None else None

    out: dict[str, FaabAdvice] = {}
    for t in targets:
        trend = role_trends.get(t.player_id)
        facts = TargetFacts(
            player_id=t.player_id, name=t.name, tier=t.priority_tier, horizon=t.horizon,
            scarcity=market.scarcity_of(t.position) if market is not None else None,
            role_label=trend.label if trend is not None else None,
            substitutes=count_substitutes(free_agents, t.position, pctl_of(t), percentile_of=pctl_of, exclude_ids={t.player_id}),
            # A relative 'need' (two of four positions are always the two weakest) is not urgency;
            # only an insurance row or a bye-hole cover is.
            need_urgency=t.priority_tier == INSURANCE or t.player_id in urgent_ids,
            suggested_pct=t.suggested_faab_pct,
        )
        advice = faab_advise(ctx, facts)
        if advice is not None:
            out[t.player_id] = advice
    for pid, note in budget_plan([out[t.player_id] for t in targets if t.player_id in out], ctx.remaining).items():
        out[pid].notes.append(note)
    return out


def _annotate_with_schedule_windows(schedule, windows: ScheduleWindows, leverage, targets: list[WaiverTarget], proposals: list[TradeProposal]) -> None:
    """Schedule facts only where they change a read: a tiebreak on a
    near-equal start/sit call, a bye inside the next window or the
    fantasy playoffs on a player being added or acquired."""
    if leverage is not None:
        for d in leverage.close_calls:
            if d.alternative is not None:
                d.schedule_note = schedule_tiebreak(
                    d.starter.name, d.starter.team, d.starter_projection,
                    d.alternative.name, d.alternative.team, d.alternative_projection, schedule, windows,
                )
    for t in targets:
        tw = team_window(schedule, t.team, windows)
        note = tw.note() if tw is not None else None
        if note:
            t.notes.append(f"Schedule: {note}")
    for p in proposals:
        for e in p.receive:
            tw = team_window(schedule, e.team, windows)
            note = tw.note() if tw is not None else None
            if note:
                p.caveats.append(f"Schedule: {e.name} has a {note}.")


def _annotate_proposals_with_replacement(
    proposals: list[TradeProposal], market: ReplacementMarket, currency: str, per_week: int,
    economics: list[TradeEconomics | None] = (),
) -> None:
    """What each piece is worth against THIS league's waiver wire. A
    give-piece with a real edge in a scarce market is a caveat (unless the
    trade's economics line already says so); one the wire nearly matches
    is a point in favour. Mirror image for receives."""
    for i, p in enumerate(proposals):
        econ = economics[i] if i < len(economics) else None
        economics_says_scarce = econ is not None and econ.scarcity_note is not None
        for e in p.give:
            ctx = market.players.get(e.player_id) or player_context(market, e, currency=currency, per_week=per_week)
            if ctx is None:
                continue
            if ctx.projection_over_waiver is None:
                if ctx.scarcity == VERY_SCARCE and not economics_says_scarce:
                    p.caveats.append(f"Replacement context: {e.name} — {ctx.clause()}; nothing on waivers replaces him.")
                continue
            if ctx.scarcity in (SCARCE, VERY_SCARCE) and ctx.projection_over_waiver >= UNDERSTATED_MIN_OVER_WAIVER:
                if not economics_says_scarce:
                    p.caveats.append(f"Replacement context: {e.name} is {ctx.clause()}; replacing him from this wire would cost that much.")
            elif ctx.projection_over_waiver <= OVERSTATED_MAX_OVER_WAIVER:
                p.rationale_for_me.append(f"Replacement context: {e.name} is {ctx.clause()} — cheap to replace from this league's wire.")
        for e in p.receive:
            ctx = player_context(market, e, currency=currency, per_week=per_week)
            if ctx is None or ctx.projection_over_waiver is None:
                continue
            if ctx.projection_over_waiver >= UNDERSTATED_MIN_OVER_WAIVER:
                p.rationale_for_me.append(f"Replacement context: {e.name} arrives {ctx.clause()}.")
            elif ctx.projection_over_waiver < 0:
                p.caveats.append(f"Replacement context: {e.name} projects {ctx.clause()} — waivers offer better production here.")
            elif ctx.projection_over_waiver <= OVERSTATED_MAX_OVER_WAIVER:
                p.caveats.append(f"Replacement context: {e.name} is only {ctx.clause()} — waivers offer nearly the same production here.")


def _annotate_waivers_with_replacement(
    targets: list[WaiverTarget], market: ReplacementMarket, currency: str, per_week: int, all_players: dict,
    directions: dict[tuple[str, str], str] | None = None,
) -> None:
    """`directions` receives which side each note argues for, so provenance
    never has to guess it from the wording."""
    directions = directions if directions is not None else {}
    for t in targets:
        ctx = player_context(market, _entry_from_target(t, all_players), currency=currency, per_week=per_week)
        if ctx is None:
            continue
        if ctx.scarcity in (SCARCE, VERY_SCARCE):
            note = f"{t.position} market is {ctx.scarcity} here: an add at this position matters more than his rank alone suggests"
            t.notes.append(note)
            directions[(t.player_id, note)] = FOR
        elif ctx.scarcity == ABUNDANT:
            if t.priority_tier in (MUST_ADD, STRONG_ADD):
                note = f"{t.position} market is Abundant here (comparable production is usually on waivers)"
            else:
                note = f"{t.position} market is Abundant here: comparable production is usually on waivers, so don't overspend"
            t.notes.append(note)
            directions[(t.player_id, note)] = AGAINST


def _annotate_clogs_with_replacement(clogs: list[RosterClog], market: ReplacementMarket) -> None:
    for c in clogs:
        s = market.scarcity_of(c.entry.position)
        if s == ABUNDANT:
            c.reasons.append(f"{c.entry.position} replacements are Abundant on this wire")
        elif s in (SCARCE, VERY_SCARCE):
            c.reasons.append(f"but {c.entry.position} replacements are {s} here — keep unless the spot is needed")


def _build_source_views(
    table, currency: str, my_roster: ValuedRoster, proposals: list[TradeProposal], targets: list[WaiverTarget]
) -> dict[str, SourceView]:
    views: dict[str, SourceView] = {}
    pieces = [(e.player_id, e.name, e.position) for e in my_roster.entries]
    pieces += [(e.player_id, e.name, e.position) for p in proposals for e in (*p.give, *p.receive)]
    pieces += [(t.player_id, t.name, t.position) for t in targets]
    for pid, name, position in pieces:
        if pid in views:
            continue
        v = source_view(name, position, lookup(table, name), currency)
        if v.describe() is not None:
            views[pid] = v
    return views


def _annotate_proposals_with_sources(proposals: list[TradeProposal], views: dict[str, SourceView]) -> None:
    """Disagreement is a caveat unless it points the way the trade already
    goes: a market-above-projection piece is one to sell, a
    projection-above-market piece one to buy."""
    for p in proposals:
        for e in p.give:
            v = views.get(e.player_id)
            if v is None or not v.disagrees:
                continue
            text = f"Sources on {e.name}: {v.describe()}"
            if v.direction == MARKET_ABOVE_PROJECTION:
                p.rationale_for_me.append(f"{text} — the market pays more than the projection supports, which favours selling.")
            else:
                p.caveats.append(f"{text}.")
        for e in p.receive:
            v = views.get(e.player_id)
            if v is None or not v.disagrees:
                continue
            text = f"Sources on {e.name}: {v.describe()}"
            if v.direction == PROJECTION_ABOVE_MARKET:
                p.rationale_for_me.append(f"{text} — the projection sees more than the market charges, which favours buying.")
            else:
                p.caveats.append(f"{text}.")


def _annotate_ladders_with_sources(ladders: dict[int, NegotiationLadder], views: dict[str, SourceView]) -> None:
    for ladder in ladders.values():
        for step in (ladder.opening, ladder.fallback, ladder.walk_away):
            if step is None:
                continue
            split = [
                f"{e.name} ({views[e.player_id].describe()})"
                for e in step.players
                if e.player_id in views and views[e.player_id].disagrees
            ]
            if split:
                step.source_note = "sources split on " + "; ".join(split)


def _annotate_proposals_with_pick_opportunity(proposals: list[TradeProposal], opportunity: PickOpportunity) -> None:
    """A proposal spending one of my picks says what that pick is to my
    roster: a Strategic pick is a caveat, a Spendable one is a point in
    favour. Never a veto."""
    for p in proposals:
        for pick in p.give_picks:
            a = opportunity.assessment_for(pick)
            if a is None:
                continue
            if a.classification == STRATEGIC:
                p.caveats.append(f"{pick.name} is Strategic for your roster — {a.reason}. Spend it knowingly, not as a throw-in.")
            elif a.classification == SPENDABLE:
                p.rationale_for_me.append(f"{pick.name} is Spendable for your roster — {a.reason}.")


def _annotate_proposals_with_league_economy(
    proposals: list[TradeProposal], economy: LeagueEconomy, rosters: dict[int, ValuedRoster]
) -> None:
    """What this league's own transaction record says about the counterparty
    — added to the "why they say yes" side, never to the acceptance bucket."""
    by_username = {r.owner_username: r.roster_id for r in rosters.values() if r.owner_username}
    for p in proposals:
        rid = by_username.get(p.target_username)
        m = economy.managers.get(rid) if rid is not None else None
        if m is None or not m.labels:
            continue
        p.rationale_for_them.append(f"This league's transaction record: {m.describe()}.")


def _annotate_proposals_with_bench_surplus(proposals: list[TradeProposal], leverage: LineupLeverage) -> None:
    """A give-piece that's bench surplus is the best kind of give: it
    costs the starting lineup nothing. Say so on the proposal."""
    surplus_by_id = {s.entry.player_id: s for s in leverage.bench_surplus}
    for p in proposals:
        for e in p.give:
            s = surplus_by_id.get(e.player_id)
            if s is None:
                continue
            p.rationale_for_me.append(
                f"Converts bench surplus: {e.name} projects at {s.ratio:.0%} of your weakest eligible starter "
                f"({s.displaced_starter.name}, {slot_label(s.displaced_slot)}) but can't crack the lineup, so moving him "
                "costs you no starting production."
            )


def _safe_build_league_report_data(
    storage: Storage, engine: ValuationEngine, league: LeagueInfo, current_week: int | None,
    schedule: Schedule | None = None, **kwargs,
) -> LeagueReportData:
    """One league's bad data (a malformed Sleeper payload, an unexpected
    None somewhere in the valuation chain) must not blank the whole daily
    report for the other nine leagues — this is the sole seam where an
    unhandled exception anywhere in the roster/status/trade/waiver pipeline
    is caught and downgraded to a per-league LeagueReportData.error, the
    same degraded-but-visible path already used for "my roster not found".
    """
    try:
        return build_league_report_data(storage, engine, league, current_week, schedule, **kwargs)
    except Exception as exc:
        logger.exception("Report generation failed for %s", league.name)
        return LeagueReportData(league=league, error=f"Report generation failed: {exc}")


def _season_of(storage: Storage, leagues: list[LeagueInfo]) -> int | None:
    for league in leagues:
        season = (storage.get_league(league.league_id) or {}).get("season")
        if season:
            try:
                return int(season)
            except (TypeError, ValueError):
                continue
    return None


def build_weekly_report_data(
    storage: Storage, engine: ValuationEngine, leagues: list[LeagueInfo] = LEAGUES, *,
    with_nfl_schedule: bool = True, with_usage: bool | None = None,
) -> WeeklyReportData:
    """`with_nfl_schedule=False` skips every non-ranking external asset
    (the schedule and, unless `with_usage` says otherwise, the nflverse
    usage layer) — the test suite's no-network path."""
    now = dt.datetime.now(dt.timezone.utc)
    current_week_raw = storage.get_meta("current_week")
    current_week = int(current_week_raw) if current_week_raw else None
    if with_usage is None:
        with_usage = with_nfl_schedule

    # One NFL schedule per run (cached daily), shared by every league.
    schedule: Schedule | None = None
    season = _season_of(storage, leagues)
    if season is not None and with_nfl_schedule:
        schedule = load_schedule(season)
    usage, crosswalk, usage_note, crosswalk_note = _load_usage_layer(storage, leagues, season if with_usage else None)

    # Grade every input BEFORE the leagues build, so a feature whose source
    # is Unavailable is skipped rather than computed from nothing.
    health = build_health(
        engine=engine, storage=storage,
        schedule_snapshot=load_snapshot("nflverse_schedule") if with_nfl_schedule else None,
        usage_health=cached_health(season) if (season is not None and with_usage) else None,
        now=now,
        current_week=current_week,
    )
    suppressed = suppressed_features(health)
    for feature, why in suppressed.items():
        logger.warning("%s suppressed this run: %s", feature, why)

    league_data = [
        _safe_build_league_report_data(
            storage, engine, league, current_week, schedule, usage=usage, crosswalk=crosswalk, suppressed=suppressed
        )
        for league in leagues
    ]
    portfolio = build_portfolio_exposure(
        (ld.league.name, ld.roster, ld.lineup) for ld in league_data if ld.drafted and ld.roster is not None
    )
    _annotate_recommendations_with_exposure(league_data, portfolio)
    asymmetries = build_asymmetries(portfolio, league_data)
    # Market velocity reads the snapshot history (today's own file is
    # replaced by this run's values, so a same-day re-run counts once).
    today = now.date().isoformat()
    history = load_snapshots(before_date=today)
    for ld in league_data:
        if ld.error or not ld.drafted or ld.roster is None:
            continue
        try:
            ld.velocity = build_velocities(history, ld, current_week=current_week, today=today)
            annotate_league(ld, ld.velocity)
        except Exception:  # an annotation pass must never blank a league that already built
            logger.exception("Market velocity skipped for %s", ld.league.name)
    # Roles against the market (velocity and the source direction are
    # both known now), then the sparse role annotations.
    for ld in league_data:
        if ld.error or not ld.drafted or not ld.role_trends:
            continue
        try:
            _annotate_with_roles(ld)
        except Exception:
            logger.exception("Role annotations skipped for %s", ld.league.name)
    # Conflicts last: they read every annotation above (exposure included).
    for ld in league_data:
        if ld.error or not ld.drafted:
            continue
        try:
            ld.conflicts = detect_conflicts(ld)
        except Exception:
            logger.exception("Conflict detection skipped for %s", ld.league.name)

    report = WeeklyReportData(
        generated_at=now,
        current_week=current_week,
        source_freshness=engine.source_freshness(),
        ff_status=ff_dynasty_status(),
        leagues=league_data,
        portfolio=portfolio,
        asymmetries=asymmetries,
        health=health,
        suppressed=suppressed,
        freshness_lines=freshness_lines(health),
        # The health block already says the feed isn't published yet; don't say it twice.
        usage_note=usage_note if not any(s.family == "nflverse_usage" and s.expected_absent for s in health.signals) else None,
        crosswalk_note=crosswalk_note,
    )
    # Provenance reads every annotation above (conflicts and exposure
    # included) and the priority key reads provenance; both need the
    # report for the current week and the portfolio.
    stale_sources = freshness_by_source(health)
    for ld in league_data:
        if ld.error or not ld.drafted:
            continue
        try:
            ld.provenance = build_provenance(ld, report, schedule=schedule, freshness_by_source=stale_sources)
            ld.priorities = {
                (kind, key): classify(kind, ld, report, provenance=prov, key=key) for (kind, key), prov in ld.provenance.items()
            }
        except Exception:
            logger.exception("Provenance skipped for %s", ld.league.name)
    report.priority_actions = build_priority_actions(league_data, report)
    report.snapshot = build_snapshot(report)
    # Skip today's own snapshot so a same-day re-run still diffs against
    # the previous day (daily_run overwrites today's file, keeping runs
    # idempotent).
    report.delta = compute_delta(load_latest_snapshot(before_date=now.date().isoformat()), report.snapshot)
    _attach_watchlist(report, now)
    _attach_ledger(report, storage, now)
    return report


def _load_usage_layer(
    storage: Storage, leagues: list[LeagueInfo], season: int | None
) -> tuple[UsageData | None, dict[str, PlayerIds] | None, str | None, str | None]:
    """The season's nflverse usage rows and the Sleeper -> gsis crosswalk,
    once per run. (None, None, note, None) until the season has games —
    the note is the ONE place the report says so; nothing per player."""
    if season is None:
        return None, None, None, None
    try:
        usage = load_usage(season)
    except Exception:
        logger.exception("Usage layer skipped")
        return None, None, None, None
    if usage is None or not usage.latest_week:
        return None, None, NO_HISTORY_NOTE, None
    try:
        ff_rows, nfl_rows = load_crosswalk_rows()
    except Exception:
        logger.exception("Crosswalk sources unavailable; Sleeper's own gsis ids only")
        ff_rows, nfl_rows = [], []
    # The players that matter: everyone rostered in these leagues plus the
    # trending adds the waiver engine draws from. A free agent outside both
    # (an insurance row, a stash) simply gets no role line.
    only_ids: set[str] = set()
    for league in leagues:
        for roster in storage.get_rosters(league.league_id):
            only_ids.update(str(p) for p in (roster.get("players") or []))
    only_ids.update(str(row["player_id"]) for row in storage.get_trending("add"))
    all_players = storage.get_all_players()
    # Free agents the report may name (insurance, stash, streamers) are
    # anyone active at a fantasy position — a few thousand, not 12k.
    only_ids.update(
        pid for pid, p in all_players.items()
        if (p.get("position") in FREE_AGENT_POSITIONS) and p.get("team") and p.get("status") in (None, "Active")
    )
    crosswalk, xreport = build_crosswalk(all_players, ff_rows=ff_rows, nfl_rows=nfl_rows, only_ids=only_ids)
    return usage, crosswalk, None, f"Player id crosswalk: {xreport.describe()}"


# The source-disagreement direction labels, as a market direction for the
# role/market cross: a market priced above the projection is a market
# that has moved UP on him relative to what he produces.
_SOURCE_DIRECTION = {MARKET_ABOVE_PROJECTION: "up", PROJECTION_ABOVE_MARKET: "down"}


def _annotate_with_roles(ld: LeagueReportData) -> None:
    """Sparse by design: only a notable role trend (Rising / Surging /
    Falling / Collapsing) is written anywhere, and it goes on the side of
    the recommendation it argues for. The market cross rides on the same
    line rather than becoming a second bullet."""
    for pid, trend in ld.role_trends.items():
        velocity = ld.velocity.get(pid)
        view = ld.source_views.get(pid)
        cross = market_cross(
            trend, value_direction=None,
            velocity_label=velocity.label if velocity is not None else None,
            source_direction=_SOURCE_DIRECTION.get(view.direction) if view is not None and view.direction else None,
        )
        if cross:
            ld.role_market[pid] = cross

    def line(pid: str, trend: RoleTrend) -> str:
        cross = ld.role_market.get(pid)
        return f"Role: {trend.describe()}" + (f" — {cross}" if cross else "")

    for t in ld.waiver_targets:
        trend = ld.role_trends.get(t.player_id)
        if trend is not None and trend.notable:
            note = line(t.player_id, trend)
            t.notes.append(note)
            ld.note_directions[(t.player_id, note)] = FOR if trend.rising else AGAINST
    for p in ld.proposals:
        for e in p.give:
            trend = ld.role_trends.get(e.player_id)
            if trend is None or not trend.notable:
                continue
            if trend.rising:
                p.caveats.append(f"{line(e.player_id, trend)} — {e.name}'s role is growing, which argues against selling now.")
            else:
                p.rationale_for_me.append(f"{line(e.player_id, trend)} — {e.name}'s role is shrinking, which favours selling.")
        for e in p.receive:
            trend = ld.role_trends.get(e.player_id)
            if trend is None or not trend.notable:
                continue
            if trend.rising:
                p.rationale_for_me.append(f"{line(e.player_id, trend)} — {e.name}'s role is growing, which favours buying.")
            else:
                p.caveats.append(f"{line(e.player_id, trend)} — {e.name}'s role is shrinking; the price may not have caught up.")
    for c in ld.stash:
        trend = ld.role_trends.get(c.entry.player_id)
        if trend is not None and trend.notable:
            c.reasons.append(line(c.entry.player_id, trend))


def _attach_watchlist(report: WeeklyReportData, now: dt.datetime) -> None:
    """Fold this run's near-misses into the stored watchlist, in memory.
    Only daily_run writes the file, after a complete run."""
    try:
        existing = load_watchlist()
        ld_by_league = {ld.league.league_id: ld for ld in report.leagues if not ld.error and ld.drafted}
        found = [c for ld in ld_by_league.values() for c in watch_candidates(ld, report, role_trends=ld.role_trends)]
        report.watchlist = update_watchlist(existing, found, now=now, ld_by_league=ld_by_league)
        report.watchlist_new, report.watchlist_watching = watchlist_lines(report.watchlist)
    except Exception:
        logger.exception("Watchlist skipped")


def _attach_ledger(report: WeeklyReportData, storage: Storage, now: dt.datetime) -> None:
    """Record this run's recommendations and stamp the open ones with what
    Sleeper shows, in memory; daily_run persists after a complete run. The
    outcome facts read the snapshot history and never judge."""
    try:
        ledger = load_ledger()
        ok = [ld for ld in report.leagues if not ld.error and ld.drafted and ld.roster is not None]
        # Observe BEFORE recording this run's entries: a recommendation made
        # this minute has had no chance to be acted on, and stamping it
        # "Still Available" would be a fact that means nothing.
        observe(
            ledger,
            transactions_by_league={ld.league.league_id: storage.get_all_transactions(ld.league.league_id) for ld in ok},
            rosters_by_league={ld.league.league_id: storage.get_rosters(ld.league.league_id) for ld in ok},
            my_roster_ids={ld.league.league_id: ld.roster.roster_id for ld in ok},
            now=now,
        )
        new, _refreshed = merge_entries(ledger, build_entries(report), run_id=now.isoformat())
        role_labels = {pid: trend.label for ld in ok for pid, trend in ld.role_trends.items()}
        report.ledger = ledger
        report.ledger_new = new
        report.ledger_summary = ledger_summary(ledger)
        report.outcome_facts = build_outcome_facts(ledger, load_snapshots(), now=now, report=report, role_labels=role_labels)
    except Exception:
        logger.exception("Decision ledger skipped")


def _annotate_recommendations_with_exposure(leagues: list[LeagueReportData], portfolio: PortfolioExposure) -> None:
    """Cross-league pass: a trade target or waiver add that would push a
    player across an exposure threshold gets a note on that
    recommendation. Has to run after every league is built — no single
    league knows what the others hold.
    """
    for ld in leagues:
        if ld.error or not ld.drafted:
            continue
        for p in ld.proposals:
            for e in p.receive:
                note = acquisition_exposure_note(portfolio, e.player_id, position=e.position)
                if note:
                    p.caveats.append(note)
        for t in ld.waiver_targets:
            note = acquisition_exposure_note(portfolio, t.player_id, position=t.position, compact=True)
            if note:
                text = note[0].upper() + note[1:]
                t.notes.append(text)
                ld.note_directions[(t.player_id, text)] = AGAINST
