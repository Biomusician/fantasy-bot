"""Shared data-gathering layer for the weekly report. Both the Markdown
renderer (report.py) and the HTML dashboard (html_report.py) consume the
same WeeklyReportData so the two outputs can never drift out of sync with
each other or re-implement the underlying business logic twice.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sleeper_tool.config import LEAGUES, LeagueInfo, MY_USER_ID
from sleeper_tool.rankings.ff_dynasty_pass import ff_dynasty_status
from sleeper_tool.roster_analysis import ValuedRoster, build_all_valued_rosters
from sleeper_tool.storage import Storage
from sleeper_tool.team_status import TeamStatusResult, classify_team_status
from sleeper_tool.trade_engine import TradeProposal, generate_trade_proposals, value_currency
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


@dataclass
class WeeklyReportData:
    generated_at: dt.datetime
    current_week: int | None
    source_freshness: dict[str, dt.timedelta]
    ff_status: str
    leagues: list[LeagueReportData]
    priority_actions: list[PriorityAction] = field(default_factory=list)


_ACTION_KIND_ORDER = {"alert": 0, "trade": 1, "waiver": 2}


def build_priority_actions(leagues: list[LeagueReportData], *, max_actions: int = 8) -> list[PriorityAction]:
    """Rank the user's highest-value actions across ALL leagues and
    transaction types: high-severity injury/bye alerts first (they're
    time-boxed to this week's lineup lock), then trades with a Good/High
    acceptance rating AND at least Medium valuation confidence (a
    favorable-looking trade built on shaky data isn't a top action), then
    Must-Add waivers. An empty result is a legitimate "nothing urgent
    right now" — this never manufactures activity to avoid an empty list,
    since a synthetic action would be actively misleading.
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
        for p in ld.proposals:
            if p.acceptance_rating in ("High", "Good") and p.confidence in ("High", "Medium"):
                actions.append(PriorityAction(
                    league_name=ld.league.name, kind="trade",
                    headline=p.summary_line(),
                    detail=f"{ld.league.name} — {p.acceptance_rating.lower()} acceptance likelihood, "
                    f"{p.trade_type.replace('_', ' ')}.",
                ))
        for t in ld.waiver_targets:
            if t.priority_tier == "Must Add":
                drop_note = f", drop {t.drop_candidate.name}" if t.drop_candidate else ""
                actions.append(PriorityAction(
                    league_name=ld.league.name, kind="waiver",
                    headline=f"Add {t.name}{drop_note}",
                    detail=f"{ld.league.name} — {t.reason}",
                ))
    actions.sort(key=lambda a: _ACTION_KIND_ORDER.get(a.kind, 9))
    return actions[:max_actions]


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
    waiver_targets = get_waiver_targets(
        storage, engine, league, my_roster, current_week=current_week, waiver_budget=waiver_budget
    )
    time_sensitive = get_time_sensitive_notes(storage, my_roster, current_week=current_week)

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

    return WeeklyReportData(
        generated_at=now,
        current_week=current_week,
        source_freshness=engine.source_freshness(),
        ff_status=ff_dynasty_status(),
        leagues=league_data,
        priority_actions=build_priority_actions(league_data),
    )
