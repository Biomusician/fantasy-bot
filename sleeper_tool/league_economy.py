"""League Economy Map — each league's own transaction economy, from the
current season's completed transactions and pick ownership: who actually
trades, who is accumulating or liquidating draft picks, and who
stockpiles a position. Consensus value is only a baseline; these local
distortions are where negotiation leverage actually lives.

Descriptive labels only, per manager, never extrapolated beyond the
observed sample:
  Frequent Trader   FREQUENT_TRADER_MIN_TRADES+ completed trades this season
  Inactive Trader   zero completed trades this season
  Pick Accumulator  net future picks vs original ownership >= +PICK_NET_THRESHOLD
  Pick Seller       net future picks <= -PICK_NET_THRESHOLD
  Position Heavy    roster count at QB/RB/WR/TE at least POSITION_HEAVY_RATIO x
                    the league median AND at least POSITION_HEAVY_MIN_ABOVE
                    players above it
If the league has fewer than MIN_LEAGUE_TRADES_FOR_ACTIVITY completed
trades in total, the trader-activity labels are suppressed entirely and
the league is marked as a limited sample — three quiet weeks in August
say nothing about anyone.

v1 is current-season only. Labels feed trade rationale text and the
per-league manager table; they do NOT touch acceptance-rating buckets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.valuation import CORE_SKILL_POSITIONS

FREQUENT_TRADER_MIN_TRADES = 3
PICK_NET_THRESHOLD = 1
POSITION_HEAVY_RATIO = 1.5
POSITION_HEAVY_MIN_ABOVE = 2
MIN_LEAGUE_TRADES_FOR_ACTIVITY = 3

FREQUENT_TRADER = "Frequent Trader"
INACTIVE_TRADER = "Inactive Trader"
PICK_ACCUMULATOR = "Pick Accumulator"
PICK_SELLER = "Pick Seller"
POSITION_HEAVY = "Position Heavy"


@dataclass
class ManagerEconomy:
    roster_id: int
    username: str | None
    team_name: str | None
    completed_trades: int
    net_future_picks: int
    heavy_positions: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = []
        for label in self.labels:
            if label in (FREQUENT_TRADER, INACTIVE_TRADER):
                bits.append(f"{label} ({self.completed_trades} completed trade{'s' if self.completed_trades != 1 else ''} this season)")
            elif label in (PICK_ACCUMULATOR, PICK_SELLER):
                n = self.net_future_picks
                bits.append(f"{label} (net {n:+d} future pick{'s' if abs(n) != 1 else ''})")
            elif label == POSITION_HEAVY:
                bits.append(f"{label} at {'/'.join(self.heavy_positions)}")
        return "; ".join(bits)


@dataclass
class LeagueEconomy:
    total_completed_trades: int
    limited_sample: bool
    managers: dict[int, ManagerEconomy]  # by roster_id

    def labelled(self) -> list[ManagerEconomy]:
        return [m for m in self.managers.values() if m.labels]


def completed_trades_by_roster(transactions: list[dict]) -> dict[int, int]:
    """Counts each roster's completed trades from raw Sleeper transaction
    payloads (any week). A trade lists every participating roster in
    roster_ids; each participant gets credit for one trade."""
    counts: dict[int, int] = {}
    for tx in transactions:
        if tx.get("type") != "trade" or tx.get("status") != "complete":
            continue
        for rid in tx.get("roster_ids") or []:
            counts[rid] = counts.get(rid, 0) + 1
    return counts


def net_future_picks_by_roster(traded_picks: list[dict], *, season: str) -> dict[int, int]:
    """+1 to the current owner and -1 to the original owner for every
    traded pick in `season` or later. Untraded picks are still with their
    original team and net to zero, so only the traded list matters."""
    net: dict[int, int] = {}
    for tp in traded_picks:
        if str(tp.get("season", "")) < str(season):
            continue
        original, owner = tp.get("roster_id"), tp.get("owner_id")
        if original is None or owner is None or original == owner:
            continue
        net[owner] = net.get(owner, 0) + 1
        net[original] = net.get(original, 0) - 1
    return net


def heavy_positions_by_roster(rosters: dict[int, ValuedRoster]) -> dict[int, list[str]]:
    counts = {rid: {pos: len(r.by_position(pos)) for pos in CORE_SKILL_POSITIONS} for rid, r in rosters.items()}
    heavy: dict[int, list[str]] = {rid: [] for rid in rosters}
    for pos in CORE_SKILL_POSITIONS:
        league_median = median(c[pos] for c in counts.values()) if counts else 0
        for rid, c in counts.items():
            if c[pos] >= POSITION_HEAVY_RATIO * league_median and c[pos] - league_median >= POSITION_HEAVY_MIN_ABOVE:
                heavy[rid].append(pos)
    return heavy


def build_league_economy(
    rosters: dict[int, ValuedRoster], transactions: list[dict], traded_picks: list[dict], *, season: str
) -> LeagueEconomy:
    trades = completed_trades_by_roster(transactions)
    net_picks = net_future_picks_by_roster(traded_picks, season=season)
    heavy = heavy_positions_by_roster(rosters)
    total_trades = sum(1 for tx in transactions if tx.get("type") == "trade" and tx.get("status") == "complete")
    limited = total_trades < MIN_LEAGUE_TRADES_FOR_ACTIVITY

    managers: dict[int, ManagerEconomy] = {}
    for rid, roster in rosters.items():
        m = ManagerEconomy(
            roster_id=rid, username=roster.owner_username, team_name=roster.team_name,
            completed_trades=trades.get(rid, 0), net_future_picks=net_picks.get(rid, 0), heavy_positions=heavy.get(rid, []),
        )
        if not limited:
            if m.completed_trades >= FREQUENT_TRADER_MIN_TRADES:
                m.labels.append(FREQUENT_TRADER)
            elif m.completed_trades == 0:
                m.labels.append(INACTIVE_TRADER)
        if m.net_future_picks >= PICK_NET_THRESHOLD:
            m.labels.append(PICK_ACCUMULATOR)
        elif m.net_future_picks <= -PICK_NET_THRESHOLD:
            m.labels.append(PICK_SELLER)
        if m.heavy_positions:
            m.labels.append(POSITION_HEAVY)
        managers[rid] = m
    return LeagueEconomy(total_completed_trades=total_trades, limited_sample=limited, managers=managers)
