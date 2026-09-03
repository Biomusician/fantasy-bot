"""Which value signal a league's trades are measured in, and the handful of
primitives that read it off a `PlayerValue`.

Split out of `trade_engine.py` because almost every decision module needs
"what is this player worth in this league's currency" without needing any
of the proposal-generation machinery — importing the 1500-line trade engine
to get a two-line accessor was the single biggest source of incidental
coupling in the package (and put `lineup_optimizer` downstream of the trade
engine, which is backwards).

Two currencies, since dynasty trade value is close to meaningless for a
league you're not keeping players in:

- Dynasty leagues: KTC dynasty value (`PlayerValue.dynasty_value`), the
  long-horizon market-value signal.
- Keeper and redraft leagues: RotoBaller's format-matched season point
  projection (`PlayerValue.proj_points`) — most of a keeper roster resets
  every year too, so treating it like dynasty would overweight long-term
  asset value that mostly doesn't carry over. A deliberate simplification.

This module sits at the bottom of the trade stack: it imports only
`roster_analysis`/`valuation` and must never import anything above it.
"""
from __future__ import annotations

from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.valuation import PlayerValue

DYNASTY_CURRENCY = "dynasty"
REDRAFT_CURRENCY = "redraft"

# A 10-12 team dynasty league rosters ~250-300 total players league-wide out
# of KTC's ~500-player pool. The old threshold (20.0, ~rank 401/500) let the
# engine treat deep-bench/practice-squad-caliber players — already flagged
# noisy by THIN_MARKET_RANK_THRESHOLD=150 in valuation.py — as legitimate
# buy-low targets. 45.0 (~rank 275/500) keeps eligibility inside the range
# a real roster in this size league would actually reach, and no longer
# contradicts the thin-market cutoff by reaching 2.5x deeper into it.
MIN_ROSTERABLE_PERCENTILE = 45.0


def value_currency(roster: ValuedRoster) -> str:
    """Which value signal this league's trades should be evaluated on."""
    return DYNASTY_CURRENCY if roster.league.kind == "dynasty" else REDRAFT_CURRENCY


def value_for_currency(pv: PlayerValue, currency: str) -> float | None:
    return pv.dynasty_value if currency == DYNASTY_CURRENCY else pv.proj_points


def percentile_for_currency(pv: PlayerValue, currency: str) -> float | None:
    return pv.dynasty_value_percentile if currency == DYNASTY_CURRENCY else pv.redraft_ecr_percentile


def value_label_for_currency(currency: str) -> str:
    return "dynasty value" if currency == DYNASTY_CURRENCY else "projected season points"


def corroborated(entry: RosterEntry, currency: str) -> bool:
    """>=2 independent ranking sources agreeing, AND a usable number in this
    league's currency. Every trade-side function filters on this because a
    single-source valuation is more likely to be a name-matching artifact
    than a real signal.
    """
    return entry.value.is_corroborated and value_for_currency(entry.value, currency) is not None


def need_percentile(pv, currency: str) -> float | None:
    """Percentile used specifically for cross-position need comparison.

    Comparing positions by overall-pool percentile is an apples-to-oranges
    mistake — "70th percentile among all dynasty assets" means something
    very different at a shallow position (TE) than a deep one (RB/WR), so a
    team can look TE-needy or RB-loaded purely as an artifact of pool size,
    not real scarcity. For dynasty currency we use KTC's WITHIN-POSITION
    percentile instead. Redraft currency doesn't have a positional
    percentile plumbed through yet, so it falls back to the overall
    percentile — a known, smaller-impact limitation (redraft leagues here
    are 1 keeper league + several not-yet-drafted redraft leagues).
    """
    if currency == DYNASTY_CURRENCY and pv.dynasty_positional_percentile is not None:
        return pv.dynasty_positional_percentile
    return percentile_for_currency(pv, currency)
