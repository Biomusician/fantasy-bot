"""Decision Delta — "what changed since I last looked?"

A ten-league report repeats most of itself day to day. After every
successful COMPLETE run (every league synced, no per-league error),
daily_run.py persists a compact JSON snapshot of the decisions the report
was built on; the next run diffs against the latest snapshot and leads
the overview with only the meaningful movement:

  - a team's contender/middling/rebuild status changed
  - a player entered or left the top trade-target / waiver-target list
  - a rostered player's reconciled value moved by VALUATION_DELTA_RATIO
    or more (relative change in the currency value number — dynasty
    value or projected points — never rank positions, which mean very
    different things at rank 50 and rank 300)
  - a player joined or left one of my rosters

Only the latest SNAPSHOTS_KEPT snapshots are retained. A partial run
(a league failed to sync, or errored while building) never writes one,
so the baseline is always a run that was itself complete — otherwise the
next delta would report "N players joined your roster" for a league that
had merely been missing.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sleeper_tool.valuation import weekly_projection

logger = logging.getLogger(__name__)

VALUATION_DELTA_RATIO = 0.15
SNAPSHOTS_KEPT = 2
# Bump when the snapshot's meaning changes (e.g. what "value" is); an
# older-schema baseline is ignored rather than diffed into nonsense.
SNAPSHOT_SCHEMA = 2
DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "run_snapshots"

STATUS = "status"
RECOMMENDATION = "recommendation"
VALUATION = "valuation"
ROSTER = "roster"
_KIND_ORDER = {STATUS: 0, ROSTER: 1, RECOMMENDATION: 2, VALUATION: 3}


@dataclass
class DeltaItem:
    kind: str
    league_name: str
    text: str


@dataclass
class DecisionDelta:
    since: dt.datetime
    items: list[DeltaItem] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[DeltaItem]:
        return [i for i in self.items if i.kind == kind]


# -- snapshot shape -------------------------------------------------------------
# {
#   "generated_at": iso8601, "current_week": int|null,
#   "leagues": {league_id: {
#       "name": str, "team_status": str|null,
#       "trade_targets": {player_id: name}, "waiver_targets": {player_id: name},
#       "roster": {player_id: {"name": str, "value": float|null}},
#   }},
#   "best_moves": [headline, ...]
# }


def _stable_value(pv, currency: str, current_week: int | None) -> float | None:
    """A value that means the same thing on two different days. Dynasty
    value is a stable number; redraft's proj_points is a rest-of-season
    total that shrinks every week by construction (a week-13 vs week-12
    comparison is -17% for every player), so redraft uses the per-game
    projection instead."""
    if currency == "dynasty":
        return pv.dynasty_value
    return weekly_projection(pv, current_week)


def build_snapshot(report) -> dict[str, Any]:
    """`report` is a WeeklyReportData (duck-typed to avoid importing
    report_data here — report_data imports this module)."""
    leagues: dict[str, Any] = {}
    for ld in report.leagues:
        if ld.error or not ld.drafted or ld.roster is None:
            continue
        leagues[ld.league.league_id] = {
            "name": ld.league.name,
            "team_status": ld.team_status.status if ld.team_status else None,
            "trade_targets": {e.player_id: e.name for p in ld.proposals for e in p.receive},
            "waiver_targets": {t.player_id: t.name for t in ld.waiver_targets},
            "roster": {
                e.player_id: {"name": e.name, "value": _stable_value(e.value, ld.currency, report.current_week)}
                for e in ld.roster.entries
            },
        }
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": report.generated_at.isoformat(),
        "current_week": report.current_week,
        "leagues": leagues,
        "best_moves": [a.headline for a in report.priority_actions],
    }


def is_complete_run(report, sync_failures: int = 0) -> bool:
    """Every league synced, none errored, and no roster was built with a
    player missing from the player cache — a roster short one player
    would make the next delta report him as 'joined your roster'."""
    if sync_failures or any(ld.error for ld in report.leagues):
        return False
    return not any(ld.roster is not None and ld.roster.skipped_player_count for ld in report.leagues)


def _snapshot_date(snapshot: dict[str, Any]) -> str:
    return snapshot["generated_at"][:10]  # YYYY-MM-DD


def save_snapshot(snapshot: dict[str, Any], snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR) -> Path:
    """One file per UTC day, overwritten by a same-day re-run — so running
    the pipeline twice in a morning is idempotent and never unlinks the
    previous day's baseline. Only the SNAPSHOTS_KEPT newest days survive."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{_snapshot_date(snapshot).replace('-', '')}.json"
    path.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    for old in sorted(snapshot_dir.glob("*.json"))[:-SNAPSHOTS_KEPT]:
        old.unlink()
    return path


