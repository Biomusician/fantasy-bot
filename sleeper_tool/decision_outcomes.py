"""Decision Outcomes — what happened afterwards, stated as facts.

The Decision Ledger records that a recommendation was made and whether
Sleeper shows it was acted on. This module answers the next question —
"and then what?" — for the entries whose observation windows have actually
elapsed, using only series the tool already stores:

  - reconciled value, from the Decision Delta snapshot history (the same
    stable per-day number market_velocity reads: dynasty value, or the
    per-game projection in redraft, so a shrinking rest-of-season total
    never reads as a decline)
  - whether the player is on my roster now, and whether he reaches the
    optimized starting lineup, from the current report
  - a role label, only if a role source is handed in
  - fantasy points, only if a points source is handed in

Windows are OUTCOME_WINDOWS_WEEKS measured in days from the run that first
made the recommendation. A window that hasn't elapsed is reported as
pending, not guessed at; a window with no snapshot behind it is reported
as insufficient history, not zero.

The vocabulary is deliberately flat. Nothing here says a recommendation
was right, wrong, good, bad, won or lost. A player can rise after a
sell-high call for reasons the model never modelled, and one observation
is not evidence about a rule. What this can honestly say is "the value
moved -14% over three weeks, he is still on your roster, and he has not
reached your optimized lineup" — and that is all it says.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sleeper_tool.decision_ledger import (
    DROP,
    Ledger,
    LedgerEntry,
    _TRADE_ACTIONS,
    _as_datetime,
)

OUTCOME_WINDOWS_WEEKS = (1, 3, 6)
DAYS_PER_WEEK = 7

PENDING = "pending"
INSUFFICIENT_HISTORY = "insufficient history"
OBSERVED = "observed"

AS_IMPLIED = "moved in the direction the read implied"
AGAINST_IMPLIED = "moved against the direction the read implied"

SELL_HIGH = "trade_type:sell_high"
BUY_LOW = "trade_type:buy_low"


@dataclass
class OutcomeFact:
    fingerprint: str
    league_name: str
    action: str
    subject: str
    window_weeks: int
    window_days: int
    state: str  # PENDING | INSUFFICIENT_HISTORY | OBSERVED
    facts: tuple[str, ...] = ()
    value_move: float | None = None  # relative, add/drop actions
    give_move: float | None = None  # relative, trade give side
    receive_move: float | None = None  # relative, trade receive side
    projection_move: float | None = None  # redraft only (stable value is per-game points there)
    thesis_direction: str | None = None  # sell_high / buy_low only
    entered_lineup: bool | None = None
    still_rostered: bool | None = None
    role_movement: str | None = None
    points_total: float | None = None
    points_weeks: int | None = None
    team_status_then: str | None = None
    team_status_now: str | None = None
    lineup_delta_then: float | None = None
    lineup_delta_now: float | None = None

    def describe(self) -> str:
        head = f"[{self.league_name}] {self.action}: {self.subject} — {self.window_weeks}-week window"
        if not self.facts:
            return f"{head}: {self.state}"
        return f"{head}: " + "; ".join(self.facts)


# -- value series ---------------------------------------------------------------


def _value_series(snapshots: Sequence[dict[str, Any]]) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """(league_id, player_id) -> [(YYYY-MM-DD, stable value), ...] oldest
    first. Both snapshot buckets are read: `roster` covers players I hold,
    `tracked` covers the trade targets and waiver adds I don't."""
    series: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for snap in snapshots:
        date = (snap.get("generated_at") or "")[:10]
        if not date:
            continue
        for league_id, league in (snap.get("leagues") or {}).items():
            for bucket in (league.get("roster") or {}, league.get("tracked") or {}):
                for pid, row in bucket.items():
                    value = row.get("value") if isinstance(row, dict) else None
                    if value is None:
                        continue
                    series.setdefault((str(league_id), str(pid)), []).append((date, float(value)))
    for key in series:
        series[key] = sorted(series[key])
    return series


def _value_at(points: list[tuple[str, float]], *, start: str, end: str) -> float | None:
    """The latest observation inside [start, end]."""
    found = None
    for date, value in points:
        if start <= date <= end:
            found = value
    return found


def _relative(base: float | None, later: float | None) -> float | None:
    if not base or later is None:
        return None
    return (later - base) / abs(base)


def _baseline(entry: LedgerEntry, pid: str, points: list[tuple[str, float]], start: str) -> float | None:
    """What the player was worth when the recommendation was made — the
    entry's own snapshot first (it is the record of the decision), the
    first stored observation on or after that day as a fallback."""
    recorded = entry.valuation_snapshot.get(pid)
    if recorded is not None:
        return float(recorded)
    for date, value in points:
        if date >= start:
            return value
    return None


