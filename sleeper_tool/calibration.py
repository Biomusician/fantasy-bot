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
from sleeper_tool import roster_clog as rcl
from sleeper_tool import roster_consolidation as rcs
from sleeper_tool import schedule_window as sw
from sleeper_tool import source_disagreement as sd
from sleeper_tool import stash_board as sb
from sleeper_tool import streamer_planner as sp
from sleeper_tool import trade_engine as te
from sleeper_tool import trade_opportunity_cost as toc
from sleeper_tool import waiver_engine as we
from sleeper_tool.team_status import CONTENDER

# --------------------------------------------------------------------------
# Diagnostic thresholds. These are the calibration lab's OWN constants —
# judgements about when a trigger rate is itself pathological, not about
# fantasy football.
# --------------------------------------------------------------------------

MIN_SAMPLE = 10  # below this many eligible observations, say nothing
NEARLY_ALWAYS_FIRES_MIN_RATE = 0.60  # at or above this share, the label stops distinguishing anything
NEVER_FIRES_MIN_ELIGIBLE = 25  # this many chances with zero triggers is a dead rule, not bad luck
LEAGUE_CONCENTRATION_MIN_SHARE = 0.75  # one league holding this share of the triggers
LEAGUE_CONCENTRATION_MIN_TRIGGERS = 5  # ... with at least this many triggers overall
LEAGUE_CONCENTRATION_MIN_LEAGUES = 3  # ... spread over at least this many eligible leagues
MAX_EXAMPLES = 3

NORMAL = "Normal"
INSUFFICIENT_SAMPLE = "Insufficient Sample"
NEVER_FIRES = "Never Fires"
NEARLY_ALWAYS_FIRES = "Nearly Always Fires"
LEAGUE_CONCENTRATED = "Highly League-Concentrated"

CROSS_LEAGUE = "(all leagues)"

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


@dataclass(frozen=True)
class RuleSpec:
    module: str
    name: str
    constants: tuple[tuple[str, Any], ...]
    observe: Callable[[Any], list[Observation]]
    note: str | None = None  # a list cap or other structural fact the rate can't show
    min_report_leagues: int = 0  # below this many drafted leagues the rule can't be judged at all


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

    @property
    def constants_text(self) -> str:
        return ", ".join(f"{n}={_fmt_value(v)}" for n, v in self.constants) or "—"


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
class CalibrationResult:
    generated_at: dt.datetime
    current_week: int | None
    leagues: list[str] = field(default_factory=list)
    rules: list[RuleResult] = field(default_factory=list)
    cross_signals: list[CrossSignalFinding] = field(default_factory=list)

    def flagged(self) -> list[RuleResult]:
        return [r for r in self.rules if r.diagnostic != NORMAL]


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


def _items(select: Callable[[Any], Iterable[Any]]) -> Callable[[Any], Iterator[tuple[str, Any]]]:
    """Turn a per-league selector into a (league name, item) stream over the
    whole report."""
    def stream(report) -> Iterator[tuple[str, Any]]:
        for ld in active_leagues(report):
            name = _league_name(ld)
            for item in select(ld) or ():
                yield name, item
    return stream


def _leagues_where(predicate: Callable[[Any], bool]) -> Callable[[Any], Iterator[tuple[str, Any]]]:
    """A rule whose unit of observation is the league itself."""
    return _items(lambda ld: [ld] if predicate(ld) else [])


def _over(
    stream: Callable[[Any], Iterator[tuple[str, Any]]],
    trigger: Callable[[Any], bool],
    *,
    describe: Callable[[Any], str] | None = None,
    magnitude: Callable[[Any], float] | None = None,
    missing: Callable[[Any], bool] | None = None,
) -> Callable[[Any], list[Observation]]:
    def observe(report) -> list[Observation]:
        out: list[Observation] = []
        for league, item in stream(report):
            if missing is not None and missing(item):
                out.append(Observation(league, False, missing=True))
                continue
            fired = bool(trigger(item))
            example = None
            mag = 0.0
            if fired and describe is not None:
                try:
                    example = f"{league} — {describe(item)}"
                except Exception:  # a diagnostic must never break on one odd record
                    example = None
            if fired and magnitude is not None:
                try:
                    mag = float(magnitude(item))
                except Exception:
                    mag = 0.0
            out.append(Observation(league, fired, example=example, magnitude=mag))
        return out
    return observe


