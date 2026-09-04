"""Generates the consolidated weekly Markdown report — one section per
league, meant to be actually read, not a raw data dump.

Layout follows one rule: per recommendation, WHAT to do, WHY NOW, WHAT
COULD GO WRONG, HOW URGENT — and everything else behind a collapsed
`<details>` block. The HTML dashboard (html_report.py) renders the same
facts in the same order with the same wording; anything that appears in
one and not the other is a bug, not a style choice. The choices about
which sentence gets the visible slot live in report_views.py so that both
renderers make them identically.
"""
from __future__ import annotations

from sleeper_tool.asset_value import percentile_for_currency, value_label_for_currency
from sleeper_tool.config import LEAGUES, LeagueInfo
from sleeper_tool.decision_delta import DecisionDelta
from sleeper_tool.decision_outcomes import OBSERVED
from sleeper_tool.faab_strategy import bid_cell, bid_detail
from sleeper_tool.formatting import age_str, ordinal_pct
from sleeper_tool.league_economy import LeagueEconomy
from sleeper_tool.lineup_optimizer import slot_label
from sleeper_tool.lineup_leverage import LineupLeverage
from sleeper_tool.move_impact import MoveImpact
from sleeper_tool.negotiation_ladder import NegotiationLadder
from sleeper_tool.pick_opportunity import PickOpportunity
from sleeper_tool.recommendation_conflicts import CONFLICTED, TRADE, WAIVER, Conflict, conflict_for
from sleeper_tool.portfolio_exposure import PortfolioExposure
from sleeper_tool.replacement_value import SCARCE, VERY_SCARCE, ReplacementMarket
from sleeper_tool.report_data import LeagueReportData, PriorityAction, WeeklyReportData, build_weekly_report_data
from sleeper_tool.report_views import (
    grouped_picks,
    pick_group_label,
    ALL_SAME_PRIORITY_NOTE,
    NOTHING_URGENT_NOTE,
    shared_priority,
    split_actions,
    CONFIDENCE_LEGEND,
    CONFIDENCE_MARK,
    MAX_IMPACT_DELTAS,
    action_view,
    claim,
    confidence_caveat,
    health_banner,
    lineup_lines,
    lineup_total,
    lineup_units,
    split_visible,
    waiver_row_view,
    health_state,
    ORDERING_NOTE,
    fact_of,
    without_repeats,
)
from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.storage import Storage
from sleeper_tool.streamer_planner import HOLD, SEQUENCE, StreamPlan
from sleeper_tool.trade_opportunity_cost import TradeEconomics
from sleeper_tool.trade_types import DropCandidate, TradeProposal
from sleeper_tool.valuation import ValuationEngine
from sleeper_tool.waiver_engine import WaiverTarget

TREND_ARROW = {"rising": "↑", "down": "↓", "no change": "→"}

# Markdown supports raw HTML, so the collapsed blocks below are the same
# progressive disclosure the dashboard uses — same summary wording, same
# contents, so the two outputs stay comparable line for line.
_OPEN_DETAILS = "<details>"
_CLOSE_DETAILS = ["", "</details>", ""]


def _summary(title: str, subtitle: str = "") -> list[str]:
    tail = f" · {subtitle}" if subtitle else ""
    return [_OPEN_DETAILS, f"<summary><strong>{title}</strong>{tail}</summary>", ""]


def _heading(title: str, subtitle: str = "") -> str:
    return f"### {title} · {subtitle}" if subtitle else f"### {title}"


# Kind labels are the shared vocabulary with the dashboard.
_KIND_LABEL = {
    "alert": "Alert", "trade": "Trade", "waiver": "Waiver",
    "roster": "Roster", "defensive_add": "Block", "streamer": "Stream",
}


