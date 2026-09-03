"""A whole synthetic Sleeper league, in memory, with no network and no disk.

`sleeper_tool.storage.Storage` is a thin JSON-blob cache: nine read methods
are everything the report path actually asks it for. `FakeStorage`
implements exactly those (plus the context-manager protocol), and
`make_synthetic_league` fills one with a small but realistic league —
rosters, users, players, matchups, transactions, traded picks, a trending
list — so an end-to-end test can call `build_weekly_report_data` for real
instead of hand-assembling a `LeagueReportData`.

`make_engine` is the other half: a `ValuationEngine` whose ranking
snapshots are built from the same synthetic player pool, so every rostered
player has a KTC value, an ECR rank and a projection without a single HTTP
request.

Nothing here reads or writes `data/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import datetime as dt

from sleeper_tool.config import MY_USER_ID, LeagueInfo
from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.rankings.fantasypros import FANTASYPROS_PAGES
from sleeper_tool.rankings.rotoballer import ROTOBALLER_SPREADSHEETS
from sleeper_tool.valuation import ValuationEngine

# A real Sleeper roster_positions payload for a superflex dynasty league.
DEFAULT_ROSTER_POSITIONS = [
    "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF",
    "BN", "BN", "BN", "BN", "BN", "IR", "TAXI",
]

DEFAULT_SCORING = {
    "rec": 1.0,
    "pass_td": 4.0,
    "bonus_rec_te": 0.0,
    "bonus_rush_yd_100": 0.0,
}

DEFAULT_SETTINGS = {
    "waiver_type": 2,  # FAAB
    "waiver_budget": 100,
    "playoff_week_start": 15,
    "playoff_teams": 4,
    "trade_deadline": 12,
    "draft_rounds": 4,
    "num_teams": 5,
    "type": 2,  # dynasty
    "taxi_slots": 1,
    "reserve_slots": 1,
}

NFL_TEAMS = ["KC", "BUF", "SF", "PHI", "DAL", "CIN", "DET", "MIA", "BAL", "LAC", "GB", "MIN"]

# Deliberately boring, unique, ASCII surnames — the ranking sources are
# keyed by normalized name, so collisions would silently mis-value people.
_SURNAMES = [
    "Ackerman", "Bellweather", "Carrow", "Danvers", "Eastwick", "Fenwick", "Garrity", "Halloway",
    "Ingram", "Jessup", "Kirkland", "Lomax", "Mardling", "Norcross", "Oakhurst", "Pemberton",
    "Quillon", "Rathbone", "Sandoval", "Thackery", "Underhill", "Vandermeer", "Whitlock", "Yardley",
    "Ashford", "Braddock", "Cavanaugh", "Drexler", "Everley", "Fairbanks", "Gallagher", "Hawthorne",
    "Ivory", "Jamison", "Kettleman", "Larkspur", "Merriweather", "Northrop", "Ovington", "Prescott",
    "Quimby", "Radcliffe", "Sterling", "Tremaine", "Ulster", "Vickery", "Wexford", "Yarborough",
    "Abernathy", "Blackwood", "Chesterton", "Duckworth", "Ellsworth", "Fitzgerald", "Grimsby", "Huxley",
    "Isenberg", "Jarvis", "Kendrick", "Lindqvist", "Mortimer", "Nightingale", "Ormsby", "Pickford",
    "Quintrell", "Rutherford", "Sedgwick", "Thornbury", "Uxbridge", "Vanterpool", "Woodhouse", "Yeardley",
    "Ainsworth", "Bramwell", "Cordelia", "Dunsmore", "Ellington", "Falconer", "Glendower", "Harrington",
]

_FIRST_NAMES = ["Alan", "Brett", "Cody", "Dane", "Eli", "Frank", "Gus", "Hank", "Ivan", "Jonah"]

# One entry per DEF: Sleeper keys team defenses by the team code itself.
_DEF_CITIES = {
    "KC": "Kansas City Chiefs", "BUF": "Buffalo Bills", "SF": "San Francisco 49ers",
    "PHI": "Philadelphia Eagles", "DAL": "Dallas Cowboys", "CIN": "Cincinnati Bengals",
    "DET": "Detroit Lions", "MIA": "Miami Dolphins", "BAL": "Baltimore Ravens",
    "LAC": "Los Angeles Chargers", "GB": "Green Bay Packers", "MIN": "Minnesota Vikings",
}


# ---------------------------------------------------------------------------
# FakeStorage
# ---------------------------------------------------------------------------


class FakeStorage:
    """The nine read methods `report_data` actually calls, over plain dicts.

    Deliberately NOT a subclass of Storage: the point is to prove the report
    path only needs this surface. Write-ish helpers (`set_meta`, `add_league`)
    exist so tests can build state without reaching into the attributes.
    """

    def __init__(self) -> None:
        self._meta: dict[str, str] = {}
        self._leagues: dict[str, dict] = {}
        self._rosters: dict[str, list[dict]] = {}
        self._users: dict[str, list[dict]] = {}
        self._players: dict[str, dict] = {}
        self._matchups: dict[tuple[str, int], list[dict]] = {}
        self._transactions: dict[str, list[dict]] = {}
        self._traded_picks: dict[str, list[dict]] = {}
        self._trending: dict[str, list[dict]] = {}
        # signal_health grades how old the Sleeper cache is; a synthetic run
        # is "just synced" unless a test says otherwise.
        self.fetched_at: dt.datetime | None = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        self.closed = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeStorage":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- the nine the report path reads -----------------------------------

    def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    def get_league(self, league_id: str) -> dict | None:
        return self._leagues.get(league_id)

    def get_rosters(self, league_id: str) -> list[dict]:
        return self._rosters.get(league_id, [])

    def get_league_users(self, league_id: str) -> list[dict]:
        return self._users.get(league_id, [])

    def get_all_players(self) -> dict[str, dict]:
        return self._players

    def get_matchups(self, league_id: str, week: int) -> list[dict]:
        return self._matchups.get((league_id, week), [])

    def get_transactions(self, league_id: str, week: int) -> list[dict]:
        return [t for t in self._transactions.get(league_id, []) if t.get("leg") == week]

    def get_all_transactions(self, league_id: str) -> list[dict]:
        return self._transactions.get(league_id, [])

    def get_traded_picks(self, league_id: str) -> list[dict]:
        return self._traded_picks.get(league_id, [])

    def get_trending(self, trend_type: str) -> list[dict]:
        return self._trending.get(trend_type, [])

    # -- freshness (what signal_health grades the Sleeper cache on) --------

    _TABLE_SOURCES = {
        "leagues": "_leagues", "rosters": "_rosters", "league_users": "_users",
        "traded_picks": "_traded_picks", "matchups": "_matchups",
        "transactions": "_transactions", "trending": "_trending",
    }

    def players_last_updated(self) -> dt.datetime | None:
        return self.fetched_at if self._players else None

    def player_count(self) -> int:
        return len(self._players)

    def row_count(self, table: str) -> int:
        store = getattr(self, self._TABLE_SOURCES[table])
        return sum(len(v) if isinstance(v, list) else 1 for v in store.values())

    def table_last_fetched(self, table: str) -> dt.datetime | None:
        return self.fetched_at if self.row_count(table) else None

    def latest_fetched_at(self, *tables: str) -> dt.datetime | None:
        stamps = [s for s in (self.table_last_fetched(t) for t in tables) if s is not None]
        return max(stamps) if stamps else None

    # -- setup helpers ------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value

    def add_players(self, players: dict[str, dict]) -> None:
        self._players.update(players)

    def drop_player(self, player_id: str) -> None:
        """Remove a player from the cache without touching any roster —
        the "Sleeper referenced a player the daily cache doesn't have" case."""
        self._players.pop(player_id, None)

    def add_league(self, synth: "SyntheticLeague") -> "FakeStorage":
        lid = synth.info.league_id
        self._leagues[lid] = synth.league
        self._rosters[lid] = synth.rosters
        self._users[lid] = synth.users
        self._transactions[lid] = synth.transactions
        self._traded_picks[lid] = synth.traded_picks
        for week, rows in synth.matchups.items():
            self._matchups[(lid, week)] = rows
        self._players.update(synth.players)
        for kind, rows in synth.trending.items():
            existing = {r["player_id"] for r in self._trending.setdefault(kind, [])}
            self._trending[kind].extend(r for r in rows if r["player_id"] not in existing)
        return self


# ---------------------------------------------------------------------------
# the synthetic league
# ---------------------------------------------------------------------------


@dataclass
class SyntheticLeague:
    """Everything one league contributes to a FakeStorage, plus the
    `LeagueInfo` `build_weekly_report_data` is called with."""

    info: LeagueInfo
    league: dict
    rosters: list[dict]
    users: list[dict]
    players: dict[str, dict]
    matchups: dict[int, list[dict]] = field(default_factory=dict)
    transactions: list[dict] = field(default_factory=list)
    traded_picks: list[dict] = field(default_factory=list)
    trending: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def my_roster(self) -> dict:
        return next(r for r in self.rosters if r.get("owner_id") == MY_USER_ID)


def _player(
    player_id: str, name: str, position: str, team: str, *, age: float = 25.0, years_exp: int = 3,
    injury_status: str | None = None, status: str = "Active",
) -> dict:
    first, _, last = name.partition(" ")
    return {
        "player_id": player_id,
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "position": position,
        "team": team,
        "age": age,
        "years_exp": years_exp,
        "injury_status": injury_status,
        "status": status,
        "gsis_id": f"00-{int(player_id):07d}" if player_id.isdigit() else None,
    }


def make_player_pool(
    *, count: int = 96, start_id: int = 1000, positions: Iterable[str] = ("QB", "RB", "WR", "TE"),
) -> dict[str, dict]:
    """`count` skill players with unique names, plus one K and one DEF per
    NFL team in NFL_TEAMS. Ages/experience vary so the dynasty-developmental
    and veteran-decline paths both have material to work with."""
    pool: dict[str, dict] = {}
    positions = list(positions)
    for i in range(count):
        pid = str(start_id + i)
        # First name varies only after a full pass through the surnames, so
        # 80 surnames x 10 first names = 800 unique pairs. Do NOT distinguish
        # players with a "II"/"Jr." suffix: name_matching strips those, and
        # two roster players sharing a normalized name would silently share
        # one row in every ranking index.
        first = _FIRST_NAMES[(i // len(_SURNAMES)) % len(_FIRST_NAMES)]
        name = f"{first} {_SURNAMES[i % len(_SURNAMES)]}"
        pool[pid] = _player(
            pid, name, positions[i % len(positions)], NFL_TEAMS[i % len(NFL_TEAMS)],
            age=22.0 + (i % 12), years_exp=i % 8,
        )
    for i, team in enumerate(NFL_TEAMS):
        kid = str(start_id + count + i)
        pool[kid] = _player(kid, f"Kip {_SURNAMES[(count + i) % len(_SURNAMES)]}", "K", team)
        pool[team] = _player(team, _DEF_CITIES[team], "DEF", team, age=0.0, years_exp=0)
    return pool


def _sorted_skill_ids(players: dict[str, dict]) -> list[str]:
    """Deterministic ordering that also fixes each player's ranking tier:
    by position, then by numeric id."""
    return sorted(
        (pid for pid, p in players.items() if p.get("position") in ("QB", "RB", "WR", "TE")),
        key=lambda pid: (players[pid]["position"], int(pid)),
    )


def make_synthetic_league(
    *,
    name: str = "Synthetic Dynasty",
    league_id: str = "9000000000000000001",
    kind: str = "dynasty",
    teams: int = 5,
    status: str = "in_season",
    season: str = "2026",
    roster_positions: list[str] | None = None,
    scoring_settings: dict | None = None,
    settings: dict | None = None,
    players: dict[str, dict] | None = None,
    roster_size: int = 15,
    my_roster_id: int = 1,
    current_week: int | None = 3,
    with_taxi: bool = True,
    with_reserve: bool = True,
    include_roster_settings: bool = True,
    games_played: int = 4,
    my_team_name: str = "Statistical Anomalies",
) -> SyntheticLeague:
    """A 4-6 team league in which roster `my_roster_id` is owned by
    `config.MY_USER_ID`.

    Every knob a report-path branch keys off is a parameter: `status`
    ("pre_draft" suppresses waivers), `roster_positions` (an unknown slot
    type disables the lineup features), `include_roster_settings` (a roster
    payload with no `settings` key at all), `players` (pass a pool with a
    hole in it to exercise the missing-player path).
    """
    if not 4 <= teams <= 6:
        raise ValueError("make_synthetic_league models a 4-6 team league")
    slots = list(roster_positions if roster_positions is not None else DEFAULT_ROSTER_POSITIONS)
    league_settings = {**DEFAULT_SETTINGS, "num_teams": teams}
    if settings:
        league_settings.update(settings)
    pool = dict(players) if players is not None else make_player_pool()

    skill_ids = _sorted_skill_ids(pool)
    k_ids = sorted((pid for pid, p in pool.items() if p.get("position") == "K"), key=lambda pid: int(pid))
    def_ids = sorted(pid for pid, p in pool.items() if p.get("position") == "DEF")

    # Deal skill players round-robin so each roster gets a spread of
    # positions and of value tiers rather than one team hoarding the top.
    per_team_skill = roster_size - 2  # one K and one DEF each
    dealt: dict[int, list[str]] = {rid: [] for rid in range(1, teams + 1)}
    for i, pid in enumerate(skill_ids):
        rid = (i % teams) + 1
        if len(dealt[rid]) < per_team_skill:
            dealt[rid].append(pid)

    rosters: list[dict] = []
    users: list[dict] = []
    for rid in range(1, teams + 1):
        is_mine = rid == my_roster_id
        owner_id = MY_USER_ID if is_mine else f"owner{rid}"
        roster_players = list(dealt[rid])
        if rid - 1 < len(k_ids):
            roster_players.append(k_ids[rid - 1])
        if rid - 1 < len(def_ids):
            roster_players.append(def_ids[rid - 1])

        starters = _pick_starters(slots, roster_players, pool)
        taxi = [roster_players[-3]] if with_taxi and len(roster_players) >= 3 else []
        reserve = [roster_players[-4]] if with_reserve and len(roster_players) >= 4 else []
        # A player can't be both a starter and stashed.
        taxi = [p for p in taxi if p not in starters]
        reserve = [p for p in reserve if p not in starters and p not in taxi]

        roster: dict[str, Any] = {
            "roster_id": rid,
            "owner_id": owner_id,
            "league_id": league_id,
            "players": roster_players,
            "starters": starters,
            "taxi": taxi,
            "reserve": reserve,
        }
        if include_roster_settings:
            # games_played must clear playoff_leverage.MIN_GAMES_FOR_LABEL
            # (3) or the playoff picture never gets computed at all.
            wins = (teams - rid) % (games_played + 1)
            roster["settings"] = {
                "wins": wins,
                "losses": games_played - wins,
                "ties": 0,
                "fpts": 300 + rid * 17,
                "fpts_decimal": 40,
                "waiver_budget_used": rid * 7,
            }
        rosters.append(roster)
        users.append({
            "user_id": owner_id,
            "display_name": my_team_name.replace(" ", "") if is_mine else f"Rival{rid}",
            "metadata": {"team_name": my_team_name if is_mine else f"Team {rid}"},
        })

    # Free agents: everything the deal didn't hand out. The trending list is
    # drawn from them so the waiver engine has real candidates.
    rostered = {pid for r in rosters for pid in r["players"]}
    free_skill = [pid for pid in skill_ids if pid not in rostered]
    trending = {
        "add": [{"player_id": pid, "count": 5000 - i * 137} for i, pid in enumerate(free_skill[:12])],
        "drop": [{"player_id": pid, "count": 900 - i * 50} for i, pid in enumerate(free_skill[12:16])],
    }

    matchups: dict[int, list[dict]] = {}
    if current_week:
        rows = []
        for rid in range(1, teams + 1):
            rows.append({
                "roster_id": rid,
                "matchup_id": ((rid - 1) // 2) + 1,
                "points": 90.0 + rid * 3.5,
                "starters": rosters[rid - 1]["starters"],
                "players": rosters[rid - 1]["players"],
            })
        matchups[current_week] = rows

    transactions = _make_transactions(rosters, free_skill, current_week or 1, teams)
    traded_picks = _make_traded_picks(season, teams)

    info = LeagueInfo(
        name=name,
        league_id=league_id,
        kind=kind,
        sleeper_type={"dynasty": 2, "keeper": 1, "redraft": 0}[kind],
        my_team_name=my_team_name,
    )
    league = {
        "league_id": league_id,
        "name": name,
        "season": season,
        "status": status,
        "settings": league_settings,
        "roster_positions": slots,
        "scoring_settings": dict(scoring_settings if scoring_settings is not None else DEFAULT_SCORING),
        "total_rosters": teams,
    }
    return SyntheticLeague(
        info=info, league=league, rosters=rosters, users=users, players=pool,
        matchups=matchups, transactions=transactions, traded_picks=traded_picks, trending=trending,
    )


def _pick_starters(slots: list[str], roster_players: list[str], pool: dict[str, dict]) -> list[str]:
    """A plausible Sleeper `starters` array: one player per starting slot,
    in slot order, "0" where nobody eligible is left (Sleeper's own filler)."""
    from sleeper_tool.lineup_optimizer import FLEX_ELIGIBILITY, NON_STARTER_SLOTS

    available = list(roster_players)
    starters: list[str] = []
    for slot in slots:
        if slot in NON_STARTER_SLOTS:
            continue
        eligible = FLEX_ELIGIBILITY.get(slot, frozenset({slot}))
        pick = next((p for p in available if (pool.get(p) or {}).get("position") in eligible), None)
        if pick is None:
            starters.append("0")
            continue
        available.remove(pick)
        starters.append(pick)
    return starters


def _make_transactions(rosters: list[dict], free_skill: list[str], week: int, teams: int) -> list[dict]:
    """One completed FAAB waiver (a real winning bid), one failed claim (a
    price that was never paid, and must not be read as one), one completed
    free-agent add, and a trade carrying both players and a draft pick."""
    a_id = free_skill[0] if free_skill else "1000"
    b_id = free_skill[1] if len(free_skill) > 1 else "1001"
    r1, r2 = rosters[0], rosters[min(2, teams - 1)]
    return [
        {
            "transaction_id": "tx-waiver-ok", "type": "waiver", "status": "complete", "leg": max(1, week - 1),
            "settings": {"waiver_bid": 14, "seq": 1},
            "adds": {a_id: r2["roster_id"]}, "drops": {r2["players"][-1]: r2["roster_id"]},
            "roster_ids": [r2["roster_id"]], "creator": r2["owner_id"], "status_updated": 1_760_000_000_000,
        },
        {
            "transaction_id": "tx-waiver-failed", "type": "waiver", "status": "failed", "leg": max(1, week - 1),
            "settings": {"waiver_bid": 55, "seq": 2},
            "adds": {a_id: r1["roster_id"]}, "drops": None,
            "roster_ids": [r1["roster_id"]], "creator": r1["owner_id"], "status_updated": 1_760_000_100_000,
        },
        {
            "transaction_id": "tx-fa", "type": "free_agent", "status": "complete", "leg": week,
            "settings": None, "adds": {b_id: r1["roster_id"]}, "drops": None,
            "roster_ids": [r1["roster_id"]], "creator": r1["owner_id"], "status_updated": 1_760_000_200_000,
        },
        {
            "transaction_id": "tx-trade", "type": "trade", "status": "complete", "leg": max(1, week - 2),
            "settings": None,
            "adds": {r1["players"][0]: r2["roster_id"], r2["players"][0]: r1["roster_id"]},
            "drops": {r1["players"][0]: r1["roster_id"], r2["players"][0]: r2["roster_id"]},
            "draft_picks": [{
                "season": "2027", "round": 2, "roster_id": r2["roster_id"],
                "previous_owner_id": r2["roster_id"], "owner_id": r1["roster_id"],
            }],
            "roster_ids": [r1["roster_id"], r2["roster_id"]],
            "creator": r1["owner_id"], "status_updated": 1_759_000_000_000,
        },
    ]


def _make_traded_picks(season: str, teams: int) -> list[dict]:
    """Sleeper only lists picks that have actually changed hands."""
    next_season = str(int(season) + 1)
    return [
        {"season": next_season, "round": 2, "roster_id": min(3, teams), "previous_owner_id": min(3, teams), "owner_id": 1},
        {"season": next_season, "round": 1, "roster_id": 1, "previous_owner_id": 1, "owner_id": 2},
    ]


def make_storage(*leagues: SyntheticLeague, current_week: int | None = 3, season: str | None = None) -> FakeStorage:
    storage = FakeStorage()
    for synth in leagues:
        storage.add_league(synth)
    if current_week is not None:
        storage.set_meta("current_week", str(current_week))
    if season is not None:
        storage.set_meta("season", season)
    return storage


# ---------------------------------------------------------------------------
# the valuation engine, without a network
# ---------------------------------------------------------------------------


def make_snapshot(source: str, payload: Any, *, age_hours: float = 2.0) -> RankingSnapshot:
    return RankingSnapshot(
        source=source,
        fetched_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours),
        payload=payload,
    )


