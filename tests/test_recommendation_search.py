"""recommendation_search: every hit is something the report already says."""
import datetime as dt

from conftest import make_entry, make_league_info, make_roster, make_value

from sleeper_tool.action_priority import DURABLE, HIGH_IRREVERSIBLE, IMMEDIATE, MARGINAL, MONITOR, NEUTRAL, PriorityKey, SINGLE
from sleeper_tool.recommendation_conflicts import TRADE, WAIVER, Conflict
from sleeper_tool.recommendation_search import (
    conflicted_moves,
    role_ahead_of_market,
    search_player,
    urgent_actions,
    very_scarce_markets,
    watchlist_hits,
)
from sleeper_tool.replacement_value import ABUNDANT, VERY_SCARCE, PositionMarket, ReplacementMarket
from sleeper_tool.report_data import LeagueReportData, PriorityAction, WeeklyReportData
from sleeper_tool.role_trends import MARKET_AHEAD, RISING, ROLE_AHEAD, RoleTrend
from sleeper_tool.trade_types import DropCandidate, TradeProposal
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget


def _report(*leagues, actions=()):
    return WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1, source_freshness={}, ff_status="absent",
        leagues=list(leagues), priority_actions=list(actions),
    )


def _league():
    jr = make_entry(player_id="a", name="Marvin Harrison Jr.", position="WR")
    rb = make_entry(player_id="b", name="Bijan Robinson", position="RB")
    roster = make_roster(entries=[jr, rb])
    proposal = TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="Rival", give=[jr], receive=[rb],
        my_value_total=100, their_value_total=100, rationale_for_me=[], rationale_for_them=[], caveats=[],
    )
    target = WaiverTarget(player_id="c", name="Jaylen Wright", position="RB", team="MIA", trend_count=3, value=make_value(),
                          fills_need=True, need_rank=0, reason="fills a need", drop_candidate=jr)
    return LeagueReportData(
        league=make_league_info(name="L"), drafted=True, roster=roster, currency="dynasty",
        proposals=[proposal], waiver_targets=[target],
        drop_candidates=[DropCandidate(entry=jr, priority="Consider Dropping", reasons=["buried"])],
        time_sensitive=[TimeSensitiveNote("Bijan Robinson", "questionable", severity="medium")],
        conflicts=[Conflict(kind=TRADE, key="0", subject="Send Marvin Harrison Jr. to Rival for Bijan Robinson", reasons_for=["value"], reasons_against=["sells a starter"])],
        replacement=ReplacementMarket(positions={
            "QB": PositionMarket("QB", None, None, None, None, VERY_SCARCE, None),
            "WR": PositionMarket("WR", None, 8.0, None, 9.0, ABUNDANT, 0.05),
        }, players={}),
        role_trends={"b": RoleTrend("g", RISING, [], games=3), "a": RoleTrend("g2", RISING, [], games=3)},
        role_market={"b": ROLE_AHEAD, "a": MARKET_AHEAD},
        replacement_clauses={"b": "+4.0/wk over the best free-agent RB (Normal market)"},
    )


def test_player_search_is_suffix_and_case_insensitive_and_covers_every_section():
    ld = _league()
    hits = search_player(_report(ld), "marvin harrison")
    assert {h.section for h in hits} == {"trade", "waiver", "drop", "roster"}
    trade = next(h for h in hits if h.section == "trade")
    assert "Conflicted: sells a starter" in trade.text
    waiver = next(h for h in hits if h.section == "waiver")
    assert "drop Marvin Harrison Jr." in waiver.text  # found as the paired drop, not only as the add
    roster = next(h for h in search_player(_report(ld), "BIJAN") if h.section == "roster")
    assert "+4.0/wk over the best free-agent RB" in roster.text and ROLE_AHEAD in roster.text
    assert any(h.section == "alert" for h in search_player(_report(ld), "bijan"))


def test_label_queries_return_only_the_matching_labels():
    ld = _league()
    report = _report(ld)
    assert [h.text for h in very_scarce_markets(report)] == ["QB: Very Scarce — no startable free agent at all"]
    assert [h.text for h in role_ahead_of_market(report)] == [f"Bijan Robinson: Role Rising (3 games) — {ROLE_AHEAD}"]
    assert [h.text for h in conflicted_moves(report)] == ["Send Marvin Harrison Jr. to Rival for Bijan Robinson — against: sells a starter"]


def test_urgent_actions_and_errored_leagues():
    ld = _league()
    key_now = PriorityKey(IMMEDIATE, MARGINAL, DURABLE, NEUTRAL, SINGLE, HIGH_IRREVERSIBLE)
    key_later = PriorityKey(MONITOR, MARGINAL, DURABLE, NEUTRAL, SINGLE, HIGH_IRREVERSIBLE)
    actions = [PriorityAction("L", "waiver", "Add X", "", priority=key_now), PriorityAction("L", "trade", "Send Y", "", priority=key_later)]
    report = _report(ld, LeagueReportData(league=make_league_info(name="Broken"), error="boom"), actions=actions)
    assert [h.text for h in urgent_actions(report)] == ["Add X [Immediate · Marginal]"]
    assert all(h.league == "L" for h in search_player(report, "bijan"))
    assert watchlist_hits(report) == []
