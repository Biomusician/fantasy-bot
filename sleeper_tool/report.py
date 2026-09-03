"""Generates the consolidated weekly Markdown report — one section per
league, meant to be actually read, not a raw data dump.
"""
from __future__ import annotations

from sleeper_tool.config import LEAGUES, LeagueInfo
from sleeper_tool.decision_delta import DecisionDelta
from sleeper_tool.formatting import age_str, ordinal_pct
from sleeper_tool.league_economy import LeagueEconomy
from sleeper_tool.lineup_leverage import LineupLeverage
from sleeper_tool.move_impact import MoveImpact
from sleeper_tool.negotiation_ladder import NegotiationLadder
from sleeper_tool.pick_opportunity import PickOpportunity
from sleeper_tool.recommendation_conflicts import CONFLICTED, TRADE, WAIVER, Conflict, conflict_for
from sleeper_tool.portfolio_exposure import PortfolioExposure
from sleeper_tool.replacement_value import ReplacementMarket
from sleeper_tool.report_data import LeagueReportData, PriorityAction, WeeklyReportData, build_weekly_report_data
from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.storage import Storage
from sleeper_tool.streamer_planner import HOLD, SEQUENCE, StreamPlan
from sleeper_tool.trade_engine import DropCandidate, TradeProposal, percentile_for_currency, value_label_for_currency
from sleeper_tool.trade_opportunity_cost import TradeEconomics
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


_DELTA_KIND_LABEL = {"status": "Team status", "roster": "Roster moves", "recommendation": "Recommendations", "valuation": "Value swings (15%+)"}


def _render_delta(delta: DecisionDelta | None) -> list[str]:
    if delta is None:
        return []  # first complete run — nothing to compare against yet
    since = delta.since.strftime("%b %d, %H:%M UTC")
    lines = [f"## Since last run ({since})", ""]
    if not delta.items:
        lines.append("No meaningful changes — same statuses, recommendations, and rosters as last time.")
        lines.append("")
        return lines
    for kind, label in _DELTA_KIND_LABEL.items():
        items = delta.by_kind(kind)
        if not items:
            continue
        lines.append(f"**{label}**")
        for i in items:
            lines.append(f"- {i.league_name}: {i.text}")
        lines.append("")
    return lines


def _render_portfolio_exposure(portfolio: PortfolioExposure | None) -> list[str]:
    if portfolio is None or not portfolio.players:
        return []
    lines = [f"## Portfolio exposure (across {portfolio.total_leagues} leagues)", ""]
    lines.append("Players you hold on several rosters at once — one injury hits all of them. A risk flag and tie-breaker, not a sell signal.")
    lines.append("")
    for p in portfolio.players:
        flags = [f for f in (p.level, "Starting QB in 3+ leagues" if p.qb_start_flag else None) if f]
        flag_str = f" — **{', '.join(flags)}**" if flags else ""
        started = f", starting in {len(p.started_in)}" if p.started_in else ""
        lines.append(f"- **{p.name}** ({p.position or '?'}, {p.team or '-'}) — {p.count} of {portfolio.total_leagues} leagues{started}{flag_str}")
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


_DECISION_MARK = {"Toss-Up": "🟡", "Lean Start": "⚪"}


def _render_lineup_leverage(lev: LineupLeverage | None, currency: str, clauses: dict[str, str] | None = None) -> list[str]:
    if lev is None or (not lev.close_calls and not lev.bench_surplus):
        return []
    clauses = clauses or {}
    lines = [f"**Lineup leverage** — best legal lineup projects ~{lev.weekly_starter_points:.0f} pts/week", ""]
    for d in lev.close_calls:
        lines.append(
            f"- {_DECISION_MARK.get(d.label, '')} **{d.label}** at {d.slot}: {d.starter.name} "
            f"({d.starter_weekly:.1f}/wk) over {d.alternative.name} ({d.alternative_weekly:.1f}/wk)"
            + (" — close enough that matchup should decide" if d.label == "Toss-Up" else "")
            + (f" — {d.schedule_note}" if d.schedule_note else "")
        )
    for s in lev.bench_surplus:
        pctl = ordinal_pct(s.value_percentile) if s.value_percentile is not None else "unranked"
        lines.append(
            f"- **Bench surplus:** {s.entry.name} ({s.entry.position or '?'}, {pctl} {value_label_for_currency(currency)}) "
            f"projects at {s.ratio:.0%} of {s.displaced_starter.name} ({s.displaced_slot}) but sits — "
            "value that could be traded for a starter without costing lineup points"
            + (f" · {clauses[s.entry.player_id]}" if s.entry.player_id in clauses else "")
        )
    lines.append("")
    return lines


