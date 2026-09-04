"""Historical replay — run today's heuristics against prior states and
describe what they did. Development only; nothing here feeds a report.

The question this answers is narrow on purpose: *given the state the tool
would actually have seen at some earlier point, what did today's rules
say, and what happened next?* It is not a backtest in the trading sense
and it produces no accuracy number. Three things make an honest answer
possible, and each is enforced here rather than trusted:

  **No leakage into the input.** Every replayed label is computed from a
  `UsageData` truncated to weeks <= W (`truncate_usage`). A player whose
  snap share triples in W+1 cannot influence the label at W, because the
  W+1 row is not in the object the labeller is handed.

  **The forward window is evaluation, never input.** Weeks W+1..W+
  FORWARD_WEEKS are read from the full season to describe what followed.
  That is allowed precisely because the label has already been fixed.

  **No fabricated history.** Historical KeepTradeCut / FantasyPros values
  do not exist — the caches hold one day's snapshot, not a series — and
  reconstructing them backwards from today's numbers would be inventing
  the very thing under test. So value-based rules are replayed only over
  the daily snapshots that were genuinely recorded, and where an input
  does not exist for a period, that component is *skipped and named*
  rather than approximated.

Three modes, each a pure function over already-loaded data:

  `replay_role_signals`   the cached 2025 season, week by week
  `replay_snapshots`      data/run_snapshots/*.json (velocity + delta)
  `replay_outcomes`       data/decision_ledger/ledger.json

**What mode 1 is and is not.** The 2025 usage file is the same data the
role thresholds were smoke-tested against while they were being written.
Running those thresholds over it describes their *behaviour* — how often
each label fires, what the share did afterwards, which cases look wrong —
and cannot validate them. Read the counts as a description of the rule,
not as evidence that the rule is right.

Vocabulary follows `calibration.py`: counts, labels, distributions and
concrete examples. No percentages of correctness, no scores, no tuning.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sleeper_tool import decision_delta as dd
from sleeper_tool import market_velocity as mv
from sleeper_tool import role_trends as rt
from sleeper_tool.decision_ledger import (
    ACQUIRED_BY_ANOTHER,
    COMPLETED,
    OBSERVATION_WINDOW_DAYS,
    Ledger,
    LedgerEntry,
    summary as ledger_summary,
)
from sleeper_tool.decision_outcomes import OUTCOME_WINDOWS_WEEKS
from sleeper_tool.nfl_usage import UsageData
from sleeper_tool.role_analysis import MEDIUM_WINDOW, played_rows, team_for_week, window_from_rows

# -- mode 1 constants ----------------------------------------------------------

# Week 3 is the first week a three-game baseline can exist at all; 18 is
# the last regular-season week, and is still labelled even though it has
# no forward window (its cases count toward the label distribution only).
FIRST_REPLAY_WEEK = 3
LAST_REPLAY_WEEK = 18
FORWARD_WEEKS = 3

# The share measured at W is the last MEDIUM_WINDOW played games through
# W — role_analysis's own "last 3" window, so the number the replay
# compares against is one the tool already computes.
BASELINE_GAMES = MEDIUM_WINDOW

# How far the forward opportunity share must move to be called a move.
# Anchored to the model's own opportunity-share component threshold: a
# forward move this big is exactly a move role_trends would itself have
# called a signal, so the yardstick is not a new invention.
FORWARD_MOVE_THRESHOLD = rt.OPPORTUNITY_SHARE_RISE

# A player the rules said nothing useful about who then gained this much
# opportunity share is a missed breakout. Deliberately larger than
# FORWARD_MOVE_THRESHOLD: "the rule was quiet through a small drift" is
# not interesting, "the rule was quiet through ten share points" is.
MISSED_BREAKOUT_MIN_CHANGE = 0.10

MAX_EXAMPLES = 5

# Forward outcomes. Named by what the share DID, not by whether the label
# was vindicated — "reverted" means opposite things for a Surging and a
# Collapsing player, and one word cannot carry both.
SHARE_UP = "share up"
SHARE_HELD = "share held"
SHARE_DOWN = "share down"
NO_GAMES = "no games"  # his team played and he did not appear
NOT_MEASURABLE = "not measurable"  # no forward games scheduled, or no share to compare
FORWARD_OUTCOMES = (SHARE_UP, SHARE_HELD, SHARE_DOWN, NO_GAMES, NOT_MEASURABLE)

# Which forward outcome continues each label's story. Stable and
# Insufficient have no story to continue and are absent on purpose.
CONTINUATION = {
    rt.RISING: SHARE_UP,
    rt.SURGING: SHARE_UP,
    rt.FALLING: SHARE_DOWN,
    rt.COLLAPSING: SHARE_DOWN,
}
CONTRADICTION = {
    rt.RISING: SHARE_DOWN,
    rt.SURGING: SHARE_DOWN,
    rt.FALLING: SHARE_UP,
    rt.COLLAPSING: SHARE_UP,
}
LABEL_ORDER = (rt.SURGING, rt.RISING, rt.STABLE, rt.FALLING, rt.COLLAPSING, rt.INSUFFICIENT)
QUIET_LABELS = (rt.STABLE, rt.INSUFFICIENT)


def _at_least(value: float, threshold: float) -> bool:
    """Inclusive of the boundary despite float noise — the same rule
    role_trends applies to its own thresholds."""
    return value >= threshold - rt.THRESHOLD_EPSILON


# -- records -------------------------------------------------------------------


@dataclass(frozen=True)
class RoleCase:
    """One (player, week) replay: the label computed from truncated data,
    and what the following weeks did."""
    week: int
    gsis_id: str
    name: str | None
    position: str | None
    team: str | None
    label: str
    games: int
    share_at: float | None  # opportunity share over the last BASELINE_GAMES through W
    forward_share: float | None
    forward_change: float | None
    forward_games: int
    forward_scheduled_weeks: tuple[int, ...]
    snap_at: float | None
    forward_snap: float | None
    opportunities_at: float | None  # targets + carries per game
    forward_opportunities: float | None
    outcome: str

    @property
    def who(self) -> str:
        pos = f" ({self.position}, {self.team})" if self.position or self.team else ""
        return f"{self.name or self.gsis_id}{pos}"

    def describe(self) -> str:
        head = f"W{self.week} {self.who}: {self.label}, {self.games} games"
        if self.share_at is None or self.forward_share is None:
            return f"{head} — {self.outcome}"
        span = _week_span(self.forward_scheduled_weeks)
        move = f"opportunity share {self.share_at:.0%} → {self.forward_share:.0%} ({_points(self.forward_change)})"
        snaps = ""
        if self.snap_at is not None and self.forward_snap is not None:
            snaps = f", snaps {self.snap_at:.0%} → {self.forward_snap:.0%}"
        opps = ""
        if self.opportunities_at is not None and self.forward_opportunities is not None:
            opps = f", {self.opportunities_at:.1f} → {self.forward_opportunities:.1f} opp/g"
        return f"{head} — {move}{snaps}{opps} over {span}"


@dataclass(frozen=True)
class Distribution:
    n: int
    minimum: float
    p25: float
    median: float
    p75: float
    maximum: float


@dataclass(frozen=True)
class LabelSummary:
    label: str
    cases: int
    measurable: int
    outcomes: dict[str, int]
    change: Distribution | None


@dataclass
class RoleReplay:
    season: int
    weeks: tuple[int, ...]
    forward_weeks: int
    cases: list[RoleCase] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_label(self) -> list[LabelSummary]:
        return [s for s in (_label_summary(self.cases, label) for label in LABEL_ORDER) if s.cases]

    def label_counts_by_week(self) -> dict[int, Counter]:
        out: dict[int, Counter] = {w: Counter() for w in self.weeks}
        for case in self.cases:
            out.setdefault(case.week, Counter())[case.label] += 1
        return out


@dataclass
class SnapshotReplay:
    snapshot_dates: tuple[str, ...]
    league_ids: tuple[str, ...]
    series_count: int
    velocity_labels: dict[str, int] = field(default_factory=dict)
    delta_pairs: list[tuple[str, str, dict[str, int]]] = field(default_factory=list)
    delta_examples: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def snapshots(self) -> int:
        return len(self.snapshot_dates)


@dataclass(frozen=True)
class OutcomeCase:
    fingerprint: str
    league_name: str
    action: str
    subject: str
    tier: str | None
    outcome: str
    outcome_detail: str | None
    recorded_value: float | None
    lineup_delta: float | None
    scarcity: str
    team_status: str | None
    faab_pct: int | None
    role_signal: str | None
    currency: str | None
    first_seen: str
    age_days: int | None

    def describe(self) -> str:
        bits = [f"[{self.league_name}] {self.action}: {self.subject}"]
        if self.tier:
            bits.append(f"tier {self.tier}")
        if self.recorded_value is not None:
            bits.append(f"recorded value {self.recorded_value:.1f}")
        if self.lineup_delta is not None:
            bits.append(f"previewed lineup {self.lineup_delta:+.1f}/wk")
        if self.scarcity:
            bits.append(self.scarcity)
        if self.team_status:
            bits.append(f"as {self.team_status}")
        if self.faab_pct is not None:
            bits.append(f"suggested {self.faab_pct}% FAAB")
        if self.role_signal:
            bits.append(f"role {self.role_signal}")
        tail = self.outcome + (f" ({self.outcome_detail})" if self.outcome_detail else "")
        age = f", {self.age_days}d old" if self.age_days is not None else ""
        return " — ".join([" · ".join(bits), f"{tail}{age}"])


@dataclass
class OutcomeReplay:
    entries: int
    with_outcome: int
    by_action_outcome: dict[str, dict[str, int]] = field(default_factory=dict)
    terminal: list[OutcomeCase] = field(default_factory=list)
    resolved: list[OutcomeCase] = field(default_factory=list)
    horizons: dict[str, tuple[int, int]] = field(default_factory=dict)  # label -> (elapsed, total)
    oldest_age_days: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class BacktestResult:
    generated_at: dt.datetime
    role: RoleReplay | None = None
    snapshot: SnapshotReplay | None = None
    outcome: OutcomeReplay | None = None
    unavailable: list[str] = field(default_factory=list)


# -- mode 1: role-signal replay over a cached season ---------------------------


def truncate_usage(usage: UsageData, week: int) -> UsageData:
    """The season as it looked after `week` — the no-leakage primitive.

    Rebuilds a `UsageData` from the rows dated <= week only, so nothing
    downstream can reach a future row even by accident. `latest_week` is
    recomputed from the surviving played rows, because role_trends treats
    a falsy latest_week as "no season yet".
    """
    player_weeks = [r for r in usage.player_weeks if r.week <= week]
    team_weeks = [t for t in usage.team_weeks if t.week <= week]
    played = [r.week for r in player_weeks if r.played]
    return UsageData(
        season=usage.season,
        fetched_at=usage.fetched_at,
        latest_week=max(played) if played else None,
        player_weeks=player_weeks,
        team_weeks=team_weeks,
    )


def _window_stats(usage: UsageData, rows) -> tuple[float | None, float | None, float | None]:
    """(opportunity share, snap share, opportunities per game) for these rows."""
    if not rows:
        return None, None, None
    window = window_from_rows(usage, rows)
    opps = None
    if window.targets is not None and window.carries is not None:
        opps = window.targets + window.carries
    return window.opportunity_share, window.snap_pct, opps


def _forward_scheduled_weeks(usage: UsageData, gsis_id: str, week: int, horizon: int, last_week: int) -> list[int]:
    """The weeks in W+1..W+horizon where his team actually played.

    A bye is an absent team-week row, so it never counts as a week he
    could have appeared in — which is what stops a bye reading as a
    disappearance.
    """
    out: list[int] = []
    for w in range(week + 1, week + 1 + horizon):
        if w > last_week:
            break
        team = team_for_week(usage, gsis_id, w)
        if team and usage.team_week(team, w) is not None:
            out.append(w)
    return out


def classify_forward(
    share_at: float | None,
    forward_share: float | None,
    *,
    forward_games: int,
    scheduled_weeks: Sequence[int],
) -> str:
    """What the opportunity share did over the forward window.

    Order matters: a window with no games his team played is *not
    measurable* (end of season, or a bye that swallowed the window),
    which is a different statement from "his team played and he did not
    appear" — the second is the disappearance worth counting.
    """
    if share_at is None or not scheduled_weeks:
        return NOT_MEASURABLE
    if forward_games == 0:
        return NO_GAMES
    if forward_share is None:
        return NOT_MEASURABLE
    change = forward_share - share_at
    if _at_least(change, FORWARD_MOVE_THRESHOLD):
        return SHARE_UP
    if _at_least(-change, FORWARD_MOVE_THRESHOLD):
        return SHARE_DOWN
    return SHARE_HELD


def replay_role_signals(
    usage: UsageData,
    *,
    first_week: int = FIRST_REPLAY_WEEK,
    last_week: int = LAST_REPLAY_WEEK,
    forward_weeks: int = FORWARD_WEEKS,
) -> RoleReplay:
    """Label every player who had played, week by week, from truncated
    data — then read the next `forward_weeks` from the full season.

    The population at week W is everyone with at least one played row
    through W, not only those with MIN_GAMES_FOR_TREND: the players below
    that bar are exactly the ones the rules call Insufficient Role
    History, and leaving them out would make that share unmeasurable and
    hide every breakout the rules were silent through.
    """
    weeks = tuple(w for w in range(first_week, last_week + 1) if usage.latest_week and w <= usage.latest_week)
    replay = RoleReplay(season=usage.season, weeks=weeks, forward_weeks=forward_weeks)
    if not weeks:
        replay.notes.append("No played weeks in range — nothing to replay.")
        return replay
    # The last week the SEASON has data for, not the last week anyone
    # happened to play: a team-week row means that week was played, so a
    # player who simply stopped appearing still has a forward window.
    season_last_week = max(
        [r.week for r in usage.player_weeks if r.played] + [t.week for t in usage.team_weeks]
    )

    for week in weeks:
        truncated = truncate_usage(usage, week)
        seen = sorted({r.gsis_id for r in truncated.player_weeks if r.played})
        for gsis_id in seen:
            rows = played_rows(truncated, gsis_id)
            if not rows:
                continue
            trend = rt.role_trend(truncated, gsis_id)
            share_at, snap_at, opps_at = _window_stats(truncated, rows[-BASELINE_GAMES:])

            scheduled = _forward_scheduled_weeks(usage, gsis_id, week, forward_weeks, season_last_week)
            wanted = set(scheduled)
            forward_rows = [r for r in played_rows(usage, gsis_id) if r.week in wanted]
            forward_share, forward_snap, forward_opps = _window_stats(usage, forward_rows)
            outcome = classify_forward(share_at, forward_share, forward_games=len(forward_rows), scheduled_weeks=scheduled)
            change = None
            if share_at is not None and forward_share is not None:
                change = forward_share - share_at

            latest = rows[-1]
            replay.cases.append(RoleCase(
                week=week,
                gsis_id=gsis_id,
                name=latest.name,
                position=latest.position,
                team=latest.team,
                label=trend.label,
                games=trend.games,
                share_at=share_at,
                forward_share=forward_share,
                forward_change=change,
                forward_games=len(forward_rows),
                forward_scheduled_weeks=tuple(scheduled),
                snap_at=snap_at,
                forward_snap=forward_snap,
                opportunities_at=opps_at,
                forward_opportunities=forward_opps,
                outcome=outcome,
            ))

    replay.notes.append(
        f"{usage.season} usage is the same season the role thresholds were smoke-tested on: "
        "this describes what the rules do, and is not validation of them."
    )
    if weeks[-1] + forward_weeks > season_last_week:
        short = [w for w in weeks if w + forward_weeks > season_last_week]
        replay.notes.append(
            f"Weeks {short[0]}-{short[-1]} have a forward window shorter than {forward_weeks} weeks "
            f"(season ends at week {season_last_week}); week {season_last_week} has none at all."
        )
    return replay


def _label_summary(cases: Sequence[RoleCase], label: str) -> LabelSummary:
    subset = [c for c in cases if c.label == label]
    outcomes = Counter(c.outcome for c in subset)
    measurable = sum(v for k, v in outcomes.items() if k != NOT_MEASURABLE)
    changes = [c.forward_change for c in subset if c.forward_change is not None]
    return LabelSummary(
        label=label,
        cases=len(subset),
        measurable=measurable,
        outcomes={k: outcomes[k] for k in FORWARD_OUTCOMES if outcomes.get(k)},
        change=_distribution(changes),
    )


def _distribution(values: Sequence[float]) -> Distribution | None:
    if not values:
        return None
    ordered = sorted(values)
    return Distribution(
        n=len(ordered),
        minimum=ordered[0],
        p25=_quantile(ordered, 0.25),
        median=statistics.median(ordered),
        p75=_quantile(ordered, 0.75),
        maximum=ordered[-1],
    )


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Nearest-rank quantile — no interpolation, so the number is always
    one an actual player produced."""
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def false_breakouts(cases: Sequence[RoleCase]) -> list[RoleCase]:
    """Rising or Surging at W, opportunity share down over the window.
    Biggest drop first; (week, gsis) breaks ties so the list is stable."""
    hits = [c for c in cases if c.label in (rt.RISING, rt.SURGING) and c.outcome == SHARE_DOWN]
    return sorted(hits, key=lambda c: (c.forward_change if c.forward_change is not None else 0.0, c.week, c.gsis_id))


