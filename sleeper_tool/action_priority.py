"""Action Priority — one ordering for "what should I actually do first"
across every league and every kind of move.

The existing Best Moves list (report_data.build_priority_actions) ranks
inside a kind: alerts, then trades by acceptance tier, then Must-Add
waivers, then Strong Drops. That answers "which trade is best" but not
"is this trade more urgent than that waiver claim". This module answers
the second question, and it does it WITHOUT inventing a score.

Six categorical dimensions, compared lexicographically in this order.
The first one that differs decides; a later dimension can never
outweigh an earlier one, so there are no weights to tune and no way for
two marginal signals to add up to a bogus urgency.

  1 Urgency        when it has to be decided (Immediate → Long Horizon)
  2 Materiality    how much it changes, in projected weekly starter
                   points (Major → Marginal)
  3 Perishability  whether waiting destroys the option (Likely to
                   disappear → Durable)
  4 Strategic Fit  does it point the way my team status already points
  5 Evidence       how much of the tool agrees (Multiple agree → Single)
  6 Cost           how expensive and how reversible (Low → High)

Why this order: a move that must be decided this week outranks a bigger
move that can wait, because next week the bigger move is still there and
this one is not. Materiality before perishability because a marginal
move that expires is still marginal. Evidence and cost are tiebreakers,
not drivers — a well-evidenced marginal move is still marginal.

Ties past all six are broken by kind order, then league name, then
headline, so the list is stable run to run.

Everything is read off objects the report builder already produced
(MoveImpact, TradeEconomics, DefensiveAdd, StreamPlan, WaiverTarget,
Conflict, PlayoffLeverage, TeamStatusResult); nothing is recomputed and
nothing is fetched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from sleeper_tool.move_impact import MATERIAL_WEEKLY_POINTS
from sleeper_tool.recommendation_conflicts import TRADE, WAIVER, conflict_for
from sleeper_tool.streamer_planner import HOLD
from sleeper_tool.team_status import CONTENDER, REBUILD, veteran_min_age, young_max_age
from sleeper_tool.trade_opportunity_cost import COSTS_LINEUP, IMPROVES_LINEUP, MAJOR_LINEUP_COST
from sleeper_tool.waiver_engine import INSURANCE, MUST_ADD, STRONG_ADD

# -- recommendation kinds (shared identity convention) -----------------------
DROP = "drop"
DEFENSIVE_ADD = "defensive_add"
STREAMER = "streamer"
STASH = "stash"
ALERT = "alert"
KIND_ORDER = {ALERT: 0, DEFENSIVE_ADD: 1, WAIVER: 2, STREAMER: 3, TRADE: 4, DROP: 5, STASH: 6}

# -- 1 Urgency ---------------------------------------------------------------
IMMEDIATE = "Immediate"
THIS_WEEK = "This Week"
MONITOR = "Monitor"
LONG_HORIZON = "Long Horizon"
URGENCY_RANK = {IMMEDIATE: 0, THIS_WEEK: 1, MONITOR: 2, LONG_HORIZON: 3}

# -- 2 Materiality -----------------------------------------------------------
MAJOR = "Major"
MEANINGFUL = "Meaningful"
MARGINAL = "Marginal"
MATERIALITY_RANK = {MAJOR: 0, MEANINGFUL: 1, MARGINAL: 2}
# Projected weekly starter points. The Meaningful bar is Move Impact's own
# materiality bar (a delta below it isn't even reported); the Major bar is
# the mirror of trade_opportunity_cost's Major Lineup Cost threshold, so a
# Major gain and a Major loss are the same size.
MAJOR_WEEKLY_POINTS = 7.0
MEANINGFUL_WEEKLY_POINTS = MATERIAL_WEEKLY_POINTS

# -- 3 Perishability ---------------------------------------------------------
LIKELY_TO_DISAPPEAR = "Likely to disappear"
TIME_SENSITIVE = "Time-sensitive"
DURABLE = "Durable"
PERISHABILITY_RANK = {LIKELY_TO_DISAPPEAR: 0, TIME_SENSITIVE: 1, DURABLE: 2}

# -- 4 Strategic fit ---------------------------------------------------------
STRONG = "Strong"
NEUTRAL = "Neutral"
POOR = "Poor"
STRATEGIC_FIT_RANK = {STRONG: 0, NEUTRAL: 1, POOR: 2}

# -- 5 Evidence --------------------------------------------------------------
MULTIPLE_AGREE = "Multiple agree"
MIXED = "Mixed"
SINGLE = "Single"
EVIDENCE_RANK = {MULTIPLE_AGREE: 0, MIXED: 1, SINGLE: 2}
MIN_REASONS_FOR_AGREEMENT = 2

# -- 6 Cost ------------------------------------------------------------------
LOW_REVERSIBLE = "Low / reversible"
MODERATE = "Moderate"
HIGH_IRREVERSIBLE = "High / irreversible"
COST_RANK = {LOW_REVERSIBLE: 0, MODERATE: 1, HIGH_IRREVERSIBLE: 2}
HIGH_FAAB_PCT = 20  # % of the season budget at which a claim stops being cheap

_URGENT_WAIVER_TIERS = frozenset({MUST_ADD, STRONG_ADD, INSURANCE})
DIMENSIONS = ("urgency", "materiality", "perishability", "strategic_fit", "evidence", "cost")


@dataclass(frozen=True)
class PriorityKey:
    urgency: str
    materiality: str
    perishability: str
    strategic_fit: str
    evidence: str
    cost: str

    def sort_key(self) -> tuple[int, int, int, int, int, int]:
        return (
            URGENCY_RANK[self.urgency],
            MATERIALITY_RANK[self.materiality],
            PERISHABILITY_RANK[self.perishability],
            STRATEGIC_FIT_RANK[self.strategic_fit],
            EVIDENCE_RANK[self.evidence],
            COST_RANK[self.cost],
        )

    def describe(self) -> str:
        return (
            f"Urgency {self.urgency} · Materiality {self.materiality} · Perishability {self.perishability} · "
            f"Fit {self.strategic_fit} · Evidence {self.evidence} · Cost {self.cost}"
        )


def priority_line(key: PriorityKey) -> str:
    """The short renderer line: the three dimensions that actually move a
    recommendation up or down the list."""
    return f"{key.urgency} · {key.materiality} · {key.perishability}"


class Action(NamedTuple):
    kind: str
    key: str
    ld: object  # LeagueReportData, duck-typed (only .league.name is read)
    priority_key: PriorityKey
    headline: str
    detail: str


# -- subject lookup ----------------------------------------------------------
def _subject(kind: str, key: str | None, ld):
    """The object a (kind, key) pair names on this league's report data."""
    if ld is None or key is None:
        return None
    if kind == TRADE:
        try:
            index = int(key)
        except (TypeError, ValueError):
            return None
        proposals = getattr(ld, "proposals", None) or []
        return proposals[index] if index < len(proposals) else None
    if kind == WAIVER:
        return next((t for t in getattr(ld, "waiver_targets", None) or [] if t.player_id == key), None)
    if kind == DROP:
        return next((d for d in getattr(ld, "drop_candidates", None) or [] if d.entry.player_id == key), None)
    if kind == DEFENSIVE_ADD:
        add = getattr(ld, "defensive_add", None)
        return add if add is not None and add.target.player_id == key else None
    if kind == STREAMER:
        return next((p for p in getattr(ld, "streamers", None) or [] if p.position == key), None)
    if kind == STASH:
        return next((s for s in getattr(ld, "stash", None) or [] if s.entry.player_id == key), None)
    if kind == ALERT:
        return next((n for n in getattr(ld, "time_sensitive", None) or [] if n.player_name == key), None)
    return None


