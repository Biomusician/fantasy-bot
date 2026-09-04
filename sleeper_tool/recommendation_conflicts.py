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
  - a waiver add whose drop is a current optimized starter, a
    developmental (clog-exempt) player, or the named fill for an upcoming
    bye hole — unless the tool's own drop list already recommends him
  (2-for-1 consolidations are ordinary proposals here and get the same checks)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.formatting import ordinal
from sleeper_tool.pick_opportunity import STRATEGIC
from sleeper_tool.portfolio_exposure import VERY_HIGH
from sleeper_tool.replacement_value import VERY_SCARCE
from sleeper_tool.roster_clog import is_dynasty_developmental
from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD
from sleeper_tool.trade_opportunity_cost import COSTS_LINEUP, FAVORABLE, MAJOR_LINEUP_COST, MOSTLY_NEUTRAL

CONFLICTED = "Conflicted Move — Review Manually"
# A developmental drop is only a conflict when the player has real dynasty
# value; the bottom of every dynasty roster is young players, and cutting
# one with no market is what the waiver engine is for.
DEVELOPMENTAL_DROP_MIN_PERCENTILE = 40.0
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


# Worse-to-better order, so a move between two of them has a direction.
_STATUS_RANK = {REBUILD: 0, MIDDLING: 1, CONTENDER: 2}


def _status_downgrade(impact) -> str | None:
    """"drops this team from contender to middling", when the previewed
    move does exactly that. A pure add can't (nothing leaves), and an
    unclassified side says nothing either way."""
    if impact is None or getattr(impact, "pure_add", False):
        return None
    before = (impact.before.displayed_status or impact.before.status) if impact.before is not None else None
    after = impact.after.status if impact.after is not None else None
    if before is None or after is None or before == after:
        return None
    if _STATUS_RANK.get(after, 1) >= _STATUS_RANK.get(before, 1):
        return None
    return f"drops this team from {before} to {after}"


def detect_conflicts(ld) -> list[Conflict]:
    """`ld` is a LeagueReportData (duck-typed; report_data imports this)."""
    out: list[Conflict] = []
    starters = set(ld.lineup.starter_ids) if ld.lineup is not None else set()
    for i, p in enumerate(ld.proposals):
        kind, key = TRADE, str(i)
        econ = ld.trade_economics[i] if i < len(ld.trade_economics) else None
        roster_econ = econ.roster_economics if econ is not None else None
        delta = f" ({econ.weekly_delta:+.1f}/wk)" if econ is not None and econ.weekly_delta is not None else ""
        against: list[str] = []
        if roster_econ == MAJOR_LINEUP_COST:
            against.append(f"{MAJOR_LINEUP_COST}{delta}")
        if p.trade_type == "sell_high":
            if roster_econ == COSTS_LINEUP and any(e.player_id in starters for e in p.give):
                against.append(f"sells a starter the lineup relies on{delta}")
        # A Very Scarce market only bites when the outgoing piece actually
        # plays: a bench-surplus sale out of a scarce market costs nothing.
        if ld.replacement is not None and (roster_econ in (COSTS_LINEUP, MAJOR_LINEUP_COST) or any(e.player_id in starters for e in p.give)):
            scarce = sorted({e.position for e in p.give if e.position and ld.replacement.scarcity_of(e.position) == VERY_SCARCE})
            for pos in scarce:
                against.append(f"{pos} replacement market is Very Scarce: no waiver replacement for what you'd send")
        # A trade that moves my own team status the wrong way (contender →
        # middling) is a strategic cost the card had no way to state: the
        # impact block printed the transition as a neutral fact while the
        # provenance card carried no Against at all.
        impact = ld.trade_impacts[i] if i < len(ld.trade_impacts) else None
        downgrade = _status_downgrade(impact)
        if downgrade is not None:
            against.append(downgrade)
        if _mentions_very_high(p.caveats):
            against.append("the acquisition would push cross-league exposure to Very High")
        if ld.pick_opportunity is not None and roster_econ == MOSTLY_NEUTRAL:
            for pick in p.give_picks:
                if ld.pick_opportunity.classification_for(pick) == STRATEGIC:
                    against.append(f"spends a Strategic pick ({pick.name}) for a Mostly Neutral lineup effect")
        if not against:
            continue
        reasons_for = [f"assets {econ.asset_economics.lower()}"] if econ is not None and econ.asset_economics == FAVORABLE else []
        reasons_for += [_short(r) for r in p.rationale_for_me[:2]]
        out.append(Conflict(kind, key, p.summary_line(), reasons_for, against))

    # The tool's own drop list: a drop it independently recommends is not
    # a conflict, however young the player.
    recommended_drops = {d.entry.player_id for d in ld.drop_candidates}
    bye_fills = _bye_fill_ids(ld)
    for t in ld.waiver_targets:
        against = []
        drop = t.drop_candidate
        if drop is not None and drop.player_id not in recommended_drops:
            if drop.player_id in starters:
                against.append(f"the drop, {drop.name}, is a current optimized starter")
            elif is_dynasty_developmental(drop, ld.currency) and (drop.value.dynasty_value_percentile or 0) >= DEVELOPMENTAL_DROP_MIN_PERCENTILE:
                against.append(f"the drop, {drop.name}, is a developmental hold worth keeping ({ordinal(round(drop.value.dynasty_value_percentile))} percentile dynasty value)")
            if drop.player_id in bye_fills:
                against.append(f"the drop, {drop.name}, is the named fill for your week {ld.bye_collision.week} bye hole")
        if _mentions_very_high([t.reason, *t.notes]):
            against.append("the add would push cross-league exposure to Very High")
        if not against:
            continue
        reasons_for = [f"{t.priority_tier} — {t.reason.split(';')[0]}"]
        out.append(Conflict(WAIVER, t.player_id, f"Add {t.name}", reasons_for, against))
    return out


def _bye_fill_ids(ld) -> set[str]:
    bye = getattr(ld, "bye_collision", None)
    if bye is None:
        return set()
    ids: set[str] = set()
    for hole in bye.holes:
        if hole.replacement is not None:
            ids.add(hole.replacement.player_id)
    return ids


def conflict_for(conflicts: list[Conflict], kind: str, key: str) -> Conflict | None:
    return next((c for c in conflicts if c.kind == kind and c.key == key), None)
