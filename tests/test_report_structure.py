"""The rendered SHAPE, in both outputs.

`test_renderer_parity` guards that every decision-layer sentence reaches
both renderers. This file guards the layer above that: the order the
reader meets things in, what is visible versus behind disclosure, and the
promise that moving a sentence behind a `<details>` never deletes it.
"""
from __future__ import annotations

import datetime as dt
import html as html_lib
import re

import pytest

from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.html_report import _league_panel, _overview_panel, render_dashboard_html
from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.move_impact import MoveImpact, RosterSnapshot
from sleeper_tool.recommendation_conflicts import CONFLICTED, TRADE, Conflict
from sleeper_tool.report import render_league_section, render_weekly_report
from sleeper_tool.report_data import LeagueReportData, PriorityAction, WeeklyReportData
from sleeper_tool.action_priority import PriorityKey
from sleeper_tool.trade_opportunity_cost import TradeEconomics
from sleeper_tool.trade_types import DropCandidate, TradeProposal
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget

SLOTS = ("QB", "RB", "WR", "TE", "FLEX", "BN", "BN")


def _snapshot(lineup) -> RosterSnapshot:
    return RosterSnapshot(
        lineup=lineup, weekly_points=100.0, depth_needs=[], status="contender", strength_percentile=60.0,
        roster_value=10_000.0, avg_starter_age=26.0,
    )


def _league(**kw) -> LeagueReportData:
    qb = make_entry(player_id="qb1", name="Quinn Structqb", position="QB")
    rb = make_entry(player_id="rb1", name="Rex Structrb", position="RB")
    wr = make_entry(player_id="wr1", name="Wade Structwr", position="WR")
    te = make_entry(player_id="te1", name="Tom Structte", position="TE")
    flex = make_entry(player_id="wr2", name="Wes Structflex", position="WR")
    bench = make_entry(player_id="wr3", name="Barry Structbench", position="WR", is_starter=False)
    roster = make_roster(roster_id=1, entries=[qb, rb, wr, te, flex, bench], fmt=make_format(roster_positions=SLOTS))
    base = dict(
        league=make_league_info(name="Struct League"), fmt_desc="Superflex, Full PPR", currency="dynasty",
        drafted=True, roster=roster, lineup=optimize_lineup(roster),
    )
    base.update(kw)
    return LeagueReportData(**base)


def _rendered(ld) -> tuple[str, str]:
    return "\n".join(render_league_section(ld)), html_lib.unescape(_league_panel(ld))


def _order(text: str, *needles: str) -> list[int]:
    found = [text.find(n) for n in needles]
    assert all(i >= 0 for i in found), [n for n, i in zip(needles, found) if i < 0]
    return found


# -- hierarchy ---------------------------------------------------------------


def test_a_league_panel_puts_this_week_before_trades_before_waivers_before_context():
    ld = _league(
        waiver_targets=[WaiverTarget(
            player_id="w1", name="Wire Guy", position="RB", team="KC", trend_count=1, value=make_value(position="RB"),
            fills_need=True, need_rank=0, reason="fills a real need", priority_tier="Must Add", horizon="Season Starter",
        )],
        drop_candidates=[DropCandidate(entry=make_entry(name="Dead Weight"), priority="Strong Drop", reasons=["no value"])],
    )
    for text in _rendered(ld):
        got = _order(text, "Trade offers", "Waiver targets", "Consider dropping", "Roster context")
        assert got == sorted(got), text[:200]


def test_a_high_severity_alert_leads_the_panel_and_a_low_one_does_not():
    high = _league(time_sensitive=[TimeSensitiveNote("Ned Structalert", "out for the year", severity="high")])
    for text in _rendered(high):
        assert _order(text, "Time-sensitive", "Trade offers") == sorted(_order(text, "Time-sensitive", "Trade offers"))
    low = _league(time_sensitive=[TimeSensitiveNote("Ned Structalert", "questionable", severity="low")])
    for text in _rendered(low):
        a, b = _order(text, "Trade offers", "Time-sensitive")
        assert a < b


