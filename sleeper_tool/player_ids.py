"""Crosswalk from Sleeper player ids to the NFL stat-world ids (gsis, pfr).

Sleeper is the roster system of record; nflverse's usage data is keyed by
gsis id and its snap counts by pro-football-reference id. Nothing joins
those three on one field, so this module walks a deliberate ladder and
records *how* each player was matched — a name-based match is weaker
evidence than an id-based one and the report should be able to say so.

The ladder, strongest first:

  1. sleeper_gsis    Sleeper's own `gsis_id` field. Present on only about a
                     fifth of rostered players (a legacy field they stopped
                     filling) and some values carry a leading space, so it
                     is stripped before use. When it exists it is right.
  2. dynastyprocess  DynastyProcess's db_playerids.csv keyed by sleeper_id.
                     Covers ~95% of rostered players and carries pfr_id too.
                     Its `team` column is stale and is never read.
  3. nflverse_name   nflverse players.csv on (normalized name, position,
                     team), restricted to status ACT. Three fields must
                     agree; name alone is never enough, because "Michael
                     Carter" and "Josh Allen" are each two real players.
  4. unmatched /     Surfaced in the report, never guessed at. An ambiguous
     ambiguous       name+position+team hit (two ACT players) keeps its
                     candidates so a human can see what happened.

Team defenses are their own case: Sleeper's player_id for a DEF *is* the
team abbreviation ("HOU"), there is no such person in nflverse, and the
usage that matters for a defense lives in the team rows anyway. They get
source "def_team" and no gsis id.

Deterministic: input order never changes the result, and the ambiguity
candidate lists are sorted. One WARNING summarises what didn't match —
never one line per player.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from sleeper_tool.name_matching import normalize_name
from sleeper_tool.nfl_schedule import normalize_team

logger = logging.getLogger(__name__)

SOURCE_SLEEPER_GSIS = "sleeper_gsis"
SOURCE_DYNASTYPROCESS = "dynastyprocess"
SOURCE_NFLVERSE_NAME = "nflverse_name"
SOURCE_DEF_TEAM = "def_team"

ACTIVE_STATUS = "ACT"  # nflverse players.csv status for a player on a roster now
DEF_POSITION = "DEF"


@dataclass(frozen=True)
class PlayerIds:
    sleeper_id: str
    gsis_id: str | None
    pfr_id: str | None
    source: str
    candidates: tuple[str, ...] = ()  # only populated for an ambiguous name hit
    note: str | None = None

    @property
    def matched(self) -> bool:
        return self.gsis_id is not None or self.source == SOURCE_DEF_TEAM


@dataclass
class CrosswalkReport:
    matched_by_source: Counter = field(default_factory=Counter)
    unmatched: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.matched_by_source.values()) + len(self.unmatched)

    def describe(self) -> str:
        by_source = ", ".join(f"{src} {n}" for src, n in sorted(self.matched_by_source.items()))
        return (
            f"{self.total - len(self.unmatched)}/{self.total} matched ({by_source or 'none'}); "
            f"{len(self.unmatched)} unmatched, {len(self.ambiguous)} ambiguous"
        )


def _clean_id(value: object) -> str | None:
    """Sleeper's gsis_id values sometimes carry a leading space, and empty
    string / "NA" mean absent in the CSV sources."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL"}:
        return None
    return text


def _sleeper_name(player: dict) -> str:
    full = player.get("full_name") or ""
    if not full:
        first = player.get("first_name") or ""
        last = player.get("last_name") or ""
        full = f"{first} {last}".strip()
    return full


def _sleeper_position(player: dict) -> str | None:
    pos = player.get("position")
    if not pos:
        fantasy = player.get("fantasy_positions") or []
        pos = fantasy[0] if fantasy else None
    return str(pos).upper() if pos else None


def _index_dynastyprocess(ff_rows: list[dict]) -> dict[str, dict]:
    """sleeper_id -> row. Later rows win; the file has one row per player."""
    index: dict[str, dict] = {}
    for row in ff_rows:
        sleeper_id = _clean_id(row.get("sleeper_id"))
        if sleeper_id:
            index[sleeper_id] = row
    return index


def _index_nflverse(nfl_rows: list[dict]) -> tuple[dict[tuple[str, str, str], list[dict]], dict[str, str]]:
    """Returns the ACT (name, position, team) index and a gsis -> pfr map.

    The pfr map is built from every row, not just ACT ones: a player who
    retired mid-season still has snap counts to join.
    """
    by_identity: dict[tuple[str, str, str], list[dict]] = {}
    gsis_to_pfr: dict[str, str] = {}
    for row in nfl_rows:
        gsis = _clean_id(row.get("gsis_id"))
        pfr = _clean_id(row.get("pfr_id"))
        if gsis and pfr:
            gsis_to_pfr[gsis] = pfr
        if not gsis or (row.get("status") or "").strip().upper() != ACTIVE_STATUS:
            continue
        name = normalize_name(row.get("display_name") or "")
        position = (row.get("position") or "").strip().upper()
        team = normalize_team(row.get("latest_team"))
        if not name or not position or not team:
            continue
        by_identity.setdefault((name, position, team), []).append(row)
    return by_identity, gsis_to_pfr


