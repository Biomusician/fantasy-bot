"""The Waiver Command Center through the whole report path, on the three
days that change the page: Tuesday (claims pending), Wednesday (final
review) and an ordinary Thursday.

The command center is built for redraft and keeper leagues only, and the
claim plan's claims ARE that league's waiver targets, so these tests also
guard the two invariants a renderer can silently break: a dynasty league
must keep the old waiver table, and the two renderers must say the same
things.
"""
from __future__ import annotations

import datetime as dt

import pytest

from fake_storage import isolate_report_data, make_engine, make_storage, make_synthetic_league

from sleeper_tool.html_report import render_dashboard_html
from sleeper_tool.report import render_weekly_report
from sleeper_tool.report_data import build_weekly_report_data
from sleeper_tool.waiver_mode import WaiverMode

TUESDAY = dt.date(2026, 9, 15)
WEDNESDAY = dt.date(2026, 9, 16)
THURSDAY = dt.date(2026, 9, 17)


def _mode(day: dt.date, *, claim_week: int = 2) -> WaiverMode:
    return WaiverMode(active=day.weekday() in (1, 2), local_date=day, weekday=day.weekday(), claim_week=claim_week)


def _leagues():
    dynasty = make_synthetic_league()
    redraft = make_synthetic_league(
        name="Half PPR Redraft", league_id="9000000000000000004", kind="redraft", teams=4,
        scoring_settings={"rec": 0.5, "pass_td": 6.0, "bonus_rec_te": 0.0, "bonus_rush_yd_100": 0.0},
    )
    keeper_pre_draft = make_synthetic_league(
        name="Keeper Holdovers", league_id="9000000000000000002", kind="keeper", status="pre_draft", teams=4,
    )
    return dynasty, redraft, keeper_pre_draft


def _report(patcher, day: dt.date):
    isolate_report_data(patcher)
    synthetic = _leagues()
    storage = make_storage(*synthetic)
    engine = make_engine(synthetic[0].players)
    return build_weekly_report_data(
        storage, engine, [s.info for s in synthetic], with_nfl_schedule=False, waiver_mode=_mode(day),
    )


@pytest.fixture(scope="module")
def tuesday():
    patcher = pytest.MonkeyPatch()
    try:
        yield _report(patcher, TUESDAY)
    finally:
        patcher.undo()


@pytest.fixture(scope="module")
def thursday():
    patcher = pytest.MonkeyPatch()
    try:
        yield _report(patcher, THURSDAY)
    finally:
        patcher.undo()


def _league(report, name):
    return next(ld for ld in report.leagues if ld.league.name == name)


# -- which leagues get one ----------------------------------------------------


def test_redraft_league_gets_a_command_center(tuesday):
    ld = _league(tuesday, "Half PPR Redraft")
    assert ld.waiver_center is not None
    assert ld.waiver_center.claim_week == 2
    assert ld.waiver_center.needs.groups, "every startable group should be assessed"


def test_dynasty_league_keeps_the_waiver_table(tuesday):
    ld = _league(tuesday, "Synthetic Dynasty")
    assert ld.waiver_center is None
    markdown = render_weekly_report(tuesday)
    dynasty_section = markdown.split("## Synthetic Dynasty", 1)[1].split("\n---", 1)[0]
    assert "Waiver targets" in dynasty_section
    assert "Waiver Command Center" not in dynasty_section


def test_pre_draft_league_is_suppressed(tuesday):
    ld = _league(tuesday, "Keeper Holdovers")
    assert ld.waiver_center is None
    assert ld.waivers_note and "pre-draft" in ld.waivers_note


# -- the claims are the targets ------------------------------------------------


