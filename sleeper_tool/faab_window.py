"""FAAB Window — a reasonable bidding range and one recommended bid inside it.

The window is not a prediction of the clearing bid; nothing here can see
what other managers will do. It is a deterministic statement of "how much
of what I have left is this add worth to THIS roster", built in four
documented steps:

  1. Base band: a share of REMAINING budget from the acquisition strength
     (BASE_BAND_PCT). Remaining, not the season budget: $16 of $40 left and
     $16 of $100 left are different decisions.
  2. Shifts: every applicable multiplier below is applied to both ends and
     named in `reasons`, so a reader can re-derive the number.
       up    Critical Need, Weak room, Immediate Starter, Scarce/Very Scarce
             position, Unique Opportunity / Few Alternatives
       down  Abundant position, Some/Many Alternatives, speculative-only
             add, Strong/Surplus room, late season for a non-priority add
     Source disagreement WIDENS the window (low down, high up) instead of
     moving it: the honest reading of a split is more uncertainty.
     A role label never shifts money (ROLE_MOVES_FAAB) until the role
     heuristic is redesigned — see docs/DECISIONS.md, 2026-09-04.
  3. Posture guardrails from faab_strategy.choose_posture: Preserve caps
     the window at PRESERVE_MAX_PCT of remaining; Priority Spend lifts its
     top to at least PRIORITY_MIN_HIGH_PCT; nothing else may exceed
     NON_PRIORITY_MAX_PCT; nothing ever exceeds what is left.
  4. The recommended point sits RECOMMEND_POINT through the window by need:
     Critical Need or an Immediate Starter near the top, a Weak room above
     the middle, an upside or speculative add in the lower third.

Opponent budgets are reported as facts ("Only 1 manager has more than $16
remaining"), never as a guess at what anyone will bid.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from sleeper_tool.faab_strategy import (
    ANCHOR_MIN_BIDS,
    ANCHOR_OVERSHOOT_RATIO,
    PRESERVE,
    PRIORITY_SPEND,
    PRIORITY_SPEND_MAX_PCT_OF_REMAINING,
    FaabAdvice,
    FaabContext,
    TargetFacts,
    _anchor_text,
    choose_posture,
)
from sleeper_tool.replacement_value import ABUNDANT, SCARCE, VERY_SCARCE
from sleeper_tool.roster_needs import CRITICAL_NEED, STRONG, SURPLUS, WEAK
from sleeper_tool.waiver_acquisition import (
    BYE_COVER,
    MAJOR_GAIN,
    DEPTH_ADD,
    IMMEDIATE_STARTER,
    PASS,
    PRIORITY_ADD,
    SPECULATIVE_ADD,
    SPECULATIVE_CLASSES,
    STREAMER,
    STRONG_ADD,
)
from sleeper_tool.waiver_alternatives import FEW, MANY, SOME, UNIQUE
from sleeper_tool.waiver_engine import MODERATE, MUST_ADD, SEASON_STARTER, SPECULATIVE
from sleeper_tool.waiver_engine import STASH as STASH_HORIZON
from sleeper_tool.waiver_engine import STREAMER as STREAMER_HORIZON
from sleeper_tool.waiver_engine import STRONG_ADD as ENGINE_STRONG_ADD

# (low %, high %) of REMAINING budget, by acquisition strength.
BASE_BAND_PCT = {PRIORITY_ADD: (14, 24), STRONG_ADD: (7, 14), DEPTH_ADD: (3, 8), SPECULATIVE_ADD: (1, 4)}
# Shifts multiply, so three modest cuts can erase a claim every board in the
# set recommends. The floor keeps the bottom of the scale meaningful: below
# it the dollar is smaller than the rounding.
MIN_TOTAL_MULTIPLIER = 0.45
# Two waiver boards inside their first twelve WITH rest-of-season or Boone
# support is the one piece of market information the window has: a claim
# other managers are also reading about is contested.
SHIFT_BROAD_CONVICTION = 1.20

SHIFT_CRITICAL_NEED = 1.35
SHIFT_WEAK = 1.15
SHIFT_IMMEDIATE_STARTER = 1.10
# The Immediate Starter premium (and the top of the window) is for a real
# upgrade, not for clearing STARTER_MIN_GAIN by a rounding error.
STARTER_PREMIUM_MIN_GAIN = 2.0
SHIFT_VERY_SCARCE = 1.25
SHIFT_SCARCE = 1.10
SHIFT_UNIQUE = 1.25
SHIFT_FEW = 1.10
SHIFT_ABUNDANT = 0.75
SHIFT_SOME = 0.90
SHIFT_MANY = 0.65
SHIFT_SPECULATIVE = 0.75
SHIFT_STRONG_ROOM = 0.70
SHIFT_SURPLUS_ROOM = 0.55
SHIFT_LATE_SEASON = 0.60
DISAGREEMENT_LOW = 0.80
DISAGREEMENT_HIGH = 1.10
LATE_SEASON_WEEKS_LEFT = 3
ROLE_MOVES_FAAB = False  # the role heuristic is annotation-only until redesigned

# Preserve caps a bid as a share of the SEASON budget, not of what is left:
# 4% of a dwindling balance means a manager with $15 in week 12 can never
# bid $2 for a starter, and FAAB has no salvage value at the end.
PRESERVE_MAX_PCT_OF_BUDGET = 3
PRIORITY_MIN_HIGH_PCT = 30
NON_PRIORITY_MAX_PCT = 35

POINT_TOP = 0.80  # Critical Need / Immediate Starter
POINT_WEAK = 0.65
POINT_NEUTRAL = 0.50
POINT_LUXURY = 0.33

# acquisition strength -> the waiver engine's tier, so faab_strategy's
# posture rules (written against those tiers) read the same add the same way.
ENGINE_TIER = {PRIORITY_ADD: MUST_ADD, STRONG_ADD: ENGINE_STRONG_ADD, DEPTH_ADD: MODERATE, SPECULATIVE_ADD: SPECULATIVE}


@dataclass(frozen=True)
class WindowFacts:
    strength: str
    need: str | None = None  # roster_needs label of the group he fills
    cls: str | None = None  # waiver_acquisition class
    scarcity: str | None = None
    alternatives: str | None = None  # waiver_alternatives density label
    alternatives_count: int = 0
    disagreement: bool = False
    projected: bool = True
    gain_per_week: float = 0.0  # the simulated lineup gain, so a premium scales with it
    broad_conviction: bool = False  # both waiver boards inside their first twelve, with support
    role_market: str | None = None  # read only when ROLE_MOVES_FAAB


@dataclass
class FaabWindow:
    low: int
    high: int
    recommended: int
    posture: str
    reasons: list[str] = field(default_factory=list)
    point: float = POINT_NEUTRAL

    def text(self) -> str:
        span = f"${self.low}" if self.low == self.high else f"${self.low}–{self.high}"
        return f"FAAB {span} · recommend ${self.recommended}"


def _horizon(cls: str | None) -> str:
    if cls == IMMEDIATE_STARTER:
        return SEASON_STARTER
    if cls in (BYE_COVER, STREAMER):
        return STREAMER_HORIZON
    return STASH_HORIZON


def build_window(ctx: FaabContext, facts: WindowFacts, *, name: str = "", player_id: str = "") -> FaabWindow | None:
    """None for a Pass, a pre-draft league, or a league that isn't FAAB."""
    if facts.strength == PASS or ctx.pre_draft or not ctx.is_faab:
        return None
    remaining = ctx.remaining
    if remaining <= 0:
        return FaabWindow(0, 0, 0, PRESERVE, ["out of FAAB — $0 claims only"], POINT_NEUTRAL)
    low_pct, high_pct = BASE_BAND_PCT[facts.strength]
    reasons = [f"{facts.strength}: {low_pct}–{high_pct}% of remaining"]
    mult = 1.0

    def shift(factor: float, why: str) -> None:
        nonlocal mult
        mult *= factor
        reasons.append(f"{why} ×{factor:g}")

    if facts.need == CRITICAL_NEED:
        shift(SHIFT_CRITICAL_NEED, "Critical Need")
    elif facts.need == WEAK:
        shift(SHIFT_WEAK, "Weak room")
    elif facts.need == SURPLUS and facts.cls != IMMEDIATE_STARTER:
        shift(SHIFT_SURPLUS_ROOM, "Surplus room")
    elif facts.need == STRONG and facts.cls != IMMEDIATE_STARTER:
        shift(SHIFT_STRONG_ROOM, "Strong room")
    if facts.cls == IMMEDIATE_STARTER and facts.gain_per_week >= STARTER_PREMIUM_MIN_GAIN:
        shift(SHIFT_IMMEDIATE_STARTER, f"Immediate Starter (+{facts.gain_per_week:.1f}/wk)")
    if facts.broad_conviction:
        shift(SHIFT_BROAD_CONVICTION, "every board recommends him")
    if facts.scarcity == VERY_SCARCE:
        shift(SHIFT_VERY_SCARCE, "Very Scarce position")
    elif facts.scarcity == SCARCE:
        shift(SHIFT_SCARCE, "Scarce position")
    elif facts.scarcity == ABUNDANT:
        shift(SHIFT_ABUNDANT, "Abundant position")
    # An unprojected player has no measurable substitutes; that is not
    # uniqueness and must never raise a bid. Neither is being the best player
    # on an Abundant wire: that market is Abundant BECAUSE of him, and
    # paying twice for one fact (once up, once down) nets to noise.
    if facts.alternatives == UNIQUE and facts.projected and facts.scarcity != ABUNDANT:
        shift(SHIFT_UNIQUE, "Unique Opportunity")
    elif facts.alternatives == FEW and facts.projected and facts.scarcity != ABUNDANT:
        shift(SHIFT_FEW, "Few Alternatives")
    elif facts.alternatives == SOME:
        shift(SHIFT_SOME, "Some Alternatives")
    elif facts.alternatives == MANY:
        shift(SHIFT_MANY, "Many Alternatives")
    if facts.cls in SPECULATIVE_CLASSES:
        shift(SHIFT_SPECULATIVE, "speculative add")
    weeks_left = ctx.weeks_to_playoffs
    # Only the speculative end is cut late: a budget that expires unspent is
    # worth nothing, so a real add late in the year is not the thing to save on.
    if weeks_left is not None and weeks_left <= LATE_SEASON_WEEKS_LEFT and facts.cls in SPECULATIVE_CLASSES:
        shift(SHIFT_LATE_SEASON, f"{weeks_left} weeks to the playoffs")
    if mult < MIN_TOTAL_MULTIPLIER:
        reasons.append(f"floored at ×{MIN_TOTAL_MULTIPLIER:g} of the base band")
        mult = MIN_TOTAL_MULTIPLIER

    low_pct, high_pct = low_pct * mult, high_pct * mult
    # The pre-widening pair survives the split: the recommendation is placed
    # inside THAT band, so disagreement widens what is reasonable without
    # making the bid larger.
    settled = (low_pct, high_pct)
    if facts.disagreement:
        low_pct, high_pct = low_pct * DISAGREEMENT_LOW, high_pct * DISAGREEMENT_HIGH
        reasons.append(f"sources disagree: window widened (low ×{DISAGREEMENT_LOW:g}, high ×{DISAGREEMENT_HIGH:g})")

    posture, posture_reasons = choose_posture(ctx, TargetFacts(
        player_id=player_id, name=name, tier=ENGINE_TIER[facts.strength], horizon=_horizon(facts.cls),
        scarcity=facts.scarcity, substitutes=facts.alternatives_count,
        need_urgency=facts.need == CRITICAL_NEED or facts.cls == BYE_COVER,
    ))
    reasons.extend(posture_reasons)
    cap_pct = NON_PRIORITY_MAX_PCT
    if posture == PRESERVE:
        budget = ctx.budget or remaining
        cap_pct = 100 * max(1, round(budget * PRESERVE_MAX_PCT_OF_BUDGET / 100)) / remaining
    elif posture == PRIORITY_SPEND:
        cap_pct = PRIORITY_SPEND_MAX_PCT_OF_REMAINING

    def dollars(band: tuple[float, float]) -> tuple[int, int]:
        lo_pct, hi_pct = band
        if posture == PRIORITY_SPEND:
            hi_pct = max(hi_pct, PRIORITY_MIN_HIGH_PCT)
        hi_pct = min(hi_pct, cap_pct)
        lo_pct = min(lo_pct, hi_pct)
        lo = min(max(1, math.floor(remaining * lo_pct / 100)), remaining)
        return lo, min(max(lo, math.ceil(remaining * hi_pct / 100)), remaining)

    low, high = dollars((low_pct, high_pct))

    if facts.need == CRITICAL_NEED or (facts.cls == IMMEDIATE_STARTER and facts.gain_per_week >= MAJOR_GAIN):
        point = POINT_TOP
    elif facts.need == WEAK or facts.cls == IMMEDIATE_STARTER:
        point = POINT_WEAK
    elif facts.cls in SPECULATIVE_CLASSES:
        point = POINT_LUXURY
    else:
        point = POINT_NEUTRAL
    if posture == PRESERVE:
        # Preserve is a decision not to chase; the point must not then sit at
        # the top of the window because the need label is urgent.
        point = min(point, POINT_NEUTRAL)
    settled_low, settled_high = dollars(settled)
    recommended = round(settled_low + point * (settled_high - settled_low))
    recommended = max(low, min(high, recommended))
    return FaabWindow(low, high, recommended, posture, reasons, point)


