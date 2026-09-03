"""Malformed, degenerate and boundary inputs, one case per test.

Everything here is a shape Sleeper (or a mid-season edge of the calendar)
can actually produce: a player listed on two rosters, more starters than
starting slots, a position nothing models, a week past the regular season,
a roster payload with no `settings` key at all. The bar for each is the
same as the production code's own: degrade visibly, never crash, and never
render a number that isn't real.
"""
from __future__ import annotations

import datetime as dt
import html as html_lib

import pytest

from conftest import make_entry, make_format, make_league_info, make_roster, make_value
from fake_storage import (
    build_rb_payload,
    isolate_report_data,
    make_engine,
    make_player_pool,
    make_storage,
    make_synthetic_league,
)

from sleeper_tool.decision_delta import build_snapshot, compute_delta, is_complete_run
from sleeper_tool.html_report import _league_panel, _overview_panel, _slug, render_dashboard_html
from sleeper_tool.report import render_league_section, render_weekly_report
from sleeper_tool.report_data import LeagueReportData, WeeklyReportData, build_weekly_report_data
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.valuation import games_remaining
from sleeper_tool.waiver_engine import MUST_ADD, WaiverTarget


def _build(monkeypatch, synth, *, current_week=3, engine=None, snapshots=None, latest=None):
    """One synthetic league through the real pipeline, off the network and
    off `data/`."""
    isolate_report_data(monkeypatch, snapshots=snapshots, latest=latest)
    storage = make_storage(synth, current_week=current_week)
    engine = engine if engine is not None else make_engine(synth.players, current_week=current_week)
    return build_weekly_report_data(storage, engine, [synth.info], with_nfl_schedule=False)


def _renders(report) -> tuple[str, str]:
    return render_weekly_report(report), render_dashboard_html(report)


# -- malformed rosters --------------------------------------------------------


def test_a_player_listed_on_two_rosters_still_builds(monkeypatch):
    """Sleeper has produced this mid-trade. Both rosters value him; the
    waiver engine must not then offer him as a free agent."""
    synth = make_synthetic_league()
    shared = synth.rosters[0]["players"][0]
    synth.rosters[1]["players"].append(shared)
    synth.rosters[1]["starters"][0] = shared  # and he's started on both
    report = _build(monkeypatch, synth)
    ld = report.leagues[0]
    assert ld.error is None
    # My side of it is intact: he appears once, and once only.
    assert sum(1 for e in ld.roster.entries if e.player_id == shared) == 1
    # He is rostered somewhere, so he must not also be offered on waivers.
    assert shared not in {t.player_id for t in ld.waiver_targets}
    # And he must not be double-counted as a trade target either.
    assert shared not in {e.player_id for p in ld.proposals for e in p.receive}
    _renders(report)


def test_more_starters_than_starting_slots(monkeypatch):
    """A `starters` array longer than roster_positions' starting slots —
    the optimizer builds its own lineup and must ignore the surplus rather
    than overfill."""
    synth = make_synthetic_league()
    mine = synth.my_roster
    mine["starters"] = list(mine["players"])  # every player flagged a starter
    startable_slots = [s for s in synth.league["roster_positions"] if s not in ("BN", "IR", "TAXI")]
    assert len(mine["starters"]) > len(startable_slots), "the fixture must actually overfill"
    report = _build(monkeypatch, synth)
    ld = report.leagues[0]
    assert ld.error is None and ld.lineup is not None
    # Sleeper's flags are input, not truth: the optimizer's own lineup is
    # still legal, and every assignment sits in a slot the league has.
    assert len(ld.lineup.assignments) <= len(startable_slots)
    assert sum(1 for e in ld.roster.entries if e.is_starter) > len(ld.lineup.assignments)
    for a in ld.lineup.assignments:
        assert a.slot in startable_slots
    assert len({a.player_id for a in ld.lineup.assignments}) == len(ld.lineup.assignments)
    _renders(report)


def test_an_unknown_position_on_my_roster(monkeypatch):
    """Sleeper carries punters and long snappers; nothing in the valuation
    or lineup layers models them. They must land on the bench, not crash."""
    pool = make_player_pool()
    pool["p999"] = {
        "player_id": "p999", "full_name": "Pete Punter", "first_name": "Pete", "last_name": "Punter",
        "position": "P", "team": "KC", "age": 30.0, "years_exp": 6,
        "injury_status": None, "status": "Active", "gsis_id": None,
    }
    synth = make_synthetic_league(players=pool)
    synth.my_roster["players"].append("p999")
    report = _build(monkeypatch, synth)
    ld = report.leagues[0]
    assert ld.error is None
    punter = next(e for e in ld.roster.entries if e.player_id == "p999")
    assert punter.position == "P"
    assert "p999" not in ld.lineup.starter_ids
    _renders(report)