def false_collapses(cases: Sequence[RoleCase]) -> list[RoleCase]:
    """Falling or Collapsing at W, opportunity share up over the window."""
    hits = [c for c in cases if c.label in (rt.FALLING, rt.COLLAPSING) and c.outcome == SHARE_UP]
    return sorted(hits, key=lambda c: (-(c.forward_change or 0.0), c.week, c.gsis_id))


def missed_breakouts(cases: Sequence[RoleCase], *, min_change: float = MISSED_BREAKOUT_MIN_CHANGE) -> list[RoleCase]:
    """Stable or Insufficient at W, opportunity share up by at least
    `min_change` over the window — the rules were quiet through a real
    role change. Biggest gain first."""
    hits = [
        c for c in cases
        if c.label in QUIET_LABELS and c.forward_change is not None and _at_least(c.forward_change, min_change)
    ]
    return sorted(hits, key=lambda c: (-(c.forward_change or 0.0), c.week, c.gsis_id))


def insufficient_share_by_week(replay: RoleReplay) -> dict[int, tuple[int, int]]:
    """week -> (Insufficient cases, population). The share of players the
    rules can say nothing about, which should fall as the season runs."""
    out: dict[int, tuple[int, int]] = {}
    for week, counts in replay.label_counts_by_week().items():
        out[week] = (counts.get(rt.INSUFFICIENT, 0), sum(counts.values()))
    return out


