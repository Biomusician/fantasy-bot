"""Renderer parity: every human-facing sentence the decision layer writes
must reach BOTH renderers.

The architecture rule this guards (CLAUDE.md): annotations land on
`WaiverTarget.notes`, `TradeProposal.rationale_*`/`caveats` and
`LadderStep.source_note`, and "renderers join them, never compute them".
A sentence that only one renderer joins is a silent capability gap — the
dashboard reader never learns why the trade is risky, or the Markdown
reader never learns which position is scarce.

Method: build ONE rich `LeagueReportData` where every such string is a
unique sentinel token, render it through `render_league_section` (Markdown)
and `html_report._league_panel` (HTML, unescaped before comparison), and
require each sentinel in both. `KNOWN_ONE_SIDED` records the gaps that
exist today — each entry is a finding, not a fix.
"""
from __future__ import annotations

import html as html_lib
import re

import pytest

from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.buyer_board import BuyerBoard, BuyerFit
from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.faab_strategy import FaabAdvice, MUST_ADD
from sleeper_tool.html_report import _league_panel
from sleeper_tool.league_economy import LeagueEconomy, ManagerEconomy, POSITION_HEAVY
from sleeper_tool.lineup_leverage import BenchSurplus, LineupLeverage, StartSitDecision
from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.matchup_leverage import MatchupLeverage
from sleeper_tool.move_impact import MoveImpact, RosterSnapshot
from sleeper_tool.negotiation_ladder import LadderStep, NegotiationLadder
from sleeper_tool.opponent_blocker import DefensiveAdd
from sleeper_tool.pick_opportunity import PickAssessment, PickOpportunity, PositionUnit
from sleeper_tool.recommendation_conflicts import Conflict, TRADE, WAIVER
from sleeper_tool.replacement_value import (
    PlayerReplacementContext,
    PositionMarket,
    ReplacementMarket,
    SCARCE,
    VERY_SCARCE,
)
from sleeper_tool.report import render_league_section
from sleeper_tool.report_data import LeagueReportData
from sleeper_tool.schedule_window import ScheduleWindows
from sleeper_tool.stash_board import StashCandidate
from sleeper_tool.streamer_planner import ADD, StreamOption, StreamPlan, WeekLine
from sleeper_tool.trade_opportunity_cost import TradeEconomics
from sleeper_tool.trade_types import DropCandidate, TradeProposal
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget


ROSTER_POSITIONS = ("QB", "RB", "WR", "TE", "FLEX", "BN", "BN", "BN")


def _e(pid: str, name: str, position: str = "WR", **kw) -> object:
    return make_entry(player_id=pid, name=name, position=position, **kw)


def _lineup_result(roster):
    return optimize_lineup(roster)


def _snapshot(lineup) -> RosterSnapshot:
    return RosterSnapshot(
        lineup=lineup, weekly_points=100.0, depth_needs=["RB"], status="contender",
        strength_percentile=60.0, roster_value=10_000.0, avg_starter_age=26.0, displayed_status="contender",
    )


