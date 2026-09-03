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


# Tables carrying a fetched_at column, and the only identifiers
# table_last_fetched/row_count will interpolate into SQL. `players` is
# absent on purpose: it tracks freshness in `meta` (players_updated_at) via
# players_last_updated().
FETCHED_AT_TABLES = frozenset(
    {"leagues", "league_users", "rosters", "matchups", "transactions", "trending", "traded_picks"}
)


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
        self._players_cache: dict[str, dict] | None = None
        self._league_cache: dict[str, dict | None] = {}

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
        self._players_cache = None

    def get_player(self, player_id: str) -> dict | None:
        row = self._conn.execute("SELECT data FROM players WHERE player_id = ?", (player_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def get_all_players(self) -> dict[str, dict]:
        """Memoized per Storage instance: ~12k JSON rows, and a report run
        asks for it dozens of times (every league's roster build, waiver
        pass, free-agent pool, and waiver preview). Invalidated by
        save_players. Callers must treat the dict as read-only."""
        if self._players_cache is None:
            rows = self._conn.execute("SELECT player_id, data FROM players").fetchall()
            self._players_cache = {r["player_id"]: json.loads(r["data"]) for r in rows}
        return self._players_cache

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
        self._league_cache.pop(league_id, None)

    def get_league(self, league_id: str) -> dict | None:
        """Memoized per Storage instance, like get_all_players: the settings
        blob never changes within a run, and a report run re-reads (and
        re-parses) it a couple of hundred times — every roster build,
        playoff-threshold shift and pick valuation. A cached None is a real
        answer ("no such league"), so absence is cached too. Invalidated by
        save_league. Callers must treat the dict as read-only."""
        if league_id in self._league_cache:
            return self._league_cache[league_id]
        row = self._conn.execute("SELECT data FROM leagues WHERE league_id = ?", (league_id,)).fetchone()
        data = json.loads(row["data"]) if row else None
        self._league_cache[league_id] = data
        return data

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

    def get_all_transactions(self, league_id: str) -> list[dict]:
        """Every transaction ever cached for this league, any week. Rows
        are keyed by transaction_id, so weeks synced on earlier runs are
        still here even though each sync only refetches the recent ones."""
        rows = self._conn.execute(
            "SELECT data FROM transactions WHERE league_id = ? ORDER BY week", (league_id,)
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
        """Replace this trend type's list wholesale.

        Sleeper's trending endpoint returns a ranked top-N snapshot, not a
        set of durable rows. Upserting into the old list (what this used to
        do) made the table an append-only union of every list ever fetched,
        ordered by a raw count that isn't comparable across days — a player
        who was hot last week and has since fallen off entirely kept his old
        count and outranked today's genuine risers. Deleting first is what
        makes get_trending's "today's list, best first" contract true.
        """
        now = utcnow_iso()
        rows = [(trend_type, p["player_id"], p.get("count"), now) for p in players]
        with self._cursor() as cur:
            cur.execute("DELETE FROM trending WHERE trend_type = ?", (trend_type,))
            cur.executemany(
                "INSERT INTO trending (trend_type, player_id, count, fetched_at) VALUES (?, ?, ?, ?)",
                rows,
            )

    def get_trending(self, trend_type: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT player_id, count, fetched_at FROM trending WHERE trend_type = ? ORDER BY count DESC",
            (trend_type,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- freshness (read-only) --------------------------------------------

    def table_last_fetched(self, table: str) -> dt.datetime | None:
        """Newest fetched_at in one table, or None if it's empty.

        Table names are whitelisted rather than interpolated blind — SQLite
        can't parameterize an identifier, so the whitelist is what keeps
        this from being string-built SQL.
        """
        if table not in FETCHED_AT_TABLES:
            raise ValueError(f"Unknown table {table!r}; known: {sorted(FETCHED_AT_TABLES)}")
        row = self._conn.execute(f"SELECT MAX(fetched_at) AS m FROM {table}").fetchone()
        raw = row["m"] if row else None
        return dt.datetime.fromisoformat(raw) if raw else None

    def latest_fetched_at(self, *tables: str) -> dt.datetime | None:
        """Newest fetched_at across several tables — the age of a whole
        family of data (see signal_health), not of one table."""
        stamps = [s for s in (self.table_last_fetched(t) for t in tables) if s is not None]
        return max(stamps) if stamps else None

    def row_count(self, table: str) -> int:
        if table not in FETCHED_AT_TABLES:
            raise ValueError(f"Unknown table {table!r}; known: {sorted(FETCHED_AT_TABLES)}")
        return self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
