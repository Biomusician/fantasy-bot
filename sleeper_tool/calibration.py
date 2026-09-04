"""Calibration lab — an ENGINEERING diagnostic, never a user-facing report.

Every decision module in this project fires a label or a recommendation when
some threshold is crossed. Nothing until now answered the two questions that
decide whether a threshold is any good:

    how often was the rule even *eligible* to fire, and how often did it?

A rule that fires on 90% of the players it looks at carries no information;
one that has never fired across 25+ chances is either dead or mis-tuned; one
whose triggers all come from a single league is describing that league, not
the game. This module walks ONE already-built WeeklyReportData, counts
eligible-vs-triggered for every rule in the decision layer, and labels the
pathologies.

It never tunes anything. It has no opinion about what a threshold *should*
be — it reports what the current constants did to the current data and
leaves the judgement to a human. The constants themselves are read live off
the owning modules, so a threshold change shows up in the next report without
touching this file.

Two deliberate design points:

  * Eligible is counted BEFORE any list cap. `replacement_value` highlights
    at most MAX_HIGHLIGHTED understated players, `roster_clog` at most
    MAX_CLOGS_PER_ROSTER; counting the capped list as "triggered" against an
    uncapped eligible set is the honest reading, and the cap is named in the
    rule's note so the depressed rate isn't mistaken for a dead threshold.
  * Report objects are duck-typed. This module imports constants from the
    decision modules but never `report_data`, so it can be pointed at a
    synthetic stand-in in tests and can't create an import cycle.

The cross-signal section answers a different question: not "did a rule fire"
but "did five rules all say the same thing on one card". Scarcity stated in a
caveat, again in the economics line and again in the conflict reasons is one
fact printed three times, which reads as three independent reasons. Those are
reported as counts and shares; nothing is rewritten or suppressed here.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from sleeper_tool import buyer_board as bb
from sleeper_tool import bye_collision as byc
from sleeper_tool import contender_insurance as ci
from sleeper_tool import decision_delta as dd
from sleeper_tool import league_economy as le
from sleeper_tool import lineup_leverage as ll
from sleeper_tool import market_velocity as mv
from sleeper_tool import matchup_leverage as ml
from sleeper_tool import move_impact as mi
from sleeper_tool import opponent_blocker as ob
from sleeper_tool import pick_opportunity as po
from sleeper_tool import playoff_leverage as pl
from sleeper_tool import portfolio_exposure as pe
from sleeper_tool import recommendation_conflicts as rc
from sleeper_tool import replacement_value as rv
from sleeper_tool import role_trends as rt
from sleeper_tool import roster_assets as ra
from sleeper_tool import roster_clog as rcl
from sleeper_tool import roster_consolidation as rcs
from sleeper_tool import schedule_window as sw
from sleeper_tool import source_disagreement as sd
from sleeper_tool import stash_board as sb
from sleeper_tool import streamer_planner as sp
from sleeper_tool import trade_engine as te
from sleeper_tool import trade_opportunity_cost as toc
from sleeper_tool import waiver_engine as we
from sleeper_tool import watchlist as wl
from sleeper_tool.team_status import CONTENDER

# --------------------------------------------------------------------------
# Diagnostic thresholds. These are the calibration lab's OWN constants —
# judgements about when a trigger rate is itself pathological, not about
# fantasy football.
# --------------------------------------------------------------------------

MIN_SAMPLE = 10  # below this many eligible observations, say nothing
NEARLY_ALWAYS_FIRES_MIN_RATE = 0.60  # at or above this share, the label stops distinguishing anything
OVERACTIVE_MIN_RATE = 0.40  # from this share up to (not including) NEARLY_ALWAYS_FIRES_MIN_RATE the label is doing a lot of work
RARE_MAX_RATE = 0.05  # below this share the rule is a rare event; fine for an alarm, suspicious for a default
NEVER_FIRES_MIN_ELIGIBLE = 25  # this many chances with zero triggers is a dead rule, not bad luck
LEAGUE_CONCENTRATION_MIN_SHARE = 0.75  # one league holding this share of the triggers
LEAGUE_CONCENTRATION_MIN_TRIGGERS = 5  # ... with at least this many triggers overall
LEAGUE_CONCENTRATION_MIN_LEAGUES = 3  # ... spread over at least this many eligible leagues
FORMAT_SHARE = 0.80  # one league format (superflex vs 1QB, or dynasty vs keeper vs redraft) holding this share of the triggers
POSITION_SHARE = 0.80  # one position holding this share of a player-level rule's triggers
POSITION_BIAS_MIN_POSITIONS = 3  # ... when at least this many positions were eligible
BIAS_MIN_TRIGGERS = 5  # format and position bias need at least this many triggers to be more than noise
OVERLAP_SHARE = 0.80  # two rules whose trigger sets overlap by this share (of the smaller set) may be one fact counted twice
OVERLAP_MIN_TRIGGERS = 5  # ... when each has at least this many triggers
MIN_DROPPABLE = 2  # a roster with fewer players droppable by every rule has nowhere to put an add
MAX_EXAMPLES = 3

# Diagnostic labels. HEALTHY is the only one that is not a flag.
HEALTHY = "Healthy"
INSUFFICIENT_SAMPLE = "Insufficient Sample"
NEVER_FIRES = "Never Fires"
RARE = "Rare"
OVERACTIVE = "Overactive"
NEARLY_UNIVERSAL = "Nearly Universal"
LEAGUE_CONCENTRATED = "Highly League-Concentrated"
FORMAT_BIASED = "Format-Biased"
POSITION_BIASED = "Position-Biased"
POTENTIAL_DOUBLE_COUNT = "Potential Double Count"

# The v1 names, kept so nothing that imported them has to change.
NORMAL = HEALTHY
NEARLY_ALWAYS_FIRES = NEARLY_UNIVERSAL

# One rule can carry several labels ("Overactive" and "Highly
# League-Concentrated" are both true of 8 of 20 from one league). `diagnostic`
# is the first of these that applies; `diagnostics` keeps the rest. The
# where-labels outrank Overactive and Rare because "all from one league" is
# the more specific thing to go and look at.
LABEL_PRIORITY: tuple[str, ...] = (
    INSUFFICIENT_SAMPLE, NEVER_FIRES, NEARLY_UNIVERSAL,
    LEAGUE_CONCENTRATED, FORMAT_BIASED, POSITION_BIASED, OVERACTIVE, RARE, POTENTIAL_DOUBLE_COUNT,
)

# The facts the dependency map follows. A rule that reads one of these
# declares it in RuleSpec.inputs; the map then shows how many "votes" the
# one fact casts across the report.
FACT_SCARCITY = "scarcity (replacement market)"
FACT_TRENDING = "trending add count"
FACT_MOVE_DELTA = "move impact weekly delta"
FACT_ROLE = "role label (usage)"
TRACKED_FACTS: tuple[str, ...] = (FACT_SCARCITY, FACT_TRENDING, FACT_MOVE_DELTA, FACT_ROLE)

# Modules that consume a fact without owning a rule in this inventory (the
# FAAB posture, the watchlist and provenance are annotators, not thresholds).
# Hand-maintained from reading the code; the rules half of the map is not.
MODULE_CONSUMERS: dict[str, tuple[str, ...]] = {
    FACT_SCARCITY: ("faab_strategy", "watchlist", "recommendation_provenance", "report_data (insurance filter, waiver/clog annotations)"),
    FACT_TRENDING: ("action_priority (perishability)", "recommendation_provenance", "roster_clog (trending exemption)"),
    FACT_MOVE_DELTA: ("action_priority (materiality)", "recommendation_provenance", "report_data (matchup note)"),
    FACT_ROLE: ("faab_strategy", "watchlist", "recommendation_provenance", "decision_outcomes", "report_data (role_market)"),
}

CROSS_LEAGUE = "(all leagues)"
UNKNOWN_FORMAT = "?"

# Rules that are structurally quiet at certain points in the season. This is
# an EXPLANATION attached to the flag, never a suppression: a quiet rule in
# week 1 still gets counted and still gets its diagnostic, it just also says
# why the count looks the way it does.
TIME_GATED: dict[str, str] = {
    "Velocity: Insufficient History": (
        f"needs {mv.MIN_OBSERVATIONS} daily snapshots; every player reads Insufficient History until the "
        "run history is that deep"
    ),
    "Velocity: Stable": "unreachable until the snapshot history is deep enough to classify anything",
    "Velocity: Rising": "unreachable until the snapshot history is deep enough to classify anything",
    "Velocity: Rapidly Rising": "unreachable until the snapshot history is deep enough to classify anything",
    "Velocity: Falling": "unreachable until the snapshot history is deep enough to classify anything",
    "Velocity: Rapidly Falling": "unreachable until the snapshot history is deep enough to classify anything",
    "Velocity: Unmeasurable": "unreachable until the snapshot history is deep enough to classify anything",
    "Playoff leverage available": f"None until {pl.MIN_GAMES_FOR_LABEL} games are played",
    "Playoff: Comfortable": f"None until {pl.MIN_GAMES_FOR_LABEL} games are played",
    "Playoff: Bubble": f"None until {pl.MIN_GAMES_FOR_LABEL} games are played",
    "Playoff: Long Shot": f"None until {pl.MIN_GAMES_FOR_LABEL} games are played",
    "Playoff: Out": f"None until {pl.MIN_GAMES_FOR_LABEL} games are played",
    "Deadline window": (
        f"only inside {pl.DEADLINE_WINDOW_WEEKS} weeks of the league's trade deadline, which is mid-season"
    ),
    "Defensive add found": (
        "needs an opponent hole the wire can exploit; before the bye weeks start, opponents rarely have one"
    ),
    "Bye collision found": (
        f"scans {byc.LOOKAHEAD_WEEKS} weeks ahead, so it is silent until the first byes are inside that window"
    ),
    "Decision delta: valuation": "needs a prior run's snapshot; a first run of the day has nothing to diff against",
    "Decision delta: status": "needs a prior run's snapshot; a first run of the day has nothing to diff against",
    "Decision delta: roster": "needs a prior run's snapshot; a first run of the day has nothing to diff against",
    "Decision delta: recommendation": "needs a prior run's snapshot; a first run of the day has nothing to diff against",
}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One chance for a rule to fire. `missing` means the inputs the rule
    needs were absent, so this chance is neither eligible nor triggered — it
    only feeds missing_data_rate."""
    league: str
    triggered: bool
    missing: bool = False
    example: str | None = None
    magnitude: float = 0.0
    subject: str | None = None  # what fired, e.g. "player:1234" or "trade:2" — lets two rules' trigger sets be compared
    position: str | None = None  # for player-level rules only
    fmt: str = UNKNOWN_FORMAT  # "SF dynasty", "1QB redraft", ... — the league's format at observation time


@dataclass(frozen=True)
class LeagueContext:
    """What a stream knows about the league an item came from, beyond its
    name. `kind` is config's dynasty/keeper/redraft; `superflex` is the
    format Sleeper reported."""
    name: str
    kind: str | None = None
    superflex: bool | None = None

    @property
    def qb_format(self) -> str:
        return UNKNOWN_FORMAT if self.superflex is None else ("SF" if self.superflex else "1QB")

    @property
    def fmt(self) -> str:
        return f"{self.qb_format} {self.kind or UNKNOWN_FORMAT}"


CROSS_LEAGUE_CTX = LeagueContext(CROSS_LEAGUE)


@dataclass(frozen=True)
class RuleSpec:
    module: str
    name: str
    constants: tuple[tuple[str, Any], ...]
    observe: Callable[[Any], list[Observation]]
    note: str | None = None  # a list cap or other structural fact the rate can't show
    min_report_leagues: int = 0  # below this many drafted leagues the rule can't be judged at all
    inputs: tuple[str, ...] = ()  # the tracked facts (TRACKED_FACTS) this rule reads


@dataclass
class RuleResult:
    module: str
    name: str
    constants: tuple[tuple[str, Any], ...]
    eligible: int
    triggered: int
    rate: float | None
    diagnostic: str
    leagues_triggered: list[str]
    leagues_eligible: int
    examples: list[str]
    missing: int
    missing_data_rate: float | None
    note: str | None = None
    time_gated: str | None = None
    diagnostics: list[str] = field(default_factory=list)  # every label that applies, `diagnostic` first
    by_format: dict[str, int] = field(default_factory=dict)  # triggers per league format
    formats_eligible: dict[str, int] = field(default_factory=dict)  # eligible per league format
    by_position: dict[str, int] = field(default_factory=dict)
    positions_eligible: dict[str, int] = field(default_factory=dict)
    subjects: frozenset[str] = frozenset()  # "league|subject" of every trigger, for the overlap check
    inputs: tuple[str, ...] = ()
    bias_detail: str = ""  # "8 of 9 triggers from SF (44% of eligible)" — filled when a bias label applies

    @property
    def constants_text(self) -> str:
        return ", ".join(f"{n}={_fmt_value(v)}" for n, v in self.constants) or "—"

    @property
    def diagnostics_text(self) -> str:
        extra = [d for d in self.diagnostics if d != self.diagnostic]
        return self.diagnostic + (f" (also: {', '.join(extra)})" if extra else "")


