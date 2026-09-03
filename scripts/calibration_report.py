"""Writes the calibration report — an engineering diagnostic, not a user
report. Builds the same WeeklyReportData `scripts/generate_report.py` builds
from the local cache, then counts eligible-vs-triggered for every rule in the
decision layer and flags the pathologies.

    .venv/Scripts/python.exe scripts/calibration_report.py [output_path]

Reads the cache only: no Sleeper sync, no extra fetches beyond the ones the
normal report already makes. Pass --no-nfl-schedule to skip the nflverse
schedule the way build_weekly_report_data's own flag does.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.calibration import calibrate, render_calibration_markdown
from sleeper_tool.console import ensure_utf8_stdout
from sleeper_tool.report_data import build_weekly_report_data
from sleeper_tool.storage import Storage
from sleeper_tool.valuation import ValuationEngine

ensure_utf8_stdout()

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "calibration_report.md"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--no-nfl-schedule"]
    with_nfl_schedule = "--no-nfl-schedule" not in sys.argv[1:]
    output_path = Path(args[0]) if args else DEFAULT_OUTPUT

    with Storage() as storage:
        current_week_raw = storage.get_meta("current_week")
        current_week = int(current_week_raw) if current_week_raw else None
        engine = ValuationEngine(current_week=current_week)
        report = build_weekly_report_data(storage, engine, with_nfl_schedule=with_nfl_schedule)

    result = calibrate(report)
    output_path.write_text(render_calibration_markdown(result), encoding="utf-8")
    flagged = result.flagged()
    print(f"Wrote calibration report to {output_path}")
    print(f"{len(result.rules)} rules over {len(result.leagues)} leagues; {len(flagged)} flagged")


if __name__ == "__main__":
    main()