# -- mode 2: snapshot replay ---------------------------------------------------


def snapshot_series(snapshots: Sequence[dict[str, Any]]) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """(league_id, player_id) -> [(YYYY-MM-DD, stable value)] oldest first.

    Mirrors the shape `decision_delta.build_snapshot` writes: both the
    `roster` bucket and the additive `tracked` bucket, values already in
    the stable currency the snapshot stored them in. Nothing is
    interpolated across a missing day.
    """
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
    return {key: sorted(points) for key, points in sorted(series.items())}


def replay_snapshots(snapshots: Sequence[dict[str, Any]]) -> SnapshotReplay:
    """Replay `market_velocity.classify_velocity` over every recorded
    player series, and `decision_delta.compute_delta` over every
    consecutive pair.

    With fewer than `market_velocity.MIN_OBSERVATIONS` days every series
    is Insufficient History by construction. That is stated, not worked
    around: the missing days are missing, and a value series cannot be
    reconstructed from a single day's cache.
    """
    ordered = sorted(snapshots, key=lambda s: (s.get("generated_at") or ""))
    dates = tuple((s.get("generated_at") or "")[:10] for s in ordered)
    leagues: list[str] = []
    for snap in ordered:
        for league_id in (snap.get("leagues") or {}):
            if str(league_id) not in leagues:
                leagues.append(str(league_id))

    replay = SnapshotReplay(snapshot_dates=dates, league_ids=tuple(sorted(leagues)), series_count=0)
    if not ordered:
        replay.notes.append("No snapshots on disk — nothing to replay. Snapshots are written only by a complete daily run.")
        return replay

    series = snapshot_series(ordered)
    replay.series_count = len(series)
    labels = Counter(mv.classify_velocity(points).label for points in series.values())
    replay.velocity_labels = dict(sorted(labels.items()))
    if len(ordered) < mv.MIN_OBSERVATIONS:
        replay.notes.append(
            f"{len(ordered)} snapshot(s) on disk; market_velocity needs {mv.MIN_OBSERVATIONS} daily observations, "
            f"so every one of the {len(series)} series reads {mv.INSUFFICIENT_HISTORY}. "
            "This is the true state of the input, not a modelling result."
        )

    if len(ordered) < 2:
        replay.notes.append("A decision delta needs two snapshots; none could be computed.")
        return replay
    for previous, current in zip(ordered, ordered[1:]):
        delta = dd.compute_delta(previous, current)
        if delta is None:
            continue
        counts = Counter(item.kind for item in delta.items)
        replay.delta_pairs.append((
            (previous.get("generated_at") or "")[:10],
            (current.get("generated_at") or "")[:10],
            dict(sorted(counts.items())),
        ))
        for item in delta.items[:MAX_EXAMPLES]:
            replay.delta_examples.append(f"[{item.league_name}] {item.kind}: {item.text}")
    return replay