@dataclass
class CrossSignalFinding:
    """One "the same fact was stated twice" check. `count` of `of`, never a
    rewrite — the renderers and the modules stay exactly as they are."""
    name: str
    count: int
    of: int
    share: float | None
    note: str


@dataclass
class DoubleCountFinding:
    """Two rules whose trigger sets are mostly the same players / trades.
    Not proof of a double count — a Must Add is trending by construction —
    but the reader should know the two labels are not independent votes."""
    rule_a: str
    rule_b: str
    overlap: int
    smaller: int
    share: float
    examples: list[str]


@dataclass
class ContradictionFinding:
    """Two rules that fired on ONE subject in opposite directions in this
    run. `count` of `of` subjects, with examples; nothing is resolved here."""
    name: str
    count: int
    of: int
    share: float | None
    note: str
    examples: list[str] = field(default_factory=list)
    should_be_zero: bool = False  # an invariant report_data claims to enforce, so any hit is a bug, not a tradeoff


@dataclass
class DropProtection:
    """Per league: how many rostered players some rule protects from being
    the drop, and how many are left. A roster where every player is
    protected by something has no legal drop for any add."""
    league: str
    rostered: int
    protected: int
    droppable: int
    by_reason: dict[str, int]  # reason -> players protected by it (a player can be counted under several)
    droppable_names: list[str]
    flagged: bool  # droppable < MIN_DROPPABLE


@dataclass
class DependencyEntry:
    fact: str
    rules: list[str]  # "module.rule name"
    modules: list[str]  # distinct modules among those rules
    other_consumers: tuple[str, ...]  # MODULE_CONSUMERS: annotators without a rule here

    @property
    def votes(self) -> int:
        return len(self.modules) + len(self.other_consumers)


@dataclass
class CalibrationResult:
    generated_at: dt.datetime
    current_week: int | None
    leagues: list[str] = field(default_factory=list)
    rules: list[RuleResult] = field(default_factory=list)
    cross_signals: list[CrossSignalFinding] = field(default_factory=list)
    double_counts: list[DoubleCountFinding] = field(default_factory=list)
    contradictions: list[ContradictionFinding] = field(default_factory=list)
    drop_protection: list[DropProtection] = field(default_factory=list)
    dependency_map: list[DependencyEntry] = field(default_factory=list)

    def flagged(self) -> list[RuleResult]:
        return [r for r in self.rules if r.diagnostic != HEALTHY]


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (frozenset, set)):
        return "{" + ", ".join(sorted(str(x) for x in v)) + "}"
    if isinstance(v, tuple):
        return "(" + ", ".join(str(x) for x in v) + ")"
    return str(v)


# --------------------------------------------------------------------------
# Observation plumbing. Every rule is (a way to enumerate chances) x (a
# predicate that says whether this chance fired).
# --------------------------------------------------------------------------


def active_leagues(report) -> list[Any]:
    """The leagues a rule could possibly have fired in: built, drafted, no
    error. A league that failed to build isn't evidence about a threshold."""
    out = []
    for ld in getattr(report, "leagues", ()) or ():
        if getattr(ld, "error", None) or not getattr(ld, "drafted", False):
            continue
        out.append(ld)
    return out


def _league_name(ld) -> str:
    league = getattr(ld, "league", None)
    return getattr(league, "name", None) or "?"


def league_context(ld) -> LeagueContext:
    """Name plus the two format axes, read defensively: `kind` off config's
    LeagueInfo, superflex off the roster's LeagueFormat."""
    league = getattr(ld, "league", None)
    fmt = getattr(getattr(ld, "roster", None), "fmt", None)
    superflex = getattr(fmt, "is_superflex", None)
    return LeagueContext(
        name=getattr(league, "name", None) or "?",
        kind=getattr(league, "kind", None),
        superflex=bool(superflex) if superflex is not None else None,
    )


def _items(select: Callable[[Any], Iterable[Any]]) -> Callable[[Any], Iterator[tuple[LeagueContext, Any]]]:
    """Turn a per-league selector into a (league context, item) stream over
    the whole report."""
    def stream(report) -> Iterator[tuple[LeagueContext, Any]]:
        for ld in active_leagues(report):
            ctx = league_context(ld)
            for item in select(ld) or ():
                yield ctx, item
    return stream


def _leagues_where(predicate: Callable[[Any], bool]) -> Callable[[Any], Iterator[tuple[LeagueContext, Any]]]:
    """A rule whose unit of observation is the league itself."""
    return _items(lambda ld: [ld] if predicate(ld) else [])


def _over(
    stream: Callable[[Any], Iterator[tuple[Any, Any]]],
    trigger: Callable[[Any], bool],
    *,
    describe: Callable[[Any], str] | None = None,
    magnitude: Callable[[Any], float] | None = None,
    missing: Callable[[Any], bool] | None = None,
    subject: Callable[[Any], Any] | None = None,
    position: Callable[[Any], Any] | None = None,
) -> Callable[[Any], list[Observation]]:
    """`subject` names what fired so trigger sets can be compared across
    rules; `position` marks a player-level rule. Both are read on eligible
    observations whether or not they fired, since the eligible set is the
    denominator for position bias."""
    def observe(report) -> list[Observation]:
        out: list[Observation] = []
        for league, item in stream(report):
            ctx = league if isinstance(league, LeagueContext) else LeagueContext(str(league))
            if missing is not None and missing(item):
                out.append(Observation(ctx.name, False, missing=True, fmt=ctx.fmt))
                continue
            fired = bool(trigger(item))
            example = None
            mag = 0.0
            if fired and describe is not None:
                try:
                    example = f"{ctx.name} — {describe(item)}"
                except Exception:  # a diagnostic must never break on one odd record
                    example = None
            if fired and magnitude is not None:
                try:
                    mag = float(magnitude(item))
                except Exception:
                    mag = 0.0
            out.append(Observation(
                ctx.name, fired, example=example, magnitude=mag,
                subject=_safe_text(subject, item), position=_safe_text(position, item), fmt=ctx.fmt,
            ))
        return out
    return observe


def _safe_text(fn: Callable[[Any], Any] | None, item) -> str | None:
    if fn is None:
        return None
    try:
        value = fn(item)
    except Exception:
        return None
    return None if value is None else str(value)


def _bucket_rules(
    module: str,
    prefix: str,
    stream: Callable[[Any], Iterator[tuple[Any, Any]]],
    label_of: Callable[[Any], Any],
    labels: Iterable[str],
    constants: tuple[tuple[str, Any], ...],
    *,
    describe: Callable[[Any], str] | None = None,
    magnitude: Callable[[Any], float] | None = None,
    missing: Callable[[Any], bool] | None = None,
    note: str | None = None,
    subject: Callable[[Any], Any] | None = None,
    position: Callable[[Any], Any] | None = None,
    inputs: tuple[str, ...] = (),
) -> list[RuleSpec]:
    """One rule per bucket of a categorical label — the histogram IS the
    calibration question for a bucketed output."""
    return [
        RuleSpec(
            module=module,
            name=f"{prefix}: {label}",
            constants=constants,
            observe=_over(
                stream,
                lambda item, label=label: label_of(item) == label,
                describe=describe, magnitude=magnitude, missing=missing, subject=subject, position=position,
            ),
            note=note,
            inputs=inputs,
        )
        for label in labels
    ]


# Subject naming — one convention per kind of thing, so a player in a waiver
# rule and the same player in a drop rule compare equal.
def _player(pid) -> str | None:
    return f"player:{pid}" if pid else None


def _trade(i) -> str:
    return f"trade:{i}"


def _pos_of(obj) -> str | None:
    return _get(obj, "position")


def _entry_pos(obj) -> str | None:
    return _get(_get(obj, "entry"), "position")


# --------------------------------------------------------------------------
# Small duck-typed accessors. Synthetic report objects in tests only set the
# fields a given rule reads, so every access degrades to empty.
# --------------------------------------------------------------------------


def _get(obj, name, default=None):
    return getattr(obj, name, default) if obj is not None else default


def _proposals(ld) -> list[Any]:
    return list(_get(ld, "proposals", []) or [])


def _economics(ld) -> list[Any]:
    return list(_get(ld, "trade_economics", []) or [])


def _impacts(ld) -> list[Any]:
    return list(_get(ld, "trade_impacts", []) or [])


def _targets(ld) -> list[Any]:
    return list(_get(ld, "waiver_targets", []) or [])


def _conflicts(ld) -> list[Any]:
    return list(_get(ld, "conflicts", []) or [])


def _starter_ids(ld) -> set[str]:
    lineup = _get(ld, "lineup")
    return set(_get(lineup, "starter_ids", ()) or ())


def _bench_entries(ld) -> list[Any]:
    roster = _get(ld, "roster")
    entries = list(_get(roster, "entries", []) or [])
    starters = _starter_ids(ld)
    if starters:
        return [e for e in entries if e.player_id not in starters]
    return [e for e in entries if not getattr(e, "is_starter", False)]


def _pairs(ld) -> list[tuple[int, Any, Any, Any]]:
    """(index, proposal, economics-or-None, impact-or-None) — the three
    parallel lists report_data keeps, zipped defensively."""
    econ, imp = _economics(ld), _impacts(ld)
    return [
        (i, p, econ[i] if i < len(econ) else None, imp[i] if i < len(imp) else None)
        for i, p in enumerate(_proposals(ld))
    ]


def _texts_of(p) -> list[str]:
    return [*(_get(p, "caveats", []) or []), *(_get(p, "rationale_for_me", []) or [])]


# --------------------------------------------------------------------------
# The rule inventory
# --------------------------------------------------------------------------