def _set_move(
    entry: LedgerEntry,
    pids: Iterable[str],
    series: dict[tuple[str, str], list[tuple[str, float]]],
    *,
    start: str,
    end: str,
) -> float | None:
    """Relative move of a whole set of players (a trade side), summing the
    stable values on both days. A side is only comparable when every
    player in it has a value on both days — a partially-priced set would
    read as a collapse."""
    base_total = later_total = 0.0
    any_player = False
    for pid in pids:
        points = series.get((entry.league_id, str(pid)), [])
        base = _baseline(entry, str(pid), points, start)
        later = _value_at(points, start=start, end=end)
        if base is None or later is None:
            return None
        base_total += base
        later_total += later
        any_player = True
    if not any_player:
        return None
    return _relative(base_total, later_total)


# -- fact building ---------------------------------------------------------------


def _league_index(report) -> dict[str, Any]:
    if report is None:
        return {}
    return {ld.league.league_id: ld for ld in report.leagues if not ld.error}


def _lineup_and_roster(ld) -> tuple[frozenset[str], frozenset[str]]:
    starters = ld.lineup.starter_ids if getattr(ld, "lineup", None) is not None else frozenset()
    rostered = frozenset(e.player_id for e in ld.roster.entries) if getattr(ld, "roster", None) is not None else frozenset()
    return frozenset(starters), rostered


def _points(points_by_player_week: dict | None, pids: Iterable[str]) -> tuple[float | None, int | None]:
    if not points_by_player_week:
        return None, None
    total = 0.0
    weeks = 0
    for pid in pids:
        for value in (points_by_player_week.get(str(pid)) or {}).values():
            if value is None:
                continue
            total += float(value)
            weeks += 1
    if not weeks:
        return None, None
    return total, weeks


def _thesis(entry: LedgerEntry, give_move: float | None, receive_move: float | None) -> tuple[str | None, str | None, float | None]:
    """(label text, direction, the move it read) for a sell-high or buy-low
    proposal. Sell-high implies the piece I'd have sent is priced above
    where it is heading; buy-low implies the piece I'd have received is
    priced below. Either can be wrong for reasons no model saw, so this
    reports the direction and nothing else."""
    labels = set(entry.reason_labels)
    if SELL_HIGH in labels and give_move is not None:
        direction = AS_IMPLIED if give_move < 0 else AGAINST_IMPLIED
        return f"sell-high read: the piece you'd have sent is {give_move:+.0%} since, {direction}", direction, give_move
    if BUY_LOW in labels and receive_move is not None:
        direction = AS_IMPLIED if receive_move > 0 else AGAINST_IMPLIED
        return f"buy-low read: the piece you'd have received is {receive_move:+.0%} since, {direction}", direction, receive_move
    return None, None, None


def build_outcome_facts(
    ledger: Ledger,
    snapshots: Sequence[dict[str, Any]],
    *,
    now: dt.datetime,
    report=None,
    role_labels: dict[str, str] | None = None,
    points_by_player_week: dict | None = None,
    lineup_delta_now: dict[str, float] | None = None,
) -> list[OutcomeFact]:
    """One OutcomeFact per (ledger entry, window). Deterministic order:
    entries oldest-first by first_seen then fingerprint, windows ascending.

    `report` is duck-typed (a WeeklyReportData) and optional — without it
    the lineup and roster facts are simply absent rather than invented.
    `role_labels` and `points_by_player_week` are equally optional; this
    module never derives either.
    """
    series = _value_series(snapshots)
    leagues = _league_index(report)
    roles = role_labels or {}
    out: list[OutcomeFact] = []

    for entry in ledger.ordered():
        first_seen = _as_datetime(entry.run_id)
        if first_seen is None:
            continue
        start = first_seen.date().isoformat()
        elapsed = (now.date() - first_seen.date()).days
        ld = leagues.get(entry.league_id)
        starters, rostered = _lineup_and_roster(ld) if ld is not None else (None, None)

        for weeks in OUTCOME_WINDOWS_WEEKS:
            window_days = weeks * DAYS_PER_WEEK
            end = (first_seen.date() + dt.timedelta(days=window_days)).isoformat()
            fact = OutcomeFact(
                fingerprint=entry.fingerprint,
                league_name=entry.league_name,
                action=entry.action,
                subject=entry.subject,
                window_weeks=weeks,
                window_days=window_days,
                state=PENDING,
            )
            if elapsed < window_days:
                fact.facts = (f"window not reached yet ({elapsed} of {window_days} days since the recommendation)",)
                out.append(fact)
                continue
            _fill_window(
                fact,
                entry,
                series,
                start=start,
                end=end,
                starters=starters,
                rostered=rostered,
                roles=roles,
                points_by_player_week=points_by_player_week,
                ld=ld,
                lineup_delta_now=lineup_delta_now,
            )
            out.append(fact)
    return out