def load_latest_snapshot(snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR, *, before_date: str | None = None) -> dict[str, Any] | None:
    """The newest snapshot, skipping any from `before_date` (YYYY-MM-DD) —
    pass today so a same-day re-run still diffs against yesterday."""
    if not snapshot_dir.exists():
        return None
    for path in sorted(snapshot_dir.glob("*.json"), reverse=True):
        if before_date is not None and path.stem == before_date.replace("-", ""):
            continue
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable snapshot %s: %s", path, exc)
            return None
        if snapshot.get("schema") != SNAPSHOT_SCHEMA:
            logger.warning("Ignoring snapshot %s: schema %s, expected %s", path, snapshot.get("schema"), SNAPSHOT_SCHEMA)
            return None
        return snapshot
    return None


def _relative_move(old: float | None, new: float | None) -> float | None:
    if not old or new is None:
        return None
    return (new - old) / abs(old)


def compute_delta(previous: dict[str, Any] | None, current: dict[str, Any]) -> DecisionDelta | None:
    """Diff two snapshots (current is what THIS run would write). None when
    there's no prior complete run to compare against."""
    if previous is None:
        return None
    delta = DecisionDelta(since=dt.datetime.fromisoformat(previous["generated_at"]))
    prev_leagues = previous.get("leagues", {})
    for league_id, cur in current.get("leagues", {}).items():
        prev = prev_leagues.get(league_id)
        if prev is None:
            continue  # league wasn't in the last complete run — nothing honest to diff
        name = cur["name"]
        if prev.get("team_status") != cur.get("team_status") and cur.get("team_status"):
            delta.items.append(DeltaItem(STATUS, name, f"Team status {prev.get('team_status') or '?'} → {cur['team_status']}"))

        for label, key in (("trade target", "trade_targets"), ("waiver target", "waiver_targets")):
            before, after = prev.get(key, {}), cur.get(key, {})
            for pid, pname in after.items():
                if pid not in before:
                    delta.items.append(DeltaItem(RECOMMENDATION, name, f"New {label}: {pname}"))
            for pid, pname in before.items():
                if pid not in after:
                    delta.items.append(DeltaItem(RECOMMENDATION, name, f"No longer a {label}: {pname}"))

        prev_roster, cur_roster = prev.get("roster", {}), cur.get("roster", {})
        for pid, info in cur_roster.items():
            if pid not in prev_roster:
                delta.items.append(DeltaItem(ROSTER, name, f"Joined your roster: {info['name']}"))
                continue
            move = _relative_move(prev_roster[pid].get("value"), info.get("value"))
            if move is not None and abs(move) >= VALUATION_DELTA_RATIO:
                delta.items.append(DeltaItem(VALUATION, name, f"{info['name']} value {move:+.0%}"))
        for pid, info in prev_roster.items():
            if pid not in cur_roster:
                delta.items.append(DeltaItem(ROSTER, name, f"Left your roster: {info['name']}"))

    delta.items.sort(key=lambda i: (_KIND_ORDER.get(i.kind, 9), i.league_name))
    return delta
