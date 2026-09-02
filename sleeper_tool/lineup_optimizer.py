"""The single owner of "what is the best legal starting lineup for this
roster?" — shared by lineup leverage, move-impact previews, bye-week
look-ahead, and contender-insurance checks so all four agree on exactly
what "legal lineup" and "available player" mean. Nothing else in the
codebase should construct a lineup on its own.

Exact, not greedy. Greedy slot-filling (best player into the most
restrictive open slot) is optimal for the common QB/RB/WR/TE/FLEX/SUPER_FLEX
shape, but Sleeper also offers partially-overlapping flex types (WRRB_FLEX,
REC_FLEX) where greedy provably leaves points on the bench — see the test
suite for a 3-player case. The optimizer is a dynamic program over a bitmask
of filled starter slots: at most 2^(slot count) states per player, which is
~1k states for a 10-slot lineup and still trivial for anything Sleeper
actually configures. It is not a general assignment solver and does not
need one.

Availability rules (applied before optimizing, and reported per player in
LineupResult.unavailable so consumers can explain a hole):
  - In an IR/reserve or taxi slot: cannot start under Sleeper's own rules.
  - injury_status IR / PUP / Sus, or a Sleeper `status` that means he
    isn't on an active NFL roster (Injured Reserve, NFI, PUP, Inactive):
    out for the foreseeable future, excluded from every lineup.
  - injury_status Out is a THIS-WEEK game-day tag: excluded only when the
    caller passes exclude_game_day_out=True (a this-week lineup). The
    structural lineup that bye planning, insurance, clog detection and
    leverage build on ignores it — a one-week absence shouldn't rewrite
    the roster's shape for the next month. Questionable/Doubtful are
    always included; consumers may annotate the risk, this module doesn't.
  - On bye in the evaluated week (`nfl_week`). nfl_week=None means "don't
    apply bye exclusions" — the other rules still apply.
A player with no projection is treated as 0.0, not benched: a legally
required slot (K/DEF have no projection source here) is better filled with
an unprojected body than left empty, and any projected player still beats
him for the slot.

Projection units are whatever PlayerValue.proj_points carries — a rest-of-
season total. Ratios between lineups are unit-free; a consumer that needs
per-week points converts with valuation.weekly_projection.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace

from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.trade_engine import value_currency
from sleeper_tool.valuation import composite_overall_rank

NON_STARTER_SLOTS = frozenset({"BN", "IR", "TAXI"})
FLEX_ELIGIBILITY: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "IDP_FLEX": frozenset({"DL", "LB", "DB"}),
}
DEDICATED_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB"})
# Anything past this is 2^19+ DP states per player — no real Sleeper league
# gets near it (a 9-slot offense + 7 IDP starters is 16), so a larger count
# almost certainly means a malformed roster_positions payload.
MAX_STARTER_SLOTS = 18

# Sleeper's real vocabularies, checked against cached player data
# (2026-08-20): injury_status is one of {COV, DNR, Doubtful, IR, NA, Out,
# PUP, Questionable, Sus}; `status` is one of {Active, Inactive, Injured
# Reserve, Non Football Injury, Physically Unable to Perform, Practice
# Squad}. The LONG_TERM sets are the season/multi-week designations (shared
# with waiver_engine's "move him to IR" alert); the optimizer additionally
# treats Inactive (holdout/unsigned, still attached to a team) as unable to
# start, and Out as a this-week exclusion only.
LONG_TERM_INJURY_STATUSES = frozenset({"IR", "PUP", "Sus"})
LONG_TERM_SLEEPER_STATUSES = frozenset({"Injured Reserve", "Non Football Injury", "Physically Unable to Perform"})
UNAVAILABLE_SLEEPER_STATUSES = LONG_TERM_SLEEPER_STATUSES | {"Inactive"}
GAME_DAY_OUT = "Out"
_EPS = 1e-9  # float-noise guard when comparing lineup totals for ties


class UnsupportedSlotError(ValueError):
    """A roster_positions entry this module doesn't know how to fill.
    Raised rather than guessed at: silently treating an unknown slot as a
    FLEX (or dropping it) would produce a confidently wrong lineup for
    every roster in that league.
    """


@dataclass(frozen=True)
class SlotAssignment:
    slot: str
    slot_index: int  # position within the league's starter slot list
    player_id: str
    name: str
    position: str | None
    projection: float  # 0.0 when no projection was available


@dataclass
class LineupResult:
    assignments: list[SlotAssignment]
    total_projected_points: float
    unfilled_slots: list[str]
    bench_player_ids: list[str]  # available but not started
    unavailable: dict[str, str]  # player_id -> reason he couldn't be started
    nfl_week: int | None = None  # the week this lineup models; None = structural (no bye exclusions)

    @property
    def slot_by_player(self) -> dict[str, str]:
        return {a.player_id: a.slot for a in self.assignments}

    @property
    def starter_ids(self) -> frozenset[str]:
        return frozenset(a.player_id for a in self.assignments)

    def assignment_for(self, player_id: str) -> SlotAssignment | None:
        return next((a for a in self.assignments if a.player_id == player_id), None)


def slot_eligibility(slot: str) -> frozenset[str]:
    if slot in FLEX_ELIGIBILITY:
        return FLEX_ELIGIBILITY[slot]
    if slot in DEDICATED_POSITIONS:
        return frozenset({slot})
    raise UnsupportedSlotError(f"Don't know which positions can fill roster slot {slot!r}")


def starter_slots_for(roster: ValuedRoster) -> list[str]:
    """The league's starting slots in Sleeper's order, validated. Raises
    UnsupportedSlotError for a slot type this module can't fill.
    """
    slots = [s for s in roster.fmt.roster_positions if s and s not in NON_STARTER_SLOTS]
    if not slots:
        # An empty/null roster_positions payload would otherwise produce a
        # zero-slot "best lineup" that every consumer then reports on as if
        # the roster were empty. Refuse loudly; report_data degrades.
        raise UnsupportedSlotError("league reports no starting slots (roster_positions empty or missing)")
    for s in slots:
        slot_eligibility(s)
    if len(slots) > MAX_STARTER_SLOTS:
        raise UnsupportedSlotError(f"{len(slots)} starter slots is more than this optimizer supports ({MAX_STARTER_SLOTS})")
    return slots


def unavailability_reason(entry: RosterEntry, nfl_week: int | None, *, exclude_game_day_out: bool = False) -> str | None:
    if entry.is_reserve:
        return "in an IR/reserve slot"
    if entry.is_taxi:
        return "on the taxi squad"
    if entry.injury_status in LONG_TERM_INJURY_STATUSES:
        return f"injury status {entry.injury_status}"
    if exclude_game_day_out and entry.injury_status == GAME_DAY_OUT:
        return "ruled out this week"
    if entry.status in UNAVAILABLE_SLEEPER_STATUSES:
        return f"roster status {entry.status}"
    if nfl_week is not None and entry.value.bye_week == nfl_week:
        return f"on bye week {nfl_week}"
    return None


def projection_of(entry: RosterEntry) -> float:
    return float(entry.value.proj_points) if entry.value.proj_points is not None else 0.0


def _ordering_key(entry: RosterEntry, currency: str) -> tuple:
    # Deterministic processing order — on equal lineup totals the DP keeps
    # the first assignment it found, so this order IS the tie-break:
    # higher projection, then better reconciled overall rank, then
    # player_id, so two runs over the same data always pick the same
    # lineup and the better-ranked of two equally-projected players starts.
    rank = composite_overall_rank(entry.value, currency)
    return (-projection_of(entry), rank if rank is not None else float("inf"), entry.player_id)


def optimize_lineup(
    roster: ValuedRoster,
    *,
    nfl_week: int | None = None,
    excluded_player_ids: Collection[str] = (),
    exclude_game_day_out: bool = False,
) -> LineupResult:
    """Best legal lineup for `roster` under its league's real slot list.
    `excluded_player_ids` are hypothetical removals ("what if he were
    hurt?") — reported as unavailable with reason "excluded".
    `exclude_game_day_out` is for a THIS-WEEK lineup (see module docstring).
    """
    slots = starter_slots_for(roster)
    eligibility = [slot_eligibility(s) for s in slots]
    currency = value_currency(roster)
    excluded = set(excluded_player_ids)

    unavailable: dict[str, str] = {}
    available: list[RosterEntry] = []
    for entry in roster.entries:
        if entry.player_id in excluded:
            unavailable[entry.player_id] = "excluded"
            continue
        reason = unavailability_reason(entry, nfl_week, exclude_game_day_out=exclude_game_day_out)
        if reason is not None:
            unavailable[entry.player_id] = reason
            continue
        available.append(entry)
    available.sort(key=lambda e: _ordering_key(e, currency))

    def eligible_slot_indexes(position: str | None) -> list[int]:
        idxs = [i for i, elig in enumerate(eligibility) if position in elig]
        # Most restrictive slot first (dedicated before FLEX before
        # SUPER_FLEX), then slot order — first-found wins on ties.
        idxs.sort(key=lambda i: (len(eligibility[i]), i))
        return idxs

    # dp: filled-slot bitmask -> (total projection, ((slot_index, player_id), ...))
    dp: dict[int, tuple[float, tuple[tuple[int, str], ...]]] = {0: (0.0, ())}
    by_id = {e.player_id: e for e in available}
    for entry in available:
        idxs = eligible_slot_indexes(entry.position)
        if not idxs:
            continue
        weight = projection_of(entry)
        new_dp = dict(dp)  # "bench him" carries every existing state forward
        for mask, (total, assignment) in dp.items():
            for j in idxs:
                bit = 1 << j
                if mask & bit:
                    continue
                next_mask = mask | bit
                candidate_total = total + weight
                current = new_dp.get(next_mask)
                # Strictly better only: an equal total keeps the earlier
                # (better-ordered) assignment, which is the tie-break.
                if current is None or candidate_total > current[0] + _EPS:
                    new_dp[next_mask] = (candidate_total, assignment + ((j, entry.player_id),))
        dp = new_dp

    # Highest total; on a tie, the lineup that fills more slots (a zero-
    # projection K still beats an empty K slot), then the one using the
    # more restrictive slots (a lone RB sits in RB, not FLEX, leaving the
    # flexible slot as the reported hole), then the lowest mask.
    def _mask_key(m: int) -> tuple:
        filled = [i for i in range(len(slots)) if m & (1 << i)]
        restrictiveness_cost = sum(len(eligibility[i]) for i in filled)
        return (round(dp[m][0], 6), len(filled), -restrictiveness_cost, -m)

    best_mask = max(dp, key=_mask_key)
    total, assignment = dp[best_mask]

    assignments = sorted(
        (
            SlotAssignment(
                slot=slots[j],
                slot_index=j,
                player_id=pid,
                name=by_id[pid].name,
                position=by_id[pid].position,
                projection=projection_of(by_id[pid]),
            )
            for j, pid in assignment
        ),
        key=lambda a: a.slot_index,
    )
    started = {a.player_id for a in assignments}
    return LineupResult(
        assignments=assignments,
        total_projected_points=total,
        unfilled_slots=[s for i, s in enumerate(slots) if not best_mask & (1 << i)],
        bench_player_ids=[e.player_id for e in available if e.player_id not in started],
        unavailable=unavailable,
        nfl_week=nfl_week,
    )


def with_optimized_starters(roster: ValuedRoster, lineup: LineupResult | None = None) -> ValuedRoster:
    """A copy of `roster` whose is_starter flags follow the optimizer's
    lineup instead of whatever Sleeper has set. team_status ranks roster
    strength on is_starter, so comparing a hypothetical roster (whose
    incoming players have no set-lineup flag) against real ones needs
    every roster on the same footing."""
    lineup = lineup if lineup is not None else optimize_lineup(roster)
    starters = lineup.starter_ids
    return replace(roster, entries=[replace(e, is_starter=e.player_id in starters) for e in roster.entries])


def roster_after_moves(
    roster: ValuedRoster, *, add_entries: Iterable[RosterEntry] = (), remove_player_ids: Collection[str] = ()
) -> ValuedRoster:
    """A hypothetical roster after a trade or add/drop. The caller's roster
    is never mutated; added entries are flagged as bench so slot-based
    availability rules (IR/taxi) don't carry over from their old team.
    """
    removed = set(remove_player_ids)
    kept = [e for e in roster.entries if e.player_id not in removed]
    added = [replace(e, is_starter=False, is_taxi=False, is_reserve=False) for e in add_entries]
    return replace(roster, entries=kept + added)


def optimize_lineup_after_moves(
    roster: ValuedRoster,
    *,
    add_entries: Iterable[RosterEntry] = (),
    remove_player_ids: Collection[str] = (),
    nfl_week: int | None = None,
    excluded_player_ids: Collection[str] = (),
    exclude_game_day_out: bool = False,
) -> LineupResult:
    """The lineup after a hypothetical roster change. Builds the temporary
    roster (roster_after_moves) and delegates to optimize_lineup — there
    is deliberately no second optimization path.
    """
    hypothetical = roster_after_moves(roster, add_entries=add_entries, remove_player_ids=remove_player_ids)
    return optimize_lineup(
        hypothetical, nfl_week=nfl_week, excluded_player_ids=excluded_player_ids, exclude_game_day_out=exclude_game_day_out
    )