def _impact(kind: str, key: str | None, ld):
    if ld is None or key is None:
        return None
    if kind == TRADE:
        impacts = getattr(ld, "trade_impacts", None) or []
        index = int(key) if str(key).lstrip("-").isdigit() else -1
        return impacts[index] if 0 <= index < len(impacts) else None
    if kind == WAIVER:
        return (getattr(ld, "waiver_impacts", None) or {}).get(key)
    return None


def _economics(kind: str, key: str | None, ld):
    if kind != TRADE or ld is None or key is None or not str(key).lstrip("-").isdigit():
        return None
    economics = getattr(ld, "trade_economics", None) or []
    index = int(key)
    return economics[index] if 0 <= index < len(economics) else None


# -- the six dimensions ------------------------------------------------------
def classify_urgency(kind: str, subject, ld, report) -> str:
    """Immediate = a decision this tool cannot defer without losing the
    option (a high-severity alert, a Must Add, the fill for next week's
    bye hole). This Week = it belongs in this week's transactions (a
    defensive block, a streamer switch, a trade inside the deadline
    window, a Strong Add). Monitor = an ordinary trade or roster cleanup.
    Long Horizon = a developmental stash."""
    if kind == ALERT:
        return IMMEDIATE if getattr(subject, "severity", None) == "high" else THIS_WEEK
    if kind == WAIVER and subject is not None:
        if subject.priority_tier == MUST_ADD:
            return IMMEDIATE
        if _covers_next_week_bye(subject, ld, report):
            return IMMEDIATE
        return THIS_WEEK if subject.priority_tier in _URGENT_WAIVER_TIERS else MONITOR
    if kind in (DEFENSIVE_ADD, STREAMER):
        return THIS_WEEK
    if kind == TRADE:
        playoff = getattr(ld, "playoff", None)
        return THIS_WEEK if playoff is not None and playoff.urgent else MONITOR
    if kind == STASH:
        return LONG_HORIZON
    return MONITOR


