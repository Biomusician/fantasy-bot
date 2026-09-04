"""Recommendation Provenance — why this move, in the tool's own words.

Every annotation sentence in the report is already derived from a
structured object the report builder computed (a MoveImpact, a
TradeEconomics, a SourceView, a Velocity, a Conflict, a replacement
context, ...). What was missing is the *ledger*: a single card per
recommendation that says which evidence argues FOR it, which argues
AGAINST it, and which is neither but worth knowing, each labelled with
the kind of evidence it is and the module it came from.

Nothing here computes a new verdict. Reasons are harvested from text the
decision layer already wrote (`TradeProposal.rationale_for_me` /
`.caveats`, `WaiverTarget.reason` / `.notes`, `DropCandidate.reasons`,
`StashCandidate.reasons`, `Conflict.reasons_against`, ...) and from the
`describe()` / `clause()` helpers of the objects those annotations came
from. A missing module output simply yields no reason — never a
manufactured one.

Categories (what kind of evidence it is):
  Market              value/price signals: asset economics, market velocity,
                      pool-wide value percentile
  Projection          the projection sources and their disagreement
  Role                snap/target/usage trend and role-vs-market (optional
                      `ld.role_trends` / `ld.role_market`)
  Roster              my lineup and roster shape: Move Impact, bench
                      surplus, the drop, pick opportunity
  Replacement Market  what this league's wire can actually replace
  Schedule            byes and games inside the relevant window
  Opponent            this week's matchup, blocking, the buyer board
  League Economy      this league's own transaction record, FAAB posture
  Portfolio           cross-league exposure
  Timing              the calendar: deadline window, playoff leverage,
                      trending adds, the week a move is about
  Risk                the tool disagreeing with itself: a Conflict, a
                      Major Lineup Cost, a Strategic Tradeoff

Selection is categorical, never numeric: each direction has its own
priority order over the categories (CATEGORY_PRIORITY) and the top
MAX_FOR / MAX_AGAINST / MAX_CONTEXT reasons are kept. Within a category,
a reason's own `priority` (lower first, used to keep a source module's
own ordering), then its source module name, then its text. There is no
confidence number anywhere: a reason is evidence of a kind, not a
weight.

The ordering, and why:
  FOR      Roster (a measured lineup gain is the least arguable reason of
           all) > Role > Replacement Market > Market > Projection >
           League Economy > Schedule > Opponent > Timing > Portfolio > Risk
  AGAINST  Risk (the tool contradicting itself outranks everything) >
           Portfolio > Replacement Market > Market > Schedule > Roster >
           Role > Projection > Opponent > League Economy > Timing
  CONTEXT  Timing > Opponent > League Economy > Replacement Market >
           Schedule > Roster > Role > Market > Projection > Portfolio > Risk
The tail of each list (past the categories the ordering is really about)
exists so every category is ranked and selection stays deterministic.

Waiver cards additionally carry two explanation rows on `Provenance.extras`
(`why_drop`, `invalidation`) — the drop half of the transaction explained in
the engine's own words, and the facts already on this card that would make
the read wrong. Both are Context and both compete for a Context slot like
everything else; `extras` is what lets a renderer show them without
MAX_CONTEXT having to grow to accommodate them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.buyer_board import STRONG_FIT
from sleeper_tool.move_impact import MATERIAL_WEEKLY_POINTS
from sleeper_tool.pick_opportunity import SPENDABLE, STRATEGIC
from sleeper_tool.portfolio_exposure import acquisition_exposure_note
from sleeper_tool.recommendation_conflicts import TRADE, WAIVER, conflict_for
from sleeper_tool.replacement_value import ABUNDANT
from sleeper_tool.role_trends import INSUFFICIENT as ROLE_INSUFFICIENT
from sleeper_tool.schedule_window import team_window
from sleeper_tool.source_disagreement import HIGH_DISAGREEMENT, PROJECTION_ABOVE_MARKET, SOURCE_DISAGREEMENT
from sleeper_tool.stash_board import PRIORITY_STASH
from sleeper_tool.streamer_planner import ADD, HOLD
from sleeper_tool.trade_opportunity_cost import FAVORABLE, MAJOR_LINEUP_COST, STRATEGIC_TRADEOFF, UNFAVORABLE
from sleeper_tool.waiver_engine import EARLY_SEASON_CLAUSE

# -- evidence categories -----------------------------------------------------
MARKET = "Market"
PROJECTION = "Projection"
ROLE = "Role"
ROSTER = "Roster"
REPLACEMENT_MARKET = "Replacement Market"
SCHEDULE = "Schedule"
OPPONENT = "Opponent"
LEAGUE_ECONOMY = "League Economy"
PORTFOLIO = "Portfolio"
TIMING = "Timing"
RISK = "Risk"

CATEGORIES = (
    MARKET, PROJECTION, ROLE, ROSTER, REPLACEMENT_MARKET, SCHEDULE,
    OPPONENT, LEAGUE_ECONOMY, PORTFOLIO, TIMING, RISK,
)

# -- directions --------------------------------------------------------------
FOR = "FOR"
AGAINST = "AGAINST"
CONTEXT = "CONTEXT"

# -- recommendation kinds (the identity convention shared with conflicts) ----
DROP = "drop"
DEFENSIVE_ADD = "defensive_add"
STREAMER = "streamer"
STASH = "stash"
ALERT = "alert"

MAX_FOR = 3
MAX_AGAINST = 2
MAX_CONTEXT = 2

# Two per-card explanation rows (see _waiver_card) that answer questions the
# For/Against/Context ledger never did: what the paired drop actually is,
# and what would make this read wrong. They are Context like anything else
# and take their chances against MAX_CONTEXT — raising the cap for them
# would make every card's "at most two" claim false — but they are also
# always kept on `Provenance.extras`, so a renderer can put them below the
# card without the caps having to lie.
EXTRA_CONTEXT_PRIORITY = 50  # after every module-written context reason in the same category
WHY_DROP_PREFIX = "Why this drop:"
INVALIDATION_PREFIX = "What could invalidate this:"
MAX_DROP_REASONS = 2  # the drop's own reasons, capped so the row stays one sentence
MAX_INVALIDATION_FACTS = 3

CATEGORY_PRIORITY: dict[str, tuple[str, ...]] = {
    FOR: (ROSTER, ROLE, REPLACEMENT_MARKET, MARKET, PROJECTION, LEAGUE_ECONOMY, SCHEDULE, OPPONENT, TIMING, PORTFOLIO, RISK),
    AGAINST: (RISK, PORTFOLIO, REPLACEMENT_MARKET, MARKET, SCHEDULE, ROSTER, ROLE, PROJECTION, OPPONENT, LEAGUE_ECONOMY, TIMING),
    CONTEXT: (TIMING, OPPONENT, LEAGUE_ECONOMY, REPLACEMENT_MARKET, SCHEDULE, ROSTER, ROLE, MARKET, PROJECTION, PORTFOLIO, RISK),
}
_CATEGORY_RANK = {d: {c: i for i, c in enumerate(cats)} for d, cats in CATEGORY_PRIORITY.items()}
_MAX_BY_DIRECTION = {FOR: MAX_FOR, AGAINST: MAX_AGAINST, CONTEXT: MAX_CONTEXT}

# Role-trend labels are matched by substring so this module doesn't have
# to track the exact wording the role module settles on.
_ROLE_UP = ("Rising", "Surging")
_ROLE_DOWN = ("Falling", "Collapsing")
ROLE_AHEAD_OF_MARKET = "Role Ahead of Market"
MARKET_AHEAD_OF_ROLE = "Market Ahead of Role"


@dataclass
class Reason:
    category: str
    direction: str
    text: str
    source: str  # the module the evidence came from
    freshness: str | None = None  # optional label for the underlying data source
    priority: int = 0  # lower first, WITHIN a category — preserves a module's own ordering

    def describe(self) -> str:
        stale = f" [{self.freshness}]" if self.freshness else ""
        return f"{self.direction} — {self.category}: {self.text}{stale}"


@dataclass
class Provenance:
    kind: str
    key: str
    subject: str
    reasons_for: list[Reason] = field(default_factory=list)
    reasons_against: list[Reason] = field(default_factory=list)
    context: list[Reason] = field(default_factory=list)
    # Explanation rows that survive the MAX_* caps because they are not
    # competing for the same slots: a renderer shows them below the card.
    # A reason here may ALSO have made the Context list on its own merit.
    extras: list[Reason] = field(default_factory=list)

    def describe(self) -> list[str]:
        return [r.describe() for r in (*self.reasons_for, *self.reasons_against, *self.context)]

    @property
    def all_reasons(self) -> list[Reason]:
        return [*self.reasons_for, *self.reasons_against, *self.context]

    @property
    def why_drop(self) -> Reason | None:
        return next((r for r in self.extras if r.text.startswith(WHY_DROP_PREFIX)), None)

    @property
    def invalidation(self) -> Reason | None:
        return next((r for r in self.extras if r.text.startswith(INVALIDATION_PREFIX)), None)


def sort_key(reason: Reason) -> tuple:
    """Deterministic, entirely categorical: category rank inside the
    reason's own direction, then the module's own ordering, then the
    source name, then the text."""
    rank = _CATEGORY_RANK[reason.direction].get(reason.category, len(CATEGORIES))
    return (rank, reason.priority, reason.source, reason.text)


def select(reasons: list[Reason], direction: str) -> list[Reason]:
    return sorted(reasons, key=sort_key)[: _MAX_BY_DIRECTION[direction]]


def conflict_reasons(prov: Provenance) -> tuple[list[str], list[str]]:
    """The same evidence a Conflict carries, as plain text — so conflict
    detection could be re-expressed on top of provenance later. Pure."""
    return ([r.text for r in prov.reasons_for], [r.text for r in prov.reasons_against])


# -- annotation harvesting ---------------------------------------------------
# The decision layer already wrote every sentence; these prefixes say which
# module wrote it, so a harvested reason keeps its numbers and its wording.
_PREFIX_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("Replacement context:", REPLACEMENT_MARKET, "replacement_value"),
    ("Sources on", PROJECTION, "source_disagreement"),
    ("Sources:", PROJECTION, "source_disagreement"),
    ("Market velocity:", MARKET, "market_velocity"),
    ("market velocity", MARKET, "market_velocity"),
    ("Role:", ROLE, "role_trends"),
    ("Schedule:", SCHEDULE, "schedule_window"),
    ("Portfolio exposure:", PORTFOLIO, "portfolio_exposure"),
    ("portfolio exposure:", PORTFOLIO, "portfolio_exposure"),
    ("Buyer board:", OPPONENT, "buyer_board"),
    ("Converts bench surplus:", ROSTER, "lineup_leverage"),
    ("This league's transaction record:", LEAGUE_ECONOMY, "league_economy"),
)
_CONTAINS_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("for your roster", ROSTER, "pick_opportunity"),
    ("market is", REPLACEMENT_MARKET, "replacement_value"),
    ("bye hole", TIMING, "bye_collision"),
)


# Sentences the decision layer writes as a rationale or a caveat that are
# really a note on the method, or an admission that the piece is not an
# upgrade: they belong in Context, never on a For/Against side, or the
# selection would count "treat this as approximate" as evidence against
# a trade and "not an immediate upgrade" as evidence for one.
_CONTEXT_ONLY_MARKERS = (
    "not an immediate upgrade",
    "treat pick values as approximate",
    "as approximate",
    "treat these offers as more approximate",
    "well outside the startup-relevant player pool",
    "KTC only models",
    "not a dynasty trade-value market",
)


def annotation_direction(text: str, default: str) -> str:
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in _CONTEXT_ONLY_MARKERS):
        return CONTEXT
    return default


def classify_annotation(text: str, *, default: tuple[str, str]) -> tuple[str, str]:
    """(category, source) for one already-written annotation sentence."""
    stripped = text.strip()
    for prefix, category, source in _PREFIX_CATEGORIES:
        if stripped.startswith(prefix):
            return category, source
    for needle, category, source in _CONTAINS_CATEGORIES:
        if needle in stripped:
            return category, source
    return default


def _fact_for(category: str, text: str) -> tuple:
    """The dedupe key for a harvested sentence: one scarcity statement, one
    exposure statement and one source-disagreement statement per card."""
    if category == REPLACEMENT_MARKET and "market is" in text:
        return ("scarcity",)
    if category == PORTFOLIO:
        return ("exposure",)
    if category == PROJECTION:
        return ("sources", text.split(":")[0])
    return ("text", text)


class _Card:
    """Collects reasons for one recommendation, deduping repeated facts."""

    def __init__(self, kind: str, key: str, subject: str, freshness_by_source: dict[str, str] | None):
        self.prov = Provenance(kind=kind, key=key, subject=subject)
        self._seen: set[tuple] = set()
        self._categories: set[tuple[str, str]] = set()
        self._freshness = freshness_by_source or {}

    def add(
        self, category: str, direction: str, text: str | None, source: str,
        *, fact: tuple | None = None, priority: int = 0, freshness_key: str | None = None,
    ) -> None:
        if not text:
            return
        text = text.strip()
        fact = fact if fact is not None else ("text", text)
        if fact in self._seen:
            return
        self._seen.add(fact)
        self._categories.add((direction, category))
        bucket = {FOR: self.prov.reasons_for, AGAINST: self.prov.reasons_against, CONTEXT: self.prov.context}[direction]
        bucket.append(
            Reason(
                category=category, direction=direction, text=text, source=source,
                freshness=self._freshness.get(freshness_key or source), priority=priority,
            )
        )

    def add_extra(self, category: str, text: str | None, source: str, *, fact: tuple) -> None:
        """A per-card explanation row. Always kept on `extras`; also offered
        to Context, where it competes for a slot like any other reason
        rather than being handed one by raising the cap."""
        if not text:
            return
        self.prov.extras.append(
            Reason(
                category=category, direction=CONTEXT, text=text.strip(), source=source,
                freshness=self._freshness.get(source), priority=EXTRA_CONTEXT_PRIORITY,
            )
        )
        self.add(category, CONTEXT, text, source, fact=fact, priority=EXTRA_CONTEXT_PRIORITY)

    def add_annotation(self, text: str, direction: str, default: tuple[str, str], *, priority: int = 0) -> None:
        category, source = classify_annotation(text, default=default)
        self.add(category, direction, text, source, fact=_fact_for(category, text), priority=priority)

    def has(self, direction: str, category: str) -> bool:
        return (direction, category) in self._categories

    def finish(self) -> Provenance:
        p = self.prov
        p.reasons_for = select(p.reasons_for, FOR)
        p.reasons_against = select(p.reasons_against, AGAINST)
        p.context = select(p.context, CONTEXT)
        return p


def _at(seq, i):
    seq = seq or []
    return seq[i] if i < len(seq) else None


def _role_direction(label: str | None) -> int:
    if not label:
        return 0
    if any(w in label for w in _ROLE_UP):
        return 1
    if any(w in label for w in _ROLE_DOWN):
        return -1
    return 0


def _role_text(trend) -> str:
    bits = [trend.label]
    components = getattr(trend, "components", None) or []
    named = "; ".join(f"{c.name} {c.direction} {c.magnitude_text}" for c in components[:2])
    if named:
        bits.append(named)
    elif getattr(trend, "note", None):
        bits.append(trend.note)
    return " — ".join(bits)


def _piece_role_reasons(card: _Card, ld, entry, *, incoming: bool) -> None:
    """Role trend / role-vs-market on one trade piece or add. Incoming
    pieces want a rising role; outgoing pieces are better sold falling."""
    trends = getattr(ld, "role_trends", None) or {}
    markets = getattr(ld, "role_market", None) or {}
    trend = trends.get(entry.player_id)
    if trend is not None:
        d = _role_direction(getattr(trend, "label", None))
        if d:
            wants_up = incoming
            direction = FOR if (d > 0) == wants_up else AGAINST
            card.add(ROLE, direction, f"{entry.name}: {_role_text(trend)}", "role_trends", fact=("role", entry.player_id))
    label = markets.get(entry.player_id)
    if label in (ROLE_AHEAD_OF_MARKET, MARKET_AHEAD_OF_ROLE):
        ahead = label == ROLE_AHEAD_OF_MARKET
        direction = FOR if ahead == incoming else AGAINST
        card.add(ROLE, direction, f"{entry.name}: {label}", "role_trends", fact=("role_market", entry.player_id), priority=1)


def _piece_schedule_reason(card: _Card, ld, entry, schedule) -> None:
    windows = getattr(ld, "windows", None)
    if schedule is None or windows is None or card.has(AGAINST, SCHEDULE):
        return
    tw = team_window(schedule, entry.team, windows)
    note = tw.note() if tw is not None else None
    if note:
        card.add(SCHEDULE, AGAINST, f"{entry.name} has a {note}.", "schedule_window", fact=("schedule", entry.team))


def _exposure_reason(card: _Card, report, entry) -> None:
    portfolio = getattr(report, "portfolio", None) if report is not None else None
    if portfolio is None or card.has(AGAINST, PORTFOLIO):
        return
    note = acquisition_exposure_note(portfolio, entry.player_id, position=entry.position)
    card.add(PORTFOLIO, AGAINST, note, "portfolio_exposure", fact=("exposure",))


def _timing_reasons(card: _Card, ld) -> None:
    playoff = getattr(ld, "playoff", None)
    if playoff is None:
        return
    if playoff.deadline_window and playoff.trade_deadline_week:
        card.add(
            TIMING, CONTEXT,
            f"Deadline window: {playoff.label}, trade deadline is week {playoff.trade_deadline_week}",
            "playoff_leverage", fact=("deadline",),
        )
    else:
        card.add(TIMING, CONTEXT, f"{playoff.label}: {playoff.reason}", "playoff_leverage", fact=("playoff",), priority=1)


def _trade_card(ld, report, index: int, proposal, *, schedule, freshness_by_source) -> Provenance:
    card = _Card(TRADE, str(index), proposal.summary_line(), freshness_by_source)
    econ = _at(getattr(ld, "trade_economics", None), index)
    impact = _at(getattr(ld, "trade_impacts", None), index)
    conflict = conflict_for(getattr(ld, "conflicts", None) or [], TRADE, str(index))

    # Risk first: the tool contradicting itself is the reason that must survive the cap.
    if conflict is not None:
        for i, text in enumerate(conflict.reasons_against):
            # Keyed on the FACT the reason states (a scarcity, an exposure),
            # so the same fact arriving again as an economics note or a
            # harvested caveat is one reason, not two dressings of one.
            category, _ = classify_annotation(text, default=(RISK, "recommendation_conflicts"))
            card.add(RISK, AGAINST, text, "recommendation_conflicts", fact=_fact_for(category, text), priority=i)
    elif econ is not None and econ.roster_economics == MAJOR_LINEUP_COST:
        delta = f" ({econ.weekly_delta:+.1f}/wk)" if econ.weekly_delta is not None else ""
        card.add(RISK, AGAINST, f"{MAJOR_LINEUP_COST}{delta}", "trade_opportunity_cost", fact=("major_cost",))
    if econ is not None and econ.strategic_tradeoff:
        delta = f" ({econ.weekly_delta:+.1f}/wk)" if econ.weekly_delta is not None else ""
        card.add(
            RISK, AGAINST,
            f"{STRATEGIC_TRADEOFF}: assets {econ.asset_economics.lower()}, lineup {econ.roster_economics.lower()}{delta}",
            "trade_opportunity_cost", fact=("tradeoff",), priority=5,
        )

    if impact is not None:
        deltas = impact.material_deltas()
        if deltas:
            text = f"Move Impact: {'; '.join(deltas)}"
            d = impact.weekly_points_delta
            direction = FOR if d >= MATERIAL_WEEKLY_POINTS else (AGAINST if d <= -MATERIAL_WEEKLY_POINTS else CONTEXT)
            card.add(ROSTER, direction, text, "move_impact", fact=("impact",))

    if econ is not None:
        if econ.asset_economics == FAVORABLE:
            card.add(
                MARKET, FOR,
                f"Assets {FAVORABLE} ({proposal.balance_label}): {proposal.my_value_total:,.0f} out vs "
                f"{proposal.their_value_total:,.0f} in on {ld.currency} value",
                "trade_opportunity_cost", fact=("assets",),
            )
        elif econ.asset_economics == UNFAVORABLE:
            card.add(
                MARKET, AGAINST,
                f"Assets {UNFAVORABLE} ({proposal.balance_label}): {proposal.my_value_total:,.0f} out vs "
                f"{proposal.their_value_total:,.0f} in on {ld.currency} value",
                "trade_opportunity_cost", fact=("assets",),
            )
        if econ.scarcity_note:
            card.add(REPLACEMENT_MARKET, AGAINST, econ.scarcity_note, "trade_opportunity_cost", fact=("scarcity",))

    for i, text in enumerate(proposal.rationale_for_me):
        card.add_annotation(text, annotation_direction(text, FOR), default=(MARKET, "trade_engine"), priority=i + 1)
    for i, text in enumerate(proposal.caveats):
        card.add_annotation(text, annotation_direction(text, AGAINST), default=(MARKET, "trade_engine"), priority=i + 1)
    for i, text in enumerate(proposal.rationale_for_them):
        card.add_annotation(text, CONTEXT, default=(OPPONENT, "trade_engine"), priority=i + 1)

    for entry in proposal.receive:
        _piece_role_reasons(card, ld, entry, incoming=True)
        _piece_schedule_reason(card, ld, entry, schedule)
        _exposure_reason(card, report, entry)
    for entry in proposal.give:
        _piece_role_reasons(card, ld, entry, incoming=False)

    # Structured fallbacks for evidence the annotation pass didn't write.
    opportunity = getattr(ld, "pick_opportunity", None)
    if opportunity is not None:
        for pick in getattr(proposal, "give_picks", None) or []:
            classification = opportunity.classification_for(pick)
            if classification == STRATEGIC:
                card.add(ROSTER, AGAINST, f"{pick.name} is Strategic for your roster", "pick_opportunity", fact=("pick", pick.name))
            elif classification == SPENDABLE:
                card.add(ROSTER, FOR, f"{pick.name} is Spendable for your roster", "pick_opportunity", fact=("pick", pick.name), priority=9)
    if not card.has(CONTEXT, OPPONENT):
        for board in getattr(ld, "buyer_boards", None) or []:
            fit = board.fit_for(proposal.target_username)
            if fit is not None and fit.label == STRONG_FIT:
                card.add(OPPONENT, CONTEXT, f"Buyer board: {fit.describe()}", "buyer_board", fact=("buyer",))
                break
    if not card.has(CONTEXT, LEAGUE_ECONOMY):
        economy = getattr(ld, "league_economy", None)
        manager = _manager_for(economy, proposal.target_username)
        if manager is not None and manager.labels:
            card.add(LEAGUE_ECONOMY, CONTEXT, f"This league's transaction record: {manager.describe()}.", "league_economy", fact=("economy",))
    _timing_reasons(card, ld)
    return card.finish()


def _manager_for(economy, username: str | None):
    if economy is None or not username:
        return None
    return next((m for m in economy.managers.values() if m.username == username), None)


# What the paired drop is, when no module wrote a reason for him: the
# waiver engine's own two fallback rules, said out loud.
WEAKEST_AT_POSITION = "the weakest bench player at his position"
WEAKEST_BENCH_PIECE = "the roster's weakest bench piece"


def _engine_drop_reasons(ld, player_id: str) -> tuple[str, str] | None:
    """(reason text, source module) for a drop the decision layer already
    judged — a roster clog or a standing drop candidate. Their sentences,
    not a re-derivation."""
    for clog in getattr(ld, "roster_clogs", None) or []:
        if clog.entry.player_id == player_id and clog.reasons:
            return "; ".join(clog.reasons[:MAX_DROP_REASONS]), "roster_clog"
    for candidate in getattr(ld, "drop_candidates", None) or []:
        if candidate.entry.player_id == player_id and candidate.reasons:
            return "; ".join(candidate.reasons[:MAX_DROP_REASONS]), "trade_engine"
    return None


def _why_drop(ld, target) -> tuple[str, str] | None:
    """The drop half of a waiver row is half the transaction and had no
    explanation anywhere in the card: it named the cost ("Costs X the
    roster spot") without ever saying why X."""
    drop = target.drop_candidate
    if drop is None:
        return None
    judged = _engine_drop_reasons(ld, drop.player_id)
    if judged is not None:
        why, source = judged
    else:
        why = WEAKEST_AT_POSITION if drop.position == target.position else WEAKEST_BENCH_PIECE
        source = "waiver_engine"
    return f"{WHY_DROP_PREFIX} {drop.name} — {why}", source


def _invalidation(ld, target) -> str | None:
    """The facts already on this card's inputs that would make the read
    wrong, in one sentence. Assembled, not inferred: each clause is a
    structured fact some module computed, and when none of them are true
    the row is simply absent rather than filled with a hedge.
    """
    facts: list[str] = []
    if EARLY_SEASON_CLAUSE in (target.reason or ""):
        facts.append(
            f"the trending count ({target.trend_count} adds) is an early-season sample, so it may be "
            "name recognition rather than usage"
        )
    market = getattr(ld, "replacement", None)
    if market is not None and market.scarcity_of(target.position) == ABUNDANT:
        facts.append(
            f"the {target.position} market here is {ABUNDANT} — comparable production stays available if he misses"
        )
    insurance = next(
        (i for i in (getattr(ld, "insurance", None) or []) if i.candidate.player_id == target.player_id), None
    )
    if insurance is not None and not insurance.starter.injury_status:
        facts.append(f"{insurance.starter.name} is healthy, so this cover may never be used")
    trend = (getattr(ld, "role_trends", None) or {}).get(target.player_id)
    if getattr(trend, "label", None) == ROLE_INSUFFICIENT:
        facts.append(f"the usage record is still {ROLE_INSUFFICIENT.lower()}")
    view = (getattr(ld, "source_views", None) or {}).get(target.player_id)
    if view is not None and view.consensus in (SOURCE_DISAGREEMENT, HIGH_DISAGREEMENT):
        facts.append(f"the ranking sources split on him ({view.consensus.lower()})")
    if not facts:
        return None
    return f"{INVALIDATION_PREFIX} " + "; ".join(facts[:MAX_INVALIDATION_FACTS]) + "."


def _waiver_card(ld, report, target, *, schedule, freshness_by_source) -> Provenance:
    drop_note = f", drop {target.drop_candidate.name}" if target.drop_candidate else ""
    card = _Card(WAIVER, target.player_id, f"Add {target.name}{drop_note}", freshness_by_source)
    conflict = conflict_for(getattr(ld, "conflicts", None) or [], WAIVER, target.player_id)
    if conflict is not None:
        for i, text in enumerate(conflict.reasons_against):
            category, _ = classify_annotation(text, default=(RISK, "recommendation_conflicts"))
            card.add(RISK, AGAINST, text, "recommendation_conflicts", fact=_fact_for(category, text), priority=i)

    clauses = [c.strip() for c in (target.reason or "").split(";") if c.strip()]
    if clauses:
        card.add(
            ROSTER, FOR, f"{target.priority_tier} ({target.horizon}): {clauses[0]}", "waiver_engine", fact=("tier",),
        )
    for i, clause in enumerate(clauses[1:]):
        direction = AGAINST if "exposure" in clause else FOR
        card.add_annotation(clause, direction, default=(ROSTER, "waiver_engine"), priority=i + 2)

    impact = (getattr(ld, "waiver_impacts", None) or {}).get(target.player_id)
    if impact is not None and impact.material_deltas():
        d = impact.weekly_points_delta
        direction = FOR if d >= MATERIAL_WEEKLY_POINTS else (AGAINST if d <= -MATERIAL_WEEKLY_POINTS else CONTEXT)
        card.add(ROSTER, direction, f"Move Impact: {'; '.join(impact.material_deltas())}", "move_impact", fact=("impact",))

    view = (getattr(ld, "source_views", None) or {}).get(target.player_id)
    # The annotator that wrote a note says which side it argues for
    # (ld.note_directions); the prose heuristics are only the fallback for
    # a note written without one.
    stated = getattr(ld, "note_directions", None) or {}
    for i, note in enumerate(target.notes):
        direction = stated.get((target.player_id, note)) or _waiver_note_direction(note, view)
        card.add_annotation(note, direction, default=(ROSTER, "waiver_engine"), priority=i + 1)

    _piece_role_reasons(card, ld, target, incoming=True)
    _piece_schedule_reason(card, ld, target, schedule)
    _exposure_reason(card, report, target)

    if target.trend_count:
        card.add(
            TIMING, CONTEXT, f"Trending add: {target.trend_count} adds across Sleeper", "waiver_engine", fact=("trending",),
        )
    faab = (getattr(ld, "faab", None) or {}).get(target.player_id)
    if faab is not None:
        card.add(
            LEAGUE_ECONOMY, CONTEXT,
            f"FAAB: {faab.posture} — {faab.share_of_remaining_text or f'${faab.suggested_dollars} bid'}", "faab_strategy", fact=("faab",),
        )
    elif target.suggested_faab_pct is not None:
        card.add(
            LEAGUE_ECONOMY, CONTEXT,
            f"Suggested FAAB: {target.suggested_faab_pct}% of the season budget", "waiver_engine", fact=("faab",),
        )
    drop = target.drop_candidate
    if drop is not None and not card.has(AGAINST, RISK):
        card.add(
            ROSTER, CONTEXT, f"Costs {drop.name} ({drop.position or '?'}) the roster spot", "waiver_engine", fact=("drop",), priority=5,
        )
    _timing_reasons(card, ld)
    why_drop = _why_drop(ld, target)
    if why_drop is not None:
        card.add_extra(ROSTER, why_drop[0], why_drop[1], fact=("why_drop",))
    card.add_extra(RISK, _invalidation(ld, target), "recommendation_provenance", fact=("invalidation",))
    return card.finish()


def _waiver_note_direction(note: str, view) -> str:
    """A note the annotation layer wrote is only a caveat where it says
    something is worse: an Abundant market, a bye, a falling market, or a
    source split that doesn't point toward buying."""
    lowered = note.lower()
    if "abundant" in lowered or lowered.startswith("schedule:"):
        return AGAINST
    if lowered.startswith("market velocity:"):
        return AGAINST if "falling" in lowered else FOR
    if lowered.startswith("sources:"):
        return FOR if view is not None and view.direction == PROJECTION_ABOVE_MARKET else AGAINST
    return FOR


def _drop_card(ld, candidate, *, freshness_by_source) -> Provenance:
    card = _Card(DROP, candidate.entry.player_id, f"{candidate.priority}: {candidate.entry.name}", freshness_by_source)
    for i, text in enumerate(candidate.reasons):
        lowered = text.lower()
        if lowered.startswith("market velocity"):
            direction = AGAINST if "rising" in lowered else FOR
        elif "keep unless" in lowered or lowered.startswith("but "):
            direction = AGAINST
        else:
            direction = FOR
        card.add_annotation(text, direction, default=(ROSTER, "trade_engine"), priority=i)
    market = getattr(ld, "replacement", None)
    if market is not None and not card.has(AGAINST, REPLACEMENT_MARKET):
        scarcity = market.scarcity_of(candidate.entry.position)
        if scarcity is not None:
            card.add(
                REPLACEMENT_MARKET, CONTEXT,
                f"{candidate.entry.position} replacement market here is {scarcity}", "replacement_value", fact=("scarcity",),
            )
    return card.finish()


def _defensive_add_card(ld, report, add, *, freshness_by_source) -> Provenance:
    drop_note = f", drop {add.drop.name}" if add.drop else ""
    card = _Card(DEFENSIVE_ADD, add.target.player_id, f"Defensive add: {add.target.name}{drop_note}", freshness_by_source)
    card.add(
        OPPONENT, FOR,
        f"{add.opponent_name} has {add.hole}; {add.target.name} would add {add.opponent_gain:+.1f} to their week-{add.week} lineup",
        "opponent_blocker", fact=("block",),
    )
    card.add(
        ROSTER, CONTEXT if abs(add.my_gain) < MATERIAL_WEEKLY_POINTS else FOR,
        f"Adds {add.my_gain:+.1f} to your own week-{add.week} lineup", "opponent_blocker", fact=("my_gain",),
    )
    matchup = getattr(ld, "matchup", None)
    if matchup is not None:
        card.add(OPPONENT, CONTEXT, matchup.describe(), "matchup_leverage", fact=("matchup",), priority=1)
    if add.drop is not None:
        card.add(
            ROSTER, AGAINST, f"Costs {add.drop.name} ({add.drop.position or '?'}) the roster spot", "opponent_blocker", fact=("drop",),
        )
    _exposure_reason(card, report, add.target)
    card.add(TIMING, CONTEXT, f"Week {add.week} only — the block expires with the matchup", "opponent_blocker", fact=("week",))
    return card.finish()


def _streamer_card(ld, plan, *, freshness_by_source) -> Provenance:
    card = _Card(STREAMER, plan.position, plan.describe(), freshness_by_source)
    card.add(PROJECTION, FOR, f"{plan.recommendation}: {plan.note}", "streamer_planner", fact=("plan",))
    best = plan.sequence.first if plan.recommendation != ADD and plan.sequence is not None else plan.single
    card.add(SCHEDULE, CONTEXT, f"{best.entry.name}: {best.week_text()}", "streamer_planner", fact=("weeks",))
    if plan.current is not None:
        card.add(
            ROSTER, CONTEXT,
            f"Current starter {plan.current.entry.name} projects {plan.current.total:.1f} over the window",
            "streamer_planner", fact=("current",), priority=1,
        )
    market = getattr(ld, "replacement", None)
    if market is not None:
        scarcity = market.scarcity_of(plan.position)
        if scarcity is not None:
            card.add(
                REPLACEMENT_MARKET, CONTEXT, f"{plan.position} replacement market here is {scarcity}",
                "replacement_value", fact=("scarcity",), priority=2,
            )
    card.add(
        TIMING, CONTEXT, f"Window is weeks {plan.weeks[0]}-{plan.weeks[-1]}" if len(plan.weeks) > 1 else f"Window is week {plan.weeks[0]}",
        "streamer_planner", fact=("window",), priority=3,
    )
    return card.finish()


def _stash_card(ld, candidate, *, freshness_by_source) -> Provenance:
    card = _Card(STASH, candidate.entry.player_id, f"{candidate.label}: {candidate.entry.name}", freshness_by_source)
    for i, text in enumerate(candidate.reasons):
        lowered = text.lower()
        if "no roster spot" in lowered:
            card.add(ROSTER, AGAINST, text, "stash_board", priority=i)
        elif "replacements are" in lowered:
            card.add(REPLACEMENT_MARKET, FOR, text, "stash_board", fact=("scarcity",), priority=i)
        elif "percentile" in lowered:
            card.add(MARKET, FOR, text, "stash_board", priority=i)
        else:
            card.add(ROSTER, CONTEXT, text, "stash_board", priority=i)
    if candidate.drop is not None:
        card.add(
            ROSTER, AGAINST, f"Costs {candidate.drop.name} ({candidate.drop.position or '?'}) the roster spot",
            "stash_board", fact=("drop",), priority=1,
        )
    card.add(TIMING, CONTEXT, "Developmental hold — not lineup help this week", "stash_board", fact=("horizon",))
    return card.finish()


def _alert_card(ld, note, *, freshness_by_source) -> Provenance:
    card = _Card(ALERT, note.player_name, f"{note.player_name} — {note.note}", freshness_by_source)
    card.add(ROSTER, FOR, f"{note.player_name}: {note.note}", "waiver_engine", fact=("alert",))
    card.add(TIMING, CONTEXT, "Check before this week's lineup locks", "waiver_engine", fact=("lock",))
    return card.finish()


def build_provenance(
    ld, report, *, schedule=None, freshness_by_source: dict[str, str] | None = None
) -> dict[tuple[str, str], Provenance]:
    """One card per recommendation in this league, keyed by the shared
    (kind, key) identity: trades by their index in `ld.proposals`, waivers
    / drops / defensive add / stashes by player_id, streamers by position,
    alerts by player name.

    `ld` is a LeagueReportData and `report` a WeeklyReportData, both
    duck-typed (this module is imported by report_data, never the other
    way round). `schedule` is the shared NFL schedule, used only where the
    annotation layer didn't already write a schedule note.
    """
    out: dict[tuple[str, str], Provenance] = {}
    if getattr(ld, "error", None) or not getattr(ld, "drafted", False):
        return out

    for i, proposal in enumerate(getattr(ld, "proposals", None) or []):
        prov = _trade_card(ld, report, i, proposal, schedule=schedule, freshness_by_source=freshness_by_source)
        out[(TRADE, str(i))] = prov
    for target in getattr(ld, "waiver_targets", None) or []:
        out[(WAIVER, target.player_id)] = _waiver_card(
            ld, report, target, schedule=schedule, freshness_by_source=freshness_by_source
        )
    for candidate in getattr(ld, "drop_candidates", None) or []:
        out[(DROP, candidate.entry.player_id)] = _drop_card(ld, candidate, freshness_by_source=freshness_by_source)
    defensive = getattr(ld, "defensive_add", None)
    if defensive is not None:
        out[(DEFENSIVE_ADD, defensive.target.player_id)] = _defensive_add_card(
            ld, report, defensive, freshness_by_source=freshness_by_source
        )
    for plan in getattr(ld, "streamers", None) or []:
        if plan.recommendation != HOLD:
            out[(STREAMER, plan.position)] = _streamer_card(ld, plan, freshness_by_source=freshness_by_source)
    for candidate in getattr(ld, "stash", None) or []:
        if candidate.label == PRIORITY_STASH:
            out[(STASH, candidate.entry.player_id)] = _stash_card(ld, candidate, freshness_by_source=freshness_by_source)
    for note in getattr(ld, "time_sensitive", None) or []:
        if note.severity == "high":
            out[(ALERT, note.player_name)] = _alert_card(ld, note, freshness_by_source=freshness_by_source)
    return out
