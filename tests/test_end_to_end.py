"""The whole report path, end to end, on a synthetic league.

Every other test in this suite hands a hand-built dataclass to one module.
This one starts from raw Sleeper payloads in a `FakeStorage`, runs
`build_weekly_report_data` for real (trades, waivers, lineups, replacement
market, FAAB, the snapshot, the delta), and pushes the result through both
renderers — the only test that would catch a break in the wiring BETWEEN
those modules.

No network and no `data/` access: `with_nfl_schedule=False` covers the
schedule and usage fetches, and `isolate_report_data` covers the watchlist,
ledger, snapshot-history and ranking-cache reads.
"""
from __future__ import annotations

import re

import pytest

from fake_storage import (
    DEFAULT_ROSTER_POSITIONS,
    isolate_report_data,
    make_engine,
    make_storage,
    make_synthetic_league,
)

from sleeper_tool.decision_delta import SNAPSHOT_SCHEMA
from sleeper_tool.html_report import _slug, render_dashboard_html
from sleeper_tool.report import render_league_section, render_weekly_report
from sleeper_tool.report_data import build_weekly_report_data


# The three league shapes that take genuinely different paths through
# build_league_report_data: the ordinary in-season league, one Sleeper still
# reports as pre-draft, and one whose slot list the lineup optimizer refuses.
# The default slot list with one bench spot swapped for "OP" — a real
# Sleeper slot (offensive player) that lineup_optimizer deliberately refuses
# rather than guessing at. Everything else about the league is unchanged, so
# the lineup-free path can be compared against the lineup-having one.
ODD_SLOTS = list(DEFAULT_ROSTER_POSITIONS)
ODD_SLOTS[ODD_SLOTS.index("BN")] = "OP"


def _leagues():
    normal = make_synthetic_league()
    pre_draft = make_synthetic_league(
        name="Keeper Holdovers", league_id="9000000000000000002", kind="keeper", status="pre_draft", teams=4,
    )
    unknown_slot = make_synthetic_league(
        name="Odd Slots Dynasty", league_id="9000000000000000003", kind="dynasty", teams=5,
        roster_positions=ODD_SLOTS,
    )
    redraft = make_synthetic_league(
        name="Half PPR Redraft", league_id="9000000000000000004", kind="redraft", teams=4,
        scoring_settings={"rec": 0.5, "pass_td": 6.0, "bonus_rec_te": 0.0, "bonus_rush_yd_100": 2.0},
    )
    return normal, pre_draft, unknown_slot, redraft


@pytest.fixture(scope="module")
def report():
    """Module-scoped: the build is the expensive part and every test that
    reads it only reads it. `pytest.MonkeyPatch()` rather than the
    function-scoped `monkeypatch` fixture, which can't be used here.
    """
    patcher = pytest.MonkeyPatch()
    try:
        isolate_report_data(patcher)
        synthetic = _leagues()
        storage = make_storage(*synthetic)
        engine = make_engine(synthetic[0].players)
        yield build_weekly_report_data(
            storage, engine, [s.info for s in synthetic], with_nfl_schedule=False
        )
    finally:
        patcher.undo()


def _by_name(report, name):
    return next(ld for ld in report.leagues if ld.league.name == name)


# -- the whole path builds --------------------------------------------------


def test_every_league_builds_without_an_error(report):
    for ld in report.leagues:
        assert ld.error is None, f"{ld.league.name}: {ld.error}"
        assert ld.drafted, ld.league.name
        assert ld.roster is not None and ld.roster.entries


def test_the_ordinary_league_produces_real_recommendations(report):
    ld = _by_name(report, "Synthetic Dynasty")
    assert ld.proposals, "no trade proposal cleared the bar on a full synthetic league"
    assert ld.waiver_targets, "no waiver target from a 12-player trending list"
    assert ld.lineup is not None and ld.lineup.assignments
    assert ld.replacement is not None and ld.replacement.positions
    assert ld.team_status is not None and ld.team_status.status in ("contender", "middling", "rebuild")
    assert ld.playoff is not None  # four games played clears MIN_GAMES_FOR_LABEL
    assert ld.league_economy is not None and ld.league_economy.total_completed_trades == 1
    assert ld.faab, "a FAAB league with waiver targets should size the bids"


def test_both_renderers_run_and_name_every_league(report):
    markdown = render_weekly_report(report)
    html = render_dashboard_html(report)
    for ld in report.leagues:
        assert ld.league.name in markdown, ld.league.name
        assert ld.league.name in html, ld.league.name
        # The nav anchor and the panel id have to agree or the tab is dead.
        assert f'id="panel-{_slug(ld.league.name)}"' in html


