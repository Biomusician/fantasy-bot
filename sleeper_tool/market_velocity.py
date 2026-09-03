"""Market Velocity — is a player's reconciled value moving, and how fast?

Reads the Decision Delta snapshot history (one per UTC day, up to
decision_delta.SNAPSHOTS_KEPT days) plus this run's own values, and
labels the direction of travel for the players a recommendation touches.
Values are the snapshot's stable currency value (dynasty value, or the
per-game projection for redraft) so a rest-of-season total shrinking by
construction never reads as "falling".

  Insufficient History  fewer than MIN_OBSERVATIONS days seen
  Stable                anything not below
  Rising / Falling      total move >= DIRECTIONAL_MIN_MOVE in that direction
                        with at least MIN_CONSECUTIVE_MOVES consecutive
                        non-zero day-to-day moves the same way (flat days
                        neither count nor break the run)
  Unmeasurable          no positive base value to measure a move from
  Rapidly Rising /      total move >= RAPID_MIN_MOVE and EVERY non-zero
  Rapidly Falling       day-to-day move in that direction

No regression, no forecast: this says what the last few weeks did, not
what the next will do. Annotations only, and only on actionable players
(trade pieces, waiver targets, drop candidates).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sleeper_tool.decision_delta import _stable_value

MIN_OBSERVATIONS = 3
DIRECTIONAL_MIN_MOVE = 0.08
RAPID_MIN_MOVE = 0.15
MIN_CONSECUTIVE_MOVES = 2

INSUFFICIENT_HISTORY = "Insufficient History"
UNMEASURABLE = "Unmeasurable"
STABLE = "Stable"
RISING = "Rising"
RAPIDLY_RISING = "Rapidly Rising"
FALLING = "Falling"
RAPIDLY_FALLING = "Rapidly Falling"
_NOTABLE = {RISING, RAPIDLY_RISING, FALLING, RAPIDLY_FALLING}


@dataclass
class Velocity:
    label: str
    observations: int
    total_move: float | None  # relative, first -> last
    first_date: str | None
    last_date: str | None

    @property
    def notable(self) -> bool:
        return self.label in _NOTABLE

    @property
    def rising(self) -> bool:
        return self.label in (RISING, RAPIDLY_RISING)

    @property
    def falling(self) -> bool:
        return self.label in (FALLING, RAPIDLY_FALLING)

    def describe(self) -> str:
        if self.label == UNMEASURABLE:
            return f"{self.label} (no positive base value in {self.observations} observations)"
        if self.label == INSUFFICIENT_HISTORY or self.total_move is None:
            return f"{self.label} ({self.observations} of {MIN_OBSERVATIONS} observations)"
        return f"{self.label} ({self.total_move:+.0%} over {self.observations} observations since {self.first_date})"


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _longest_run(moves: list[float], direction: int) -> int:
    best = run = 0
    for m in moves:
        run = run + 1 if _sign(m) == direction else 0
        best = max(best, run)
    return best


def classify_velocity(observations: list[tuple[str, float]]) -> Velocity:
    """`observations`: (date, value) pairs, oldest first, one per day."""
    n = len(observations)
    if n < MIN_OBSERVATIONS:
        return Velocity(INSUFFICIENT_HISTORY, n, None, observations[0][0] if observations else None, observations[-1][0] if observations else None)
    first_date, first = observations[0]
    last_date, last = observations[-1]
    if first <= 0:
        return Velocity(UNMEASURABLE, n, None, first_date, last_date)
    total = (last - first) / first
    direction = _sign(total)
    moves = [b - a for (_, a), (_, b) in zip(observations, observations[1:]) if b != a]
    label = STABLE
    if direction and abs(total) >= RAPID_MIN_MOVE and moves and all(_sign(m) == direction for m in moves):
        label = RAPIDLY_RISING if direction > 0 else RAPIDLY_FALLING
    elif direction and abs(total) >= DIRECTIONAL_MIN_MOVE and _longest_run(moves, direction) >= MIN_CONSECUTIVE_MOVES:
        label = RISING if direction > 0 else FALLING
    return Velocity(label, n, total, first_date, last_date)


def _snapshot_values(snapshot: dict[str, Any], league_id: str) -> dict[str, float]:
    league = (snapshot.get("leagues") or {}).get(league_id) or {}
    values: dict[str, float] = {}
    for bucket in (league.get("roster") or {}, league.get("tracked") or {}):
        for pid, row in bucket.items():
            v = row.get("value") if isinstance(row, dict) else None
            if v is not None:
                values[pid] = float(v)
    return values


def current_observations(ld, current_week: int | None) -> dict[str, float]:
    """This run's stable values for the players a recommendation touches
    (plus the roster), in the same unit the snapshots store."""
    values: dict[str, float] = {}
    entries = list(ld.roster.entries) if ld.roster is not None else []
    entries += [e for p in ld.proposals for e in (*p.give, *p.receive)]
    entries += [d.entry for d in ld.drop_candidates]
    for e in entries:
        v = _stable_value(e.value, ld.currency, current_week)
        if v is not None:
            values[e.player_id] = float(v)
    for t in ld.waiver_targets:
        if t.value is not None:
            v = _stable_value(t.value, ld.currency, current_week)
            if v is not None:
                values[t.player_id] = float(v)
    return values


def actionable_ids(ld) -> set[str]:
    ids = {e.player_id for p in ld.proposals for e in (*p.give, *p.receive)}
    ids |= {t.player_id for t in ld.waiver_targets}
    ids |= {d.entry.player_id for d in ld.drop_candidates}
    return ids


def build_velocities(
    history: list[dict[str, Any]], ld, *, current_week: int | None, today: str
) -> dict[str, Velocity]:
    """Velocity for each actionable player in a league, from the stored
    snapshots (oldest first) plus this run's values dated `today`. A
    stored snapshot from `today` (a same-day re-run) is superseded by
    this run's values rather than counted twice."""
    league_id = ld.league.league_id
    series: dict[str, list[tuple[str, float]]] = {}
    for snap in history:
        date = (snap.get("generated_at") or "")[:10]
        if not date or date == today:
            continue
        for pid, v in _snapshot_values(snap, league_id).items():
            series.setdefault(pid, []).append((date, v))
    for pid, v in current_observations(ld, current_week).items():
        series.setdefault(pid, []).append((today, v))
    out: dict[str, Velocity] = {}
    for pid in actionable_ids(ld):
        obs = sorted(series.get(pid, []))
        out[pid] = classify_velocity(obs)
    return out