def _render_priority_actions(actions: list[PriorityAction]) -> list[str]:
    """Two lists, not one: what is time-boxed or materially changes the
    lineup ("Do this week"), and the value plays that can wait. Within a
    row: headline (what) → Why now → Against, with the old free-text detail
    demoted to a muted trailing line. A Conflicted move is labelled, never
    led with, and never counts as a to-do."""
    lines = ["## Best moves right now", ""]
    if not actions:
        lines.append("Nothing urgent across any league — hold.")
        lines.append("")
        return lines
    do_now, optional = split_actions(actions)
    shared = shared_priority(actions)

    def rows(group: list[PriorityAction]) -> list[str]:
        out: list[str] = []
        for a in group:
            v = action_view(a)
            flag = " · ⚠️ **Conflicted**" if v.conflicted else ""
            where = f" — *{v.league}*" if v.league else ""
            out.append(f"- **[{_KIND_LABEL.get(a.kind, a.kind.capitalize())}]** {a.headline}{where}{flag}")
            if v.priority and not shared:
                out.append(f"  - Priority: {v.priority}")
            if v.why_now:
                out.append(f"  - Why now: {' · '.join(v.why_now)}")
            if v.against:
                out.append(f"  - Against: {' · '.join(v.against)}")
            if v.conflict_note:
                out.append(f"  - Conflict: {v.conflict_note}")
            if v.detail:
                out.append(f"  - _{v.detail}_")
        return out

    lines.append(f"_{ORDERING_NOTE}_")
    lines.append("")
    if shared:
        lines.append(f"_{ALL_SAME_PRIORITY_NOTE.format(priority=shared)}_")
        lines.append("")
    if do_now:
        lines.append("**Do this week**")
        lines.append("")
        lines.extend(rows(do_now))
        lines.append("")
    if optional:
        lines.append("**Optional value plays**" + ("" if do_now else f" — {NOTHING_URGENT_NOTE}"))
        lines.append("")
        lines.extend(rows(optional))
    lines.append("")
    return lines


def _render_provenance(prov, seen: set | None = None, *, include_context: bool = True) -> list[str]:
    """The For / Against (/ Context) card, one line each. Nothing here is
    computed: the texts are the provenance layer's selections, minus any
    fact `seen` already put on screen (the economics line)."""
    if prov is None or not prov.all_reasons:
        return []
    seen = seen if seen is not None else set()
    lines = []
    groups = [("For", prov.reasons_for), ("Against", prov.reasons_against)]
    if include_context:
        groups.append(("Context", prov.context))
    for label, reasons in groups:
        kept = [r for r in reasons if fact_of(r.text) not in seen]
        claim(seen, [r.text for r in kept])
        if kept:
            lines.append(f"- **{label}:** {' · '.join(f'{r.text} [{r.category}]' for r in kept)}")
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


def _render_portfolio_exposure(portfolio: PortfolioExposure | None, asymmetries=()) -> list[str]:
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
    if asymmetries:
        lines.append("")
        lines.append("Where the same player is cheapest to move (an Abundant/Normal market and a small edge over the wire) versus costliest — a fact for the trade engine to use, not a sell signal:")
        lines.extend(f"- {a.describe()}" for a in asymmetries)
    lines.append("")
    return lines


def _render_lineup(data: LeagueReportData) -> list[str]:
    """The optimizer's best legal lineup — the single source of "who
    starts", which nothing rendered directly before this."""
    games_left = data.lineup_leverage.games_left if data.lineup_leverage is not None else None
    rows = lineup_lines(data.lineup, games_left)
    if not rows:
        return []
    unit = lineup_units(games_left)
    lines = [
        _heading("Best starting lineup", "structural — no bye-week exclusions"),
        "",
        f"Projects ~{lineup_total(data.lineup, games_left):.1f} pts{unit}.",
        "",
    ]
    lines.extend(f"- **{slot}** — {name} · {proj}{unit}" for slot, name, proj in rows)
    lines.append("")
    return lines


def _render_roster_snapshot(roster: ValuedRoster, currency: str) -> list[str]:
    label = value_label_for_currency(currency)
    lines = [_heading("Roster", label), ""]
    lines.append("| Slot | Player | Pos | Team | Value | Trend |")
    lines.append("|---|---|---|---|---|---|")
    starters = sorted(roster.starters(), key=lambda e: -(percentile_for_currency(e.value, currency) or 0))
    flagged = False
    for e in starters:
        pctl = percentile_for_currency(e.value, currency)
        val_str = ordinal_pct(pctl) if pctl is not None else "unranked"
        arrow = TREND_ARROW.get(e.value.trend or "", "")
        mark = f" {CONFIDENCE_MARK}" if confidence_caveat(e.value) else ""
        flagged = flagged or bool(mark)
        lines.append(f"| Start | {e.name}{mark} | {e.position or '?'} | {e.team or '-'} | {val_str} | {arrow} |")
    if flagged:
        lines.append("")
        lines.append(f"_{CONFIDENCE_LEGEND}_")

    bench = sorted(roster.bench(), key=lambda e: -(percentile_for_currency(e.value, currency) or 0))
    if bench:
        # Every bench player, same as the dashboard's bench strip — a
        # truncated list here was a renderer divergence, not a kindness.
        names = ", ".join(
            f"{e.name} ({ordinal_pct(percentile_for_currency(e.value, currency)) if percentile_for_currency(e.value, currency) is not None else 'unranked'})"
            for e in bench
        )
        lines.append("")
        lines.append(f"Bench ({len(bench)}): {names}")

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


