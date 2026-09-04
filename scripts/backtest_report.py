"""Writes the historical replay report — a developer diagnostic, never a
user report and never an accuracy claim.

    .venv/Scripts/python.exe scripts/backtest_report.py [output_path] [--season 2025]

Reads the on-disk caches only. There is no network call anywhere in this
path: the season usage comes from `historical_replay.load_cached_usage`,
which reads the cache file directly rather than through `get_or_fetch`,
so an aged cache is used as-is instead of triggering a refetch. An input
that is not on disk is named in the report and its mode is skipped.

The only file written is data/backtest_report.md.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.console import ensure_utf8_stdout
from sleeper_tool.decision_delta import load_snapshots
from sleeper_tool.decision_ledger import load_ledger, ledger_path
from sleeper_tool.historical_replay import build_result, load_cached_usage, render_backtest_markdown

ensure_utf8_stdout()

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "backtest_report.md"
# Newest first. The current season is normally absent (its files 404
# until the first game), so the previous one is the usual answer.
CANDIDATE_SEASONS = (dt.date.today().year, dt.date.today().year - 1, dt.date.today().year - 2)


def _pick_usage(season: int | None):
    seasons = (season,) if season else CANDIDATE_SEASONS
    tried: list[str] = []
    for candidate in seasons:
        usage = load_cached_usage(candidate)
        if usage is not None and usage.latest_week:
            return usage, tried
        tried.append(str(candidate))
    return None, tried


def main() -> None:
    argv = sys.argv[1:]
    season = None
    if "--season" in argv:
        index = argv.index("--season")
        season = int(argv[index + 1])
        argv = argv[:index] + argv[index + 2:]
    output_path = Path(argv[0]) if argv else DEFAULT_OUTPUT

    usage, tried = _pick_usage(season)
    unavailable: list[str] = []
    if usage is None and tried:
        unavailable.append(f"No cached nflverse usage for season(s) {', '.join(tried)} (not published, or never fetched).")

    snapshots = load_snapshots()
    ledger = load_ledger() if ledger_path().exists() else None

    result = build_result(usage=usage, snapshots=snapshots, ledger=ledger, unavailable=unavailable)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_backtest_markdown(result), encoding="utf-8")

    print(f"Wrote historical replay report to {output_path}")
    if result.role is not None:
        players = len({c.gsis_id for c in result.role.cases})
        print(f"Mode 1: {len(result.role.cases)} player-week cases over {players} players, "
              f"{result.role.season} weeks {result.role.weeks[0]}-{result.role.weeks[-1]}")
    if result.snapshot is not None:
        print(f"Mode 2: {result.snapshot.snapshots} snapshot(s), {result.snapshot.series_count} value series")
    if result.outcome is not None:
        print(f"Mode 3: {result.outcome.entries} ledger entries, {result.outcome.with_outcome} with an outcome")
    for note in result.unavailable:
        print(f"Unavailable: {note}")


if __name__ == "__main__":
    main()
