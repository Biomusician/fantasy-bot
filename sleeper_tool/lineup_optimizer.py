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
from functools import lru_cache

from sleeper_tool.asset_value import value_currency
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
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


@lru_cache(maxsize=64)
def _slot_tables(slots: tuple[str, ...]) -> tuple[tuple[frozenset[str], ...], tuple[int, ...], dict[str, tuple[int, ...]]]:
    """Everything about a league's starter slots that depends on the slot
    list alone, built once per distinct shape and reused by every call.

    - `eligibility[i]`  positions that may fill slot i.
    - `cost[mask]`      the tie-break's restrictiveness sum (total
      eligibility width of the filled slots) for every mask at once. It
      used to be re-derived per mask inside the final `max()`, which cost
      more than the DP transition that produced the mask.
    - `by_position[p]`  slots p can fill, most restrictive first then slot
      order — the DP's candidate list, and first-found-wins on ties, so
      this ordering is part of the tie-break.

    A league is capped at MAX_STARTER_SLOTS slots, so the cost table is at
    worst the same order of magnitude as the DP's own state space.
    """
    eligibility = tuple(slot_eligibility(s) for s in slots)
    sizes = tuple(len(e) for e in eligibility)
    cost = [0] * (1 << len(slots))
    for m in range(1, len(cost)):
        low = m & -m  # lowest set bit; the rest of the mask is already solved
        cost[m] = cost[m ^ low] + sizes[low.bit_length() - 1]
    by_position: dict[str, tuple[int, ...]] = {}
    for pos in {p for elig in eligibility for p in elig}:
        by_position[pos] = tuple(sorted((i for i, elig in enumerate(eligibility) if pos in elig), key=lambda i: (sizes[i], i)))
    return eligibility, tuple(cost), by_position


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


# A report run asks for the same lineup more than once — several decision
# modules independently want "this roster, this week, nobody excluded" —
# and the DP is expensive enough that recognising a repeat is worth the key.
# Process-local and bounded; cleared wholesale rather than evicted because
# a run's working set is far under the limit and LRU bookkeeping would cost
# more than the rare full rebuild.
_MEMO_LIMIT = 4096
_lineup_memo: dict[tuple, LineupResult] = {}


def _memo_entry_key(entry: RosterEntry) -> tuple:
    """Every field of a roster entry that can change the lineup: what makes
    a player unavailable, what orders him against the others (_ordering_key
    via composite_overall_rank), and what lands in SlotAssignment. A new
    input to any of those three MUST be added here or the memo will serve a
    stale lineup; nothing else about the entry is read by optimize_lineup.
    """
    v = entry.value
    return (
        entry.player_id, entry.name, entry.position,
        entry.is_reserve, entry.is_taxi, entry.injury_status, entry.status,
        v.proj_points, v.bye_week,
        v.dynasty_rank, v.dynasty_ecr_rank, v.redraft_ecr_rank,
    )


def _detached(result: LineupResult) -> LineupResult:
    """A copy that shares only the frozen SlotAssignments, so a caller can
    never mutate a memoized result out from under the next caller."""
    return replace(
        result,
        assignments=list(result.assignments),
        unfilled_slots=list(result.unfilled_slots),
        bench_player_ids=list(result.bench_player_ids),
        unavailable=dict(result.unavailable),
    )


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
    _eligibility, mask_cost, slots_by_position = _slot_tables(tuple(slots))
    currency = value_currency(roster)
    excluded = frozenset(excluded_player_ids)

    # starter_slots_for above still runs on a memo hit, so a league with an
    # unfillable slot list raises exactly as it always did.
    memo_key = (
        tuple(slots), currency, nfl_week, exclude_game_day_out, excluded,
        tuple(_memo_entry_key(e) for e in roster.entries),
    )
    memoized = _lineup_memo.get(memo_key)
    if memoized is not None:
        return _detached(memoized)

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

    # dp: filled-slot bitmask -> (total projection, index into `nodes`).
    # `nodes` is an append-only back-pointer chain: node -> (parent node,
    # slot_index, player_id), node 0 being the empty lineup. Each state
    # remembers only the one slot it just filled, so a transition is O(1)
    # instead of copying and extending the whole assignment tuple; the
    # winning chain is walked once, at the end. Nodes are never rewritten,
    # so a chain always reconstructs the assignment that produced the total
    # recorded with it, even after a later player improves its parent mask.
    nodes: list[tuple[int, int, str]] = [(-1, -1, "")]
    dp: dict[int, tuple[float, int]] = {0: (0.0, 0)}
    by_id = {e.player_id: e for e in available}
    for entry in available:
        # Most restrictive slot first, then slot order (see _slot_tables);
        # a position no slot accepts — including None — fills nothing.
        idxs = slots_by_position.get(entry.position, ())
        if not idxs:
            continue
        weight = projection_of(entry)
        pid = entry.player_id
        # Sources are the states as they stood BEFORE this player (the
        # snapshot), so he can't be placed twice; writes land in `dp`
        # itself. That is exactly what the old `new_dp = dict(dp)` did —
        # "bench him" carries every state forward, this player's earlier
        # writes are visible to his later ones — including the key
        # insertion order the mask tie-break falls back on.
        for mask, (total, node) in list(dp.items()):
            candidate_total = total + weight
            for j in idxs:
                bit = 1 << j
                if mask & bit:
                    continue
                next_mask = mask | bit
                current = dp.get(next_mask)
                # Strictly better only: an equal total keeps the earlier
                # (better-ordered) assignment, which is the tie-break.
                if current is None or candidate_total > current[0] + _EPS:
                    nodes.append((node, j, pid))
                    dp[next_mask] = (candidate_total, len(nodes) - 1)

    # Highest total; on a tie, the lineup that fills more slots (a zero-
    # projection K still beats an empty K slot), then the one using the
    # more restrictive slots (a lone RB sits in RB, not FLEX, leaving the
    # flexible slot as the reported hole), then the lowest mask.
    def _mask_key(m: int) -> tuple:
        return (round(dp[m][0], 6), m.bit_count(), -mask_cost[m], -m)

    best_mask = max(dp, key=_mask_key)
    total, node = dp[best_mask]
    assignment: list[tuple[int, str]] = []
    while node > 0:  # node 0 is the empty lineup
        node, j, pid = nodes[node]
        assignment.append((j, pid))

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
    result = LineupResult(
        assignments=assignments,
        total_projected_points=total,
        unfilled_slots=[s for i, s in enumerate(slots) if not best_mask & (1 << i)],
        bench_player_ids=[e.player_id for e in available if e.player_id not in started],
        unavailable=unavailable,
        nfl_week=nfl_week,
    )
    if len(_lineup_memo) >= _MEMO_LIMIT:
        _lineup_memo.clear()
    _lineup_memo[memo_key] = result
    return _detached(result)


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