def _ktc_block(value: int, rank: int, pos_rank: int) -> dict:
    return {"value": value, "rank": rank, "positional_rank": pos_rank}


def build_ktc_payload(players: dict[str, dict]) -> list[dict]:
    """One row per skill player, ranked by the deterministic pool order, in
    the exact shape `ktc.parse_ktc_players` produces (`asdict(KTCPlayer)`)."""
    rows = []
    pos_counts: dict[str, int] = {}
    for rank, pid in enumerate(_sorted_skill_ids(players), start=1):
        p = players[pid]
        pos = p["position"]
        pos_counts[pos] = pos_counts.get(pos, 0) + 1
        value = max(200, 9000 - rank * 60)
        blocks = {
            "one_qb": _ktc_block(value, rank, pos_counts[pos]),
            "superflex": _ktc_block(value + 250, rank, pos_counts[pos]),
        }
        for tier in ("tep", "tepp", "teppp"):
            bump = {"tep": 100, "tepp": 200, "teppp": 300}[tier] if pos == "TE" else 0
            blocks[f"one_qb_{tier}"] = _ktc_block(value + bump, rank, pos_counts[pos])
            blocks[f"superflex_{tier}"] = _ktc_block(value + 250 + bump, rank, pos_counts[pos])
        rows.append({
            "name": p["full_name"], "position": pos, "team": p["team"], "age": p["age"],
            "is_rookie": p["years_exp"] == 0, **blocks,
        })
    return rows


