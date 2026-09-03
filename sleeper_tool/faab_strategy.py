"""FAAB Strategy — what a suggested waiver bid actually costs out of the
budget I still have, and whether this is a week to spend or to preserve.

`waiver_engine._suggested_faab_pct` answers a narrower question: a
percentage of the TOTAL season budget for a priority tier, capped at the
percentage still unspent. Three things it deliberately doesn't do, and
this module does:

  1. It never reads `settings.waiver_type`, so a priority-waiver league
     would silently get a bid percentage. Here a non-FAAB league gets no
     advice at all (see `advise` / `status_note`).
  2. Its rows are independent: eight rows at 20% of the total budget can
     collectively exceed what's left. `budget_plan` marks the rows that
     stop being affordable once the ones above them clear — a note, never
     a re-rank.
  3. A percentage of the total budget is not what a manager is deciding.
     With $12 left, "20%" means $20 he cannot bid; every line here is
     expressed in dollars and as a share of REMAINING budget.

Postures (documented rules, named constants, no scoring):

  Priority Spend  a Must Add out of a Scarce/Very Scarce market with at
                  most FEW_SUBSTITUTES alternatives (or a surging role in
                  a scarce market) — the one case worth a real share of
                  what's left
  Aggressive      a Must/Strong Add where the market is scarce or the
                  roster need is urgent, and substitutes are thin
  Preserve        a streamer with plenty of substitutes, a marginal target
                  inside LATE_SEASON_WEEKS_LEFT of the playoffs, or a
                  budget already down to LOW_REMAINING_PCT
  Normal          everything else

The leverage and anchor lines are FACTS, not predictions: how many other
managers still hold more money than the proposed bid, and what winning
bids in this league have actually cost this season. The anchor never caps
a bid — it says the bid is far above what this league has been paying and
leaves the decision alone.

Everything here is plain values in and one record out: no Storage, no
network, no league objects. Callers assemble `FaabContext` from
`storage.get_league` / `storage.get_rosters` / `storage.get_all_transactions`.
"""
from __future__ import annotations

import statistics
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field

from sleeper_tool.replacement_value import ABUNDANT, SCARCE, VERY_SCARCE
from sleeper_tool.waiver_engine import (
    _FAAB_PCT_BY_TIER,
    MODERATE,
    MONITOR,
    MUST_ADD,
    SPECULATIVE,
    STREAMER,
    STRONG_ADD,
)

# Sleeper's settings.waiver_type: 0 rolling priority, 1 reversed standings,
# 2 FAAB. Read, never assumed — a priority league has no bid to size.
FAAB_WAIVER_TYPE = 2

# -- Postures ----------------------------------------------------------------
PRESERVE = "Preserve"
NORMAL = "Normal"
AGGRESSIVE = "Aggressive"
PRIORITY_SPEND = "Priority Spend"

# -- Named thresholds --------------------------------------------------------
FEW_SUBSTITUTES = 1  # at most this many comparable free agents = he is the market
SOME_SUBSTITUTES = 3
MANY_SUBSTITUTES = 4  # at or above this, a streamer is never worth a real bid
LATE_SEASON_WEEKS_LEFT = 3  # weeks to playoff_week_start at or below which marginal adds stop being worth money
LOW_REMAINING_PCT = 15  # remaining budget at or below this % of the total: preserve unless the target is a Priority Spend
PRIORITY_SPEND_MAX_PCT_OF_REMAINING = 60
SUBSTITUTE_PERCENTILE_BAND = 10.0  # percentile points either side of the target that counts as "the same player"
ANCHOR_OVERSHOOT_RATIO = 2.0  # proposed bid over this multiple of the season's largest winning bid gets a note
# One league in the real cache had settled two claims all season, both at
# $1. "More than 2x the largest winning bid" is arithmetically true there
# and means nothing: two bids are not a market. The median and max are
# still reported as the facts they are — only the overshoot NOTE waits.
ANCHOR_MIN_BIDS = 3

