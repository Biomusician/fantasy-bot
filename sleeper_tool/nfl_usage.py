"""Weekly NFL usage from nflverse — fetch, parse and cache only.

This module knows how to get player-week and team-week stat lines plus
snap counts onto disk and into typed rows. It computes nothing about
roles; `role_analysis.py` does that, and `role_trends.py` labels it. The
split is the same one the ranking scrapers have: when nflverse changes a
column, only this file should need touching.

Five assets, all through the ranking scrapers' own file cache
(data/rankings_cache/), all plain gzipped CSV read with stdlib csv/gzip —
no pandas, no nflreadpy:

  nflverse_stats_player_{season}   per player-week stat line   24h
  nflverse_stats_team_{season}     per team-week totals        24h
  nflverse_snap_counts_{season}    per player-week snaps       24h
  nflverse_players                 gsis <-> pfr identity       7d
  dynastyprocess_playerids         sleeper -> gsis/pfr         7d

The season files get a 24h max age because in-season they are rebuilt
after each game; the two identity files change only when a player signs
somewhere, so a week is plenty.

**Absent seasons.** Before the season's first game there is no
stats_player_week_{season}.csv.gz at all and the release URL 404s. That is
a normal state, not an error: the 404 is cached as
`{"season": S, "absent": true, "checked_at": ...}` so a second run inside
the max age does not re-request it, and `load_usage` returns None. The
deprecated `player_stats` release family is deliberately not used.

**Kept columns.** The player file has 150 columns; the payload keeps the
fifteen that describe opportunity (see `PlayerWeek`). Rows are filtered to
regular season and to `KEPT_POSITIONS` — a defensive tackle's snap count
is not a fantasy role signal, and team defenses are served by the team
rows. Red-zone usage would need play-by-play (about 19 MB a season) and is
a documented future extension, not a gap in this file.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests

from sleeper_tool.nfl_schedule import normalize_team
from sleeper_tool.rankings.cache import get_or_fetch, load_snapshot

logger = logging.getLogger(__name__)

_RELEASE = "https://github.com/nflverse/nflverse-data/releases/download"
STATS_PLAYER_URL = _RELEASE + "/stats_player/stats_player_week_{season}.csv.gz"
STATS_TEAM_URL = _RELEASE + "/stats_team/stats_team_week_{season}.csv.gz"
SNAP_COUNTS_URL = _RELEASE + "/snap_counts/snap_counts_{season}.csv.gz"
NFLVERSE_PLAYERS_URL = _RELEASE + "/players/players.csv.gz"
DYNASTYPROCESS_PLAYERIDS_URL = "https://github.com/dynastyprocess/data/raw/master/files/db_playerids.csv"

STATS_PLAYER_SOURCE = "nflverse_stats_player_{season}"
STATS_TEAM_SOURCE = "nflverse_stats_team_{season}"
SNAP_COUNTS_SOURCE = "nflverse_snap_counts_{season}"
NFLVERSE_PLAYERS_SOURCE = "nflverse_players"
DYNASTYPROCESS_PLAYERIDS_SOURCE = "dynastyprocess_playerids"

SEASON_MAX_AGE = dt.timedelta(hours=24)
CROSSWALK_MAX_AGE = dt.timedelta(days=7)
# In season the stat files rebuild within a day of each game; a payload
# older than a full week plus slack means we have silently missed a week.
STALE_AFTER = dt.timedelta(days=8)

REGULAR_SEASON = "REG"
KEPT_POSITIONS = frozenset({"QB", "RB", "FB", "WR", "TE", "K"})
_MISSING = {"", "NA", "N/A", "NULL", "NONE"}
_HEADERS = {"User-Agent": "sleeper-dynasty-tool/0.1 (personal use)"}
_HTTP_TIMEOUT = 120


class AssetAbsent(Exception):
    """The asset does not exist yet (HTTP 404) — a state, not a failure."""


@dataclass(frozen=True)
class PlayerWeek:
    gsis_id: str
    week: int
    team: str
    position: str | None
    snaps: int | None
    snap_pct: float | None  # 0-1 fraction as published, None when no snap row
    targets: float
    receptions: float
    rec_yards: float
    air_yards: float
    carries: float
    rush_yards: float
    pass_attempts: float
    target_share: float | None  # nflverse's own share, kept for reference
    air_yards_share: float | None
    name: str | None = None

    @property
    def opportunities(self) -> float:
        return self.targets + self.carries

    @property
    def played(self) -> bool:
        """A row exists and the player did something. A player-week with no
        row at all is a bye or an inactive — never a zero-usage game — and
        never reaches here."""
        if self.snaps is not None and self.snaps > 0:
            return True
        return (self.targets + self.carries + self.pass_attempts + self.receptions) > 0


@dataclass(frozen=True)
class TeamWeek:
    team: str
    week: int
    targets: float
    carries: float
    attempts: float

    @property
    def opportunities(self) -> float:
        return self.targets + self.carries


@dataclass(frozen=True)
class UsageHealth:
    source: str
    fetched_at: dt.datetime | None
    latest_week: int | None
    rows: int
    absent: bool
    stale: bool

    def describe(self) -> str:
        if self.absent:
            return f"{self.source}: no data published yet"
        week = f"through week {self.latest_week}" if self.latest_week else "no games yet"
        age = f", fetched {self.fetched_at:%Y-%m-%d}" if self.fetched_at else ""
        return f"{self.source}: {self.rows} player-weeks {week}{age}{' (stale)' if self.stale else ''}"


@dataclass
class UsageData:
    season: int
    fetched_at: dt.datetime | None
    latest_week: int | None
    player_weeks: list[PlayerWeek]
    team_weeks: list[TeamWeek]
    malformed_rows: int = 0
    _by_player: dict[str, list[PlayerWeek]] = field(default_factory=dict, repr=False)
    _by_team_week: dict[tuple[str, int], TeamWeek] = field(default_factory=dict, repr=False)
    _by_team: dict[str, list[PlayerWeek]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for row in self.player_weeks:
            self._by_player.setdefault(row.gsis_id, []).append(row)
            self._by_team.setdefault(row.team, []).append(row)
        for rows in self._by_player.values():
            rows.sort(key=lambda r: r.week)
        for rows in self._by_team.values():
            rows.sort(key=lambda r: (r.week, r.gsis_id))
        for tw in self.team_weeks:
            self._by_team_week[(tw.team, tw.week)] = tw

    def weeks_for(self, gsis_id: str | None) -> list[PlayerWeek]:
        return list(self._by_player.get(gsis_id or "", ()))

    def team_week(self, team: str | None, week: int) -> TeamWeek | None:
        team = normalize_team(team)
        return self._by_team_week.get((team, week)) if team else None

    def team_player_weeks(self, team: str | None) -> list[PlayerWeek]:
        team = normalize_team(team)
        return list(self._by_team.get(team or "", ()))

    def team_played_weeks(self, team: str | None) -> list[int]:
        team = normalize_team(team)
        return sorted(w for (t, w) in self._by_team_week if t == team)

    def health(self) -> UsageHealth:
        stale = bool(self.fetched_at) and (dt.datetime.now(dt.timezone.utc) - self.fetched_at) > STALE_AFTER
        return UsageHealth(
            source=f"nflverse usage {self.season}",
            fetched_at=self.fetched_at,
            latest_week=self.latest_week,
            rows=len(self.player_weeks),
            absent=False,
            stale=stale,
        )


# -- parsing -----------------------------------------------------------------


def _num(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.upper() in _MISSING:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _num0(value: object) -> float:
    n = _num(value)
    return 0.0 if n is None else n


def _int(value: object) -> int | None:
    n = _num(value)
    return None if n is None else int(n)


def read_csv_rows(data: bytes, *, gzipped: bool) -> list[dict]:
    """Bytes -> list of dicts. Kept public so tests can push gzip bytes
    through exactly the path a real fetch takes."""
    if gzipped:
        data = gzip.decompress(data)
    text = data.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def parse_player_weeks(rows: Iterable[dict], season: int) -> tuple[list[dict], int]:
    """Regular-season, fantasy-position player-weeks as compact dicts.
    Returns (rows, malformed_count); a malformed row is skipped, never fatal."""
    out: list[dict] = []
    malformed = 0
    for raw in rows:
        try:
            if (raw.get("season_type") or "").strip() != REGULAR_SEASON:
                continue
            if int(str(raw["season"]).strip()) != season:
                continue
            position = (raw.get("position") or "").strip().upper()
            if position not in KEPT_POSITIONS:
                continue
            gsis = (raw.get("player_id") or "").strip()
            team = normalize_team(raw.get("team"))
            week = int(str(raw["week"]).strip())
            if not gsis or not team:
                malformed += 1
                continue
            out.append({
                "gsis_id": gsis,
                "week": week,
                "team": team,
                "position": position,
                "name": (raw.get("player_display_name") or "").strip() or None,
                "targets": _num0(raw.get("targets")),
                "receptions": _num0(raw.get("receptions")),
                "rec_yards": _num0(raw.get("receiving_yards")),
                "air_yards": _num0(raw.get("receiving_air_yards")),
                "carries": _num0(raw.get("carries")),
                "rush_yards": _num0(raw.get("rushing_yards")),
                "pass_attempts": _num0(raw.get("attempts")),
                "target_share": _num(raw.get("target_share")),
                "air_yards_share": _num(raw.get("air_yards_share")),
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            malformed += 1
    return out, malformed


def parse_team_weeks(rows: Iterable[dict], season: int) -> tuple[list[dict], int]:
    out: list[dict] = []
    malformed = 0
    for raw in rows:
        try:
            if (raw.get("season_type") or "").strip() != REGULAR_SEASON:
                continue
            if int(str(raw["season"]).strip()) != season:
                continue
            team = normalize_team(raw.get("team"))
            week = int(str(raw["week"]).strip())
            if not team:
                malformed += 1
                continue
            out.append({
                "team": team,
                "week": week,
                "targets": _num0(raw.get("targets")),
                "carries": _num0(raw.get("carries")),
                "attempts": _num0(raw.get("attempts")),
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            malformed += 1
    return out, malformed


def parse_snap_counts(rows: Iterable[dict], season: int) -> tuple[list[dict], int]:
    """Snap counts are keyed by pfr id and carry playoff rounds under their
    own game_type codes (WC/DIV/CON/SB), so REG is filtered explicitly
    rather than by excluding "POST"."""
    out: list[dict] = []
    malformed = 0
    for raw in rows:
        try:
            if (raw.get("game_type") or "").strip() != REGULAR_SEASON:
                continue
            if int(str(raw["season"]).strip()) != season:
                continue
            pfr = (raw.get("pfr_player_id") or "").strip()
            week = int(str(raw["week"]).strip())
            if not pfr:
                malformed += 1
                continue
            out.append({
                "pfr_id": pfr,
                "week": week,
                "team": normalize_team(raw.get("team")),
                "offense_snaps": _int(raw.get("offense_snaps")),
                "offense_pct": _num(raw.get("offense_pct")),
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            malformed += 1
    return out, malformed


def parse_nflverse_players(rows: Iterable[dict]) -> tuple[list[dict], int]:
    out: list[dict] = []
    malformed = 0
    for raw in rows:
        try:
            gsis = (raw.get("gsis_id") or "").strip()
            if not gsis:
                continue
            out.append({
                "gsis_id": gsis,
                "pfr_id": (raw.get("pfr_id") or "").strip() or None,
                "display_name": (raw.get("display_name") or "").strip(),
                "position": (raw.get("position") or "").strip().upper() or None,
                "latest_team": normalize_team(raw.get("latest_team")),
                "status": (raw.get("status") or "").strip().upper() or None,
                "last_season": (raw.get("last_season") or "").strip() or None,
            })
        except (TypeError, ValueError, AttributeError):
            malformed += 1
    return out, malformed


def parse_dynastyprocess_ids(rows: Iterable[dict]) -> tuple[list[dict], int]:
    """Only the id columns are kept. The file's `team` is stale and its
    `position` is only used for reporting, never for matching."""
    out: list[dict] = []
    malformed = 0
    for raw in rows:
        try:
            sleeper_id = (raw.get("sleeper_id") or "").strip()
            if not sleeper_id:
                continue
            out.append({
                "sleeper_id": sleeper_id,
                "gsis_id": (raw.get("gsis_id") or "").strip() or None,
                "pfr_id": (raw.get("pfr_id") or "").strip() or None,
                "name": (raw.get("name") or "").strip() or None,
                "merge_name": (raw.get("merge_name") or "").strip() or None,
                "position": (raw.get("position") or "").strip().upper() or None,
            })
        except (TypeError, ValueError, AttributeError):
            malformed += 1
    return out, malformed


# -- fetching ----------------------------------------------------------------


def _http_get_bytes(url: str) -> bytes:
    resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
    if resp.status_code == 404:
        raise AssetAbsent(url)
    resp.raise_for_status()
    return resp.content


def _absent_payload(season: int | None) -> dict:
    return {"season": season, "absent": True, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def _fetch_payload(url: str, parse, season: int | None, fetch: Callable[[str], bytes]) -> dict:
    """One asset -> a cache payload. A 404 becomes the absent marker so the
    cache remembers "not published yet" for the full max age."""
    try:
        data = fetch(url)
    except AssetAbsent:
        logger.info("nflverse asset not published yet: %s", url)
        return _absent_payload(season)
    rows = read_csv_rows(data, gzipped=url.endswith(".gz"))
    parsed, malformed = parse(rows) if season is None else parse(rows, season)
    payload: dict = {"rows": parsed, "malformed": malformed, "absent": False}
    if season is not None:
        payload["season"] = season
    return payload


def _load_asset(source: str, url: str, parse, *, season: int | None, max_age: dt.timedelta, force: bool, fetch: Callable[[str], bytes]) -> tuple[dict | None, dt.datetime | None]:
    try:
        snapshot = get_or_fetch(source, lambda: _fetch_payload(url, parse, season, fetch), max_age=max_age, force=force)
    except Exception as exc:  # nothing cached to fall back to
        logger.warning("nflverse asset unavailable (%s): %s", source, exc)
        return None, None
    payload = snapshot.payload or {}
    if season is not None and payload.get("season") not in (None, season):
        return None, None  # a cache holding another season is not usable
    return payload, snapshot.fetched_at


def load_crosswalk_rows(*, force: bool = False, fetch: Callable[[str], bytes] | None = None) -> tuple[list[dict], list[dict]]:
    """(dynastyprocess rows, nflverse players rows) — the two files
    `player_ids.build_crosswalk` wants as `ff_rows` and `nfl_rows`. Either
    may come back empty; the crosswalk ladder degrades a rung at a time."""
    fetch = fetch or _http_get_bytes
    ff_payload, _ = _load_asset(DYNASTYPROCESS_PLAYERIDS_SOURCE, DYNASTYPROCESS_PLAYERIDS_URL, parse_dynastyprocess_ids, season=None, max_age=CROSSWALK_MAX_AGE, force=force, fetch=fetch)
    nfl_payload, _ = _load_asset(NFLVERSE_PLAYERS_SOURCE, NFLVERSE_PLAYERS_URL, parse_nflverse_players, season=None, max_age=CROSSWALK_MAX_AGE, force=force, fetch=fetch)
    ff_rows = (ff_payload or {}).get("rows") or []
    nfl_rows = (nfl_payload or {}).get("rows") or []
    return ff_rows, nfl_rows


def usage_from_payloads(season: int, player_rows: list[dict], team_rows: list[dict], snap_rows: list[dict], pfr_to_gsis: dict[str, str], *, fetched_at: dt.datetime | None = None, malformed: int = 0) -> UsageData:
    """Build the typed rows, joining snaps on (gsis, week). A snap row whose
    pfr id we cannot resolve to a gsis id is dropped: it belongs to a
    player the stat rows don't cover (a lineman) or to someone the identity
    file doesn't know."""
    snaps_by_key: dict[tuple[str, int], dict] = {}
    for row in snap_rows:
        gsis = pfr_to_gsis.get(row.get("pfr_id") or "")
        if not gsis:
            continue
        snaps_by_key[(gsis, int(row["week"]))] = row

    player_weeks: list[PlayerWeek] = []
    for row in player_rows:
        try:
            key = (row["gsis_id"], int(row["week"]))
            snap = snaps_by_key.get(key) or {}
            player_weeks.append(PlayerWeek(
                gsis_id=row["gsis_id"],
                week=int(row["week"]),
                team=row["team"],
                position=row.get("position"),
                snaps=snap.get("offense_snaps"),
                snap_pct=snap.get("offense_pct"),
                targets=float(row.get("targets") or 0.0),
                receptions=float(row.get("receptions") or 0.0),
                rec_yards=float(row.get("rec_yards") or 0.0),
                air_yards=float(row.get("air_yards") or 0.0),
                carries=float(row.get("carries") or 0.0),
                rush_yards=float(row.get("rush_yards") or 0.0),
                pass_attempts=float(row.get("pass_attempts") or 0.0),
                target_share=row.get("target_share"),
                air_yards_share=row.get("air_yards_share"),
                name=row.get("name"),
            ))
        except (KeyError, TypeError, ValueError):
            malformed += 1

    team_weeks: list[TeamWeek] = []
    for row in team_rows:
        try:
            team_weeks.append(TeamWeek(
                team=row["team"],
                week=int(row["week"]),
                targets=float(row.get("targets") or 0.0),
                carries=float(row.get("carries") or 0.0),
                attempts=float(row.get("attempts") or 0.0),
            ))
        except (KeyError, TypeError, ValueError):
            malformed += 1

    weeks = [r.week for r in player_weeks if r.played]
    latest_week = max(weeks) if weeks else None
    return UsageData(
        season=season,
        fetched_at=fetched_at,
        latest_week=latest_week,
        player_weeks=player_weeks,
        team_weeks=team_weeks,
        malformed_rows=malformed,
    )


