"""What position Sleeper actually means.

A Sleeper player carries two position fields and they disagree often enough
to matter. `position` is his PRIMARY NFL listing; `fantasy_positions` is the
list of slots he is eligible to fill. Travis Hunter is position "DB" with
fantasy_positions ["DB", "WR"], and every fullback is position "FB" with
fantasy_positions ["RB"] — 124 players in the current player file.

Reading `position` alone therefore drops real, startable players out of
whatever is being built: the free-agent universe, the ranking-source name
index, a roster's positional depth. This module is the one place that
decides the question, so the scraping layer and the analysis layer cannot
answer it differently.
"""
from __future__ import annotations

FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Sources spell defense and kicker several ways; Sleeper uses the values above.
POSITION_ALIASES = {"DST": "DEF", "D/ST": "DEF", "PK": "K"}

# When a player is eligible at more than one, the one to treat him as. Order
# is by how much of a fantasy roster the position actually occupies, so a
# WR/DB is a WR and an RB/WR is an RB.
_PREFERENCE = ("QB", "RB", "WR", "TE", "K", "DEF")


def normalize_position(position: str | None) -> str | None:
    if not position:
        return None
    cleaned = position.strip().upper()
    return POSITION_ALIASES.get(cleaned, cleaned)


def fantasy_positions(player: dict) -> set[str]:
    """Every fantasy position this player is eligible at (possibly empty)."""
    listed = player.get("fantasy_positions") or []
    found = {normalize_position(p) for p in listed if p}
    found.add(normalize_position(player.get("position")))
    return {p for p in found if p in FANTASY_POSITIONS}


def fantasy_position(player: dict) -> str | None:
    """The single position to treat this player as, or None if he has no
    fantasy eligibility at all."""
    eligible = fantasy_positions(player)
    for pos in _PREFERENCE:
        if pos in eligible:
            return pos
    return None
