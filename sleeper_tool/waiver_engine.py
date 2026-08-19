"""Trending-add / free-agent waiver recommendations, cross-referenced
against my roster's positional needs.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.config import LeagueInfo, MY_USER_ID
from sleeper_tool.formatting import ordinal
from sleeper_tool.roster_analysis import SKILL_POSITIONS, ValuedRoster, player_name
from sleeper_tool.storage import Storage
from sleeper_tool.trade_engine import identify_needs, percentile_for_currency, value_currency, value_label_for_currency
from sleeper_tool.valuation import PlayerValue, ValuationEngine

EARLY_SEASON_WEEK_CUTOFF = 4  # below this week, trending-adds are hype-driven more than usage-driven


@dataclass
class WaiverTarget:
    player_id: str
    name: str
    position: str | None
    team: str | None
    trend_count: int
    value: PlayerValue
    fills_need: bool
    reason: str


def get_rostered_player_ids(storage: Storage, league: LeagueInfo) -> set[str]:
    rostered: set[str] = set()
    for roster in storage.get_rosters(league.league_id):
        rostered.update(roster.get("players") or [])
    return rostered


def get_waiver_targets(
    storage: Storage,
    engine: ValuationEngine,
    league: LeagueInfo,
    my_roster: ValuedRoster,
    *,
    top_n: int = 8,
    current_week: int | None = None,
) -> list[WaiverTarget]:
    if not my_roster.entries:
        # No roster yet usually means the league hasn't drafted (redraft
        # leagues start empty) — nothing meaningful to recommend yet.
        return []

    all_players = storage.get_all_players()
    rostered_ids = get_rostered_player_ids(storage, league)
    trending = storage.get_trending("add")
    my_needs = set(identify_needs(my_roster)[:2])
    currency = value_currency(my_roster)

    targets: list[WaiverTarget] = []
    for row in trending:
        pid = row["player_id"]
        if pid in rostered_ids:
            continue  # already on a roster in this league — not a valid waiver target here
        pdata = all_players.get(pid)
        if not pdata or pdata.get("position") not in SKILL_POSITIONS:
            continue
        # Sleeper's trending list can include players who are inactive/retired
        # league-wide; a NULL team means they're not currently on an NFL roster.
        if not pdata.get("team"):
            continue

        name = player_name(pdata)
        value = engine.value_player(name, my_roster.fmt, pdata.get("position"))
        fills_need = pdata.get("position") in my_needs

        # Sleeper's trending endpoint is platform-wide (all leagues, not just
        # this one) — there's no per-league trending data available via the API.
        reason_bits = [f"{row.get('count', 0)} adds across Sleeper in the last 48h"]
        if current_week is not None and current_week < EARLY_SEASON_WEEK_CUTOFF:
            # Early-season trending is hype/name-recognition driven more than
            # usage-driven — there just isn't enough game data yet for adds
            # to reflect real opportunity share the way they will by week 4+.
            reason_bits.append("small early-season sample, treat as hype risk")
        if fills_need:
            reason_bits.append(f"fills your {pdata['position']} need")
        pctl = percentile_for_currency(value, currency)
        if pctl is not None:
            reason_bits.append(f"{ordinal(round(pctl))} percentile {value_label_for_currency(currency)}")

        targets.append(
            WaiverTarget(
                player_id=pid,
                name=name,
                position=pdata.get("position"),
                team=pdata.get("team"),
                trend_count=row.get("count", 0),
                value=value,
                fills_need=fills_need,
                reason="; ".join(reason_bits),
            )
        )

    targets.sort(key=lambda t: (not t.fills_need, -(percentile_for_currency(t.value, currency) or 0)))
    return targets[:top_n]


@dataclass
class TimeSensitiveNote:
    player_name: str
    note: str


def get_time_sensitive_notes(
    storage: Storage, my_roster: ValuedRoster, *, current_week: int | None = None
) -> list[TimeSensitiveNote]:
    """Injury/inactive/bye-week flags for my own roster — the "anything
    time-sensitive" part of the weekly report. Bye week comes from
    FantasyPros/RotoBaller (via PlayerValue.bye_week) since Sleeper's player
    objects don't carry it at all.
    """
    notes: list[TimeSensitiveNote] = []
    for entry in my_roster.entries:
        if entry.injury_status and entry.injury_status not in ("Healthy", None):
            notes.append(TimeSensitiveNote(entry.name, f"Injury status: {entry.injury_status}"))
        if entry.status and entry.status not in ("Active", "Inactive"):
            # "Inactive" alone is common/benign (e.g. practice squad); flag anything unusual instead.
            notes.append(TimeSensitiveNote(entry.name, f"Roster status: {entry.status}"))
        if current_week is not None and entry.value.bye_week == current_week and entry.is_starter:
            notes.append(TimeSensitiveNote(entry.name, f"On bye week {current_week} — starting slot needs a fill-in"))
    return notes
