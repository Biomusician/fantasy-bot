"""Shared data-gathering layer for the weekly report. Both the Markdown
renderer (report.py) and the HTML dashboard (html_report.py) consume the
same WeeklyReportData so the two outputs can never drift out of sync with
each other or re-implement the underlying business logic twice.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sleeper_tool.config import LEAGUES, LeagueInfo, MY_USER_ID
from sleeper_tool.rankings.ff_dynasty_pass import ff_dynasty_status
from sleeper_tool.roster_analysis import ValuedRoster, build_all_valued_rosters
from sleeper_tool.storage import Storage
from sleeper_tool.team_status import TeamStatusResult, classify_team_status
from sleeper_tool.trade_engine import TradeProposal, generate_trade_proposals, value_currency
from sleeper_tool.valuation import LeagueFormat, ValuationEngine
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget, get_time_sensitive_notes, get_waiver_targets


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
class WeeklyReportData:
    generated_at: dt.datetime
    current_week: int | None
    source_freshness: dict[str, dt.timedelta]
    ff_status: str
    leagues: list[LeagueReportData]


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
    waiver_targets = get_waiver_targets(storage, engine, league, my_roster, current_week=current_week)
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


def build_weekly_report_data(
    storage: Storage, engine: ValuationEngine, leagues: list[LeagueInfo] = LEAGUES
) -> WeeklyReportData:
    now = dt.datetime.now(dt.timezone.utc)
    current_week_raw = storage.get_meta("current_week")
    current_week = int(current_week_raw) if current_week_raw else None

    league_data = [build_league_report_data(storage, engine, league, current_week) for league in leagues]

    return WeeklyReportData(
        generated_at=now,
        current_week=current_week,
        source_freshness=engine.source_freshness(),
        ff_status=ff_dynasty_status(),
        leagues=league_data,
    )
