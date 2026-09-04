"""Renders WeeklyReportData as a self-contained, responsive HTML dashboard.

Single fragment (no <!DOCTYPE>/<html>/<head>/<body> wrapper) so the same
file works both as a local double-click-to-open page and as an Artifact
publish target. All CSS/JS is inlined; the only external reference is the
Google Fonts stylesheet link, which is the one host the Artifact CSP allows.
"""
from __future__ import annotations

from html import escape as esc

from sleeper_tool.asset_value import percentile_for_currency, value_label_for_currency
from sleeper_tool.decision_delta import DecisionDelta
from sleeper_tool.decision_outcomes import OBSERVED
from sleeper_tool.faab_strategy import bid_cell, bid_detail
from sleeper_tool.formatting import age_str
from sleeper_tool.league_economy import LeagueEconomy
from sleeper_tool.lineup_optimizer import slot_label
from sleeper_tool.lineup_leverage import LineupLeverage
from sleeper_tool.matchup_leverage import LARGE_DEFICIT, MODEST_DEFICIT, MODEST_EDGE, STRONG_EDGE, MatchupLeverage
from sleeper_tool.move_impact import MoveImpact
from sleeper_tool.negotiation_ladder import NegotiationLadder
from sleeper_tool.pick_opportunity import PickOpportunity
from sleeper_tool.recommendation_conflicts import CONFLICTED, TRADE, WAIVER, Conflict, conflict_for
from sleeper_tool.portfolio_exposure import VERY_HIGH, PortfolioExposure
from sleeper_tool.playoff_leverage import BUBBLE, COMFORTABLE, LONG_SHOT, OUT
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, SCARCE, VERY_SCARCE, ReplacementMarket
from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD
from sleeper_tool.report_data import LeagueReportData, PriorityAction, WeeklyReportData, describe_format
from sleeper_tool.report_views import (
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
    claim,
    fact_of,
    without_repeats,
)
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.roster_clog import RosterClog
from sleeper_tool.streamer_planner import ADD, HOLD, SEQUENCE, StreamPlan
from sleeper_tool.trade_opportunity_cost import (
    COSTS_LINEUP,
    FAVORABLE,
    IMPROVES_LINEUP,
    MAJOR_LINEUP_COST,
    STRATEGIC_TRADEOFF,
    UNFAVORABLE,
    TradeEconomics,
)
from sleeper_tool.trade_types import DropCandidate, TradeProposal
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget

TREND_META = {
    "rising": ("&#8593;", "positive", "Trending up"),
    "down": ("&#8595;", "negative", "Trending down"),
    "no change": ("&#8594;", "neutral", "Steady"),
}


