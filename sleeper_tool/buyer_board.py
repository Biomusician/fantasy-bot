"""Buyer Board — for each of my sell-high candidates, the three
counterparties most likely to actually pay, scored on signals the tool
already computes (nothing new is estimated):

  need        the piece upgrades their position (trade_fit.piece_fits)
              and/or that position is one of their top needs
  timeline    contender wants proven production, rebuild wants youth
              (trade_fit.status_fit)
  economy     League Economy labels: a Frequent Trader is a live buyer; an
              Inactive Trader is capped at Possible Fit whatever the need;
              a manager already heavy at the position has less use for him
              (and the position is not counted as a need for them)
  scarcity    a Scarce/Very Scarce replacement market means a buyer can't
              fix the position from waivers
  fundable    their tradeable assets plus valued picks can cover the price
              — a prerequisite (a heavy penalty when they can't), not a
              point in favour, since nearly every roster can fund
              something

Labels: Strong Fit (score >= STRONG_FIT_MIN), Possible Fit
(>= POSSIBLE_FIT_MIN), Poor Fit (hidden). Strong needs a real need AND
at least one of timeline, economy or scarcity on top. The board feeds the trade
engine's sell-high proposals as annotations only: a Strong-Fit target is
said to be one; a stronger buyer elsewhere is a caveat on the proposal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.asset_value import value_currency, value_for_currency
from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.league_economy import FREQUENT_TRADER, INACTIVE_TRADER, POSITION_HEAVY, LeagueEconomy
from sleeper_tool.replacement_value import SCARCE, VERY_SCARCE, ReplacementMarket
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.roster_assets import tradeable_pool
from sleeper_tool.team_status import MIDDLING
from sleeper_tool.trade_fit import piece_fits, status_fit
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.valuation import CORE_SKILL_POSITIONS
from sleeper_tool.trade_engine import identify_needs, identify_sell_high

STRONG_FIT_MIN = 4
POSSIBLE_FIT_MIN = 2
UNFUNDED_PENALTY = 2
MAX_BUYERS = 3
MAX_CANDIDATES = 5
TOP_NEEDS = 2

STRONG_FIT = "Strong Fit"
POSSIBLE_FIT = "Possible Fit"
POOR_FIT = "Poor Fit"


@dataclass
class BuyerFit:
    roster_id: int
    username: str
    team_name: str
    label: str
    score: int
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.team_name} — {self.label} ({'; '.join(self.reasons)})"


@dataclass
class BuyerBoard:
    candidate: RosterEntry
    buyers: list[BuyerFit]  # Strong/Possible only, best first, capped at MAX_BUYERS
    all_fits: list[BuyerFit] = field(default_factory=list)  # every counterparty scored, for lookups

    def fit_for(self, username: str) -> BuyerFit | None:
        return next((b for b in self.all_fits if b.username == username), None)

    @property
    def strong(self) -> list[BuyerFit]:
        return [b for b in self.buyers if b.label == STRONG_FIT]


def fit_label(score: int) -> str:
    if score >= STRONG_FIT_MIN:
        return STRONG_FIT
    if score >= POSSIBLE_FIT_MIN:
        return POSSIBLE_FIT
    return POOR_FIT


def score_buyer(
    their: ValuedRoster,
    piece: RosterEntry,
    currency: str,
    *,
    their_status: str,
    economy_labels: list[str],
    heavy_positions: list[str],
    scarcity: str | None,
    pick_value: float,
) -> BuyerFit:
    score = 0
    reasons: list[str] = []
    pos = piece.position or "?"
    heavy = POSITION_HEAVY in economy_labels and pos in heavy_positions
    # One positional-need fact, scored once: a position is a top need
    # precisely because a piece upgrades it, so the two readings never add.
    if piece_fits(their, piece, currency):
        score += 2
        reasons.append(f"upgrades their {pos}")
        if not heavy and pos in identify_needs(their)[:TOP_NEEDS]:
            reasons.append(f"{pos} is a top need")
    elif not heavy and pos in identify_needs(their)[:TOP_NEEDS]:
        score += 1
        reasons.append(f"{pos} is a top need")
    timeline_fit = status_fit([piece], [], their_status)
    if timeline_fit == "good_fit":
        score += 1
        reasons.append(f"fits a {their_status} timeline")
    elif timeline_fit == "mismatch":
        score -= 1
        reasons.append(f"cuts against a {their_status} timeline")
    if FREQUENT_TRADER in economy_labels:
        score += 1
        reasons.append("frequent trader")
    if INACTIVE_TRADER in economy_labels:
        score -= 1
        reasons.append("inactive trader")
    if heavy:
        score -= 1
        reasons.append(f"already heavy at {pos}")
    if scarcity in (SCARCE, VERY_SCARCE):
        score += 1
        reasons.append(f"{pos} is {scarcity} on waivers")
    price = value_for_currency(piece.value, currency) or 0
    funds = sum(value_for_currency(e.value, currency) or 0 for e in tradeable_pool(their, their_status)) + pick_value
    if price and funds < price:
        score -= UNFUNDED_PENALTY
        reasons.append("little to pay with")
    label = fit_label(score)
    if label == STRONG_FIT and INACTIVE_TRADER in economy_labels:
        label = POSSIBLE_FIT  # a manager who doesn't trade is never a Strong buyer, whatever the need
    return BuyerFit(
        roster_id=their.roster_id, username=their.owner_username or "", team_name=their.team_name or their.owner_username or f"roster {their.roster_id}",
        label=label, score=score, reasons=reasons,
    )


def sell_high_candidates(my_roster: ValuedRoster, proposals: list[TradeProposal]) -> list[RosterEntry]:
    currency = value_currency(my_roster)
    seen: dict[str, RosterEntry] = {}
    for p in proposals:
        if p.trade_type == "sell_high":
            for e in p.give:
                seen.setdefault(e.player_id, e)
    for e in identify_sell_high(my_roster):
        seen.setdefault(e.player_id, e)
    skill = [e for e in seen.values() if e.position in CORE_SKILL_POSITIONS]
    return sorted(skill, key=lambda e: (-(value_for_currency(e.value, currency) or 0), e.name))[:MAX_CANDIDATES]


def build_buyer_boards(
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    candidates: list[RosterEntry],
    *,
    status_of: dict[int, str],
    economy: LeagueEconomy | None,
    market: ReplacementMarket | None,
    valued_picks: dict[int, list[OwnedPick]] | None,
) -> list[BuyerBoard]:
    currency = value_currency(my_roster)
    boards: list[BuyerBoard] = []
    for piece in candidates:
        fits: list[BuyerFit] = []
        scored: list[BuyerFit] = []
        for rid, their in rosters.items():
            if rid == my_roster.roster_id or not their.entries:
                continue
            m = economy.managers.get(rid) if economy is not None else None
            fit = score_buyer(
                their, piece, currency,
                their_status=status_of.get(rid, MIDDLING),
                economy_labels=list(m.labels) if m else [],
                heavy_positions=list(m.heavy_positions) if m else [],
                scarcity=market.scarcity_of(piece.position) if market is not None else None,
                pick_value=sum(p.value or 0 for p in (valued_picks or {}).get(rid, [])),
            )
            scored.append(fit)
            if fit.label != POOR_FIT:
                fits.append(fit)
        fits.sort(key=lambda f: (-f.score, f.team_name))
        boards.append(BuyerBoard(piece, fits[:MAX_BUYERS], scored))
    return boards


def annotate_sell_high_proposals(proposals: list[TradeProposal], boards: list[BuyerBoard]) -> None:
    by_piece = {b.candidate.player_id: b for b in boards}
    for p in proposals:
        if p.trade_type != "sell_high":
            continue
        for e in p.give:
            board = by_piece.get(e.player_id)
            if board is None:
                continue
            target = board.fit_for(p.target_username)
            if target is not None and target.label == STRONG_FIT:
                p.rationale_for_them.append(f"Buyer board: {target.team_name} is a Strong Fit for {e.name} ({'; '.join(target.reasons)}).")
            stronger = [b for b in board.strong if b.username != p.target_username]
            if stronger and (target is None or target.label != STRONG_FIT):
                best = stronger[0]
                p.caveats.append(
                    f"Buyer board: {best.team_name} is a Strong Fit for {e.name} ({'; '.join(best.reasons)}); "
                    f"{p.target_team_name or p.target_username} rates {target.label if target else POOR_FIT}."
                )