def _covers_next_week_bye(target, ld, report) -> bool:
    """The bye-collision annotator already wrote "would also cover your
    week N bye hole" onto the target's reason; next week's hole is this
    week's waiver move."""
    bye = getattr(ld, "bye_collision", None)
    current_week = getattr(report, "current_week", None) if report is not None else None
    if bye is None or current_week is None or bye.week != current_week + 1:
        return False
    fills = {hole.replacement.player_id for hole in bye.holes if getattr(hole, "replacement", None) is not None}
    covering = {getattr(getattr(hole, "normal_starter", None), "position", None) for hole in bye.holes}
    return target.player_id in fills or target.position in covering


def classify_materiality(kind: str, subject, ld, key: str | None = None) -> str:
    """Projected weekly starter points wherever a preview exists, the
    opponent's gain for a block, the window gain per week for a streamer.
    Without any of those, a move is Marginal unless its own tier says it
    is not."""
    # Materiality is the GAIN a move makes; a lineup cost is carried by the
    # Risk reason and the conflict, never promoted as if it were a gain.
    impact = _impact(kind, key, ld)
    if impact is not None:
        return _materiality_of(max(impact.weekly_points_delta, 0.0))
    if kind == TRADE:
        economics = _economics(kind, key, ld)
        if economics is not None and economics.weekly_delta is not None:
            return _materiality_of(max(economics.weekly_delta, 0.0))
        if economics is not None and economics.roster_economics == IMPROVES_LINEUP:
            return MEANINGFUL
    if kind == DEFENSIVE_ADD and subject is not None:
        return _materiality_of(abs(subject.opponent_gain))
    if kind == STREAMER and subject is not None:
        held = subject.current.total if subject.current is not None else 0.0
        best = subject.sequence.total if subject.recommendation != HOLD and subject.sequence is not None else subject.single.total
        weeks = max(1, len(subject.weeks))
        return _materiality_of(abs(best - held) / weeks)
    if kind == ALERT:
        return MEANINGFUL if getattr(subject, "severity", None) == "high" else MARGINAL
    if kind == WAIVER and subject is not None and subject.priority_tier == MUST_ADD:
        return MEANINGFUL
    return MARGINAL


def _materiality_of(weekly_points: float) -> str:
    if weekly_points >= MAJOR_WEEKLY_POINTS:
        return MAJOR
    if weekly_points >= MEANINGFUL_WEEKLY_POINTS:
        return MEANINGFUL
    return MARGINAL