# -- mode 3: outcome replay ----------------------------------------------------


def _entry_value(entry: LedgerEntry) -> float | None:
    """The value recorded for the entry's subject when it was made. Never
    re-derived from today's rankings — the point is what the tool saw."""
    snapshot = entry.valuation_snapshot or {}
    wanted = [pid for pid in (entry.player_ids or ()) if pid in snapshot] or list(snapshot)
    values = [snapshot[pid] for pid in wanted if snapshot.get(pid) is not None]
    return sum(float(v) for v in values) if values else None


def _age_days(iso: str | None, now: dt.datetime) -> int | None:
    if not iso:
        return None
    try:
        moment = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return (now - moment).days


def _outcome_case(entry: LedgerEntry, now: dt.datetime) -> OutcomeCase:
    scarcity = ", ".join(f"{pos} {label}" for pos, label in sorted((entry.replacement_context or {}).items()))
    return OutcomeCase(
        fingerprint=entry.fingerprint,
        league_name=entry.league_name,
        action=entry.action,
        subject=entry.subject,
        tier=entry.tier,
        outcome=entry.outcome or "",
        outcome_detail=entry.outcome_detail,
        recorded_value=_entry_value(entry),
        lineup_delta=entry.projected_lineup_delta,
        scarcity=scarcity,
        team_status=entry.team_status,
        faab_pct=entry.faab_pct,
        role_signal=entry.role_signal,
        currency=entry.currency,
        first_seen=entry.run_id,
        age_days=_age_days(entry.run_id, now),
    )


