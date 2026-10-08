"""NFL schedule from nflverse's public games.csv — the one external dataset
this tool reads that isn't a ranking source. Direct HTTP through the same
file cache the ranking scrapers use (data/rankings_cache/), fetched at
most once a day unless forced, never in tests (fixtures build a Schedule
from CSV text via parse_schedule_csv / schedule_from_rows).

Only the columns the decision layer needs are kept: season, game_type,
week, gameday, home/away team. nflverse codes the Rams "LA" where Sleeper
says "LAR"; the alias table below normalizes to Sleeper's codes so
`schedule.opponent("LAR", 3)` just works.

A missing schedule (no cache, fetch failed, season not published yet) is
None and every consumer degrades to what it had before — projections and
the ranking sources' bye weeks. Nothing here invents opponent strength.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from dataclasses import dataclass

import requests

from sleeper_tool.rankings.cache import RankingSnapshot, get_or_fetch, load_snapshot
from sleeper_tool.rankings.freshness import ceiling_for
from sleeper_tool.rankings.snapshot_validation import by_row_count, rows_of

logger = logging.getLogger(__name__)

SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
# The release asset above is a published copy of this file; while nflverse
# replaces the asset the release URL can briefly 404, so the source repo is
# tried second. Same columns, same games.
SCHEDULE_MIRROR_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
SCHEDULE_URLS = (SCHEDULE_URL, SCHEDULE_MIRROR_URL)
SCHEDULE_SOURCE = "nflverse_schedule"
SCHEDULE_MAX_AGE = dt.timedelta(hours=24)
REGULAR_SEASON = "REG"
_TEAM_ALIASES = {"LA": "LAR"}
_HEADERS = {"User-Agent": "sleeper-dynasty-tool/0.1 (personal use)"}


def normalize_team(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip().upper()
    return _TEAM_ALIASES.get(code, code)


@dataclass(frozen=True)
class Game:
    season: int
    week: int
    game_type: str
    home: str
    away: str
    gameday: str | None  # ISO date text as published; informational


@dataclass
class Schedule:
    season: int
    games: list[Game]
    fetched_at: dt.datetime | None = None

    def regular_weeks(self) -> list[int]:
        return sorted({g.week for g in self.games if g.game_type == REGULAR_SEASON})

    def last_regular_week(self) -> int | None:
        weeks = self.regular_weeks()
        return weeks[-1] if weeks else None

    def teams(self) -> set[str]:
        return {t for g in self.games if g.game_type == REGULAR_SEASON for t in (g.home, g.away)}

    def game_for(self, team: str | None, week: int) -> Game | None:
        team = normalize_team(team)
        if team is None:
            return None
        for g in self.games:
            if g.game_type == REGULAR_SEASON and g.week == week and team in (g.home, g.away):
                return g
        return None

    def opponent(self, team: str | None, week: int) -> str | None:
        """Regular-season opponent, or None on a bye / unknown team / week
        outside the schedule."""
        g = self.game_for(team, week)
        if g is None:
            return None
        return g.away if normalize_team(team) == g.home else g.home

    def is_bye(self, team: str | None, week: int) -> bool:
        """True only for a team the schedule knows, in a regular-season
        week it has no game — never for an unknown team or an unscheduled
        week, which are "don't know", not "bye"."""
        team = normalize_team(team)
        if team is None or team not in self.teams() or week not in self.regular_weeks():
            return False
        return self.game_for(team, week) is None

    def bye_weeks(self, team: str | None) -> set[int]:
        team = normalize_team(team)
        if team is None or team not in self.teams():
            return set()
        return {w for w in self.regular_weeks() if self.game_for(team, w) is None}


def parse_schedule_csv(text: str, season: int) -> list[dict]:
    """The season's rows as plain JSON-able dicts (the cache payload)."""
    rows: list[dict] = []
    for raw in csv.DictReader(io.StringIO(text)):
        try:
            if int(raw["season"]) != season:
                continue
            rows.append({
                "season": season,
                "week": int(raw["week"]),
                "game_type": raw["game_type"],
                "home": normalize_team(raw["home_team"]),
                "away": normalize_team(raw["away_team"]),
                "gameday": raw.get("gameday") or None,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return rows


def schedule_from_rows(rows: list[dict], season: int, fetched_at: dt.datetime | None = None) -> Schedule:
    games: list[Game] = []
    for r in rows:
        try:
            if int(r.get("season", season)) != season or not r.get("home") or not r.get("away"):
                continue
            games.append(Game(season=season, week=int(r["week"]), game_type=str(r["game_type"]), home=r["home"], away=r["away"], gameday=r.get("gameday")))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # a malformed cached row is skipped, never fatal
    return Schedule(season=season, games=games, fetched_at=fetched_at)


def fetch_schedule_rows(season: int) -> dict:
    failures: list[str] = []
    for url in SCHEDULE_URLS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=60)
            resp.raise_for_status()
            rows = parse_schedule_csv(resp.text, season)
            if not rows:
                raise ValueError(f"no rows for season {season}")
        except Exception as exc:  # noqa: BLE001 — try the next copy, report all
            failures.append(f"{url}: {exc}")
            continue
        return {"season": season, "rows": rows}
    raise RuntimeError("nflverse schedule unavailable from every source: " + "; ".join(failures))


# A season is ~285 regular + post games across 32 teams. The team count is
# what matters: a partial file still parses, and `regular_weeks()` is derived
# from whatever survived — so three rows produce a confident "no byes" rather
# than a missing schedule, which is a wrong answer rather than an absent one.
MIN_SCHEDULE_ROWS = 100
MIN_SCHEDULE_TEAMS = 30


def _covers_the_league(payload) -> str | None:
    rows = (payload or {}).get("rows") or []
    teams = {row.get(key) for row in rows for key in ("home", "away")} - {None, ""}
    if len(teams) < MIN_SCHEDULE_TEAMS:
        return (
            f"NFL schedule refresh covers {len(teams)} teams across {len(rows)} games — "
            "a partial file gives wrong bye weeks rather than no schedule"
        )
    return None


_validate_schedule = by_row_count(
    label="NFL schedule", floor=MIN_SCHEDULE_ROWS, extra=_covers_the_league
)


def load_schedule(season: int, *, force: bool = False) -> Schedule | None:
    """Cached daily; a cache holding a different season is treated as
    stale (a new season's first run refetches). None when nothing usable
    exists — consumers must cope."""
    cached = load_snapshot(SCHEDULE_SOURCE)
    if cached is not None and (cached.payload or {}).get("season") != season:
        force = True
    try:
        snapshot: RankingSnapshot = get_or_fetch(
            SCHEDULE_SOURCE,
            lambda: fetch_schedule_rows(season),
            max_age=SCHEDULE_MAX_AGE,
            force=force,
            ceiling=ceiling_for(SCHEDULE_SOURCE),
            validate=_validate_schedule,
        )
    except Exception as exc:  # no cache to fall back to
        logger.warning("NFL schedule unavailable for %s: %s", season, exc)
        return None
    payload = snapshot.payload or {}
    if payload.get("season") != season:
        return None
    return schedule_from_rows(payload.get("rows") or [], season, fetched_at=snapshot.fetched_at)