def classify_perishability(kind: str, subject, ld) -> str:
    """Does waiting destroy the option? A trending free agent is gone by
    Wednesday; a block and a streamer expire with the week; a trade offer
    and a stash are still there next week."""
    if kind == WAIVER and subject is not None:
        # Every trending-derived row has a nonzero count, so the count alone
        # discriminates nothing; only a paid tier that is also trending is
        # the kind of add that is gone by Wednesday.
        if subject.priority_tier == MUST_ADD or (subject.priority_tier == STRONG_ADD and subject.trend_count):
            return LIKELY_TO_DISAPPEAR
        return TIME_SENSITIVE
    if kind in (DEFENSIVE_ADD, STREAMER, ALERT):
        return TIME_SENSITIVE
    if kind == TRADE:
        playoff = getattr(ld, "playoff", None)
        return TIME_SENSITIVE if playoff is not None and playoff.urgent else DURABLE
    return DURABLE


def _incoming_pieces(kind: str, subject) -> tuple[list, bool]:
    """(player entries acquired, whether draft picks are acquired)."""
    if subject is None:
        return [], False
    if kind == TRADE:
        return list(getattr(subject, "receive", []) or []), bool(getattr(subject, "receive_picks", None))
    if kind == DEFENSIVE_ADD:
        return [subject.target], False
    if kind == STASH:
        return [subject.entry], False
    if kind == WAIVER:
        return [subject], False
    return [], False


def classify_strategic_fit(kind: str, subject, ld) -> str:
    """Does what I'm acquiring point the way my team status already
    points? Ages are compared against team_status's own position-specific
    thresholds, so a 27-year-old RB is a veteran and a 27-year-old QB is
    not. Picks count as young. Middling teams are always Neutral: there is
    no timeline to fit."""
    status = getattr(getattr(ld, "team_status", None), "status", None)
    if status not in (CONTENDER, REBUILD):
        return NEUTRAL
    entries, picks = _incoming_pieces(kind, subject)
    if not entries and not picks:
        return NEUTRAL
    # A WaiverTarget carries no age (the engine reads it off the player
    # cache when it needs it), so a missing age is simply no signal.
    ages = [(getattr(e, "age", None), getattr(e, "position", None)) for e in entries]
    young = picks or any(age is not None and age <= young_max_age(pos) for age, pos in ages)
    veteran = any(age is not None and age >= veteran_min_age(pos) for age, pos in ages)
    if young == veteran:  # both or neither: no clear timeline signal
        return NEUTRAL
    wants_young = status == REBUILD
    return STRONG if young == wants_young else POOR


def classify_evidence(kind: str, key: str | None, ld, provenance) -> str:
    """How much of the tool agrees. A Conflict is Mixed by construction —
    it IS two of the tool's signals pointing opposite ways."""
    if _conflict(kind, key, ld) is not None:
        return MIXED
    if provenance is None:
        return SINGLE
    if provenance.reasons_against:
        return MIXED
    # Agreement is between SOURCES: three clauses of one engine's sentence
    # are one module agreeing with itself.
    sources = {r.source for r in provenance.reasons_for}
    return MULTIPLE_AGREE if len(sources) >= MIN_REASONS_FOR_AGREEMENT else SINGLE


def _conflict(kind: str, key: str | None, ld):
    if kind not in (TRADE, WAIVER) or ld is None or key is None:
        return None
    return conflict_for(getattr(ld, "conflicts", None) or [], kind, key)


def classify_cost(kind: str, subject, ld, key: str | None = None) -> str:
    """How expensive, and how hard to undo. A trade is the only move that
    cannot be reversed at all; a claim that costs a starter or most of the
    FAAB budget is Moderate; everything else is cheap."""
    if kind == TRADE:
        return HIGH_IRREVERSIBLE
    economics = _economics(kind, key, ld)
    if economics is not None and economics.roster_economics == MAJOR_LINEUP_COST:
        return HIGH_IRREVERSIBLE
    starters = set(getattr(getattr(ld, "lineup", None), "starter_ids", ()) or ())
    if kind in (WAIVER, DEFENSIVE_ADD, STASH) and subject is not None:
        drop = getattr(subject, "drop_candidate", None) or getattr(subject, "drop", None)
        faab = getattr(subject, "suggested_faab_pct", None) or 0
        if drop is not None and drop.player_id in starters:
            return MODERATE
        if faab >= HIGH_FAAB_PCT:
            return MODERATE
        return LOW_REVERSIBLE
    if kind == DROP:
        return MODERATE  # a cut can't be undone if someone else claims him
    return LOW_REVERSIBLE


