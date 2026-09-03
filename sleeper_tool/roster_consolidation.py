"""Roster Consolidation — 2-for-1 trades for teams that should be
tightening the roster: contenders, and middling teams strong enough
(STRONG_MIDDLING_MIN_PERCENTILE) that a better starter beats more depth.

A consolidation is two of my players for one of theirs where:
  - both outgoing pieces are non-starters in my optimized lineup, or one
    is a starter whose slot the incoming player refills;
  - the incoming player enters my optimized lineup;
  - the lineup improves by MIN_WEEKLY_IMPROVEMENT projected points/week;
  - the freed roster slot has non-negative utility: no new unfilled slot
    and no new depth need;
  - value is matched with the engine's own numbers, inside
    [VALUE_RATIO_MIN, VALUE_RATIO_MAX] (a 2-for-1 premium is expected);
  - at least one outgoing piece plausibly helps the counterparty
    (trade_engine._recipient_need_fit).
Acceptance is bucketed by the engine's own rate_acceptance with the same
OpponentFit shape the trade engine builds. If the trade would leave a
starter Fragile (contender_insurance), the proposal says so. At most
MAX_PER_TEAM per league, distinct counterparties, no piece reused, never
3-for-1.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from sleeper_tool.config import LeagueInfo
from sleeper_tool.contender_insurance import identify_fragile_starters
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, optimize_lineup_after_moves, roster_after_moves
from sleeper_tool.owner_profiles import get_owner_profile
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.team_status import CONTENDER, MIDDLING, TeamStatusResult
from sleeper_tool.trade_engine import (
    UNTOUCHABLE_COUNT,
    OpponentFit,
    TradeProposal,
    _corroborated,
    _recipient_need_fit,
    _status_fit,
    _untouchable_ids,
    generate_trade_message,
    identify_depth_needs,
    proposal_confidence,
    rate_acceptance,
    value_currency,
    value_for_currency,
)
from sleeper_tool.valuation import games_remaining

MIN_WEEKLY_IMPROVEMENT = 3.0
MAX_PER_TEAM = 2
STRONG_MIDDLING_MIN_PERCENTILE = 55.0
VALUE_RATIO_MIN = 0.90  # (what I give) / (what I get)
VALUE_RATIO_MAX = 1.35
MAX_MY_PIECES = 12  # my cheapest-to-move pieces considered, by value
MAX_TARGETS_PER_ROSTER = 4
TRADE_TYPE = "consolidation"


@dataclass
class Consolidation:
    proposal: TradeProposal
    weekly_gain: float
    freed_slot_note: str
    fragility_note: str | None
    lineup_after: LineupResult

    def describe(self) -> str:
        give = " + ".join(e.name for e in self.proposal.give)
        return f"{give} for {self.proposal.receive[0].name} ({self.proposal.target_team_name or self.proposal.target_username}): {self.weekly_gain:+.1f}/wk"


def eligible(status: TeamStatusResult | None) -> bool:
    if status is None:
        return False
    return status.status == CONTENDER or (status.status == MIDDLING and status.strength_percentile >= STRONG_MIDDLING_MIN_PERCENTILE)


def _outgoing_note(a: RosterEntry, b: RosterEntry, my_starters: set[str], incoming: RosterEntry) -> str:
    starters = [e.name for e in (a, b) if e.player_id in my_starters]
    if not starters:
        return f"{a.name} and {b.name} are not costing you starting production"
    bench = [e.name for e in (a, b) if e.player_id not in my_starters]
    return f"{starters[0]} starts today but {incoming.name} refills that slot; {bench[0] if bench else 'the other piece'} is not costing you starting production"


def _weekly(entry: RosterEntry, per_week: int) -> float:
    return (entry.value.proj_points or 0.0) / per_week


def _fragile_ids(roster: ValuedRoster, free_agents: list[RosterEntry], status: str, lineup: LineupResult) -> set[str]:
    return {r.starter.player_id for r in identify_fragile_starters(roster, free_agents, team_status=status, lineup=lineup)}


def find_consolidations(
    league: LeagueInfo,
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    *,
    status_result: TeamStatusResult | None,
    status_of: dict[int, str],
    lineup: LineupResult | None,
    free_agents: list[RosterEntry],
    current_week: int | None,
    exclude_ids: frozenset[str] = frozenset(),
) -> list[Consolidation]:
    if lineup is None or not eligible(status_result):
        return []
    currency = value_currency(my_roster)
    per_week = games_remaining(current_week)
    my_starters = set(lineup.starter_ids)
    untouchable = _untouchable_ids(my_roster, currency, UNTOUCHABLE_COUNT)
    pool = [
        e for e in my_roster.entries
        if _corroborated(e, currency) and e.player_id not in untouchable and e.player_id not in exclude_ids
        and not e.is_taxi and not e.is_reserve and e.value.proj_points is not None
    ]
    non_starters = sorted((e for e in pool if e.player_id not in my_starters), key=lambda e: -(value_for_currency(e.value, currency) or 0))[:MAX_MY_PIECES]
    starters = [e for e in pool if e.player_id in my_starters]
    pairs = list(combinations(non_starters, 2)) + [(s, n) for s in starters for n in non_starters]
    if not pairs:
        return []
    weakest_weekly = min((a.projection for a in lineup.assignments), default=0.0) / per_week
    base_points = lineup.total_projected_points
    depth_before = set(identify_depth_needs(my_roster, my_roster.fmt.starter_slots or None))
    skill_fas = [fa for fa in free_agents if fa.value.proj_points is not None]
    fragile_before = _fragile_ids(my_roster, skill_fas, status_result.status, lineup)

    # Every viable combo per counterparty, best first, so a piece collision
    # between two counterparties' favourite combos can fall back to the
    # next-best combo instead of dropping the counterparty.
    per_roster: dict[int, list[tuple]] = {}
    for rid, their in rosters.items():
        if rid == my_roster.roster_id or not their.entries:
            continue
        their_untouchable = _untouchable_ids(their, currency, UNTOUCHABLE_COUNT)
        targets = sorted(
            (
                e for e in their.entries
                if _corroborated(e, currency) and e.player_id not in their_untouchable and not e.is_reserve and not e.is_taxi
                and e.value.proj_points is not None and _weekly(e, per_week) >= weakest_weekly + MIN_WEEKLY_IMPROVEMENT
            ),
            key=lambda e: (-(e.value.proj_points or 0), e.name),
        )[:MAX_TARGETS_PER_ROSTER]
        combos: list[tuple] = []
        for t in targets:
            tv = value_for_currency(t.value, currency) or 0
            if tv <= 0:
                continue
            # Removing a non-starter can't change the optimum, so the
            # post-trade lineup depends only on the target and which
            # starter (if any) leaves: one optimizer call per such key.
            after_by_key: dict[str | None, LineupResult] = {}
            for a, b in pairs:
                gv = (value_for_currency(a.value, currency) or 0) + (value_for_currency(b.value, currency) or 0)
                ratio = gv / tv
                if not VALUE_RATIO_MIN <= ratio <= VALUE_RATIO_MAX:
                    continue
                starter_out = a.player_id if a.player_id in my_starters else (b.player_id if b.player_id in my_starters else None)
                if starter_out not in after_by_key:
                    after_by_key[starter_out] = optimize_lineup_after_moves(
                        my_roster, add_entries=[t], remove_player_ids=[a.player_id, b.player_id]
                    )
                after = after_by_key[starter_out]
                if t.player_id not in after.starter_ids or len(after.unfilled_slots) > len(lineup.unfilled_slots):
                    continue
                gain = round((after.total_projected_points - base_points) / per_week, 1)
                if gain < MIN_WEEKLY_IMPROVEMENT:
                    continue
                fits_any, fits_all, notes = _recipient_need_fit(their, [a, b], currency, exclude_player_id=t.player_id)
                if not fits_any:
                    continue
                combos.append((gain, -abs(ratio - 1.0), t, (a, b), after, ratio, fits_all, notes))
        if combos:
            combos.sort(key=lambda c: (-c[0], -c[1], c[2].name, c[3][0].name, c[3][1].name))
            per_roster[rid] = combos

    ordered = sorted(per_roster.items(), key=lambda kv: (-kv[1][0][0], -kv[1][0][1], rosters[kv[0]].owner_username or ""))
    out: list[Consolidation] = []
    used: set[str] = set()
    for rid, combos in ordered:
        if len(out) >= MAX_PER_TEAM:
            break
        their = rosters[rid]
        pick = next((c for c in combos if c[3][0].player_id not in used and c[3][1].player_id not in used), None)
        if pick is None:
            continue
        gain, _, t, (a, b), after, ratio, fits_all, notes = pick
        after_roster = roster_after_moves(my_roster, add_entries=[t], remove_player_ids=[a.player_id, b.player_id])
        depth_after = set(identify_depth_needs(after_roster, after_roster.fmt.starter_slots or None))
        if depth_after - depth_before:
            continue  # the freed slot would cost me a depth need: negative utility
        their_status = status_of.get(rid, MIDDLING)
        their_lineup = optimize_lineup(their)
        fit = OpponentFit(
            target_is_starter=t.player_id in their_lineup.starter_ids,
            would_upgrade_their_roster=fits_all,
            fit_notes=notes,
            opponent_status=their_status,
            status_fit=_status_fit([a, b], [], their_status),
            piece_count=2,
        )
        profile = get_owner_profile(their.owner_username or "", league.name)
        rating, reasons = rate_acceptance(fit, ratio, profile, give=[a, b])
        new_fragile = _fragile_ids(after_roster, skill_fas, status_result.status, after) - fragile_before
        by_id = {e.player_id: e for e in after_roster.entries}
        fragility = (
            "creates a Fragile starter: " + ", ".join(sorted(by_id[pid].name for pid in new_fragile if pid in by_id))
            if new_fragile else None
        )
        freed = "frees one roster spot with no new depth need"
        caveats = [f"{n} — this piece may read as a throw-in rather than real value to them." for n in notes]
        caveats.append("Two pieces for one — some managers read fragmented value as a lowball; lead with the better piece.")
        if fragility:
            caveats.append(f"{fragility} — the depth you send is the depth behind him.")
        proposal = TradeProposal(
            league_name=league.name, currency=currency,
            target_username=their.owner_username or "unknown", target_team_name=their.team_name,
            give=[a, b], receive=[t],
            my_value_total=(value_for_currency(a.value, currency) or 0) + (value_for_currency(b.value, currency) or 0),
            their_value_total=value_for_currency(t.value, currency) or 0,
            rationale_for_me=[
                f"{t.name} enters your optimized lineup: {gain:+.1f} projected points per week over the current best lineup.",
                _outgoing_note(a, b, my_starters, t) + f"; {freed}.",
            ],
            rationale_for_them=[f"Two rosterable pieces for one — depth for a {their_status} roster."],
            caveats=caveats, trade_type=TRADE_TYPE, acceptance_rating=rating, acceptance_reasons=reasons,
            confidence=proposal_confidence([t.value, a.value, b.value]),
        )
        proposal.message = generate_trade_message(proposal, fit)
        out.append(Consolidation(proposal, gain, freed, fragility, after))
        used.update({a.player_id, b.player_id})
    return out
