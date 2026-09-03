"""The record types a trade proposal is made of, plus the one identity
helper that says whether two packages are the same package.

Kept separate from `trade_engine.py` so that a module which only needs to
*hold* or *render* a proposal (report renderers, move_impact, market
velocity, the negotiation ladder) doesn't have to import the generator to
get the dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.draft_picks import OwnedPick, pick_key
from sleeper_tool.roster_analysis import RosterEntry

_TRADE_TYPE_LABELS = {"buy_low": "Buy low", "sell_high": "Sell high", "pick_target": "Pick target", "consolidation": "2-for-1 consolidation"}


def proposal_asset_key(players: list[RosterEntry], picks: list[OwnedPick]) -> frozenset:
    """The identity of a package (or of a proposal's give/receive side) by
    what's actually in it. A frozenset rather than a tuple because order
    within a package carries no meaning — "A + B" and "B + A" are the same
    offer — and because set difference is exactly how the negotiation
    ladder asks "is this step one move away from that one".
    """
    return frozenset([*(("player", e.player_id) for e in players), *(("pick", pick_key(p)) for p in picks)])


@dataclass
class TradeProposal:
    league_name: str
    currency: str
    target_username: str
    target_team_name: str | None
    give: list[RosterEntry]
    receive: list[RosterEntry]
    my_value_total: float
    their_value_total: float
    rationale_for_me: list[str]
    rationale_for_them: list[str]
    caveats: list[str]
    give_picks: list[OwnedPick] = field(default_factory=list)
    receive_picks: list[OwnedPick] = field(default_factory=list)
    # "buy_low" | "sell_high" | "pick_target" — which generation path built
    # this proposal, so the UI/user can tell a reactive buy-low ask apart
    # from a proactive sell-before-regression pitch or a rebuild pick grab.
    trade_type: str = "buy_low"
    acceptance_rating: str = "Moderate"  # one of ACCEPTANCE_TIERS — a bucket, not a fabricated precise probability
    acceptance_reasons: list[str] = field(default_factory=list)
    confidence: str = "Medium"  # "Low"|"Medium"|"High" — confidence in the underlying valuations, separate from acceptance
    message: str = ""  # short, casual chat-offer text ready to send the opponent

    @property
    def value_ratio(self) -> float:
        """>1 means I'm giving up more value than I get (rare, unfavorable)."""
        if self.their_value_total == 0:
            return float("inf")
        return self.my_value_total / self.their_value_total

    @property
    def balance_kind(self) -> str:
        """UI styling bucket for value_ratio — the single source of truth
        both report.py and html_report.py should read from instead of each
        re-deriving their own thresholds (they'd previously drifted:
        different label casing/wording for the same trade)."""
        ratio = self.value_ratio
        if ratio <= 1.1:
            return "positive"
        if ratio <= 1.15:
            return "caution"
        return "negative"

    @property
    def balance_label(self) -> str:
        ratio = self.value_ratio
        if 0.9 <= ratio <= 1.1:
            return "Balanced"
        if ratio < 0.9:
            return "Favors me"
        if ratio <= 1.15:
            return "Slight overpay"
        return "Overpay"

    @property
    def trade_type_label(self) -> str:
        """Single source of truth for trade_type -> display text, so
        report.py and html_report.py can't drift the way balance_kind's
        docstring says they previously did for value-ratio wording."""
        return _TRADE_TYPE_LABELS.get(self.trade_type, self.trade_type)

    def summary_line(self) -> str:
        give_names = ", ".join([*(e.name for e in self.give), *(p.name for p in self.give_picks)])
        receive_names = ", ".join([*(e.name for e in self.receive), *(p.name for p in self.receive_picks)])
        return f"Send {give_names} to {self.target_team_name or self.target_username} for {receive_names}"


@dataclass
class DropCandidate:
    entry: RosterEntry
    priority: str  # "Strong Drop" | "Consider Dropping"
    reasons: list[str]


@dataclass
class OpponentFit:
    target_is_starter: bool
    would_upgrade_their_roster: bool
    fit_notes: list[str]
    opponent_status: str  # contender | middling | rebuild
    status_fit: str  # good_fit | neutral | mismatch
    piece_count: int  # len(give) + len(give_picks) — 1 = clean ask, 2+ = fragmented