def classify(kind: str, ld, report, *, provenance=None, key: str | None = None) -> PriorityKey:
    """The full six-dimension key for one recommendation. `key` is the
    shared identity (proposal index as text, player_id, position, player
    name); `provenance` is that recommendation's Provenance card, used
    only for the Evidence dimension."""
    subject = _subject(kind, key, ld)
    urgency = classify_urgency(kind, subject, ld, report)
    materiality = classify_materiality(kind, subject, ld, key)
    # A marginal claim or switch is not this week's business just because
    # its tier says so: without a Must Add or a measured gain it is a
    # Monitor, so a Major trade is never outranked by a +0.0 add.
    if urgency == THIS_WEEK and materiality == MARGINAL and kind in (WAIVER, STREAMER, DEFENSIVE_ADD):
        tier = getattr(subject, "priority_tier", None)
        if tier != MUST_ADD:
            urgency = MONITOR
    return PriorityKey(
        urgency=urgency,
        materiality=materiality,
        perishability=classify_perishability(kind, subject, ld),
        strategic_fit=classify_strategic_fit(kind, subject, ld),
        evidence=classify_evidence(kind, key, ld, provenance),
        cost=classify_cost(kind, subject, ld, key),
    )


# -- ordering ----------------------------------------------------------------
def _league_name(ld) -> str:
    league = getattr(ld, "league", None)
    return getattr(league, "name", "") or ""


def _as_action(a) -> Action:
    return a if isinstance(a, Action) else Action(*a)


def _order_key(a: Action) -> tuple:
    return (*a.priority_key.sort_key(), KIND_ORDER.get(a.kind, len(KIND_ORDER)), _league_name(a.ld), a.headline)


def rank_actions(actions) -> list[Action]:
    """Deterministic cross-league order: the six dimensions
    lexicographically, then kind order, league name and headline."""
    return sorted((_as_action(a) for a in actions), key=_order_key)


def explain_order(a, b) -> str | None:
    """The first dimension that separates two actions (or two
    PriorityKeys), as "<dimension>: <a's value> before <b's value>", or
    None when nothing separates them. The leading token is always the
    dimension name, so callers can assert on it."""
    key_a = a if isinstance(a, PriorityKey) else _as_action(a).priority_key
    key_b = b if isinstance(b, PriorityKey) else _as_action(b).priority_key
    for dimension, ranks in zip(
        DIMENSIONS,
        (URGENCY_RANK, MATERIALITY_RANK, PERISHABILITY_RANK, STRATEGIC_FIT_RANK, EVIDENCE_RANK, COST_RANK),
    ):
        va, vb = getattr(key_a, dimension), getattr(key_b, dimension)
        if ranks[va] != ranks[vb]:
            first, second = (va, vb) if ranks[va] < ranks[vb] else (vb, va)
            return f"{dimension}: {first} before {second}"
    if isinstance(a, PriorityKey) or isinstance(b, PriorityKey):
        return None
    action_a, action_b = _as_action(a), _as_action(b)
    for dimension, value_a, value_b in (
        ("kind", KIND_ORDER.get(action_a.kind, len(KIND_ORDER)), KIND_ORDER.get(action_b.kind, len(KIND_ORDER))),
        ("league", _league_name(action_a.ld), _league_name(action_b.ld)),
        ("headline", action_a.headline, action_b.headline),
    ):
        if value_a != value_b:
            first, second = (action_a, action_b) if value_a < value_b else (action_b, action_a)
            shown = {"kind": (first.kind, second.kind), "league": (_league_name(first.ld), _league_name(second.ld))}.get(
                dimension, (first.headline, second.headline)
            )
            return f"{dimension}: {shown[0]} before {shown[1]}"
    return None