# Tiers a late-season budget shouldn't be spent on. Must/Strong Add are
# never "marginal" — the point of preserving is to still be able to pay
# for one of those.
MARGINAL_TIERS = (MODERATE, SPECULATIVE, MONITOR)
SCARCE_MARKETS = (SCARCE, VERY_SCARCE)

# Role labels this module reacts to. Strings, not an import: the role-trend
# module is a separate feature and this reads whatever label it is handed.
ROLE_RISING = "Role Rising"
ROLE_SURGING = "Role Surging"


@dataclass
class FaabContext:
    """One league's FAAB facts. `others_used` is every OTHER roster's
    `settings.waiver_budget_used`; a negative used value (FAAB acquired in
    a trade) is real and leaves that manager with more than the league
    budget, which is why nothing here clamps at the budget from above.
    """
    waiver_type: int | None = None
    budget: int | None = None
    my_used: int = 0
    others_used: list[int] = field(default_factory=list)
    current_week: int | None = None
    playoff_week_start: int | None = None
    trade_deadline: int | None = None
    pre_draft: bool = False
    league_bids: list[int] = field(default_factory=list)  # winning bids already paid in this league this season

    @property
    def is_faab(self) -> bool:
        return self.waiver_type == FAAB_WAIVER_TYPE and bool(self.budget)

    @property
    def remaining(self) -> int:
        """Can exceed `budget` (FAAB acquired by trade); never below zero."""
        return max(0, (self.budget or 0) - self.my_used)

    @property
    def others_remaining(self) -> list[int]:
        return [max(0, (self.budget or 0) - used) for used in self.others_used]

    @property
    def weeks_to_playoffs(self) -> int | None:
        if self.current_week is None or not self.playoff_week_start:
            return None
        return self.playoff_week_start - self.current_week


@dataclass
class TargetFacts:
    """Everything about ONE waiver target that changes the bid. All of it
    is already computed elsewhere (waiver_engine, replacement_value, the
    role-trend labels, bye/insurance/injury flags); nothing is re-derived
    here.
    """
    player_id: str
    name: str = ""
    tier: str = MODERATE
    horizon: str = STREAMER
    scarcity: str | None = None  # replacement_value label for his position
    role_label: str | None = None
    substitutes: int = 0  # comparable free agents at the position (count_substitutes)
    need_urgency: bool = False  # covers a bye hole / insures a fragile starter / answers an injury alert
    suggested_pct: int | None = None  # waiver_engine.WaiverTarget.suggested_faab_pct


@dataclass
class FaabAdvice:
    player_id: str
    posture: str
    suggested_pct: int | None  # the waiver engine's % of TOTAL budget, carried through unchanged
    suggested_dollars: int
    remaining: int
    share_of_remaining_text: str | None
    leverage_text: str | None
    anchor_text: str | None
    notes: list[str] = field(default_factory=list)
    name: str = ""
    tier: str = ""  # carried so budget_plan can find the Must/Strong Add rows

    def describe(self) -> str:
        bits = [f"{self.posture}: bid ${self.suggested_dollars} (${self.remaining} left)"]
        bits += [t for t in (self.share_of_remaining_text, self.leverage_text, self.anchor_text) if t]
        bits += self.notes
        name = f"{self.name}: " if self.name else ""
        return name + " — ".join(bits)


