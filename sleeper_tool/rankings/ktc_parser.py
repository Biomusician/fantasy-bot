"""KeepTradeCut page parsing — the strategies, and the validation that
decides whether what came back is a board at all.

Kept apart from `ktc.py` (which fetches and caches) so that when KTC moves
its data again only this file and its fixtures need touching, and so every
strategy is testable from a saved page with no network.

KTC has moved its player data twice, and each move broke a single-strategy
parser. The parse is therefore a list of strategies tried in order:

  embedded_json_v2   <script type="application/json" id="ktc-players">
                     — the September 2026 layout, and the only complete one.
  embedded_json_v1   the older `var playersArray = [...]` literal. KTC now
                     writes `var playersArray = JSON.parse(...)` instead, so
                     a parser matching only this finds nothing at all.
  rendered_markup    the visible ranking rows.

The last one is deliberately marked INCOMPLETE and is never accepted as a
snapshot. The page renders one page of rows (50) carrying one format's
value and no TE-premium variants; the other 450 players and seven of the
eight value blocks exist only in the JSON. A 50-row snapshot would not read
as a failure downstream — it would read as a league where 450 rostered
players are suddenly worthless. It is parsed anyway because it answers the
question diagnostics actually need: is this a real rankings page, or a wall?

A parse that yields nothing, or implausibly little, is an explicit failure.
Returning [] quietly was how a layout change could overwrite a good cache
with an empty one and still look like a successful refresh.
"""
from __future__ import annotations

import html as html_lib
import json
import math
import re
from dataclasses import asdict, dataclass, field

EMBEDDED_JSON_V2 = "embedded_json_v2"
EMBEDDED_JSON_V1 = "embedded_json_v1"
RENDERED_MARKUP = "rendered_markup"

# Strategies that can produce the full canonical snapshot. Anything else is
# diagnostic only and must never be cached.
COMPLETE_STRATEGIES = frozenset({EMBEDDED_JSON_V2, EMBEDDED_JSON_V1})

# KTC's board is ~500 rows: 416 ranked players plus ~84 rookie picks as of
# 2026-09, and the pick share swells in the offseason when several draft
# classes are live. So this floor is counted over RANKED PLAYERS only, and
# it is deliberately well under the measured 416 rather than just under it:
# its job is to catch a parse that returned a fraction of the board (three
# players, or the rendered page's fifty), not to fail the week KTC trims a
# few names. `coverage_floor("ktc")` stays the health layer's floor on the
# total row count and is a different question.
MIN_PLAYERS = 300
MEASURED_RANKED_PLAYERS = 416  # 2026-09-19, for whoever revisits the floor
# A dynasty board missing one of these parsed the wrong object, whatever
# its row count.
REQUIRED_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})
# KTC's scale is 0-9999. A board of zeroes is a parse that found the right
# keys inside the wrong objects.
VALUE_MAX = 9999
MIN_NONZERO_VALUE_RATE = 0.90
# Every block a league might actually read. Checking only one of them was a
# hole the width of the product: `superflex` plain is read by a Superflex
# league with no TE premium and by nobody else, while `one_qb` and the six
# TE-premium blocks carry every other league. The tep/tepp/teppp keys are
# also the newest part of KTC's schema, so they are the likeliest to move —
# and `_value` turns a moved key into a silent zero.
VALUE_BLOCKS = (
    "one_qb", "superflex",
    "one_qb_tep", "one_qb_tepp", "one_qb_teppp",
    "superflex_tep", "superflex_tepp", "superflex_teppp",
)
# Two rows for one name happens (a collision); a board that is mostly
# duplicates is one row matched over and over.
MAX_DUPLICATE_RATE = 0.05

# Phrases that mean a challenge or interstitial page was served with a 200.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "captcha-delivery",
    "enable javascript and cookies to continue",
    "attention required",
    "access denied",
)

