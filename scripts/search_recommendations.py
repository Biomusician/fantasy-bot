"""Query one built report from the cache — for debugging and personal use.

    .venv/Scripts/python.exe scripts/search_recommendations.py --player "Bijan"
    .venv/Scripts/python.exe scripts/search_recommendations.py --scarce
    .venv/Scripts/python.exe scripts/search_recommendations.py --role-ahead
    .venv/Scripts/python.exe scripts/search_recommendations.py --conflicts
    .venv/Scripts/python.exe scripts/search_recommendations.py --urgent
    .venv/Scripts/python.exe scripts/search_recommendations.py --watchlist [--player NAME]

Reads the cache only (same fetch policy as generate_report.py); never
writes the ledger, watchlist or snapshot.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.console import ensure_utf8_stdout
from sleeper_tool.recommendation_search import (
    conflicted_moves,
    role_ahead_of_market,
    search_player,
    urgent_actions,
    very_scarce_markets,
    watchlist_hits,
)
from sleeper_tool.report_data import build_weekly_report_data
from sleeper_tool.storage import Storage
from sleeper_tool.valuation import ValuationEngine

ensure_utf8_stdout()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", help="substring of a player name (suffix/case-insensitive)")
    ap.add_argument("--scarce", action="store_true", help="every Very Scarce replacement market")
    ap.add_argument("--role-ahead", action="store_true", help="every Role Ahead of Market")
    ap.add_argument("--conflicts", action="store_true", help="every Conflicted Move")
    ap.add_argument("--urgent", action="store_true", help="Best Moves at Immediate / This Week urgency")
    ap.add_argument("--watchlist", action="store_true", help="watchlist items (optionally for --player)")
    ap.add_argument("--no-nfl-schedule", action="store_true")
    args = ap.parse_args()
    if not any((args.player, args.scarce, args.role_ahead, args.conflicts, args.urgent, args.watchlist)):
        ap.error("pick at least one query")

    with Storage() as storage:
        current_week_raw = storage.get_meta("current_week")
        current_week = int(current_week_raw) if current_week_raw else None
        engine = ValuationEngine(current_week=current_week)
        report = build_weekly_report_data(storage, engine, with_nfl_schedule=not args.no_nfl_schedule)

    hits = []
    if args.player and not args.watchlist:
        hits += search_player(report, args.player)
    if args.scarce:
        hits += very_scarce_markets(report)
    if args.role_ahead:
        hits += role_ahead_of_market(report)
    if args.conflicts:
        hits += conflicted_moves(report)
    if args.urgent:
        hits += urgent_actions(report)
    if args.watchlist:
        hits += watchlist_hits(report, args.player)
    if not hits:
        print("no hits")
    for hit in hits:
        print(hit.describe())


if __name__ == "__main__":
    main()