def build_fp_payload(players: dict[str, dict], *, offset: int = 0) -> list[dict]:
    """`fantasypros.parse_fp_players` shape. `offset` rotates the ordering a
    little between pages so dynasty and redraft ECR aren't identical."""
    ordered = _sorted_skill_ids(players)
    if offset:
        ordered = ordered[offset:] + ordered[:offset]
    rows = []
    pos_counts: dict[str, int] = {}
    for rank, pid in enumerate(ordered, start=1):
        p = players[pid]
        pos = p["position"]
        pos_counts[pos] = pos_counts.get(pos, 0) + 1
        rows.append({
            "name": p["full_name"], "position": pos, "team": p["team"],
            "bye_week": 5 + (rank % 9),  # 5..13, never the current week in these tests
            "rank_ecr": rank, "pos_rank": f"{pos}{pos_counts[pos]}", "owned_avg": 90.0,
            "rank_std": 1.0 + rank * 0.02,  # deliberately under the panel-disagreement line
            "rank_min": max(1, rank - 2), "rank_max": rank + 2, "rank_ave": float(rank),
            "tier": 1 + rank // 12, "ecr_delta": 0.0,
        })
    return rows


def build_rb_payload(players: dict[str, dict], *, ppr: bool = True) -> list[dict]:
    """`rotoballer.parse_rb_players` shape — the only source of projections.
    K and DEF get rows too: the streamer planner and the lineup optimizer
    both need them projected."""
    rows = []
    ordered = _sorted_skill_ids(players)
    ordered += sorted(
        (pid for pid, p in players.items() if p.get("position") in ("K", "DEF")),
        key=lambda pid: (players[pid]["position"], pid),
    )
    for rank, pid in enumerate(ordered, start=1):
        p = players[pid]
        base = max(20.0, 300.0 - rank * 2.0)
        rows.append({
            "name": p["full_name"], "position": p["position"], "team": p["team"],
            "bye_week": 5 + (rank % 9), "rank": rank, "tier": 1 + rank // 12,
            "trend": ["rising", "no change", "down"][rank % 3],
            "proj_points_ppr": base if ppr else base * 0.85,
            "proj_points_standard": base * 0.85,
            "proj_points_te_premium": base * 1.1,
        })
    return rows


