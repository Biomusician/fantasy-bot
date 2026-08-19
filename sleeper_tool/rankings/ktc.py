"""KeepTradeCut dynasty trade value scraper.

KTC's dynasty-rankings page embeds the full player dataset as a JS variable
(`playersArray`) — no headless browser needed, just pull the page and
regex out the JSON. Each player carries separate 1QB and Superflex values,
plus three TE-premium variants (tep/tepp/teppp = +0.5/+1/+1.5 per reception
to TEs) for each. That's exactly the axis our leagues vary on, so this is
the primary dynasty valuation source.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass

import requests

from sleeper_tool.name_matching import build_name_index
from sleeper_tool.rankings.cache import RankingSnapshot, get_or_fetch

KTC_URL = "https://keeptradecut.com/dynasty-rankings"
DEFAULT_MAX_AGE = dt.timedelta(hours=20)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_PLAYERS_ARRAY_RE = re.compile(r"var playersArray = (\[.*?\]);", re.DOTALL)


class KTCFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class KTCValue:
    value: int
    rank: int
    positional_rank: int


@dataclass(frozen=True)
class KTCPlayer:
    name: str
    position: str
    team: str | None
    age: float | None
    is_rookie: bool
    one_qb: KTCValue
    superflex: KTCValue
    one_qb_tep: KTCValue
    one_qb_tepp: KTCValue
    one_qb_teppp: KTCValue
    superflex_tep: KTCValue
    superflex_tepp: KTCValue
    superflex_teppp: KTCValue


def _value(block: dict, key: str) -> KTCValue:
    sub = block.get(key) if key else block
    return KTCValue(
        value=sub.get("value", 0),
        rank=sub.get("rank", 0),
        positional_rank=sub.get("positionalRank", 0),
    )


def _parse_player(raw: dict) -> KTCPlayer | None:
    one_qb = raw.get("oneQBValues")
    sf = raw.get("superflexValues")
    if not one_qb or not sf:
        return None
    return KTCPlayer(
        name=raw.get("playerName", ""),
        position=raw.get("position", ""),
        team=raw.get("team") or None,
        age=raw.get("age"),
        is_rookie=bool(raw.get("rookie", False)),
        one_qb=_value(one_qb, ""),
        superflex=_value(sf, ""),
        one_qb_tep=_value(one_qb, "tep"),
        one_qb_tepp=_value(one_qb, "tepp"),
        one_qb_teppp=_value(one_qb, "teppp"),
        superflex_tep=_value(sf, "tep"),
        superflex_tepp=_value(sf, "tepp"),
        superflex_teppp=_value(sf, "teppp"),
    )


def fetch_ktc_html() -> str:
    resp = requests.get(KTC_URL, headers=_BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_ktc_players(html: str) -> list[dict]:
    match = _PLAYERS_ARRAY_RE.search(html)
    if not match:
        raise KTCFetchError("Could not find playersArray in KTC page — site layout may have changed")
    try:
        raw_players = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise KTCFetchError(f"Failed to parse KTC playersArray JSON: {exc}") from exc

    players = []
    for raw in raw_players:
        parsed = _parse_player(raw)
        if parsed is not None:
            players.append(asdict(parsed))
    if not players:
        raise KTCFetchError("Parsed 0 players from KTC — site layout may have changed")
    return players


def _fetch_and_parse() -> list[dict]:
    return parse_ktc_players(fetch_ktc_html())


def get_ktc_rankings(*, force: bool = False, max_age: dt.timedelta = DEFAULT_MAX_AGE) -> RankingSnapshot:
    return get_or_fetch("ktc_dynasty", _fetch_and_parse, max_age=max_age, force=force)


def index_by_name(snapshot: RankingSnapshot) -> dict[str, dict]:
    """Normalized-name lookup -> KTC player dict."""
    return build_name_index(snapshot.payload, name_key="name")