def build_rules() -> list[RuleSpec]:
    """Every label and threshold in the decision layer, as a rule. Constants
    are read off the owning module at call time so a retuned threshold shows
    up here with no edit. `subject` names what fired (a player, a trade, a
    position, the league) so trigger sets can be compared across rules;
    `position` marks the player-level rules the position-bias check reads;
    `inputs` declares which tracked facts the rule's label depends on."""
    rules: list[RuleSpec] = []

    # --- replacement_value -------------------------------------------------
    scarcity_constants = (
        ("ABUNDANT_MAX_GAP", rv.ABUNDANT_MAX_GAP),
        ("NORMAL_MAX_GAP", rv.NORMAL_MAX_GAP),
        ("SCARCE_MAX_GAP", rv.SCARCE_MAX_GAP),
    )
    rules += _bucket_rules(
        "replacement_value", "Scarcity",
        _items(lambda ld: (_get(_get(ld, "replacement"), "positions", {}) or {}).values()),
        lambda m: m.scarcity,
        (rv.ABUNDANT, rv.NORMAL, rv.SCARCE, rv.VERY_SCARCE),
        scarcity_constants,
        describe=lambda m: f"{m.position} gap {m.gap:.2f}" if m.gap is not None else f"{m.position} no free agent",
        magnitude=lambda m: abs(m.gap) if m.gap is not None else 1.0,
        subject=lambda m: f"pos:{m.position}",
        note="this IS the scarcity fact; every rule listing it as an input is a downstream vote",
    )
    rules.append(RuleSpec(
        module="replacement_value", name="Rank understates replacement edge",
        constants=(
            ("RANK_DIVERGENCE_MIN", rv.RANK_DIVERGENCE_MIN),
            ("UNDERSTATED_MIN_OVER_WAIVER", rv.UNDERSTATED_MIN_OVER_WAIVER),
            ("MAX_HIGHLIGHTED", rv.MAX_HIGHLIGHTED),
        ),
        observe=_over(
            _measurable_replacement_players,
            lambda pair: pair[1] in pair[2],
            describe=lambda pair: f"{pair[0].entry.name} +{pair[0].projection_over_waiver:.1f}/wk over waiver",
            magnitude=lambda pair: pair[0].projection_over_waiver or 0.0,
            subject=lambda pair: _player(pair[1]),
            position=lambda pair: _entry_pos(pair[0]),
        ),
        note=f"triggered list is capped at MAX_HIGHLIGHTED={rv.MAX_HIGHLIGHTED}; eligible is uncapped",
    ))
    rules.append(RuleSpec(
        module="replacement_value", name="Rank overstates replacement edge",
        constants=(
            ("RANK_DIVERGENCE_MIN", rv.RANK_DIVERGENCE_MIN),
            ("OVERSTATED_MAX_OVER_WAIVER", rv.OVERSTATED_MAX_OVER_WAIVER),
            ("MAX_HIGHLIGHTED", rv.MAX_HIGHLIGHTED),
        ),
        observe=_over(
            _measurable_replacement_players,
            lambda pair: pair[1] in pair[3],
            describe=lambda pair: f"{pair[0].entry.name} {pair[0].projection_over_waiver:+.1f}/wk over waiver",
            magnitude=lambda pair: -(pair[0].projection_over_waiver or 0.0),
            subject=lambda pair: _player(pair[1]),
            position=lambda pair: _entry_pos(pair[0]),
        ),
        note=f"triggered list is capped at MAX_HIGHLIGHTED={rv.MAX_HIGHLIGHTED}; eligible is uncapped",
    ))

    # --- source_disagreement -----------------------------------------------
    # Items are (player_id, view) so the subject is the same player key the
    # waiver and drop rules use.
    source_views = _items(lambda ld: list((_get(ld, "source_views", {}) or {}).items()))
    consensus_constants = (
        ("STRONG_CONSENSUS_MAX_GAP", sd.STRONG_CONSENSUS_MAX_GAP),
        ("SIGNIFICANT_RANK_GAP", sd.SIGNIFICANT_RANK_GAP),
        ("HIGH_RANK_GAP", sd.HIGH_RANK_GAP),
        ("RANK_GAP_SCALE_PER_PLACE", sd.RANK_GAP_SCALE_PER_PLACE),
    )
    view_subject = lambda kv: _player(kv[0])  # noqa: E731
    view_position = lambda kv: _pos_of(kv[1])  # noqa: E731
    rules += _bucket_rules(
        "source_disagreement", "Consensus",
        source_views,
        lambda kv: kv[1].consensus,
        (sd.STRONG_CONSENSUS, sd.NORMAL_CONSENSUS, sd.SOURCE_DISAGREEMENT, sd.HIGH_DISAGREEMENT),
        consensus_constants,
        describe=lambda kv: f"{kv[1].name} gap {kv[1].consensus_gap} ({kv[1].consensus_pair[0]} vs {kv[1].consensus_pair[1]})",
        magnitude=lambda kv: float(kv[1].consensus_gap or 0),
        missing=lambda kv: kv[1].consensus_gap is None,
        subject=view_subject, position=view_position,
    )
    rules += _bucket_rules(
        "source_disagreement", "Direction",
        source_views,
        lambda kv: kv[1].direction,
        (sd.MARKET_ABOVE_PROJECTION, sd.PROJECTION_ABOVE_MARKET),
        (("DYNASTY_PAIR", sd.DYNASTY_PAIR), ("REDRAFT_PAIR", sd.REDRAFT_PAIR)),
        describe=lambda kv: f"{kv[1].name} market {kv[1].market_rank} vs projection {kv[1].projection_rank}",
        subject=view_subject, position=view_position,
    )
    rules.append(RuleSpec(
        module="source_disagreement", name="Direction: none",
        constants=(("DYNASTY_PAIR", sd.DYNASTY_PAIR), ("REDRAFT_PAIR", sd.REDRAFT_PAIR)),
        observe=_over(source_views, lambda kv: kv[1].direction is None, subject=view_subject, position=view_position),
    ))
    rules.append(RuleSpec(
        module="source_disagreement", name="Expert note present",
        constants=(("SIGNIFICANT_RANK_GAP", sd.SIGNIFICANT_RANK_GAP),),
        observe=_over(
            source_views,
            lambda kv: kv[1].expert_note is not None,
            describe=lambda kv: f"{kv[1].name}: {kv[1].expert_note}",
            subject=view_subject, position=view_position,
        ),
    ))

    # --- trade_opportunity_cost --------------------------------------------
    economics = _items(lambda ld: [(i, e) for i, e in enumerate(_economics(ld)) if e is not None])
    econ_subject = lambda pair: _trade(pair[0])  # noqa: E731
    roster_econ_constants = (
        ("IMPROVES_MIN", toc.IMPROVES_MIN), ("COSTS_MAX", toc.COSTS_MAX), ("MAJOR_COST_MAX", toc.MAJOR_COST_MAX),
    )
    rules += _bucket_rules(
        "trade_opportunity_cost", "Roster economics",
        economics,
        lambda pair: pair[1].roster_economics,
        (toc.IMPROVES_LINEUP, toc.MOSTLY_NEUTRAL, toc.COSTS_LINEUP, toc.MAJOR_LINEUP_COST),
        roster_econ_constants,
        describe=lambda pair: f"{pair[1].weekly_delta:+.1f}/wk",
        magnitude=lambda pair: abs(pair[1].weekly_delta or 0.0),
        missing=lambda pair: pair[1].weekly_delta is None,
        subject=econ_subject,
        inputs=(FACT_MOVE_DELTA,),
    )
    rules += _bucket_rules(
        "trade_opportunity_cost", "Asset economics",
        economics,
        lambda pair: pair[1].asset_economics,
        (toc.FAVORABLE, toc.ROUGHLY_EVEN, toc.UNFAVORABLE),
        (("_ASSET_BY_BALANCE", tuple(sorted(toc._ASSET_BY_BALANCE))),),
        subject=econ_subject,
    )
    rules.append(RuleSpec(
        module="trade_opportunity_cost", name="Strategic Tradeoff",
        constants=roster_econ_constants,
        observe=_over(
            economics,
            lambda pair: bool(pair[1].strategic_tradeoff),
            describe=lambda pair: f"assets {pair[1].asset_economics.lower()}, lineup {str(pair[1].roster_economics).lower()}",
            magnitude=lambda pair: abs(pair[1].weekly_delta or 0.0),
            subject=econ_subject,
        ),
        inputs=(FACT_MOVE_DELTA,),
    ))
    rules.append(RuleSpec(
        module="trade_opportunity_cost", name="Scarcity note present",
        constants=(("VERY_SCARCE", rv.VERY_SCARCE),),
        observe=_over(
            economics,
            lambda pair: pair[1].scarcity_note is not None,
            describe=lambda pair: str(pair[1].scarcity_note),
            subject=econ_subject,
        ),
        inputs=(FACT_SCARCITY,),
    ))

    # --- market_velocity ---------------------------------------------------
    velocity_constants = (
        ("MIN_OBSERVATIONS", mv.MIN_OBSERVATIONS),
        ("DIRECTIONAL_MIN_MOVE", mv.DIRECTIONAL_MIN_MOVE),
        ("RAPID_MIN_MOVE", mv.RAPID_MIN_MOVE),
        ("MIN_CONSECUTIVE_MOVES", mv.MIN_CONSECUTIVE_MOVES),
    )
    rules += _bucket_rules(
        "market_velocity", "Velocity",
        _items(lambda ld: list((_get(ld, "velocity", {}) or {}).items())),
        lambda kv: kv[1].label,
        (mv.INSUFFICIENT_HISTORY, mv.UNMEASURABLE, mv.STABLE, mv.RISING, mv.RAPIDLY_RISING, mv.FALLING, mv.RAPIDLY_FALLING),
        velocity_constants,
        describe=lambda kv: (
            f"{kv[1].observations} obs, move {kv[1].total_move:+.0%}" if kv[1].total_move is not None
            else f"{kv[1].observations} obs"
        ),
        magnitude=lambda kv: abs(kv[1].total_move or 0.0),
        subject=lambda kv: _player(kv[0]),
    )

    # --- role_trends (usage) -----------------------------------------------
    role_items = _items(lambda ld: list((_get(ld, "role_trends", {}) or {}).items()))
    role_constants = (("MIN_GAMES_FOR_TREND", rt.MIN_GAMES_FOR_TREND),
                      ("MIN_GAMES_FOR_STRONG", rt.MIN_GAMES_FOR_STRONG))
    rules += _bucket_rules(
        "role_trends", "Role",
        role_items,
        lambda kv: _get(kv[1], "label"),
        (rt.INSUFFICIENT, rt.STABLE, rt.RISING, rt.SURGING, rt.FALLING, rt.COLLAPSING),
        role_constants,
        describe=lambda kv: f"{kv[0]}: {_get(kv[1], 'note') or _get(kv[1], 'label')}",
        magnitude=lambda kv: float(_get(kv[1], "games", 0) or 0),
        subject=lambda kv: _player(kv[0]),
        note="this IS the role fact; empty until the season has usage rows",
    )
    rules += _bucket_rules(
        "role_trends", "Role vs market",
        _items(lambda ld: list((_get(ld, "role_market", {}) or {}).items())),
        lambda kv: kv[1],
        (rt.ROLE_AHEAD, rt.MARKET_AHEAD, rt.CONFIRM),
        role_constants,
        describe=lambda kv: f"{kv[0]}: {kv[1]}",
        subject=lambda kv: _player(kv[0]),
        inputs=(FACT_ROLE,),
    )

    # --- matchup_leverage --------------------------------------------------
    rules += _bucket_rules(
        "matchup_leverage", "Matchup",
        _items(lambda ld: [m] if (m := _get(ld, "matchup")) is not None else []),
        lambda m: m.label,
        (ml.STRONG_EDGE, ml.MODEST_EDGE, ml.NEAR_EVEN, ml.MODEST_DEFICIT, ml.LARGE_DEFICIT),
        (("STRONG_EDGE_MIN", ml.STRONG_EDGE_MIN), ("MODEST_EDGE_MIN", ml.MODEST_EDGE_MIN)),
        describe=lambda m: f"vs {m.opponent_name}, {m.gap:+.1f}",
        magnitude=lambda m: abs(m.gap),
        subject=lambda m: "league",
        note="one observation per league per week — structurally sample-starved by design",
    )

    # --- opponent_blocker --------------------------------------------------
    rules.append(RuleSpec(
        module="opponent_blocker", name="Defensive add found",
        constants=(("OPPONENT_GAIN_MIN", ob.OPPONENT_GAIN_MIN), ("MAX_CANDIDATES", ob.MAX_CANDIDATES)),
        observe=_over(
            _leagues_where(lambda ld: _get(ld, "matchup") is not None and not _get(ld, "waivers_note")),
            lambda ld: _get(ld, "defensive_add") is not None,
            describe=lambda ld: f"{ld.defensive_add.target.name} blocks {ld.defensive_add.opponent_name} "
                                f"(+{ld.defensive_add.opponent_gain:.1f} to them)",
            magnitude=lambda ld: ld.defensive_add.opponent_gain,
            subject=lambda ld: "league",
        ),
    ))

    # --- streamer_planner --------------------------------------------------
    rules += _bucket_rules(
        "streamer_planner", "Streamer",
        _items(lambda ld: _get(ld, "streamers", []) or []),
        lambda s: s.recommendation,
        (sp.HOLD, sp.ADD, sp.SEQUENCE),
        (
            ("MIN_GAIN_OVER_HOLD", sp.MIN_GAIN_OVER_HOLD),
            ("SINGLE_PREFERENCE_TOLERANCE", sp.SINGLE_PREFERENCE_TOLERANCE),
            ("PLAN_WEEKS", sp.PLAN_WEEKS),
        ),
        describe=lambda s: f"{s.position}: {s.note}",
        subject=lambda s: f"pos:{s.position}",
    )

    # --- roster_consolidation ----------------------------------------------
    rules.append(RuleSpec(
        module="roster_consolidation", name="Consolidation found",
        constants=(
            ("MIN_WEEKLY_IMPROVEMENT", rcs.MIN_WEEKLY_IMPROVEMENT),
            ("VALUE_RATIO_MIN", rcs.VALUE_RATIO_MIN),
            ("VALUE_RATIO_MAX", rcs.VALUE_RATIO_MAX),
            ("MAX_PER_TEAM", rcs.MAX_PER_TEAM),
        ),
        observe=_over(
            _leagues_where(lambda ld: rcs.eligible(_get(ld, "team_status"))),
            lambda ld: bool(_get(ld, "consolidations")),
            describe=lambda ld: ld.consolidations[0].describe(),
            magnitude=lambda ld: ld.consolidations[0].weekly_gain,
            subject=lambda ld: "league",
        ),
        note=f"eligible = contender or middling at >= {rcs.STRONG_MIDDLING_MIN_PERCENTILE:g}th percentile strength",
    ))

    # --- stash_board -------------------------------------------------------
    rules += _bucket_rules(
        "stash_board", "Stash",
        _items(lambda ld: _get(ld, "stash", []) or []),
        lambda s: s.label,
        (sb.PRIORITY_STASH, sb.WATCH),
        (
            ("PRIORITY_MIN_PERCENTILE", sb.PRIORITY_MIN_PERCENTILE),
            ("WATCH_MIN_PERCENTILE", sb.WATCH_MIN_PERCENTILE),
            ("STASH_MAX", sb.STASH_MAX),
        ),
        describe=lambda s: f"{s.entry.name} ({s.percentile:.0f}th pctl)",
        magnitude=lambda s: s.percentile,
        subject=lambda s: _player(s.entry.player_id),
        position=_entry_pos,
        note=f"board is capped at STASH_MAX={sb.STASH_MAX} per league",
        inputs=(FACT_SCARCITY,),
    )

    # --- schedule_window ---------------------------------------------------
    rules.append(RuleSpec(
        module="schedule_window", name="Start/sit schedule tiebreak",
        constants=(("TIEBREAK_MAX_VALUE_GAP", sw.TIEBREAK_MAX_VALUE_GAP), ("NEXT_GAMES_WINDOW", sw.NEXT_GAMES_WINDOW)),
        observe=_over(
            _items(lambda ld: _get(_get(ld, "lineup_leverage"), "close_calls", []) or []),
            lambda d: _get(d, "schedule_note") is not None,
            describe=lambda d: f"{d.slot}: {d.schedule_note}",
            subject=lambda d: f"slot:{d.slot}",
        ),
    ))
    rules.append(RuleSpec(
        module="schedule_window", name="Waiver schedule note",
        constants=(("NEXT_GAMES_WINDOW", sw.NEXT_GAMES_WINDOW),),
        observe=_over(
            _items(_targets),
            lambda t: any(str(n).startswith("Schedule:") for n in (_get(t, "notes", []) or [])),
            describe=lambda t: f"{t.name}: " + next(str(n) for n in t.notes if str(n).startswith("Schedule:")),
            subject=lambda t: _player(t.player_id),
            position=_pos_of,
        ),
    ))

    # --- buyer_board -------------------------------------------------------
    rules += _bucket_rules(
        "buyer_board", "Buyer fit",
        _items(lambda ld: [(b, f) for b in (_get(ld, "buyer_boards", []) or []) for f in (_get(b, "all_fits", []) or [])]),
        lambda pair: pair[1].label,
        (bb.STRONG_FIT, bb.POSSIBLE_FIT, bb.POOR_FIT),
        (
            ("STRONG_FIT_MIN", bb.STRONG_FIT_MIN),
            ("POSSIBLE_FIT_MIN", bb.POSSIBLE_FIT_MIN),
            ("UNFUNDED_PENALTY", bb.UNFUNDED_PENALTY),
        ),
        describe=lambda pair: f"{pair[1].username} score {pair[1].score} ({'; '.join(pair[1].reasons)})",
        magnitude=lambda pair: float(pair[1].score),
        subject=lambda pair: f"buyer:{_get(_get(pair[0], 'candidate'), 'player_id')}:{pair[1].username}",
        position=lambda pair: _pos_of(_get(pair[0], "candidate")),
        note=f"only the top MAX_BUYERS={bb.MAX_BUYERS} are shown to the reader; eligible counts every scored counterparty",
        inputs=(FACT_SCARCITY,),
    )

    # --- recommendation_conflicts ------------------------------------------
    rules.append(RuleSpec(
        module="recommendation_conflicts", name="Trade conflict",
        constants=(("CONFLICTED", rc.CONFLICTED),),
        observe=_over(
            _items(lambda ld: [(i, p, rc.conflict_for(_conflicts(ld), rc.TRADE, str(i))) for i, p, _, _ in _pairs(ld)]),
            lambda t: t[2] is not None,
            describe=lambda t: f"{t[1].summary_line()} — {'; '.join(t[2].reasons_against)}",
            magnitude=lambda t: float(len(t[2].reasons_against)),
            subject=lambda t: _trade(t[0]),
        ),
        inputs=(FACT_SCARCITY, FACT_MOVE_DELTA),
    ))
    rules.append(RuleSpec(
        module="recommendation_conflicts", name="Waiver conflict",
        constants=(("DEVELOPMENTAL_DROP_MIN_PERCENTILE", rc.DEVELOPMENTAL_DROP_MIN_PERCENTILE),),
        observe=_over(
            _items(lambda ld: [(t, rc.conflict_for(_conflicts(ld), rc.WAIVER, t.player_id)) for t in _targets(ld)]),
            lambda pair: pair[1] is not None,
            describe=lambda pair: f"Add {pair[0].name} — {'; '.join(pair[1].reasons_against)}",
            magnitude=lambda pair: float(len(pair[1].reasons_against)),
            subject=lambda pair: _player(pair[0].player_id),
            position=lambda pair: _pos_of(pair[0]),
        ),
    ))
    for family, needle, family_inputs in CONFLICT_REASON_FAMILIES:
        rules.append(RuleSpec(
            module="recommendation_conflicts", name=f"Conflict reason: {family}",
            constants=(("substring", needle),),
            observe=_over(
                _items(_conflicts),
                lambda c, needle=needle: any(needle in r for r in (_get(c, "reasons_against", []) or [])),
                describe=lambda c: f"{c.subject} — {'; '.join(c.reasons_against)}",
                subject=_conflict_subject,
            ),
            note="eligible = every conflict raised; a conflict can belong to more than one family",
            inputs=family_inputs,
        ))

    # --- move_impact -------------------------------------------------------
    impacts = _items(lambda ld: [(i, p, imp) for i, p, _, imp in _pairs(ld)])
    rules.append(RuleSpec(
        module="move_impact", name="Trade preview present",
        constants=(("MIN_ACCEPTANCE_FOR_PREVIEW", mi.MIN_ACCEPTANCE_FOR_PREVIEW),),
        observe=_over(impacts, lambda t: t[2] is not None, subject=lambda t: _trade(t[0])),
    ))
    rules.append(RuleSpec(
        module="move_impact", name="Trade preview has material delta",
        constants=(
            ("MATERIAL_WEEKLY_POINTS", mi.MATERIAL_WEEKLY_POINTS),
            ("MATERIAL_VALUE_RATIO", mi.MATERIAL_VALUE_RATIO),
            ("MATERIAL_AGE_YEARS", mi.MATERIAL_AGE_YEARS),
            ("MATERIAL_STATUS_PERCENTILE", mi.MATERIAL_STATUS_PERCENTILE),
        ),
        observe=_over(
            impacts,
            lambda t: bool(t[2].material_deltas()),
            describe=lambda t: f"{t[1].summary_line()} — {'; '.join(t[2].material_deltas())}",
            magnitude=lambda t: abs(_get(t[2], "weekly_points_delta", 0.0) or 0.0),
            missing=lambda t: t[2] is None,
            subject=lambda t: _trade(t[0]),
        ),
        note="missing = proposals below the preview bar, so no impact was computed",
        inputs=(FACT_MOVE_DELTA,),
    ))
    rules.append(RuleSpec(
        module="move_impact", name="Waiver preview present",
        constants=(("PREVIEWED_WAIVER_TIERS", mi.PREVIEWED_WAIVER_TIERS),),
        observe=_over(
            _items(lambda ld: [(t, (_get(ld, "waiver_impacts", {}) or {}).get(t.player_id))
                               for t in _targets(ld) if _get(t, "priority_tier") in mi.PREVIEWED_WAIVER_TIERS]),
            lambda pair: pair[1] is not None,
            subject=lambda pair: _player(pair[0].player_id),
            position=lambda pair: _pos_of(pair[0]),
        ),
    ))

    # --- lineup_leverage ---------------------------------------------------
    rules += _bucket_rules(
        "lineup_leverage", "Start/sit",
        _items(lambda ld: _get(_get(ld, "lineup_leverage"), "decisions", []) or []),
        lambda d: d.label,
        (ll.CLEAR_START, ll.LEAN_START, ll.TOSS_UP),
        (("TOSS_UP_RATIO", ll.TOSS_UP_RATIO), ("LEAN_START_RATIO", ll.LEAN_START_RATIO)),
        describe=lambda d: f"{d.slot}: {d.starter.name} vs {d.alternative.name if d.alternative else '—'}",
        subject=lambda d: f"slot:{d.slot}",
        position=lambda d: _pos_of(_get(d, "starter")),
    )
    rules.append(RuleSpec(
        module="lineup_leverage", name="Bench surplus",
        constants=(("BENCH_SURPLUS_RATIO", ll.BENCH_SURPLUS_RATIO), ("MAX_SURPLUS_LISTED", ll.MAX_SURPLUS_LISTED)),
        observe=_over(
            _items(_projected_bench_with_surplus),
            lambda pair: pair[0].player_id in pair[1],
            describe=lambda pair: f"{pair[0].name}",
            subject=lambda pair: _player(pair[0].player_id),
            position=lambda pair: _pos_of(pair[0]),
        ),
        note=f"triggered list is capped at MAX_SURPLUS_LISTED={ll.MAX_SURPLUS_LISTED}; eligible is every projected bench player",
    ))

    # --- roster_clog -------------------------------------------------------
    rules.append(RuleSpec(
        module="roster_clog", name="Roster clog",
        constants=(
            ("DYNASTY_CLOG_RANK_CUTOFF", rcl.DYNASTY_CLOG_RANK_CUTOFF),
            ("REDRAFT_CLOG_RANK_CUTOFF", rcl.REDRAFT_CLOG_RANK_CUTOFF),
            ("MAX_CLOGS_PER_ROSTER", rcl.MAX_CLOGS_PER_ROSTER),
            ("DEVELOPMENTAL_MAX_YEARS_EXP", rcl.DEVELOPMENTAL_MAX_YEARS_EXP),
        ),
        observe=_over(
            _items(_bench_with_clog_ids),
            lambda pair: pair[0].player_id in pair[1],
            describe=lambda pair: pair[0].name,
            subject=lambda pair: _player(pair[0].player_id),
            position=lambda pair: _pos_of(pair[0]),
        ),
        note=(
            f"triggered list is capped at MAX_CLOGS_PER_ROSTER={rcl.MAX_CLOGS_PER_ROSTER} and further filtered by "
            "drop-candidate overlap in report_data; eligible is every non-starter"
        ),
        inputs=(FACT_TRENDING,),
    ))

    # --- contender_insurance -----------------------------------------------
    rules.append(RuleSpec(
        module="contender_insurance", name="Insurance recommended",
        constants=(
            ("FRAGILE_REPLACEMENT_RATIO", ci.FRAGILE_REPLACEMENT_RATIO),
            ("INSURANCE_MIN_IMPROVEMENT", ci.INSURANCE_MIN_IMPROVEMENT),
            ("MAX_INSURANCE_PER_TEAM", ci.MAX_INSURANCE_PER_TEAM),
        ),
        observe=_over(
            _leagues_where(lambda ld: _get(_get(ld, "team_status"), "status") == CONTENDER),
            lambda ld: bool(_get(ld, "insurance")),
            describe=lambda ld: "; ".join(f"{r.starter.name} -> {r.candidate.name}" for r in ld.insurance),
            magnitude=lambda ld: float(len(ld.insurance)),
            subject=lambda ld: "league",
        ),
        note="eligible = contender leagues only, by design; report_data drops rows for Abundant/Normal positions",
        inputs=(FACT_SCARCITY,),
    ))

    # --- bye_collision -----------------------------------------------------
    rules.append(RuleSpec(
        module="bye_collision", name="Bye collision found",
        constants=(("LOOKAHEAD_WEEKS", byc.LOOKAHEAD_WEEKS), ("BYE_HOLE_REPLACEMENT_RATIO", byc.BYE_HOLE_REPLACEMENT_RATIO)),
        observe=_over(
            _leagues_where(lambda ld: _get(ld, "lineup") is not None),
            lambda ld: _get(ld, "bye_collision") is not None,
            describe=lambda ld: f"week {ld.bye_collision.week}, {len(ld.bye_collision.holes)} hole(s)",
            magnitude=lambda ld: float(len(ld.bye_collision.holes)),
            subject=lambda ld: "league",
        ),
    ))

    # --- playoff_leverage --------------------------------------------------
    playoff_constants = (
        ("MIN_GAMES_FOR_LABEL", pl.MIN_GAMES_FOR_LABEL),
        ("COMFORTABLE_MARGIN_WINS", pl.COMFORTABLE_MARGIN_WINS),
        ("BUBBLE_MARGIN_WINS", pl.BUBBLE_MARGIN_WINS),
    )
    rules.append(RuleSpec(
        module="playoff_leverage", name="Playoff leverage available",
        constants=playoff_constants,
        observe=_over(_leagues_where(lambda ld: True), lambda ld: _get(ld, "playoff") is not None, subject=lambda ld: "league"),
    ))
    rules += _bucket_rules(
        "playoff_leverage", "Playoff",
        _items(lambda ld: [p] if (p := _get(ld, "playoff")) is not None else []),
        lambda p: p.label,
        (pl.COMFORTABLE, pl.BUBBLE, pl.LONG_SHOT, pl.OUT),
        playoff_constants,
        describe=lambda p: f"seed {p.seed} of {p.playoff_teams}, {p.wins}-{p.losses}",
        subject=lambda p: "league",
    )
    rules.append(RuleSpec(
        module="playoff_leverage", name="Deadline window",
        constants=(("DEADLINE_WINDOW_WEEKS", pl.DEADLINE_WINDOW_WEEKS),),
        observe=_over(
            _items(lambda ld: [p] if (p := _get(ld, "playoff")) is not None else []),
            lambda p: bool(p.deadline_window),
            describe=lambda p: f"deadline week {p.trade_deadline_week}",
            subject=lambda p: "league",
        ),
    ))

    # --- pick_opportunity --------------------------------------------------
    rules += _bucket_rules(
        "pick_opportunity", "Pick",
        _items(lambda ld: _get(_get(ld, "pick_opportunity"), "assessments", []) or []),
        lambda a: a.classification,
        (po.STRATEGIC, po.USEFUL, po.SPENDABLE),
        (("BOTTOM_UNITS", po.BOTTOM_UNITS), ("ASSESSED_ROUNDS", po.ASSESSED_ROUNDS)),
        describe=lambda a: f"{a.pick.name}: {a.reason}",
        subject=lambda a: f"pick:{a.pick.name} {_get(a, 'origin', '')}".strip(),
    )

    # --- portfolio_exposure ------------------------------------------------
    exposure_constants = (
        ("HIGH_EXPOSURE_LEAGUES", pe.HIGH_EXPOSURE_LEAGUES),
        ("VERY_HIGH_EXPOSURE_LEAGUES", pe.VERY_HIGH_EXPOSURE_LEAGUES),
    )
    for level in (pe.HIGH, pe.VERY_HIGH):
        rules.append(RuleSpec(
            module="portfolio_exposure", name=f"Exposure: {level}",
            constants=exposure_constants,
            observe=_over(
                _exposure_counts,
                lambda t, level=level: pe.exposure_level(t[2]) == level,
                describe=lambda t: f"{t[1]} in {t[2]} leagues",
                magnitude=lambda t: float(t[2]),
                subject=lambda t: _player(t[0]),
            ),
            min_report_leagues=pe.HIGH_EXPOSURE_LEAGUES,
            note="cross-league by construction: one observation per rostered player, not per league",
        ))

    # --- league_economy ----------------------------------------------------
    economy_constants = (
        ("FREQUENT_TRADER_MIN_TRADES", le.FREQUENT_TRADER_MIN_TRADES),
        ("PICK_NET_THRESHOLD", le.PICK_NET_THRESHOLD),
        ("POSITION_HEAVY_RATIO", le.POSITION_HEAVY_RATIO),
        ("MIN_LEAGUE_TRADES_FOR_ACTIVITY", le.MIN_LEAGUE_TRADES_FOR_ACTIVITY),
    )
    for label in (le.FREQUENT_TRADER, le.INACTIVE_TRADER, le.PICK_ACCUMULATOR, le.PICK_SELLER, le.POSITION_HEAVY):
        rules.append(RuleSpec(
            module="league_economy", name=f"Manager label: {label}",
            constants=economy_constants,
            observe=_over(
                _items(lambda ld: (_get(_get(ld, "league_economy"), "managers", {}) or {}).values()),
                lambda m, label=label: label in (_get(m, "labels", []) or []),
                describe=lambda m: f"{m.username or m.roster_id}: {', '.join(m.labels)}",
                subject=lambda m: f"manager:{_get(m, 'roster_id')}",
            ),
        ))

    # --- trade_engine ------------------------------------------------------
    proposals = _items(lambda ld: list(enumerate(_proposals(ld))))
    proposal_subject = lambda pair: _trade(pair[0])  # noqa: E731
    rules += _bucket_rules(
        "trade_engine", "Acceptance",
        proposals,
        lambda pair: _get(pair[1], "acceptance_rating"),
        te.ACCEPTANCE_TIERS,
        (("ACCEPTANCE_TIERS", te.ACCEPTANCE_TIERS),),
        describe=lambda pair: pair[1].summary_line(),
        subject=proposal_subject,
    )
    rules += _bucket_rules(
        "trade_engine", "Confidence",
        proposals,
        lambda pair: _get(pair[1], "confidence"),
        ("High", "Medium", "Low"),
        (("VALUE_TOLERANCE", te.VALUE_TOLERANCE),),
        subject=proposal_subject,
    )
    rules += _bucket_rules(
        "trade_engine", "Balance",
        proposals,
        lambda pair: _get(pair[1], "balance_label"),
        ("Favors me", "Balanced", "Slight overpay", "Overpay"),
        (("VALUE_TOLERANCE", te.VALUE_TOLERANCE),),
        describe=lambda pair: f"{pair[1].summary_line()} (ratio {pair[1].value_ratio:.2f})",
        subject=proposal_subject,
    )
    rules += _bucket_rules(
        "trade_engine", "Trade type",
        proposals,
        lambda pair: _get(pair[1], "trade_type"),
        ("buy_low", "sell_high", "pick_target", rcs.TRADE_TYPE),
        (("MAX_CANDIDATES_PER_OPPONENT", te.MAX_CANDIDATES_PER_OPPONENT), ("UNTOUCHABLE_COUNT", te.UNTOUCHABLE_COUNT)),
        subject=proposal_subject,
    )
    rules += _bucket_rules(
        "trade_engine", "Drop priority",
        _items(lambda ld: _get(ld, "drop_candidates", []) or []),
        lambda d: _get(d, "priority"),
        ("Strong Drop", "Consider Dropping"),
        (
            ("DROP_LOW_VALUE_PERCENTILE", te.DROP_LOW_VALUE_PERCENTILE),
            ("DROP_EXCESS_DEPTH_BUFFER", te.DROP_EXCESS_DEPTH_BUFFER),
        ),
        describe=lambda d: f"{d.entry.name}: {'; '.join(d.reasons)}",
        magnitude=lambda d: float(len(d.reasons)),
        subject=lambda d: _player(_get(_get(d, "entry"), "player_id")),
        position=_entry_pos,
    )

    # --- waiver_engine -----------------------------------------------------
    target_subject = lambda t: _player(_get(t, "player_id"))  # noqa: E731
    rules += _bucket_rules(
        "waiver_engine", "Priority tier",
        _items(_targets),
        lambda t: _get(t, "priority_tier"),
        (we.MUST_ADD, we.STRONG_ADD, we.MODERATE, we.SPECULATIVE, we.MONITOR, we.INSURANCE),
        (
            ("STASH_MIN_PERCENTILE", we.STASH_MIN_PERCENTILE),
            ("SEASON_STARTER_MIN_PERCENTILE", we.SEASON_STARTER_MIN_PERCENTILE),
            ("TOP_TREND_RANK_CUTOFF", we.TOP_TREND_RANK_CUTOFF),
        ),
        describe=lambda t: f"{t.name}: {t.reason}",
        subject=target_subject, position=_pos_of,
        inputs=(FACT_TRENDING,),
    )
    rules += _bucket_rules(
        "waiver_engine", "Horizon",
        _items(_targets),
        lambda t: _get(t, "horizon"),
        (we.BREAKOUT, we.SEASON_STARTER, we.STASH, we.STREAMER),
        (
            ("BREAKOUT_YEARS_EXP_THRESHOLD", we.BREAKOUT_YEARS_EXP_THRESHOLD),
            ("STASH_MIN_PERCENTILE", we.STASH_MIN_PERCENTILE),
            ("SEASON_STARTER_MIN_PERCENTILE", we.SEASON_STARTER_MIN_PERCENTILE),
        ),
        subject=target_subject, position=_pos_of,
        inputs=(FACT_TRENDING,),
    )
    rules += _bucket_rules(
        "waiver_engine", "Alert severity",
        _items(lambda ld: _get(ld, "time_sensitive", []) or []),
        lambda n: _get(n, "severity"),
        ("high", "medium", "low"),
        (("EARLY_SEASON_WEEK_CUTOFF", we.EARLY_SEASON_WEEK_CUTOFF),),
        describe=lambda n: f"{n.player_name}: {n.note}",
        subject=lambda n: f"alert:{_get(n, 'player_name')}",
    )

    # --- decision_delta ----------------------------------------------------
    rules += _bucket_rules(
        "decision_delta", "Decision delta",
        _delta_items,
        lambda i: _get(i, "kind"),
        (dd.STATUS, dd.ROSTER, dd.RECOMMENDATION, dd.VALUATION),
        (("VALUATION_DELTA_RATIO", dd.VALUATION_DELTA_RATIO), ("SNAPSHOTS_KEPT", dd.SNAPSHOTS_KEPT)),
        describe=lambda i: i.text,
    )
    return rules