def make_engine(
    players: dict[str, dict], *, current_week: int | None = 3, age_hours: float = 2.0,
    ktc_rows: list[dict] | None = None, fp_rows: list[dict] | None = None, rb_rows: list[dict] | None = None,
) -> ValuationEngine:
    """A ValuationEngine with every source present and no fetch attempted.

    Passing all three snapshot arguments explicitly is what suppresses the
    `_FETCH` sentinel — omitting any one of them would send the constructor
    to the live scrapers. The `*_rows` overrides replace one source's rows
    everywhere (same rows on every page/sheet), which is how the adversarial
    tests inject None, negative or zero projections.
    """
    ktc = make_snapshot("ktc_dynasty", ktc_rows if ktc_rows is not None else build_ktc_payload(players), age_hours=age_hours)
    fp = {
        key: make_snapshot(
            f"fantasypros_{key}",
            fp_rows if fp_rows is not None else build_fp_payload(players, offset=i),
            age_hours=age_hours,
        )
        for i, key in enumerate(FANTASYPROS_PAGES)
    }
    rb = {
        key: make_snapshot(
            f"rotoballer_{key}",
            rb_rows if rb_rows is not None else build_rb_payload(players, ppr=key != "standard"),
            age_hours=age_hours,
        )
        for key in ROTOBALLER_SPREADSHEETS
    }
    return ValuationEngine(
        ktc_snapshot=ktc, fp_snapshots=fp, rb_snapshots=rb, ff_rows=[], current_week=current_week
    )


# ---------------------------------------------------------------------------
# keeping the report path off data/
# ---------------------------------------------------------------------------


def isolate_report_data(monkeypatch, *, snapshots: list[dict] | None = None, latest: dict | None = None) -> None:
    """`report_data` imports its persistence helpers by name, so the patch
    has to land in report_data's namespace, not the defining module's.
    Without this, a test run reads (and the watchlist/ledger paths would
    happily keep reading) the real `data/` directory."""
    import sleeper_tool.report_data as rd
    from sleeper_tool.decision_ledger import Ledger
    from sleeper_tool.watchlist import Watchlist

    history = list(snapshots or [])
    monkeypatch.setattr(rd, "load_watchlist", lambda *a, **k: Watchlist())
    monkeypatch.setattr(rd, "load_ledger", lambda *a, **k: Ledger())
    monkeypatch.setattr(rd, "load_snapshots", lambda *a, **k: list(history))
    monkeypatch.setattr(rd, "load_latest_snapshot", lambda *a, **k: latest)
    # The one non-ranking external fetch and the ranking-cache read both go
    # to disk/network; with_nfl_schedule=False covers the first, this the second.
    monkeypatch.setattr(rd, "load_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(rd, "ff_dynasty_status", lambda *a, **k: "absent (test)")