def _render_ladder(ladder: NegotiationLadder) -> list[str]:
    def step(s):
        note = f" — {s.starter_note}" if s.starter_note else ""
        note += f" — {s.source_note}" if s.source_note else ""
        return f"{s.asset_names} ({s.outgoing_value:.0f}, {s.ratio:.0%} of what you get · acceptance {s.acceptance}){note}"

    lines = ["Negotiation ladder:"]
    lowball = " — deliberately below value, expect a counter" if ladder.opening.lowball else ""
    lines.append(f"- **Opening:** {step(ladder.opening)}{lowball}")
    if ladder.opening_message:
        lines.append(f"  - _Message:_ {ladder.opening_message}")
    if ladder.fallback:
        lines.append(f"- **Fallback (after a counter):** {step(ladder.fallback)}")
    else:
        lines.append("- **Fallback:** none within 10% of the baseline improves the rating — if the opening is countered, hold or walk")
    if ladder.walk_away:
        lines.append(f"- **Walk away above:** {step(ladder.walk_away)} — the most the engine still calls acceptable; past this you're overpaying")
    else:
        top = "fallback" if ladder.fallback else "opening"
        lines.append(f"- **Walk away above the {top}** — nothing more expensive still rates acceptable")
    return lines


def _render_conflict(conflict: Conflict) -> list[str]:
    return [
        f"⚠️ **{CONFLICTED}**",
        f"- For: {'; '.join(conflict.reasons_for) or 'see the recommendation'}",
        f"- Against: {'; '.join(conflict.reasons_against)}",
        "",
    ]


def _render_trade_proposal(
    p: TradeProposal, index: int, impact: MoveImpact | None = None, ladder: NegotiationLadder | None = None,
    economics: TradeEconomics | None = None, conflict: Conflict | None = None,
) -> list[str]:
    lines = [f"**Offer {index} ({p.trade_type_label}): {p.summary_line()}**", ""]
    lines.append(
        f"*{value_label_for_currency(p.currency)}: {p.my_value_total:.0f} vs {p.their_value_total:.0f} "
        f"({p.balance_label.lower()}) · Acceptance: {p.acceptance_rating} · Confidence: {p.confidence}*"
    )
    if economics is not None:
        lines.append(f"*Economics: {economics.describe()}*")
    lines.append("")
    if conflict is not None:
        lines.extend(_render_conflict(conflict))
    if impact is not None:
        deltas = impact.material_deltas()
        lines.append("What actually changes:")
        if deltas:
            lines.extend(f"- {d}" for d in deltas)
        else:
            lines.append("- nothing material — lineup, depth, status, and roster value all hold; this is a value play, not a lineup play")
        if impact.matchup_note:
            lines.append(f"- {impact.matchup_note}")
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
    if ladder is not None:
        lines.append("")
        lines.extend(_render_ladder(ladder))
    return lines


_TIER_MARK = {"Must Add": "🔴", "Strong Add": "🟠", "Moderate": "🟡", "Speculative": "⚪", "Monitor": "⚪", "Insurance": "🛡️"}


