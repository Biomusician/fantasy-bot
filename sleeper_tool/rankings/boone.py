"""Justin Boone's weekly positional rankings (Yahoo Sports) — supplemental only.

Boone finished #1 in FantasyPros' 2025 in-season accuracy, so his weekly
board is worth having as a cross-check. It is never a primary signal and the
bot must behave identically when it is missing: `load_boone_board` never
raises, it logs a warning and returns None.

Where the data actually lives (researched 2026-09-15, NFL Week 2):

  The Yahoo articles — e.g.
    https://sports.yahoo.com/fantasy/article/fantasy-football-rankings-justin-boones-top-running-backs-for-week-2-202322255.html
    https://sports.yahoo.com/fantasy/article/justin-boones-fantasy-football-flex-rankings-for-week-2-203257269.html
  (all linked from https://sports.yahoo.com/author/justin-boone/) contain NO
  player rows in their HTML: no <table>, no names anywhere in the page. The
  article body carries a `rankingPro` node whose URL is a FantasyPros partner
  widget:
    https://partners.fantasypros.com/external/widget/fp-widget.php?...&wtype=ST
        &filters=317&scoring=HALF&expert=1663&year=2026&week=2&half_positions=RB
  That widget is an empty <div> plus cdn.fantasypros.com/js/fp-widget-2.0.js,
  which loads the rows from a JSON endpoint on the same partner host:
    https://partners.fantasypros.com/api/v1/consensus-rankings.php
        ?sport=NFL&year=2026&week=2&position=RB&scoring=HALF
        &type=ST&widget=ST&experts=show&id=1663&filters=317

So this module does not scrape Yahoo at all. It calls that JSON endpoint
directly — one request per position list — with the expert filter pinned to
Boone (317; the response's `expert_names` confirms "Justin Boone"). That is a
structured, parametric source: no article-URL discovery, no HTML parsing, and
the same backend every FantasyPros partner embed uses. Hence
ENABLED_BY_DEFAULT is True.

Known fragilities, each handled by failing closed rather than guessing:
  - It is an undocumented partner API. A changed shape parses to 0 rows and
    raises BooneFetchError, which the loader turns into warn + None.
  - The week MUST be passed. Without `week` the endpoint silently returns
    Boone's preseason draft board (`ranking_type_name: "draft"`, week 0); the
    parser rejects anything that isn't a weekly board.
  - Expert id 317 / partner id 1663 could change (or Boone could leave Yahoo).
    A response whose experts don't include Justin Boone is rejected.
  - Boone publishes QB, D/ST and K once (FantasyPros labels them plain
    "Weekly", not scoring-specific); only RB/WR/TE/FLEX differ between
    half-PPR and full-PPR. Both boards request every list with their own
    scoring param; QB/DST/K under PPR were not separately verified.

Request budget: one refresh fetches at most one response per position list
(7), spaced `_REQUEST_SPACING` seconds apart, and the 20h max_age means a
daily run refreshes each scoring variant at most once. He updates Tuesday,
Thursday and Sunday morning, so 20h picks up each revision within a day.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

import requests

from sleeper_tool.name_matching import normalize_name
from sleeper_tool.rankings.cache import RankingSnapshot, get_or_fetch, load_snapshot
from sleeper_tool.rankings.freshness import ceiling_for

logger = logging.getLogger(__name__)

SOURCE_FAMILY = "boone_weekly"
ENABLED_BY_DEFAULT: bool = True

HALF_PPR = "half_ppr"
FULL_PPR = "ppr"
POSITIONS = ("QB", "RB", "WR", "TE", "FLEX", "DST", "K")
# The lists a refresh actually requests. The waiver evidence reads positional
# ranks for skill players only (FLEX is derivable, DST/K are the streamer
# planner's job), and every list is one more request to a source that can
# rate-limit — four per scoring variant, once a day, not seven.
FETCHED_POSITIONS = ("QB", "RB", "WR", "TE")

DEFAULT_MAX_AGE = dt.timedelta(hours=20)

_API_URL = "https://partners.fantasypros.com/api/v1/consensus-rankings.php"
_EXPERT_FILTER = "317"  # Boone's FantasyPros expert id
_PARTNER_ID = "1663"  # the Yahoo partner embed id the articles use
_EXPERT_NAME = "Justin Boone"
_API_SCORING = {HALF_PPR: "HALF", FULL_PPR: "PPR"}
# Our list name -> the endpoint's position code. Only FLEX differs.
_API_POSITION = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "FLEX": "FLX", "DST": "DST", "K": "K"}
_LIST_FROM_API = {code: name for name, code in _API_POSITION.items()}
# Sleeper spells two positions differently from Boone's list names.
_POSITION_ALIASES = {"DEF": "DST", "D/ST": "DST", "PK": "K", "FLX": "FLEX"}
# FantasyPros uses JAC; Sleeper and nflverse use JAX.
_TEAM_ALIASES = {"JAC": "JAX"}
_REQUEST_SPACING = 1.5  # seconds between position-list requests in one refresh

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# The widget JS asks for JSONP (`FPW.rankingsCB({...})`); tolerate that
# wrapper in case the endpoint ever stops honouring a callback-less request.
_JSONP_RE = re.compile(r"^\s*[\w.$]+\((.*)\)\s*;?\s*$", re.DOTALL)


class BooneFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class BooneRow:
    position_list: str  # which list this came from: QB/RB/WR/TE/FLEX/DST/K
    rank: int  # rank within that list
    name: str
    team: str | None
    position: str | None  # player's own position (FLEX list rows carry RB/WR/TE)


@dataclass
class BooneBoard:
    scoring: str  # HALF_PPR or FULL_PPR
    week: int | None
    fetched_at: dt.datetime | None
    rows: list[BooneRow]
    urls: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def positional_rank(self, name: str, position: str) -> int | None:
        """Rank in that position's own list (never the FLEX list), joined on
        normalize_name. A defense also matches on its team code, since
        Sleeper names DEF entries by team while Boone uses "Philadelphia Eagles"."""
        pos = (position or "").upper()
        pos = _POSITION_ALIASES.get(pos, pos)
        key = normalize_name(name)
        team_key = _team((name or "").strip().upper()) if pos == "DST" else None
        for row in self.rows:
            if row.position_list != pos:
                continue
            if key and normalize_name(row.name) == key:
                return row.rank
            if team_key and row.team == team_key:
                return row.rank
        return None


def scoring_for_ppr(ppr: float) -> str | None:
    """Map a league's points-per-reception to one of Boone's boards. A
    standard (0 PPR) league has no honest Boone mapping, so it gets None
    rather than being quietly served the half-PPR list."""
    if ppr is None:
        return None
    if ppr >= 0.75:
        return FULL_PPR
    if ppr >= 0.25:
        return HALF_PPR
    return None


def _team(code: str | None) -> str | None:
    if not code:
        return None
    code = code.upper()
    return _TEAM_ALIASES.get(code, code)


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_boone_page(raw: str, *, position_list: str | None = None, url: str | None = None) -> dict:
    """Parse one consensus-rankings.php response (one position list) into a
    JSON-able {"week", "position_list", "published", "rows", "problems"}.

    Raises BooneFetchError when the response is not Boone's weekly board or
    yields 0 rows — the caller must never mistake an empty or wrong list for
    "Boone ranks nobody"."""
    where = url or "Boone rankings response"
    text = raw or ""
    wrapped = _JSONP_RE.match(text)
    if wrapped and not text.lstrip().startswith("{"):
        text = wrapped.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BooneFetchError(f"{where} is not JSON ({exc}) — endpoint shape may have changed") from exc
    if not isinstance(data, dict):
        raise BooneFetchError(f"{where} is not a JSON object")

    kind = str(data.get("ranking_type_name") or "").lower()
    if kind != "weekly":
        raise BooneFetchError(
            f"{where} is a {kind or 'unlabelled'!r} board, not weekly rankings (week={data.get('week')!r})"
        )
    names = data.get("expert_names") or {}
    if _EXPERT_NAME not in names.values():
        raise BooneFetchError(f"{where} does not carry {_EXPERT_NAME}'s rankings (experts: {list(names.values())})")
    expert_ids = [eid for eid, n in names.items() if n == _EXPERT_NAME]

    api_pos = str(data.get("position_id") or "").upper()
    list_name = position_list or _LIST_FROM_API.get(api_pos, api_pos)
    if list_name not in POSITIONS:
        raise BooneFetchError(f"{where} has unknown position list {list_name!r}")
    week = _int(data.get("week"))
    problems: list[str] = []
    if week is None or week < 1:
        problems.append(f"{list_name}: response week {data.get('week')!r} is not a regular-season week")

    rows: list[dict] = []
    seen: set[int] = set()
    for player in data.get("players") or []:
        if not isinstance(player, dict):
            continue
        name = player.get("player_name")
        experts = player.get("experts") or {}
        rank = next((_int(experts[e]) for e in expert_ids if e in experts), None)
        if rank is None:
            rank = _int(player.get("rank_ecr"))
        if not name or rank is None or rank < 1:
            continue
        if rank in seen:
            problems.append(f"{list_name}: duplicate rank {rank} ({name})")
        seen.add(rank)
        rows.append(
            asdict(
                BooneRow(
                    position_list=list_name,
                    rank=rank,
                    name=str(name),
                    team=_team(player.get("player_team_id")),
                    position=(player.get("player_position_id") or None),
                )
            )
        )
    if not rows:
        raise BooneFetchError(f"Parsed 0 rows from {where} — endpoint shape may have changed")
    rows.sort(key=lambda r: r["rank"])
    published = (data.get("expert_pub") or {}).get(expert_ids[0]) if expert_ids else None
    return {"week": week, "position_list": list_name, "published": published, "rows": rows, "problems": problems}


def _list_url(position_list: str, scoring: str, week: int, season: int) -> str:
    params = {
        "sport": "NFL",
        "year": season,
        "week": week,
        "position": _API_POSITION[position_list],
        "scoring": _API_SCORING[scoring],
        "type": "ST",
        "widget": "ST",
        "experts": "show",
        "id": _PARTNER_ID,
        "filters": _EXPERT_FILTER,
    }
    return requests.Request("GET", _API_URL, params=params).prepare().url


def fetch_boone_list(position_list: str, scoring: str, week: int, season: int) -> tuple[str, str]:
    url = _list_url(position_list, scoring, week, season)
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30, allow_redirects=False)
    if resp.status_code in (301, 302, 303, 307, 308):
        raise BooneFetchError(f"{url} redirected — the partner endpoint moved")
    resp.raise_for_status()
    return url, resp.text


def _default_season(today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    # January/February games belong to the previous calendar year's season.
    return today.year if today.month >= 3 else today.year - 1


def fetch_boone_payload(scoring: str, week: int | None, season: int | None = None) -> dict:
    """Fetch and parse every position list for one scoring variant. One
    failed list becomes a problem on the board; all lists failing raises."""
    if scoring not in _API_SCORING:
        raise BooneFetchError(f"Unknown Boone scoring {scoring!r}; known: {list(_API_SCORING)}")
    if week is None or week < 1:
        raise BooneFetchError(
            "A regular-season week is required: without one the endpoint returns Boone's preseason draft board"
        )
    season = season or _default_season()
    rows: list[dict] = []
    urls: list[str] = []
    problems: list[str] = []
    for i, position_list in enumerate(FETCHED_POSITIONS):
        if i and _REQUEST_SPACING:
            time.sleep(_REQUEST_SPACING)
        try:
            url, raw = fetch_boone_list(position_list, scoring, week, season)
            parsed = parse_boone_page(raw, position_list=position_list, url=url)
        except Exception as exc:  # noqa: BLE001 — one list failing must not sink the board
            problems.append(f"{position_list}: {exc}")
            continue
        if parsed["week"] != week:
            problems.append(f"{position_list}: asked for week {week}, got week {parsed['week']}; list dropped")
            continue
        urls.append(url)
        rows.extend(parsed["rows"])
        problems.extend(parsed["problems"])
    if not rows:
        raise BooneFetchError(f"No Boone {scoring} lists parsed for week {week}: {'; '.join(problems)}")
    return {"scoring": scoring, "week": week, "season": season, "rows": rows, "urls": urls, "problems": problems}


def _board_from_snapshot(scoring: str, snapshot: RankingSnapshot) -> BooneBoard:
    payload = snapshot.payload or {}
    rows = [BooneRow(**row) for row in payload.get("rows") or []]
    problems = list(payload.get("problems") or [])
    if snapshot.served_from_fallback:
        problems.append(f"live refresh failed; serving the board fetched {snapshot.fetched_at.isoformat()}")
    return BooneBoard(
        scoring=scoring,
        week=payload.get("week"),
        fetched_at=snapshot.fetched_at,
        rows=rows,
        urls=list(payload.get("urls") or []),
        problems=problems,
    )


def load_boone_board(
    scoring: str,
    *,
    week: int | None = None,
    season: int | None = None,
    force: bool = False,
    allow_fetch: bool | None = None,
    max_age: dt.timedelta = DEFAULT_MAX_AGE,
) -> BooneBoard | None:
    """Cache-first Boone board for one scoring variant. NEVER raises.

    `allow_fetch=None` means ENABLED_BY_DEFAULT. Pass the NFL `week` the board
    is for: a live fetch needs it, and a cached board from a different week
    is refused (None) rather than served as this week's opinion. With no
    `week`, only a cached board is usable, whatever week it describes —
    check `board.week`.
    """
    try:
        if scoring not in _API_SCORING:
            logger.warning("Boone: no board for scoring %r", scoring)
            return None
        if allow_fetch is None:
            allow_fetch = ENABLED_BY_DEFAULT
        key = f"boone_{scoring}"
        ceiling = ceiling_for(SOURCE_FAMILY)
        cached = load_snapshot(key)
        cached_week = (cached.payload or {}).get("week") if cached is not None else None
        wrong_week = week is not None and cached is not None and cached_week != week

        if not allow_fetch or week is None:
            if cached is None or wrong_week:
                return None
            if ceiling is not None and cached.age() > ceiling:
                logger.warning("Boone %s cache is %s old, past its %s ceiling; ignoring it", scoring, cached.age(), ceiling)
                return None
            return _board_from_snapshot(scoring, cached)

        snapshot = get_or_fetch(
            key,
            lambda: fetch_boone_payload(scoring, week, season),
            max_age=max_age,
            force=force or wrong_week,
            ceiling=ceiling,
        )
        if (snapshot.payload or {}).get("week") != week:
            # get_or_fetch fell back to a cached board from another week.
            logger.warning("Boone %s refresh for week %s failed and the cache holds another week; skipping", scoring, week)
            return None
        return _board_from_snapshot(scoring, snapshot)
    except Exception as exc:  # noqa: BLE001 — Boone is optional; the bot runs without it
        logger.warning("Boone %s board unavailable: %s", scoring, exc)
        return None