# The lookbehind keeps `data-id="ktc-players"` and `aria-id=...` out: in
# `data-id` the hyphen is a non-word character, so a plain \b is satisfied
# and the decoy matches. The backreference on the quote is what stops
# `id="ktc-players-old"` matching.
_PLAYERS_SCRIPT_RE = re.compile(
    r"""<script\b[^>]*(?<![-\w])id\s*=\s*(["'])ktc-players\1[^>]*>(.*?)</script>""",
    re.DOTALL | re.IGNORECASE,
)
_PLAYERS_ARRAY_RE = re.compile(r"var playersArray\s*=\s*(\[.*?\]);", re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

_ROW_RE = re.compile(r'<div class="onePlayer">(.*?)(?=<div class="onePlayer">|\Z)', re.DOTALL)
_ROW_RANK_RE = re.compile(r'<div class="rank-number">\s*<p>\s*(\d+)\s*</p>', re.DOTALL)
_ROW_NAME_RE = re.compile(r'<div class="player-name">.*?<a[^>]*>(.*?)</a>', re.DOTALL)
_ROW_TEAM_RE = re.compile(r'<span class="player-team">(.*?)</span>', re.DOTALL)
_ROW_POS_RE = re.compile(r'<p class="position">\s*([A-Z]+)(\d+)?\s*</p>', re.DOTALL)
_ROW_VALUE_RE = re.compile(r'<div class="value">\s*<p>\s*(\d+)\s*</p>', re.DOTALL)


class KTCParseError(RuntimeError):
    """The page was served, but it is not a usable KTC board."""


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


@dataclass(frozen=True)
class KtcParse:
    """What one parse produced, and which strategy produced it."""

    players: list[dict]
    strategy: str
    complete: bool
    notes: list[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return len(self.players)


def page_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    return " ".join(match.group(1).split())[:90] if match else ""


def looks_like_challenge(html: str) -> bool:
    """A bot wall or interstitial served with a 200, which must never be
    mistaken for an empty board."""
    head = html[:4000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _text(raw: dict, key: str) -> str:
    """A string field, or "". A non-string that is merely truthy — a nested
    `team` object, a numeric `position` id — used to reach `.strip()` and
    take down every entry point, including the diagnostics one."""
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def _value(block: dict, key: str) -> KTCValue:
    sub = block.get(key) if key else block
    if not isinstance(sub, dict):
        sub = {}
    try:
        # OverflowError is an ArithmeticError, not a ValueError: int(inf)
        # escaped the old tuple and came out of every entry point.
        return KTCValue(
            value=int(sub.get("value") or 0),
            rank=int(sub.get("rank") or 0),
            positional_rank=int(sub.get("positionalRank") or 0),
        )
    except (TypeError, ValueError, OverflowError):
        return KTCValue(value=0, rank=0, positional_rank=0)


def _player_from_json(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    one_qb, sf = raw.get("oneQBValues"), raw.get("superflexValues")
    if not isinstance(one_qb, dict) or not isinstance(sf, dict):
        return None
    age = raw.get("age")
    usable_age = (
        isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age)
    )
    return asdict(KTCPlayer(
        name=_text(raw, "playerName"),
        position=_text(raw, "position"),
        team=_text(raw, "team") or None,
        age=float(age) if usable_age else None,
        is_rookie=bool(raw.get("rookie", False)),
        one_qb=_value(one_qb, ""),
        superflex=_value(sf, ""),
        one_qb_tep=_value(one_qb, "tep"),
        one_qb_tepp=_value(one_qb, "tepp"),
        one_qb_teppp=_value(one_qb, "teppp"),
        superflex_tep=_value(sf, "tep"),
        superflex_tepp=_value(sf, "tepp"),
        superflex_teppp=_value(sf, "teppp"),
    ))


def _players_from_json_text(raw_json: str, strategy: str) -> KtcParse:
    def _reject(constant: str):
        raise ValueError(f"non-standard JSON literal {constant}")

    try:
        raw_players = json.loads(raw_json, parse_constant=_reject)
    except (json.JSONDecodeError, ValueError) as exc:
        raise KTCParseError(f"KTC {strategy} payload is not valid JSON: {exc}") from exc
    if not isinstance(raw_players, list):
        raise KTCParseError(f"KTC {strategy} payload is {type(raw_players).__name__}, not a list")
    players = [p for p in (_player_from_json(raw) for raw in raw_players) if p is not None]
    notes = []
    if len(players) < len(raw_players):
        notes.append(f"{len(raw_players) - len(players)} of {len(raw_players)} records carried no value blocks")
    return KtcParse(players=players, strategy=strategy, complete=True, notes=notes)


def _from_script_tag(html: str) -> KtcParse | None:
    """Every tag carrying that id, in order.

    Matching only the first one meant a placeholder tag ahead of the real
    one — a template that emits an empty node and hydrates a second, or a
    duplicated partial — took the primary dynasty source down while the page
    was perfectly healthy.
    """
    problems: list[str] = []
    for match in _PLAYERS_SCRIPT_RE.finditer(html):
        body = match.group(2)
        if not body.strip():
            continue
        try:
            parse = _players_from_json_text(body, EMBEDDED_JSON_V2)
        except KTCParseError as exc:
            problems.append(str(exc))
            continue
        if parse.rows:
            return parse
        problems.append(f"a {EMBEDDED_JSON_V2} tag yielded 0 players")
    if problems:
        raise KTCParseError("; ".join(problems))
    return None


def _from_players_array(html: str) -> KtcParse | None:
    match = _PLAYERS_ARRAY_RE.search(html)
    if not match:
        return None
    parse = _players_from_json_text(match.group(1), EMBEDDED_JSON_V1)
    return parse if parse.rows else None


def _from_rendered_markup(html: str) -> KtcParse | None:
    """The visible rows. Always INCOMPLETE — see the module docstring."""
    players: list[dict] = []
    for block in _ROW_RE.findall(html):
        name = _ROW_NAME_RE.search(block)
        value = _ROW_VALUE_RE.search(block)
        position = _ROW_POS_RE.search(block)
        if not (name and value and position):
            continue
        # Rookie-pick rows carry no rank cell. Requiring one dropped them and
        # made the diagnostic row count disagree with what the page shows.
        rank = _ROW_RANK_RE.search(block)
        team = _ROW_TEAM_RE.search(block)
        shown = KTCValue(
            value=int(value.group(1)),
            rank=int(rank.group(1)) if rank else 0,
            positional_rank=int(position.group(2)) if position.group(2) else 0,
        )
        players.append(asdict(KTCPlayer(
            name=html_lib.unescape(re.sub(r"<[^>]+>", "", name.group(1))).strip(),
            position=position.group(1).strip(),
            team=(team.group(1).strip() if team else None) or None,
            age=None,
            is_rookie=False,
            # The rendered board shows ONE format's value and no TE-premium
            # variants. The eight blocks carry the same number because the
            # shape is shared, not because seven of them are known — which
            # is exactly why `complete=False` keeps this out of the cache.
            one_qb=shown, superflex=shown,
            one_qb_tep=shown, one_qb_tepp=shown, one_qb_teppp=shown,
            superflex_tep=shown, superflex_tepp=shown, superflex_teppp=shown,
        )))
    if not players:
        return None
    return KtcParse(
        players=players, strategy=RENDERED_MARKUP, complete=False,
        notes=[f"{len(players)} visible rows, one format, no TE-premium variants — diagnostic only"],
    )


# Ordered: the complete strategies first, the diagnostic one last.
STRATEGIES = (_from_script_tag, _from_players_array, _from_rendered_markup)


def parse_ktc(html: str) -> KtcParse:
    """The first strategy that finds anything. Raises KTCParseError when the
    page carries no board at all, including when it is a wall."""
    if looks_like_challenge(html):
        raise KTCParseError(
            f"KTC served a challenge page, not the rankings ({len(html)} chars, title {page_title(html)!r})"
        )
    failures: list[str] = []
    for strategy in STRATEGIES:
        try:
            parse = strategy(html)
        except KTCParseError as exc:
            failures.append(str(exc))
            continue
        if parse is not None and parse.rows:
            # What the earlier strategies said travels with the result: when
            # the JSON moved and the rendered rows answered instead, the line
            # naming what actually broke is the only useful one.
            return KtcParse(
                players=parse.players, strategy=parse.strategy, complete=parse.complete,
                notes=[*parse.notes, *failures],
            )
        if parse is not None:
            failures.append(f"{parse.strategy} yielded 0 players")
    detail = f"; also: {'; '.join(failures)}" if failures else ""
    raise KTCParseError(
        f"No KTC player data found by any strategy ({len(html)} chars, title {page_title(html)!r}) — "
        f"site layout may have changed{detail}"
    )


def validate_parse(parse: KtcParse, *, min_players: int = MIN_PLAYERS) -> None:
    """Raise KTCParseError unless this parse is a board worth caching.

    Every check here is a way a parse can succeed and still be wrong, and
    none of them may pass quietly: whatever gets through overwrites the last
    good snapshot.
    """
    if not parse.complete:
        raise KTCParseError(
            f"KTC {parse.strategy} cannot produce a full board "
            f"({parse.rows} rows, one format, no TE-premium variants)"
        )
    # Rookie-pick rows (position "RDP") are 84 of today's 500, and that share
    # swells in the offseason when several draft classes are live. Counting
    # them toward a player floor let the floor drift with the calendar and
    # left ~16 players of real headroom instead of 100.
    ranked = [p for p in parse.players if p.get("position") in REQUIRED_POSITIONS]
    if len(ranked) < min_players:
        raise KTCParseError(
            f"KTC {parse.strategy} yielded {len(ranked)} ranked players of {parse.rows} rows, fewer "
            f"than the {min_players} a whole board carries — a parse failure, not a smaller board"
        )
    names = [p["name"] for p in parse.players]
    nameless = sum(1 for n in names if not n)
    if nameless:
        raise KTCParseError(f"KTC {parse.strategy} produced {nameless} nameless rows")
    missing = REQUIRED_POSITIONS - {p["position"] for p in parse.players}
    if missing:
        raise KTCParseError(
            f"KTC {parse.strategy} board has no {'/'.join(sorted(missing))} — parsed the wrong object"
        )
    duplicates = len(names) - len(set(names))
    if duplicates > parse.rows * MAX_DUPLICATE_RATE:
        raise KTCParseError(f"KTC {parse.strategy} board is {duplicates} duplicate names of {parse.rows} rows")
    # Every block, not just one: a league reads whichever matches its format,
    # and a moved sub-key zeroes its block without raising anything.
    for block in VALUE_BLOCKS:
        try:
            values = [p[block]["value"] for p in parse.players]
        except (KeyError, TypeError) as exc:
            raise KTCParseError(f"KTC {parse.strategy} board has no usable {block} values: {exc}") from exc
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > VALUE_MAX for v in values):
            raise KTCParseError(f"KTC {parse.strategy} {block} values fall outside 0-{VALUE_MAX}")
        nonzero = sum(1 for v in values if v > 0)
        if nonzero < parse.rows * MIN_NONZERO_VALUE_RATE:
            raise KTCParseError(
                f"KTC {parse.strategy} board has only {nonzero} of {parse.rows} rows with a "
                f"{block} value above zero"
            )