def _render_waiver_targets(
    targets: list[WaiverTarget], impacts: dict[str, MoveImpact] | None = None, conflicts: list[Conflict] | None = None
) -> list[str]:
    if not targets:
        return ["No standout waiver targets this week."]
    impacts = impacts or {}
    conflicts = conflicts or []
    lines = ["| Priority | Player | Pos | Team | Drop | Horizon | FAAB | Why |", "|---|---|---|---|---|---|---|---|"]
    for t in targets:  # already capped by the engine; insurance rows ride along after the cap
        mark = _TIER_MARK.get(t.priority_tier, "")
        drop = t.drop_candidate.name if t.drop_candidate else "—"
        faab = f"{t.suggested_faab_pct}%" if t.suggested_faab_pct is not None else "—"
        reason = t.reason
        impact = impacts.get(t.player_id)
        if impact is not None:
            deltas = impact.material_deltas()
            reason += " · **Impact:** " + ("; ".join(deltas) if deltas else "no lineup change — depth only")
            if impact.matchup_note:
                reason += " · " + impact.matchup_note
        if t.notes:
            reason += " · " + " · ".join(t.notes)
        conflict = conflict_for(conflicts, WAIVER, t.player_id)
        if conflict is not None:
            reason = f"⚠️ **{CONFLICTED}** (against: {'; '.join(conflict.reasons_against)}) · " + reason
        lines.append(
            f"| {mark} {t.priority_tier} | {t.name} | {t.position or '?'} | {t.team or '-'} | {drop} | "
            f"{t.horizon} | {faab} | {reason} |"
        )
    return lines


def _render_streamers(plans: list[StreamPlan]) -> list[str]:
    lines = ["**Streaming plan** (projected points over the window; byes from the NFL schedule, no opponent adjustment):", ""]
    for plan in plans:
        text = plan.describe()
        lines.append(f"- {text}" if plan.recommendation == HOLD else f"- **{text}**")
        if plan.recommendation != HOLD:
            if plan.recommendation == SEQUENCE:
                lines.append(f"  - {plan.sequence.first.entry.name}: {plan.sequence.first.week_text()}")
                lines.append(f"  - {plan.sequence.second.entry.name}: {plan.sequence.second.week_text()}")
            else:
                lines.append(f"  - {plan.single.entry.name}: {plan.single.week_text()}")
            if plan.current is not None:
                lines.append(f"  - {plan.current.entry.name} (current): {plan.current.week_text()}")
    lines.append("")
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


_PICK_MARK = {"Strategic": "🔒", "Useful": "🟡", "Spendable": "🟢"}


def _render_pick_opportunity(opp: PickOpportunity, market: ReplacementMarket | None = None) -> list[str]:
    lines = ["What your 1st/2nd-round picks mean to this roster (an annotation, never a veto):", ""]
    for a in opp.assessments:
        value = f", KTC {a.pick.value:,}" if a.pick.value else ""
        lines.append(f"- {_PICK_MARK.get(a.classification, '')} **{a.classification}: {a.display_name}**{value} — {a.reason}")
    lines.append("")
    weak = [u for u in opp.units if u.bottom_three]
    if weak:
        lines.append("Position units driving this: " + "; ".join(_unit_line(u, market) for u in weak))
        lines.append("")
    return lines


def _unit_line(u, market: ReplacementMarket | None) -> str:
    text = u.describe() + (" (weak-aging)" if u.weak_aging else "")
    scarcity = market.scarcity_of(u.position) if market is not None else None
    return text + (f", {scarcity} replacement market" if scarcity else "")


def _render_replacement_market(market: ReplacementMarket) -> list[str]:
    lines = ["How replaceable each starting position is from THIS league's waiver wire (scarcest first):", ""]
    for m in market.scarcest():
        lines.append(f"- **{m.describe()}**" if m.scarcity in ("Scarce", "Very Scarce") else f"- {m.describe()}")
    if market.understated:
        lines.append("")
        lines.append("Rank understates their edge here: " + "; ".join(f"{c.entry.name} ({c.clause()})" for c in market.understated))
    if market.overstated:
        lines.append("")
        lines.append("Rank overstates their edge here: " + "; ".join(f"{c.entry.name} ({c.clause()})" for c in market.overstated))
    lines.append("")
    return lines


