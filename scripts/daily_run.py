"""Single entry point for the automated daily run: syncs fresh Sleeper/
ranking data, then regenerates both report formats. Invoked by a scheduled
cloud routine, which then publishes data/dashboard.html as a Claude
Artifact itself — that step can't live in this script since Artifact
publishing is a Claude Code tool call, not a Python API.

    .venv/Scripts/python.exe scripts/daily_run.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.client import SleeperClient
from sleeper_tool.config import LEAGUES
from sleeper_tool.console import ensure_utf8_stdout
from sleeper_tool.decision_delta import is_complete_run, save_snapshot
from sleeper_tool.html_report import render_dashboard_html
from sleeper_tool.report import render_weekly_report
from sleeper_tool.report_data import build_weekly_report_data
from sleeper_tool.storage import Storage
from sleeper_tool.sync import sync_leagues
from sleeper_tool.valuation import ValuationEngine

ensure_utf8_stdout()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    client = SleeperClient()
    with Storage() as storage:
        results = sync_leagues(client, storage, LEAGUES, weeks_back=2)
        failed = [r for r in results if not r.ok]
        for r in failed:
            print(f"WARNING: sync failed for {r.league.name}: {r.error}", file=sys.stderr)

        current_week_raw = storage.get_meta("current_week")
        current_week = int(current_week_raw) if current_week_raw else None
        engine = ValuationEngine(current_week=current_week)

        report_data = build_weekly_report_data(storage, engine)

    report_path = DATA_DIR / "weekly_report.md"
    report_path.write_text(render_weekly_report(report_data), encoding="utf-8")

    dashboard_path = DATA_DIR / "dashboard.html"
    dashboard_path.write_text(render_dashboard_html(report_data), encoding="utf-8")

    print(f"OK: synced {len(results) - len(failed)}/{len(results)} leagues")
    print(f"OK: wrote {report_path}")
    print(f"OK: wrote {dashboard_path}")

    # The "since last run" delta only ever compares against a run that was
    # itself complete — a partial run as the baseline would make the next
    # delta report a missing league's players as "joined your roster".
    if is_complete_run(report_data, sync_failures=len(failed)) and report_data.snapshot is not None:
        print(f"OK: saved decision snapshot {save_snapshot(report_data.snapshot)}")
    else:
        print("SKIP: decision snapshot not saved (run was not complete)", file=sys.stderr)


if __name__ == "__main__":
    main()