def build_rich_league() -> LeagueReportData:
    """One league carrying every annotation the decision layer can attach."""
    qb = _e("qb1", "Quinn Sentinelqb", "QB")
    rb = _e("rb1", "Rex Sentinelrb", "RB")
    wr = _e("wr1", "Wade Sentinelwr", "WR")
    te = _e("te1", "Tom Sentinelte", "TE")
    flex = _e("wr2", "Wes Sentinelflex", "WR")
    bench = _e("wr3", "Barry Sentinelbench", "WR", is_starter=False)
    clog = _e("rb2", "Carl Sentinelclog", "RB", is_starter=False)
    dropee = _e("te2", "Drew Sentineldrop", "TE", is_starter=False)
    roster = make_roster(
        roster_id=1, entries=[qb, rb, wr, te, flex, bench, clog, dropee],
        fmt=make_format(roster_positions=ROSTER_POSITIONS), wins=3, losses=1, points_for=420.5,
    )
    lineup = _lineup_result(roster)

    incoming = _e("in1", "Ivan Sentinelin", "RB")
    proposal = TradeProposal(
        league_name="Parity League", currency="dynasty", target_username="RivalGuy",
        target_team_name="Rival Squad", give=[flex], receive=[incoming],
        my_value_total=5000, their_value_total=5200,
        rationale_for_me=["SENTINEL-rationale-for-me"],
        rationale_for_them=["SENTINEL-rationale-for-them"],
        caveats=["SENTINEL-caveat"],
        acceptance_reasons=["SENTINEL-acceptance-reason"],
        acceptance_rating="Good", confidence="High",
        message="SENTINEL-trade-message",
        trade_type="buy_low",
    )
    economics = TradeEconomics(
        asset_economics="Favorable", roster_economics="Improves Lineup", weekly_delta=2.4,
        strategic_tradeoff=False, scarcity_note="SENTINEL-scarcity-note",
    )
    impact = MoveImpact(
        label="Trade", before=_snapshot(lineup), after=_snapshot(lineup),
        lineup_in=["Ivan Sentinelin"], lineup_out=["Wes Sentinelflex"],
        matchup_note="SENTINEL-matchup-note",
    )
    trade_conflict = Conflict(
        kind=TRADE, key="0", subject="Wes Sentinelflex",
        reasons_for=["SENTINEL-conflict-for"], reasons_against=["SENTINEL-conflict-against"],
    )
    pick = OwnedPick(season="2027", round=1, original_roster_id=1, tier="Mid", name="2027 Mid 1st", value=3000)
    ladder = NegotiationLadder(
        baseline_value=5000.0,
        opening=LadderStep(
            name="opening", players=[flex], picks=[], outgoing_value=4500.0, ratio=0.87,
            acceptance="Good", reasons=[], starters_given=["Wes Sentinelflex"],
            source_note="SENTINEL-ladder-source-note",
        ),
        fallback=LadderStep(
            name="fallback", players=[flex, bench], picks=[pick], outgoing_value=5200.0, ratio=1.0,
            acceptance="High", reasons=[],
        ),
        walk_away=None,
        opening_message="SENTINEL-ladder-opening-message",
    )

    faab = FaabAdvice(
        player_id="wa1", posture="Aggressive", suggested_pct=25, suggested_dollars=20, remaining=80,
        share_of_remaining_text="SENTINEL-faab-share", leverage_text="SENTINEL-faab-leverage",
        anchor_text="SENTINEL-faab-anchor", notes=["SENTINEL-faab-note"], name="Walt Sentinelwaiver",
        tier=MUST_ADD,
    )
    target = WaiverTarget(
        player_id="wa1", name="Walt Sentinelwaiver", position="RB", team="BUF", trend_count=900,
        value=make_value(position="RB"), fills_need=True, need_rank=0,
        reason="SENTINEL-waiver-reason", priority_tier=MUST_ADD, horizon="Season Starter",
        drop_candidate=dropee, suggested_faab_pct=25, notes=["SENTINEL-waiver-note"],
    )
    waiver_conflict = Conflict(
        kind=WAIVER, key="wa1", subject="Walt Sentinelwaiver",
        reasons_for=["SENTINEL-waiver-conflict-for"], reasons_against=["SENTINEL-waiver-conflict-against"],
    )
    waiver_impact = MoveImpact(
        label="Add", before=_snapshot(lineup), after=_snapshot(lineup),
        lineup_in=["Walt Sentinelwaiver"], lineup_out=[], matchup_note="SENTINEL-waiver-matchup-note",
    )

    market = ReplacementMarket(
        positions={
            "RB": PositionMarket(
                "RB", _e("fa1", "Fred Sentinelfa", "RB"), 8.0, _e("st1", "Stan Sentinelstarter", "RB"), 14.0,
                VERY_SCARCE, 0.55,
            ),
            "WR": PositionMarket(
                "WR", _e("fa2", "Frank Sentinelfa2", "WR"), 11.0, _e("st2", "Sam Sentinelstarter2", "WR"), 12.0,
                SCARCE, 0.3,
            ),
        },
        players={},
        understated=[PlayerReplacementContext(
            entry=rb, weekly_projection=15.0, projection_over_waiver=7.0,
            projection_over_starter_replacement=1.0, value_over_waiver=900.0, scarcity=VERY_SCARCE,
        )],
        overstated=[PlayerReplacementContext(
            entry=wr, weekly_projection=9.0, projection_over_waiver=-2.0,
            projection_over_starter_replacement=-3.0, value_over_waiver=-100.0, scarcity=SCARCE,
        )],
    )

    leverage = LineupLeverage(
        lineup=lineup,
        decisions=[StartSitDecision(
            slot="FLEX", starter=flex, starter_projection=100.0, alternative=bench,
            alternative_projection=98.0, label="Toss-Up", games_left=10,
            schedule_note="SENTINEL-schedule-note",
        )],
        bench_surplus=[BenchSurplus(
            entry=bench, projection=95.0, displaced_slot="WR", displaced_starter=wr,
            displaced_projection=100.0, ratio=0.95, value_percentile=72.0,
        )],
        weekly_starter_points=110.0, games_left=10,
    )

    economy = LeagueEconomy(
        total_completed_trades=6, limited_sample=False,
        managers={1: ManagerEconomy(
            roster_id=1, username="Me", team_name="My Team", completed_trades=3,
            net_future_picks=0, heavy_positions=["QB"], labels=[POSITION_HEAVY],
        )},
    )

    stream = StreamPlan(
        position="TE", weeks=[3, 4, 5], current=None,
        single=StreamOption(entry=_e("te9", "Ted Sentinelstream", "TE", is_starter=False), rostered=False,
                            weeks=[WeekLine(3, 9.5)], total=9.5),
        sequence=None, recommendation=ADD, note="SENTINEL-streamer-note",
    )

    return LeagueReportData(
        league=make_league_info(name="Parity League"),
        fmt_desc="Superflex, Full PPR",
        currency="dynasty",
        drafted=True,
        roster=roster,
        proposals=[proposal],
        trade_impacts=[impact],
        trade_economics=[economics],
        ladders={0: ladder},
        conflicts=[trade_conflict, waiver_conflict],
        waiver_targets=[target],
        waiver_impacts={"wa1": waiver_impact},
        faab={"wa1": faab},
        time_sensitive=[TimeSensitiveNote("Ned Sentinelalert", "SENTINEL-alert-note", severity="high")],
        drop_candidates=[DropCandidate(entry=dropee, priority="Strong Drop", reasons=["SENTINEL-drop-reason"])],
        roster_clogs=[__import__("sleeper_tool.roster_clog", fromlist=["RosterClog"]).RosterClog(
            entry=clog, reasons=["SENTINEL-clog-reason"], composite_rank=180.0,
        )],
        lineup=lineup,
        lineup_leverage=leverage,
        replacement=market,
        replacement_clauses={"wr3": "SENTINEL-replacement-clause"},
        stash=[StashCandidate(entry=_e("rk1", "Rook Sentinelstash", "WR", is_starter=False),
                              label="Priority Stash", percentile=55.0, reasons=["SENTINEL-stash-reason"])],
        buyer_boards=[BuyerBoard(
            candidate=wr,
            buyers=[BuyerFit(roster_id=2, username="RivalGuy", team_name="Rival Squad",
                             label="Strong fit", score=9, reasons=["SENTINEL-buyer-reason"])],
        )],
        windows=ScheduleWindows(current_week=3, next_weeks=[3, 4, 5], remaining_weeks=list(range(3, 18)),
                                playoff_weeks=[15, 16, 17], playoff_teams=6),
        pick_opportunity=PickOpportunity(
            units=[PositionUnit(position="RB", starters=2, avg_age=28.0, league_median_age=25.0,
                                strength=20.0, strength_rank=5, teams=6)],
            assessments=[PickAssessment(pick=pick, classification="Strategic", reason="SENTINEL-pick-reason")],
        ),
        league_economy=economy,
        streamers=[stream],
        defensive_add=DefensiveAdd(
            target=_e("df1", "Dan Sentinelblock", "WR", is_starter=False), opponent_name="Rival Squad",
            opponent_gain=6.5, hole="SENTINEL-blocker-hole", drop=clog, my_gain=0.5, week=3,
        ),
        matchup=MatchupLeverage(
            week=3, opponent_roster_id=2, opponent_name="Sentinelopponent Squad",
            my_points=118.0, opponent_points=110.0, gap=8.0, label="Modest edge",
            my_lineup=lineup, opponent_lineup=lineup,
        ),
    )