def test_rosters_with_no_settings_key_at_all(monkeypatch):
    """`roster["settings"]` missing entirely: records, points-for and FAAB
    spend all default rather than raising."""
    synth = make_synthetic_league(include_roster_settings=False)
    assert "settings" not in synth.my_roster
    report = _build(monkeypatch, synth)
    ld = report.leagues[0]
    assert ld.error is None
    assert ld.roster.wins == 0 and ld.roster.points_for == 0.0 and ld.roster.waiver_budget_used == 0
    assert ld.playoff is None  # zero games played is below MIN_GAMES_FOR_LABEL
    _renders(report)


def test_taxi_and_reserve_players_are_flagged_and_never_started(monkeypatch):
    synth = make_synthetic_league()
    report = _build(monkeypatch, synth)
    ld = report.leagues[0]
    taxi = [e for e in ld.roster.entries if e.is_taxi]
    reserve = [e for e in ld.roster.entries if e.is_reserve]
    assert taxi and reserve
    for e in taxi + reserve:
        assert e.player_id not in ld.lineup.starter_ids
        assert e.player_id in ld.lineup.unavailable
    markdown = "\n".join(render_league_section(ld))
    assert "Taxi squad:" in markdown and "IR/Reserve:" in markdown


def test_a_rostered_player_missing_from_the_cache_marks_the_run_incomplete(monkeypatch):
    """`decision_delta.is_complete_run` exists precisely so a roster short
    one player never becomes the baseline — the next delta would report him
    as having 'joined your roster'."""
    synth = make_synthetic_league()
    isolate_report_data(monkeypatch)
    storage = make_storage(synth)
    engine = make_engine(synth.players)
    ghost = synth.my_roster["players"][0]
    storage.drop_player(ghost)
    report = build_weekly_report_data(storage, engine, [synth.info], with_nfl_schedule=False)
    ld = report.leagues[0]
    assert ld.error is None
    assert ld.roster.skipped_player_count == 1
    assert is_complete_run(report) is False
    # And the report says so out loud rather than presenting a short roster as whole.
    assert "may be incomplete" in "\n".join(render_league_section(ld))


# -- degenerate valuations ----------------------------------------------------


def test_none_and_negative_projections(monkeypatch):
    """RotoBaller is the only projection source. A null projection means
    'unprojected'; a negative one is nonsense the sources have emitted
    before (a DEF with a bad matchup model). Neither may crash a lineup."""
    pool = make_player_pool()
    rows = build_rb_payload(pool)
    for i, row in enumerate(rows):
        if i % 3 == 0:
            row["proj_points_ppr"] = None
            row["proj_points_standard"] = None
            row["proj_points_te_premium"] = None
        elif i % 3 == 1:
            row["proj_points_ppr"] = -12.5
            row["proj_points_standard"] = -12.5
            row["proj_points_te_premium"] = -12.5
    synth = make_synthetic_league(players=pool)
    engine = make_engine(pool, rb_rows=rows)
    report = _build(monkeypatch, synth, engine=engine)
    ld = report.leagues[0]
    assert ld.error is None and ld.lineup is not None
    projections = [e.value.proj_points for e in ld.roster.entries]
    assert any(p is None for p in projections) and any(p is not None and p < 0 for p in projections)
    _renders(report)


def test_k_and_def_with_zero_projection(monkeypatch):
    """A kicker projected at exactly 0 must still be startable (the slot
    has to be filled by someone) and must not divide by itself anywhere."""
    pool = make_player_pool()
    rows = build_rb_payload(pool)
    for row in rows:
        if row["position"] in ("K", "DEF"):
            row["proj_points_ppr"] = 0.0
            row["proj_points_standard"] = 0.0
            row["proj_points_te_premium"] = 0.0
    synth = make_synthetic_league(players=pool)
    report = _build(monkeypatch, synth, engine=make_engine(pool, rb_rows=rows))
    ld = report.leagues[0]
    assert ld.error is None and ld.lineup is not None
    slots = {a.slot for a in ld.lineup.assignments}
    assert {"K", "DEF"} <= slots, "a zero-projection K/DEF still has to fill its slot"
    _renders(report)


# -- calendar boundaries ------------------------------------------------------


def test_current_week_none(monkeypatch):
    """Preseason: Sleeper has no current week yet. Everything week-shaped
    (matchup, bye collision, streamers) goes quiet rather than guessing."""
    synth = make_synthetic_league(current_week=None)
    report = _build(monkeypatch, synth, current_week=None)
    ld = report.leagues[0]
    assert report.current_week is None
    assert ld.error is None and ld.lineup is not None
    assert ld.matchup is None  # `if lineup is not None and current_week:`
    _renders(report)