def test_the_roster_and_the_optimized_lineup_are_reference_material_not_a_headline():
    """Both live inside the collapsed context block, and the lineup — which
    neither renderer showed at all before — is there in both."""
    markdown, html = _rendered(_league())
    for text in (markdown, html):
        assert "Best starting lineup" in text
        assert "Quinn Structqb" in text  # the optimizer's QB assignment
        a, b = _order(text, "Roster context", "Best starting lineup")
        assert a < b, "the lineup must sit inside the collapsed context block"


def test_diagnostics_and_history_stay_collapsed_at_the_bottom():
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={},
        ff_status="absent", leagues=[], ledger_summary={"waiver": {"(open)": 2}},
    )
    markdown, html = render_weekly_report(report), _overview_panel(report)
    for text in (markdown, html):
        assert "Diagnostics and history" in text
        i = text.find("Diagnostics and history")
        assert "<details" in text[:i], "diagnostics must be inside a disclosure block"


# -- trade cards -------------------------------------------------------------


def _proposal(**kw) -> TradeProposal:
    base = dict(
        league_name="Struct League", currency="dynasty", target_username="RivalGuy", target_team_name="Rival Squad",
        give=[make_entry(player_id="wr2", name="Wes Structflex", position="WR")],
        receive=[make_entry(player_id="in1", name="Ivan Structin", position="RB")],
        my_value_total=5000, their_value_total=5200,
        rationale_for_me=["mine one", "mine two"], rationale_for_them=["theirs one"],
        caveats=["a caveat"], acceptance_reasons=["a factor"], acceptance_rating="Good", confidence="High",
        message="send this", trade_type="buy_low",
    )
    base.update(kw)
    return TradeProposal(**base)


def test_a_trade_card_leads_with_the_summary_and_hides_the_full_rationale():
    ld = _league(proposals=[_proposal()], trade_economics=[TradeEconomics("Favorable", "Improves Lineup", 2.4, False)])
    for text in _rendered(ld):
        got = _order(text, "Ivan Structin", "Acceptance: Good", "Full rationale", "mine one", "a caveat", "send this")
        assert got == sorted(got)
        # Everything still present, just later.
        for sentence in ("mine one", "mine two", "theirs one", "a factor", "a caveat", "send this"):
            assert sentence in text


def test_a_conflicted_card_shows_a_label_up_top_and_the_reasons_below():
    conflict = Conflict(kind=TRADE, key="0", subject="Wes Structflex",
                        reasons_for=["the case for"], reasons_against=["the case against"])
    ld = _league(proposals=[_proposal()], conflicts=[conflict])
    for text in _rendered(ld):
        i_label, i_details, i_reason = _order(text, CONFLICTED, "Full rationale", "the case against")
        assert i_label < i_details < i_reason
        assert "the case for" in text


def test_the_impact_list_is_capped_and_the_remainder_is_disclosed_not_dropped():
    lineup = optimize_lineup(make_roster(
        entries=[make_entry(player_id="q", name="Q", position="QB")], fmt=make_format(roster_positions=("QB", "BN")),
    ))
    impact = MoveImpact("Trade", _snapshot(lineup), _snapshot(lineup))
    impact.material_deltas = lambda: [f"delta {i}" for i in range(7)]  # type: ignore[method-assign]
    ld = _league(proposals=[_proposal()], trade_impacts=[impact])
    for text in _rendered(ld):
        for i in range(7):
            assert f"delta {i}" in text, i
        assert "3 further change(s)" in text
        assert _order(text, "delta 3", "Full rationale", "delta 4") == sorted(_order(text, "delta 3", "Full rationale", "delta 4"))


def test_one_scarcity_sentence_survives_per_trade_card():
    """The economics line already states it; the caveat that repeats it in
    another phrasing is suppressed rather than shown twice."""
    econ = TradeEconomics(
        "Favorable", "Costs Lineup", -3.0, False,
        scarcity_note="WR replacement market is Very Scarce — waivers won't repair this",
    )
    p = _proposal(caveats=["WR replacement market is Very Scarce: no waiver replacement for what you'd send",
                           "an unrelated caveat"])
    ld = _league(proposals=[p], trade_economics=[econ])
    for text in _rendered(ld):
        assert text.count("Very Scarce") == 1, text.count("Very Scarce")
        assert "an unrelated caveat" in text