def test_plan_claims_are_the_waiver_targets(tuesday):
    ld = _league(tuesday, "Half PPR Redraft")
    center = ld.waiver_center
    planned = [c.call.entry.name for c in center.plan.claims()]
    targets = [t.name for t in ld.waiver_targets]
    assert targets[: len(planned)] == planned, "the plan's order is the waiver list's order"
    for claim in center.plan.claims():
        target = next(t for t in ld.waiver_targets if t.player_id == claim.call.player_id)
        assert (target.drop_candidate.player_id if target.drop_candidate else None) == (
            claim.drop.player_id if claim.drop else None
        ), f"{target.name}: the table's drop must be the plan's drop"


def test_a_pass_never_becomes_a_waiver_target(tuesday):
    ld = _league(tuesday, "Half PPR Redraft")
    passes = {c.player_id for c in ld.waiver_center.calls if not c.is_claim}
    assert not passes & {t.player_id for t in ld.waiver_targets}


def test_every_claim_has_a_drop_or_an_open_spot(tuesday):
    for claim in _league(tuesday, "Half PPR Redraft").waiver_center.plan.claims():
        assert claim.drop is not None or any("open roster spot" in n for n in claim.notes)


# -- the day changes the page --------------------------------------------------


def test_tuesday_leads_with_the_command_center(tuesday):
    markdown = render_weekly_report(tuesday)
    assert "## Waiver Command Center — Tuesday · claims pending" in markdown
    section = markdown.split("## Half PPR Redraft", 1)[1].split("\n---", 1)[0]
    headings = [line for line in section.splitlines() if line.startswith("### ")]
    assert headings, "the redraft league should have sections"
    assert headings[0].startswith("### Waiver Command Center"), headings[:3]


def test_thursday_returns_to_the_standard_hierarchy(thursday):
    markdown = render_weekly_report(thursday)
    assert "## Waiver Command Center" not in markdown.split("## Synthetic Dynasty", 1)[0], "no cross-league block off a waiver day"
    section = markdown.split("## Half PPR Redraft", 1)[1].split("\n---", 1)[0]
    headings = [line for line in section.splitlines() if line.startswith("### ")]
    assert not headings[0].startswith("### Waiver Command Center"), headings[:3]
    # ... but the block itself is still there, in its usual place.
    assert any(h.startswith("### Waiver Command Center") for h in headings)


def test_wednesday_says_final_review():
    patcher = pytest.MonkeyPatch()
    try:
        report = _report(patcher, WEDNESDAY)
        markdown = render_weekly_report(report)
        assert "Wednesday · final waiver review" in markdown
    finally:
        patcher.undo()


# -- the two renderers agree ----------------------------------------------------


def test_both_renderers_name_every_claim_and_its_drop(tuesday):
    markdown = render_weekly_report(tuesday)
    html = render_dashboard_html(tuesday)
    center = _league(tuesday, "Half PPR Redraft").waiver_center
    for claim in center.plan.claims():
        assert claim.call.entry.name in markdown
        assert claim.call.entry.name in html
        if claim.drop is not None:
            assert claim.drop.name in markdown
            assert claim.drop.name in html


def test_both_renderers_show_the_same_bid_window(tuesday):
    markdown = render_weekly_report(tuesday)
    html = render_dashboard_html(tuesday)
    for claim in _league(tuesday, "Half PPR Redraft").waiver_center.plan.claims():
        if claim.window is None:
            continue
        text = claim.window.text().replace("FAAB ", "")
        money = text.split(" · ")[0]
        assert money in markdown, money
        assert money.replace("–", "&ndash;") in html or money in html, money


def test_both_renderers_carry_the_do_not_spend_list(tuesday):
    center = _league(tuesday, "Half PPR Redraft").waiver_center
    if not center.plan.do_not_spend:
        pytest.skip("no expert-backed pass on this synthetic wire")
    markdown = render_weekly_report(tuesday)
    html = render_dashboard_html(tuesday)
    assert "Do not spend" in markdown and "Do not spend" in html


def test_the_command_center_never_renders_a_literal_none(tuesday):
    markdown = render_weekly_report(tuesday)
    section = markdown.split("### Waiver Command Center", 1)[1].split("###", 1)[0]
    assert "None" not in section, section[:400]
