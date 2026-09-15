"""KeepTradeCut dynasty trade value scraper.

KTC's dynasty-rankings page embeds the full player dataset in the HTML — no
headless browser needed, just pull the page and regex out the JSON. Since
September 2026 it lives in a `<script type="application/json" id="ktc-players">`
tag (the page's JS then does `playersArray = JSON.parse(...)` on it); before
that it was an inline `var playersArray = [...]` literal. Both shapes carry
the same per-player records, so we try the script tag first and fall back to
the literal in case KTC reverts. Each player carries separate 1QB and Superflex values,
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
from sleeper_tool.rankings.freshness import ceiling_for

KTC_URL = "https://keeptradecut.com/dynasty-rankings"
DEFAULT_MAX_AGE = dt.timedelta(hours=20)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_PLAYERS_SCRIPT_RE = re.compile(
    r"""<script\b[^>]*\bid=["']ktc-players["'][^>]*>(.*?)</script>""", re.DOTALL | re.IGNORECASE
)
_PLAYERS_ARRAY_RE = re.compile(r"var playersArray = (\[.*?\]);", re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


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


def _extract_players_json(html: str) -> tuple[str, str]:
    """(label, raw JSON text) for whichever embedding the page uses."""
    match = _PLAYERS_SCRIPT_RE.search(html)
    if match and match.group(1).strip():
        return "ktc-players script tag", match.group(1)
    match = _PLAYERS_ARRAY_RE.search(html)
    if match:
        return "playersArray literal", match.group(1)
    # Say what was actually served so a bot wall or a redesign is
    # distinguishable from the log line alone.
    title = _TITLE_RE.search(html)
    title_text = " ".join(title.group(1).split())[:80] if title else "no <title>"
    raise KTCFetchError(
        "Could not find player data in KTC page (neither the ktc-players script tag nor "
        f"var playersArray; served {len(html)} chars, title {title_text!r}) — "
        "site layout may have changed"
    )


def parse_ktc_players(html: str) -> list[dict]:
    label, raw_json = _extract_players_json(html)
    try:
        raw_players = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise KTCFetchError(f"Failed to parse KTC {label} JSON: {exc}") from exc
    if not isinstance(raw_players, list):
        raise KTCFetchError(f"KTC {label} is not a JSON array — site layout may have changed")

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
    return get_or_fetch(
        "ktc_dynasty", _fetch_and_parse, max_age=max_age, force=force, ceiling=ceiling_for("ktc")
    )


# The last (snapshot, index) pair built. A report run holds exactly one KTC
# snapshot but rebuilds this ~500-key index a hundred times over —
# draft_picks.value_owned_picks calls it once per roster, once per
# classify_team_status. Keyed on object identity (`is`), and the snapshot
# itself is held so its id can't be recycled onto a different object; a
# re-fetch produces a new snapshot and rebuilds.
_last_index: tuple[RankingSnapshot, dict[str, dict]] | None = None


def index_by_name(snapshot: RankingSnapshot) -> dict[str, dict]:
    """Normalized-name lookup -> KTC player dict. Memoized per snapshot
    object; callers must treat the result as read-only."""
    global _last_index
    if _last_index is not None and _last_index[0] is snapshot:
        return _last_index[1]
    index = build_name_index(snapshot.payload, name_key="name")
    _last_index = (snapshot, index)
    return index