def _bucket_rules(
    module: str,
    prefix: str,
    stream: Callable[[Any], Iterator[tuple[str, Any]]],
    label_of: Callable[[Any], Any],
    labels: Iterable[str],
    constants: tuple[tuple[str, Any], ...],
    *,
    describe: Callable[[Any], str] | None = None,
    magnitude: Callable[[Any], float] | None = None,
    missing: Callable[[Any], bool] | None = None,
    note: str | None = None,
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
                describe=describe, magnitude=magnitude, missing=missing,
            ),
            note=note,
        )
        for label in labels
    ]


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
    up here with no edit."""
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
        ),
        note=f"triggered list is capped at MAX_HIGHLIGHTED={rv.MAX_HIGHLIGHTED}; eligible is uncapped",
    ))

    # --- source_disagreement -----------------------------------------------
    consensus_constants = (
        ("STRONG_CONSENSUS_MAX_GAP", sd.STRONG_CONSENSUS_MAX_GAP),
        ("SIGNIFICANT_RANK_GAP", sd.SIGNIFICANT_RANK_GAP),
        ("HIGH_RANK_GAP", sd.HIGH_RANK_GAP),
        ("RANK_GAP_SCALE_PER_PLACE", sd.RANK_GAP_SCALE_PER_PLACE),
    )
    rules += _bucket_rules(
        "source_disagreement", "Consensus",
        _items(lambda ld: (_get(ld, "source_views", {}) or {}).values()),
        lambda v: v.consensus,
        (sd.STRONG_CONSENSUS, sd.NORMAL_CONSENSUS, sd.SOURCE_DISAGREEMENT, sd.HIGH_DISAGREEMENT),
        consensus_constants,
        describe=lambda v: f"{v.name} gap {v.consensus_gap} ({v.consensus_pair[0]} vs {v.consensus_pair[1]})",
        magnitude=lambda v: float(v.consensus_gap or 0),
        missing=lambda v: v.consensus_gap is None,
    )
    rules += _bucket_rules(
        "source_disagreement", "Direction",
        _items(lambda ld: (_get(ld, "source_views", {}) or {}).values()),
        lambda v: v.direction,
        (sd.MARKET_ABOVE_PROJECTION, sd.PROJECTION_ABOVE_MARKET),
        (("DYNASTY_PAIR", sd.DYNASTY_PAIR), ("REDRAFT_PAIR", sd.REDRAFT_PAIR)),
        describe=lambda v: f"{v.name} market {v.market_rank} vs projection {v.projection_rank}",
    )
    rules.append(RuleSpec(
        module="source_disagreement", name="Direction: none",
        constants=(("DYNASTY_PAIR", sd.DYNASTY_PAIR), ("REDRAFT_PAIR", sd.REDRAFT_PAIR)),
        observe=_over(
            _items(lambda ld: (_get(ld, "source_views", {}) or {}).values()),
            lambda v: v.direction is None,
        ),
    ))
    rules.append(RuleSpec(
        module="source_disagreement", name="Expert note present",
        constants=(("SIGNIFICANT_RANK_GAP", sd.SIGNIFICANT_RANK_GAP),),
        observe=_over(
            _items(lambda ld: (_get(ld, "source_views", {}) or {}).values()),
            lambda v: v.expert_note is not None,
            describe=lambda v: f"{v.name}: {v.expert_note}",
        ),
    ))

    # --- trade_opportunity_cost --------------------------------------------
    roster_econ_constants = (
        ("IMPROVES_MIN", toc.IMPROVES_MIN), ("COSTS_MAX", toc.COSTS_MAX), ("MAJOR_COST_MAX", toc.MAJOR_COST_MAX),
    )
    rules += _bucket_rules(
        "trade_opportunity_cost", "Roster economics",
        _items(lambda ld: [e for e in _economics(ld) if e is not None]),
        lambda e: e.roster_economics,
        (toc.IMPROVES_LINEUP, toc.MOSTLY_NEUTRAL, toc.COSTS_LINEUP, toc.MAJOR_LINEUP_COST),
        roster_econ_constants,
        describe=lambda e: f"{e.weekly_delta:+.1f}/wk",
        magnitude=lambda e: abs(e.weekly_delta or 0.0),
        missing=lambda e: e.weekly_delta is None,
    )
    rules += _bucket_rules(
        "trade_opportunity_cost", "Asset economics",
        _items(lambda ld: [e for e in _economics(ld) if e is not None]),
        lambda e: e.asset_economics,
        (toc.FAVORABLE, toc.ROUGHLY_EVEN, toc.UNFAVORABLE),
        (("_ASSET_BY_BALANCE", tuple(sorted(toc._ASSET_BY_BALANCE))),),
    )
    rules.append(RuleSpec(
        module="trade_opportunity_cost", name="Strategic Tradeoff",
        constants=roster_econ_constants,
        observe=_over(
            _items(lambda ld: [e for e in _economics(ld) if e is not None]),
            lambda e: bool(e.strategic_tradeoff),
            describe=lambda e: f"assets {e.asset_economics.lower()}, lineup {str(e.roster_economics).lower()}",
            magnitude=lambda e: abs(e.weekly_delta or 0.0),
        ),
    ))
    rules.append(RuleSpec(
        module="trade_opportunity_cost", name="Scarcity note present",
        constants=(("VERY_SCARCE", rv.VERY_SCARCE),),
        observe=_over(
            _items(lambda ld: [e for e in _economics(ld) if e is not None]),
            lambda e: e.scarcity_note is not None,
            describe=lambda e: str(e.scarcity_note),
        ),
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
        _items(lambda ld: (_get(ld, "velocity", {}) or {}).values()),
        lambda v: v.label,
        (mv.INSUFFICIENT_HISTORY, mv.UNMEASURABLE, mv.STABLE, mv.RISING, mv.RAPIDLY_RISING, mv.FALLING, mv.RAPIDLY_FALLING),
        velocity_constants,
        describe=lambda v: f"{v.observations} obs, move {v.total_move:+.0%}" if v.total_move is not None else f"{v.observations} obs",
        magnitude=lambda v: abs(v.total_move or 0.0),
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
        note=f"board is capped at STASH_MAX={sb.STASH_MAX} per league",
    )

    # --- schedule_window ---------------------------------------------------
    rules.append(RuleSpec(
        module="schedule_window", name="Start/sit schedule tiebreak",
        constants=(("TIEBREAK_MAX_VALUE_GAP", sw.TIEBREAK_MAX_VALUE_GAP), ("NEXT_GAMES_WINDOW", sw.NEXT_GAMES_WINDOW)),
        observe=_over(
            _items(lambda ld: _get(_get(ld, "lineup_leverage"), "close_calls", []) or []),
            lambda d: _get(d, "schedule_note") is not None,
            describe=lambda d: f"{d.slot}: {d.schedule_note}",
        ),
    ))
    rules.append(RuleSpec(
        module="schedule_window", name="Waiver schedule note",
        constants=(("NEXT_GAMES_WINDOW", sw.NEXT_GAMES_WINDOW),),
        observe=_over(
            _items(_targets),
            lambda t: any(str(n).startswith("Schedule:") for n in (_get(t, "notes", []) or [])),
            describe=lambda t: f"{t.name}: " + next(str(n) for n in t.notes if str(n).startswith("Schedule:")),
        ),
    ))

    # --- buyer_board -------------------------------------------------------
    rules += _bucket_rules(
        "buyer_board", "Buyer fit",
        _items(lambda ld: [f for b in (_get(ld, "buyer_boards", []) or []) for f in (_get(b, "all_fits", []) or [])]),
        lambda f: f.label,
        (bb.STRONG_FIT, bb.POSSIBLE_FIT, bb.POOR_FIT),
        (
            ("STRONG_FIT_MIN", bb.STRONG_FIT_MIN),
            ("POSSIBLE_FIT_MIN", bb.POSSIBLE_FIT_MIN),
            ("UNFUNDED_PENALTY", bb.UNFUNDED_PENALTY),
        ),
        describe=lambda f: f"{f.username} score {f.score} ({'; '.join(f.reasons)})",
        magnitude=lambda f: float(f.score),
        note=f"only the top MAX_BUYERS={bb.MAX_BUYERS} are shown to the reader; eligible counts every scored counterparty",
    )

    # --- recommendation_conflicts ------------------------------------------
    rules.append(RuleSpec(
        module="recommendation_conflicts", name="Trade conflict",
        constants=(("CONFLICTED", rc.CONFLICTED),),
        observe=_over(
            _items(lambda ld: [(p, rc.conflict_for(_conflicts(ld), rc.TRADE, str(i))) for i, p, _, _ in _pairs(ld)]),
            lambda pair: pair[1] is not None,
            describe=lambda pair: f"{pair[0].summary_line()} — {'; '.join(pair[1].reasons_against)}",
            magnitude=lambda pair: float(len(pair[1].reasons_against)),
        ),
    ))
    rules.append(RuleSpec(
        module="recommendation_conflicts", name="Waiver conflict",
        constants=(("DEVELOPMENTAL_DROP_MIN_PERCENTILE", rc.DEVELOPMENTAL_DROP_MIN_PERCENTILE),),
        observe=_over(
            _items(lambda ld: [(t, rc.conflict_for(_conflicts(ld), rc.WAIVER, t.player_id)) for t in _targets(ld)]),
            lambda pair: pair[1] is not None,
            describe=lambda pair: f"Add {pair[0].name} — {'; '.join(pair[1].reasons_against)}",
            magnitude=lambda pair: float(len(pair[1].reasons_against)),
        ),
    ))
    for family, needle in CONFLICT_REASON_FAMILIES:
        rules.append(RuleSpec(
            module="recommendation_conflicts", name=f"Conflict reason: {family}",
            constants=(("substring", needle),),
            observe=_over(
                _items(_conflicts),
                lambda c, needle=needle: any(needle in r for r in (_get(c, "reasons_against", []) or [])),
                describe=lambda c: f"{c.subject} — {'; '.join(c.reasons_against)}",
            ),
            note="eligible = every conflict raised; a conflict can belong to more than one family",
        ))

    # --- move_impact -------------------------------------------------------
    rules.append(RuleSpec(
        module="move_impact", name="Trade preview present",
        constants=(("MIN_ACCEPTANCE_FOR_PREVIEW", mi.MIN_ACCEPTANCE_FOR_PREVIEW),),
        observe=_over(
            _items(lambda ld: [(p, imp) for _, p, _, imp in _pairs(ld)]),
            lambda pair: pair[1] is not None,
        ),
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
            _items(lambda ld: [(p, imp) for _, p, _, imp in _pairs(ld)]),
            lambda pair: bool(pair[1].material_deltas()),
            describe=lambda pair: f"{pair[0].summary_line()} — {'; '.join(pair[1].material_deltas())}",
            magnitude=lambda pair: abs(_get(pair[1], "weekly_points_delta", 0.0) or 0.0),
            missing=lambda pair: pair[1] is None,
        ),
        note="missing = proposals below the preview bar, so no impact was computed",
    ))
    rules.append(RuleSpec(
        module="move_impact", name="Waiver preview present",
        constants=(("PREVIEWED_WAIVER_TIERS", mi.PREVIEWED_WAIVER_TIERS),),
        observe=_over(
            _items(lambda ld: [(t, (_get(ld, "waiver_impacts", {}) or {}).get(t.player_id))
                               for t in _targets(ld) if _get(t, "priority_tier") in mi.PREVIEWED_WAIVER_TIERS]),
            lambda pair: pair[1] is not None,
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
    )
    rules.append(RuleSpec(
        module="lineup_leverage", name="Bench surplus",
        constants=(("BENCH_SURPLUS_RATIO", ll.BENCH_SURPLUS_RATIO), ("MAX_SURPLUS_LISTED", ll.MAX_SURPLUS_LISTED)),
        observe=_over(
            _items(_projected_bench_with_surplus),
            lambda pair: pair[0].player_id in pair[1],
            describe=lambda pair: f"{pair[0].name}",
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
        ),
        note=(
            f"triggered list is capped at MAX_CLOGS_PER_ROSTER={rcl.MAX_CLOGS_PER_ROSTER} and further filtered by "
            "drop-candidate overlap in report_data; eligible is every non-starter"
        ),
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
        ),
        note="eligible = contender leagues only, by design",
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
        observe=_over(_leagues_where(lambda ld: True), lambda ld: _get(ld, "playoff") is not None),
    ))
    rules += _bucket_rules(
        "playoff_leverage", "Playoff",
        _items(lambda ld: [p] if (p := _get(ld, "playoff")) is not None else []),
        lambda p: p.label,
        (pl.COMFORTABLE, pl.BUBBLE, pl.LONG_SHOT, pl.OUT),
        playoff_constants,
        describe=lambda p: f"seed {p.seed} of {p.playoff_teams}, {p.wins}-{p.losses}",
    )
    rules.append(RuleSpec(
        module="playoff_leverage", name="Deadline window",
        constants=(("DEADLINE_WINDOW_WEEKS", pl.DEADLINE_WINDOW_WEEKS),),
        observe=_over(
            _items(lambda ld: [p] if (p := _get(ld, "playoff")) is not None else []),
            lambda p: bool(p.deadline_window),
            describe=lambda p: f"deadline week {p.trade_deadline_week}",
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
                lambda pair, level=level: pe.exposure_level(pair[1]) == level,
                describe=lambda pair: f"{pair[0]} in {pair[1]} leagues",
                magnitude=lambda pair: float(pair[1]),
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
            ),
        ))

    # --- trade_engine ------------------------------------------------------
    rules += _bucket_rules(
        "trade_engine", "Acceptance",
        _items(_proposals),
        lambda p: _get(p, "acceptance_rating"),
        te.ACCEPTANCE_TIERS,
        (("ACCEPTANCE_TIERS", te.ACCEPTANCE_TIERS),),
        describe=lambda p: p.summary_line(),
    )
    rules += _bucket_rules(
        "trade_engine", "Confidence",
        _items(_proposals),
        lambda p: _get(p, "confidence"),
        ("High", "Medium", "Low"),
        (("VALUE_TOLERANCE", te.VALUE_TOLERANCE),),
    )
    rules += _bucket_rules(
        "trade_engine", "Balance",
        _items(_proposals),
        lambda p: _get(p, "balance_label"),
        ("Favors me", "Balanced", "Slight overpay", "Overpay"),
        (("VALUE_TOLERANCE", te.VALUE_TOLERANCE),),
        describe=lambda p: f"{p.summary_line()} (ratio {p.value_ratio:.2f})",
    )
    rules += _bucket_rules(
        "trade_engine", "Trade type",
        _items(_proposals),
        lambda p: _get(p, "trade_type"),
        ("buy_low", "sell_high", "pick_target", rcs.TRADE_TYPE),
        (("MAX_CANDIDATES_PER_OPPONENT", te.MAX_CANDIDATES_PER_OPPONENT), ("UNTOUCHABLE_COUNT", te.UNTOUCHABLE_COUNT)),
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
    )

    # --- waiver_engine -----------------------------------------------------
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
    )
    rules += _bucket_rules(
        "waiver_engine", "Alert severity",
        _items(lambda ld: _get(ld, "time_sensitive", []) or []),
        lambda n: _get(n, "severity"),
        ("high", "medium", "low"),
        (("EARLY_SEASON_WEEK_CUTOFF", we.EARLY_SEASON_WEEK_CUTOFF),),
        describe=lambda n: f"{n.player_name}: {n.note}",
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


CONFLICT_REASON_FAMILIES: tuple[tuple[str, str], ...] = (
    ("Major Lineup Cost", toc.MAJOR_LINEUP_COST),
    ("Very Scarce market", rv.VERY_SCARCE),
    ("Cross-league exposure", "exposure"),
    ("Strategic pick", "Strategic pick"),
    ("Drop is a starter", "optimized starter"),
    ("Developmental drop", "developmental hold"),
    ("Bye-hole fill", "bye hole"),
)


def _measurable_replacement_players(report) -> Iterator[tuple[str, tuple]]:
    """(context, player_id, understated ids, overstated ids) for every player
    the replacement market could actually measure. Eligible is the measurable
    set, NOT the capped highlight lists."""
    for ld in active_leagues(report):
        market = _get(ld, "replacement")
        if market is None:
            continue
        understated = {c.entry.player_id for c in (_get(market, "understated", []) or [])}
        overstated = {c.entry.player_id for c in (_get(market, "overstated", []) or [])}
        for ctx in (_get(market, "players", {}) or {}).values():
            if ctx.projection_over_waiver is None:
                continue
            yield _league_name(ld), (ctx, ctx.entry.player_id, understated, overstated)


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


def _exposure_counts(report) -> Iterator[tuple[str, tuple[str, int]]]:
    portfolio = _get(report, "portfolio")
    if portfolio is None:
        return
    names = {p.player_id: p.name for p in (_get(portfolio, "players", []) or [])}
    for pid, count in sorted((_get(portfolio, "counts_by_player_id", {}) or {}).items()):
        yield CROSS_LEAGUE, (names.get(pid, pid), count)


def _delta_items(report) -> Iterator[tuple[str, Any]]:
    delta = _get(report, "delta")
    for item in (_get(delta, "items", []) or []):
        yield _get(item, "league_name") or CROSS_LEAGUE, item


RULES: list[RuleSpec] = build_rules()


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------


def diagnose(eligible: int, triggered: int, by_league: Counter, leagues_eligible: int) -> str:
    """The order matters: a rule with too little data gets no verdict at all,
    and "never fires" outranks "concentrated" because a dead rule's zero
    triggers can't be concentrated anywhere."""
    if eligible < MIN_SAMPLE:
        return INSUFFICIENT_SAMPLE
    if triggered == 0:
        return NEVER_FIRES if eligible >= NEVER_FIRES_MIN_ELIGIBLE else NORMAL
    if triggered / eligible >= NEARLY_ALWAYS_FIRES_MIN_RATE:
        return NEARLY_ALWAYS_FIRES
    if (
        triggered >= LEAGUE_CONCENTRATION_MIN_TRIGGERS
        and leagues_eligible >= LEAGUE_CONCENTRATION_MIN_LEAGUES
        and by_league
        and max(by_league.values()) / triggered >= LEAGUE_CONCENTRATION_MIN_SHARE
    ):
        return LEAGUE_CONCENTRATED
    return NORMAL