def annotate_league(ld, velocities: dict[str, Velocity]) -> None:
    """Notable velocity on the recommendation itself: favourable when it
    points the way the move already goes, otherwise a caveat."""
    for p in ld.proposals:
        for e in p.give:
            v = velocities.get(e.player_id)
            if v is None or not v.notable:
                continue
            text = f"Market velocity: {e.name} is {v.describe()}"
            (p.rationale_for_me if v.falling else p.caveats).append(
                f"{text} — selling into a falling market beats holding it." if v.falling else f"{text} — you'd be selling a rising asset."
            )
        for e in p.receive:
            v = velocities.get(e.player_id)
            if v is None or not v.notable:
                continue
            text = f"Market velocity: {e.name} is {v.describe()}"
            (p.rationale_for_me if v.rising else p.caveats).append(
                f"{text} — the market is moving toward you." if v.rising else f"{text} — check why before paying today's price."
            )
    for t in ld.waiver_targets:
        v = velocities.get(t.player_id)
        if v is not None and v.notable:
            t.notes.append(f"Market velocity: {v.describe()}")
    for d in ld.drop_candidates:
        v = velocities.get(d.entry.player_id)
        if v is not None and v.notable:
            d.reasons.append(f"market velocity {v.describe()}" + (" — a rising player is worth a second look before cutting" if v.rising else ""))