def _slug(text: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")


def _chip(text: str, kind: str = "neutral") -> str:
    return f'<span class="chip chip-{kind}">{esc(text)}</span>'


def _trend_badge(trend: str | None) -> str:
    arrow, kind, label = TREND_META.get(trend or "", ("", "neutral", "No trend data"))
    if not arrow:
        return '<span class="trend trend-neutral" title="No trend data">&#8212;</span>'
    return f'<span class="trend trend-{kind}" title="{esc(label)}">{arrow}</span>'


def _value_cell(pctl: float | None) -> str:
    if pctl is None:
        return '<span class="pctl-unranked">unranked</span>'
    return (
        '<div class="pctl-cell">'
        f'<div class="pctl-bar"><div class="pctl-fill" style="width:{pctl:.0f}%"></div></div>'
        f'<span class="pctl-num">{pctl:.0f}<sup>{_ordsuffix(pctl)}</sup></span>'
        "</div>"
    )


def _ordsuffix(n: float) -> str:
    i = round(n)
    if 10 <= i % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")


def _confidence_flag(v) -> str:
    """A small marker for a shaky valuation, shown right on the roster
    table instead of only ever surfacing inside a generated trade's
    collapsed caveats (i.e. only when that player happens to be part of a
    trade offer that week). The rubric is report_views.confidence_caveat,
    which the Markdown roster table uses too — this used to be an
    HTML-only flag.
    """
    title = confidence_caveat(v)
    if not title:
        return ""
    return f'<span class="confidence-flag" title="{esc(title)}">{esc(CONFIDENCE_MARK)}</span>'


def _roster_rows(entries: list[RosterEntry], currency: str) -> str:
    rows = []
    for e in entries:
        pctl = percentile_for_currency(e.value, currency)
        rows.append(
            "<tr>"
            f'<td class="player-cell">{esc(e.name)} {_confidence_flag(e.value)}</td>'
            f'<td>{esc(e.position or "?")}</td>'
            f'<td>{esc(e.team or "-")}</td>'
            f"<td>{_value_cell(pctl)}</td>"
            f"<td>{_trend_badge(e.value.trend)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _lineup_section(data: LeagueReportData) -> str:
    """The optimizer's best legal lineup. `lineup_optimizer` is the single
    owner of "who starts" and nothing rendered it directly before this."""
    games_left = data.lineup_leverage.games_left if data.lineup_leverage is not None else None
    rows = lineup_lines(data.lineup, games_left)
    if not rows:
        return ""
    unit = lineup_units(games_left)
    items = "".join(
        f'<li class="alert-item">{_chip(slot, "accent")} <strong>{esc(name)}</strong> '
        f'<span class="tabular muted">{esc(proj)}{esc(unit)}</span></li>'
        for slot, name, proj in rows
    )
    return f"""
    <section class="panel-block">
      <h3>Best starting lineup <span class="muted">&middot; structural &mdash; no bye-week exclusions</span></h3>
      <p class="roster-note">Projects ~{lineup_total(data.lineup, games_left):.1f} pts{esc(unit)}.</p>
      <ul class="alert-list">{items}</ul>
    </section>
    """


def _roster_section(roster: ValuedRoster, currency: str) -> str:
    label = value_label_for_currency(currency)
    starters = sorted(roster.starters(), key=lambda e: -(percentile_for_currency(e.value, currency) or 0))
    bench = sorted(roster.bench(), key=lambda e: -(percentile_for_currency(e.value, currency) or 0))
    taxi = [e for e in roster.entries if e.is_taxi]
    reserve = [e for e in roster.entries if e.is_reserve]

    html = [
        '<section class="panel-block">',
        f'<h3>Roster <span class="muted">&middot; {esc(label)}</span></h3>',
        '<div class="table-scroll"><table class="roster-table">',
        "<thead><tr><th>Starter</th><th>Pos</th><th>Team</th><th>Value</th><th>Trend</th></tr></thead>",
        f"<tbody>{_roster_rows(starters, currency)}</tbody>",
        "</table></div>",
    ]
    if any(confidence_caveat(e.value) for e in starters):
        html.append(f'<p class="roster-note">{esc(CONFIDENCE_LEGEND)}</p>')
    if bench:
        html.append(f'<div class="bench-strip"><span class="bench-label">Bench ({len(bench)})</span>')
        for e in bench:
            pctl = percentile_for_currency(e.value, currency)
            pctl_str = f"{pctl:.0f}{_ordsuffix(pctl)}" if pctl is not None else "unranked"
            html.append(f'<span class="bench-chip">{esc(e.name)} <b>{pctl_str}</b></span>')
        html.append("</div>")
    if taxi:
        html.append(f'<p class="roster-note"><strong>Taxi squad:</strong> {esc(", ".join(e.name for e in taxi))}</p>')
    if reserve:
        html.append(f'<p class="roster-note"><strong>IR/Reserve:</strong> {esc(", ".join(e.name for e in reserve))}</p>')
    if roster.skipped_player_count:
        html.append(
            f'<p class="roster-note">Note: {roster.skipped_player_count} roster player(s) couldn\'t be valued '
            "this week (missing from the player cache) &mdash; this snapshot may be incomplete.</p>"
        )
    html.append("</section>")
    return "".join(html)


_DECISION_CHIP_KIND = {"Toss-Up": "caution", "Lean Start": "neutral"}


def _lineup_decisions_section(dec) -> str:
    if dec is None:
        return ""
    week = f"week {dec.week}" if dec.week else "this week"
    if not dec.items:
        body = '<p class="empty-note">Lineup is already the best legal one &mdash; nothing to change before kickoff.</p>'
    else:
        items = []
        for item in dec.items:
            extras = "".join(f'<div class="drop-reasons">{esc(x)}</div>' for x in ([item.what_if] if item.what_if else []) + list(item.context))
            items.append(
                f'<li class="alert-item">{_chip(item.kind, "accent")} {esc(item.summary)}{extras}</li>'
            )
        stake = (
            f'<p class="muted">{dec.close_call_stake:.1f} pts/wk rides on the close calls above.</p>'
            if dec.close_call_stake else ""
        )
        body = f'<ul class="alert-list">{"".join(items)}</ul>{stake}'
    return f"""
    <section class="panel-block">
      <h3>This week&rsquo;s decisions <span class="muted">&middot; {esc(week)}</span></h3>
      {body}
    </section>
    """


def _lineup_leverage_section(
    lev: LineupLeverage | None, currency: str, clauses: dict[str, str] | None = None, *, close_calls_shown: bool = False
) -> str:
    calls = [] if close_calls_shown else (lev.close_calls if lev is not None else [])
    if lev is None or (not calls and not lev.bench_surplus):
        return ""
    clauses = clauses or {}
    items = []
    for d in calls:
        hint = " &middot; close enough that matchup should decide" if d.label == "Toss-Up" else ""
        items.append(
            f'<li class="alert-item alert-{_DECISION_CHIP_KIND.get(d.label, "neutral")}">'
            f'{_chip(d.label, _DECISION_CHIP_KIND.get(d.label, "neutral"))} <strong>{esc(slot_label(d.slot))}</strong>: '
            f"{esc(d.starter.name)} <span class=\"tabular\">{d.starter_weekly:.1f}</span>/wk over "
            f"{esc(d.alternative.name)} <span class=\"tabular\">{d.alternative_weekly:.1f}</span>/wk{hint}"
            + (f' <span class="muted">&middot; {esc(d.schedule_note)}</span>' if d.schedule_note else "")
            + "</li>"
        )
    for s in lev.bench_surplus:
        pctl = f"{s.value_percentile:.0f}{_ordsuffix(s.value_percentile)} percentile" if s.value_percentile is not None else "unranked"
        items.append(
            f'<li class="alert-item">{_chip("Bench surplus", "accent")} <strong>{esc(s.entry.name)}</strong> '
            f"({esc(s.entry.position or '?')}, {pctl} {esc(value_label_for_currency(currency))}) projects at "
            f"{s.ratio:.0%} of {esc(s.displaced_starter.name)} ({esc(slot_label(s.displaced_slot))}) but sits "
            '<div class="drop-reasons">Value that could be traded for a starter without costing lineup points'
            + (f" &middot; {esc(clauses[s.entry.player_id])}" if s.entry.player_id in clauses else "")
            + "</div></li>"
        )
    return f"""
    <section class="panel-block">
      <h3>Lineup leverage <span class="muted">&middot; best legal lineup ~{lev.weekly_starter_points:.0f} pts/week</span></h3>
      <ul class="alert-list">{"".join(items)}</ul>
    </section>
    """


_ACCEPTANCE_CHIP_KIND = {"High": "positive", "Good": "positive", "Moderate": "neutral", "Low": "caution", "Very Low": "negative"}
_CONFIDENCE_CHIP_KIND = {"High": "positive", "Medium": "neutral", "Low": "caution"}


def _asset_chip(name: str, *, is_pick: bool) -> str:
    tag = '<span class="pick-tag">PICK</span>' if is_pick else ""
    return f'<span class="asset">{tag}{esc(name)}</span>'


def _impact_block(impact: MoveImpact | None, more: list[str]) -> str:
    if impact is None:
        return ""
    deltas, _ = split_visible(impact.material_deltas(), MAX_IMPACT_DELTAS)
    items = "".join(f"<li>{esc(d)}</li>" for d in deltas) if deltas else (
        "<li>nothing material &mdash; lineup, depth, status, and roster value all hold; a value play, not a lineup play</li>"
    )
    if impact.matchup_note:
        items += f"<li>{esc(impact.matchup_note)}</li>"
    if more:
        items += f'<li class="muted">&hellip; {len(more)} further change(s) in the full rationale below</li>'
    return f'<div class="impact-block"><span class="rationale-label">What actually changes</span><ul>{items}</ul></div>'


def _ladder_block(ladder: NegotiationLadder | None) -> str:
    if ladder is None:
        return ""

    def step(s) -> str:
        note = f' <span class="muted">— {esc(s.starter_note)}</span>' if s.starter_note else ""
        note += f' <span class="muted">— {esc(s.source_note)}</span>' if s.source_note else ""
        return (
            f"<strong>{esc(s.asset_names)}</strong> "
            f'<span class="tabular muted">{s.outgoing_value:.0f} · {s.ratio:.0%} of what you get</span> '
            f"{_chip('Acceptance: ' + s.acceptance, _ACCEPTANCE_CHIP_KIND.get(s.acceptance, 'neutral'))}{note}"
        )

    lowball = ' <span class="muted">— deliberately below value, expect a counter</span>' if ladder.opening.lowball else ""
    rows = [f'<li><span class="ladder-step">Opening</span> {step(ladder.opening)}{lowball}</li>']
    if ladder.opening_message:
        rows.append(f'<li class="ladder-message">{esc(ladder.opening_message)}</li>')
    if ladder.fallback:
        rows.append(f'<li><span class="ladder-step">Fallback</span> {step(ladder.fallback)}</li>')
    else:
        rows.append('<li><span class="ladder-step">Fallback</span> <span class="muted">none within 10% improves the rating — if countered, hold or walk</span></li>')
    if ladder.walk_away:
        rows.append(
            f'<li><span class="ladder-step">Walk away above</span> {step(ladder.walk_away)} '
            '<span class="muted">— past this you\'re overpaying</span></li>'
        )
    else:
        top = "fallback" if ladder.fallback else "opening"
        rows.append(f'<li><span class="ladder-step">Walk away</span> <span class="muted">above the {top} — nothing more expensive still rates acceptable</span></li>')
    return f'<div class="ladder"><span class="rationale-label">Negotiation ladder</span><ul>{"".join(rows)}</ul></div>'


_ASSET_CHIP_KIND = {FAVORABLE: "positive", UNFAVORABLE: "caution"}
_ROSTER_CHIP_KIND = {IMPROVES_LINEUP: "positive", COSTS_LINEUP: "caution", MAJOR_LINEUP_COST: "negative"}


def _economics_chips(econ: TradeEconomics | None) -> str:
    if econ is None:
        return ""
    chips = _chip("Assets: " + esc(econ.asset_economics), _ASSET_CHIP_KIND.get(econ.asset_economics, "neutral"))
    if econ.roster_economics:
        delta = f" ({econ.weekly_delta:+.1f}/wk)" if econ.weekly_delta is not None else ""
        chips += _chip("Lineup: " + esc(econ.roster_economics) + delta, _ROSTER_CHIP_KIND.get(econ.roster_economics, "neutral"))
    if econ.strategic_tradeoff:
        chips += _chip(STRATEGIC_TRADEOFF, "caution")
    return chips


def _conflict_block(conflict: Conflict | None, reasons_for: list[str], reasons_against: list[str]) -> str:
    if conflict is None:
        return ""
    return (
        f'<div class="caveats conflict-block"><span class="caveat-label">{_chip(esc(CONFLICTED), "negative")}</span><ul>'
        f"<li><b>For:</b> {esc('; '.join(reasons_for) or 'see Why now above')}</li>"
        f"<li><b>Against:</b> {esc('; '.join(reasons_against) or 'see Why now above')}</li></ul></div>"
    )


def _rationale_items(texts: list[str]) -> str:
    if not texts:
        return '<li class="muted">see Why now above</li>'
    return "".join(f"<li>{esc(t)}</li>" for t in texts)


def _trade_card(
    p: TradeProposal, index: int, impact: MoveImpact | None = None, ladder: NegotiationLadder | None = None,
    economics: TradeEconomics | None = None, conflict: Conflict | None = None, provenance=None,
) -> str:
    """Visible: what the offer is, how it rates, its economics, a conflict
    chip if any, the Why-now card, and what actually changes. Collapsed:
    the full rationale, acceptance factors, caveats, the message and the
    ladder. Nothing is dropped; a sentence already shown in the Why-now
    card is simply not repeated underneath it."""
    give_chips = "".join(
        [*(_asset_chip(e.name, is_pick=False) for e in p.give), *(_asset_chip(pk.name, is_pick=True) for pk in p.give_picks)]
    )
    receive_chips = "".join(
        [*(_asset_chip(e.name, is_pick=False) for e in p.receive), *(_asset_chip(pk.name, is_pick=True) for pk in p.receive_picks)]
    )
    target = esc(p.target_team_name or p.target_username)
    type_label = p.trade_type_label
    value_label = value_label_for_currency(p.currency)

    # One vocabulary per fact: the economics line and the Why-now card
    # claim their facts first, then the collapsed block below shows what
    # is left rather than restating them in another phrasing.
    seen: set = set()
    if economics is not None:
        claim(seen, [economics.scarcity_note])
    why_block = _provenance_block(provenance, seen)  # claims its facts before the context and the collapsed block
    context_rows = _provenance_rows(provenance, seen, groups=("Context",))

    _, more_deltas = split_visible(impact.material_deltas() if impact is not None else [], MAX_IMPACT_DELTAS)

    mine = _rationale_items(without_repeats(p.rationale_for_me, seen))
    theirs = _rationale_items(without_repeats(p.rationale_for_them, seen))
    acceptance_reasons = "".join(f"<li>{esc(r)}</li>" for r in without_repeats(p.acceptance_reasons, seen))
    acceptance_block = (
        f'<div><span class="rationale-label">Why this rating</span><ul>{acceptance_reasons}</ul></div>'
        if acceptance_reasons
        else ""
    )
    conflict_block = _conflict_block(
        conflict,
        without_repeats(conflict.reasons_for, seen) if conflict is not None else [],
        without_repeats(conflict.reasons_against, seen) if conflict is not None else [],
    )
    more_block = (
        f'<div><span class="rationale-label">Further changes</span>'
        f'<ul>{"".join(f"<li>{esc(d)}</li>" for d in more_deltas)}</ul></div>'
        if more_deltas
        else ""
    )
    caveats = "".join(f'<li class="caveat-item">{esc(c)}</li>' for c in without_repeats(p.caveats, seen))
    caveats_block = f'<div class="caveats"><span class="caveat-label">Caveats</span><ul>{caveats}</ul></div>' if caveats else ""
    message_block = (
        f'<div class="trade-message"><span class="rationale-label">Message to send</span>'
        f'<p class="trade-message-text">{esc(p.message)}</p></div>'
        if p.message
        else ""
    )

    return f"""
    <article class="trade-card">
      <header class="trade-card-head">
        <span class="trade-index">Offer {index} &middot; {esc(type_label)}</span>
        {_chip(p.balance_label, p.balance_kind)}
      </header>
      <div class="trade-flow">
        <div class="trade-side"><span class="trade-side-label">You send</span><div class="asset-list">{give_chips}</div></div>
        <div class="trade-arrow" aria-hidden="true">&#8644;</div>
        <div class="trade-side"><span class="trade-side-label">You get</span><div class="asset-list">{receive_chips}</div></div>
      </div>
      <p class="trade-target">To <strong>{target}</strong> &middot; {esc(value_label)}: {p.my_value_total:.0f} vs {p.their_value_total:.0f}</p>
      <div class="trade-signals">
        {_chip('Acceptance: ' + esc(p.acceptance_rating), _ACCEPTANCE_CHIP_KIND.get(p.acceptance_rating, 'neutral'))}
        {_chip('Confidence: ' + esc(p.confidence), _CONFIDENCE_CHIP_KIND.get(p.confidence, 'neutral'))}
        {_economics_chips(economics)}
        {_chip(CONFLICTED, "negative") if conflict is not None else ""}
      </div>
      {f'<p class="muted status-reason">{esc(economics.scarcity_note)}</p>' if economics is not None and economics.scarcity_note else ""}
      {why_block}
      {_impact_block(impact, more_deltas)}
      <details class="trade-details">
        <summary>Full rationale &middot; both sides, acceptance factors, caveats, message, ladder</summary>{context_rows}
        {conflict_block}
        <div class="trade-rationale">
          <div><span class="rationale-label">For me</span><ul>{mine}</ul></div>
          <div><span class="rationale-label">For {esc(p.target_username)}</span><ul>{theirs}</ul></div>
        </div>
        {acceptance_block}
        {more_block}
        {caveats_block}
        {message_block}
        {_ladder_block(ladder)}
      </details>
    </article>
    """


def _waiver_table(
    targets: list[WaiverTarget], impacts: dict[str, MoveImpact] | None = None, conflicts: list[Conflict] | None = None,
    faab: dict | None = None, provenance: dict | None = None,
) -> str:
    if not targets:
        return '<p class="empty-note">No standout waiver targets this week.</p>'
    impacts = impacts or {}
    conflicts = conflicts or []
    faab = faab or {}
    # "positive" (green), not "negative" (red) -- Must Add is a GOOD thing
    # to see, matching the green used for waiver actions in the "Best
    # moves right now" section (_ACTION_KIND_META). "negative" is reserved
    # for genuinely bad outcomes (a lopsided trade, a high-severity
    # injury) elsewhere on the page; reusing it here read as if the
    # tool's own top waiver pick were a warning.
    _TIER_CHIP_KIND = {
        "Must Add": "positive", "Strong Add": "accent", "Moderate": "neutral", "Speculative": "neutral",
        "Monitor": "neutral", "Insurance": "caution",
    }
    _CHIP_KIND = {"negative": "negative", "neutral": "neutral"}
    rows = []
    seen_leads: set = set()
    for t in targets:  # already capped by the engine; insurance rows ride along after the cap
        tier_chip = _chip(t.priority_tier, _TIER_CHIP_KIND.get(t.priority_tier, "neutral"))
        drop = esc(t.drop_candidate.name) if t.drop_candidate else '<span class="muted">—</span>'
        advice = faab.get(t.player_id)
        faab_text = esc(bid_cell(advice, t.suggested_faab_pct))
        row = waiver_row_view(
            t, impact=impacts.get(t.player_id), conflict=conflict_for(conflicts, WAIVER, t.player_id),
            faab_detail=bid_detail(advice), seen_leads=seen_leads,
        )
        chips = "".join(_chip(text, _CHIP_KIND.get(kind, "neutral")) for text, kind in row.chips)
        chips_html = f'<div class="row-chips">{chips}</div>' if chips else ""
        # Everything the engine wrote that isn't the lead clause lives one
        # click away instead of turning the cell into a 600-character wall.
        # Plus the two explanation rows provenance keeps off the capped
        # lists: what the paired drop is, and what would make this wrong.
        prov = (provenance or {}).get((WAIVER, t.player_id))
        extras = [r.text for r in ((prov.why_drop, prov.invalidation) if prov is not None else ()) if r is not None]
        details = (
            '<details class="row-details"><summary>Details</summary>'
            + "".join(f'<div class="impact-inline">{esc(d)}</div>' for d in [*row.details, *extras])
            + "</details>"
            if row.details or extras
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{tier_chip}</td>"
            f'<td class="player-cell">{esc(t.name)}</td>'
            f'<td>{esc(t.position or "?")}</td>'
            f'<td>{esc(t.team or "-")}</td>'
            f'<td>{drop}</td>'
            f'<td>{_chip(t.horizon, "neutral")}</td>'
            f'<td class="tabular">{faab_text}</td>'
            f'<td class="waiver-reason">{esc(row.lead)}{chips_html}{details}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table class="waiver-table">'
        "<thead><tr><th>Priority</th><th>Player</th><th>Pos</th><th>Team</th><th>Drop</th>"
        "<th>Horizon</th><th>FAAB</th><th>Why</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


_MATCHUP_CHIP_KIND = {STRONG_EDGE: "positive", MODEST_EDGE: "positive", MODEST_DEFICIT: "caution", LARGE_DEFICIT: "negative"}


def _matchup_section(m: MatchupLeverage | None) -> str:
    if m is None:
        return ""
    return f"""
    <section class="panel-block">
      <h3>This week's matchup <span class="muted">&middot; week {m.week} vs {esc(m.opponent_name)}</span></h3>
      <p class="roster-note">{_chip(m.label, _MATCHUP_CHIP_KIND.get(m.label, "neutral"))} you project
      <span class="tabular">{m.my_points:.1f}</span>, they project <span class="tabular">{m.opponent_points:.1f}</span>
      (<span class="tabular">{m.gap:+.1f}</span>)</p>
      <p class="roster-note">This-week lineups with byes and outs applied.</p>
    </section>
    """


def _defensive_add_block(add) -> str:
    if add is None:
        return ""
    return (
        '<div class="streamers"><span class="rationale-label">Defensive add &middot; deny this week\'s opponent</span>'
        f'<ul class="alert-list"><li class="alert-item alert-caution">{_chip("Defensive Add", "caution")} {esc(add.describe())}</li></ul></div>'
    )


def _consolidation_block(consolidations) -> str:
    if not consolidations:
        return ""
    notes = "".join(
        f'<li class="alert-item{" alert-caution" if c.fragility_note else ""}">{_chip("2-for-1", "accent")} {esc(c.describe())} &middot; {esc(c.freed_slot_note)}'
        + (f' &middot; {esc(c.fragility_note)}' if c.fragility_note else "") + "</li>"
        for c in consolidations
    )
    return (
        '<div class="streamers"><span class="rationale-label">Consolidation (2-for-1) summary &middot; the 2-for-1 offers above, by lineup gain</span>'
        f'<ul class="alert-list">{notes}</ul></div>'
    )


def _stash_section(stash) -> str:
    if not stash:
        return ""
    items = "".join(
        f'<li class="alert-item">{_chip(c.label, "accent" if c.label == "Priority Stash" else "neutral")} {esc(c.describe())}</li>' for c in stash
    )
    return f"""
    <section class="panel-block">
      <h3>Stash board <span class="muted">&middot; developmental holds, not lineup help</span></h3>
      <ul class="alert-list">{items}</ul>
    </section>
    """


def _buyer_board_section(boards) -> str:
    if not boards:
        return ""
    items = []
    for b in boards:
        buyers = "; ".join(f.describe() for f in b.buyers) if b.buyers else "no Strong or Possible fit in this league"
        items.append(f'<li class="alert-item"><strong>{esc(b.candidate.name)}</strong> ({esc(b.candidate.position or "?")})<div class="drop-reasons">{esc(buyers)}</div></li>')
    return f"""
    <section class="panel-block">
      <h3>Buyer board <span class="muted">&middot; who could pay for your sell-high pieces</span></h3>
      <ul class="alert-list">{"".join(items)}</ul>
    </section>
    """


def _schedule_section(windows) -> str:
    if windows is None:
        return ""
    return f"""
    <section class="panel-block">
      <h3>Schedule windows</h3>
      <p class="roster-note">{esc(windows.describe())}</p>
    </section>
    """


_STREAM_CHIP_KIND = {HOLD: "neutral", ADD: "positive", SEQUENCE: "accent"}


def _streamers_block(plans: list[StreamPlan]) -> str:
    if not plans:
        return ""
    items = []
    for plan in plans:
        detail = ""
        if plan.recommendation != HOLD:
            if plan.recommendation == SEQUENCE:
                rows = [
                    f"{esc(plan.sequence.first.entry.name)}: {esc(plan.sequence.first.week_text())}",
                    f"{esc(plan.sequence.second.entry.name)}: {esc(plan.sequence.second.week_text())}",
                ]
            else:
                rows = [f"{esc(plan.single.entry.name)}: {esc(plan.single.week_text())}"]
            if plan.current is not None:
                rows.append(f"{esc(plan.current.entry.name)} (current): {esc(plan.current.week_text())}")
            detail = '<div class="drop-reasons">' + "<br>".join(rows) + "</div>"
        items.append(
            f'<li class="alert-item">{_chip(plan.recommendation, _STREAM_CHIP_KIND.get(plan.recommendation, "neutral"))} '
            f"{esc(plan.describe())}{detail}</li>"
        )
    return (
        '<div class="streamers"><span class="rationale-label">Streaming plan &middot; projected points over the window; '
        'byes from the NFL schedule, no opponent adjustment</span>'
        f'<ul class="alert-list">{"".join(items)}</ul></div>'
    )


_DROP_PRIORITY_KIND = {"Strong Drop": "caution", "Consider Dropping": "neutral"}


def _drop_candidates_section(candidates: list[DropCandidate]) -> str:
    # Only rendered when there's something real to say -- no synthetic
    # "nothing to drop" filler, matching the empty-state discipline used
    # elsewhere on this page.
    if not candidates:
        return ""
    items = "".join(
        f'<li class="alert-item alert-{_DROP_PRIORITY_KIND.get(c.priority, "neutral")}">'
        f'<strong>{esc(c.entry.name)}</strong> ({esc(c.entry.position or "?")}) &middot; {_chip(c.priority, _DROP_PRIORITY_KIND.get(c.priority, "neutral"))}'
        f'<div class="drop-reasons">{esc("; ".join(c.reasons))}</div>'
        "</li>"
        for c in candidates
    )
    return f"""
    <section class="panel-block">
      <h3>Consider dropping</h3>
      <ul class="alert-list">{items}</ul>
    </section>
    """


def _roster_clogs_section(clogs: list[RosterClog]) -> str:
    if not clogs:
        return ""
    items = "".join(
        f'<li class="alert-item alert-caution">'
        f'<strong>{esc(c.entry.name)}</strong> ({esc(c.entry.position or "?")}) &middot; {_chip("Roster Clog", "caution")}'
        f'<div class="drop-reasons">{esc("; ".join(c.reasons))}</div>'
        "</li>"
        for c in clogs
    )
    return f"""
    <section class="panel-block">
      <h3>Roster clogs <span class="muted">&middot; dead roster spots</span></h3>
      <ul class="alert-list">{items}</ul>
    </section>
    """


_PICK_CHIP_KIND = {"Strategic": "negative", "Useful": "caution", "Spendable": "positive"}


def _pick_opportunity_section(opp: PickOpportunity | None, market: ReplacementMarket | None = None) -> str:
    if opp is None or not opp.assessments:
        return ""
    items = "".join(
        f'<li class="alert-item">{_chip(a.classification, _PICK_CHIP_KIND.get(a.classification, "neutral"))} '
        f"<strong>{esc(a.display_name)}</strong>"
        + (f' <span class="tabular muted">KTC {a.pick.value:,}</span>' if a.pick.value else "")
        + f'<div class="drop-reasons">{esc(a.reason)}</div></li>'
        for a in opp.assessments
    )
    weak = [u for u in opp.units if u.bottom_three]
    units_note = (
        '<p class="roster-note">Position units driving this: '
        + esc("; ".join(_unit_text(u, market) for u in weak))
        + "</p>"
        if weak
        else ""
    )
    return f"""
    <section class="panel-block">
      <h3>Draft capital <span class="muted">&middot; what your 1st/2nd-round picks mean to this roster</span></h3>
      <p class="roster-note">An annotation, never a veto.</p>
      <ul class="alert-list">{items}</ul>
      {units_note}
    </section>
    """


def _unit_text(u, market: ReplacementMarket | None) -> str:
    text = u.describe() + (" (weak-aging)" if u.weak_aging else "")
    scarcity = market.scarcity_of(u.position) if market is not None else None
    return text + (f", {scarcity} replacement market" if scarcity else "")


_SCARCITY_CHIP_KIND = {VERY_SCARCE: "negative", SCARCE: "caution", NORMAL: "neutral", ABUNDANT: "positive"}


def _replacement_market_section(market: ReplacementMarket | None) -> str:
    if market is None or not market.positions:
        return ""
    items = "".join(
        f'<li class="alert-item">{_chip(m.scarcity, _SCARCITY_CHIP_KIND.get(m.scarcity, "neutral"))} '
        f"<strong>{esc(m.position)}</strong>"
        f'<div class="drop-reasons">{esc(m.detail())}</div></li>'
        for m in market.scarcest()
    )
    notes = ""
    if market.understated:
        notes += '<p class="roster-note">Rank understates their edge here: ' + esc("; ".join(f"{c.entry.name} ({c.clause()})" for c in market.understated)) + "</p>"
    if market.overstated:
        notes += '<p class="roster-note">Closer to replacement than rank suggests: ' + esc("; ".join(f"{c.entry.name} ({c.clause()})" for c in market.overstated)) + "</p>"
    return f"""
    <section class="panel-block">
      <h3>Replacement market <span class="muted">&middot; how replaceable each position is from this league's wire</span></h3>
      <p class="roster-note">Scarcest first.</p>
      <ul class="alert-list">{items}</ul>
      {notes}
    </section>
    """


_ECONOMY_LABEL_KIND = {
    "Frequent Trader": "positive", "Inactive Trader": "caution", "Pick Accumulator": "accent",
    "Pick Seller": "accent", "Position Heavy": "neutral",
}


def _league_economy_section(economy: LeagueEconomy | None, my_roster_id: int) -> str:
    if economy is None:
        return ""
    labelled = economy.labelled()
    if not labelled and not economy.limited_sample:
        return ""
    note = ""
    if economy.limited_sample:
        n = economy.total_completed_trades
        note = (
            f'<p class="roster-note">Limited trade-history sample ({n} completed trade{"s" if n != 1 else ""} this season) '
            "&mdash; trader-activity labels suppressed.</p>"
        )
    rows = []
    for m in sorted(labelled, key=lambda m: (m.roster_id != my_roster_id, -m.completed_trades)):
        who = esc(m.team_name or m.username or f"roster {m.roster_id}") + (" <span class=\"muted\">(you)</span>" if m.roster_id == my_roster_id else "")
        chips = "".join(_chip(label, _ECONOMY_LABEL_KIND.get(label, "neutral")) for label in m.labels)
        rows.append(f'<tr><td class="player-cell">{who}</td><td>{chips}</td><td class="waiver-reason">{esc(m.describe())}</td></tr>')
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Manager</th><th>Tendencies</th><th>Detail</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        if rows
        else ""
    )
    return f"""
    <section class="panel-block">
      <h3>League economy <span class="muted">&middot; this season's transaction record</span></h3>
      {note}{table}
    </section>
    """


_ALERT_SEVERITY_KIND = {"high": "negative", "medium": "caution", "low": "neutral"}


def _alerts_list(notes: list[TimeSensitiveNote]) -> str:
    if not notes:
        return '<p class="empty-note">Nothing flagged.</p>'
    items = []
    for n in notes:
        kind = _ALERT_SEVERITY_KIND.get(n.severity, "caution")
        items.append(f'<li class="alert-item alert-{kind}"><strong>{esc(n.player_name)}</strong> &middot; {esc(n.note)}</li>')
    return f'<ul class="alert-list">{"".join(items)}</ul>'


CONTEXT_SUMMARY = "lineup, roster, clogs, replacement market, stash board, buyer board, schedule, draft capital, league economy"
DIAGNOSTICS_SUMMARY = "decision ledger, outcome facts, watchlist"

_STATUS_CHIP_KIND = {CONTENDER: "positive", MIDDLING: "neutral", REBUILD: "caution"}
_PLAYOFF_CHIP_KIND = {COMFORTABLE: "positive", BUBBLE: "caution", LONG_SHOT: "caution", OUT: "negative"}


def _league_panel(data: LeagueReportData) -> str:
    slug = _slug(data.league.name)
    status_chip = ""
    status_reason = ""
    if data.team_status:
        kind = _STATUS_CHIP_KIND.get(data.team_status.status, "neutral")
        status_chip = _chip(data.team_status.status.upper(), kind)
        status_reason = f'<p class="muted status-reason">{esc(data.team_status.reason)}</p>'
    playoff_chip = ""
    playoff_reason = ""
    if data.playoff:
        playoff_chip = _chip("Playoffs: " + data.playoff.label, _PLAYOFF_CHIP_KIND.get(data.playoff.label, "neutral"))
        if data.playoff.deadline_window:
            playoff_chip += _chip("Deadline Window", "accent")
        playoff_reason = f'<p class="muted status-reason">{esc(data.playoff.reason)}</p>'
    header = f"""
    <header class="panel-header">
      <h2>{esc(data.league.name)}</h2>
      <div class="panel-tags">{_chip(data.league.kind.capitalize(), "accent")}{_chip(data.fmt_desc, "neutral") if data.fmt_desc else ""}{status_chip}{playoff_chip}</div>
    </header>
    {status_reason}{playoff_reason}
    """

    if data.error:
        body = f'<p class="empty-note">{esc(data.error)}</p>'
    elif not data.drafted:
        body = '<p class="empty-note">Not drafted yet this season &mdash; check back after your draft for roster/trade/waiver analysis.</p>'
    else:
        alert_count = len(data.time_sensitive)
        has_high_alert = any(n.severity == "high" for n in data.time_sensitive)
        alerts_section = f"""
        <section class="panel-block">
          <h3>Time-sensitive {f'<span class="badge-count badge-alert">{alert_count}</span>' if alert_count else ""}</h3>
          {_alerts_list(data.time_sensitive)}
        </section>
        """
        waivers_html = (
            f'<p class="empty-note">{esc(data.waivers_note)}</p>'
            if data.waivers_note
            else _waiver_table(data.waiver_targets, data.waiver_impacts, data.conflicts, data.faab, data.provenance)
        )
        if data.faab_note and not data.waivers_note and data.waiver_targets:
            waivers_html += f'<p class="muted">{esc(data.faab_note)}</p>'
        trades_and_waivers = f"""
        <section class="panel-block">
          <h3>Trade offers</h3>
          <div class="trade-grid">
            {"".join(_trade_card(p, i, data.trade_impacts[i - 1] if i - 1 < len(data.trade_impacts) else None, data.ladders.get(i - 1), data.trade_economics[i - 1] if i - 1 < len(data.trade_economics) else None, conflict_for(data.conflicts, TRADE, str(i - 1)), data.provenance.get((TRADE, str(i - 1)))) for i, p in enumerate(data.proposals, start=1)) if data.proposals else '<p class="empty-note">No trade offers cleared the value-match bar this week.</p>'}
          </div>
          {_consolidation_block(data.consolidations)}
        </section>
        <section class="panel-block">
          <h3>Waiver targets</h3>
          {waivers_html}
          {_streamers_block(data.streamers)}
          {_defensive_add_block(data.defensive_add)}
        </section>
        {_drop_candidates_section(data.drop_candidates)}
        """
        # Context that explains or qualifies the moves above, collapsed by
        # default so twelve capabilities don't read as twelve equal panels.
        # The roster and the optimized lineup live here too: they are
        # reference material, not this week's decisions.
        context_html = (
            _lineup_section(data)
            + _roster_section(data.roster, data.currency)
            + _roster_clogs_section(data.roster_clogs)
            + _replacement_market_section(data.replacement)
            + _stash_section(data.stash)
            + _buyer_board_section(data.buyer_boards)
            + _schedule_section(data.windows)
            + _pick_opportunity_section(data.pick_opportunity, data.replacement)
            + _league_economy_section(data.league_economy, data.roster.roster_id)
        )
        context = (
            f'<details class="context-details"><summary>Roster context &middot; {CONTEXT_SUMMARY}</summary>{context_html}</details>'
            if context_html.strip()
            else ""
        )
        # A high-severity alert (a long-term injury not yet moved to an
        # IR slot, or a starter's bye) is time-boxed to this week's
        # lineup lock — don't make a scrolling reader pass sections that
        # may all be empty-state ("no trades this week") to reach it.
        body = (
            (alerts_section if has_high_alert else "")
            + _matchup_section(data.matchup)
            + _lineup_decisions_section(data.lineup_decisions)
            + _lineup_leverage_section(
                data.lineup_leverage, data.currency, data.replacement_clauses,
                close_calls_shown=data.lineup_decisions is not None,
            )
            + trades_and_waivers
            + ("" if has_high_alert else alerts_section)
            + context
        )

    return f'<div class="panel" id="panel-{slug}" role="tabpanel">{header}{body}</div>'


def _overview_row(data: LeagueReportData) -> str:
    slug = _slug(data.league.name)
    team_status_chip = ""
    if data.error:
        status = _chip("Error", "negative")
        stats = ""
    elif not data.drafted:
        status = _chip("Pre-draft", "caution")
        stats = ""
    else:
        status = _chip("Ready", "positive")
        if data.team_status:
            team_status_chip = _chip(data.team_status.status.capitalize(), _STATUS_CHIP_KIND.get(data.team_status.status, "neutral"))
        n_trades = len(data.proposals)
        n_waivers = len(data.waiver_targets)
        n_alerts = len(data.time_sensitive)
        stats = (
            f'<span class="overview-stat">{n_trades} trade{"s" if n_trades != 1 else ""}</span>'
            f'<span class="overview-stat">{n_waivers} waiver{"s" if n_waivers != 1 else ""}</span>'
            + (f'<span class="overview-stat overview-stat-alert">{n_alerts} alert{"s" if n_alerts != 1 else ""}</span>' if n_alerts else "")
        )
    return f"""
    <a class="overview-row" href="#{slug}" data-target="{slug}">
      <span class="overview-name">{esc(data.league.name)}</span>
      <span class="overview-kind">{esc(data.league.kind.capitalize())}</span>
      {status}
      {team_status_chip}
      <span class="overview-stats">{stats}</span>
    </a>
    """


_ACTION_KIND_META = {
    "alert": ("&#128680;", "negative"),
    "trade": ("&#128260;", "accent"),
    "waiver": ("&#9989;", "positive"),
    "roster": ("&#9986;", "caution"),
    "defensive_add": ("&#128737;", "caution"),
    "streamer": ("&#127926;", "neutral"),
}
_ACTION_KIND_LABEL = {"defensive_add": "Block", "streamer": "Stream"}


def _priority_action_row(a: PriorityAction, *, show_priority: bool = True) -> str:
    """Headline (what) → priority (how urgent) → Why now → Against, with
    the old free-text detail demoted to a muted trailing line. A Conflicted
    move gets a chip; it no longer swallows the start of the detail."""
    icon, kind = _ACTION_KIND_META.get(a.kind, ("", "neutral"))
    league_slug = _slug(a.league_name)
    v = action_view(a)
    priority = f'<span class="action-priority muted">{esc(v.priority)}</span>' if v.priority and show_priority else ""
    why = f'<span class="action-why"><b>Why now:</b> {esc(" · ".join(v.why_now))}</span>' if v.why_now else ""
    against = f'<span class="action-why"><b>Against:</b> {esc(" · ".join(v.against))}</span>' if v.against else ""
    conflict = f'<span class="action-why"><b>Conflict:</b> {esc(v.conflict_note)}</span>' if v.conflict_note else ""
    detail = f'<span class="action-detail muted">{esc(v.detail)}</span>' if v.detail else ""
    return f"""
    <a class="action-row" href="#{league_slug}" data-target="{league_slug}">
      <span class="action-icon" aria-hidden="true">{icon}</span>
      <span class="action-body">
        <span class="action-headline">{esc(a.headline)}
          <span class="action-league muted">{esc(v.league)}</span>
          {_chip(CONFLICTED, "negative") if v.conflicted else ""}</span>
        {priority}{why}{against}{conflict}{detail}
      </span>
      {_chip(_ACTION_KIND_LABEL.get(a.kind, a.kind.capitalize()), kind)}
    </a>
    """


def _provenance_rows(prov, seen: set | None = None, *, groups=("For", "Against")) -> str:
    """For / Against (or Context) rows; texts are the provenance layer's,
    minus any fact `seen` already put on screen."""
    if prov is None:
        return ""
    seen = seen if seen is not None else set()
    by_label = {"For": prov.reasons_for, "Against": prov.reasons_against, "Context": prov.context}
    rows = []
    for label in groups:
        kept = [r for r in by_label[label] if fact_of(r.text) not in seen]
        claim(seen, [r.text for r in kept])
        if kept:
            items = "".join(f"<li>{esc(r.text)} <span class=muted>[{esc(r.category)}]</span></li>" for r in kept)
            rows.append(f'<div class="why-row"><b>{label}:</b><ul>{items}</ul></div>')
    return "".join(rows)


def _provenance_block(prov, seen: set | None = None) -> str:
    rows = _provenance_rows(prov, seen)
    if not rows:
        return ""
    return f'<div class="why-now"><span class="rationale-label">Why now</span>{rows}</div>'


def _priority_actions_section(actions: list[PriorityAction]) -> str:
    """Two groups: what is time-boxed or materially changes the lineup, and
    the value plays that can wait. A shared priority line is printed once
    above the list rather than on every identical row."""
    if not actions:
        return """
        <section class="panel-block priority-block">
          <h3>Best moves right now</h3>
          <p class="empty-note">Nothing urgent across any league &mdash; hold.</p>
        </section>
        """
    do_now, optional = split_actions(actions)
    shared = shared_priority(actions)
    shared_line = f'<p class="muted">{esc(ALL_SAME_PRIORITY_NOTE.format(priority=shared))}</p>' if shared else ""

    def group(title: str, rows: list[PriorityAction], note: str = "") -> str:
        if not rows:
            return ""
        sub = f' <span class="muted">&middot; {esc(note)}</span>' if note else ""
        body = "".join(_priority_action_row(a, show_priority=not shared) for a in rows)
        return f'<h4 class="action-group">{esc(title)}{sub}</h4><div class="action-list">{body}</div>'

    return f"""
    <section class="panel-block priority-block">
      <h3>Best moves right now</h3>
      <p class="muted">{esc(ORDERING_NOTE)}</p>
      {shared_line}
      {group("Do this week", do_now)}
      {group("Optional value plays", optional, "" if do_now else NOTHING_URGENT_NOTE)}
    </section>
    """


_DELTA_KIND_META = {
    "status": ("Team status", "caution"),
    "roster": ("Roster moves", "accent"),
    "recommendation": ("Recommendations", "neutral"),
    "valuation": ("Value swings (15%+)", "neutral"),
}


def _delta_section(delta: DecisionDelta | None) -> str:
    if delta is None:
        return ""  # first complete run — nothing to compare against yet
    since = delta.since.strftime("%b %d, %H:%M UTC")
    if not delta.items:
        body = '<p class="empty-note">No meaningful changes &mdash; same statuses, recommendations, and rosters as last time.</p>'
    else:
        items = []
        for kind, (label, chip_kind) in _DELTA_KIND_META.items():
            for i in delta.by_kind(kind):
                items.append(
                    f'<li class="alert-item">{_chip(label, chip_kind)} <strong>{esc(i.league_name)}</strong> &middot; {esc(i.text)}</li>'
                )
        body = f'<ul class="alert-list">{"".join(items)}</ul>'
    return f"""
    <section class="panel-block">
      <h3>Since last run <span class="muted">&middot; {esc(since)}</span></h3>
      {body}
    </section>
    """


def _asymmetry_list(asymmetries) -> str:
    if not asymmetries:
        return ""
    items = "".join(f"<li>{esc(a.describe())}</li>" for a in asymmetries)
    return ('<p class="muted">Where the same player is cheapest to move (an Abundant/Normal market and a small edge over the wire) versus costliest &mdash; a fact for the trade engine, not a sell signal:</p>'
            f'<ul class="alert-list">{items}</ul>')


def _portfolio_section(portfolio: PortfolioExposure | None, asymmetries=()) -> str:
    if portfolio is None or not portfolio.players:
        return ""
    rows = []
    for p in portfolio.players:
        flags = ""
        if p.level:
            flags += _chip(p.level, "negative" if p.level == VERY_HIGH else "caution")
        if p.qb_start_flag:
            flags += _chip("Starting QB x" + str(len(p.started_in)), "caution")
        rows.append(
            "<tr>"
            f'<td class="player-cell">{esc(p.name)}</td>'
            f'<td>{esc(p.position or "?")}</td>'
            f'<td>{esc(p.team or "-")}</td>'
            f'<td class="tabular">{p.count} / {portfolio.total_leagues}</td>'
            f'<td class="tabular">{len(p.started_in)}</td>'
            f"<td>{flags}</td>"
            "</tr>"
        )
    return f"""
    <section class="panel-block">
      <h3>Portfolio exposure <span class="muted">&middot; same player, several rosters</span></h3>
      <p class="roster-note">One injury hits every team holding him. A risk flag and tie-breaker, not a sell signal.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Player</th><th>Pos</th><th>Team</th><th>Leagues</th><th>Starting in</th><th>Flag</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table></div>
    {_asymmetry_list(asymmetries)}
      </section>
    """


_HEALTH_CHIP_KIND = {"Fresh": "positive", "Usable": "neutral", "Partial": "caution", "Stale": "caution", "Unavailable": "negative"}


def _signal_health_section(report: WeeklyReportData) -> str:
    """One chip per source with its label; the run's notes and suppressed
    features underneath. Falls back to bare ages without a health grade."""
    if report.health is None:
        chips = "".join(
            f'<span class="freshness-chip">{esc(source)} <b>{age_str(age)}</b></span>'
            for source, age in report.source_freshness.items()
        ) + f'<span class="freshness-chip">ff_dynasty_pass <b>{esc(report.ff_status)}</b></span>'
        title = "Data freshness"
        notes_html = ""
    else:
        chips = "".join(
            f'<span class="freshness-chip">{esc(s.display_name)} {_chip(esc(s.label), _HEALTH_CHIP_KIND.get(s.label, "neutral"))}'
            f'{" <b>" + esc(_age_text(s.cache_age)) + "</b>" if s.cache_age is not None else ""}'
            f'{" <span class=muted>" + str(s.coverage) + " rows</span>" if s.coverage is not None else ""}</span>'
            for s in report.health.signals
        )
        title = "Signal health &middot; " + esc(health_state(report))
        items = [f"<li>{esc(n)}</li>" for n in report.health.notes]
        items += [f"<li>Suppressed this run: {esc(f.replace('_', ' '))} &mdash; {esc(why)}</li>" for f, why in sorted(report.suppressed.items())]
        notes_html = f'<ul class="alert-list">{"".join(items)}</ul>' if items else ""
    extra = []
    if report.usage_note:
        extra.append(f'<p class="muted">Player usage: {esc(report.usage_note)}.</p>')
    if report.crosswalk_note:
        extra.append(f'<p class="muted">{esc(report.crosswalk_note)}</p>')
    return f"""
      <section class="panel-block">
        <h3>{title}</h3>
        <div class="freshness-grid">{chips}</div>
        {notes_html}{"".join(extra)}
      </section>
    """


def _age_text(age) -> str:
    hours = age.total_seconds() / 3600
    if hours < 1:
        return f"{int(age.total_seconds() // 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


MAX_DIAGNOSTIC_LINES = 10


def _diagnostics_section(report: WeeklyReportData) -> str:
    """Collapsed by default: the ledger's counts, the observed outcome
    facts, the watchlist's new triggers. Same fields as the Markdown."""
    parts: list[str] = []
    if report.ledger_summary:
        rows = "".join(
            f"<li><b>{esc(action)}</b>: " + ", ".join(f"{esc('awaiting outcome' if label == '(open)' else label)} {n}" for label, n in counts.items()) + "</li>"
            for action, counts in report.ledger_summary.items()
        )
        parts.append(f'<h4>Decision ledger</h4><p class="muted">Recommendations recorded, by action and observed outcome.</p><ul class="alert-list">{rows}</ul>')
    observed = [f for f in report.outcome_facts if f.state == OBSERVED]
    if observed:
        rows = "".join(f"<li>{esc(f.describe())}</li>" for f in observed[-MAX_DIAGNOSTIC_LINES:])
        more = f'<li class="muted">… {len(observed) - MAX_DIAGNOSTIC_LINES} earlier facts not shown</li>' if len(observed) > MAX_DIAGNOSTIC_LINES else ""
        parts.append(f'<h4>Outcome facts</h4><p class="muted">Descriptive; the value moves are the sources\', not a verdict.</p><ul class="alert-list">{rows}{more}</ul>')
    if report.watchlist_sections or report.watchlist_new:
        rows = "".join(
            f"<li>{_chip(section, 'accent')} {esc(line)}</li>"
            for section, lines_ in report.watchlist_sections.items()
            for line in lines_
        ) or "".join(f"<li>{_chip('New Trigger', 'accent')} {esc(line)}</li>" for line in report.watchlist_new)
        if report.watchlist_watching:
            rows += f'<li class="muted">{report.watchlist_watching} more item(s) watched with nothing new to say</li>'
        parts.append(f'<h4>Watchlist</h4><ul class="alert-list">{rows}</ul>')
    if not parts:
        return ""
    return (
        f'<details class="context-details"><summary>Diagnostics and history &middot; {DIAGNOSTICS_SUMMARY}</summary>'
        + "".join(parts) + "</details>"
    )


def _overview_panel(report: WeeklyReportData) -> str:
    rows = "".join(_overview_row(d) for d in report.leagues)
    # A degraded run must be visible before anything else on the page, not
    # only down in the health section a reader may never reach.
    banner = health_banner(report)
    banner_html = (
        f'<p class="health-banner">{_chip(banner.label, "negative" if banner.degraded else "caution")} '
        f"{esc(banner.text)} Details in Signal health below.</p>"
        if banner is not None
        else ""
    )
    return f"""
    <div class="panel" id="panel-overview" role="tabpanel">
      <header class="panel-header">
        <h2>Overview</h2>
        <p class="muted">Generated {report.generated_at.strftime('%b %d, %Y &middot; %H:%M UTC')}</p>
      </header>
      {banner_html}
      {_priority_actions_section(report.priority_actions)}
      {_delta_section(report.delta)}
      <section class="panel-block">
        <h3>Leagues</h3>
        <div class="overview-grid">{rows}</div>
      </section>
      {_portfolio_section(report.portfolio, report.asymmetries)}
      {_signal_health_section(report)}
      {_diagnostics_section(report)}
    </div>
    """


def _nav_items(report: WeeklyReportData) -> str:
    items = ['<a class="nav-item nav-item-active" href="#overview" data-target="overview">Overview</a>']
    for d in report.leagues:
        slug = _slug(d.league.name)
        alert = len(d.time_sensitive) if d.drafted else 0
        high = any(n.severity == "high" for n in d.time_sensitive) if d.drafted else False
        badge = (
            f'<span class="badge-count {"badge-alert" if high else "badge-note"}" title="{alert} time-sensitive note{"s" if alert != 1 else ""}">{alert}</span>'
            if alert else ""
        )
        items.append(f'<a class="nav-item" href="#{slug}" data-target="{slug}">{esc(d.league.name)}{badge}</a>')
    return "".join(items)


CSS = """
.why-now { margin: 8px 0; padding: 8px 10px; background: var(--neutral-bg); border-radius: 6px; font-size: 0.92em; }
.why-now .why-row { margin: 2px 0; }
.why-now ul { margin: 2px 0 2px 16px; padding: 0; }
.action-priority, .action-why { display: block; font-size: 0.85em; margin-top: 2px; }
.action-headline .chip { margin-left: 8px; vertical-align: 1px; }
.action-league { font-size: 12px; font-weight: 400; margin-left: 8px; }
.health-banner {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  margin: 0 0 20px; padding: 10px 14px; font-size: 13px;
  /* The body text is --ink, not --negative: --negative on --negative-bg
     is a 4.2:1 pair, which is fine for a chip label but under AA for a
     sentence the reader is meant to act on. The chip and rule carry the
     colour instead. */
  background: var(--negative-bg); color: var(--ink);
  border-left: 3px solid var(--negative); border-radius: 0 8px 8px 0;
}
.row-chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.row-details > summary {
  cursor: pointer; margin-top: 6px; font-size: 11px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.04em; color: var(--accent-ink);
}
.row-details[open] > summary { margin-bottom: 2px; }
:root {
  --ground: #13161A;
  --surface: #1B1F24;
  --surface-raised: #232830;
  --ink: #EDEDE7;
  --ink-muted: #9CA0A6;
  --ink-faint: #8A8F96;
  --line: #262B31;
  --accent: #E0A94D;
  --accent-ink: #F4CD82;
  --positive: #67C295;
  --positive-bg: #1B2A22;
  --negative: #E3897A;
  --negative-bg: #2E1F1C;
  --caution: #B8A2CC;
  --caution-bg: #26212E;
  --neutral-bg: #1E2126;
  --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 28px rgba(0,0,0,0.45);
  --radius: 10px;
  --font-display: "Big Shoulders Display", "Arial Narrow", sans-serif;
  --font-body: "Public Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "Courier New", monospace;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --ground: #F4F1E9;
    --surface: #FFFFFF;
    --surface-raised: #FBF9F3;
    --ink: #1C1F1B;
    --ink-muted: #625C4E;
    --ink-faint: #8B8474;
    --line: #DEDACB;
    --accent: #B8842A;
    --accent-ink: #6E4F17;
    --positive: #3E7A5B;
    --positive-bg: #E1EDE4;
    --negative: #B14B3E;
    --negative-bg: #F3E1DE;
    --caution: #8A6FA3;
    --caution-bg: #EAE3F0;
    --neutral-bg: #ECE8DC;
    --shadow: 0 1px 2px rgba(28,31,27,0.06), 0 4px 16px rgba(28,31,27,0.05);
  }
}
:root[data-theme="light"] {
  --ground: #F4F1E9;
  --surface: #FFFFFF;
  --surface-raised: #FBF9F3;
  --ink: #1C1F1B;
  --ink-muted: #625C4E;
  --ink-faint: #8B8474;
  --line: #DEDACB;
  --accent: #B8842A;
  --accent-ink: #6E4F17;
  --positive: #3E7A5B;
  --positive-bg: #E1EDE4;
  --negative: #B14B3E;
  --negative-bg: #F3E1DE;
  --caution: #8A6FA3;
  --caution-bg: #EAE3F0;
  --neutral-bg: #ECE8DC;
  --shadow: 0 1px 2px rgba(28,31,27,0.06), 0 4px 16px rgba(28,31,27,0.05);
}
:root[data-theme="dark"] {
  --ground: #13161A;
  --surface: #1B1F24;
  --surface-raised: #232830;
  --ink: #EDEDE7;
  --ink-muted: #9CA0A6;
  --ink-faint: #8A8F96;
  --line: #262B31;
  --accent: #E0A94D;
  --accent-ink: #F4CD82;
  --positive: #67C295;
  --positive-bg: #1B2A22;
  --negative: #E3897A;
  --negative-bg: #2E1F1C;
  --caution: #B8A2CC;
  --caution-bg: #26212E;
  --neutral-bg: #1E2126;
  --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 28px rgba(0,0,0,0.45);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: var(--font-body);
  font-size: 15px;
  line-height: 1.5;
}
h1, h2, h3 { font-family: var(--font-display); font-weight: 700; text-wrap: balance; margin: 0; letter-spacing: 0.01em; }
a { color: inherit; }
.muted { color: var(--ink-muted); font-weight: 400; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }

.app-shell { display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }
.app-content { min-width: 0; }
.brand {
  padding: 22px 20px 16px;
  border-bottom: 1px solid var(--line);
}
.brand-title { font-family: var(--font-display); font-size: 26px; color: var(--accent-ink); line-height: 1; }
.brand-sub { font-size: 12px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.08em; margin-top: 4px; }

.sidebar { background: var(--surface); border-right: 1px solid var(--line); position: sticky; top: 0; height: 100vh; overflow-y: auto; }
.nav-list { display: flex; flex-direction: column; padding: 10px; gap: 2px; }
.nav-item {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 10px 12px; border-radius: 8px; text-decoration: none;
  color: var(--ink-muted); font-size: 14px; font-weight: 600;
}
.nav-item:hover { background: var(--surface-raised); color: var(--ink); }
.nav-item-active { background: var(--neutral-bg); color: var(--ink); }

.main { padding: 28px 32px 60px; max-width: 980px; }
.panel { display: none; animation: none; }
.panel-visible { display: block; }
.panel-header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px 16px; margin-bottom: 20px; }
.panel-header h2 { font-size: 34px; }
.panel-tags { display: flex; gap: 8px; flex-wrap: wrap; }
.panel-block { margin-bottom: 32px; }
.status-reason { font-size: 13px; margin: -12px 0 20px; }
.panel-block h3 { font-size: 18px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }

.chip {
  display: inline-block; padding: 3px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
  white-space: nowrap;
}
.chip-accent { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent-ink); }
.chip-positive { background: var(--positive-bg); color: var(--positive); }
.chip-negative { background: var(--negative-bg); color: var(--negative); }
.chip-caution { background: var(--caution-bg); color: var(--caution); }
.chip-neutral { background: var(--neutral-bg); color: var(--ink-muted); }

.table-scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: var(--radius); }
table { border-collapse: collapse; width: 100%; min-width: 480px; background: var(--surface); }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); font-size: 14px; white-space: nowrap; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-faint); font-weight: 700; }
tbody tr:last-child td { border-bottom: none; }
.player-cell { font-weight: 700; white-space: normal; }
.waiver-reason { white-space: normal; color: var(--ink-muted); font-size: 13px; min-width: 260px; }
.tabular { font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
.confidence-flag { font-size: 11px; cursor: help; }

.pctl-cell { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.pctl-bar { width: 56px; height: 6px; border-radius: 3px; background: var(--neutral-bg); overflow: hidden; }
.pctl-fill { height: 100%; background: var(--accent); border-radius: 3px; }
.pctl-num sup { font-size: 9px; }
.pctl-unranked { color: var(--ink-faint); font-size: 12px; }

.trend { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 6px; font-size: 13px; font-weight: 700; }
.trend-positive { background: var(--positive-bg); color: var(--positive); }
.trend-negative { background: var(--negative-bg); color: var(--negative); }
.trend-neutral { background: var(--neutral-bg); color: var(--ink-faint); }

.bench-strip { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.bench-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-faint); margin-right: 4px; }
.bench-chip { font-size: 12px; background: var(--surface-raised); border: 1px solid var(--line); padding: 3px 9px; border-radius: 999px; color: var(--ink-muted); }
.bench-chip b { font-family: var(--font-mono); color: var(--ink); font-weight: 600; }
.roster-note { font-size: 13px; color: var(--ink-muted); margin: 10px 0 0; }

.trade-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.trade-card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); }
.trade-card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.trade-index { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.06em; }
.trade-flow { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.trade-side { flex: 1; min-width: 0; }
.trade-side-label { display: block; font-size: 11px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px; }
.trade-arrow { color: var(--accent); font-size: 16px; }
.asset-list { display: flex; flex-direction: column; gap: 3px; }
.asset { font-size: 14px; font-weight: 700; }
.pick-tag { display: inline-block; font-size: 9px; font-weight: 700; letter-spacing: 0.04em; color: var(--accent-ink); background: color-mix(in srgb, var(--accent) 18%, transparent); border-radius: 4px; padding: 1px 4px; margin-right: 5px; vertical-align: 1px; }
.trade-signals { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.trade-target { font-size: 13px; color: var(--ink-muted); margin: 0 0 4px; font-variant-numeric: tabular-nums; }
.impact-block { margin: 6px 0 4px; padding: 8px 12px; border-left: 3px solid var(--accent); background: var(--surface-raised); border-radius: 0 8px 8px 0; }
.impact-block ul { margin: 4px 0 0; padding-left: 18px; font-size: 13px; color: var(--ink); }
.impact-inline { margin-top: 4px; font-size: 12px; color: var(--ink); }
.trade-message { margin: 8px 0 4px; padding: 10px 12px; background: var(--neutral-bg); border-radius: 8px; }
.trade-message-text { margin: 4px 0 0; font-size: 13px; color: var(--ink); font-style: italic; }
.trade-details summary { cursor: pointer; font-size: 13px; font-weight: 700; color: var(--accent-ink); padding: 6px 0; }
.trade-rationale { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 6px; }
.trade-details ul, .caveats ul { margin: 4px 0 0; padding-left: 18px; font-size: 13px; color: var(--ink-muted); }
.trade-details > div + div { margin-top: 10px; }
.rationale-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-faint); }
.streamers { margin-top: 14px; }
.conflict-block { border-left: 3px solid var(--negative); padding-left: 10px; }
.context-details { margin-top: 8px; border-top: 1px solid var(--line); padding-top: 12px; }
.context-details > summary { cursor: pointer; font-family: var(--font-display); font-size: 18px; font-weight: 700; color: var(--ink-muted); list-style: none; padding: 6px 0 14px; }
.context-details > summary::before { content: "\\25B8"; display: inline-block; margin-right: 8px; color: var(--accent); transition: transform 0.15s; }
.context-details[open] > summary::before { transform: rotate(90deg); }
.ladder { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); }
.ladder ul { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.ladder-step { display: inline-block; min-width: 118px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--accent-ink); }
.ladder-message { padding-left: 118px; font-style: italic; color: var(--ink-muted); }
.caveats { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); }
.caveat-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--caution); }
.caveat-item { color: var(--caution) !important; }

.alert-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.alert-item { border-radius: 8px; padding: 8px 12px; font-size: 13px; border-left: 3px solid var(--line); background: var(--surface-raised); }
.drop-reasons { margin-top: 4px; font-size: 12px; color: var(--ink-muted); }
.alert-negative { border-left-color: var(--negative); }
.alert-caution { border-left-color: var(--caution); }
.badge-count { display: inline-flex; align-items: center; justify-content: center; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 999px; font-size: 11px; font-family: var(--font-mono); }
.badge-alert { background: var(--negative-bg); color: var(--negative); }
.badge-note { background: var(--neutral-bg); color: var(--ink-muted); }

.empty-note { color: var(--ink-faint); font-size: 14px; font-style: italic; }

.priority-block { padding: 4px 0 20px; border-bottom: 1px solid var(--line); margin-bottom: 28px; }
.action-list { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; background: var(--surface); }
.action-row { display: flex; align-items: center; gap: 12px; padding: 12px 16px; text-decoration: none; color: var(--ink); border-bottom: 1px solid var(--line); }
.action-row:last-child { border-bottom: none; }
.action-row:hover { background: var(--surface-raised); }
.action-icon { font-size: 16px; flex: 0 0 auto; }
.action-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.action-headline { font-weight: 700; font-size: 14px; }
.action-detail { font-size: 12px; }

.overview-grid { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; background: var(--surface); }
.overview-row { display: grid; grid-template-columns: 1fr auto auto auto 1fr; align-items: center; gap: 14px; padding: 12px 16px; text-decoration: none; color: var(--ink); border-bottom: 1px solid var(--line); }
.overview-row:last-child { border-bottom: none; }
.overview-row:hover { background: var(--surface-raised); }
.overview-name { font-weight: 700; }
.overview-kind { font-size: 12px; color: var(--ink-faint); }
.overview-stats { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
.overview-stat { font-size: 12px; font-family: var(--font-mono); color: var(--ink-muted); background: var(--neutral-bg); padding: 2px 8px; border-radius: 999px; }
.overview-stat-alert { color: var(--negative); background: var(--negative-bg); }

.freshness-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.freshness-chip { font-size: 12px; background: var(--surface); border: 1px solid var(--line); padding: 5px 10px; border-radius: 999px; color: var(--ink-muted); }
.freshness-chip b { font-family: var(--font-mono); color: var(--ink); font-weight: 600; }

.mobile-tabs { display: none; }

@media (max-width: 860px) {
  .app-shell { grid-template-columns: 1fr; }
  .sidebar { display: none; }
  .mobile-tabs {
    display: flex; overflow-x: auto; gap: 6px; padding: 12px 14px;
    background: var(--surface); border-bottom: 1px solid var(--line);
    position: sticky; top: 0; z-index: 5; -webkit-overflow-scrolling: touch;
  }
  .mobile-tabs .nav-item { flex: 0 0 auto; background: var(--neutral-bg); border-radius: 999px; padding: 8px 14px; }
  .mobile-tabs .nav-item-active { background: var(--accent); color: var(--surface); }
  .main { padding: 20px 16px 48px; }
  .panel-header h2 { font-size: 26px; }
  .trade-rationale { grid-template-columns: 1fr; }
  .overview-row { grid-template-columns: 1fr; gap: 4px; }
  .overview-stats { justify-content: flex-start; }
}
"""

JS = """
(function () {
  var targets = document.querySelectorAll('[data-target]');
  var panels = document.querySelectorAll('.panel');
  function activate(slug) {
    panels.forEach(function (p) { p.classList.toggle('panel-visible', p.id === 'panel-' + slug); });
    targets.forEach(function (t) { t.classList.toggle('nav-item-active', t.dataset.target === slug); });
  }
  targets.forEach(function (t) {
    t.addEventListener('click', function (e) {
      e.preventDefault();
      history.replaceState(null, '', '#' + t.dataset.target);
      activate(t.dataset.target);
      window.scrollTo({ top: 0, behavior: 'auto' });
    });
  });
  var initial = (location.hash || '#overview').slice(1);
  if (!document.getElementById('panel-' + initial)) { initial = 'overview'; }
  activate(initial);
})();
"""


def render_dashboard_html(report: WeeklyReportData) -> str:
    nav_items = _nav_items(report)
    panels = _overview_panel(report) + "".join(_league_panel(d) for d in report.leagues)

    # The charset declaration matters for the local double-click / static-server
    # case: without it a server that guesses latin-1 turns every em dash and
    # arrow in the report into mojibake. An Artifact publish already supplies
    # one in its own <head>; a second declaration there is simply ignored.
    return f"""<meta charset="utf-8">
<title>Fantasy Command Center</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;800&family=Public+Sans:wght@400;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap">
<style>{CSS}</style>
<div class="app-shell">
  <nav class="sidebar" aria-label="Leagues">
    <div class="brand">
      <div class="brand-title">Fantasy Command Center</div>
      <div class="brand-sub">Weekly dynasty &amp; redraft desk</div>
    </div>
    <div class="nav-list">{nav_items}</div>
  </nav>
  <div class="app-content">
    <nav class="mobile-tabs" aria-label="Leagues (mobile)">{nav_items}</nav>
    <main class="main">{panels}</main>
  </div>
</div>
<script>{JS}</script>
"""
