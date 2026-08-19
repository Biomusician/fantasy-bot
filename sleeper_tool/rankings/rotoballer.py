"""RotoBaller rankings scraper.

RotoBaller's rankings page is backed by a public WordPress REST endpoint
(`/wp-json/rb/v1/rankings`) that returns real point projections per player,
including a TE-premium projection (`te_prem_points`) nobody else exposes
directly. It's genuinely useful — but only for **redraft** signal.

IMPORTANT caveat found during research (2026-08-18): the endpoint accepts a
`league` query param that looks like it should switch between Overall/
Dynasty/Superflex rankings, but `league=Dynasty` and `league=Superflex`
returned byte-identical data to `league=Overall` in testing. That param does
not appear to be wired to anything real server-side. We therefore only ever
request `league=Overall` here and never treat this source as dynasty-aware —
it feeds the redraft/points-projection/trending signal only, alongside
FantasyPros for dynasty leagues' buy-low/sell-high context.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import requests

from sleeper_tool.name_matching import build_name_index
from sleeper_tool.rankings.cache import RankingSnapshot, get_or_fetch

ROTOBALLER_SPREADSHEETS: dict[str, str] = {
    "full_ppr": "ppr",
    "half_ppr": "half-ppr",
    "standard": "standard",
}

DEFAULT_MAX_AGE = dt.timedelta(hours=20)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_API_URL = "https://www.rotoballer.com/wp-json/rb/v1/rankings"


class RotoBallerFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class RBPlayer:
    name: str
    position: str
    team: str | None
    bye_week: int | None
    rank: int
    tier: int | None
    trend: str | None
    proj_points_ppr: float | None
    proj_points_standard: float | None
    proj_points_te_premium: float | None


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_row(raw: dict) -> RBPlayer | None:
    player = raw.get("player") or {}
    name = player.get("name")
    if not name:
        return None
    projections = player.get("projections") or {}
    return RBPlayer(
        name=name,
        position=raw.get("position", ""),
        team=raw.get("team") or None,
        bye_week=_to_int(raw.get("bye_week")),
        rank=raw.get("rank", 0),
        tier=raw.get("tier"),
        trend=raw.get("literal_trend"),
        proj_points_ppr=_to_float(projections.get("ppr_points")),
        proj_points_standard=_to_float(projections.get("non_ppr_points")),
        proj_points_te_premium=_to_float(projections.get("te_prem_points")),
    )


def fetch_rb_json(spreadsheet_key: str, per_page: int = 600) -> dict:
    if spreadsheet_key not in ROTOBALLER_SPREADSHEETS:
        raise ValueError(
            f"Unknown RotoBaller spreadsheet key {spreadsheet_key!r}; known: {list(ROTOBALLER_SPREADSHEETS)}"
        )
    spreadsheet = ROTOBALLER_SPREADSHEETS[spreadsheet_key]
    params = {"league": "Overall", "perPage": per_page, "spreadsheet": spreadsheet}
    resp = requests.get(_API_URL, params=params, headers=_BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_rb_players(data: dict) -> list[dict]:
    raw_rows = data.get("data", [])
    players = []
    for raw in raw_rows:
        parsed = _parse_row(raw)
        if parsed is not None:
            players.append(asdict(parsed))
    if not players:
        raise RotoBallerFetchError("Parsed 0 players from RotoBaller — API shape may have changed")
    return players


def _fetcher(spreadsheet_key: str):
    def _fetch() -> list[dict]:
        return parse_rb_players(fetch_rb_json(spreadsheet_key))

    return _fetch


def get_rb_rankings(
    spreadsheet_key: str, *, force: bool = False, max_age: dt.timedelta = DEFAULT_MAX_AGE
) -> RankingSnapshot:
    return get_or_fetch(f"rotoballer_{spreadsheet_key}", _fetcher(spreadsheet_key), max_age=max_age, force=force)


def index_by_name(snapshot: RankingSnapshot) -> dict[str, dict]:
    return build_name_index(snapshot.payload, name_key="name")