def _render_lineup_decisions(dec) -> list[str]:
    """This week's lineup, as a short list of things that need a decision —
    and the verdict when nothing does. Structural leverage (who to trade,
    who sits every week) stays in Lineup leverage below."""
    if dec is None:
        return []
    week = f"week {dec.week}" if dec.week else "this week"
    lines = [_heading("This week's decisions", week), ""]
    if not dec.items:
        lines.extend(["Lineup is already the best legal one — nothing to change before kickoff.", ""])
        return lines
    for item in dec.items:
        lines.append(f"- **{item.kind}** — {item.summary}")
        if item.what_if:
            lines.append(f"    - {item.what_if}")
        for note in item.context:
            lines.append(f"    - _{note}_")
    if dec.close_call_stake:
        lines.append("")
        lines.append(f"_{dec.close_call_stake:.1f} pts/wk rides on the close calls above._")
    lines.append("")
    return lines


def _render_lineup_leverage(
    lev: LineupLeverage | None, currency: str, clauses: dict[str, str] | None = None, *, close_calls_shown: bool = False
) -> list[str]:
    """`close_calls_shown`: this week's decisions block already listed the
    start/sit calls, so only the structural fact (bench surplus, which is
    trade material rather than a lineup change) stays here."""
    calls = [] if close_calls_shown else (lev.close_calls if lev is not None else [])
    if lev is None or (not calls and not lev.bench_surplus):
        return []
    clauses = clauses or {}
    lines = [_heading("Lineup leverage", f"best legal lineup ~{lev.weekly_starter_points:.0f} pts/week"), ""]
    for d in calls:
        lines.append(
            f"- {_DECISION_MARK.get(d.label, '')} **{d.label}** at {slot_label(d.slot)}: {d.starter.name} "
            f"({d.starter_weekly:.1f}/wk) over {d.alternative.name} ({d.alternative_weekly:.1f}/wk)"
            + (" — close enough that matchup should decide" if d.label == "Toss-Up" else "")
            + (f" — {d.schedule_note}" if d.schedule_note else "")
        )
    for s in lev.bench_surplus:
        pctl = ordinal_pct(s.value_percentile) if s.value_percentile is not None else "unranked"
        lines.append(
            f"- **Bench surplus:** {s.entry.name} ({s.entry.position or '?'}, {pctl} {value_label_for_currency(currency)}) "
            f"projects at {s.ratio:.0%} of {s.displaced_starter.name} ({slot_label(s.displaced_slot)}) but sits — "
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


def _bullets(header: str, items: list[str], *, empty: str | None = None) -> list[str]:
    if not items:
        return [header, f"- {empty}"] if empty else []
    return [header, *(f"- {i}" for i in items)]


def _render_trade_proposal(
    p: TradeProposal, index: int, impact: MoveImpact | None = None, ladder: NegotiationLadder | None = None,
    economics: TradeEconomics | None = None, conflict: Conflict | None = None, provenance=None,
) -> list[str]:
    """Visible: what the offer is, how it rates, its economics, why now,
    and what actually changes. Collapsed: the full rationale, acceptance
    factors, caveats, the message to send, and the negotiation ladder.
    Nothing is dropped — a sentence already shown in the Why-now card is
    simply not repeated verbatim underneath it."""
    lines = [f"**Offer {index} ({p.trade_type_label}): {p.summary_line()}**", ""]
    lines.append(
        f"*{value_label_for_currency(p.currency)}: {p.my_value_total:.0f} vs {p.their_value_total:.0f} "
        f"({p.balance_label.lower()}) · Acceptance: {p.acceptance_rating} · Confidence: {p.confidence}*"
    )
    lines.append(f"*To {p.target_team_name or p.target_username}*")
    if economics is not None:
        lines.append(f"*Economics: {economics.describe()}*")
    if conflict is not None:
        lines.append(f"⚠️ **{CONFLICTED}** — see the full rationale below")
    lines.append("")

    # One vocabulary per fact: the economics line and the Why-now card
    # claim their facts first; the collapsed block below never restates
    # one of them in a different phrasing.
    seen: set = set()
    if economics is not None:
        claim(seen, [economics.scarcity_note])
    why = _render_provenance(provenance, seen, include_context=False)
    if why:
        lines.append("Why now:")
        lines.extend(why)
        lines.append("")
    context = _render_provenance(provenance, seen) if provenance is not None and provenance.context else []
    context = [c for c in context if c.startswith("- **Context:**")]

    more: list[str] = []
    if impact is not None:
        deltas, more = split_visible(without_repeats(impact.material_deltas(), seen), MAX_IMPACT_DELTAS)
        lines.append("What actually changes:")
        if deltas:
            lines.extend(f"- {d}" for d in deltas)
        else:
            lines.append("- nothing material — lineup, depth, status, and roster value all hold; this is a value play, not a lineup play")
        for note in without_repeats([impact.matchup_note], seen):
            lines.append(f"- {note}")
        if more:
            lines.append(f"- … {len(more)} further change(s) in the full rationale below")
        lines.append("")

    mine = without_repeats(p.rationale_for_me, seen)
    theirs = without_repeats(p.rationale_for_them, seen)
    acceptance = without_repeats(p.acceptance_reasons, seen)
    conflict_for_ = without_repeats(conflict.reasons_for, seen) if conflict is not None else []
    conflict_against = without_repeats(conflict.reasons_against, seen) if conflict is not None else []
    caveats = without_repeats(p.caveats, seen)

    lines.extend(_summary("Full rationale", "context, both sides, acceptance factors, caveats, message, ladder"))
    if context:
        lines.extend(context)
        lines.append("")
    if conflict is not None:
        lines.append(f"⚠️ **{CONFLICTED}**")
        lines.append(f"- For: {'; '.join(conflict_for_) or 'see Why now above'}")
        lines.append(f"- Against: {'; '.join(conflict_against) or 'see Why now above'}")
        lines.append("")
    lines.extend(_bullets("Why it works for me:", mine, empty="see Why now above"))
    lines.extend(_bullets(f"Why {p.target_username} plausibly says yes:", theirs, empty="see Why now above"))
    if acceptance:
        lines.append("")
        lines.extend(_bullets("Acceptance factors:", acceptance))
    if more:
        lines.append("")
        lines.extend(_bullets("Further changes:", more))
    if caveats:
        lines.append("")
        lines.extend(_bullets("Caveats:", [f"⚠️ {c}" for c in caveats]))
    if p.message:
        lines.append("")
        lines.append(f"> Message to send: _{p.message}_")
    if ladder is not None:
        lines.append("")
        lines.extend(_render_ladder(ladder))
    lines.extend(_CLOSE_DETAILS)
    return lines


_TIER_MARK = {"Must Add": "🔴", "Strong Add": "🟠", "Moderate": "🟡", "Speculative": "⚪", "Monitor": "⚪", "Insurance": "🛡️"}


def _render_waiver_targets(
    targets: list[WaiverTarget], impacts: dict[str, MoveImpact] | None = None, conflicts: list[Conflict] | None = None,
    faab: dict | None = None, provenance: dict | None = None,
) -> list[str]:
    """The table keeps its columns; the Why cell keeps the engine's own
    lead clauses plus short chips. Notes, FAAB sizing and source/schedule
    annotations move into a keyed details list under the table — a table
    cell can't hold a sub-list in Markdown."""
    if not targets:
        return ["No standout waiver targets this week."]
    impacts = impacts or {}
    conflicts = conflicts or []
    faab = faab or {}
    lines = ["| Priority | Player | Pos | Team | Drop | Horizon | FAAB | Why |", "|---|---|---|---|---|---|---|---|"]
    details: list[str] = []
    seen_leads: set = set()
    for t in targets:  # already capped by the engine; insurance rows ride along after the cap
        mark = _TIER_MARK.get(t.priority_tier, "")
        drop = t.drop_candidate.name if t.drop_candidate else "—"
        advice = faab.get(t.player_id)
        row = waiver_row_view(
            t, impact=impacts.get(t.player_id), conflict=conflict_for(conflicts, WAIVER, t.player_id),
            faab_detail=bid_detail(advice), seen_leads=seen_leads,
        )
        chips = "".join(f" **[{text}]**" for text, _ in row.chips)
        lines.append(
            f"| {mark} {t.priority_tier} | {t.name} | {t.position or '?'} | {t.team or '-'} | {drop} | "
            f"{t.horizon} | {bid_cell(advice, t.suggested_faab_pct)} | {row.lead}{chips} |"
        )
        # The two explanation rows the provenance layer keeps off the
        # capped lists: what the paired drop actually is, and what would
        # make this read wrong.
        prov = (provenance or {}).get((WAIVER, t.player_id))
        extras = [r.text for r in ((prov.why_drop, prov.invalidation) if prov is not None else ()) if r is not None]
        if row.details or extras:
            details.append(f"**{t.name}**")
            details.extend(f"- {d}" for d in row.details)
            details.extend(f"- {d}" for d in extras)
            details.append("")
    if details:
        lines.append("")
        lines.extend(_summary("Waiver details", "notes, impact, FAAB sizing, source and schedule notes"))
        lines.extend(details)
        lines.extend(_CLOSE_DETAILS)
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
    lines = ["An annotation, never a veto.", ""]
    for classification, reason, items in grouped_picks(opp.assessments):
        lines.append(f"- {_PICK_MARK.get(classification, '')} **{classification}: {pick_group_label(items)}** — {reason}")
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
    lines = ["Scarcest first.", ""]
    for m in market.scarcest():
        lines.append(f"- **{m.describe()}**" if m.scarcity in (SCARCE, VERY_SCARCE) else f"- {m.describe()}")
    if market.understated:
        lines.append("")
        lines.append("Rank understates their edge here: " + "; ".join(f"{c.entry.name} ({c.clause()})" for c in market.understated))
    if market.overstated:
        lines.append("")
        lines.append("Closer to replacement than rank suggests: " + "; ".join(f"{c.entry.name} ({c.clause()})" for c in market.overstated))
    lines.append("")
    return lines


def _render_league_economy(economy: LeagueEconomy | None, my_roster_id: int) -> list[str]:
    """Omitted entirely when there is nothing to say — the dashboard does
    the same, and a "nothing stands out" placeholder was one more
    equal-weight section competing with the recommendations."""
    if economy is None:
        return []
    labelled = economy.labelled()
    if not labelled and not economy.limited_sample:
        return []
    lines = []
    if economy.limited_sample:
        lines.append(
            f"_Limited trade-history sample ({economy.total_completed_trades} completed trade"
            f"{'s' if economy.total_completed_trades != 1 else ''} this season) — trader-activity labels suppressed._"
        )
    for m in sorted(labelled, key=lambda m: (m.roster_id != my_roster_id, -m.completed_trades)):
        who = f"{m.team_name or m.username or f'roster {m.roster_id}'}{' (you)' if m.roster_id == my_roster_id else ''}"
        lines.append(f"- **{who}** — {m.describe()}")
    lines.append("")
    return lines


CONTEXT_SUMMARY = "lineup, roster, clogs, replacement market, stash board, buyer board, schedule, draft capital, league economy"


def _render_context(data: LeagueReportData) -> list[str]:
    """Everything that explains or qualifies the moves above, collapsed so
    that a dozen capabilities don't read as a dozen equal sections."""
    body: list[str] = []
    body.extend(_render_lineup(data))
    body.extend(_render_roster_snapshot(data.roster, data.currency))
    body.append("")

    if data.roster_clogs:
        body.append(_heading("Roster clogs", "dead roster spots"))
        body.append("")
        body.extend(f"- **{c.entry.name}** ({c.entry.position or '?'}) — {'; '.join(c.reasons)}" for c in data.roster_clogs)
        body.append("")

    if data.replacement is not None and data.replacement.positions:
        body.append(_heading("Replacement market", "how replaceable each position is from this league's wire"))
        body.append("")
        body.extend(_render_replacement_market(data.replacement))

    if data.stash:
        body.append(_heading("Stash board", "developmental holds, not lineup help"))
        body.append("")
        body.extend(f"- **{c.label}:** {c.describe()}" for c in data.stash)
        body.append("")

    if data.buyer_boards:
        body.append(_heading("Buyer board", "who could pay for your sell-high pieces"))
        body.append("")
        for b in data.buyer_boards:
            buyers = "; ".join(f.describe() for f in b.buyers) if b.buyers else "no Strong or Possible fit in this league"
            body.append(f"- **{b.candidate.name}** ({b.candidate.position or '?'}): {buyers}")
        body.append("")

    if data.windows is not None:
        body.append(_heading("Schedule windows"))
        body.append("")
        body.append(data.windows.describe())
        body.append("")

    if data.pick_opportunity and data.pick_opportunity.assessments:
        body.append(_heading("Draft capital", "what your 1st/2nd-round picks mean to this roster"))
        body.append("")
        body.extend(_render_pick_opportunity(data.pick_opportunity, data.replacement))

    economy_lines = _render_league_economy(data.league_economy, data.roster.roster_id)
    if economy_lines:
        body.append(_heading("League economy", "this season's transaction record"))
        body.append("")
        body.extend(economy_lines)

    return [*_summary("Roster context", CONTEXT_SUMMARY), *body, *_CLOSE_DETAILS]


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

    # Time-sensitive alerts lead when there's a high-severity one — a
    # scrolling reader shouldn't have to pass sections that may all be
    # empty-state to reach the one thing that's actually time-boxed to
    # this week's lineup lock.
    has_high_alert = any(n.severity == "high" for n in data.time_sensitive)
    alert_lines: list[str] = []
    for n in data.time_sensitive:
        mark = _SEVERITY_MARK.get(n.severity, "")
        alert_lines.append(f"- {mark} **{n.player_name}**: {n.note}")
    alert_lines.append("")
    alerts = (_heading("Time-sensitive"), alert_lines) if data.time_sensitive else None

    sections: list[tuple[str, list[str]]] = []
    if has_high_alert and alerts is not None:
        sections.append(alerts)

    if data.matchup is not None:
        sections.append((
            _heading("This week's matchup", f"week {data.matchup.week} vs {data.matchup.opponent_name}"),
            [data.matchup.describe(), "", "This-week lineups with byes and outs applied.", ""],
        ))

    decisions = _render_lineup_decisions(data.lineup_decisions)
    if decisions:
        sections.append((decisions[0], decisions[2:]))

    leverage = _render_lineup_leverage(
        data.lineup_leverage, data.currency, data.replacement_clauses,
        close_calls_shown=bool(decisions),
    )
    if leverage:
        sections.append((leverage[0], leverage[2:]))  # [0] heading, [1] its blank line

    trade_lines = []
    if data.proposals:
        for i, p in enumerate(data.proposals, start=1):
            impact = data.trade_impacts[i - 1] if i - 1 < len(data.trade_impacts) else None
            economics = data.trade_economics[i - 1] if i - 1 < len(data.trade_economics) else None
            conflict = conflict_for(data.conflicts, TRADE, str(i - 1))
            trade_lines.extend(_render_trade_proposal(p, i, impact, data.ladders.get(i - 1), economics, conflict, data.provenance.get((TRADE, str(i - 1)))))
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
    sections.append((_heading("Trade offers"), trade_lines))

    waiver_lines = (
        [f"_{data.waivers_note}_", ""]
        if data.waivers_note
        else _render_waiver_targets(data.waiver_targets, data.waiver_impacts, data.conflicts, data.faab, data.provenance) + [""]
    )
    if data.faab_note and not data.waivers_note and data.waiver_targets:
        waiver_lines.extend([f"_{data.faab_note}_", ""])
    if data.streamers:
        waiver_lines.extend(_render_streamers(data.streamers))
    if data.defensive_add is not None:
        waiver_lines.extend([f"**🛡 Defensive add** (deny this week's opponent): {data.defensive_add.describe()}", ""])
    sections.append((_heading("Waiver targets"), waiver_lines))

    if data.drop_candidates:
        # Only rendered when there's something real to say — no synthetic
        # "nothing to drop" filler, matching the empty-state discipline
        # used elsewhere in this report.
        sections.append((_heading("Consider dropping"), _render_drop_candidates(data.drop_candidates)))

    if not has_high_alert and alerts is not None:
        sections.append(alerts)

    for header, body in sections:
        lines.append(header)
        lines.append("")
        lines.extend(body)

    lines.extend(_render_context(data))
    return lines


def render_weekly_report(report: WeeklyReportData) -> str:
    lines = [
        "# Weekly Fantasy Football Report",
        "",
        f"_Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    banner = health_banner(report)
    if banner is not None:
        lines.extend([f"> ⚠️ **{banner.text}** Details in Signal health below.", ""])
    lines.extend(_render_priority_actions(report.priority_actions))
    lines.extend(_render_delta(report.delta))
    lines.extend(_render_portfolio_exposure(report.portfolio, report.asymmetries))
    lines.extend(_render_signal_health(report))

    for league_data in report.leagues:
        lines.extend(render_league_section(league_data))
        lines.append("---")
        lines.append("")

    lines.extend(_render_diagnostics(report))
    return "\n".join(lines)


def _render_signal_health(report: WeeklyReportData) -> list[str]:
    """Every input with its label and age, then what that cost this run.
    Falls back to the bare ages when the report carries no health grade
    (a test-built report)."""
    lines: list[str] = []
    if report.health is None:
        lines.extend(["## Data freshness", ""])
        for source, age in report.source_freshness.items():
            lines.append(f"- {source}: {age_str(age)} old")
        lines.append(f"- ff_dynasty_pass (manual CSV): {report.ff_status}")
    else:
        lines.extend([f"## Signal health — {health_state(report)}", ""])
        lines.extend(f"- {line}" for line in report.freshness_lines)
        for note in report.health.notes:
            lines.append(f"- ⚠️ {note}")
        for feature, why in sorted(report.suppressed.items()):
            lines.append(f"- ⚠️ Suppressed this run: {feature.replace('_', ' ')} — {why}")
    if report.usage_note:
        lines.append(f"- Player usage: {report.usage_note}.")
    if report.crosswalk_note:
        lines.append(f"- {report.crosswalk_note}")
    lines.extend(["", "---", ""])
    return lines


MAX_DIAGNOSTIC_LINES = 10

DIAGNOSTICS_SUMMARY = "decision ledger, outcome facts, watchlist"


def _render_diagnostics(report: WeeklyReportData) -> list[str]:
    """History and self-checks, last and collapsed: what earlier runs
    recommended and what Sleeper then showed, what the watchlist promoted,
    how many are still watched. Facts only; nothing here scores the tool."""
    body: list[str] = []
    if report.ledger_summary:
        body.append("**Decision ledger** (recommendations recorded, by action and observed outcome):")
        body.append("")
        for action, counts in report.ledger_summary.items():
            body.append(f"- {action}: " + ", ".join(f"{'awaiting outcome' if label == '(open)' else label} {n}" for label, n in counts.items()))
        body.append("")
    observed = [f for f in report.outcome_facts if f.state == OBSERVED]
    if observed:
        body.append("**Outcome facts** (descriptive; the value moves are the sources', not a verdict):")
        body.append("")
        body.extend(f"- {f.describe()}" for f in observed[-MAX_DIAGNOSTIC_LINES:])
        if len(observed) > MAX_DIAGNOSTIC_LINES:
            body.append(f"- … {len(observed) - MAX_DIAGNOSTIC_LINES} earlier facts not shown")
        body.append("")
    if report.watchlist_sections or report.watchlist_new:
        body.append("**Watchlist** (each item is a thesis; this is what happened to it since it was written):")
        body.append("")
        for section, lines_ in report.watchlist_sections.items():
            body.append(f"_{section}_")
            body.extend(f"- {line}" for line in lines_)
            body.append("")
        if not report.watchlist_sections:
            body.extend(f"- 🆕 {line}" for line in report.watchlist_new)
        if report.watchlist_watching:
            body.append(f"- {report.watchlist_watching} more item(s) watched with nothing new to say")
        body.append("")
    if not body:
        return []
    return [*_summary("Diagnostics and history", DIAGNOSTICS_SUMMARY), *body, *_CLOSE_DETAILS]


def generate_weekly_report(
    storage: Storage, engine: ValuationEngine, leagues: list[LeagueInfo] = LEAGUES
) -> str:
    report = build_weekly_report_data(storage, engine, leagues)
    return render_weekly_report(report)
