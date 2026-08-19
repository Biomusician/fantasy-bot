"""Generates the HTML dashboard from locally cached data.

    .venv/Scripts/python.exe scripts/generate_dashboard.py [output_path]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.console import ensure_utf8_stdout
from sleeper_tool.html_report import render_dashboard_html
from sleeper_tool.report_data import build_weekly_report_data
from sleeper_tool.storage import Storage
from sleeper_tool.valuation import ValuationEngine

ensure_utf8_stdout()

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "dashboard.html"


def main() -> None:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    with Storage() as storage:
        current_week_raw = storage.get_meta("current_week")
        current_week = int(current_week_raw) if current_week_raw else None
        engine = ValuationEngine(current_week=current_week)
        report = build_weekly_report_data(storage, engine)
    html = render_dashboard_html(report)
    output_path.write_text(html, encoding="utf-8")
    print(f"Wrote dashboard to {output_path} ({len(html)} chars)")


if __name__ == "__main__":
    main()
