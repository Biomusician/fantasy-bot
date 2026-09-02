from conftest import make_entry, make_league_info

from sleeper_tool.html_report import _league_panel
from sleeper_tool.playoff_leverage import PlayoffLeverage
from sleeper_tool.report import render_league_section
from sleeper_tool.report_data import LeagueReportData, _safe_build_league_report_data, build_priority_actions
from sleeper_tool.trade_engine import DropCandidate, TradeProposal
from sleeper_tool.waiver_engine import TimeSensitiveNote, WaiverTarget


def test_safe_build_league_report_data_isolates_a_bad_league_instead_of_crashing():
    # Regression: build_all_valued_rosters raises a bare ValueError when a
    # league was never synced ("No cached league data ... run sync first").
    # Previously this propagated out of build_weekly_report_data uncaught,
    # taking down the ENTIRE daily cron run for all leagues, not just this
    # one -- _safe_build_league_report_data is the one seam that catches it.
    class _EmptyStorage:
        def get_league(self, league_id):
            return None

    league = make_league_info(name="Never Synced League")
    result = _safe_build_league_report_data(_EmptyStorage(), engine=None, league=league, current_week=1)
    assert result.error is not None
    assert result.league == league


def _league_data(**overrides):
    defaults = dict(league=make_league_info(name="Test League"), drafted=True, error=None)
    defaults.update(overrides)
    return LeagueReportData(**defaults)


def test_build_priority_actions_surfaces_high_severity_alerts():
    ld = _league_data(time_sensitive=[
        TimeSensitiveNote("Star Player", "Injury status: Out", severity="high"),
        TimeSensitiveNote("Bench Guy", "Roster status: PUP", severity="low"),
    ])
    actions = build_priority_actions([ld])
    assert len(actions) == 1
    assert actions[0].kind == "alert"
    assert "Star Player" in actions[0].headline


def test_build_priority_actions_surfaces_high_confidence_favorable_trades():
    good_trade = TradeProposal(
        league_name="Test League", currency="dynasty", target_username="rival", target_team_name="Rival",
        give=[], receive=[], my_value_total=100, their_value_total=100,
        rationale_for_me=[], rationale_for_them=[], caveats=[],
        acceptance_rating="High", confidence="High",
    )
    shaky_trade = TradeProposal(
        league_name="Test League", currency="dynasty", target_username="rival2", target_team_name="Rival2",
        give=[], receive=[], my_value_total=100, their_value_total=100,
        rationale_for_me=[], rationale_for_them=[], caveats=[],
        acceptance_rating="High", confidence="Low",  # high acceptance but shaky valuation -- should NOT surface
    )
    ld = _league_data(proposals=[good_trade, shaky_trade])
    actions = build_priority_actions([ld])
    assert len(actions) == 1
    assert actions[0].kind == "trade"


def test_build_priority_actions_surfaces_must_add_waivers():
    target = WaiverTarget(
        player_id="p1", name="Hot Add", position="RB", team="KC", trend_count=50,
        value=None, fills_need=True, need_rank=0, reason="fills a real need",
        priority_tier="Must Add",
    )
    ld = _league_data(waiver_targets=[target])
    actions = build_priority_actions([ld])
    assert len(actions) == 1
    assert actions[0].kind == "waiver"
    assert "Hot Add" in actions[0].headline


def test_build_priority_actions_is_empty_when_nothing_urgent():
    ld = _league_data(time_sensitive=[TimeSensitiveNote("X", "Roster status: PUP", severity="low")])
    assert build_priority_actions([ld]) == []


def test_build_priority_actions_skips_leagues_with_errors_or_not_drafted():
    errored = _league_data(error="boom", time_sensitive=[TimeSensitiveNote("X", "Injury status: Out", severity="high")])
    undrafted = LeagueReportData(league=make_league_info(name="Undrafted League"), drafted=False)
    actions = build_priority_actions([errored, undrafted])
    assert actions == []


def test_build_priority_actions_respects_max_actions_cap():
    notes = [TimeSensitiveNote(f"P{i}", "Injury status: Out", severity="high") for i in range(20)]
    ld = _league_data(time_sensitive=notes)
    actions = build_priority_actions([ld], max_actions=3)
    assert len(actions) == 3


