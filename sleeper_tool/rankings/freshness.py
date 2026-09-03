"""Per-source freshness policy — how old each input is allowed to get.

Three windows per source, as a `(fresh, usable, ceiling)` triple:

  fresh    — within this age the data is treated as current.
  usable   — older than `fresh` but still worth building a report on, with
             the age surfaced to the reader.
  ceiling  — the hard limit. Past this, a source is Unavailable: the cache
             layer refuses to serve it as a fallback and the health layer
             tells consumers to suppress whatever depended on it.

The table lives here rather than in `cache.py` so the cache stays a generic
mechanism that knows nothing about source names, and rather than in
`signal_health.py` so the ranking scrapers can pass a ceiling into
`get_or_fetch` without importing the health layer (which imports them).

The values are policy, not measurement: KTC/FantasyPros/RotoBaller move
daily in season, so a week is the point at which their numbers describe a
different league than the one being played. The nflverse schedule barely
changes once published, so it gets two months. Sleeper's own tables are
split by how fast they turn over — rosters and league settings drift
slowly, weekly matchups/transactions/trending do not.
"""
from __future__ import annotations

import datetime as dt

_H = dt.timedelta(hours=1)
_D = dt.timedelta(days=1)

# family -> (fresh, usable, ceiling)
SOURCE_WINDOWS: dict[str, tuple[dt.timedelta, dt.timedelta, dt.timedelta]] = {
    "ktc": (20 * _H, 3 * _D, 7 * _D),
    "fantasypros": (20 * _H, 3 * _D, 7 * _D),
    "rotoballer": (20 * _H, 3 * _D, 7 * _D),
    "nflverse_schedule": (24 * _H, 14 * _D, 60 * _D),
    "nflverse_usage": (24 * _H, 8 * _D, 21 * _D),
    "sleeper_players": (20 * _H, 2 * _D, 7 * _D),
    "sleeper_league": (36 * _H, 3 * _D, 10 * _D),
    "sleeper_weekly": (36 * _H, 8 * _D, 21 * _D),
    # The Dynasty Pass CSV is a manual export; ff_dynasty_pass.py already
    # refuses to read one over a week old, so usable and ceiling coincide.
    "ff_dynasty_pass": (2 * _D, 7 * _D, 7 * _D),
}

# family -> minimum row count below which the source is only Partial. These
# are floors on "did we get a whole list", not targets: KTC publishes ~500
# ranked players, the FantasyPros lists 300-600, RotoBaller ~600, the
# nflverse schedule ~285 regular+post games, Sleeper ~11k players.
MIN_COVERAGE: dict[str, int] = {
    "ktc": 400,
    "fantasypros": 300,
    "rotoballer": 300,
    "nflverse_schedule": 200,
    "sleeper_players": 5000,
}


def windows_for(family: str) -> tuple[dt.timedelta, dt.timedelta, dt.timedelta] | None:
    return SOURCE_WINDOWS.get(family)


def ceiling_for(family: str) -> dt.timedelta | None:
    windows = SOURCE_WINDOWS.get(family)
    return windows[2] if windows else None


def coverage_floor(family: str) -> int | None:
    return MIN_COVERAGE.get(family)