def load_usage(season: int, *, force: bool = False, fetch: Callable[[str], bytes] | None = None) -> UsageData | None:
    """Cached weekly usage for a season, or None when there is none.

    None means one of two things and both are normal: the season has not
    started (the release 404s, and that answer is cached), or every asset
    failed with nothing cached to fall back on. Team totals and snap counts
    degrade independently — without them shares and snap percentages are
    None, but the raw opportunity counts still work.
    """
    fetch = fetch or _http_get_bytes
    player_payload, fetched_at = _load_asset(STATS_PLAYER_SOURCE.format(season=season), STATS_PLAYER_URL.format(season=season), parse_player_weeks, season=season, max_age=SEASON_MAX_AGE, force=force, fetch=fetch)
    if player_payload is None:
        return None
    if player_payload.get("absent"):
        logger.info("No nflverse usage published for %s yet", season)
        return None

    team_payload, _ = _load_asset(STATS_TEAM_SOURCE.format(season=season), STATS_TEAM_URL.format(season=season), parse_team_weeks, season=season, max_age=SEASON_MAX_AGE, force=force, fetch=fetch)
    snap_payload, _ = _load_asset(SNAP_COUNTS_SOURCE.format(season=season), SNAP_COUNTS_URL.format(season=season), parse_snap_counts, season=season, max_age=SEASON_MAX_AGE, force=force, fetch=fetch)
    _, nfl_rows = load_crosswalk_rows(force=force, fetch=fetch)

    team_rows = [] if not team_payload or team_payload.get("absent") else (team_payload.get("rows") or [])
    snap_rows = [] if not snap_payload or snap_payload.get("absent") else (snap_payload.get("rows") or [])
    pfr_to_gsis = {r["pfr_id"]: r["gsis_id"] for r in nfl_rows if r.get("pfr_id") and r.get("gsis_id")}

    malformed = int(player_payload.get("malformed") or 0) + int((team_payload or {}).get("malformed") or 0) + int((snap_payload or {}).get("malformed") or 0)
    return usage_from_payloads(
        season,
        player_payload.get("rows") or [],
        team_rows,
        snap_rows,
        pfr_to_gsis,
        fetched_at=fetched_at,
        malformed=malformed,
    )


def cached_health(season: int) -> UsageHealth:
    """What the cache says about a season without touching the network —
    including "absent", which `load_usage` can only report as None."""
    snapshot = load_snapshot(STATS_PLAYER_SOURCE.format(season=season))
    source = f"nflverse usage {season}"
    if snapshot is None:
        return UsageHealth(source=source, fetched_at=None, latest_week=None, rows=0, absent=False, stale=True)
    payload = snapshot.payload or {}
    rows = payload.get("rows") or []
    weeks = [int(r["week"]) for r in rows if r.get("week") is not None]
    return UsageHealth(
        source=source,
        fetched_at=snapshot.fetched_at,
        latest_week=max(weeks) if weeks else None,
        rows=len(rows),
        absent=bool(payload.get("absent")),
        stale=snapshot.age() > STALE_AFTER,
    )
