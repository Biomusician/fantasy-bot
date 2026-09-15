"""Match names published by an external waiver source to Sleeper player ids.

The Fantasy Footballers waiver board, RotoBaller's waiver board and Yahoo's
Boone rankings all publish a name, a team and sometimes a position — never a
Sleeper id. Each of them needs the same join, so it lives here once instead of
being re-derived (slightly differently) in every source module.

The join is deliberately conservative: a row that can't be pinned to exactly
one Sleeper player is reported as unmatched or ambiguous rather than guessed,
because a wrong id silently attaches one player's ranking to another.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from sleeper_tool.name_matching import normalize_name
from sleeper_tool.sleeper_positions import (
    FANTASY_POSITIONS,
    POSITION_ALIASES,
    fantasy_positions as _fantasy_positions,
    normalize_position,
)

# Sources disagree on a handful of team codes; Sleeper uses the right-hand side.
TEAM_CODE_ALIASES = {"JAC": "JAX", "LA": "LAR", "WSH": "WAS"}

# Full team name -> Sleeper team code. Sleeper's DEF player ids are the codes.
TEAM_FULL_NAMES = {
    "Arizona Cardinals": "ARI",
    "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR",
    "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN",
    "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN",
    "Detroit Lions": "DET",
    "Green Bay Packers": "GB",
    "Houston Texans": "HOU",
    "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV",
    "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR",
    "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN",
    "New England Patriots": "NE",
    "New Orleans Saints": "NO",
    "New York Giants": "NYG",
    "New York Jets": "NYJ",
    "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA",
    "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}
_TEAM_CODES = frozenset(TEAM_FULL_NAMES.values())
_FULL_NAME_TO_CODE = {normalize_name(n): c for n, c in TEAM_FULL_NAMES.items()}
# "Broncos D/ST" is as common as "Denver D/ST"; both the nickname (last word)
# and the city part are unique across the league except the two LA and two NY
# cities, which are left out rather than guessed.
_NICKNAME_TO_CODE = {normalize_name(n).split()[-1]: c for n, c in TEAM_FULL_NAMES.items()}


def _city(full_name: str) -> str:
    return " ".join(normalize_name(full_name).split()[:-1])


_CITY_COUNTS = {}
for _full_name in TEAM_FULL_NAMES:
    _CITY_COUNTS[_city(_full_name)] = _CITY_COUNTS.get(_city(_full_name), 0) + 1
_CITY_TO_CODE = {_city(n): c for n, c in TEAM_FULL_NAMES.items() if _CITY_COUNTS[_city(n)] == 1}

_DEFENSE_SUFFIX_RE = re.compile(r"\s*(?:d/st|dst|defense)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class SourceName:
    name: str
    team: str | None = None
    position: str | None = None


@dataclass
class MatchResult:
    matched: dict[int, str] = field(default_factory=dict)
    unmatched: list[SourceName] = field(default_factory=list)
    ambiguous: list[SourceName] = field(default_factory=list)
    team_mismatches: list[tuple[SourceName, str, str | None]] = field(default_factory=list)

    def describe(self) -> str:
        total = len(self.matched) + len(self.unmatched) + len(self.ambiguous)
        parts = [f"{len(self.matched)}/{total} matched"]
        if self.unmatched:
            names = ", ".join(r.name for r in self.unmatched)
            parts.append(f"{len(self.unmatched)} unmatched ({names})")
        if self.ambiguous:
            names = ", ".join(r.name for r in self.ambiguous)
            parts.append(f"{len(self.ambiguous)} ambiguous ({names})")
        if self.team_mismatches:
            noun = "team mismatch" if len(self.team_mismatches) == 1 else "team mismatches"
            details = ", ".join(
                f"{row.name}: CSV {row.team}, Sleeper {sleeper_team or 'FA'}"
                for row, _pid, sleeper_team in self.team_mismatches
            )
            parts.append(f"{len(self.team_mismatches)} {noun} ({details})")
        return "; ".join(parts)


def normalize_team(code: str | None) -> str | None:
    """Upper-case, alias-resolved team code; None for blank or free agent."""
    if not code:
        return None
    cleaned = code.strip().upper()
    if not cleaned or cleaned == "FA":
        return None
    return TEAM_CODE_ALIASES.get(cleaned, cleaned)


def _sleeper_name(player: dict) -> str:
    # Same rule as roster_analysis.player_name; not imported from there so the
    # rankings package stays free of the analysis layer's import graph.
    return player.get("full_name") or " ".join(
        filter(None, [player.get("first_name"), player.get("last_name")])
    )


def build_sleeper_name_index(all_players: dict[str, dict]) -> dict[str, list[str]]:
    """Normalized full name -> sorted player_ids, fantasy positions only.

    Sorted so a later tie-break never depends on the dict order Sleeper
    happened to return.
    """
    index: dict[str, list[str]] = {}
    for pid, player in all_players.items():
        if not _fantasy_positions(player):
            continue
        key = normalize_name(_sleeper_name(player))
        if key:
            index.setdefault(key, []).append(pid)
    for pids in index.values():
        pids.sort()
    return index


def _team_code_from_text(text: str) -> str | None:
    stripped = text.strip()
    code = normalize_team(stripped)
    if code in _TEAM_CODES:
        return code
    key = normalize_name(stripped)
    return _FULL_NAME_TO_CODE.get(key) or _NICKNAME_TO_CODE.get(key) or _CITY_TO_CODE.get(key)


def _defense_player_id(row: SourceName, all_players: dict[str, dict]) -> str | None | bool:
    """Resolve a team-defense row to Sleeper's DEF id (the team code).

    Returns False when the row isn't a defense at all, None when it is one but
    no team could be identified, else the player id.
    """
    position = normalize_position(row.position)
    has_suffix = bool(_DEFENSE_SUFFIX_RE.search(row.name))
    full_name_code = _FULL_NAME_TO_CODE.get(normalize_name(row.name))
    if position != "DEF" and not has_suffix and not full_name_code:
        return False
    # A full team name only counts as a defense when nothing says otherwise.
    if not has_suffix and position not in (None, "DEF"):
        return False

    base = _DEFENSE_SUFFIX_RE.sub("", row.name)
    code = _team_code_from_text(base) if base.strip() else None
    if code is None:
        code = normalize_team(row.team)
    if code is None:
        return None
    player = all_players.get(code)
    if player is None or player.get("position") != "DEF":
        return None
    return code


def _is_active_on_team(player: dict) -> bool:
    return player.get("status") == "Active" and bool(player.get("team"))


def match_source_names(
    rows: Sequence[SourceName],
    all_players: dict[str, dict],
    *,
    index: dict[str, list[str]] | None = None,
) -> MatchResult:
    if index is None:
        index = build_sleeper_name_index(all_players)
    result = MatchResult()

    for i, row in enumerate(rows):
        row_team = normalize_team(row.team)

        defense = _defense_player_id(row, all_players)
        if defense is not False:
            if defense is None:
                result.unmatched.append(row)
            else:
                result.matched[i] = defense
            continue

        candidates = list(index.get(normalize_name(row.name), []))
        if not candidates:
            result.unmatched.append(row)
            continue

        position = normalize_position(row.position)
        if position:
            by_position = [p for p in candidates if position in _fantasy_positions(all_players[p])]
            # Sources occasionally list a gadget player at another position
            # (a QB-eligible TE, a WR/RB hybrid). An empty filter means the
            # label disagreed, not that the player doesn't exist — keep the
            # name candidates and let team/status decide.
            if by_position:
                candidates = by_position

        if len(candidates) > 1 and row_team:
            by_team = [p for p in candidates if normalize_team(all_players[p].get("team")) == row_team]
            if by_team:
                candidates = by_team

        if len(candidates) > 1:
            active = [p for p in candidates if _is_active_on_team(all_players[p])]
            if active:
                candidates = active

        if len(candidates) > 1:
            result.ambiguous.append(row)
            continue

        pid = candidates[0]
        result.matched[i] = pid
        sleeper_team = all_players[pid].get("team")
        # Still a match: players change teams and Sleeper is often fresher than
        # a CSV, but the caller should see it.
        if row_team and normalize_team(sleeper_team) != row_team:
            result.team_mismatches.append((row, pid, sleeper_team))

    return result