def _conflict_subject(c) -> str | None:
    """A conflict is keyed on the thing it argues about: a proposal index
    or a waiver player_id."""
    kind, key = _get(c, "kind"), _get(c, "key")
    if kind == rc.TRADE:
        return _trade(key)
    if kind == rc.WAIVER:
        return _player(key)
    return None


# (family, reason substring, tracked facts the reason restates)
CONFLICT_REASON_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Major Lineup Cost", toc.MAJOR_LINEUP_COST, (FACT_MOVE_DELTA,)),
    ("Very Scarce market", rv.VERY_SCARCE, (FACT_SCARCITY,)),
    ("Cross-league exposure", "exposure", ()),
    ("Strategic pick", "Strategic pick", ()),
    ("Drop is a starter", "optimized starter", ()),
    ("Developmental drop", "developmental hold", ()),
    ("Bye-hole fill", "bye hole", ()),
)


def _measurable_replacement_players(report) -> Iterator[tuple[LeagueContext, tuple]]:
    """(context, player_id, understated ids, overstated ids) for every player
    the replacement market could actually measure. Eligible is the measurable
    set, NOT the capped highlight lists."""
    for ld in active_leagues(report):
        market = _get(ld, "replacement")
        if market is None:
            continue
        league = league_context(ld)
        understated = {c.entry.player_id for c in (_get(market, "understated", []) or [])}
        overstated = {c.entry.player_id for c in (_get(market, "overstated", []) or [])}
        for ctx in (_get(market, "players", {}) or {}).values():
            if ctx.projection_over_waiver is None:
                continue
            yield league, (ctx, ctx.entry.player_id, understated, overstated)