def _fill_window(
    fact: OutcomeFact,
    entry: LedgerEntry,
    series: dict[tuple[str, str], list[tuple[str, float]]],
    *,
    start: str,
    end: str,
    starters: frozenset[str] | None,
    rostered: frozenset[str] | None,
    roles: dict[str, str],
    points_by_player_week: dict | None,
    ld,
    lineup_delta_now: dict[str, float] | None,
) -> None:
    facts: list[str] = []
    if entry.action in _TRADE_ACTIONS:
        fact.give_move = _set_move(entry, entry.give_ids, series, start=start, end=end)
        fact.receive_move = _set_move(entry, entry.receive_ids, series, start=start, end=end)
        if fact.give_move is None and fact.receive_move is None:
            fact.state = INSUFFICIENT_HISTORY
            facts.append(f"{INSUFFICIENT_HISTORY} for the assets on either side of this offer")
        else:
            fact.state = OBSERVED
            if fact.give_move is not None:
                facts.append(f"the players you'd have sent are {fact.give_move:+.0%} in reconciled value")
            if fact.receive_move is not None:
                facts.append(f"the players you'd have received are {fact.receive_move:+.0%}")
        if entry.give_picks or entry.receive_picks:
            facts.append("draft picks in this offer carry no stored value series and are not counted")
        text, direction, _ = _thesis(entry, fact.give_move, fact.receive_move)
        if text:
            fact.thesis_direction = direction
            facts.append(text)
        if entry.currency == "redraft":
            # In redraft the stable value IS the per-game projection, so the
            # same series answers "did the projection move" directly.
            fact.projection_move = fact.receive_move if fact.receive_move is not None else fact.give_move
            if fact.projection_move is not None:
                facts.append(f"per-game projection {fact.projection_move:+.0%} (redraft currency)")
        fact.lineup_delta_then = entry.projected_lineup_delta
        if lineup_delta_now is not None:
            fact.lineup_delta_now = lineup_delta_now.get(entry.fingerprint)
        if fact.lineup_delta_then is not None:
            tail = f", now {fact.lineup_delta_now:+.1f}/wk" if fact.lineup_delta_now is not None else ""
            facts.append(f"previewed at {fact.lineup_delta_then:+.1f}/wk when recommended{tail}")
        fact.team_status_then = entry.team_status
        fact.team_status_now = ld.team_status.status if ld is not None and ld.team_status else None
        if fact.team_status_then and fact.team_status_now and fact.team_status_then != fact.team_status_now:
            facts.append(f"your team status went {fact.team_status_then} -> {fact.team_status_now}")
        _role_fact(fact, entry, entry.player_ids, roles, facts)
    else:
        pids = entry.give_ids if entry.action == DROP else (entry.receive_ids or entry.player_ids)
        fact.value_move = _set_move(entry, pids, series, start=start, end=end)
        if fact.value_move is None:
            fact.state = INSUFFICIENT_HISTORY
            facts.append(f"{INSUFFICIENT_HISTORY} for {fact.subject}")
        else:
            fact.state = OBSERVED
            facts.append(f"reconciled value {fact.value_move:+.0%} over the window")
        if entry.currency == "redraft" and fact.value_move is not None:
            fact.projection_move = fact.value_move
        if starters is not None:
            fact.entered_lineup = any(str(p) in starters for p in pids)
            facts.append("has since reached your optimized lineup" if fact.entered_lineup else "has not reached your optimized lineup")
        if rostered is not None:
            fact.still_rostered = any(str(p) in rostered for p in pids)
            if entry.action == DROP:
                facts.append("still on your roster" if fact.still_rostered else "no longer on your roster")
            else:
                facts.append("on your roster now" if fact.still_rostered else "not on your roster")
        _role_fact(fact, entry, pids, roles, facts)
        total, weeks = _points(points_by_player_week, pids)
        if total is not None:
            fact.points_total = total
            fact.points_weeks = weeks
            facts.append(f"{total:.1f} fantasy points recorded across {weeks} player-week(s)")
    fact.facts = tuple(facts)


def _role_fact(fact: OutcomeFact, entry: LedgerEntry, pids: Iterable[str], roles: dict[str, str], facts: list[str]) -> None:
    label = entry.role_signal
    if label is None:
        for pid in pids:
            label = roles.get(str(pid))
            if label:
                break
    if label:
        fact.role_movement = label
        facts.append(f"role: {label}")


def outcomes_summary(facts: Sequence[OutcomeFact]) -> dict[str, dict[str, int]]:
    """Counts by action -> "<weeks>w <state>", for a diagnostics block.
    Deterministic ordering (both levels sorted)."""
    out: dict[str, dict[str, int]] = {}
    for fact in facts:
        key = f"{fact.window_weeks}w {fact.state}"
        bucket = out.setdefault(fact.action, {})
        bucket[key] = bucket.get(key, 0) + 1
    return {action: dict(sorted(counts.items())) for action, counts in sorted(out.items())}
