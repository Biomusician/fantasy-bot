"""Shared data-gathering layer for the weekly report. Both the Markdown
renderer (report.py) and the HTML dashboard (html_report.py) consume the
same WeeklyReportData so the two outputs can never drift out of sync with
each other or re-implement the underlying business logic twice.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sleeper_tool.bye_collision import ByeCollision, describe_bye_collision, plan_bye_collisions, positions_covering
from sleeper_tool.config import LEAGUES, LeagueInfo, MY_USER_ID
from sleeper_tool.contender_insurance import (
    InsuranceRecommendation,
    free_agent_candidates,
    identify_fragile_starters,
    merge_insurance_into_waiver_targets,
)
from sleeper_tool.decision_delta import DecisionDelta, build_snapshot, compute_delta, load_latest_snapshot
from sleeper_tool.league_economy import LeagueEconomy, build_league_economy
from sleeper_tool.lineup_leverage import LineupLeverage, build_lineup_leverage
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup
from sleeper_tool.move_impact import PREVIEWED_WAIVER_TIERS, MoveImpact, preview_add_drop, preview_trade, snapshot_roster
from sleeper_tool.playoff_leverage import PlayoffLeverage, classify_playoff_leverage
from sleeper_tool.portfolio_exposure import PortfolioExposure, acquisition_exposure_note, build_portfolio_exposure
from sleeper_tool.rankings.ff_dynasty_pass import ff_dynasty_status
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster, build_all_valued_rosters
from sleeper_tool.roster_clog import RosterClog, identify_roster_clogs
from sleeper_tool.storage import Storage
from sleeper_tool.team_status import CONTENDER, TeamStatusResult, classify_team_status
from sleeper_tool.trade_engine import DropCandidate, TradeProposal, generate_trade_proposals, identify_drop_candidates, value_currency
from sleeper_tool.valuation import LeagueFormat, ValuationEngine
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget, get_time_sensitive_notes, get_waiver_targets

logger = logging.getLogger(__name__)


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
    insurance: list[InsuranceRecommendation] = field(default_factory=list)  # contenders only; also merged into waiver_targets
    bye_collision: ByeCollision | None = None  # earliest look-ahead week with a Bye Hole; also a time_sensitive note
    league_economy: LeagueEconomy | None = None  # per-manager trade/pick/position tendencies, current season
    trade_impacts: list[MoveImpact | None] = field(default_factory=list)  # parallel to proposals; None = below preview bar
    waiver_impacts: dict[str, MoveImpact] = field(default_factory=dict)  # by waiver target player_id (Must Add only)
    playoff: PlayoffLeverage | None = None  # standings position vs the playoff cut; None until 3 games are played
    error: str | None = None


@dataclass
class PriorityAction:
    """One entry in the cross-league "best moves right now" list — without
    this, a user with 10 leagues has to click into each one individually
    to find anything urgent; nothing in this module previously aggregated
    across leagues at all.
    """
    league_name: str
    kind: str  # "alert" | "trade" | "waiver"
    headline: str
    detail: str
    rank: int = 0  # lower = more important WITHIN this kind — quality tiebreak, not shown to the user


@dataclass
class WeeklyReportData:
    generated_at: dt.datetime
    current_week: int | None
    source_freshness: dict[str, dt.timedelta]
    ff_status: str
    leagues: list[LeagueReportData]
    priority_actions: list[PriorityAction] = field(default_factory=list)
    portfolio: PortfolioExposure | None = None  # cross-league player concentration
    delta: DecisionDelta | None = None  # vs the last complete run's snapshot; None on a first run
    snapshot: dict | None = None  # this run's decision snapshot, for daily_run to persist after a complete run


_ACTION_KIND_ORDER = {"alert": 0, "trade": 1, "waiver": 2, "roster": 3}


_CONFIDENCE_RANK = {"High": 0, "Medium": 1, "Low": 2}


# A week with several Good/High-confidence trades can otherwise fill every
# slot with trades alone, leaving a Must-Add waiver or a Strong-Drop
# invisible even though it exists in every league that week -- reserve a
# minimum floor per kind so those still surface. Trades and alerts need no
# floor: alerts are already sorted first, and trades routinely fill the
# rest of the budget on their own.
_KIND_FLOOR = {"waiver": 2, "roster": 2}
DEADLINE_WINDOW_RANK_BOOST = 100  # sorts a deadline-window team's trades ahead of every other trade action


def build_priority_actions(leagues: list[LeagueReportData], *, max_actions: int = 8) -> list[PriorityAction]:
    """Rank the user's highest-value actions across ALL leagues and
    transaction types: high-severity injury/bye alerts first (they're
    time-boxed to this week's lineup lock), then trades with a Good/High
    acceptance rating AND at least Medium valuation confidence (a
    favorable-looking trade built on shaky data isn't a top action), then
    Must-Add waivers, then Strong-Drop roster cleanup. Within each kind,
    ranked by actual quality (trade acceptance tier then confidence;
    waiver percentile) rather than league-iteration order — otherwise
    truncating to max_actions could silently drop an objectively better
    action from a later-processed league in favor of a weaker one from an
    earlier league. A minimum number of waiver and roster-cleanup slots are
    reserved (_KIND_FLOOR) so a week with many good trades can't crowd every
    other kind out of the list entirely. An empty result is a legitimate
    "nothing urgent right now" — this never manufactures activity to avoid
    an empty list, since a synthetic action would be actively misleading.
    """
    actions: list[PriorityAction] = []
    for ld in leagues:
        if ld.error or not ld.drafted:
            continue
        for note in ld.time_sensitive:
            if note.severity == "high":
                actions.append(PriorityAction(
                    league_name=ld.league.name, kind="alert",
                    headline=f"{note.player_name} — {note.note}",
                    detail=f"{ld.league.name} — check before this week's lineup locks.",
                ))
        # A Bubble / Long Shot team inside its trade deadline window: the
        # trades already generated for it are the time-boxed ones — they
        # lead the trade list and say why. Nothing new is generated.
        deadline_urgent = ld.playoff is not None and ld.playoff.urgent
        for p in ld.proposals:
            if p.acceptance_rating in ("High", "Good") and p.confidence in ("High", "Medium"):
                tier_rank = 0 if p.acceptance_rating == "High" else 1
                detail = f"{ld.league.name} — {p.acceptance_rating.lower()} acceptance likelihood, {p.trade_type.replace('_', ' ')}."
                rank = tier_rank * 10 + _CONFIDENCE_RANK.get(p.confidence, 2)
                if deadline_urgent:
                    detail = f"Deadline Window ({ld.playoff.label}, deadline week {ld.playoff.trade_deadline_week}) — {detail}"
                    rank -= DEADLINE_WINDOW_RANK_BOOST
                actions.append(PriorityAction(league_name=ld.league.name, kind="trade", headline=p.summary_line(), detail=detail, rank=rank))
        for t in ld.waiver_targets:
            if t.priority_tier == "Must Add":
                drop_note = f", drop {t.drop_candidate.name}" if t.drop_candidate else ""
                pctl = (t.value.dynasty_value_percentile or t.value.redraft_ecr_percentile) if t.value else None
                actions.append(PriorityAction(
                    league_name=ld.league.name, kind="waiver",
                    headline=f"Add {t.name}{drop_note}",
                    detail=f"{ld.league.name} — {t.reason}",
                    rank=-(pctl or 0),  # higher percentile ranks first (more negative sorts earlier)
                ))
        for d in ld.drop_candidates:
            if d.priority == "Strong Drop":
                actions.append(PriorityAction(
                    league_name=ld.league.name, kind="roster",
                    headline=f"Consider dropping {d.entry.name}",
                    detail=f"{ld.league.name} — {'; '.join(d.reasons)}",
                    rank=-len(d.reasons),  # more independent reasons = a more clear-cut cut, sorts first
                ))
    actions.sort(key=lambda a: (_ACTION_KIND_ORDER.get(a.kind, 9), a.rank))

    by_kind: dict[str, list[PriorityAction]] = {}
    for a in actions:
        by_kind.setdefault(a.kind, []).append(a)
    selected: list[PriorityAction] = []
    for kind, floor in _KIND_FLOOR.items():
        selected.extend(by_kind.get(kind, [])[:floor])
    selected_ids = {id(a) for a in selected}
    for a in actions:
        if len(selected) >= max_actions:
            break
        if id(a) not in selected_ids:
            selected.append(a)
            selected_ids.add(id(a))
    selected.sort(key=lambda a: (_ACTION_KIND_ORDER.get(a.kind, 9), a.rank))
    return selected[:max_actions]


def build_league_report_data(
    storage: Storage, engine: ValuationEngine, league: LeagueInfo, current_week: int | None
) -> LeagueReportData:
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
    status_result = classify_team_status(my_roster.roster_id, rosters, currency, storage=storage, engine=engine)
    proposals = generate_trade_proposals(league, rosters, status_result=status_result, storage=storage, engine=engine)
    league_data = storage.get_league(league.league_id) or {}
    waiver_budget = (league_data.get("settings") or {}).get("waiver_budget")
    # Exclude anyone already used as a give-piece in a live trade proposal
    # this run -- otherwise the same player could be told to both trade
    # away for value and cut for nothing in the same report.
    proposed_give_ids = frozenset(e.player_id for p in proposals for e in p.give)
    trending_add_ids = {row["player_id"] for row in storage.get_trending("add")}
    lineup = optimize_lineup(my_roster)
    roster_clogs = identify_roster_clogs(
        my_roster, trending_add_ids=trending_add_ids, exclude_ids=proposed_give_ids, lineup=lineup
    )
    clog_ids = frozenset(c.entry.player_id for c in roster_clogs)
    waiver_targets = get_waiver_targets(
        storage, engine, league, my_roster, current_week=current_week, waiver_budget=waiver_budget, clog_ids=clog_ids
    )
    insurance: list[InsuranceRecommendation] = []
    if status_result.status == CONTENDER:
        free_agents = free_agent_candidates(storage, engine, league, my_roster)
        insurance = identify_fragile_starters(my_roster, free_agents, team_status=status_result.status, lineup=lineup)
        trade_deadline = (league_data.get("settings") or {}).get("trade_deadline")
        deadline_passed = bool(trade_deadline) and current_week is not None and current_week > int(trade_deadline)
        waiver_targets = merge_insurance_into_waiver_targets(
            waiver_targets, insurance, my_roster, current_week=current_week, deadline_passed=deadline_passed, clog_ids=clog_ids
        )
    time_sensitive = get_time_sensitive_notes(storage, my_roster, current_week=current_week)
    bye_collision = plan_bye_collisions(my_roster, current_week=current_week, lineup=lineup)
    if bye_collision is not None:
        # Next week's hole is this week's waiver move; further out is a heads-up.
        severity = "medium" if bye_collision.week == (current_week or 0) + 1 else "low"
        time_sensitive.append(
            TimeSensitiveNote(f"Week {bye_collision.week} bye hole", describe_bye_collision(bye_collision), severity=severity)
        )
        covering = positions_covering(bye_collision)
        for t in waiver_targets:
            if t.position in covering:
                t.reason = f"{t.reason}; would also cover your week {bye_collision.week} bye hole"
    drop_candidates = identify_drop_candidates(my_roster, status_result.status, exclude_ids=proposed_give_ids)
    # A clog that's already a drop candidate is surfaced there; listing him
    # twice under two headings is noise, not extra information.
    drop_ids = {d.entry.player_id for d in drop_candidates}
    roster_clogs = [c for c in roster_clogs if c.entry.player_id not in drop_ids]

    lineup_leverage = build_lineup_leverage(my_roster, lineup=lineup, current_week=current_week)
    if lineup_leverage is not None:
        _annotate_proposals_with_bench_surplus(proposals, lineup_leverage)

    league_economy = build_league_economy(
        rosters, storage.get_all_transactions(league.league_id), storage.get_traded_picks(league.league_id),
        season=str(league_data.get("season") or ""),
    )
    _annotate_proposals_with_league_economy(proposals, league_economy, rosters)

    settings = league_data.get("settings") or {}
    playoff = classify_playoff_leverage(
        my_roster.roster_id, rosters,
        playoff_teams=settings.get("playoff_teams"), playoff_week_start=settings.get("playoff_week_start"),
        trade_deadline=settings.get("trade_deadline"), current_week=current_week,
    )

    before = snapshot_roster(
        my_roster, rosters, current_week=current_week, storage=storage, engine=engine, lineup=lineup, status=status_result.status
    )
    trade_impacts = [
        preview_trade(p, my_roster, rosters, before, current_week=current_week, storage=storage, engine=engine) for p in proposals
    ]
    waiver_impacts: dict[str, MoveImpact] = {}
    all_players = storage.get_all_players()
    for t in waiver_targets:
        if t.priority_tier not in PREVIEWED_WAIVER_TIERS:
            continue
        pdata = all_players.get(t.player_id) or {}
        add_entry = RosterEntry(
            player_id=t.player_id, name=t.name, position=t.position, team=t.team, age=pdata.get("age"),
            years_exp=pdata.get("years_exp"), injury_status=pdata.get("injury_status"), status=pdata.get("status"),
            is_starter=False, is_taxi=False, is_reserve=False, value=t.value,
        )
        drop_id = t.drop_candidate.player_id if t.drop_candidate else None
        label = f"Add {t.name}" + (f", drop {t.drop_candidate.name}" if t.drop_candidate else "")
        waiver_impacts[t.player_id] = preview_add_drop(
            label, add_entry, drop_id, my_roster, rosters, before, current_week=current_week, storage=storage, engine=engine
        )

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
        insurance=insurance,
        bye_collision=bye_collision,
        league_economy=league_economy,
        trade_impacts=trade_impacts,
        waiver_impacts=waiver_impacts,
        playoff=playoff,
    )


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
                f"({s.displaced_starter.name}, {s.displaced_slot}) but can't crack the lineup, so moving him "
                "costs you no starting production."
            )


def _safe_build_league_report_data(
    storage: Storage, engine: ValuationEngine, league: LeagueInfo, current_week: int | None
) -> LeagueReportData:
    """One league's bad data (a malformed Sleeper payload, an unexpected
    None somewhere in the valuation chain) must not blank the whole daily
    report for the other nine leagues — this is the sole seam where an
    unhandled exception anywhere in the roster/status/trade/waiver pipeline
    is caught and downgraded to a per-league LeagueReportData.error, the
    same degraded-but-visible path already used for "my roster not found".
    """
    try:
        return build_league_report_data(storage, engine, league, current_week)
    except Exception as exc:
        logger.exception("Report generation failed for %s", league.name)
        return LeagueReportData(league=league, error=f"Report generation failed: {exc}")


def build_weekly_report_data(
    storage: Storage, engine: ValuationEngine, leagues: list[LeagueInfo] = LEAGUES
) -> WeeklyReportData:
    now = dt.datetime.now(dt.timezone.utc)
    current_week_raw = storage.get_meta("current_week")
    current_week = int(current_week_raw) if current_week_raw else None

    league_data = [_safe_build_league_report_data(storage, engine, league, current_week) for league in leagues]
    portfolio = build_portfolio_exposure(
        (ld.league.name, ld.roster, ld.lineup) for ld in league_data if ld.drafted and ld.roster is not None
    )
    _annotate_recommendations_with_exposure(league_data, portfolio)

    report = WeeklyReportData(
        generated_at=now,
        current_week=current_week,
        source_freshness=engine.source_freshness(),
        ff_status=ff_dynasty_status(),
        leagues=league_data,
        priority_actions=build_priority_actions(league_data),
        portfolio=portfolio,
    )
    report.snapshot = build_snapshot(report)
    report.delta = compute_delta(load_latest_snapshot(), report.snapshot)
    return report


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
                t.reason = f"{t.reason}; {note}"
