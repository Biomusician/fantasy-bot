"""Manual CSV import for Fantasy Footballers Dynasty Pass rankings.

The Dynasty Pass rankings tool is paywalled (requires their Ultimate Draft
Kit+ subscription) and can't be scraped or logged into automatically. If you
have your own subscription, export/copy the rankings periodically into a CSV
at `data/ff_dynasty_pass.csv` and this module will pick it up — as long as
the file isn't more than a week old. Older files are treated as absent
rather than silently used, since Sleeper trade decisions shouldn't be made
on stale outside rankings.

Expected CSV columns (header row required): player_name, position, rank
Optional columns: team, notes
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ff_dynasty_pass.csv"
MAX_AGE = dt.timedelta(days=7)
SOURCE_URL = "https://www.thefantasyfootballers.com/2026-dynasty-pass/rankings/"


@dataclass(frozen=True)
class FFRankingRow:
    player_name: str
    position: str
    rank: int
    team: str | None = None
    notes: str | None = None


def _file_age(path: Path) -> dt.timedelta:
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) - mtime


def load_ff_dynasty_rankings(csv_path: Path | str = DEFAULT_CSV_PATH) -> list[FFRankingRow] | None:
    """Return parsed rankings, or None if the file is missing/stale/malformed.

    Returning None (rather than raising) lets callers treat "no fresh FF
    data" as a normal, expected state — this source is optional by design.
    """
    path = Path(csv_path)
    if not path.exists():
        return None

    age = _file_age(path)
    if age > MAX_AGE:
        return None

    rows: list[FFRankingRow] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"player_name", "position", "rank"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            return None
        for raw in reader:
            try:
                rows.append(
                    FFRankingRow(
                        player_name=raw["player_name"].strip(),
                        position=raw["position"].strip().upper(),
                        rank=int(raw["rank"]),
                        team=(raw.get("team") or "").strip() or None,
                        notes=(raw.get("notes") or "").strip() or None,
                    )
                )
            except (ValueError, KeyError, AttributeError):
                continue

    return rows or None


def ff_dynasty_status(csv_path: Path | str = DEFAULT_CSV_PATH) -> str:
    """Human-readable freshness status, for the weekly report's source list."""
    path = Path(csv_path)
    if not path.exists():
        return f"not provided (optional — export from {SOURCE_URL})"
    age = _file_age(path)
    if age > MAX_AGE:
        days = age.days
        return f"stale ({days}d old, ignoring — re-export from {SOURCE_URL})"
    hours = int(age.total_seconds() // 3600)
    return f"fresh ({hours}h old)"
