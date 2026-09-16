"""RotoBaller weekly waiver-wire rankings (an acquisition-priority board).

This is a different product from the season-long rankings API that
`rotoballer.py` reads. Every Tuesday in season RotoBaller publishes an
article titled "Waiver Wire Rankings - Fantasy Football Week N (YYYY)" whose
body carries one combined, ranked HTML table (pasted from Google Sheets, so
the markup is plain `<table><tr><td>`):

    Rank | Player Name | Position | % Ros. | Baller Move

"Baller Move" is a short, regular tag — "Add in All Leagues", "Add in 12+
Team PPR Leagues", "Add in 14+ Team Leagues", "Add in 2QB Leagues" — which is
the only league-size signal we take. The article's prose, and the per-position
tables that repeat the same players below it, are ignored.

Research, 2026-09-15 (Week 2), two requests:
  * https://www.rotoballer.com/waiver-wire-rankings-fantasy-football-week-2-2026/1930758
    — server-rendered HTML, not paywalled, not JS-built. The combined table
    had 85 rows (62 RB/WR/TE, then QBs, then D/STs, grouped at the bottom on
    purpose). There is NO NFL team column, so `team` is always None here;
    consumers match on name + position.
  * https://www.rotoballer.com/category/nfl/fantasy-football-advice-analysis/waiver-wire-articles
    — the stable category listing. It links the current week's article by
    its slug. The URL changes every week (slug + post id), hence discovery.
  * No waiver endpoint in the `wp-json/rb/v1/` family was found referenced on
    either page, so none was probed.

Related slugs deliberately NOT matched by discovery: "updated-waiver-wire-
rankings-ahead-of-...-week-N" and "weekend-waiver-wire-rankings-...-week-N".
Their structure was not inspected (politeness budget), and the Tuesday
article is the canonical weekly board.

Fetch (`_discover_article_url`, `fetch_article_html`) and parse
(`parse_waiver_board`, `find_latest_article_url`) are kept apart so a layout
change only touches the parsers. A refresh costs exactly two requests.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import re
from dataclasses import asdict, dataclass

import requests

from sleeper_tool.rankings import cache
from sleeper_tool.rankings.freshness import ceiling_for

logger = logging.getLogger(__name__)

SOURCE = "rotoballer_waivers"
DEFAULT_MAX_AGE = dt.timedelta(hours=20)

INDEX_URL = "https://www.rotoballer.com/category/nfl/fantasy-football-advice-analysis/waiver-wire-articles"

# Same browser UA as rotoballer.py; the site serves bots a different page.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Anchored on the domain + "/" so the "updated-" and "weekend-" variants,
# whose slugs merely contain this text, don't match.
_ARTICLE_URL_RE = re.compile(
    r"https://www\.rotoballer\.com/waiver-wire-rankings-fantasy-football-week-(\d{1,2})-(\d{4})/(\d+)"
)
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_WEEK_RE = re.compile(r"\bWeek\s+(\d{1,2})\b", re.I)
_MIN_SIZE_RE = re.compile(r"\b(\d{1,2})\+\s*Team", re.I)
# "Add in 2QB Leagues" / "Superflex Leagues" states a FORMAT, not a size.
_SUPERFLEX_RE = re.compile(r"\b(2\s*QB|Superflex|Super\s*Flex)\b", re.I)


class RotoBallerWaiverFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WaiverBoardRow:
    rank: int
    name: str
    team: str | None
    position: str | None
    league_size_note: str | None  # verbatim short tag e.g. "12+ Team PPR Leagues", else None
    min_league_size: int | None  # 12 for "12+", None when not stated
    superflex_only: bool = False  # tagged for 2QB/Superflex leagues only


@dataclass
class WaiverBoard:
    source: str
    week: int | None
    url: str | None
    fetched_at: dt.datetime | None
    rows: list[WaiverBoardRow]
    problems: list[str]


# --- parsing ---------------------------------------------------------------


def _cell_text(fragment: str) -> str:
    text = html.unescape(_TAG_RE.sub("", fragment))
    return re.sub(r"\s+", " ", text).replace(" ", " ").strip()


def _table_rows(table_html: str) -> list[list[str]]:
    return [[_cell_text(c) for c in _CELL_RE.findall(row)] for row in _ROW_RE.findall(table_html)]


def _header_index(header: list[str]) -> dict[str, int] | None:
    """Map our fields to column positions by header name, so a reordered or
    widened table still parses. None when this isn't the combined board."""
    lowered = [h.lower() for h in header]

    def find(*needles: str) -> int | None:
        for i, h in enumerate(lowered):
            if any(n in h for n in needles):
                return i
        return None

    rank, name, position = find("rank"), find("player"), find("position", "pos")
    if rank is None or name is None:
        return None
    return {"rank": rank, "name": name, "position": position, "move": find("baller move", "move")}


def _league_size(move: str | None) -> tuple[str | None, int | None, bool]:
    """"Add in 12+ Team PPR Leagues" -> ("12+ Team PPR Leagues", 12, False).
    "Add in All Leagues" keeps its note but states no minimum size. A "2QB
    Leagues" tag states a FORMAT instead of a size, and stating no size is
    not the same as applying to every league: without the flag those rows
    read as universal recommendations in a 1QB league."""
    if not move:
        return None, None, False
    note = re.sub(r"^add\s+in\s+", "", move, flags=re.I).strip() or None
    match = _MIN_SIZE_RE.search(move)
    return note, int(match.group(1)) if match else None, bool(_SUPERFLEX_RE.search(move))


