"""Generates the consolidated weekly Markdown report — one section per
league, meant to be actually read, not a raw data dump.
"""
from __future__ import annotations

from sleeper_tool.config import LEAGUES, LeagueInfo
from sleeper_tool.formatting import age_str, ordinal_pct
from sleeper_tool.report_data import LeagueReportData, PriorityAction, WeeklyReportData, build_weekly_report_data
from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.storage import Storage
from sleeper_tool.trade_engine import DropCandidate, TradeProposal, percentile_for_currency, value_label_for_currency
from sleeper_tool.valuation import ValuationEngine
from sleeper_tool.waiver_engine import WaiverTarget

TREND_ARROW = {"rising": "↑", "down": "↓", "no change": "→"}


def _render_priority_actions(actions: list[PriorityAction]) -> list[str]:
    lines = ["## Best moves right now", ""]
    if not actions:
        lines.append("Nothing urgent across any league — hold.")
        lines.append("")
        return lines
    kind_label = {"alert": "ALERT", "trade": "TRADE", "waiver": "WAIVER"}
    for a in actions:
        lines.append(f"- **[{kind_label.get(a.kind, a.kind.upper())}]** {a.headline} — _{a.detail}_")
    lines.append("")
    return lines


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
    if roster.skipped_player_count:
        lines.append(
            f"_Note: {roster.skipped_player_count} roster player(s) couldn't be valued this week "
            "(missing from the player cache) — this snapshot may be incomplete._"
        )

    return lines


def _render_trade_proposal(p: TradeProposal, index: int) -> list[str]:
    lines = [f"**Offer {index} ({p.trade_type_label}): {p.summary_line()}**", ""]
    lines.append(
        f"*{value_label_for_currency(p.currency)}: {p.my_value_total:.0f} vs {p.their_value_total:.0f} "
        f"({p.balance_label.lower()}) · Acceptance: {p.acceptance_rating} · Confidence: {p.confidence}*"
    )
    lines.append("")
    lines.append("Why it works for me:")
    for r in p.rationale_for_me:
        lines.append(f"- {r}")
    lines.append(f"Why {p.target_username} plausibly says yes:")
    for r in p.rationale_for_them:
        lines.append(f"- {r}")
    if p.acceptance_reasons:
        lines.append("")
        lines.append("Acceptance factors:")
        for r in p.acceptance_reasons:
            lines.append(f"- {r}")
    if p.caveats:
        lines.append("")
        lines.append("Caveats:")
        for c in p.caveats:
            lines.append(f"- ⚠️ {c}")
    if p.message:
        lines.append("")
        lines.append(f"> Message to send: _{p.message}_")
    return lines


_TIER_MARK = {"Must Add": "🔴", "Strong Add": "🟠", "Moderate": "🟡", "Speculative": "⚪", "Monitor": "⚪"}


def _render_waiver_targets(targets: list[WaiverTarget]) -> list[str]:
    if not targets:
        return ["No standout waiver targets this week."]
    lines = ["| Priority | Player | Pos | Team | Drop | Horizon | FAAB | Why |", "|---|---|---|---|---|---|---|---|"]
    for t in targets[:8]:
        mark = _TIER_MARK.get(t.priority_tier, "")
        drop = t.drop_candidate.name if t.drop_candidate else "—"
        faab = f"{t.suggested_faab_pct}%" if t.suggested_faab_pct is not None else "—"
        lines.append(
            f"| {mark} {t.priority_tier} | {t.name} | {t.position or '?'} | {t.team or '-'} | {drop} | "
            f"{t.horizon} | {faab} | {t.reason} |"
        )
    return lines


_SEVERITY_MARK = {"high": "🔴", "medium": "🟠", "low": "⚪"}


_DROP_PRIORITY_MARK = {"Strong Drop": "🔴", "Consider Dropping": "🟡"}


def _render_drop_candidates(candidates: list[DropCandidate]) -> list[str]:
    lines = []
    for c in candidates:
        mark = _DROP_PRIORITY_MARK.get(c.priority, "")
        lines.append(f"- {mark} **{c.priority}: {c.entry.name}** ({c.entry.position or '?'}) — {'; '.join(c.reasons)}")
    lines.append("")
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

    # Time-sensitive alerts lead when there's a high-severity one — a
    # scrolling reader shouldn't have to pass two possibly-empty sections
    # (trades, waivers) to reach the one thing that's actually time-boxed
    # to this week's lineup lock.
    has_high_alert = any(n.severity == "high" for n in data.time_sensitive)
    sections: list[tuple[str, list[str]]] = []

    trade_lines = []
    if data.proposals:
        for i, p in enumerate(data.proposals, start=1):
            trade_lines.extend(_render_trade_proposal(p, i))
            trade_lines.append("")
    else:
        trade_lines.append("No trade offers cleared the value-match bar this week.")
        trade_lines.append("")
    sections.append(("### Trade offers", trade_lines))

    waiver_lines = _render_waiver_targets(data.waiver_targets) + [""]
    sections.append(("### Waiver targets", waiver_lines))

    if data.drop_candidates:
        # Only rendered when there's something real to say — no synthetic
        # "nothing to drop" filler, matching the empty-state discipline
        # used elsewhere in this report.
        sections.append(("### Consider dropping", _render_drop_candidates(data.drop_candidates)))

    alert_lines = []
    if data.time_sensitive:
        for n in data.time_sensitive:
            mark = _SEVERITY_MARK.get(n.severity, "")
            alert_lines.append(f"- {mark} **{n.player_name}**: {n.note}")
    else:
        alert_lines.append("Nothing flagged.")
    alert_lines.append("")
    sections.append(("### Time-sensitive", alert_lines))

    if has_high_alert:
        alert_index = next(i for i, (header, _) in enumerate(sections) if header == "### Time-sensitive")
        sections.insert(0, sections.pop(alert_index))

    for header, body in sections:
        lines.append(header)
        lines.append("")
        lines.extend(body)

    return lines


def render_weekly_report(report: WeeklyReportData) -> str:
    lines = [
        "# Weekly Fantasy Football Report",
        "",
        f"_Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    lines.extend(_render_priority_actions(report.priority_actions))
    lines.append("## Data freshness")
    lines.append("")
    for source, age in report.source_freshness.items():
        lines.append(f"- {source}: {age_str(age)} old")
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