def _trade(username, rating, confidence):
    return TradeProposal(
        league_name="X", currency="dynasty", target_username=username, target_team_name=username,
        give=[], receive=[], my_value_total=100, their_value_total=100,
        rationale_for_me=[], rationale_for_them=[], caveats=[],
        acceptance_rating=rating, confidence=confidence,
    )


def test_build_priority_actions_ranks_by_quality_not_league_order():
    # Regression: a later-processed league's objectively better trade
    # (High/High) was previously truncated in favor of an earlier
    # league's weaker-but-still-qualifying trades (Good/Medium), purely
    # because the sort only bucketed by kind, not by quality within it.
    weak_league = _league_data(
        league=make_league_info(name="League A"),
        proposals=[_trade(f"rival{i}", "Good", "Medium") for i in range(8)],
    )
    strong_league = _league_data(
        league=make_league_info(name="League B"),
        proposals=[_trade("BestRival", "High", "High")],
    )
    actions = build_priority_actions([weak_league, strong_league], max_actions=8)
    headlines = [a.headline for a in actions]
    assert any("BestRival" in h for h in headlines), "the objectively best trade must survive the cap regardless of league order"
    assert actions[0].headline == next(h for h in headlines if "BestRival" in h)  # and it should rank first


def _playoff(label, *, deadline_window):
    return PlayoffLeverage(
        label=label, wins=4, losses=4, ties=0, games_remaining=6, seed=5, playoff_teams=4, cut_wins=4,
        deadline_window=deadline_window, trade_deadline_week=11, reason="4-4, seed 5 of 8",
    )


def test_deadline_window_bubble_team_trades_lead_the_trade_list_and_say_why():
    calm = _league_data(league=make_league_info(name="Calm League"), proposals=[_trade("rivalA", "High", "High")])
    urgent = _league_data(
        league=make_league_info(name="Urgent League"),
        proposals=[_trade("rivalB", "Good", "Medium")],  # objectively weaker than Calm's High/High
        playoff=_playoff("Bubble", deadline_window=True),
    )
    actions = build_priority_actions([calm, urgent])
    assert actions[0].league_name == "Urgent League"
    assert actions[0].detail.startswith("Deadline Window (Bubble, deadline week 11)")
    # Comfortable teams get no boost even inside the window.
    comfortable = _league_data(
        league=make_league_info(name="Comfy League"), proposals=[_trade("rivalC", "Good", "Medium")],
        playoff=_playoff("Comfortable", deadline_window=True),
    )
    actions = build_priority_actions([calm, comfortable])
    assert actions[0].league_name == "Calm League"


def test_playoff_picture_renders_in_both_outputs_only_when_present():
    # drafted=False: the status/playoff header lines render before the
    # roster sections, which is all this checks.
    ld = _league_data(playoff=_playoff("Long Shot", deadline_window=True), drafted=False)
    md = "\n".join(render_league_section(ld))
    assert "**Playoff picture: Long Shot** · **Deadline Window** — 4-4, seed 5 of 8" in md
    html = _league_panel(ld)
    assert "Playoffs: Long Shot" in html and "Deadline Window" in html
    assert "Playoff" not in "\n".join(render_league_section(_league_data(drafted=False)))


def test_build_priority_actions_reserves_slots_for_waivers_and_drops_even_when_trades_fill_the_cap():
    # Regression: live production data hit this exact case -- 8+ Good/High
    # trades filled every slot in the cross-league list, so a Must-Add
    # waiver and a Strong-Drop candidate (present in every league that
    # week) never appeared anywhere in "Best moves right now", even though
    # both are genuinely time-relevant.
    trades = [_trade(f"rival{i}", "High", "High") for i in range(10)]
    waiver = WaiverTarget(
        player_id="w1", name="Hot Add", position="RB", team="KC", trend_count=50,
        value=None, fills_need=True, need_rank=0, reason="fills a real need",
        priority_tier="Must Add",
    )
    drop = DropCandidate(entry=make_entry(name="Dead Weight"), priority="Strong Drop", reasons=["low value", "buried"])
    ld = _league_data(proposals=trades, waiver_targets=[waiver], drop_candidates=[drop])
    actions = build_priority_actions([ld], max_actions=8)
    kinds = [a.kind for a in actions]
    assert "waiver" in kinds, "a Must-Add waiver must not be crowded out entirely by an abundance of good trades"
    assert "roster" in kinds, "a Strong-Drop candidate must not be crowded out entirely by an abundance of good trades"
    assert len(actions) == 8
