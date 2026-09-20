"""KeepTradeCut dynasty trade value fetch and cache.

KTC's dynasty-rankings page embeds the full player dataset in the HTML — no
headless browser needed. Each player carries separate 1QB and Superflex
values plus three TE-premium variants (tep/tepp/teppp = +0.5/+1/+1.5 per
reception to TEs) for each, which is exactly the axis these leagues vary
on, so this is the primary dynasty valuation source.

Where the dataset lives in the page has changed twice; `ktc_parser.py` owns
the strategies and the validation, and this module owns the request, the
cache and the name index. A parse that does not survive validation never
reaches the cache, so a layout change degrades to "serving yesterday's
board" instead of "overwriting it with nothing".
"""
from __future__ import annotations

import datetime as dt

import requests

from sleeper_tool.name_matching import build_name_index
from sleeper_tool.rankings.cache import RankingSnapshot, get_or_fetch
from sleeper_tool.rankings.freshness import ceiling_for
from sleeper_tool.rankings.ktc_parser import KTCParseError, KtcParse, parse_ktc, validate_parse

KTC_URL = "https://keeptradecut.com/dynasty-rankings"
DEFAULT_MAX_AGE = dt.timedelta(hours=20)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


class KTCFetchError(RuntimeError):
    pass


def fetch_ktc_html() -> str:
    resp = requests.get(KTC_URL, headers=_BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_ktc_players(html: str) -> list[dict]:
    """The validated player list, or KTCFetchError. The single entry point
    for "turn a KTC page into a board"; `ktc_parser.parse_ktc` is the
    unvalidated view of the same page, for diagnostics."""
    try:
        parse = parse_ktc(html)
        validate_parse(parse)
    except KTCParseError as exc:
        raise KTCFetchError(str(exc)) from exc
    return parse.players


def parse_for_diagnostics(html: str) -> tuple[KtcParse | None, str | None]:
    """(parse, failure reason). Never raises — the diagnostics command wants
    to report a bad page, not die on it."""
    try:
        return parse_ktc(html), None
    except Exception as exc:  # the command exists to explain a bad page, not die on one
        return None, f"{type(exc).__name__}: {exc}"


def _fetch_and_parse() -> list[dict]:
    return parse_ktc_players(fetch_ktc_html())


def valid_ktc_payload(payload, previous=None) -> bool | str:
    """Cache-write gate: True, or what is wrong with this payload.

    `get_or_fetch` calls this before replacing a good snapshot, so a parse
    that slips past the fetch path (a hand-edited cache, a future caller)
    still cannot install a board that downstream would read as "most of the
    league is worthless". The reason is returned rather than swallowed: it
    is the line that says whether anyone needs to go and look.
    """
    if not isinstance(payload, list):
        return f"KTC payload is {type(payload).__name__}, not a list of players"
    try:
        validate_parse(KtcParse(players=payload, strategy="refresh", complete=True))
    except KTCParseError as exc:
        return str(exc)
    except (KeyError, TypeError) as exc:
        return f"KTC payload is not shaped like a board: {type(exc).__name__}: {exc}"
    return True


def get_ktc_rankings(*, force: bool = False, max_age: dt.timedelta = DEFAULT_MAX_AGE) -> RankingSnapshot:
    return get_or_fetch(
        "ktc_dynasty",
        _fetch_and_parse,
        max_age=max_age,
        force=force,
        ceiling=ceiling_for("ktc"),
        validate=valid_ktc_payload,
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