def build_crosswalk(
    players: dict[str, dict],
    *,
    ff_rows: list[dict],
    nfl_rows: list[dict],
    only_ids: set[str] | None = None,
) -> tuple[dict[str, PlayerIds], CrosswalkReport]:
    """Map Sleeper player ids to gsis/pfr ids via the documented ladder.

    `players` is Storage.get_all_players() (sleeper_id -> Sleeper record),
    `ff_rows` DynastyProcess db_playerids rows, `nfl_rows` nflverse
    players.csv rows. `only_ids` restricts the work to the players that
    matter (everyone rostered across the leagues) — the full Sleeper cache
    is ~12k rows of which most are practice-squad linemen.
    """
    dp_by_sleeper = _index_dynastyprocess(ff_rows)
    nfl_by_identity, gsis_to_pfr = _index_nflverse(nfl_rows)

    wanted = sorted(only_ids) if only_ids is not None else sorted(players)
    out: dict[str, PlayerIds] = {}
    report = CrosswalkReport()

    for sleeper_id in wanted:
        player = players.get(sleeper_id)
        if player is None:
            report.unmatched.append({"sleeper_id": sleeper_id, "name": None, "position": None, "team": None, "reason": "not in Sleeper player cache"})
            continue

        name = _sleeper_name(player)
        position = _sleeper_position(player)
        team = normalize_team(player.get("team"))

        if position == DEF_POSITION:
            out[sleeper_id] = PlayerIds(
                sleeper_id=sleeper_id,
                gsis_id=None,
                pfr_id=None,
                source=SOURCE_DEF_TEAM,
                note=f"team defense {normalize_team(sleeper_id) or sleeper_id}",
            )
            report.matched_by_source[SOURCE_DEF_TEAM] += 1
            continue

        gsis = _clean_id(player.get("gsis_id"))
        source = SOURCE_SLEEPER_GSIS if gsis else None
        pfr = None

        if gsis is None:
            dp_row = dp_by_sleeper.get(sleeper_id)
            if dp_row is not None:
                gsis = _clean_id(dp_row.get("gsis_id"))
                pfr = _clean_id(dp_row.get("pfr_id"))
                if gsis:
                    source = SOURCE_DYNASTYPROCESS
                else:
                    pfr = None  # a pfr id with no gsis id can't reach the usage rows

        if gsis is None and name and position and team:
            hits = nfl_by_identity.get((normalize_name(name), position, team), [])
            distinct = sorted({_clean_id(h.get("gsis_id")) or "" for h in hits})
            if len(distinct) == 1:
                gsis = distinct[0]
                source = SOURCE_NFLVERSE_NAME
            elif len(distinct) > 1:
                report.ambiguous.append({
                    "sleeper_id": sleeper_id,
                    "name": name,
                    "position": position,
                    "team": team,
                    "candidates": distinct,
                })
                out[sleeper_id] = PlayerIds(
                    sleeper_id=sleeper_id,
                    gsis_id=None,
                    pfr_id=None,
                    source="ambiguous",
                    candidates=tuple(distinct),
                    note=f"{len(distinct)} active nflverse players share name+position+team",
                )
                report.unmatched.append({"sleeper_id": sleeper_id, "name": name, "position": position, "team": team, "reason": "ambiguous"})
                continue

        if gsis is None:
            report.unmatched.append({"sleeper_id": sleeper_id, "name": name, "position": position, "team": team, "reason": "no id in any source"})
            continue

        if pfr is None:
            pfr = gsis_to_pfr.get(gsis) or _clean_id((dp_by_sleeper.get(sleeper_id) or {}).get("pfr_id"))
        out[sleeper_id] = PlayerIds(sleeper_id=sleeper_id, gsis_id=gsis, pfr_id=pfr, source=source or SOURCE_NFLVERSE_NAME)
        report.matched_by_source[source or SOURCE_NFLVERSE_NAME] += 1

    if report.unmatched:
        logger.warning("Player id crosswalk: %s", report.describe())
    return out, report


def by_gsis(crosswalk: dict[str, PlayerIds]) -> dict[str, str]:
    """gsis_id -> sleeper_id, for going back the other way. On the (rare)
    duplicate the lower sleeper_id wins, so the mapping is stable."""
    out: dict[str, str] = {}
    for sleeper_id in sorted(crosswalk):
        gsis = crosswalk[sleeper_id].gsis_id
        if gsis and gsis not in out:
            out[gsis] = sleeper_id
    return out