def _projected_bench_with_surplus(ld) -> Iterator[tuple]:
    leverage = _get(ld, "lineup_leverage")
    if leverage is None:
        return
    surplus = {s.entry.player_id for s in (_get(leverage, "bench_surplus", []) or [])}
    for e in _bench_entries(ld):
        if _get(_get(e, "value"), "proj_points") is None:
            continue
        yield e, surplus


def _bench_with_clog_ids(ld) -> Iterator[tuple]:
    if _get(ld, "lineup") is None:
        return
    clogs = {c.entry.player_id for c in (_get(ld, "roster_clogs", []) or [])}
    for e in _bench_entries(ld):
        yield e, clogs


def _exposure_counts(report) -> Iterator[tuple[LeagueContext, tuple[str, str, int]]]:
    portfolio = _get(report, "portfolio")
    if portfolio is None:
        return
    names = {p.player_id: p.name for p in (_get(portfolio, "players", []) or [])}
    for pid, count in sorted((_get(portfolio, "counts_by_player_id", {}) or {}).items()):
        yield CROSS_LEAGUE_CTX, (pid, names.get(pid, pid), count)


def _delta_items(report) -> Iterator[tuple[str, Any]]:
    delta = _get(report, "delta")
    for item in (_get(delta, "items", []) or []):
        yield _get(item, "league_name") or CROSS_LEAGUE, item


RULES: list[RuleSpec] = build_rules()


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------


