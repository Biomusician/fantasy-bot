"""Manual CSV import for the Fantasy Footballers weekly waiver-wire board.

The Ballers publish a waiver ranking every Tuesday. The user downloads the
CSV by hand and drops it into `data/manual/` without renaming it, so the file
name looks like "Week 2 Fantasy Football Waivers - Fantasy Footballers
Podcast.csv". Other Fantasy Footballers CSVs (DFS, dynasty) share that folder
naming style, so a file qualifies by its header, never by its name.

Only the free part of the board is read: the overall Rank and each host's
(Andy, Jason, Mike) rank. The FAAB and upcoming-matchup columns hold FootClan
paywall placeholder text for non-members; they are ignored outright — never
parsed, stored, or inferred from — and only their presence is noted.

The published Rank is authoritative. Host ranks are used only to describe
how much the three hosts agree; the overall rank is never recomputed from them.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sleeper_tool.name_matching import normalize_name

logger = logging.getLogger(__name__)

MANUAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "manual"
SOURCE_FAMILY = "ballers_waivers"
REQUIRED_COLUMNS = ("Name", "Team", "Rank", "Andy", "Jason", "Mike")
# The Ballers' dynasty-startup and rookie exports carry every REQUIRED_COLUMN
# too, so those alone can't tell a waiver board apart. The FAAB column header
# exists only on the waiver board; its presence is checked, its cells never read.
WAIVER_MARKER_COLUMN = "FAAB"

STRONG_AGREEMENT_MAX_SPREAD = 5
MODERATE_AGREEMENT_MAX_SPREAD = 12
STRONG_AGREEMENT = "Strong Ballers Agreement"
MODERATE_AGREEMENT = "Moderate Ballers Agreement"
BALLERS_SPLIT = "Ballers Split"

# The board is published weekly. A file whose name carries no week can't be
# placed on the calendar, so it is only trusted while it is plausibly this
# week's download.
MAX_UNDATED_AGE = dt.timedelta(days=6)

_WEEK_RE = re.compile(r"week\s*(\d{1,2})", re.IGNORECASE)
_NO_FILE_REASON = "no Fantasy Footballers waiver CSV in data/manual"


@dataclass(frozen=True)
class BallersRow:
    name: str
    team: str | None
    rank: int
    andy: int | None
    jason: int | None
    mike: int | None

    @property
    def spread(self) -> int | None:
        hosts = [r for r in (self.andy, self.jason, self.mike) if r is not None]
        # One host rank says nothing about agreement.
        if len(hosts) < 2:
            return None
        return max(hosts) - min(hosts)

    @property
    def agreement(self) -> str | None:
        spread = self.spread
        if spread is None:
            return None
        if spread <= STRONG_AGREEMENT_MAX_SPREAD:
            return STRONG_AGREEMENT
        if spread <= MODERATE_AGREEMENT_MAX_SPREAD:
            return MODERATE_AGREEMENT
        return BALLERS_SPLIT


@dataclass
class BallersBoard:
    week: int | None
    source_file: str | None
    loaded_at: dt.datetime
    file_mtime: dt.datetime | None
    rows: list[BallersRow]
    problems: list[str]
    status: str


def week_from_filename(name: str) -> int | None:
    match = _WEEK_RE.search(name or "")
    return int(match.group(1)) if match else None


def _clean_header_cell(cell: str) -> str:
    # utf-8-sig strips a leading BOM, but an editor re-save can leave one
    # inside the first quoted cell.
    return cell.replace("﻿", "").strip()


def is_ballers_waiver_header(fieldnames) -> bool:
    if not fieldnames:
        return False
    cleaned = {_clean_header_cell(str(f)) for f in fieldnames if f is not None}
    return WAIVER_MARKER_COLUMN in cleaned and all(col in cleaned for col in REQUIRED_COLUMNS)


def _read_header(path: Path) -> list[str] | None:
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            return next(csv.reader(f), None)
    except (OSError, UnicodeError, csv.Error):
        return None


def _as_utc(moment: dt.datetime | None) -> dt.datetime:
    if moment is None:
        return dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def _mtime(path: Path) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return None


def _format_age(age: dt.timedelta) -> str:
    hours = int(age.total_seconds() // 3600)
    if hours < 1:
        return "under 1h"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _no_file_reason(directory: Path) -> str:
    if directory.resolve() == MANUAL_DIR.resolve():
        return _NO_FILE_REASON
    return f"no Fantasy Footballers waiver CSV in {directory}"


def find_waiver_csv(
    directory: Path = MANUAL_DIR,
    *,
    target_week: int | None = None,
    now: dt.datetime | None = None,
) -> tuple[Path | None, str]:
    now = _as_utc(now)
    directory = Path(directory)
    if not directory.is_dir():
        return None, _no_file_reason(directory)

    # (path, filename week, mtime) for every CSV with the waiver-board header.
    valid: list[tuple[Path, int | None, dt.datetime]] = []
    for path in sorted(directory.glob("*.csv")):
        if not is_ballers_waiver_header(_read_header(path)):
            continue
        mtime = _mtime(path)
        if mtime is None:
            continue
        valid.append((path, week_from_filename(path.name), mtime))
    if not valid:
        return None, _no_file_reason(directory)

    def newest(entries):
        # Name breaks mtime ties so the choice never depends on glob order.
        return max(entries, key=lambda e: (e[2], e[0].name))

    if target_week is not None:
        same_week = [e for e in valid if e[1] == target_week]
        if same_week:
            path = newest(same_week)[0]
            return path, f"week {target_week} Ballers board: {path.name}"

    undated = [e for e in valid if e[1] is None]
    fresh_undated = [e for e in undated if now - e[2] <= MAX_UNDATED_AGE]
    if fresh_undated:
        path, _, mtime = newest(fresh_undated)
        return path, f"undated Ballers board {path.name} (modified {_format_age(now - mtime)} ago)"

    dated = [e for e in valid if e[1] is not None]
    if target_week is None and dated:
        path, week, _ = max(dated, key=lambda e: (e[1], e[2], e[0].name))
        return path, f"newest Ballers board is week {week}: {path.name}"

    if dated:
        # A board for a different week would rank last week's waiver pool as
        # if it were this week's — never use it.
        latest_week = max(e[1] for e in dated)
        return None, (
            f"newest Ballers board is for week {latest_week}, not week {target_week}"
            " — download this week's CSV"
        )

    path, _, mtime = newest(undated)
    return None, (
        f"Ballers CSV {path.name} has no week in its name and is {_format_age(now - mtime)} old"
        " — download this week's CSV"
    )


def _parse_int(cell: str | None) -> int | None:
    if cell is None:
        return None
    text = cell.strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    # Ranks start at 1; zero or negative is a spreadsheet artefact, not a rank.
    return value if value > 0 else None


def parse_ballers_csv(
    path: Path,
    *,
    week: int | None = None,
    now: dt.datetime | None = None,
) -> BallersBoard:
    """Parse one waiver CSV. Raises OSError/UnicodeError/csv.Error on an
    unreadable file; `load_ballers_board` is the non-raising entry point.

    `week` is the week the caller expects. The file name's week wins when both
    are known, and a disagreement is recorded as a problem.
    """
    path = Path(path)
    loaded_at = _as_utc(now)
    file_mtime = _mtime(path)
    problems: list[str] = []

    file_week = week_from_filename(path.name)
    board_week = file_week if file_week is not None else week
    if file_week is not None and week is not None and file_week != week:
        problems.append(f"file name says week {file_week} but week {week} was expected")
    elif file_week is None and week is not None:
        problems.append(f"file name carries no week; assumed week {week}")

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        data_rows = list(reader)

    def board(rows: list[BallersRow], status: str) -> BallersBoard:
        return BallersBoard(
            week=board_week,
            source_file=path.name,
            loaded_at=loaded_at,
            file_mtime=file_mtime,
            rows=rows,
            problems=problems,
            status=status,
        )

    if not is_ballers_waiver_header(header):
        problems.append(
            "header is not a waiver board: needs " + ", ".join((*REQUIRED_COLUMNS, WAIVER_MARKER_COLUMN))
        )
        return board([], f"{path.name} is not a Fantasy Footballers waiver board")

    cleaned_header = [_clean_header_cell(c) for c in header]
    column = {name: cleaned_header.index(name) for name in REQUIRED_COLUMNS}
    ignored = [c for c in cleaned_header if c not in REQUIRED_COLUMNS]
    if ignored:
        # Names only — the cells behind them are FootClan-gated placeholders.
        noun = "column" if len(ignored) == 1 else "columns"
        problems.append(
            f"ignored {len(ignored)} gated {noun} ({', '.join(ignored)}); only the free board is read"
        )

    def cell(raw: list[str], name: str) -> str:
        idx = column[name]
        return raw[idx].strip() if idx < len(raw) else ""

    kept: dict[str, BallersRow] = {}
    for line_no, raw in enumerate(data_rows, start=2):
        if not any(c.strip() for c in raw):
            continue
        name = cell(raw, "Name")
        if not name:
            problems.append(f"line {line_no}: blank player name, row skipped")
            continue
        rank = _parse_int(cell(raw, "Rank"))
        if rank is None:
            problems.append(f"line {line_no}: {name} has no usable Rank ({cell(raw, 'Rank')!r}), row skipped")
            continue
        row = BallersRow(
            name=name,
            team=cell(raw, "Team").upper() or None,
            rank=rank,
            andy=_parse_int(cell(raw, "Andy")),
            jason=_parse_int(cell(raw, "Jason")),
            mike=_parse_int(cell(raw, "Mike")),
        )
        key = normalize_name(name)
        previous = kept.get(key)
        if previous is not None:
            better = previous if previous.rank <= row.rank else row
            problems.append(
                f"duplicate player {name} (ranks {previous.rank} and {row.rank}); kept rank {better.rank}"
            )
            kept[key] = better
            continue
        kept[key] = row

    rows = sorted(kept.values(), key=lambda r: (r.rank, r.name))
    week_label = f"Week {board_week} board" if board_week is not None else "Undated board"
    parts = [week_label, f"{len(rows)} players"]
    if file_mtime is not None:
        parts.append(f"loaded {_format_age(max(loaded_at - file_mtime, dt.timedelta(0)))} ago")
    return board(rows, " · ".join(parts))


def load_ballers_board(
    directory: Path = MANUAL_DIR,
    *,
    target_week: int | None = None,
    now: dt.datetime | None = None,
) -> BallersBoard | None:
    """The board to use this run, or None. Never raises: this source is
    optional, and a bad download must not take the weekly run down with it."""
    try:
        path, reason = find_waiver_csv(directory, target_week=target_week, now=now)
    except OSError as exc:
        logger.warning("Ballers waiver board: could not scan %s: %s", directory, exc)
        return None
    if path is None:
        logger.info("Ballers waiver board not used: %s", reason)
        return None
    try:
        board = parse_ballers_csv(path, week=target_week, now=now)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        logger.warning("Ballers waiver board %s unreadable: %s", path.name, exc)
        return None
    if not board.rows:
        logger.warning("Ballers waiver board %s has no usable rows", path.name)
        return None
    return board
