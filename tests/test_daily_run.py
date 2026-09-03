"""The persistence gate in scripts/daily_run.py.

Three files are the tool's memory across runs: the decision snapshot (the
"since last run" baseline and the market-velocity history), the decision
ledger, and the watchlist. All three are written ONLY after a run that was
complete, because a half-synced morning that saved them would corrupt the
next run's reading of what changed — a league that failed to load would
have its players reported as having left my roster, and a watched player
would be marked gone because his league never arrived.

`daily_run` is a script with a `main()` and nothing extractable, and this
suite may not edit `scripts/`. So: import the module, monkeypatch the names
it imported into its own namespace, and run `main()` for real against a
fake report. `is_complete_run` is deliberately NOT patched — the gate's
actual decision is what is under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.daily_run as daily_run  # noqa: E402

from conftest import make_league_info  # noqa: E402
from sleeper_tool.decision_ledger import Ledger  # noqa: E402
from sleeper_tool.signal_health import FRESH, SignalHealth, SignalHealthReport  # noqa: E402
from sleeper_tool.watchlist import Watchlist  # noqa: E402


class _Sync:
    def __init__(self, ok: bool = True, name: str = "Test League"):
        self.ok = ok
        self.error = None if ok else "boom"
        self.league = make_league_info(name=name)


class _Storage:
    """The context manager and the one read main() makes of it."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_meta(self, key):
        return "5" if key == "current_week" else None


class _LD:
    def __init__(self, *, error=None, skipped=0, role_trends=None):
        self.league = make_league_info(league_id="L1")
        self.error = error
        self.drafted = error is None
        self.roster = _Roster(skipped)
        self.role_trends = role_trends or {}


class _Roster:
    def __init__(self, skipped_player_count: int):
        self.skipped_player_count = skipped_player_count
        self.entries = []


class _Report:
    def __init__(self, *, leagues=None, health=None):
        self.leagues = leagues if leagues is not None else [_LD()]
        self.health = health
        self.snapshot = {"schema": 2, "generated_at": "2026-09-02T12:00:00+00:00", "leagues": {}}
        self.ledger = Ledger()
        self.ledger_new = 0
        self.watchlist = Watchlist()
        self.watchlist_new = []


def _healthy() -> SignalHealthReport:
    return SignalHealthReport(signals=[SignalHealth(source="ktc_dynasty", family="ktc", label=FRESH)])


def _degraded() -> SignalHealthReport:
    """KTC served from cache after a failed re-fetch: the label is still
    Fresh, and only `fallback` says the price is yesterday's."""
    return SignalHealthReport(
        signals=[SignalHealth(source="ktc_dynasty", family="ktc", label=FRESH, fallback=True)]
    )


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """Runs `main()` with everything external stubbed, and returns which of
    the three persistence calls actually fired."""
    calls: dict[str, int] = {"snapshot": 0, "ledger": 0, "watchlist": 0}

    def run(report: _Report, *, sync_results: list[_Sync] | None = None, calibrate=None) -> dict[str, int]:
        monkeypatch.setattr(daily_run, "DATA_DIR", tmp_path)
        monkeypatch.setattr(daily_run, "SleeperClient", lambda *a, **k: object())
        monkeypatch.setattr(daily_run, "Storage", _Storage)
        monkeypatch.setattr(daily_run, "ValuationEngine", lambda **k: object())
        monkeypatch.setattr(daily_run, "sync_leagues", lambda *a, **k: sync_results or [_Sync()])
        monkeypatch.setattr(daily_run, "build_weekly_report_data", lambda *a, **k: report)
        monkeypatch.setattr(daily_run, "render_weekly_report", lambda r: "# report")
        monkeypatch.setattr(daily_run, "render_dashboard_html", lambda r: "<html></html>")
        monkeypatch.setattr(daily_run, "calibrate", calibrate or (lambda *a, **k: object()))
        monkeypatch.setattr(daily_run, "render_calibration_markdown", lambda c: "# calibration")
        monkeypatch.setattr(daily_run, "save_snapshot", lambda s: calls.__setitem__("snapshot", calls["snapshot"] + 1) or tmp_path / "s.json")
        monkeypatch.setattr(daily_run, "save_ledger", lambda l: calls.__setitem__("ledger", calls["ledger"] + 1) or tmp_path / "l.json")
        monkeypatch.setattr(daily_run, "save_watchlist", lambda w: calls.__setitem__("watchlist", calls["watchlist"] + 1) or tmp_path / "w.json")
        daily_run.main()
        return dict(calls)

    return run


