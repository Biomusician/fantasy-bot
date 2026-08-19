"""Generates the weekly Markdown report from locally cached data.
Run `scripts/pull_data.py` first to refresh Sleeper data if it's been a
while. Rankings sources refresh themselves automatically (cached ~20h).

    .venv/Scripts/python.exe scripts/generate_report.py [output_path]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.console import ensure_utf8_stdout
from sleeper_tool.report import generate_weekly_report
from sleeper_tool.storage import Storage
from sleeper_tool.valuation import ValuationEngine

ensure_utf8_stdout()

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "weekly_report.md"


def main() -> None:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    with Storage() as storage:
        current_week_raw = storage.get_meta("current_week")
        current_week = int(current_week_raw) if current_week_raw else None
        engine = ValuationEngine(current_week=current_week)
        report = generate_weekly_report(storage, engine)
    output_path.write_text(report, encoding="utf-8")
    print(f"Wrote report to {output_path} ({len(report)} chars)")


if __name__ == "__main__":
    main()
