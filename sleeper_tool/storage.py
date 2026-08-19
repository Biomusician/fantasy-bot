"""SQLite persistence for everything pulled from Sleeper.

Design: store the raw API payload as a JSON blob per row (so we never lose
data to an incomplete schema when Sleeper adds fields) plus a handful of
indexed columns for the lookups we actually do (owner_id, week, etc.), and a
`fetched_at` timestamp on every row so callers can decide whether cached data
is fresh enough to skip a re-fetch.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sleeper.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    full_name TEXT,
    position TEXT,
    team TEXT,
    status TEXT,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leagues (
    league_id TEXT PRIMARY KEY,
    name TEXT,
    season TEXT,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS league_users (
    league_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT,
    team_name TEXT,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, user_id)
);

CREATE TABLE IF NOT EXISTS rosters (
    league_id TEXT NOT NULL,
    roster_id INTEGER NOT NULL,
    owner_id TEXT,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, roster_id)
);

CREATE TABLE IF NOT EXISTS matchups (
    league_id TEXT NOT NULL,
    week INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,
    matchup_id INTEGER,
    points REAL,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, week, roster_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    league_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    week INTEGER,
    type TEXT,
    status TEXT,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS trending (
    trend_type TEXT NOT NULL,
    player_id TEXT NOT NULL,
    count INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (trend_type, player_id)
);

CREATE TABLE IF NOT EXISTS traded_picks (
    league_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # -- meta -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- players -----------------------------------------------------------

    def players_last_updated(self) -> dt.datetime | None:
        raw = self.get_meta("players_updated_at")
        return dt.datetime.fromisoformat(raw) if raw else None

    def save_players(self, players: dict[str, dict]) -> None:
        now = utcnow_iso()
        rows = []
        for player_id, p in players.items():
            full_name = p.get("full_name") or " ".join(
                filter(None, [p.get("first_name"), p.get("last_name")])
            )
            rows.append(
                (
                    player_id,
                    full_name or None,
                    p.get("position"),
                    p.get("team"),
                    p.get("status"),
                    json.dumps(p),
                    now,
                )
            )
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO players (player_id, full_name, position, team, status, data, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(player_id) DO UPDATE SET "
                "full_name=excluded.full_name, position=excluded.position, team=excluded.team, "
                "status=excluded.status, data=excluded.data, updated_at=excluded.updated_at",
                rows,
            )
        self.set_meta("players_updated_at", now)

    def get_player(self, player_id: str) -> dict | None:
        row = self._conn.execute("SELECT data FROM players WHERE player_id = ?", (player_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def get_all_players(self) -> dict[str, dict]:
        rows = self._conn.execute("SELECT player_id, data FROM players").fetchall()
        return {r["player_id"]: json.loads(r["data"]) for r in rows}

    def player_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]

    # -- leagues -----------------------------------------------------------

    def save_league(self, league_id: str, data: dict) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO leagues (league_id, name, season, data, fetched_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(league_id) DO UPDATE SET "
                "name=excluded.name, season=excluded.season, data=excluded.data, fetched_at=excluded.fetched_at",
                (league_id, data.get("name"), data.get("season"), json.dumps(data), utcnow_iso()),
            )

    def get_league(self, league_id: str) -> dict | None:
        row = self._conn.execute("SELECT data FROM leagues WHERE league_id = ?", (league_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def league_fetched_at(self, league_id: str) -> dt.datetime | None:
        row = self._conn.execute(
            "SELECT fetched_at FROM leagues WHERE league_id = ?", (league_id,)
        ).fetchone()
        return dt.datetime.fromisoformat(row["fetched_at"]) if row else None

    # -- league users -----------------------------------------------------------

    def save_league_users(self, league_id: str, users: list[dict]) -> None:
        now = utcnow_iso()
        rows = [
            (
                league_id,
                u["user_id"],
                u.get("display_name"),
                (u.get("metadata") or {}).get("team_name"),
                json.dumps(u),
                now,
            )
            for u in users
        ]
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO league_users (league_id, user_id, display_name, team_name, data, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(league_id, user_id) DO UPDATE SET "
                "display_name=excluded.display_name, team_name=excluded.team_name, "
                "data=excluded.data, fetched_at=excluded.fetched_at",
                rows,
            )

    def get_league_users(self, league_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT data FROM league_users WHERE league_id = ?", (league_id,)
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # -- rosters -----------------------------------------------------------

    def save_rosters(self, league_id: str, rosters: list[dict]) -> None:
        now = utcnow_iso()
        rows = [
            (league_id, r["roster_id"], r.get("owner_id"), json.dumps(r), now)
            for r in rosters
        ]
        with self._cursor() as cur:
            cur.execute("DELETE FROM rosters WHERE league_id = ?", (league_id,))
            cur.executemany(
                "INSERT INTO rosters (league_id, roster_id, owner_id, data, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def get_rosters(self, league_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT data FROM rosters WHERE league_id = ? ORDER BY roster_id", (league_id,)
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # -- matchups -----------------------------------------------------------

    def save_matchups(self, league_id: str, week: int, matchups: list[dict]) -> None:
        now = utcnow_iso()
        rows = [
            (
                league_id,
                week,
                m["roster_id"],
                m.get("matchup_id"),
                m.get("points"),
                json.dumps(m),
                now,
            )
            for m in matchups
        ]
        with self._cursor() as cur:
            cur.execute("DELETE FROM matchups WHERE league_id = ? AND week = ?", (league_id, week))
            cur.executemany(
                "INSERT INTO matchups (league_id, week, roster_id, matchup_id, points, data, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def get_matchups(self, league_id: str, week: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT data FROM matchups WHERE league_id = ? AND week = ?", (league_id, week)
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # -- transactions -----------------------------------------------------------

    def save_transactions(self, league_id: str, week: int, transactions: list[dict]) -> None:
        now = utcnow_iso()
        rows = [
            (
                league_id,
                t["transaction_id"],
                t.get("leg", week),
                t.get("type"),
                t.get("status"),
                json.dumps(t),
                now,
            )
            for t in transactions
        ]
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO transactions (league_id, transaction_id, week, type, status, data, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(league_id, transaction_id) DO UPDATE SET "
                "week=excluded.week, type=excluded.type, status=excluded.status, "
                "data=excluded.data, fetched_at=excluded.fetched_at",
                rows,
            )

    def get_transactions(self, league_id: str, week: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT data FROM transactions WHERE league_id = ? AND week = ?", (league_id, week)
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # -- traded picks -----------------------------------------------------------

    def save_traded_picks(self, league_id: str, picks: list[dict]) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO traded_picks (league_id, data, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(league_id) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                (league_id, json.dumps(picks), utcnow_iso()),
            )

    def get_traded_picks(self, league_id: str) -> list[dict]:
        row = self._conn.execute(
            "SELECT data FROM traded_picks WHERE league_id = ?", (league_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else []

    # -- trending -----------------------------------------------------------

    def save_trending(self, trend_type: str, players: list[dict]) -> None:
        now = utcnow_iso()
        rows = [(trend_type, p["player_id"], p.get("count"), now) for p in players]
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO trending (trend_type, player_id, count, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(trend_type, player_id) DO UPDATE SET "
                "count=excluded.count, fetched_at=excluded.fetched_at",
                rows,
            )

    def get_trending(self, trend_type: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT player_id, count, fetched_at FROM trending WHERE trend_type = ? ORDER BY count DESC",
            (trend_type,),
        ).fetchall()
        return [dict(r) for r in rows]