def _render_league_economy(economy: LeagueEconomy | None, my_roster_id: int) -> list[str]:
    if economy is None:
        return []
    lines = []
    if economy.limited_sample:
        lines.append(
            f"_Limited trade-history sample ({economy.total_completed_trades} completed trade"
            f"{'s' if economy.total_completed_trades != 1 else ''} this season) — trader-activity labels suppressed._"
        )
    labelled = economy.labelled()
    if labelled:
        for m in sorted(labelled, key=lambda m: (m.roster_id != my_roster_id, -m.completed_trades)):
            who = f"{m.team_name or m.username or f'roster {m.roster_id}'}{' (you)' if m.roster_id == my_roster_id else ''}"
            lines.append(f"- **{who}** — {m.describe()}")
    elif not economy.limited_sample:
        lines.append("Nothing stands out: no frequent/inactive traders, pick hoarders, or position stockpiles this season.")
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
    if data.playoff:
        window = " · **Deadline Window**" if data.playoff.deadline_window else ""
        lines.append(f"**Playoff picture: {data.playoff.label}**{window} — {data.playoff.reason}")
        lines.append("")

    if not data.drafted:
        lines.append("**Not drafted yet this season** — check back after your draft for roster/trade/waiver analysis.")
        lines.append("")
        return lines

    lines.extend(_render_roster_snapshot(data.roster, data.currency))
    lines.append("")
    lines.extend(_render_lineup_leverage(data.lineup_leverage, data.currency, data.replacement_clauses))
    if data.matchup is not None:
        lines.append(f"**This week's matchup** — {data.matchup.describe()}")
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
            impact = data.trade_impacts[i - 1] if i - 1 < len(data.trade_impacts) else None
            economics = data.trade_economics[i - 1] if i - 1 < len(data.trade_economics) else None
            conflict = conflict_for(data.conflicts, TRADE, str(i - 1))
            trade_lines.extend(_render_trade_proposal(p, i, impact, data.ladders.get(i - 1), economics, conflict))
            trade_lines.append("")
    else:
        trade_lines.append("No trade offers cleared the value-match bar this week.")
        trade_lines.append("")
    if data.consolidations:
        trade_lines.append("**Consolidation (2-for-1) summary** — the 2-for-1 offers above, by lineup gain:")
        trade_lines.append("")
        for c in data.consolidations:
            trade_lines.append(f"- {c.describe()} · {c.freed_slot_note}" + (f" · ⚠️ {c.fragility_note}" if c.fragility_note else ""))
        trade_lines.append("")
    sections.append(("### Trade offers", trade_lines))

    waiver_lines = (
        [f"_{data.waivers_note}_", ""] if data.waivers_note else _render_waiver_targets(data.waiver_targets, data.waiver_impacts, data.conflicts) + [""]
    )
    if data.streamers:
        waiver_lines.extend(_render_streamers(data.streamers))
    if data.defensive_add is not None:
        waiver_lines.extend([f"**🛡 Defensive add** (deny this week's opponent): {data.defensive_add.describe()}", ""])
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

    # Context sections last: they explain and qualify the moves above
    # rather than compete with them.
    if data.roster_clogs:
        clog_lines = [
            f"- **{c.entry.name}** ({c.entry.position or '?'}) — {'; '.join(c.reasons)}" for c in data.roster_clogs
        ] + [""]
        sections.append(("### Roster clogs (dead roster spots)", clog_lines))

    if data.replacement is not None and data.replacement.positions:
        sections.append(("### Replacement market", _render_replacement_market(data.replacement)))

    if data.stash:
        stash_lines = [f"- **{c.label}:** {c.describe()}" for c in data.stash] + [""]
        sections.append(("### Stash board (developmental holds)", stash_lines))

    if data.buyer_boards:
        buyer_lines = []
        for b in data.buyer_boards:
            buyers = "; ".join(f.describe() for f in b.buyers) if b.buyers else "no Strong or Possible fit in this league"
            buyer_lines.append(f"- **{b.candidate.name}** ({b.candidate.position or '?'}): {buyers}")
        buyer_lines.append("")
        sections.append(("### Buyer board (who could pay for your sell-high pieces)", buyer_lines))

    if data.windows is not None:
        sections.append(("### Schedule windows", [data.windows.describe(), ""]))

    if data.pick_opportunity and data.pick_opportunity.assessments:
        sections.append(("### Draft capital", _render_pick_opportunity(data.pick_opportunity, data.replacement)))

    economy_lines = _render_league_economy(data.league_economy, data.roster.roster_id)
    if economy_lines:
        sections.append(("### League economy", economy_lines))

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
    lines.extend(_render_delta(report.delta))
    lines.extend(_render_portfolio_exposure(report.portfolio))
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