# Every sentence the decision layer produces, and where it currently lands.
# A sentinel listed here reached only ONE renderer when this test was
# written — each one is a reported finding, not something the test fixes.
KNOWN_ONE_SIDED: dict[str, str] = {
    # `_render_ladder` prints the step's acceptance/ratio and its notes for
    # every step; `_ladder_block` does too — but only the Markdown renderer
    # prints the walk-away explanation when walk_away is None. No sentinel
    # covers that (it is renderer-authored boilerplate, not a decision-layer
    # sentence), so nothing is listed for it.
}


SENTINELS = [
    "SENTINEL-rationale-for-me",
    "SENTINEL-rationale-for-them",
    "SENTINEL-caveat",
    "SENTINEL-acceptance-reason",
    "SENTINEL-trade-message",
    "SENTINEL-scarcity-note",
    "SENTINEL-matchup-note",
    "SENTINEL-conflict-for",
    "SENTINEL-conflict-against",
    "SENTINEL-ladder-source-note",
    "SENTINEL-ladder-opening-message",
    "SENTINEL-waiver-reason",
    "SENTINEL-waiver-note",
    "SENTINEL-waiver-conflict-against",
    "SENTINEL-waiver-matchup-note",
    "SENTINEL-faab-share",
    "SENTINEL-faab-leverage",
    "SENTINEL-faab-note",
    "SENTINEL-alert-note",
    "SENTINEL-drop-reason",
    "SENTINEL-clog-reason",
    "SENTINEL-stash-reason",
    "SENTINEL-buyer-reason",
    "SENTINEL-pick-reason",
    "SENTINEL-streamer-note",
    "SENTINEL-blocker-hole",
    "SENTINEL-schedule-note",
    "SENTINEL-replacement-clause",
    "SENTINEL-faab-anchor",
    # Names carried by decision-layer records rather than by a rendered sentence.
    "Sentinelopponent Squad",     # matchup opponent
    "Fred Sentinelfa",            # replacement market: best free agent
    "Stan Sentinelstarter",       # replacement market: worst current starter
    "Drew Sentineldrop",          # the waiver target's paired drop
    "Ned Sentinelalert",          # time-sensitive subject
    "Rook Sentinelstash",
    "Dan Sentinelblock",
    "Ted Sentinelstream",
]


