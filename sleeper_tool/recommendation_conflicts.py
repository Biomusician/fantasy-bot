"""Recommendation Conflicts — when two of the tool's own signals point
opposite ways on the same move, say so instead of letting whichever
module rendered last win. Nothing is suppressed or re-scored; the move is
labelled "Conflicted Move — Review Manually" with the reasons on each
side, next to the recommendation and on its Best Moves line.

Detected pairs (all from objects report_data already built):
  - a Sell High that gives a starter the lineup relies on
    (trade economics Costs Lineup / Major Lineup Cost)
  - a Sell High out of a Very Scarce replacement market
  - any trade with a Major Lineup Cost
  - an acquisition (trade target or waiver add) that would push a player
    to Very High cross-league exposure
  - spending a Strategic pick for a Mostly Neutral lineup effect
  - a waiver add whose drop is a developmental (clog-exempt) player
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.pick_opportunity import STRATEGIC
from sleeper_tool.portfolio_exposure import VERY_HIGH
from sleeper_tool.replacement_value import VERY_SCARCE
from sleeper_tool.roster_clog import _is_dynasty_developmental
from sleeper_tool.trade_opportunity_cost import COSTS_LINEUP, MAJOR_LINEUP_COST, MOSTLY_NEUTRAL

CONFLICTED = "Conflicted Move — Review Manually"
TRADE = "trade"
WAIVER = "waiver"


@dataclass
class Conflict:
    kind: str  # TRADE / WAIVER
    key: str  # proposal index (as text) or waiver player_id
    subject: str
    reasons_for: list[str] = field(default_factory=list)
    reasons_against: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{CONFLICTED}: for — {'; '.join(self.reasons_for) or 'see the recommendation'} | against — {'; '.join(self.reasons_against)}"


def _short(text: str) -> str:
    """The first clause of a rationale sentence — enough to name the case for."""
    return text.split(" — ")[0].rstrip(".")


def _mentions_very_high(texts) -> bool:
    return any(VERY_HIGH in t for t in texts)


def detect_conflicts(ld) -> list[Conflict]:
    """`ld` is a LeagueReportData (duck-typed; report_data imports this)."""
    out: list[Conflict] = []
    starters = set(ld.lineup.starter_ids) if ld.lineup is not None else set()
    for i, p in enumerate(ld.proposals):
        econ = ld.trade_economics[i] if i < len(ld.trade_economics) else None
        roster_econ = econ.roster_economics if econ is not None else None
        delta = f" ({econ.weekly_delta:+.1f}/wk)" if econ is not None and econ.weekly_delta is not None else ""
        against: list[str] = []
        if roster_econ == MAJOR_LINEUP_COST:
            against.append(f"{MAJOR_LINEUP_COST}{delta}")
        if p.trade_type == "sell_high":
            if roster_econ == COSTS_LINEUP and any(e.player_id in starters for e in p.give):
                against.append(f"sells a starter the lineup relies on{delta}")
            if ld.replacement is not None:
                scarce = sorted({e.position for e in p.give if e.position and ld.replacement.scarcity_of(e.position) == VERY_SCARCE})
                for pos in scarce:
                    against.append(f"{pos} replacement market is Very Scarce: no waiver replacement for what you'd send")
        if _mentions_very_high(p.caveats):
            against.append("the acquisition would push cross-league exposure to Very High")
        if ld.pick_opportunity is not None and roster_econ == MOSTLY_NEUTRAL:
            for pick in p.give_picks:
                if ld.pick_opportunity.classification_for(pick) == STRATEGIC:
                    against.append(f"spends a Strategic pick ({pick.name}) for a Mostly Neutral lineup effect")
        if not against:
            continue
        reasons_for = [f"assets {econ.asset_economics.lower()}"] if econ is not None else []
        reasons_for += [_short(r) for r in p.rationale_for_me[:2]]
        out.append(Conflict(TRADE, str(i), p.summary_line(), reasons_for, against))

    for t in ld.waiver_targets:
        against = []
        if t.drop_candidate is not None and _is_dynasty_developmental(t.drop_candidate, ld.currency):
            against.append(f"the drop, {t.drop_candidate.name}, is a developmental hold (clog-exempt)")
        if _mentions_very_high([t.reason, *t.notes]):
            against.append("the add would push cross-league exposure to Very High")
        if not against:
            continue
        reasons_for = [f"{t.priority_tier} — {t.reason.split(';')[0]}"]
        out.append(Conflict(WAIVER, t.player_id, f"Add {t.name}", reasons_for, against))
    return out


def conflict_for(conflicts: list[Conflict], kind: str, key: str) -> Conflict | None:
    return next((c for c in conflicts if c.kind == kind and c.key == key), None)
