"""Opportunity Cost Trade Analysis — every serious trade gets two separate
verdicts that are never blended into one score:

  asset_economics   what the trade does to the VALUE I hold, straight from
                    the engine's own valuation of the two sides
                    (TradeProposal.balance_label):
                      Favorable / Roughly Even / Unfavorable
  roster_economics  what the trade does to the LINEUP, from Move Impact's
                    projected weekly starter points (shared optimizer):
                      Improves Lineup     delta >= IMPROVES_MIN
                      Mostly Neutral      COSTS_MAX < delta < IMPROVES_MIN
                      Costs Lineup        delta <= COSTS_MAX
                      Major Lineup Cost   delta <= MAJOR_COST_MAX

When the two point in opposite directions — a value win that hurts the
lineup, or a value loss that improves it — the trade is a STRATEGIC
TRADEOFF and is labelled as such. That's the "sell C.J. Stroud for a 2028
1st in Superflex: +value, -10.7 pts/week" shape, and it's the point of the
module: to surface the tension rather than collapse it. Trades below the
preview bar (no Move Impact) get asset economics only.

Replacement context (replacement_value) is attached as a note where the
outgoing side sits in a Scarce/Very Scarce market — the lineup cost is
then also a cost that waivers can't repair.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.move_impact import MoveImpact
from sleeper_tool.replacement_value import SCARCE, VERY_SCARCE, ReplacementMarket
from sleeper_tool.trade_engine import TradeProposal

IMPROVES_MIN = 3.0  # projected weekly starter points
COSTS_MAX = -2.0
MAJOR_COST_MAX = -7.0

FAVORABLE = "Favorable"
ROUGHLY_EVEN = "Roughly Even"
UNFAVORABLE = "Unfavorable"
IMPROVES_LINEUP = "Improves Lineup"
MOSTLY_NEUTRAL = "Mostly Neutral"
COSTS_LINEUP = "Costs Lineup"
MAJOR_LINEUP_COST = "Major Lineup Cost"
STRATEGIC_TRADEOFF = "Strategic Tradeoff"

_ASSET_BY_BALANCE = {"Favors me": FAVORABLE, "Balanced": ROUGHLY_EVEN, "Slight overpay": UNFAVORABLE, "Overpay": UNFAVORABLE}


@dataclass
class TradeEconomics:
    asset_economics: str
    roster_economics: str | None  # None when the trade wasn't previewed
    weekly_delta: float | None
    strategic_tradeoff: bool
    scarcity_note: str | None = None

    def describe(self) -> str:
        bits = [f"Assets: {self.asset_economics}"]
        if self.roster_economics:
            delta = f" ({self.weekly_delta:+.1f}/wk)" if self.weekly_delta is not None else ""
            bits.append(f"Lineup: {self.roster_economics}{delta}")
        if self.strategic_tradeoff:
            bits.append(STRATEGIC_TRADEOFF)
        if self.scarcity_note:
            bits.append(self.scarcity_note)
        return " · ".join(bits)


def asset_economics(proposal: TradeProposal) -> str:
    return _ASSET_BY_BALANCE.get(proposal.balance_label, ROUGHLY_EVEN)


def roster_economics(weekly_delta: float) -> str:
    if weekly_delta <= MAJOR_COST_MAX:
        return MAJOR_LINEUP_COST
    if weekly_delta <= COSTS_MAX:
        return COSTS_LINEUP
    if weekly_delta >= IMPROVES_MIN:
        return IMPROVES_LINEUP
    return MOSTLY_NEUTRAL


def is_strategic_tradeoff(asset: str, roster: str | None) -> bool:
    if roster is None:
        return False
    value_up = asset == FAVORABLE
    value_down = asset == UNFAVORABLE
    lineup_up = roster == IMPROVES_LINEUP
    lineup_down = roster in (COSTS_LINEUP, MAJOR_LINEUP_COST)
    return (value_up and lineup_down) or (value_down and lineup_up)


def analyze_trade(
    proposal: TradeProposal, impact: MoveImpact | None, market: ReplacementMarket | None = None
) -> TradeEconomics:
    asset = asset_economics(proposal)
    delta = impact.weekly_points_delta if impact is not None else None
    roster = roster_economics(delta) if delta is not None else None
    note = None
    if market is not None and roster in (COSTS_LINEUP, MAJOR_LINEUP_COST):
        scarce = sorted({e.position for e in proposal.give if market.scarcity_of(e.position) in (SCARCE, VERY_SCARCE) and e.position})
        if scarce:
            note = f"{'/'.join(scarce)} replacement market is {'/'.join(market.scarcity_of(p) for p in scarce)} — waivers won't repair this"
    return TradeEconomics(
        asset_economics=asset, roster_economics=roster, weekly_delta=delta,
        strategic_tradeoff=is_strategic_tradeoff(asset, roster), scarcity_note=note,
    )