def test_the_html_never_renders_a_literal_none_in_a_cell(report):
    """A `None` reaching a rendered cell means an f-string interpolated an
    unset optional instead of the renderer branching on it."""
    html = render_dashboard_html(report)
    leaked = [m.group(1) for m in re.finditer(r">([^<>]*\bNone\b[^<>]*)<", html)]
    assert not leaked, f"literal None in rendered text: {leaked[:5]}"


def test_the_markdown_never_renders_a_literal_none(report):
    markdown = render_weekly_report(report)
    leaked = [line for line in markdown.splitlines() if re.search(r"\bNone\b", line)]
    assert not leaked, f"literal None in the Markdown report: {leaked[:5]}"


# -- the branches ------------------------------------------------------------


def test_pre_draft_league_suppresses_waivers_insurance_stash_and_streamers(report):
    ld = _by_name(report, "Keeper Holdovers")
    assert ld.waiver_targets == []
    assert ld.insurance == []
    assert ld.stash == []
    assert ld.streamers == []
    assert ld.waivers_note and "pre-draft" in ld.waivers_note
    # Trades and the lineup still stand — the kept roster is real.
    assert ld.lineup is not None
    assert ld.waivers_note in "\n".join(render_league_section(ld))
    assert ld.waivers_note in render_dashboard_html(report)


def test_an_unknown_slot_type_keeps_trades_and_waivers_and_drops_lineup_features(report):
    ld = _by_name(report, "Odd Slots Dynasty")
    # "OP" is a real Sleeper slot the optimizer doesn't model.
    assert ld.lineup is None
    assert ld.lineup_leverage is None
    assert ld.replacement is None
    assert ld.bye_collision is None
    assert ld.matchup is None
    assert ld.roster_clogs == []
    assert ld.pick_opportunity is None
    # ... but the features that never needed a lineup survive.
    assert ld.proposals, "trades must survive an unmodellable slot list"
    assert ld.waiver_targets, "waivers must survive an unmodellable slot list"
    assert ld.team_status is not None


def test_an_unmodellable_slot_costs_only_the_lineup_features(monkeypatch):
    """The same league twice — once with a slot list the optimizer can
    solve, once with "OP" in place of a bench spot. Trades and waivers must
    come out identical, because neither of them needs a lineup."""
    def build(slots, league_id):
        isolate_report_data(monkeypatch)
        synth = make_synthetic_league(league_id=league_id, roster_positions=slots)
        storage = make_storage(synth)
        report = build_weekly_report_data(
            storage, make_engine(synth.players), [synth.info], with_nfl_schedule=False
        )
        return report.leagues[0]

    solvable = build(list(DEFAULT_ROSTER_POSITIONS), "9000000000000000010")
    unmodellable = build(ODD_SLOTS, "9000000000000000011")

    assert solvable.lineup is not None and unmodellable.lineup is None
    assert [p.summary_line() for p in solvable.proposals] == [p.summary_line() for p in unmodellable.proposals]
    assert [t.player_id for t in solvable.waiver_targets] == [t.player_id for t in unmodellable.waiver_targets]
    assert solvable.team_status.status == unmodellable.team_status.status


def test_an_empty_roster_positions_payload_degrades_the_same_way(monkeypatch):
    isolate_report_data(monkeypatch)
    synth = make_synthetic_league(roster_positions=[])
    storage = make_storage(synth)
    report = build_weekly_report_data(storage, make_engine(synth.players), [synth.info], with_nfl_schedule=False)
    ld = report.leagues[0]
    assert ld.error is None and ld.drafted
    assert ld.lineup is None and ld.proposals is not None


# -- the snapshot and determinism -------------------------------------------


def test_the_snapshot_builds_for_every_drafted_league(report):
    snapshot = report.snapshot
    assert snapshot is not None
    assert snapshot["schema"] == SNAPSHOT_SCHEMA
    assert set(snapshot["leagues"]) == {ld.league.league_id for ld in report.leagues}
    for league_id, entry in snapshot["leagues"].items():
        assert entry["roster"], league_id
        assert "tracked" in entry  # schema 2's additive bucket
    # A first run has no prior snapshot to diff against.
    assert report.delta is None


def _generated_line_stripped(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if not line.startswith("_Generated ")]