def diagnose_all(
    eligible: int,
    triggered: int,
    by_league: Counter,
    leagues_eligible: int,
    *,
    by_format: Counter | None = None,
    formats_eligible: Counter | None = None,
    by_position: Counter | None = None,
    positions_eligible: Counter | None = None,
) -> list[str]:
    """Every label that applies, in LABEL_PRIORITY order. Too little data
    is the only label that silences the rest: nothing else can be judged
    on it. Zero triggers is Never Fires with enough chances and Rare below
    that. The rate bands (Nearly Universal / Overactive / Rare) are
    exclusive; the where-labels (league, format, position) stack on top."""
    if eligible < MIN_SAMPLE:
        return [INSUFFICIENT_SAMPLE]
    if triggered == 0:
        return [NEVER_FIRES if eligible >= NEVER_FIRES_MIN_ELIGIBLE else RARE]
    labels: list[str] = []
    rate = triggered / eligible
    if rate >= NEARLY_ALWAYS_FIRES_MIN_RATE:
        labels.append(NEARLY_UNIVERSAL)
    elif rate >= OVERACTIVE_MIN_RATE:
        labels.append(OVERACTIVE)
    elif rate < RARE_MAX_RATE:
        labels.append(RARE)
    if (
        triggered >= LEAGUE_CONCENTRATION_MIN_TRIGGERS
        and leagues_eligible >= LEAGUE_CONCENTRATION_MIN_LEAGUES
        and by_league
        and max(by_league.values()) / triggered >= LEAGUE_CONCENTRATION_MIN_SHARE
    ):
        labels.append(LEAGUE_CONCENTRATED)
    if format_bias(triggered, by_format or Counter(), formats_eligible or Counter()) is not None:
        labels.append(FORMAT_BIASED)
    if position_bias(triggered, by_position or Counter(), positions_eligible or Counter()) is not None:
        labels.append(POSITION_BIASED)
    labels.sort(key=LABEL_PRIORITY.index)
    return labels or [HEALTHY]


def diagnose(eligible: int, triggered: int, by_league: Counter, leagues_eligible: int, **kw) -> str:
    """The first label by priority — the one the table shows."""
    return diagnose_all(eligible, triggered, by_league, leagues_eligible, **kw)[0]


def _axis(fmt: str, axis: int) -> str:
    """Split "SF dynasty" into its two format axes. Unknown halves stay '?'."""
    parts = fmt.split(" ", 1)
    return parts[axis] if len(parts) > axis else UNKNOWN_FORMAT


def format_bias(triggered: int, by_format: Counter, formats_eligible: Counter) -> tuple[str, int, float] | None:
    """(format, triggers from it, its share of eligible) when one format on
    either axis — superflex/1QB or dynasty/keeper/redraft — supplied at
    least FORMAT_SHARE of the triggers while being a smaller share of what
    was eligible. A format that IS most of the eligible set explains its
    own share; that is the leagues, not the rule."""
    if triggered < BIAS_MIN_TRIGGERS:
        return None
    for axis in (0, 1):
        trig = Counter()
        elig = Counter()
        for fmt, n in by_format.items():
            trig[_axis(fmt, axis)] += n
        for fmt, n in formats_eligible.items():
            elig[_axis(fmt, axis)] += n
        trig.pop(UNKNOWN_FORMAT, None)
        known_elig = {k: v for k, v in elig.items() if k != UNKNOWN_FORMAT}
        if len(known_elig) < 2 or not trig:
            continue
        top, n = trig.most_common(1)[0]
        total_elig = sum(known_elig.values())
        elig_share = known_elig.get(top, 0) / total_elig if total_elig else 1.0
        if n / triggered >= FORMAT_SHARE and elig_share < FORMAT_SHARE:
            return top, n, elig_share
    return None


def position_bias(triggered: int, by_position: Counter, positions_eligible: Counter) -> tuple[str, int, float] | None:
    """(position, triggers at it, its share of eligible) when one position
    holds at least POSITION_SHARE of a player-level rule's triggers and at
    least POSITION_BIAS_MIN_POSITIONS positions were eligible. The
    eligible-share guard is the same as the format one: a rule that only
    ever sees WRs is not WR-biased."""
    if triggered < BIAS_MIN_TRIGGERS or not by_position:
        return None
    known_elig = {k: v for k, v in positions_eligible.items() if k}
    if len(known_elig) < POSITION_BIAS_MIN_POSITIONS:
        return None
    top, n = by_position.most_common(1)[0]
    total_elig = sum(known_elig.values())
    elig_share = known_elig.get(top, 0) / total_elig if total_elig else 1.0
    if n / triggered >= POSITION_SHARE and elig_share < POSITION_SHARE:
        return top, n, elig_share
    return None


def evaluate(spec: RuleSpec, report) -> RuleResult:
    observations = spec.observe(report)
    eligible_obs = [o for o in observations if not o.missing]
    missing = len(observations) - len(eligible_obs)
    triggered_obs = [o for o in eligible_obs if o.triggered]
    by_league = Counter(o.league for o in triggered_obs)
    leagues_eligible = len({o.league for o in eligible_obs})
    eligible = len(eligible_obs)
    by_format = Counter(o.fmt for o in triggered_obs)
    formats_eligible = Counter(o.fmt for o in eligible_obs)
    by_position = Counter(o.position for o in triggered_obs if o.position)
    positions_eligible = Counter(o.position for o in eligible_obs if o.position)

    diagnostics = diagnose_all(
        eligible, len(triggered_obs), by_league, leagues_eligible,
        by_format=by_format, formats_eligible=formats_eligible,
        by_position=by_position, positions_eligible=positions_eligible,
    )
    if spec.min_report_leagues and len(active_leagues(report)) < spec.min_report_leagues:
        diagnostics = [INSUFFICIENT_SAMPLE]

    bias_detail = ""
    fb = format_bias(len(triggered_obs), by_format, formats_eligible)
    if fb is not None:
        bias_detail = f"{fb[1]} of {len(triggered_obs)} triggers from {fb[0]} leagues ({fb[2]:.0%} of eligible)"
    pb = position_bias(len(triggered_obs), by_position, positions_eligible)
    if pb is not None:
        bias_detail += ("; " if bias_detail else "") + f"{pb[1]} of {len(triggered_obs)} triggers at {pb[0]} ({pb[2]:.0%} of eligible)"

    ranked = sorted(
        (o for o in triggered_obs if o.example),
        key=lambda o: (-o.magnitude, o.example or ""),
    )
    return RuleResult(
        module=spec.module,
        name=spec.name,
        constants=spec.constants,
        eligible=eligible,
        triggered=len(triggered_obs),
        rate=(len(triggered_obs) / eligible) if eligible else None,
        diagnostic=diagnostics[0],
        leagues_triggered=sorted(by_league),
        leagues_eligible=leagues_eligible,
        examples=[o.example for o in ranked[:MAX_EXAMPLES] if o.example],
        missing=missing,
        missing_data_rate=(missing / (missing + eligible)) if (missing + eligible) else None,
        note=spec.note,
        time_gated=TIME_GATED.get(spec.name),
        diagnostics=diagnostics,
        by_format=dict(by_format),
        formats_eligible=dict(formats_eligible),
        by_position=dict(by_position),
        positions_eligible=dict(positions_eligible),
        subjects=frozenset(f"{o.league}|{o.subject}" for o in triggered_obs if o.subject),
        inputs=spec.inputs,
        bias_detail=bias_detail,
    )


# --------------------------------------------------------------------------
# Potential double counts: two rules whose trigger sets are mostly the same
# subjects. Computed after every rule is evaluated, so it is a label added
# to results, not a rule of its own.
# --------------------------------------------------------------------------


def find_double_counts(results: list[RuleResult]) -> list[DoubleCountFinding]:
    """Overlap is measured against the SMALLER trigger set (containment),
    with two guards: each set needs OVERLAP_MIN_TRIGGERS, and the larger
    rule must not itself fire on >= OVERLAP_SHARE of its eligible set —
    a rule that fires on nearly everything contains every other set
    trivially, which says nothing about the pair."""
    out: list[DoubleCountFinding] = []
    candidates = [r for r in results if len(r.subjects) >= OVERLAP_MIN_TRIGGERS]
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            small, large = (a, b) if len(a.subjects) <= len(b.subjects) else (b, a)
            if large.rate is not None and large.rate >= OVERLAP_SHARE:
                continue
            overlap = a.subjects & b.subjects
            share = len(overlap) / len(small.subjects)
            if share < OVERLAP_SHARE:
                continue
            out.append(DoubleCountFinding(
                rule_a=f"{a.module}.{a.name}", rule_b=f"{b.module}.{b.name}",
                overlap=len(overlap), smaller=len(small.subjects), share=share,
                examples=sorted(overlap)[:MAX_EXAMPLES],
            ))
    return out


def _apply_double_count_labels(results: list[RuleResult], findings: list[DoubleCountFinding]) -> None:
    named = {f.rule_a for f in findings} | {f.rule_b for f in findings}
    for r in results:
        if f"{r.module}.{r.name}" in named and POTENTIAL_DOUBLE_COUNT not in r.diagnostics:
            labels = [d for d in r.diagnostics if d != HEALTHY] + [POTENTIAL_DOUBLE_COUNT]
            r.diagnostics = sorted(labels, key=LABEL_PRIORITY.index)
            r.diagnostic = r.diagnostics[0]


# --------------------------------------------------------------------------
# Cross-signal double-statement checks
# --------------------------------------------------------------------------


def _finding(name: str, count: int, of: int, note: str) -> CrossSignalFinding:
    return CrossSignalFinding(name, count, of, (count / of) if of else None, note)


def _very_scarce_sell_high_conflicts(report) -> CrossSignalFinding:
    total = hit = 0
    for ld in active_leagues(report):
        market = _get(ld, "replacement")
        if market is None:
            continue
        for i, p, _econ, _imp in _pairs(ld):
            if _get(p, "trade_type") != "sell_high":
                continue
            if not any(market.scarcity_of(_get(e, "position")) == rv.VERY_SCARCE for e in (_get(p, "give", []) or [])):
                continue
            total += 1
            if rc.conflict_for(_conflicts(ld), rc.TRADE, str(i)) is not None:
                hit += 1
    return _finding(
        "Very Scarce sell-high proposals that are also Conflicted", hit, total,
        "the sell-high generator and the conflict detector reading the same scarcity fact in opposite directions; a "
        "high share means the engine routinely proposes trades it then labels Conflicted",
    )


def _scarcity_stated_twice(report) -> CrossSignalFinding:
    total = hit = 0
    for ld in active_leagues(report):
        for i, p, econ, _imp in _pairs(ld):
            total += 1
            texts = list(_get(p, "caveats", []) or [])
            if econ is not None and _get(econ, "scarcity_note"):
                texts.append(str(econ.scarcity_note))
            conflict = rc.conflict_for(_conflicts(ld), rc.TRADE, str(i))
            if conflict is not None:
                texts.extend(_get(conflict, "reasons_against", []) or [])
            if sum(str(t).count("Scarce") for t in texts) >= 2:
                hit += 1
    return _finding(
        "Trade cards stating scarcity twice or more", hit, total,
        "one market fact printed as a caveat, an economics line and a conflict reason reads to the user as three "
        "independent reasons",
    )


def _exposure_stated_twice(report) -> CrossSignalFinding:
    def says_exposure(texts) -> bool:
        return any("exposure" in str(t).lower() for t in texts)

    total = hit = 0
    for ld in active_leagues(report):
        for i, p, _econ, _imp in _pairs(ld):
            in_caveat = says_exposure(_get(p, "caveats", []) or [])
            conflict = rc.conflict_for(_conflicts(ld), rc.TRADE, str(i))
            in_conflict = conflict is not None and says_exposure(conflict.reasons_against or [])
            if in_caveat or in_conflict:
                total += 1
                hit += int(in_caveat and in_conflict)
        for t in _targets(ld):
            in_note = says_exposure([_get(t, "reason", ""), *(_get(t, "notes", []) or [])])
            conflict = rc.conflict_for(_conflicts(ld), rc.WAIVER, _get(t, "player_id"))
            in_conflict = conflict is not None and says_exposure(conflict.reasons_against or [])
            if in_note or in_conflict:
                total += 1
                hit += int(in_note and in_conflict)
    return _finding(
        "Recommendations stating exposure in both a note and a conflict", hit, total,
        "the conflict detector reads the exposure caveat it then repeats; the second statement adds no information",
    )


def _source_split_on_ladder_and_caveat(report) -> CrossSignalFinding:
    total = hit = 0
    for ld in active_leagues(report):
        proposals = _proposals(ld)
        for idx, ladder in (_get(ld, "ladders", {}) or {}).items():
            proposal = proposals[idx] if isinstance(idx, int) and idx < len(proposals) else None
            for step in (_get(ladder, "opening"), _get(ladder, "fallback"), _get(ladder, "walk_away")):
                if step is None or not _get(step, "source_note"):
                    continue
                for e in (_get(step, "players", []) or []):
                    if e.name not in str(step.source_note):
                        continue
                    total += 1
                    if proposal is not None and any(f"Sources on {e.name}" in str(t) for t in _texts_of(proposal)):
                        hit += 1
    return _finding(
        "Ladder pieces whose source split is also stated on the trade card", hit, total,
        "the same disagreement appears on the proposal and again on the negotiation ladder step for the same player",
    )


