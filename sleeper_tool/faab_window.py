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
BASE_BAND_PCT = {PRIORITY_ADD: (14, 24), STRONG_ADD: (7, 14), DEPTH_ADD: (2, 6), SPECULATIVE_ADD: (1, 3)}

SHIFT_CRITICAL_NEED = 1.35
SHIFT_WEAK = 1.15
SHIFT_IMMEDIATE_STARTER = 1.10
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

PRESERVE_MAX_PCT = 4
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
    if facts.cls == IMMEDIATE_STARTER:
        shift(SHIFT_IMMEDIATE_STARTER, "Immediate Starter")
    if facts.scarcity == VERY_SCARCE:
        shift(SHIFT_VERY_SCARCE, "Very Scarce position")
    elif facts.scarcity == SCARCE:
        shift(SHIFT_SCARCE, "Scarce position")
    elif facts.scarcity == ABUNDANT:
        shift(SHIFT_ABUNDANT, "Abundant position")
    # An unprojected player has no measurable substitutes; that is not
    # uniqueness and must never raise a bid.
    if facts.alternatives == UNIQUE and facts.projected:
        shift(SHIFT_UNIQUE, "Unique Opportunity")
    elif facts.alternatives == FEW and facts.projected:
        shift(SHIFT_FEW, "Few Alternatives")
    elif facts.alternatives == SOME:
        shift(SHIFT_SOME, "Some Alternatives")
    elif facts.alternatives == MANY:
        shift(SHIFT_MANY, "Many Alternatives")
    if facts.cls in SPECULATIVE_CLASSES:
        shift(SHIFT_SPECULATIVE, "speculative add")
    weeks_left = ctx.weeks_to_playoffs
    if weeks_left is not None and weeks_left <= LATE_SEASON_WEEKS_LEFT and facts.strength != PRIORITY_ADD:
        shift(SHIFT_LATE_SEASON, f"{weeks_left} weeks to the playoffs")

    low_pct, high_pct = low_pct * mult, high_pct * mult
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
        cap_pct = PRESERVE_MAX_PCT
    elif posture == PRIORITY_SPEND:
        high_pct = max(high_pct, PRIORITY_MIN_HIGH_PCT)
        cap_pct = PRIORITY_SPEND_MAX_PCT_OF_REMAINING
    high_pct = min(high_pct, cap_pct)
    low_pct = min(low_pct, high_pct)

    low = max(1, math.floor(remaining * low_pct / 100))
    high = max(low, math.ceil(remaining * high_pct / 100))
    low, high = min(low, remaining), min(high, remaining)

    if facts.need == CRITICAL_NEED or facts.cls == IMMEDIATE_STARTER:
        point = POINT_TOP
    elif facts.need == WEAK:
        point = POINT_WEAK
    elif facts.cls in SPECULATIVE_CLASSES:
        point = POINT_LUXURY
    else:
        point = POINT_NEUTRAL
    recommended = max(low, min(high, round(low + point * (high - low))))
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
    return advice