# -- waiver rows -------------------------------------------------------------


def test_the_waiver_why_cell_is_short_and_the_rest_is_disclosed():
    target = WaiverTarget(
        player_id="w1", name="Wire Guy", position="RB", team="KC", trend_count=1, value=make_value(position="RB"),
        fills_need=True, need_rank=0, priority_tier="Must Add", horizon="Season Starter",
        reason="lead one; lead two; buried three; buried four",
        notes=["a role note", "Schedule: two home games"],
    )
    markdown, html = _rendered(_league(waiver_targets=[target]))
    # Markdown: the cell itself is one table row.
    row = next(line for line in markdown.splitlines() if "| Wire Guy |" in line)
    why = row.strip("|").split("|")[-1].strip()
    assert why == "lead one; lead two"
    assert "buried three" not in row and "a role note" not in row
    for text in (markdown, html):
        for sentence in ("buried three", "buried four", "a role note", "Schedule: two home games"):
            assert sentence in text, sentence
    # Both renderers keep the same columns, including Team.
    assert "| Priority | Player | Pos | Team | Drop | Horizon | FAAB | Why |" in markdown
    assert re.search(r"<th>Priority</th>\s*<th>Player</th>\s*<th>Pos</th>\s*<th>Team</th>", html)
    assert ">KC<" in html


# -- Best Moves --------------------------------------------------------------


def _report(actions) -> WeeklyReportData:
    return WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={},
        ff_status="absent", leagues=[], priority_actions=actions,
    )


def test_a_best_moves_row_orders_what_then_urgency_then_why_then_against_then_detail():
    action = PriorityAction(
        league_name="Struct League", kind="trade", headline="Send X for Y",
        detail="Struct League — good acceptance likelihood, sell high.",
        priority=PriorityKey("This Week", "Major", "Time-sensitive", "Neutral", "Mixed", "Moderate"),
        why_now=["the reason for"], against=["the reason against"],
    )
    report = _report([action])
    for text in (render_weekly_report(report), html_lib.unescape(_overview_panel(report))):
        got = _order(text, "Send X for Y", "This Week · Major · Time-sensitive", "the reason for",
                     "the reason against", "Good acceptance likelihood")
        assert got == sorted(got), text


def test_a_conflicted_best_move_is_labelled_not_led_with():
    action = PriorityAction(
        league_name="Struct League", kind="trade", headline="Send X for Y",
        detail=f"{CONFLICTED}: against — sells a starter. Struct League — good acceptance likelihood, sell high.",
        against=["sells a starter"],
    )
    report = _report([action])
    for text in (render_weekly_report(report), html_lib.unescape(_overview_panel(report))):
        i_head, i_flag = _order(text, "Send X for Y", "Conflicted")
        assert i_head < i_flag
        assert "sells a starter" in text  # the reason itself is not lost
    # The detail line no longer opens with the banner.
    md = render_weekly_report(report)
    detail_line = next(line for line in md.splitlines() if "Good acceptance likelihood" in line)
    assert CONFLICTED not in detail_line


# -- degraded signals --------------------------------------------------------


class _Signal:
    display_name = "KTC dynasty"
    label = "Stale"
    expected_absent = False
    cache_age = None
    coverage = None


class _Health:
    degraded = True
    signals = [_Signal()]
    notes: list[str] = []


def test_a_degraded_run_is_visible_at_the_top_of_both_outputs():
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={},
        ff_status="absent", leagues=[], health=_Health(), freshness_lines=["KTC dynasty · Stale"],
    )
    md, html = render_weekly_report(report), html_lib.unescape(render_dashboard_html(report))
    for text in (md, html):
        i_banner, i_section = _order(text, "Signal health: degraded", "Best moves right now")
        assert i_banner < i_section, "the degraded banner must precede the recommendations"


def test_no_banner_and_no_contradiction_when_the_run_is_clean():
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={},
        ff_status="absent", leagues=[],
    )
    for text in (render_weekly_report(report), html_lib.unescape(render_dashboard_html(report))):
        assert "Signal health: degraded" not in text
