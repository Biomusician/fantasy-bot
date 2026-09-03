"""Which players on a roster are actually available to trade, and which are
cornerstones nobody would move.

Split out of `trade_engine.py`: the trade engine, the buyer board, the
negotiation ladder and the consolidation search all need the same
"untouchable / tradeable" partition, and three of the four were reaching
into the engine's private helpers to get it.
"""
from __future__ import annotations

from sleeper_tool.asset_value import (
    DYNASTY_CURRENCY,
    MIN_ROSTERABLE_PERCENTILE,
    corroborated,
    need_percentile,
    percentile_for_currency,
    value_currency,
    value_for_currency,
)
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD, veteran_min_age

UNTOUCHABLE_COUNT = 2  # skip each roster's top N players by value as trade targets/gives
# A corroborated player who is their position's clear best asset (top decile
# within-position) is treated as untouchable even outside the top-2-overall-
# value cut — protects a scarce position's only real starter (e.g. a
# league's one rosterable TE) that raw cross-position dollar value would
# otherwise miss, since TE dollar values run far lower than RB/WR.
SCARCE_POSITION_PROTECTION_PERCENTILE = 90.0
POSITION_ORDER = ("QB", "RB", "WR", "TE")  # fixed order so need-ranking ties break deterministically


def untouchable_ids(roster: ValuedRoster, currency: str, exclude_top: int) -> set[str]:
    """The top-`exclude_top` players a roster is treated as unwilling to
    move. Ranked among STARTERS first, falling back to all corroborated
    entries only if there aren't enough starters to fill the cut — a
    redundant, non-starting backup (e.g. a QB2 sitting behind a more
    valuable QB1) must never be "protected" purely by raw dollar value,
    since losing a bench piece costs a roster nothing competitively; only
    an active starter is genuinely hard to pry loose.

    Ranked by PERCENTILE, not raw value — for REDRAFT_CURRENCY, raw value
    is RotoBaller's un-normalized proj_points, which runs structurally
    higher for QBs than RB/WR/TE (see identify_buy_low's sort for the same
    fix). Ranking the "top-2 cornerstone" cut by raw value would let a
    true elite starting WR/RB get out-ranked by a merely-good QB's
    inflated point total and slip out of protection — confirmed to
    reproduce in practice before this fix.

    Also protects each position's clear best corroborated asset when it's
    a scarcity outlier (SCARCE_POSITION_PROTECTION_PERCENTILE), using the
    within-position percentile for dynasty (where it's available) or the
    pool-wide percentile for redraft (the closest available proxy) — a
    league's only real starting TE shouldn't be targetable just because TE
    dollar values run low relative to RB/WR. And protects any starter
    whose loss would drop the roster below the league's own required
    starter count at that position (LeagueFormat.starter_slots) — losing a
    lineup-critical starter (e.g. a Superflex league's only good QB2) is a
    real cost even when that player isn't a raw top-2/percentile outlier.
    """
    corroborated_entries = [e for e in roster.entries if corroborated(e, currency)]
    starters = [e for e in corroborated_entries if e.is_starter]
    rank_pool = starters if len(starters) >= exclude_top else corroborated_entries
    ids = {
        e.player_id
        for e in sorted(rank_pool, key=lambda e: -(percentile_for_currency(e.value, currency) or 0))[:exclude_top]
    }
    for pos in POSITION_ORDER:
        pos_entries = [e for e in corroborated_entries if e.position == pos]
        if not pos_entries:
            continue
        if currency == DYNASTY_CURRENCY:
            best = max(pos_entries, key=lambda e: e.value.dynasty_positional_percentile or 0)
            best_pctl = best.value.dynasty_positional_percentile or 0
        else:
            best = max(pos_entries, key=lambda e: percentile_for_currency(e.value, currency) or 0)
            best_pctl = percentile_for_currency(best.value, currency) or 0
        if best_pctl >= SCARCE_POSITION_PROTECTION_PERCENTILE:
            ids.add(best.player_id)

        required = roster.fmt.starter_slots.get(pos)
        if required:
            rosterable = sorted(
                (e for e in pos_entries if (percentile_for_currency(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE),
                key=lambda e: -(percentile_for_currency(e.value, currency) or 0),
            )
            # Protect exactly the top `required` at this position -- losing
            # any of them would drop the roster below what the league's own
            # starting lineup needs there.
            ids.update(e.player_id for e in rosterable[: int(required)])
    return ids


def tradeable_pool(
    roster: ValuedRoster, my_status: str = CONTENDER, exclude_top: int = UNTOUCHABLE_COUNT
) -> list[RosterEntry]:
    """My own tradeable assets (the 'give' side). Untouchables (my top N by
    value) are never on the table. For a middling/rebuild team, aging
    veterans are sorted first so value-matching prefers shipping them out
    over young roster pieces when there's a choice — the goal is shedding
    win-now veterans for future value, not the reverse.
    """
    currency = value_currency(roster)
    protected = untouchable_ids(roster, currency, exclude_top)
    pool = [e for e in roster.entries if corroborated(e, currency) and e.player_id not in protected]

    if my_status in (MIDDLING, REBUILD):
        return sorted(
            pool,
            key=lambda e: (
                not (e.age is not None and e.age >= veteran_min_age(e.position)),
                -(value_for_currency(e.value, currency) or 0),
            ),
        )
    return sorted(pool, key=lambda e: -(value_for_currency(e.value, currency) or 0))


def position_rosterable_count(roster: ValuedRoster, position: str, currency: str) -> int:
    return sum(
        1
        for e in roster.by_position(position)
        if corroborated(e, currency) and (need_percentile(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE
    )