@pytest.mark.parametrize("week", [17, 18, 19])
def test_late_and_past_season_weeks_never_divide_by_zero(monkeypatch, week):
    """`games_remaining` is clamped to 1 exactly so a week-18+ report can't
    turn a per-week division into a ZeroDivisionError. Checked at the
    helper AND through the whole pipeline, since the clamp only helps if
    every consumer actually goes through it."""
    assert games_remaining(week) >= 1
    synth = make_synthetic_league(current_week=week)
    report = _build(monkeypatch, synth, current_week=week)
    ld = report.leagues[0]
    assert ld.error is None
    if ld.lineup_leverage is not None:
        for d in ld.lineup_leverage.close_calls:
            assert d.starter_weekly == d.starter_projection / d.games_left
    _renders(report)


# -- snapshot history ---------------------------------------------------------


def test_a_snapshot_naming_a_league_no_longer_configured(monkeypatch):
    """A league dropped from config.py leaves its rows in the snapshot
    history. The delta iterates the CURRENT run's leagues, so the stale one
    must simply not appear — not raise, and not be reported as 'left'."""
    synth = make_synthetic_league()
    stale = {
        "schema": 2,
        "generated_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat(),
        "current_week": 2,
        "leagues": {
            "0000000000000000000": {
                "name": "A League I Left", "team_status": "contender",
                "trade_targets": {}, "waiver_targets": {},
                "roster": {"9999": {"name": "Ghost Player", "value": 5000}}, "tracked": {},
            }
        },
    }
    report = _build(monkeypatch, synth, snapshots=[stale], latest=stale)
    assert report.delta is not None
    assert not any("A League I Left" in i.league_name for i in report.delta.items)
    assert not any("Ghost Player" in i.text for i in report.delta.items)
    _renders(report)


def test_a_player_renamed_between_snapshots_is_not_reported_as_joining_and_leaving(monkeypatch):
    """Sleeper corrects spellings mid-season. The snapshot is keyed by
    player_id, so a rename must produce no roster movement at all."""
    synth = make_synthetic_league()
    first = _build(monkeypatch, synth)
    baseline = first.snapshot
    baseline["generated_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()

    renamed_pool = {pid: dict(p) for pid, p in synth.players.items()}
    victim = synth.my_roster["players"][0]
    renamed_pool[victim]["full_name"] = "Renamed Player"
    renamed_pool[victim]["first_name"] = "Renamed"
    renamed_pool[victim]["last_name"] = "Player"
    second_synth = make_synthetic_league(players=renamed_pool)
    second = _build(monkeypatch, second_synth, snapshots=[baseline], latest=baseline)

    delta = compute_delta(baseline, second.snapshot)
    assert delta is not None
    movement = [i.text for i in delta.items if "your roster" in i.text]
    assert not movement, f"a rename produced roster movement: {movement}"


def test_the_snapshot_survives_a_league_with_no_valued_players(monkeypatch):
    """Every source absent: build_snapshot still has to produce a readable
    document rather than a half-written one."""
    synth = make_synthetic_league()
    from sleeper_tool.valuation import ValuationEngine

    engine = ValuationEngine(ktc_snapshot=None, fp_snapshots=None, rb_snapshots=None, ff_rows=[])
    report = _build(monkeypatch, synth, engine=engine)
    snapshot = build_snapshot(report)
    assert snapshot["schema"] == 2
    ld = report.leagues[0]
    assert ld.error is None
    _renders(report)


# -- bugs found while writing these tests (failing on purpose) ----------------


def test_a_traded_pick_from_a_roster_with_no_user_record(monkeypatch):
    synth = make_synthetic_league()
    # The fixture already trades roster 3's 2027 2nd to me; drop roster 3's
    # owner from the user list, exactly as an abandoned team looks.
    assert any(tp["roster_id"] == 3 and tp["owner_id"] == 1 for tp in synth.traded_picks)
    synth.users[:] = [u for u in synth.users if u["user_id"] != "owner3"]
    report = _build(monkeypatch, synth)
    assert report.leagues[0].error is None


def test_a_non_integer_trade_deadline_setting(monkeypatch):
    synth = make_synthetic_league(settings={"trade_deadline": "11"})
    report = _build(monkeypatch, synth)
    assert report.leagues[0].error is None


# -- renderer edges -----------------------------------------------------------


def test_a_trade_proposal_whose_give_and_receive_are_the_same_player():
    """Degenerate but constructible: value_ratio, balance_label and both
    renderers must all cope rather than dividing by a zero total."""
    piece = make_entry(player_id="x1", name="Xavier Same", position="WR")
    proposal = TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="Rival",
        give=[piece], receive=[piece], my_value_total=0.0, their_value_total=0.0,
        rationale_for_me=["no net change"], rationale_for_them=["no net change"], caveats=[],
    )
    assert proposal.value_ratio == float("inf")
    assert proposal.balance_label == "Overpay"
    roster = make_roster(entries=[piece], fmt=make_format(roster_positions=("WR", "BN")))
    ld = LeagueReportData(league=make_league_info(name="L"), drafted=True, roster=roster,
                          currency="dynasty", proposals=[proposal])
    markdown = "\n".join(render_league_section(ld))
    html = html_lib.unescape(_league_panel(ld))
    assert markdown.count("Xavier Same") >= 2  # named on both sides
    assert html.count("Xavier Same") >= 2


