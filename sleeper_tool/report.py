"""Generates the consolidated weekly Markdown report — one section per
league, meant to be actually read, not a raw data dump.
"""
from __future__ import annotations

import datetime as dt

from sleeper_tool.config import LEAGUES, LeagueInfo
from sleeper_tool.formatting import ordinal_pct
from sleeper_tool.report_data import LeagueReportData, WeeklyReportData, build_weekly_report_data
from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.storage import Storage
from sleeper_tool.trade_engine import TradeProposal, percentile_for_currency, value_label_for_currency
from sleeper_tool.valuation import ValuationEngine
from sleeper_tool.waiver_engine import WaiverTarget

TREND_ARROW = {"rising": "↑", "down": "↓", "no change": "→"}


def _age_str(age: dt.timedelta) -> str:
    hours = age.total_seconds() / 3600
    if hours < 1:
        return f"{int(age.total_seconds() // 60)}m old"
    if hours < 48:
        return f"{hours:.0f}h old"
    return f"{age.days}d old"


def _render_roster_snapshot(roster: ValuedRoster, currency: str) -> list[str]:
    label = value_label_for_currency(currency)
    lines = [f"**Roster snapshot** ({label}):", ""]
    lines.append("| Slot | Player | Pos | Team | Value | Trend |")
    lines.append("|---|---|---|---|---|---|")
    starters = sorted(roster.starters(), key=lambda e: -(percentile_for_currency(e.value, currency) or 0))
    for e in starters:
        pctl = percentile_for_currency(e.value, currency)
        val_str = ordinal_pct(pctl) if pctl is not None else "unranked"
        arrow = TREND_ARROW.get(e.value.trend or "", "")
        lines.append(f"| Start | {e.name} | {e.position or '?'} | {e.team or '-'} | {val_str} | {arrow} |")

    bench = sorted(roster.bench(), key=lambda e: -(percentile_for_currency(e.value, currency) or 0))
    if bench:
        top_bench = ", ".join(
            f"{e.name} ({ordinal_pct(percentile_for_currency(e.value, currency))})" for e in bench[:4]
        )
        lines.append("")
        lines.append(f"Bench ({len(bench)}): {top_bench}{', ...' if len(bench) > 4 else ''}")

    taxi = [e for e in roster.entries if e.is_taxi]
    if taxi:
        lines.append(f"Taxi squad: {', '.join(e.name for e in taxi)}")
    reserve = [e for e in roster.entries if e.is_reserve]
    if reserve:
        lines.append(f"IR/Reserve: {', '.join(e.name for e in reserve)}")

    return lines


def _render_trade_proposal(p: TradeProposal, index: int) -> list[str]:
    lines = [f"**Offer {index}: {p.summary_line()}**", ""]
    ratio_note = "balanced" if 0.9 <= p.value_ratio <= 1.1 else ("favors me" if p.value_ratio < 0.9 else "slight overpay")
    lines.append(f"*Value: {p.my_value_total:.0f} vs {p.their_value_total:.0f} ({ratio_note})*")
    lines.append("")
    lines.append("Why it works for me:")
    for r in p.rationale_for_me:
        lines.append(f"- {r}")
    lines.append(f"Why {p.target_username} plausibly says yes:")
    for r in p.rationale_for_them:
        lines.append(f"- {r}")
    if p.caveats:
        lines.append("")
        lines.append("Caveats:")
        for c in p.caveats:
            lines.append(f"- ⚠️ {c}")
    return lines


def _render_waiver_targets(targets: list[WaiverTarget]) -> list[str]:
    if not targets:
        return ["No standout waiver targets this week."]
    lines = ["| Player | Pos | Team | Why |", "|---|---|---|---|"]
    for t in targets[:6]:
        lines.append(f"| {t.name} | {t.position or '?'} | {t.team or '-'} | {t.reason} |")
    return lines


def render_league_section(data: LeagueReportData) -> list[str]:
    lines = [f"## {data.league.name}", ""]

    if data.error:
        lines.append(f"_{data.error}_")
        lines.append("")
        return lines

    lines.append(f"*{data.league.kind.capitalize()} · {data.fmt_desc}*")
    lines.append("")

    if data.team_status:
        lines.append(f"**Team status: {data.team_status.status.upper()}** — {data.team_status.reason}")
        lines.append("")

    if not data.drafted:
        lines.append("**Not drafted yet this season** — check back after your draft for roster/trade/waiver analysis.")
        lines.append("")
        return lines

    lines.extend(_render_roster_snapshot(data.roster, data.currency))
    lines.append("")

    lines.append("### Trade offers")
    lines.append("")
    if data.proposals:
        for i, p in enumerate(data.proposals, start=1):
            lines.extend(_render_trade_proposal(p, i))
            lines.append("")
    else:
        lines.append("No trade offers cleared the value-match bar this week.")
        lines.append("")

    lines.append("### Waiver targets")
    lines.append("")
    lines.extend(_render_waiver_targets(data.waiver_targets))
    lines.append("")

    lines.append("### Time-sensitive")
    lines.append("")
    if data.time_sensitive:
        for n in data.time_sensitive:
            lines.append(f"- **{n.player_name}**: {n.note}")
    else:
        lines.append("Nothing flagged.")
    lines.append("")

    return lines


def render_weekly_report(report: WeeklyReportData) -> str:
    lines = [
        "# Weekly Fantasy Football Report",
        "",
        f"_Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Data freshness",
        "",
    ]
    for source, age in report.source_freshness.items():
        lines.append(f"- {source}: {_age_str(age)}")
    lines.append(f"- ff_dynasty_pass (manual CSV): {report.ff_status}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for league_data in report.leagues:
        lines.extend(render_league_section(league_data))
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def generate_weekly_report(
    storage: Storage, engine: ValuationEngine, leagues: list[LeagueInfo] = LEAGUES
) -> str:
    report = build_weekly_report_data(storage, engine, leagues)
    return render_weekly_report(report)