def evaluate(spec: RuleSpec, report) -> RuleResult:
    observations = spec.observe(report)
    eligible_obs = [o for o in observations if not o.missing]
    missing = len(observations) - len(eligible_obs)
    triggered_obs = [o for o in eligible_obs if o.triggered]
    by_league = Counter(o.league for o in triggered_obs)
    leagues_eligible = len({o.league for o in eligible_obs})
    eligible = len(eligible_obs)

    diagnostic = diagnose(eligible, len(triggered_obs), by_league, leagues_eligible)
    if spec.min_report_leagues and len(active_leagues(report)) < spec.min_report_leagues:
        diagnostic = INSUFFICIENT_SAMPLE

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
        diagnostic=diagnostic,
        leagues_triggered=sorted(by_league),
        leagues_eligible=leagues_eligible,
        examples=[o.example for o in ranked[:MAX_EXAMPLES] if o.example],
        missing=missing,
        missing_data_rate=(missing / (missing + eligible)) if (missing + eligible) else None,
        note=spec.note,
        time_gated=TIME_GATED.get(spec.name),
    )


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
# Entry points
# --------------------------------------------------------------------------


def calibrate(report, *, role_labels: dict[str, str] | None = None, rules: list[RuleSpec] | None = None) -> CalibrationResult:
    specs = rules if rules is not None else build_rules()
    return CalibrationResult(
        generated_at=_get(report, "generated_at") or dt.datetime.now(dt.timezone.utc),
        current_week=_get(report, "current_week"),
        leagues=[_league_name(ld) for ld in active_leagues(report)],
        rules=[evaluate(spec, report) for spec in specs],
        cross_signals=cross_signals(report, role_labels=role_labels),
    )