def replay_outcomes(ledger: Ledger, *, now: dt.datetime | None = None) -> OutcomeReplay:
    """Restate every recorded decision's prior state against the outcome
    Sleeper later showed.

    Nothing is graded. `Still Available` is not a failure and `Completed`
    is not a success — the ledger cannot see an offer that was never
    sent. What this can honestly do is put the tier, the recorded value,
    the previewed lineup delta and the scarcity read next to the fact,
    and count the combinations.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    entries = ledger.ordered()
    replay = OutcomeReplay(entries=len(entries), with_outcome=sum(1 for e in entries if e.outcome))
    if not entries:
        replay.notes.append("The decision ledger is empty — no recommendation has been recorded yet.")
        return replay

    replay.by_action_outcome = ledger_summary(ledger)
    resolved = [_outcome_case(e, now) for e in entries if e.outcome]
    replay.resolved = resolved
    replay.terminal = [c for c in resolved if c.outcome in (COMPLETED, ACQUIRED_BY_ANOTHER)]
    if not resolved:
        replay.notes.append("No entry carries an outcome yet; every recommendation is still open.")

    ages = [c.age_days for c in (_outcome_case(e, now) for e in entries) if c.age_days is not None]
    replay.oldest_age_days = max(ages) if ages else None

    horizons: dict[str, tuple[int, int]] = {}
    windows = [(f"observation window ({OBSERVATION_WINDOW_DAYS}d)", OBSERVATION_WINDOW_DAYS)]
    windows += [(f"outcome window ({w}w)", w * 7) for w in OUTCOME_WINDOWS_WEEKS]
    for label, days in windows:
        elapsed = sum(1 for a in ages if a >= days)
        horizons[label] = (elapsed, len(entries))
        if not elapsed:
            replay.notes.append(f"The {label} has not elapsed for any entry (oldest is {replay.oldest_age_days}d old).")
    replay.horizons = horizons
    return replay


# -- loading (the only disk-touching functions; never the network) -------------


def load_cached_usage(season: int) -> UsageData | None:
    """Season usage built straight from the on-disk cache.

    Deliberately not `nfl_usage.load_usage`: that goes through
    `get_or_fetch`, which would hit nflverse the moment the cache aged
    past its 24h window. A replay must never refetch — an absent or
    unwritten asset means the mode is skipped and said so.
    """
    from sleeper_tool import nfl_usage as nu
    from sleeper_tool.rankings.cache import load_snapshot

    player = load_snapshot(nu.STATS_PLAYER_SOURCE.format(season=season))
    if player is None or not player.payload or player.payload.get("absent"):
        return None
    team = load_snapshot(nu.STATS_TEAM_SOURCE.format(season=season))
    snaps = load_snapshot(nu.SNAP_COUNTS_SOURCE.format(season=season))
    players = load_snapshot(nu.NFLVERSE_PLAYERS_SOURCE)

    def rows(snapshot) -> list[dict]:
        payload = (snapshot.payload if snapshot else None) or {}
        return [] if payload.get("absent") else (payload.get("rows") or [])

    pfr_to_gsis = {r["pfr_id"]: r["gsis_id"] for r in rows(players) if r.get("pfr_id") and r.get("gsis_id")}
    usage = nu.usage_from_payloads(
        season,
        player.payload.get("rows") or [],
        rows(team),
        rows(snaps),
        pfr_to_gsis,
        fetched_at=player.fetched_at,
    )
    return usage if usage.player_weeks else None


# -- rendering -----------------------------------------------------------------


def _points(delta: float | None) -> str:
    return "—" if delta is None else f"{delta * 100:+.0f} pts"


def _week_span(weeks: Sequence[int]) -> str:
    if not weeks:
        return "no games"
    return f"W{weeks[0]}" if len(weeks) == 1 else f"W{weeks[0]}-W{weeks[-1]}"


def _share(part: int, whole: int) -> str:
    return "—" if not whole else f"{part / whole:.0%}"


HEADER_RULES = [
    "**This is a developer diagnostic, not an accuracy report.** Nothing below says a rule was right or wrong.",
    "",
    "Leakage rules this harness enforces:",
    "",
    "1. Every replayed label is computed from usage truncated to weeks <= W. A future row cannot reach the labeller.",
    "2. The forward window (weeks W+1..W+3) is read only to describe what followed, after the label is fixed.",
    "3. Historical KeepTradeCut / FantasyPros values are never reconstructed from today's. Value-based rules are "
    "replayed only over the daily snapshots that were genuinely recorded.",
    "4. An input that does not exist for a period is skipped and named, never approximated.",
    "5. No accuracy percentages: counts, trigger rates, distributions and concrete examples only. A trigger rate is "
    "how often a rule fired, not how often it was correct.",
]


def render_backtest_markdown(result: BacktestResult) -> str:
    lines: list[str] = ["# Historical replay (backtest harness)", ""]
    lines += HEADER_RULES
    lines += ["", f"- Generated: {result.generated_at.isoformat()}", ""]
    lines += _render_summary(result)
    lines += _render_role(result.role)
    lines += _render_snapshots(result.snapshot)
    lines += _render_outcomes(result.outcome)
    if result.unavailable:
        lines += ["## Inputs that could not be replayed", ""]
        lines += [f"- {note}" for note in result.unavailable]
        lines.append("")
    return "\n".join(lines)


def _render_summary(result: BacktestResult) -> list[str]:
    lines = ["## Summary", "", "| Mode | Cases replayed | Notes |", "| --- | ---: | --- |"]
    role = result.role
    if role is None:
        lines.append("| 1. Role signals | 0 | not replayed — see Inputs that could not be replayed |")
    else:
        lines.append(
            f"| 1. Role signals ({role.season}, weeks {role.weeks[0]}-{role.weeks[-1]}) | {len(role.cases)} | "
            f"{len({c.gsis_id for c in role.cases})} players; forward window {role.forward_weeks} weeks |"
        )
    snap = result.snapshot
    if snap is None:
        lines.append("| 2. Snapshots | 0 | not replayed |")
    else:
        lines.append(
            f"| 2. Snapshots | {snap.series_count} | {snap.snapshots} snapshot(s), "
            f"{len(snap.delta_pairs)} consecutive pair(s) diffed |"
        )
    out = result.outcome
    if out is None:
        lines.append("| 3. Ledger outcomes | 0 | not replayed |")
    else:
        lines.append(f"| 3. Ledger outcomes | {out.entries} | {out.with_outcome} carry a recorded outcome |")
    lines.append("")

    if role is not None:
        rising = [s for s in role.by_label() if s.label in (rt.RISING, rt.SURGING)]
        for s in rising:
            contradicted = s.outcomes.get(SHARE_DOWN, 0)
            continued = s.outcomes.get(SHARE_UP, 0)
            lines.append(
                f"- **{s.label}**: {s.cases} cases, {s.measurable} with a forward window; "
                f"share up afterwards {continued} ({_share(continued, s.measurable)}), "
                f"share down {contradicted} ({_share(contradicted, s.measurable)})"
                + (f", median forward change {_points(s.change.median)}." if s.change else ".")
            )
        missed = missed_breakouts(role.cases)
        quiet = sum(
            1 for c in role.cases if c.label in QUIET_LABELS and c.forward_change is not None
        )
        lines.append(
            f"- **Missed breakouts**: {len(missed)} of {quiet} Stable/Insufficient cases with a measurable "
            f"forward change ({_share(len(missed), quiet)}) gained "
            f"{MISSED_BREAKOUT_MIN_CHANGE * 100:.0f}+ opportunity-share points within {role.forward_weeks} weeks."
        )
    if snap is not None and snap.notes:
        lines.append(f"- **Snapshots**: {snap.notes[0]}")
    if out is not None and out.notes:
        lines.append(f"- **Ledger**: {out.notes[0]}")
    lines.append("")
    return lines


def _render_role(role: RoleReplay | None) -> list[str]:
    lines = ["## Mode 1 — role-signal replay", ""]
    if role is None:
        lines += ["Skipped: no cached season usage on disk.", ""]
        return lines
    lines += [f"_{note}_" for note in role.notes] + [""]
    lines += [
        f"Weeks {role.weeks[0]}-{role.weeks[-1]}; at each week the label is computed from usage truncated to that "
        f"week, then weeks W+1..W+{role.forward_weeks} are read to describe what followed. The measured quantity is "
        f"opportunity share (targets + carries as a share of the team's), taken over the last {BASELINE_GAMES} played "
        f"games through W against the forward window's games.",
        "",
        f"A move of {FORWARD_MOVE_THRESHOLD * 100:.0f} share points counts as up or down "
        "(`role_trends.OPPORTUNITY_SHARE_RISE`, the model's own component threshold). "
        "`no games` means his team played in the window and he did not appear; a bye is not counted as a week he "
        "could have appeared in. The Continues/Contradicts shares are out of every case with a forward window at "
        "all, `no games` included — a player who disappeared did not continue anything.",
        "",
        "### Label outcomes", "",
        "| Label | Cases | " + " | ".join(FORWARD_OUTCOMES) + " | Continues | Contradicts |",
        "| --- | ---: | " + " | ".join("---:" for _ in FORWARD_OUTCOMES) + " | ---: | ---: |",
    ]
    for s in role.by_label():
        cells = " | ".join(str(s.outcomes.get(o, 0)) for o in FORWARD_OUTCOMES)
        cont = CONTINUATION.get(s.label)
        contra = CONTRADICTION.get(s.label)
        cont_cell = "—" if cont is None else f"{s.outcomes.get(cont, 0)} ({_share(s.outcomes.get(cont, 0), s.measurable)})"
        contra_cell = "—" if contra is None else f"{s.outcomes.get(contra, 0)} ({_share(s.outcomes.get(contra, 0), s.measurable)})"
        lines.append(f"| {s.label} | {s.cases} | {cells} | {cont_cell} | {contra_cell} |")
    lines += ["", "### Forward opportunity-share change by label", "",
              "| Label | n | min | p25 | median | p75 | max |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for s in role.by_label():
        d = s.change
        if d is None:
            lines.append(f"| {s.label} | 0 | — | — | — | — | — |")
            continue
        lines.append(
            f"| {s.label} | {d.n} | {_points(d.minimum)} | {_points(d.p25)} | {_points(d.median)} | "
            f"{_points(d.p75)} | {_points(d.maximum)} |"
        )

    lines += ["", "### Trigger rate and Insufficient History share by week", "",
              "| Week | Population | " + " | ".join(LABEL_ORDER) + " | Insufficient share |",
              "| ---: | ---: | " + " | ".join("---:" for _ in LABEL_ORDER) + " | ---: |"]
    counts_by_week = role.label_counts_by_week()
    insufficient = insufficient_share_by_week(role)
    for week in role.weeks:
        counts = counts_by_week[week]
        total = sum(counts.values())
        cells = " | ".join(str(counts.get(label, 0)) for label in LABEL_ORDER)
        part, whole = insufficient[week]
        lines.append(f"| {week} | {total} | {cells} | {_share(part, whole)} |")

    move = f"{FORWARD_MOVE_THRESHOLD * 100:.0f}+ share points"
    lines += ["", f"### False breakouts (Rising/Surging at W, share down by {move})", ""]
    lines += _example_lines(false_breakouts(role.cases))
    lines += ["", f"### False collapses (Falling/Collapsing at W, share up by {move})", ""]
    lines += _example_lines(false_collapses(role.cases))
    lines += ["", f"### Missed breakouts (Stable/Insufficient at W, share up {MISSED_BREAKOUT_MIN_CHANGE * 100:.0f}+ share points)", ""]
    lines += _example_lines(missed_breakouts(role.cases))
    lines.append("")
    return lines


def _example_lines(cases: Sequence[RoleCase], limit: int = MAX_EXAMPLES) -> list[str]:
    if not cases:
        return ["None."]
    head = [f"{len(cases)} cases; the {min(limit, len(cases))} largest moves:"]
    return head + [""] + [f"- {c.describe()}" for c in cases[:limit]]


def _render_snapshots(snap: SnapshotReplay | None) -> list[str]:
    lines = ["## Mode 2 — snapshot replay", ""]
    if snap is None:
        lines += ["Skipped.", ""]
        return lines
    lines += [
        f"- Snapshots on disk: {snap.snapshots}" + (f" ({', '.join(snap.snapshot_dates)})" if snap.snapshot_dates else ""),
        f"- Leagues covered: {len(snap.league_ids)}",
        f"- Player value series replayed through `market_velocity.classify_velocity`: {snap.series_count}",
        "",
    ]
    if snap.velocity_labels:
        lines += ["| Velocity label | Series |", "| --- | ---: |"]
        lines += [f"| {label} | {count} |" for label, count in snap.velocity_labels.items()]
        lines.append("")
    if snap.delta_pairs:
        lines += ["| From | To | Items by kind |", "| --- | --- | --- |"]
        for previous, current, counts in snap.delta_pairs:
            text = ", ".join(f"{k} {v}" for k, v in counts.items()) or "none"
            lines.append(f"| {previous} | {current} | {text} |")
        lines.append("")
        if snap.delta_examples:
            lines += ["Examples:", ""] + [f"- {e}" for e in snap.delta_examples[:MAX_EXAMPLES]] + [""]
    for note in snap.notes:
        lines.append(f"- {note}")
    lines.append("")
    return lines


def _pair_table(heading: str, pairs: Sequence[tuple[str, str]]) -> list[str]:
    """A two-key count table, sorted by key then by outcome — the same
    shape for tier-vs-outcome and scarcity-vs-outcome."""
    counts = Counter(pairs)
    lines = [f"| {heading} | Outcome | Count |", "| --- | --- | ---: |"]
    for (key, outcome), count in sorted(counts.items()):
        lines.append(f"| {key} | {outcome} | {count} |")
    lines.append("")
    return lines


def _render_outcomes(out: OutcomeReplay | None) -> list[str]:
    lines = ["## Mode 3 — ledger outcome replay", ""]
    if out is None:
        lines += ["Skipped.", ""]
        return lines
    lines += [f"- Entries: {out.entries}; with a recorded outcome: {out.with_outcome}",
              f"- Oldest entry: {out.oldest_age_days}d old" if out.oldest_age_days is not None else "- Oldest entry: unknown",
              ""]
    if out.by_action_outcome:
        lines += ["| Action | Outcome | Count |", "| --- | --- | ---: |"]
        for action, counts in out.by_action_outcome.items():
            for outcome, count in counts.items():
                lines.append(f"| {action} | {outcome} | {count} |")
        lines.append("")
    if out.horizons:
        lines += ["| Horizon | Entries elapsed | Of |", "| --- | ---: | ---: |"]
        for label, (elapsed, total) in out.horizons.items():
            lines.append(f"| {label} | {elapsed} | {total} |")
        lines.append("")
    if out.resolved:
        lines += ["### Recorded prior state against outcome", "",
                  "_The state the tool recorded when it made the call, counted against what Sleeper later showed. "
                  "Neither column grades the other: `Still Available` is not a failure, and the ledger cannot see an "
                  "offer that was never sent._", ""]
        lines += _pair_table("Tier", [(c.tier or "(none)", c.outcome) for c in out.resolved])
        lines += _pair_table("Scarcity", [(c.scarcity or "(none)", c.outcome) for c in out.resolved])
        deltas = _distribution([c.lineup_delta for c in out.resolved if c.lineup_delta is not None])
        if deltas is not None:
            lines.append(f"- Previewed lineup delta on entries with an outcome: n={deltas.n}, "
                         f"min {deltas.minimum:+.1f}, median {deltas.median:+.1f}, max {deltas.maximum:+.1f} pts/wk "
                         f"({len(out.resolved) - deltas.n} of {len(out.resolved)} recorded none)")
        # Split by currency: a dynasty value and a redraft per-game
        # projection are different units, and one distribution over both
        # would be a number with no meaning.
        for currency in sorted({c.currency or "(unknown)" for c in out.resolved}):
            values = _distribution([
                c.recorded_value for c in out.resolved
                if c.recorded_value is not None and (c.currency or "(unknown)") == currency
            ])
            if values is not None:
                lines.append(f"- Recorded {currency} value on entries with an outcome: n={values.n}, "
                             f"min {values.minimum:.1f}, median {values.median:.1f}, max {values.maximum:.1f}")
        lines.append("")
    lines += [f"### Terminal outcomes ({COMPLETED} / {ACQUIRED_BY_ANOTHER})", ""]
    if not out.terminal:
        lines.append("None yet.")
    else:
        lines += [f"- {c.describe()}" for c in out.terminal]
    lines.append("")
    for note in out.notes:
        lines.append(f"- {note}")
    lines.append("")
    return lines


def build_result(
    *,
    usage: UsageData | None,
    snapshots: Sequence[dict[str, Any]],
    ledger: Ledger | None,
    generated_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
    unavailable: Iterable[str] = (),
) -> BacktestResult:
    """Assemble all three modes over already-loaded inputs. A missing
    input is an entry in `unavailable`, never a substitute."""
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    result = BacktestResult(generated_at=generated_at, unavailable=list(unavailable))
    if usage is not None:
        result.role = replay_role_signals(usage)
    else:
        result.unavailable.append(
            "Mode 1 (role signals): no cached nflverse season usage on disk. The replay never fetches, so this "
            "mode is skipped rather than filled in."
        )
    result.snapshot = replay_snapshots(snapshots)
    if ledger is not None:
        result.outcome = replay_outcomes(ledger, now=now)
    else:
        result.unavailable.append("Mode 3 (ledger outcomes): no decision ledger on disk.")
    return result