def _parse_week(raw: str) -> int | None:
    for pattern in (_TITLE_RE, _H1_RE):
        found = pattern.search(raw)
        if found:
            week = _WEEK_RE.search(_cell_text(found.group(1)))
            if week:
                return int(week.group(1))
    return None


def parse_waiver_board(raw: str, *, url: str | None = None) -> dict:
    """Parse the article HTML into a JSON-able payload. Only the first table
    whose header carries both Rank and Player columns is read — the
    per-position tables below it repeat the same players without a rank."""
    problems: list[str] = []
    rows: list[WaiverBoardRow] = []
    for table in _TABLE_RE.findall(raw):
        table_rows = _table_rows(table)
        if not table_rows:
            continue
        columns = _header_index(table_rows[0])
        if columns is None:
            continue
        seen_ranks: set[int] = set()
        for cells in table_rows[1:]:
            if len(cells) <= max(columns["rank"], columns["name"]):
                problems.append(f"short row skipped: {cells!r}")
                continue
            try:
                rank = int(cells[columns["rank"]])
            except ValueError:
                problems.append(f"non-numeric rank skipped: {cells!r}")
                continue
            name = cells[columns["name"]]
            if not name:
                problems.append(f"rank {rank} has no player name")
                continue
            if rank in seen_ranks:
                problems.append(f"duplicate rank {rank} ({name})")
            seen_ranks.add(rank)

            def col(key: str) -> str | None:
                i = columns[key]
                return (cells[i] or None) if i is not None and i < len(cells) else None

            note, min_size, superflex_only = _league_size(col("move"))
            position = col("position")
            rows.append(
                WaiverBoardRow(
                    rank=rank,
                    name=name,
                    team=None,  # the board has no team column
                    position=position.upper() if position else None,
                    league_size_note=note,
                    min_league_size=min_size,
                    superflex_only=superflex_only,
                )
            )
        break
    if not rows:
        raise RotoBallerWaiverFetchError("Parsed 0 rows from the RotoBaller waiver board — layout may have changed")
    rows.sort(key=lambda r: r.rank)
    return {"week": _parse_week(raw), "url": url, "rows": [asdict(r) for r in rows], "problems": problems}


def find_latest_article_url(index_html: str) -> str | None:
    """Newest weekly waiver-rankings article linked from the category page,
    by (season, week, post id)."""
    best: tuple[int, int, int] | None = None
    best_url: str | None = None
    for match in _ARTICLE_URL_RE.finditer(index_html):
        key = (int(match.group(2)), int(match.group(1)), int(match.group(3)))
        if best is None or key > best:
            best, best_url = key, match.group(0)
    return best_url


def _with_format_flag(row: dict) -> dict:
    """A board cached before the format tag was parsed still holds the note
    it was parsed from, so the flag is re-derived rather than waiting a week
    for the next fetch to correct itself."""
    if "superflex_only" in row:
        return row
    return {**row, "superflex_only": bool(_SUPERFLEX_RE.search(row.get("league_size_note") or ""))}


def board_from_snapshot(snapshot) -> WaiverBoard | None:
    if snapshot is None or not isinstance(snapshot.payload, dict):
        return None
    payload = snapshot.payload
    try:
        rows = [WaiverBoardRow(**_with_format_flag(row)) for row in payload.get("rows") or []]
    except TypeError:
        logger.warning("RotoBaller waiver cache has an unexpected row shape; ignoring it")
        return None
    return WaiverBoard(
        source=SOURCE,
        week=payload.get("week"),
        url=payload.get("url"),
        fetched_at=snapshot.fetched_at,
        rows=rows,
        problems=list(payload.get("problems") or []),
    )


# --- fetching --------------------------------------------------------------


def _get(url: str) -> str:
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _discover_article_url() -> str:
    url = find_latest_article_url(_get(INDEX_URL))
    if url is None:
        raise RotoBallerWaiverFetchError(f"No waiver-wire-rankings article linked from {INDEX_URL}")
    return url


def _fetch_payload() -> dict:
    url = _discover_article_url()
    return parse_waiver_board(_get(url), url=url)


def load_rotoballer_waiver_board(
    *, force: bool = False, allow_fetch: bool = True, max_age: dt.timedelta = DEFAULT_MAX_AGE
) -> WaiverBoard | None:
    """The current board, or None. Never raises: this source is optional and
    Fantasy Bot must run the same without it."""
    ceiling = ceiling_for(SOURCE)
    try:
        if not allow_fetch:
            snapshot = cache.load_snapshot(SOURCE)
            if snapshot is None or (ceiling is not None and snapshot.age() > ceiling):
                return None
        else:
            snapshot = cache.get_or_fetch(SOURCE, _fetch_payload, max_age=max_age, force=force, ceiling=ceiling)
        return board_from_snapshot(snapshot)
    except Exception as exc:  # noqa: BLE001 — optional source, absent on any failure
        logger.warning("RotoBaller waiver board unavailable: %s", exc)
        return None