def context_from_sleeper(
    league_data: dict, rosters: list[dict], transactions: list[dict], my_roster_id: int,
    *, current_week: int | None, pre_draft: bool,
) -> FaabContext:
    """A FaabContext straight from the cached Sleeper payloads: league
    settings (waiver_type, waiver_budget, playoff_week_start,
    trade_deadline), every roster's `settings.waiver_budget_used`, and the
    winning bids of every completed waiver claim. A Sleeper league_id is one
    season, so every stored transaction is this season's."""
    settings = league_data.get("settings") or {}
    my_used = 0
    others: list[int] = []
    for r in rosters:
        try:
            used = int((r.get("settings") or {}).get("waiver_budget_used") or 0)
        except (TypeError, ValueError):
            used = 0
        if r.get("roster_id") == my_roster_id:
            my_used = used
        else:
            others.append(used)
    bids: list[int] = []
    for tx in transactions:
        if tx.get("type") != "waiver" or tx.get("status") != "complete":
            continue
        bid = (tx.get("settings") or {}).get("waiver_bid")
        if bid is None:
            continue
        try:
            bids.append(int(bid))
        except (TypeError, ValueError):
            continue

    def _int_or_none(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return FaabContext(
        waiver_type=_int_or_none(settings.get("waiver_type")),
        budget=_int_or_none(settings.get("waiver_budget")),
        my_used=my_used, others_used=others, current_week=current_week,
        playoff_week_start=_int_or_none(settings.get("playoff_week_start")),
        trade_deadline=_int_or_none(settings.get("trade_deadline")),
        pre_draft=pre_draft, league_bids=sorted(bids),
    )


def status_note(ctx: FaabContext) -> str | None:
    """Why there is no FAAB advice, when there isn't any. Renderers show
    this in place of a bid column rather than printing a bare "0%"."""
    if ctx.pre_draft:
        return None  # the league says nothing about waivers yet; silence is the honest output
    if not ctx.is_faab:
        return "League is not FAAB — waiver claims here run on priority order, so there is no bid to size."
    return None


def count_substitutes(
    free_agents: Sequence,
    position: str | None,
    target_percentile: float | None,
    *,
    percentile_of: Callable[[object], float | None] | None = None,
    exclude_ids: Collection[str] = (),
    band: float = SUBSTITUTE_PERCENTILE_BAND,
) -> int:
    """How many other free agents at `position` sit within `band` percentile
    points of the target — the "could I just add someone else instead?"
    count that separates a bid worth winning from one worth losing.

    Duck-typed: `free_agents` need only carry `player_id`, `position` and
    (for the default `percentile_of`) a `value`. Callers with a league
    currency in hand should pass their own `percentile_of` rather than rely
    on the default's attribute order.

    A target with no measurable percentile has no measurable substitutes:
    0, which reads as "he is the market". That is the wrong reading, so
    `advise` never promotes a target whose scarcity is unknown — the
    posture rules all require a scarcity label or an urgent need.
    """
    if target_percentile is None or not position:
        return 0
    getter = percentile_of or _default_percentile
    excluded = set(exclude_ids)
    count = 0
    for fa in free_agents:
        if getattr(fa, "position", None) != position or getattr(fa, "player_id", None) in excluded:
            continue
        pctl = getter(fa)
        if pctl is not None and abs(pctl - target_percentile) <= band:
            count += 1
    return count


def _default_percentile(entry) -> float | None:
    value = getattr(entry, "value", None)
    for attr in ("dynasty_positional_percentile", "dynasty_value_percentile", "redraft_ecr_percentile"):
        pctl = getattr(value, attr, None)
        if pctl is not None:
            return float(pctl)
    return None


def _is_scarce(scarcity: str | None) -> bool:
    return scarcity in SCARCE_MARKETS


def _is_priority_spend(facts: TargetFacts) -> bool:
    if facts.tier == MUST_ADD and _is_scarce(facts.scarcity) and facts.substitutes <= FEW_SUBSTITUTES:
        return True
    return facts.role_label == ROLE_SURGING and _is_scarce(facts.scarcity)


def _is_aggressive(facts: TargetFacts) -> bool:
    return (
        facts.tier in (MUST_ADD, STRONG_ADD)
        and (_is_scarce(facts.scarcity) or facts.need_urgency)
        and facts.substitutes <= SOME_SUBSTITUTES
    )


def choose_posture(ctx: FaabContext, facts: TargetFacts) -> tuple[str, list[str]]:
    """The posture and the reasons for it, in evaluation order. The
    streamer guardrail is checked FIRST so no later rule can talk the tool
    into paying up for a player four others can replace; the low-budget
    and late-season preserves are checked before Aggressive so a thin
    budget wins over an urgent-looking tier."""
    reasons: list[str] = []
    if facts.horizon == STREAMER and facts.substitutes >= MANY_SUBSTITUTES:
        return PRESERVE, [f"a streamer with {facts.substitutes} comparable free agents at {facts.scarcity or 'his position'} — win the cheap one or take the next"]
    if facts.scarcity == ABUNDANT and facts.substitutes >= MANY_SUBSTITUTES and not facts.need_urgency:
        return PRESERVE, [
            f"an Abundant market with {facts.substitutes} comparable free agents and no urgent need — "
            f"comparable production is on waivers, so don't pay a {facts.tier} price for it"
        ]

    priority = _is_priority_spend(facts)
    if ctx.budget and ctx.remaining <= round(ctx.budget * LOW_REMAINING_PCT / 100) and not priority:
        return PRESERVE, [f"${ctx.remaining} left of a ${ctx.budget} budget — hold it for a target you must win"]
    weeks_left = ctx.weeks_to_playoffs
    if weeks_left is not None and weeks_left <= LATE_SEASON_WEEKS_LEFT and facts.tier in MARGINAL_TIERS:
        return PRESERVE, [f"{weeks_left} weeks to the playoffs and this is a {facts.tier} — marginal adds stop paying for themselves here"]

    if priority:
        reasons.append(f"{facts.tier} in a {facts.scarcity} market with {facts.substitutes} comparable free agent(s)"
                       if facts.tier == MUST_ADD else f"{facts.role_label} in a {facts.scarcity} market")
        return PRIORITY_SPEND, reasons
    if _is_aggressive(facts):
        why = f"{facts.scarcity} replacement market" if _is_scarce(facts.scarcity) else "fills an urgent roster hole"
        return AGGRESSIVE, [f"{facts.tier}, {why}, {facts.substitutes} comparable free agent(s)"]
    return NORMAL, []


def _tier_bounds(tier: str, budget: int) -> tuple[int, int]:
    lo, hi = _FAAB_PCT_BY_TIER.get(tier, (0, 2))
    return round(budget * lo / 100), round(budget * hi / 100)


def _dollars_for_posture(ctx: FaabContext, facts: TargetFacts, posture: str, pct: int) -> int:
    budget = ctx.budget or 0
    base = min(round(budget * pct / 100), ctx.remaining)
    low, high = _tier_bounds(facts.tier, budget)
    if posture == PRESERVE:
        dollars = min(base, low)
    elif posture == AGGRESSIVE:
        dollars = high
    elif posture == PRIORITY_SPEND:
        # Up to 60% of what's left, but a Priority Spend never bids under
        # the tier's own high bound — the whole point is not losing him.
        dollars = max(high, round(ctx.remaining * PRIORITY_SPEND_MAX_PCT_OF_REMAINING / 100))
    else:
        dollars = base
    return max(0, min(dollars, ctx.remaining))


def _leverage_text(ctx: FaabContext, dollars: int) -> str | None:
    others = ctx.others_remaining
    if not others:
        return None
    can = sum(1 for r in others if r > dollars)
    if can == 0:
        return f"No other manager can outbid ${dollars}"
    if can == len(others):
        return f"All {can} other managers can outbid ${dollars}"
    return f"Only {can} of {len(others)} other managers can outbid ${dollars}"


def _anchor_text(bids: Sequence[int]) -> str | None:
    if not bids:
        return None
    plural = "" if len(bids) == 1 else "s"
    return f"Winning bids this season: median ${round(statistics.median(bids))}, max ${max(bids)} ({len(bids)} bid{plural})"


def advise(ctx: FaabContext, facts: TargetFacts) -> FaabAdvice | None:
    """None when there is nothing honest to say: a pre-draft league (no
    waiver wire yet) or a league whose `waiver_type` isn't FAAB. Callers
    render `status_note(ctx)` in that case rather than a bid.
    """
    if ctx.pre_draft or not ctx.is_faab:
        return None
    budget = ctx.budget or 0
    remaining = ctx.remaining
    lo, hi = _FAAB_PCT_BY_TIER.get(facts.tier, (0, 2))
    pct = facts.suggested_pct if facts.suggested_pct is not None else round((lo + hi) / 2)

    if remaining <= 0:
        return FaabAdvice(
            player_id=facts.player_id, posture=PRESERVE, suggested_pct=pct, suggested_dollars=0, remaining=0,
            share_of_remaining_text=None, leverage_text=None, anchor_text=_anchor_text(ctx.league_bids),
            notes=["you are out of FAAB; $0 claims only"], name=facts.name, tier=facts.tier,
        )

    posture, reasons = choose_posture(ctx, facts)
    dollars = _dollars_for_posture(ctx, facts, posture, pct)
    share = f"Suggested bid uses approximately {round(100 * dollars / remaining)}% of remaining budget (${dollars} of ${remaining})"

    notes = list(reasons)
    anchor = _anchor_text(ctx.league_bids)
    max_bid = max(ctx.league_bids) if len(ctx.league_bids) >= ANCHOR_MIN_BIDS else 0
    if max_bid > 0 and dollars > max_bid * ANCHOR_OVERSHOOT_RATIO:
        notes.append(
            f"${dollars} is more than {ANCHOR_OVERSHOOT_RATIO:g}x the largest winning bid this league has paid "
            f"(${max_bid}) — not a cap, but check it is what you meant"
        )
    if budget and remaining > budget:
        notes.append(f"you hold ${remaining} against a ${budget} league budget (FAAB acquired by trade)")

    return FaabAdvice(
        player_id=facts.player_id, posture=posture, suggested_pct=pct, suggested_dollars=dollars, remaining=remaining,
        share_of_remaining_text=share, leverage_text=_leverage_text(ctx, dollars), anchor_text=anchor,
        notes=notes, name=facts.name, tier=facts.tier,
    )


def bid_cell(advice: FaabAdvice | None, raw_pct: int | None) -> str:
    """The waiver table's FAAB column: a sized bid with its posture when the
    league is FAAB and the target got advice, else the engine's raw
    percentage of the total budget, else a dash."""
    if advice is not None:
        return f"${advice.suggested_dollars} · {advice.posture}"
    return f"{raw_pct}%" if raw_pct is not None else "—"


def bid_detail(advice: FaabAdvice | None) -> str | None:
    """The bid's reasoning, for Must/Strong Add rows only — the rows where
    money is actually at stake. One line: share of remaining budget, the
    outbid leverage, then the posture's own notes."""
    if advice is None or advice.tier not in (MUST_ADD, STRONG_ADD):
        return None
    bits = [b for b in (advice.share_of_remaining_text, advice.leverage_text, *advice.notes) if b]
    return "; ".join(bits) if bits else None


AFFORDABILITY_NOTE = "if the first claim clears, this one may not be affordable"


def budget_plan(targets: list[FaabAdvice], remaining: int) -> dict[str, str]:
    """Table-level sanity: the Must/Strong Add rows are independent bids on
    the same one budget. Walking them in the order they are displayed, the
    first row whose running total passes what's left — and every row after
    it — gets `AFFORDABILITY_NOTE`.

    Returns {player_id: note}. Nothing is re-ranked, re-priced or dropped:
    which claim to actually win is the manager's call, and the tool has no
    way to know which one clears.
    """
    notes: dict[str, str] = {}
    spent = 0
    for advice in targets:
        if advice.tier not in (MUST_ADD, STRONG_ADD):
            continue
        spent += advice.suggested_dollars
        if spent > remaining:
            notes[advice.player_id] = AFFORDABILITY_NOTE
    return notes