def test_an_empty_proposals_list_through_every_renderer():
    roster = make_roster(entries=[make_entry(player_id="q", name="Q", position="QB")],
                         fmt=make_format(roster_positions=("QB", "BN")))
    ld = LeagueReportData(league=make_league_info(name="Empty League"), drafted=True, roster=roster,
                          currency="dynasty", proposals=[], waiver_targets=[])
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1,
        source_freshness={}, ff_status="absent", leagues=[ld],
    )
    empty_note = "No trade offers cleared the value-match bar this week."
    markdown = render_weekly_report(report)
    html = render_dashboard_html(report)
    assert empty_note in markdown and empty_note in html
    assert "No standout waiver targets this week." in markdown
    assert "No standout waiver targets this week." in html
    assert empty_note in "\n".join(render_league_section(ld))
    assert empty_note in _league_panel(ld)
    assert _overview_panel(report)  # the overview must still render with nothing to show


def test_unicode_league_and_team_names_survive_both_renderers_and_the_slug():
    name = "Ünïcödé Léagüe \U0001F3F3️"
    ld = LeagueReportData(
        league=make_league_info(name=name),
        drafted=True, currency="dynasty",
        roster=make_roster(
            entries=[make_entry(player_id="q", name="Ámon-Rá St. Brøwn", position="WR", team="DET")],
            fmt=make_format(roster_positions=("WR", "BN")),
            team_name="Équipe Númérö Un",
        ),
    )
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=1,
        source_freshness={}, ff_status="absent", leagues=[ld],
    )
    markdown = render_weekly_report(report)
    html = render_dashboard_html(report)
    assert name in markdown and "Ámon-Rá St. Brøwn" in markdown
    assert name in html and "Ámon-Rá St. Brøwn" in html
    slug = _slug(name)
    assert slug and not slug.startswith("-") and not slug.endswith("-")
    # The nav link and the panel id must still agree once slugged, or the
    # tab for a non-ASCII league name opens nothing.
    assert f'id="panel-{slug}"' in html
    assert f'data-target="{slug}"' in html


def test_a_waiver_target_whose_player_id_is_a_def_team_code():
    """Sleeper keys team defenses by the team abbreviation ("LAC"), not a
    numeric id. Anything that treats a player_id as numeric — or that keys a
    dict on it alongside real players — has to cope."""
    target = WaiverTarget(
        player_id="LAC", name="Los Angeles Chargers", position="DEF", team="LAC", trend_count=400,
        value=make_value(name="Los Angeles Chargers", position="DEF", proj_points=110.0),
        fills_need=False, need_rank=None, reason="best available streaming defense",
        priority_tier=MUST_ADD, horizon="Streamer",
    )
    roster = make_roster(entries=[make_entry(player_id="q", name="Q", position="QB")],
                         fmt=make_format(roster_positions=("QB", "DEF", "BN")))
    ld = LeagueReportData(league=make_league_info(name="DEF League"), drafted=True, roster=roster,
                          currency="redraft", waiver_targets=[target])
    report = WeeklyReportData(
        generated_at=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), current_week=3,
        source_freshness={}, ff_status="absent", leagues=[ld],
    )
    markdown = render_weekly_report(report)
    html = render_dashboard_html(report)
    assert "Los Angeles Chargers" in markdown and "Los Angeles Chargers" in html
    # And the snapshot keys it without choking on the non-numeric id.
    snapshot = build_snapshot(report)
    assert snapshot["leagues"]["1"]["waiver_targets"]["LAC"] == "Los Angeles Chargers"


def test_a_def_on_a_real_roster_goes_through_the_whole_pipeline(monkeypatch):
    """The same non-numeric id, but from raw Sleeper payloads rather than a
    hand-built target — the path that actually runs weekly."""
    synth = make_synthetic_league()
    report = _build(monkeypatch, synth)
    ld = report.leagues[0]
    defs = [e for e in ld.roster.entries if e.position == "DEF"]
    assert defs and not defs[0].player_id.isdigit()
    assert defs[0].player_id in report.snapshot["leagues"][synth.info.league_id]["roster"]
    _renders(report)