def _developmental_drop_overlap(report) -> CrossSignalFinding:
    total = hit = 0
    for ld in active_leagues(report):
        drop_ids = {_get(_get(d, "entry"), "player_id") for d in (_get(ld, "drop_candidates", []) or [])}
        by_target = {_get(t, "player_id"): t for t in _targets(ld)}
        for c in _conflicts(ld):
            if _get(c, "kind") != rc.WAIVER:
                continue
            if not any("developmental hold" in str(r) for r in (_get(c, "reasons_against", []) or [])):
                continue
            total += 1
            target = by_target.get(_get(c, "key"))
            drop = _get(target, "drop_candidate")
            if drop is not None and _get(drop, "player_id") in drop_ids:
                hit += 1
    return _finding(
        "Developmental-drop conflicts whose drop is also a drop candidate", hit, total,
        "should be zero: detect_conflicts excludes the tool's own recommended drops, so any hit is a contradiction "
        "between the drop list and the conflict detector",
    )


def _buyer_scarcity_with_replacement_caveat(report) -> CrossSignalFinding:
    total = hit = 0
    for ld in active_leagues(report):
        caveat_texts = [str(t) for p in _proposals(ld) for t in (_get(p, "caveats", []) or []) if "Replacement context" in str(t)]
        for board in (_get(ld, "buyer_boards", []) or []):
            fits = _get(board, "all_fits", []) or []
            if not any("on waivers" in str(r) for f in fits for r in (_get(f, "reasons", []) or [])):
                continue
            total += 1
            name = _get(_get(board, "candidate"), "name", "")
            if name and any(name in t for t in caveat_texts):
                hit += 1
    return _finding(
        "Sell-high candidates scored up for scarcity who also carry a replacement caveat", hit, total,
        "the buyer board treats scarcity as a reason THEY pay; the replacement caveat treats it as a reason not to "
        "sell — both are on the same player",
    )


def _role_vs_velocity(report, role_labels: dict[str, str] | None) -> list[CrossSignalFinding]:
    """Optional: an external role-trend feed compared against market velocity.
    Skipped entirely when no labels are supplied — this module never invents
    the comparison input."""
    if not role_labels:
        return []
    agree = disagree = total = 0
    for ld in active_leagues(report):
        for pid, v in (_get(ld, "velocity", {}) or {}).items():
            role = _direction_of(role_labels.get(pid))
            market = _direction_of(_get(v, "label"))
            if role is None or market is None:
                continue
            total += 1
            if role == market:
                agree += 1
            else:
                disagree += 1
    return [
        _finding("Role trend and market velocity agree", agree, total,
                 "both signals point the same way; the second is confirmation, not new information"),
        _finding("Role trend and market velocity disagree", disagree, total,
                 "usage says one thing and the market another — the interesting case, and the one worth surfacing"),
    ]


def _direction_of(label: str | None) -> str | None:
    if not label:
        return None
    text = str(label).lower()
    if any(w in text for w in ("rising", "rise", "up", "increas", "growing")):
        return "up"
    if any(w in text for w in ("falling", "fall", "down", "decreas", "shrink")):
        return "down"
    return None


def cross_signals(report, *, role_labels: dict[str, str] | None = None) -> list[CrossSignalFinding]:
    return [
        _very_scarce_sell_high_conflicts(report),
        _scarcity_stated_twice(report),
        _exposure_stated_twice(report),
        _source_split_on_ladder_and_caveat(report),
        _developmental_drop_overlap(report),
        _buyer_scarcity_with_replacement_caveat(report),
        *_role_vs_velocity(report, role_labels),
    ]


# --------------------------------------------------------------------------
# Contradictions: two rules on ONE subject pulling opposite ways in the same
# run. Some are invariants report_data claims to enforce (should_be_zero);
# the rest are tradeoffs the reader should at least see counted.
# --------------------------------------------------------------------------


def _contradiction(name, hits: list[str], of: int, note: str, *, should_be_zero: bool = False) -> ContradictionFinding:
    return ContradictionFinding(
        name=name, count=len(hits), of=of, share=(len(hits) / of) if of else None, note=note,
        examples=hits[:MAX_EXAMPLES], should_be_zero=should_be_zero,
    )


def _drop_and_bench_surplus(report) -> ContradictionFinding:
    hits: list[str] = []
    of = 0
    for ld in active_leagues(report):
        surplus = {s.entry.player_id: s for s in (_get(_get(ld, "lineup_leverage"), "bench_surplus", []) or [])}
        for d in _get(ld, "drop_candidates", []) or []:
            of += 1
            pid = _get(_get(d, "entry"), "player_id")
            if pid in surplus:
                hits.append(f"{_league_name(ld)} — {d.entry.name} is a {d.priority} and bench surplus")
    return _contradiction(
        "Drop candidate who is also bench surplus", hits, of,
        "surplus is trade material by the report's own reading; report_data excludes surplus ids from the drop search",
        should_be_zero=True,
    )


def _drop_is_a_starter(report) -> ContradictionFinding:
    hits: list[str] = []
    of = 0
    for ld in active_leagues(report):
        starters = _starter_ids(ld)
        if not starters:
            continue
        for d in _get(ld, "drop_candidates", []) or []:
            of += 1
            if _get(_get(d, "entry"), "player_id") in starters:
                hits.append(f"{_league_name(ld)} — {d.entry.name} ({d.priority}) starts in the optimized lineup")
        for t in _targets(ld):
            drop = _get(t, "drop_candidate")
            if drop is None:
                continue
            of += 1
            if _get(drop, "player_id") in starters:
                hits.append(f"{_league_name(ld)} — Add {t.name}, drop {drop.name}: the drop is an optimized starter")
    return _contradiction(
        "Drop (list or paired waiver drop) who is an optimized starter", hits, of,
        "an optimized starter is never the paired drop (DECISIONS 2026-09-03); the drop list is built from non-starters",
        should_be_zero=True,
    )


def _clog_and_drop(report) -> ContradictionFinding:
    hits: list[str] = []
    of = 0
    for ld in active_leagues(report):
        drops = {_get(_get(d, "entry"), "player_id") for d in (_get(ld, "drop_candidates", []) or [])}
        for c in _get(ld, "roster_clogs", []) or []:
            of += 1
            if _get(_get(c, "entry"), "player_id") in drops:
                hits.append(f"{_league_name(ld)} — {c.entry.name} is a clog and a drop candidate")
    return _contradiction(
        "Roster clog who is also a drop candidate", hits, of,
        "report_data removes clogs that are already drop candidates so one player is not listed under two headings",
        should_be_zero=True,
    )


def _sell_high_from_very_scarce(report) -> ContradictionFinding:
    hits: list[str] = []
    of = 0
    for ld in active_leagues(report):
        market = _get(ld, "replacement")
        if market is None:
            continue
        starters = _starter_ids(ld)
        for i, p, _econ, _imp in _pairs(ld):
            if _get(p, "trade_type") != "sell_high":
                continue
            for e in _get(p, "give", []) or []:
                of += 1
                pid = _get(e, "player_id")
                plays = not starters or pid in starters
                if plays and market.scarcity_of(_get(e, "position")) == rv.VERY_SCARCE:
                    hits.append(f"{_league_name(ld)} — {p.summary_line()}: {e.name}'s {e.position} market is Very Scarce")
    return _contradiction(
        "Sell-high piece who starts at a Very Scarce position", hits, of,
        "the sell-high generator reads the market arrow, the replacement market says nothing on the wire replaces him; "
        "a bench-surplus sale out of a scarce market is deliberately not counted",
    )


def _paid_tier_not_an_upgrade(report, tier: str, *, should_be_zero: bool) -> ContradictionFinding:
    hits: list[str] = []
    of = 0
    for ld in active_leagues(report):
        for t in _targets(ld):
            if _get(t, "priority_tier") != tier:
                continue
            of += 1
            if "not an immediate upgrade" in str(_get(t, "reason", "")):
                hits.append(f"{_league_name(ld)} — {t.name} ({tier}): {t.reason}")
    if should_be_zero:
        note = "a Must Add has to beat the weakest starter he would replace (DECISIONS 2026-09-03), so this reason and this tier cannot share a row"
    else:
        note = ("allowed by design — a depth-only need is a Strong Add — but the reader sees 'Strong' and 'not an upgrade' "
                "on the same row; the count is how often the top paid tier is depth")
    return _contradiction(f"{tier} whose row says 'not an immediate upgrade'", hits, of, note, should_be_zero=should_be_zero)


def _insurance_for_abundant_position(report) -> ContradictionFinding:
    hits: list[str] = []
    of = 0
    for ld in active_leagues(report):
        market = _get(ld, "replacement")
        if market is None:
            continue
        for r in _get(ld, "insurance", []) or []:
            of += 1
            pos = _get(_get(r, "starter"), "position")
            if market.scarcity_of(pos) == rv.ABUNDANT:
                hits.append(f"{_league_name(ld)} — insure {r.starter.name} ({pos}) with {r.candidate.name}: {pos} market is Abundant")
        for t in _targets(ld):
            if _get(t, "priority_tier") != we.INSURANCE:
                continue
            of += 1
            if market.scarcity_of(_get(t, "position")) == rv.ABUNDANT:
                hits.append(f"{_league_name(ld)} — Insurance add {t.name}: {t.position} market is Abundant")
    return _contradiction(
        "Insurance row for an Abundant position", hits, of,
        "an Abundant market is its own insurance; report_data keeps only Scarce/Very Scarce positions",
        should_be_zero=True,
    )


def _favorable_assets_major_lineup_cost(report) -> ContradictionFinding:
    hits: list[str] = []
    of = 0
    for ld in active_leagues(report):
        for i, p, econ, _imp in _pairs(ld):
            if econ is None:
                continue
            of += 1
            if _get(econ, "asset_economics") == toc.FAVORABLE and _get(econ, "roster_economics") == toc.MAJOR_LINEUP_COST:
                hits.append(f"{_league_name(ld)} — {p.summary_line()}: assets Favorable, lineup {econ.weekly_delta:+.1f}/wk")
    return _contradiction(
        "Trade Favorable by assets and a Major Lineup Cost", hits, of,
        "the two economics are kept separate on purpose and this is the Strategic Tradeoff case; counted so the reader "
        "knows how often 'win the trade, lose the week' is proposed",
    )


def contradictions(report) -> list[ContradictionFinding]:
    return [
        _drop_and_bench_surplus(report),
        _drop_is_a_starter(report),
        _clog_and_drop(report),
        _sell_high_from_very_scarce(report),
        _paid_tier_not_an_upgrade(report, we.MUST_ADD, should_be_zero=True),
        _paid_tier_not_an_upgrade(report, we.STRONG_ADD, should_be_zero=False),
        _insurance_for_abundant_position(report),
        _favorable_assets_major_lineup_cost(report),
    ]


# --------------------------------------------------------------------------
# Drop protection: who on each roster is protected from being the drop by
# ANY rule, and who is left. Every protection is defensible on its own; the
# monitor asks whether they add up to "nobody".
# --------------------------------------------------------------------------

PROTECT_STARTER = "optimized starter"
PROTECT_SURPLUS = "bench surplus"
PROTECT_DEVELOPMENTAL = "developmental exemption"
PROTECT_WATCHLIST = "watchlist thesis active"
PROTECT_VERY_SCARCE = "Very Scarce position"
PROTECT_UNTOUCHABLE = "trade-engine untouchable"
PROTECT_TRADE_PIECE = "piece in a live proposal"
PROTECTION_REASONS: tuple[str, ...] = (
    PROTECT_STARTER, PROTECT_SURPLUS, PROTECT_DEVELOPMENTAL, PROTECT_WATCHLIST,
    PROTECT_VERY_SCARCE, PROTECT_UNTOUCHABLE, PROTECT_TRADE_PIECE,
)


def _active_watch_ids(report, league_id: str | None) -> set[str]:
    watchlist = _get(report, "watchlist")
    items = _get(watchlist, "items", {}) or {}
    values = items.values() if isinstance(items, dict) else items
    resolved = getattr(wl, "RESOLVED", "RESOLVED")
    return {
        str(_get(i, "player_id"))
        for i in values
        if _get(i, "trigger_state") != resolved and (league_id is None or str(_get(i, "league_id")) == str(league_id))
    }


def _untouchable_ids(ld) -> set[str]:
    fn = getattr(ra, "untouchable_ids", None)
    roster = _get(ld, "roster")
    if fn is None or roster is None:
        return set()
    try:
        return set(fn(roster, _get(ld, "currency"), te.UNTOUCHABLE_COUNT))
    except Exception:  # a synthetic roster without values must not break a diagnostic
        return set()


