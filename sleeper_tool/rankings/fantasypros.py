"""FantasyPros expert-consensus rankings (ECR) scraper.

Every rankings page on FantasyPros embeds the same `ecrData` JS variable
with a clean player list — no headless browser needed. We use this for
dynasty consensus rank (cross-checking KTC) and for redraft/ROS consensus
rank (the primary signal for the non-dynasty leagues and for buy-low/
sell-high "is expert opinion moving on this guy" context).

Confirmed-live page slugs as of 2026-08-18 (others 302-redirect to a
generic cheatsheet page, meaning they don't exist):
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass

import requests

from sleeper_tool.name_matching import build_name_index
from sleeper_tool.rankings.cache import RankingSnapshot, get_or_fetch

FANTASYPROS_PAGES: dict[str, str] = {
    "redraft_full_ppr": "ppr-cheatsheets",
    "redraft_half_ppr": "half-point-ppr-cheatsheets",
    "redraft_superflex": "superflex-cheatsheets",
    "ros_full_ppr": "ros-ppr-overall",
    "ros_half_ppr": "ros-half-point-ppr-overall",
    "dynasty_1qb": "dynasty-overall",
    "dynasty_superflex": "dynasty-superflex",
}

DEFAULT_MAX_AGE = dt.timedelta(hours=20)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_ECR_DATA_RE = re.compile(r"var ecrData = (\{.*?\});", re.DOTALL)


class FantasyProsFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FPPlayer:
    name: str
    position: str
    team: str | None
    bye_week: int | None
    rank_ecr: int
    pos_rank: str | None
    owned_avg: float | None
    rank_std: float | None  # spread among the 100+ experts behind this ECR number
    # Expert dispersion, from the same ecrData blob (confirmed present
    # 2026-09-02: rank_min/rank_max/rank_ave/tier/player_ecr_delta). Older
    # cached snapshots predate these fields; readers must treat None as
    # "not captured yet", not as zero spread.
    rank_min: int | None = None  # best (lowest) overall rank any expert gave
    rank_max: int | None = None  # worst
    rank_ave: float | None = None
    tier: int | None = None
    ecr_delta: float | None = None  # FantasyPros' own week-over-week ECR movement


def _num(raw: dict, key: str, cast):
    try:
        value = raw.get(key)
        return cast(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _parse_player(raw: dict) -> FPPlayer | None:
    name = raw.get("player_name")
    if not name:
        return None
    return FPPlayer(
        name=name,
        position=raw.get("player_position_id", ""),
        team=raw.get("player_team_id") or None,
        bye_week=_num(raw, "player_bye_week", int),
        rank_ecr=raw.get("rank_ecr", 0),
        pos_rank=raw.get("pos_rank"),
        owned_avg=raw.get("player_owned_avg"),
        rank_std=_num(raw, "rank_std", float),
        rank_min=_num(raw, "rank_min", lambda v: int(float(v))),
        rank_max=_num(raw, "rank_max", lambda v: int(float(v))),
        rank_ave=_num(raw, "rank_ave", float),
        tier=_num(raw, "tier", lambda v: int(float(v))),
        ecr_delta=_num(raw, "player_ecr_delta", float),
    )


def fetch_fp_html(page_key: str) -> str:
    if page_key not in FANTASYPROS_PAGES:
        raise ValueError(f"Unknown FantasyPros page key {page_key!r}; known: {list(FANTASYPROS_PAGES)}")
    slug = FANTASYPROS_PAGES[page_key]
    url = f"https://www.fantasypros.com/nfl/rankings/{slug}.php"
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30, allow_redirects=False)
    if resp.status_code in (301, 302):
        raise FantasyProsFetchError(
            f"{url} redirected (page likely renamed/removed) — update FANTASYPROS_PAGES"
        )
    resp.raise_for_status()
    return resp.text


def parse_fp_players(html: str) -> list[dict]:
    match = _ECR_DATA_RE.search(html)
    if not match:
        raise FantasyProsFetchError("Could not find ecrData in FantasyPros page — site layout may have changed")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise FantasyProsFetchError(f"Failed to parse FantasyPros ecrData JSON: {exc}") from exc

    raw_players = data.get("players", [])
    players = []
    for raw in raw_players:
        parsed = _parse_player(raw)
        if parsed is not None:
            players.append(asdict(parsed))
    if not players:
        raise FantasyProsFetchError("Parsed 0 players from FantasyPros — site layout may have changed")
    return players


def _fetcher(page_key: str):
    def _fetch() -> list[dict]:
        return parse_fp_players(fetch_fp_html(page_key))

    return _fetch


def get_fp_rankings(
    page_key: str, *, force: bool = False, max_age: dt.timedelta = DEFAULT_MAX_AGE
) -> RankingSnapshot:
    return get_or_fetch(f"fantasypros_{page_key}", _fetcher(page_key), max_age=max_age, force=force)


def index_by_name(snapshot: RankingSnapshot) -> dict[str, dict]:
    return build_name_index(snapshot.payload, name_key="name")