@pytest.fixture(scope="module")
def rendered() -> tuple[str, str]:
    ld = build_rich_league()
    markdown = "\n".join(render_league_section(ld))
    # The HTML escapes on the way out; compare on the same footing.
    html = html_lib.unescape(_league_panel(ld)).replace("–", "-").replace("—", "-")
    return markdown, html


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_every_decision_layer_sentence_reaches_both_renderers(sentinel, rendered):
    markdown, html = rendered
    in_md, in_html = sentinel in markdown, sentinel in html
    assert in_md or in_html, f"{sentinel!r} reached NEITHER renderer — the fixture no longer wires it up"
    if sentinel in KNOWN_ONE_SIDED:
        pytest.xfail(KNOWN_ONE_SIDED[sentinel])
    assert in_md, f"{sentinel!r} is rendered only in HTML"
    assert in_html, f"{sentinel!r} is rendered only in Markdown"


def test_waiver_target_team_reaches_both_renderers(rendered):
    """The NFL team of a suggested add matters for bye-week and schedule
    reasoning; both tables carry a Team column (the HTML one was added in
    the 2026-09-03 UX pass after this test first pinned the gap)."""
    markdown, html = rendered
    assert "| BUF |" in markdown  # the waiver target's NFL team, unique in this fixture
    assert ">BUF<" in html


def test_faab_anchor_text_reaches_both_renderers(rendered):
    """`FaabAdvice.anchor_text` (what the league has actually paid for
    comparable adds) is part of `bid_detail`, which both renderers show."""
    markdown, html = rendered
    assert "SENTINEL-faab-anchor" in markdown
    assert "SENTINEL-faab-anchor" in html


def test_conflict_reasons_for_are_html_only_on_waivers(rendered):
    """Both renderers show a conflicted waiver's reasons AGAINST; neither
    shows its reasons FOR (the recommendation itself is the case for). On a
    TRADE both renderers show both sides. Pinned so the asymmetry stays
    deliberate."""
    markdown, html = rendered
    assert "SENTINEL-waiver-conflict-for" not in markdown
    assert "SENTINEL-waiver-conflict-for" not in html
    assert "SENTINEL-conflict-for" in markdown and "SENTINEL-conflict-for" in html


def test_the_sentinel_list_still_covers_the_fixture(rendered):
    """A guard on the guard: if someone adds a field to the fixture but not
    to SENTINELS, the parity check silently stops covering it."""
    markdown, html = rendered
    found = set(re.findall(r"SENTINEL-[a-z-]+[a-z]", markdown + html))
    missing = found - set(SENTINELS)
    assert not missing, f"sentinels present in output but not asserted on: {sorted(missing)}"
