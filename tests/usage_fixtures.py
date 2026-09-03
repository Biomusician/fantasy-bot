"""Builders for the usage/role tests: synthetic player-weeks and a fake
fetcher that serves the CSV fixtures gzipped in memory, so the tests walk
the same decompress-and-parse path a real fetch does. No network, ever.
"""
from __future__ import annotations

import gzip
from pathlib import Path

from sleeper_tool import nfl_usage
from sleeper_tool.nfl_usage import PlayerWeek, TeamWeek, UsageData

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "nflverse"
FIXTURE_SEASON = 2025

# url substring -> fixture file
_FIXTURES = {
    "stats_player_week": "stats_player_week.csv",
    "stats_team_week": "stats_team_week.csv",
    "snap_counts": "snap_counts.csv",
    "players.csv": "players.csv",
    "db_playerids": "db_playerids.csv",
}


def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def fake_fetch(url: str) -> bytes:
    """Serve a fixture for the asset the URL names, gzipping it when the
    real asset would be gzipped."""
    for marker, filename in _FIXTURES.items():
        if marker in url:
            data = fixture_text(filename).encode("utf-8")
            return gzip.compress(data) if url.endswith(".gz") else data
    raise AssertionError(f"no fixture for {url}")


def absent_fetch(url: str) -> bytes:
    raise nfl_usage.AssetAbsent(url)


def make_player_week(
    gsis_id: str = "g1",
    week: int = 1,
    *,
    team: str = "KC",
    position: str | None = "WR",
    snaps: int | None = 40,
    snap_pct: float | None = 0.60,
    targets: float = 5.0,
    receptions: float = 3.0,
    rec_yards: float = 40.0,
    air_yards: float = 50.0,
    carries: float = 0.0,
    rush_yards: float = 0.0,
    pass_attempts: float = 0.0,
    target_share: float | None = None,
    air_yards_share: float | None = None,
    name: str | None = None,
) -> PlayerWeek:
    return PlayerWeek(
        gsis_id=gsis_id,
        week=week,
        team=team,
        position=position,
        snaps=snaps,
        snap_pct=snap_pct,
        targets=targets,
        receptions=receptions,
        rec_yards=rec_yards,
        air_yards=air_yards,
        carries=carries,
        rush_yards=rush_yards,
        pass_attempts=pass_attempts,
        target_share=target_share,
        air_yards_share=air_yards_share,
        name=name or gsis_id,
    )


def make_team_week(team: str = "KC", week: int = 1, *, targets: float = 30.0, carries: float = 20.0, attempts: float = 30.0) -> TeamWeek:
    return TeamWeek(team=team, week=week, targets=targets, carries=carries, attempts=attempts)


def make_usage(
    player_weeks: list[PlayerWeek],
    *,
    team_weeks: list[TeamWeek] | None = None,
    season: int = FIXTURE_SEASON,
    latest_week: int | None = None,
    teams: tuple[str, ...] = ("KC",),
    weeks: tuple[int, ...] | None = None,
) -> UsageData:
    """Team weeks default to a flat 30 targets / 20 carries for every team
    and week seen, which makes shares easy to reason about in tests."""
    if team_weeks is None:
        span = weeks or tuple(sorted({r.week for r in player_weeks}))
        team_weeks = [make_team_week(t, w) for t in teams for w in span]
    played = [r.week for r in player_weeks if r.played]
    return UsageData(
        season=season,
        fetched_at=None,
        latest_week=latest_week if latest_week is not None else (max(played) if played else None),
        player_weeks=list(player_weeks),
        team_weeks=list(team_weeks),
    )