def drop_protection(report) -> list[DropProtection]:
    out: list[DropProtection] = []
    for ld in active_leagues(report):
        roster = _get(ld, "roster")
        entries = list(_get(roster, "entries", []) or [])
        if not entries:
            continue
        market = _get(ld, "replacement")
        currency = _get(ld, "currency")
        protected_by: dict[str, set[str]] = {
            PROTECT_STARTER: _starter_ids(ld),
            PROTECT_SURPLUS: {s.entry.player_id for s in (_get(_get(ld, "lineup_leverage"), "bench_surplus", []) or [])},
            PROTECT_DEVELOPMENTAL: {e.player_id for e in entries if _is_developmental(e, currency)},
            PROTECT_WATCHLIST: _active_watch_ids(report, _get(_get(ld, "league"), "league_id")),
            PROTECT_VERY_SCARCE: (
                {e.player_id for e in entries if market.scarcity_of(_get(e, "position")) == rv.VERY_SCARCE}
                if market is not None else set()
            ),
            PROTECT_UNTOUCHABLE: _untouchable_ids(ld),
            PROTECT_TRADE_PIECE: {_get(e, "player_id") for p in _proposals(ld) for e in (_get(p, "give", []) or [])},
        }
        ids = {e.player_id for e in entries}
        protected = set().union(*protected_by.values()) & ids
        droppable = [e for e in entries if e.player_id not in protected]
        out.append(DropProtection(
            league=_league_name(ld),
            rostered=len(entries),
            protected=len(protected),
            droppable=len(droppable),
            by_reason={reason: len(protected_by[reason] & ids) for reason in PROTECTION_REASONS},
            droppable_names=[e.name for e in droppable],
            flagged=len(droppable) < MIN_DROPPABLE,
        ))
    return out


def _is_developmental(entry, currency) -> bool:
    fn = getattr(rcl, "is_dynasty_developmental", None)
    if fn is None:
        return False
    try:
        return bool(fn(entry, currency))
    except Exception:
        return False


# --------------------------------------------------------------------------
# Dependency map: for each tracked fact, every rule that declared it as an
# input, so the reader can count how many votes one fact casts.
# --------------------------------------------------------------------------


def dependency_map(specs: list[RuleSpec]) -> list[DependencyEntry]:
    facts = list(TRACKED_FACTS)
    for spec in specs:
        for fact in spec.inputs:
            if fact not in facts:
                facts.append(fact)
    out: list[DependencyEntry] = []
    for fact in facts:
        rules = [f"{s.module}.{s.name}" for s in specs if fact in s.inputs]
        modules: list[str] = []
        for s in specs:
            if fact in s.inputs and s.module not in modules:
                modules.append(s.module)
        out.append(DependencyEntry(fact=fact, rules=rules, modules=modules, other_consumers=MODULE_CONSUMERS.get(fact, ())))
    return out


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def calibrate(report, *, role_labels: dict[str, str] | None = None, rules: list[RuleSpec] | None = None) -> CalibrationResult:
    specs = rules if rules is not None else build_rules()
    results = [evaluate(spec, report) for spec in specs]
    double_counts = find_double_counts(results)
    _apply_double_count_labels(results, double_counts)
    return CalibrationResult(
        generated_at=_get(report, "generated_at") or dt.datetime.now(dt.timezone.utc),
        current_week=_get(report, "current_week"),
        leagues=[_league_name(ld) for ld in active_leagues(report)],
        rules=results,
        cross_signals=cross_signals(report, role_labels=role_labels),
        double_counts=double_counts,
        contradictions=contradictions(report),
        drop_protection=drop_protection(report),
        dependency_map=dependency_map(specs),
    )


_INTERPRETATION = {
    INSUFFICIENT_SAMPLE: "too few eligible observations to say anything about this threshold",
    NEVER_FIRES: "never fired despite plenty of chances — the threshold may be unreachable on real data",
    RARE: "fires on under 5% of what it sees — right for an alarm, wrong for a label meant to be common",
    OVERACTIVE: "fires on 40-60% of what it sees — a coin flip carries little information",
    NEARLY_UNIVERSAL: "fires on most of what it sees, so the label barely distinguishes anything",
    LEAGUE_CONCENTRATED: "almost every trigger came from one league — likely a league artifact, not a general rule",
    FORMAT_BIASED: "almost every trigger came from one league format — the rule may be describing the format, not the player",
    POSITION_BIASED: "almost every trigger is one position — the threshold may be a position rule in disguise",
    POTENTIAL_DOUBLE_COUNT: "its trigger set is mostly another rule's trigger set — two labels, one vote",
}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def interpret(result: RuleResult) -> str:
    """One line saying what the diagnostic means for THIS rule, with the
    numbers that produced it."""
    base = _INTERPRETATION.get(result.diagnostic, "")
    if result.diagnostic == INSUFFICIENT_SAMPLE:
        detail = f"{result.eligible} eligible (< MIN_SAMPLE={MIN_SAMPLE})"
    elif result.diagnostic == NEVER_FIRES:
        detail = f"0 of {result.eligible}"
    elif result.diagnostic in (NEARLY_UNIVERSAL, OVERACTIVE, RARE):
        detail = f"{result.triggered} of {result.eligible} ({_pct(result.rate)})"
    elif result.diagnostic == LEAGUE_CONCENTRATED:
        detail = f"{result.triggered} triggers, leagues: {', '.join(result.leagues_triggered)}"
    elif result.diagnostic in (FORMAT_BIASED, POSITION_BIASED):
        detail = result.bias_detail or f"{result.triggered} of {result.eligible}"
    else:
        detail = f"{result.triggered} of {result.eligible}"
    line = f"{base} [{detail}]"
    extra = [d for d in result.diagnostics if d != result.diagnostic]
    if extra:
        line += f" — also: {', '.join(extra)}"
        if result.bias_detail and result.diagnostic not in (FORMAT_BIASED, POSITION_BIASED):
            line += f" ({result.bias_detail})"
    if result.note:
        line += f" — note: {result.note}"
    if result.time_gated:
        line += f" — expected early: {result.time_gated}"
    return line


def render_calibration_markdown(result: CalibrationResult) -> str:
    """A developer report. Deliberately dense and unfriendly — nobody reads
    this looking for a lineup call."""
    lines: list[str] = [
        "# Calibration report",
        "",
        "_Engineering diagnostic. Counts how often each rule was eligible to fire and how often it did._",
        "_Nothing here is auto-tuned; every number is an observation about the current constants._",
        "",
        f"- Generated: {result.generated_at.isoformat()}",
        f"- Week: {result.current_week if result.current_week is not None else '—'}",
        f"- Leagues analysed: {len(result.leagues)}" + (f" ({', '.join(result.leagues)})" if result.leagues else ""),
        f"- Rules evaluated: {len(result.rules)}",
        "",
    ]

    modules: list[str] = []
    for r in result.rules:
        if r.module not in modules:
            modules.append(r.module)

    lines += ["## Rules", ""]
    for module in modules:
        lines += [f"### {module}", "", "| Rule | Constants | Eligible | Triggered | Rate | Missing | Diagnostic | Leagues | Example |",
                  "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |"]
        for r in [x for x in result.rules if x.module == module]:
            leagues = ", ".join(r.leagues_triggered) if r.leagues_triggered else "—"
            example = r.examples[0].replace("|", "\\|") if r.examples else "—"
            lines.append(
                f"| {r.name} | {r.constants_text} | {r.eligible} | {r.triggered} | {_pct(r.rate)} | "
                f"{_pct(r.missing_data_rate)} | {r.diagnostics_text} | {leagues} | {example} |"
            )
        lines.append("")

    flagged = result.flagged()
    lines += ["## Flags", ""]
    if not flagged:
        lines += [f"Every rule read {HEALTHY}.", ""]
    else:
        lines.append(f"{len(flagged)} of {len(result.rules)} rules are not {HEALTHY}.")
        lines.append("")
        for label in FLAG_ORDER:
            group = [r for r in flagged if r.diagnostic == label]
            if not group:
                continue
            lines += [f"### {label} ({len(group)})", ""]
            for r in group:
                lines.append(f"- **{r.module}.{r.name}** — {interpret(r)}")
                for ex in r.examples[1:]:
                    lines.append(f"  - e.g. {ex}")
            lines.append("")

    lines += ["## Cross-signal findings", "",
              "_Where several rules state the same underlying fact on one card. Reported, never rewritten._", "",
              "| Finding | Count | Of | Share | Note |", "| --- | ---: | ---: | ---: | --- |"]
    for f in result.cross_signals:
        lines.append(f"| {f.name} | {f.count} | {f.of} | {_pct(f.share)} | {f.note} |")
    lines.append("")

    lines += ["### Potential double counts", "",
              f"_Rule pairs whose trigger sets overlap by >= OVERLAP_SHARE={OVERLAP_SHARE:g} of the smaller set "
              f"(each with >= OVERLAP_MIN_TRIGGERS={OVERLAP_MIN_TRIGGERS}; a rule firing on >= {OVERLAP_SHARE:.0%} of its "
              "eligible set is skipped as the larger side). Not proof — a Must Add is trending by construction — but the two "
              "labels are not independent votes._", ""]
    if not result.double_counts:
        lines += ["None.", ""]
    else:
        lines += ["| Rule A | Rule B | Overlap | Of smaller | Share | Examples |", "| --- | --- | ---: | ---: | ---: | --- |"]
        for d in sorted(result.double_counts, key=lambda d: (-d.share, -d.overlap, d.rule_a, d.rule_b)):
            examples = "; ".join(d.examples).replace("|", "/")
            lines.append(f"| {d.rule_a} | {d.rule_b} | {d.overlap} | {d.smaller} | {_pct(d.share)} | {examples} |")
        lines.append("")

    lines += ["## Contradictions", "",
              "_Two rules on one subject pulling opposite ways in this run. 'Invariant' rows are exclusions report_data "
              "claims to enforce, so any hit there is a bug; the rest are tradeoffs, counted so their frequency is known._", "",
              "| Contradiction | Count | Of | Share | Kind | Note |", "| --- | ---: | ---: | ---: | --- | --- |"]
    for c in result.contradictions:
        kind = "invariant" if c.should_be_zero else "tradeoff"
        lines.append(f"| {c.name} | {c.count} | {c.of} | {_pct(c.share)} | {kind} | {c.note} |")
    lines.append("")
    hits = [c for c in result.contradictions if c.count]
    if hits:
        for c in hits:
            lines.append(f"- **{c.name}** ({c.count} of {c.of}{'; should be zero' if c.should_be_zero else ''})")
            for ex in c.examples:
                lines.append(f"  - e.g. {ex.replace('|', '/')}")
        lines.append("")

    lines += ["## Drop protection", "",
              f"_Per league: rostered players some rule protects from being the drop, and who is left. A league with fewer "
              f"than MIN_DROPPABLE={MIN_DROPPABLE} droppable players is a roster with nobody droppable._", ""]
    if not result.drop_protection:
        lines += ["No rosters to assess.", ""]
    else:
        header = " | ".join(PROTECTION_REASONS)
        lines += [f"| League | Rostered | Protected | Droppable | {header} | Flag |",
                  "| --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in PROTECTION_REASONS) + " | --- |"]
        for d in result.drop_protection:
            counts = " | ".join(str(d.by_reason.get(reason, 0)) for reason in PROTECTION_REASONS)
            flag = "**roster with nobody droppable**" if d.flagged else "—"
            lines.append(f"| {d.league} | {d.rostered} | {d.protected} | {d.droppable} | {counts} | {flag} |")
        lines.append("")
        for d in result.drop_protection:
            names = ", ".join(d.droppable_names[:8]) + (" …" if len(d.droppable_names) > 8 else "")
            lines.append(f"- {d.league}: droppable = {names or 'nobody'}")
        lines.append("")

    lines += ["## Dependency map", "",
              "_How many votes one fact casts: every rule that declared the fact as an input (from RuleSpec.inputs), plus "
              "the annotators that read it without owning a rule here (hand-maintained list)._", ""]
    for e in result.dependency_map:
        lines.append(f"### {e.fact} — {e.votes} module(s), {len(e.rules)} rule(s)")
        lines.append("")
        if e.rules:
            for name in e.rules:
                lines.append(f"- {name}")
        else:
            lines.append("- no rule in this inventory reads it")
        if e.other_consumers:
            lines.append(f"- also consumed by: {', '.join(e.other_consumers)}")
        lines.append("")
    return "\n".join(lines)


# The order the Flags section lists the labels in: the loud ones first, the
# structural explanations last.
FLAG_ORDER: tuple[str, ...] = (
    NEARLY_UNIVERSAL, OVERACTIVE, NEVER_FIRES, RARE, LEAGUE_CONCENTRATED, FORMAT_BIASED, POSITION_BIASED,
    POTENTIAL_DOUBLE_COUNT, INSUFFICIENT_SAMPLE,
)