def outbid_text(ctx: FaabContext, dollars: int) -> str | None:
    """How many other managers hold more than the recommendation — a fact
    about budgets, not a forecast of bids."""
    others = ctx.others_remaining
    if not others:
        return None
    richer = sum(1 for r in others if r > dollars)
    if richer == 0:
        return f"No other manager has more than ${dollars} remaining"
    if richer == 1:
        return f"Only 1 manager has more than ${dollars} remaining"
    if richer == len(others):
        return f"All {richer} other managers can outbid ${dollars}"
    return f"{richer} managers can outbid ${dollars}"


def advice_from_window(ctx: FaabContext, window: FaabWindow, *, player_id: str, name: str, tier: str) -> FaabAdvice:
    """The window as a FaabAdvice, so every existing consumer (the waiver
    table's bid cell, Best Moves, the decision ledger) reads the
    recommended bid without a second code path."""
    remaining = ctx.remaining
    share = (
        f"Recommended bid uses approximately {round(100 * window.recommended / remaining)}% of remaining budget "
        f"(${window.recommended} of ${remaining})" if remaining else None
    )
    advice = FaabAdvice(
        player_id=player_id, posture=window.posture, suggested_pct=None, suggested_dollars=window.recommended,
        remaining=remaining, share_of_remaining_text=share, leverage_text=outbid_text(ctx, window.recommended),
        anchor_text=_anchor_text(ctx.league_bids), notes=[], name=name, tier=tier,
    )
    advice.window_low, advice.window_high, advice.window_reasons = window.low, window.high, list(window.reasons)
    contested = [b for b in ctx.league_bids if b > 0]
    if len(contested) >= ANCHOR_MIN_BIDS and max(contested) > 0 and window.recommended > max(contested) * ANCHOR_OVERSHOOT_RATIO:
        advice.notes.append(
            f"${window.recommended} is more than {ANCHOR_OVERSHOOT_RATIO:g}x the largest winning bid this league has paid "
            f"(${max(contested)}) — not a cap, but check it is what you meant"
        )
    return advice