_INTERPRETATION = {
    INSUFFICIENT_SAMPLE: "too few eligible observations to say anything about this threshold",
    NEVER_FIRES: "never fired despite plenty of chances — the threshold may be unreachable on real data",
    NEARLY_ALWAYS_FIRES: "fires on most of what it sees, so the label barely distinguishes anything",
    LEAGUE_CONCENTRATED: "almost every trigger came from one league — likely a league artifact, not a general rule",
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
    elif result.diagnostic == NEARLY_ALWAYS_FIRES:
        detail = f"{result.triggered} of {result.eligible} ({_pct(result.rate)})"
    elif result.diagnostic == LEAGUE_CONCENTRATED:
        detail = f"{result.triggered} triggers, leagues: {', '.join(result.leagues_triggered)}"
    else:
        detail = f"{result.triggered} of {result.eligible}"
    line = f"{base} [{detail}]"
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
                f"{_pct(r.missing_data_rate)} | {r.diagnostic} | {leagues} | {example} |"
            )
        lines.append("")

    flagged = result.flagged()
    lines += ["## Flags", ""]
    if not flagged:
        lines += ["Every rule read Normal.", ""]
    else:
        lines.append(f"{len(flagged)} of {len(result.rules)} rules are not Normal.")
        lines.append("")
        for label in (NEARLY_ALWAYS_FIRES, NEVER_FIRES, LEAGUE_CONCENTRATED, INSUFFICIENT_SAMPLE):
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
    return "\n".join(lines)