def test_a_second_run_is_deterministic(monkeypatch):
    """Two runs over the same inputs must differ only in the timestamp —
    the project's idempotency rule, checked on the rendered artifact rather
    than on any one module."""
    isolate_report_data(monkeypatch)

    def once():
        synthetic = _leagues()
        storage = make_storage(*synthetic)
        engine = make_engine(synthetic[0].players)
        return render_weekly_report(build_weekly_report_data(
            storage, engine, [s.info for s in synthetic], with_nfl_schedule=False
        ))

    assert _generated_line_stripped(once()) == _generated_line_stripped(once())


def test_nothing_in_the_run_touched_the_real_data_directory(report):
    """The isolation patches are what keep the suite off `data/`. Checked on
    the names `report_data` actually calls (it imports them into its own
    namespace), plus the consequence: the ledger contains only what THIS run
    recorded, so nothing was read off disk."""
    import sleeper_tool.report_data as rd

    assert rd.load_watchlist().items == {}
    assert rd.load_ledger().entries == {}
    assert rd.load_snapshots() == []
    assert rd.load_latest_snapshot() is None
    assert rd.load_snapshot("ktc_dynasty") is None

    assert report.ledger is not None
    assert report.ledger_new >= 1  # this run's own recommendations, in memory
    assert len(report.ledger.entries) == report.ledger_new  # and nothing else
    # The watchlist this run produced holds only items first seen today.
    today = report.generated_at.date().isoformat()
    assert all(item.last_run_on == today for item in report.watchlist.items.values())


# -- the fixture itself ------------------------------------------------------


def test_the_fixture_puts_my_user_id_on_exactly_one_roster():
    from sleeper_tool.config import MY_USER_ID

    synth = make_synthetic_league()
    mine = [r for r in synth.rosters if r["owner_id"] == MY_USER_ID]
    assert len(mine) == 1 and mine[0]["roster_id"] == 1
    assert synth.my_roster is mine[0]


def test_the_fixture_models_a_complete_waiver_a_failed_one_and_a_trade():
    synth = make_synthetic_league()
    by_kind = {(t["type"], t["status"]) for t in synth.transactions}
    assert ("waiver", "complete") in by_kind
    assert ("waiver", "failed") in by_kind
    assert ("trade", "complete") in by_kind
    complete = next(t for t in synth.transactions if t["type"] == "waiver" and t["status"] == "complete")
    assert complete["settings"]["waiver_bid"] > 0
    trade = next(t for t in synth.transactions if t["type"] == "trade")
    assert trade["adds"] and trade["drops"] and trade["draft_picks"]
    assert synth.traded_picks and {"season", "round", "roster_id", "owner_id"} <= set(synth.traded_picks[0])


def test_the_fixture_fills_taxi_and_reserve_slots():
    synth = make_synthetic_league()
    assert synth.my_roster["taxi"] and synth.my_roster["reserve"]
    # Nobody is both stashed and in the starting lineup.
    starters = set(synth.my_roster["starters"])
    assert not starters & set(synth.my_roster["taxi"])
    assert not starters & set(synth.my_roster["reserve"])


def test_every_synthetic_player_has_a_unique_normalized_name_and_a_value():
    """The ranking indexes are keyed by normalized name and last-write-wins,
    so two fixture players who normalize to the same key would silently
    share one KTC/FP/RotoBaller row — a fixture bug that would look like a
    valuation bug. (name_matching strips Jr./II/III, which is how the first
    version of this pool collided.)"""
    from sleeper_tool.name_matching import normalize_name

    synth = make_synthetic_league()
    keys = [normalize_name(p["full_name"]) for p in synth.players.values()]
    assert len(set(keys)) == len(keys)

    engine = make_engine(synth.players)
    fmt = __import__("sleeper_tool.valuation", fromlist=["derive_league_format"]).derive_league_format(synth.league)
    for pid in synth.my_roster["players"]:
        p = synth.players[pid]
        value = engine.value_player(p["full_name"], fmt, p["position"])
        assert value.sources_used, f"{p['full_name']} ({p['position']}) got no value from any source"
        if p["position"] in ("QB", "RB", "WR", "TE"):
            assert value.dynasty_value is not None and value.proj_points is not None


def test_the_default_slot_list_is_one_the_optimizer_can_model():
    from sleeper_tool.lineup_optimizer import slot_eligibility, NON_STARTER_SLOTS

    for slot in DEFAULT_ROSTER_POSITIONS:
        if slot not in NON_STARTER_SLOTS:
            slot_eligibility(slot)  # raises UnsupportedSlotError if not