def test_a_complete_run_saves_all_three_memories(gate, tmp_path):
    assert gate(_Report(health=_healthy())) == {"snapshot": 1, "ledger": 1, "watchlist": 1}
    # ... and it wrote both renderers' output, which is not gated at all.
    assert (tmp_path / "weekly_report.md").exists()
    assert (tmp_path / "dashboard.html").exists()


def test_a_sync_failure_saves_none_of_them(gate, tmp_path):
    report = _Report(health=_healthy())
    saved = gate(report, sync_results=[_Sync(ok=True), _Sync(ok=False, name="Broken League")])
    assert saved == {"snapshot": 0, "ledger": 0, "watchlist": 0}
    # The report itself is still written: a partial run is still worth reading.
    assert (tmp_path / "weekly_report.md").exists()


def test_a_league_that_errored_saves_none_of_them(gate):
    report = _Report(leagues=[_LD(), _LD(error="Could not find my roster")], health=_healthy())
    assert gate(report) == {"snapshot": 0, "ledger": 0, "watchlist": 0}


def test_a_roster_built_with_a_player_missing_from_the_cache_saves_none_of_them(gate):
    """A roster one player short would make the next delta report him as
    having joined my roster."""
    report = _Report(leagues=[_LD(skipped=1)], health=_healthy())
    assert gate(report) == {"snapshot": 0, "ledger": 0, "watchlist": 0}


def test_a_ranking_source_served_from_a_failed_refetch_saves_none_of_them(gate):
    """The subtle one: every league built fine and nothing errored, but KTC
    came from cache after a failed re-fetch. Its label is still Fresh — only
    `signal.fallback` says so — and saving that snapshot would make market
    velocity read a flat day as a real price move. `is_complete_run` reads
    `report.health` for exactly this."""
    assert gate(_Report(health=_degraded())) == {"snapshot": 0, "ledger": 0, "watchlist": 0}


def test_an_unavailable_ranking_family_also_stops_the_save(gate):
    unavailable = SignalHealthReport(
        signals=[SignalHealth(source="fantasypros_dynasty", family="fantasypros", label="Unavailable")]
    )
    assert gate(_Report(health=unavailable)) == {"snapshot": 0, "ledger": 0, "watchlist": 0}


def test_a_degraded_family_outside_the_ranking_sources_does_not_stop_the_save(gate):
    """Only the three ranking families gate persistence; a stale schedule or
    usage feed degrades the report without corrupting the baseline."""
    other = SignalHealthReport(
        signals=[
            SignalHealth(source="ktc_dynasty", family="ktc", label=FRESH),
            SignalHealth(source="nflverse_usage", family="nflverse_usage", label="Unavailable", fallback=True),
        ]
    )
    assert gate(_Report(health=other)) == {"snapshot": 1, "ledger": 1, "watchlist": 1}


def test_a_broken_calibration_report_never_stops_the_run(gate, tmp_path):
    """The calibration report is an engineering diagnostic; main() catches
    everything it raises. It runs after the three saves, so a failure there
    must not un-save them."""

    def boom(*a, **k):
        raise RuntimeError("calibration is broken")

    saved = gate(_Report(health=_healthy()), calibrate=boom)
    assert saved == {"snapshot": 1, "ledger": 1, "watchlist": 1}
    assert not (tmp_path / "calibration_report.md").exists()
