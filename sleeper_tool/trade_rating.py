"""How likely is the other manager to say yes, and how much do we trust the
numbers underneath the offer? Two separate questions, two separate outputs.

Extracted from `trade_engine.py` so the negotiation ladder and the
consolidation search can score a package without importing the generator
that normally builds one.
"""
from __future__ import annotations

from sleeper_tool.roster_analysis import RosterEntry
from sleeper_tool.team_status import veteran_min_age, young_max_age
from sleeper_tool.trade_types import OpponentFit
from sleeper_tool.valuation import PlayerValue

ACCEPTANCE_TIERS = ("Very Low", "Low", "Moderate", "Good", "High")
VERY_LOW_ACCEPTANCE = ACCEPTANCE_TIERS[0]


def rate_acceptance(
    fit: OpponentFit, value_ratio: float, profile, give: list[RosterEntry] = ()
) -> tuple[str, list[str]]:
    """Buckets acceptance likelihood into ACCEPTANCE_TIERS rather than a
    fabricated precise probability — the underlying signals (value
    closeness, roster fit, timeline fit, owner tendencies) support a
    directional read, not a number with real statistical meaning.

    `value_ratio` is (value I give) / (value I receive) at every call site
    — below 1.0 means I'm lowballing them, above 1.0 means I'm overpaying
    (favorable to THEM). Only the lowball direction should ever reduce
    acceptance likelihood; a generous overpay should never look less
    likely to be accepted than a fair trade (the user's own "am I
    overpaying" signal is surfaced separately via balance_label, not here).

    `give` is the piece(s) the OPPONENT would receive — used only to check
    the offer's age profile against their documented youth/veteran
    preference (same veteran_heavy/young_heavy signal status_fit uses).
    """
    score = 0
    reasons: list[str] = []
    if abs(value_ratio - 1.0) <= 0.05:
        score += 1
        reasons.append("Value is nearly dead-even.")
    elif value_ratio < 0.85:
        score -= 1
        reasons.append("Value sits near the edge of an acceptable range.")
    if fit.target_is_starter:
        score -= 1
        reasons.append("Asks for their current starter — expect more resistance than a bench piece.")
    if not fit.would_upgrade_their_roster:
        score -= 2
        reasons.append("What they'd receive doesn't clearly beat their existing depth at that position.")
    if fit.status_fit == "good_fit":
        score += 1
        reasons.append(f"Matches their apparent {fit.opponent_status} timeline.")
    elif fit.status_fit == "mismatch":
        score -= 1
        reasons.append(f"Cuts against their apparent {fit.opponent_status} timeline.")
    if fit.piece_count >= 2:
        if profile.dislikes_multi_piece:
            score -= 2
            reasons.append(f"{profile.username or 'This owner'} has specifically pushed back on lopsided multi-piece offers before — this needs to be a clean, close-to-even bundle or it likely stalls.")
        else:
            score -= 1
            reasons.append("Multi-piece offer — some managers read this as a lowball.")
    if profile.trades_often == "inactive":
        score = min(score, -2)
        reasons.append(f"{profile.username or 'This owner'} is documented as rarely completing trades.")
    elif profile.trades_often == "infrequent":
        score -= 1
        reasons.append(f"{profile.username or 'This owner'} doesn't complete trades often — even a fair offer may take patience and follow-up.")
    elif profile.trades_often == "active":
        score += 1
        reasons.append(f"{profile.username or 'This owner'} trades often.")
    if give:
        veteran_heavy = any(e.age is not None and e.age >= veteran_min_age(e.position) for e in give)
        young_heavy = (not veteran_heavy) and any(e.age is not None and e.age <= young_max_age(e.position) for e in give)
        if profile.youth_vs_veteran == "prefers_youth" and veteran_heavy:
            score -= 1
            reasons.append(f"{profile.username or 'This owner'} prefers young players — this leans veteran, which may need sweetening.")
        elif profile.youth_vs_veteran == "prefers_veteran" and young_heavy:
            score -= 1
            reasons.append(f"{profile.username or 'This owner'} buys veterans, not unproven youth — this leans young, which may need sweetening.")
    idx = max(0, min(len(ACCEPTANCE_TIERS) - 1, 2 + score))
    return ACCEPTANCE_TIERS[idx], reasons


def player_confidence(pv: PlayerValue) -> str:
    if not pv.is_corroborated or pv.cross_source_agreement == "high_disagreement":
        return "Low"
    if pv.cross_source_agreement == "moderate_disagreement" or pv.thin_market_caveat or pv.panel_disagreement_caveat:
        return "Medium"
    return "High"


_CONFIDENCE_RANK = {"Low": 0, "Medium": 1, "High": 2}


def proposal_confidence(values: list[PlayerValue]) -> str:
    """Confidence in the VALUATIONS behind a proposal — distinct from
    acceptance_rating (confidence in whether they'd say yes). Rolls up
    is_corroborated/cross_source_agreement/thin_market/panel_disagreement,
    which were already computed per-player but previously only ever
    surfaced as free-text caveat prose, not a structured, sortable field.
    Takes the WORST confidence among all players in play — one shaky
    valuation is enough to make the whole proposal less trustworthy.
    """
    if not values:
        return "Medium"
    return min((player_confidence(pv) for pv in values), key=lambda c: _CONFIDENCE_RANK[c])
