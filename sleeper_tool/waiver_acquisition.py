"""Waiver Acquisition — "should I acquire this player, for THIS roster?"

The evidence matrix says what the analysts think of a player; this module
decides what he is worth to one specific roster, by simulating the move
with the shared lineup optimizer (never a second lineup model):

  1. What does he do to my lineup? Structural lineup (the season's shape)
     and the claim-week lineup (that week's byes and long-term injuries),
     each re-optimized with him added. The difference, per week.
  2. What kind of add is that?
       Immediate Starter          enters the structural lineup for at least
                                  STARTER_MIN_GAIN per week
       Injury/Bye Cover           helps the claim week by COVER_MIN_GAIN but
                                  not the season's lineup
       Streamer                   the same, at QB/TE, where the group has no
                                  claim-week hole
       Likely FLEX/Depth Upgrade  enters the lineup for less, or becomes a
                                  credible best cover at his position
       Upside Bench Add           no lineup path now, but a waiver board
                                  ranks him inside its first WAIVER_TOP, or
                                  FantasyPros ROS / Boone rank him inside
                                  this league's STARTABLE depth — a starter
                                  in this format who is somehow still free
       Speculative Stash          no lineup path: a board lists him deeper,
                                  or a ROS/weekly rank puts him inside
                                  rosterable depth only
  3. Who goes? The drop board's cheapest option that survives the
     opportunity-cost guardrail (below). No viable drop and no open spot
     makes the add a Pass, however good the player.
  4. How strongly? Priority Add / Strong Add / Depth Add / Speculative Add
     / Pass, from the class, the need of the group he fills (roster_needs)
     and — for adds with no lineup path — how much outside support he has.

Opportunity-cost guardrail (each rejection is recorded, never silent):
  - a drop who is real cover (waiver_drops.MATERIAL_COVER_POINTS) is only
    paired with an add whose own lineup gain is at least that cover, or who
    covers the same position at least as well
  - a ROS starter-calibre drop is only paired with a same-position add
    ranked better in FantasyPros ROS
  - an upside or speculative add only takes a dead roster spot (no cover,
    no starter calibre) — the explicit exception for stashes
  - a drop who would still start in either simulated lineup is never used

Kickers and defenses are deliberately out of scope: `candidate_universe`
evaluates QB/RB/WR/TE only. This module judges an add by what it does to
the lineup and by where the sources place him in this league's positional
depth, and neither input exists for K/DEF — no waiver board ranks them, no
FantasyPros ROS positional rank is loaded for them, and the replacement
market has no depth curve for a position whose weekly spread is mostly
noise. The K/DST decision is owned end to end by the streaming planner
(`streamer_planner.py`, which does cover K and DEF), so nothing here —
including a Pass — should be read as a depth judgement about them.

Nothing here averages sources or turns them into a score. The one ordering
(`order_key`) is lexicographic over the labels, then lineup gain. Role
labels stay context: the role heuristic is annotation-only until it is
redesigned (docs/DECISIONS.md, 2026-09-04).
"""
from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from sleeper_tool.lineup_optimizer import (
    LineupResult,
    SlotAssignment,
    optimize_lineup_after_moves,
    slot_eligibility,
    slot_label,
    starter_slots_for,
)
from sleeper_tool.replacement_value import ABUNDANT, NORMAL
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.roster_needs import CRITICAL_NEED, DEPTH_COVER_RATIO, NEED_ORDER, STRONG, SURPLUS, WEAK, PositionNeed, RosterNeeds, group_of_slot
from sleeper_tool.valuation import games_remaining
from sleeper_tool.waiver_alternatives import AlternativeDensity, alternative_density
from sleeper_tool.waiver_drops import MATERIAL_COVER_POINTS, DropBoard, DropOption
from sleeper_tool.waiver_evidence import (
    DEEPER_LEAGUES_ONLY,
    FANTASYPROS_HIGHER,
    SUPERFLEX_ONLY,
    SUPPORT_AGREE,
    SUPPORT_BROAD,
    SUPPORT_ORDER,
    SUPPORT_SINGLE,
    WAIVER_LISTED,
    WAIVER_TOP,
    WaiverEvidence,
)

# -- Classes ------------------------------------------------------------------
IMMEDIATE_STARTER = "Immediate Starter"
FLEX_DEPTH = "Likely FLEX/Depth Upgrade"
BYE_COVER = "Injury/Bye Cover"
STREAMER = "Streamer"
UPSIDE_BENCH = "Upside Bench Add"
SPECULATIVE_STASH = "Speculative Stash"
CLASS_ORDER = {IMMEDIATE_STARTER: 0, BYE_COVER: 1, STREAMER: 2, FLEX_DEPTH: 3, UPSIDE_BENCH: 4, SPECULATIVE_STASH: 5}
SPECULATIVE_CLASSES = (UPSIDE_BENCH, SPECULATIVE_STASH)

# -- Strength -----------------------------------------------------------------
PRIORITY_ADD = "Priority Add"
STRONG_ADD = "Strong Add"
DEPTH_ADD = "Depth Add"
SPECULATIVE_ADD = "Speculative Add"
PASS = "Pass"
STRENGTH_ORDER = {PRIORITY_ADD: 0, STRONG_ADD: 1, DEPTH_ADD: 2, SPECULATIVE_ADD: 3, PASS: 4}
_DEMOTE = {PRIORITY_ADD: STRONG_ADD, STRONG_ADD: DEPTH_ADD, DEPTH_ADD: SPECULATIVE_ADD, SPECULATIVE_ADD: PASS, PASS: PASS}

STARTER_MIN_GAIN = 1.0  # points/week in the structural lineup
MAJOR_GAIN = 3.0  # points/week: an upgrade this large is a Priority Add whatever the need label
COVER_MIN_GAIN = 1.0  # points/week in the claim-week lineup
STREAM_POSITIONS = ("QB", "TE")
DROP_TOLERANCE = 0.05  # float noise when comparing a paired move's gain with the add alone
MAX_CANDIDATES = 48  # evaluated per league; the universe is ordered so the cap trims the least relevant
PER_POSITION_BY_PROJECTION = 6  # best projected free agents per position always evaluated
NO_DROP_REASONS = (
    "no one on the bench is worth less to this roster than he would be",
    "every rostered player is protected (starters, IR/taxi, trade pieces)",
)


@dataclass
class AcquisitionCall:
    entry: RosterEntry
    evidence: WaiverEvidence
    cls: str | None  # None: no lineup path and no outside support — not a claim
    strength: str
    problem: str  # the roster problem he solves, e.g. "FLEX upgrade (over Quentin Johnston)"
    problem_positions: tuple[str, ...]
    need: PositionNeed | None
    structural_gain: float  # points/week
    week_gain: float  # points/week in the claim week
    entered_slot: str | None
    drop: DropOption | None = None
    needs_drop: bool = True  # False when an open roster spot takes him
    alternatives: AlternativeDensity | None = None
    why: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    pass_reason: str | None = None  # the one-sentence reason a Pass is a Pass
    rejected_drops: list[str] = field(default_factory=list)
    problem_key: str = ""  # claims sharing a key solve the same roster problem
    replaces: str | None = None  # the starter he pushes out of the lineup
    structural_after: LineupResult | None = field(default=None, repr=False)
    week_after: LineupResult | None = field(default=None, repr=False)

    @property
    def player_id(self) -> str:
        return self.entry.player_id

    @property
    def is_claim(self) -> bool:
        return self.strength != PASS

    @property
    def expert_backed(self) -> bool:
        return self.evidence.support in (SUPPORT_BROAD, SUPPORT_AGREE, SUPPORT_SINGLE)

    def order_key(self) -> tuple:
        need_rank = NEED_ORDER[self.need.label] if self.need is not None else len(NEED_ORDER)
        return (
            STRENGTH_ORDER[self.strength], need_rank, CLASS_ORDER.get(self.cls, 9),
            -round(self.week_gain + self.structural_gain, 2), SUPPORT_ORDER.get(self.evidence.support, 9),
            self.evidence.best_waiver_rank or 999, self.entry.name,
        )


@dataclass
class LeagueWaiverContext:
    """Everything about one league's roster that every candidate is judged
    against, built once per league by report_data."""
    roster: ValuedRoster
    lineup: LineupResult  # structural
    week_lineup: LineupResult | None  # the claim week's (byes, long-term injuries)
    needs: RosterNeeds
    drops: DropBoard
    free_agents: Collection[RosterEntry]  # projected, available, skill positions
    current_week: int | None
    claim_week: int | None
    ros_pos_rank: Callable[[RosterEntry], int | None] = lambda e: None

    @property
    def per_week(self) -> int:
        return games_remaining(self.current_week)


# -- Candidate universe ----------------------------------------------------------------


def candidate_universe(
    free_agents: Collection[RosterEntry],
    *,
    expert_rank: dict[str, int],
    trending_ids: Collection[str] = (),
    positions: Collection[str] = ("QB", "RB", "WR", "TE"),
    per_position: int = PER_POSITION_BY_PROJECTION,
    cap: int = MAX_CANDIDATES,
) -> list[RosterEntry]:
    """Who gets evaluated, most relevant first so the cap trims the least:
    every player a waiver board ranks inside WAIVER_TOP (a player a board
    names must never vanish because Sleeper isn't trending him), the best
    projected free agents at each position, the rest of the waiver boards'
    listed players (inside WAIVER_LISTED), Sleeper's trending adds, then
    anyone else a board names. `expert_rank` is each player's best rank on
    any waiver board; `free_agents` must already exclude rostered players.

    `positions` defaults to the four this module can actually judge. K and
    DEF are excluded on purpose (module docstring): there is no source rank
    or depth curve for them here, and streamer_planner owns that decision."""
    wanted = set(positions)
    pool = [fa for fa in free_agents if fa.position in wanted]
    by_id = {fa.player_id: fa for fa in pool}
    experts = sorted((pid for pid in expert_rank if pid in by_id), key=lambda pid: (expert_rank[pid], pid))
    ordered: list[RosterEntry] = [by_id[pid] for pid in experts if expert_rank[pid] <= WAIVER_TOP]
    for pos in positions:
        at = sorted((fa for fa in pool if fa.position == pos and fa.value.proj_points is not None),
                    key=lambda e: (-e.value.proj_points, e.name))
        ordered.extend(at[:per_position])
    ordered.extend(by_id[pid] for pid in experts if WAIVER_TOP < expert_rank[pid] <= WAIVER_LISTED)
    ordered.extend(by_id[pid] for pid in trending_ids if pid in by_id)
    ordered.extend(by_id[pid] for pid in experts if expert_rank[pid] > WAIVER_LISTED)
    seen: set[str] = set()
    out: list[RosterEntry] = []
    for fa in ordered:
        if fa.player_id in seen:
            continue
        seen.add(fa.player_id)
        out.append(fa)
    return out[:cap]


# -- Simulation -----------------------------------------------------------------------------------------


def _connected_floor(lineup: LineupResult, position: str | None) -> float | None:
    """The lowest projection among starters in any slot a player at
    `position` could reach by a chain of slot swaps. A player projecting at
    or below it cannot raise the lineup total (adding a vertex to a
    max-weight matching only helps by displacing someone reachable), so the
    optimizer run is skipped. None means "must simulate" (an open slot)."""
    if not position or lineup.unfilled_slots:
        return None
    reach = {position}
    changed = True
    while changed:
        changed = False
        for a in lineup.assignments:
            elig = slot_eligibility(a.slot)
            if elig & reach and not elig <= reach:
                reach |= elig
                changed = True
    floors = [a.projection for a in lineup.assignments if slot_eligibility(a.slot) & reach]
    return min(floors) if floors else None


def _gain(
    roster: ValuedRoster, base: LineupResult, entry: RosterEntry, *, nfl_week: int | None, remove: Collection[str] = ()
) -> tuple[float, LineupResult | None]:
    """Total projection change (season units) from adding `entry` (and
    removing `remove`), with the lineup that produced it — None when the
    shortcut proved there is nothing to gain."""
    proj = entry.value.proj_points or 0.0
    if not remove:
        floor = _connected_floor(base, entry.position)
        if floor is not None and proj <= floor:
            return 0.0, None
    after = optimize_lineup_after_moves(roster, add_entries=[entry], remove_player_ids=remove, nfl_week=nfl_week)
    return after.total_projected_points - base.total_projected_points, after


def _displaced(base: LineupResult | None, after: LineupResult | None) -> SlotAssignment | None:
    if base is None or after is None:
        return None
    gone = [a for a in base.assignments if a.player_id not in after.starter_ids]
    return min(gone, key=lambda a: (a.projection, a.name)) if gone else None


# -- Assessment -------------------------------------------------------------------------------------------------------


def _problem(cls: str, entry: RosterEntry, slot: str | None, displaced: SlotAssignment | None, claim_week: int | None) -> tuple[str, str, tuple[str, ...]]:
    """(key, label, eligible positions) of the roster problem an add solves.
    Two adds that would push the SAME starter out of the lineup solve the
    same problem whatever slot each lands in (an RB entering at RB can slide
    the RB2 into FLEX and bench the same WR a WR add would), so the
    displaced starter is the key."""
    pos = entry.position or "?"
    if cls == IMMEDIATE_STARTER and displaced is not None:
        label = "Superflex QB upgrade" if displaced.slot == "SUPER_FLEX" else f"{slot_label(displaced.slot)} upgrade"
        positions = tuple(sorted(slot_eligibility(displaced.slot) & {"QB", "RB", "WR", "TE"}))
        return f"upgrade:{displaced.player_id}", f"{label} (over {displaced.name})", positions or (pos,)
    if cls == IMMEDIATE_STARTER:
        return f"fill:{slot}", f"Fill {slot_label(slot)}", (pos,)
    if cls == BYE_COVER:
        group = group_of_slot(slot) or pos
        return f"cover:{claim_week}:{group}", (f"Week {claim_week} {group} cover" if claim_week else f"{group} cover"), (pos,)
    if cls == STREAMER:
        return f"stream:{pos}", f"{pos} streamer", (pos,)
    if cls == FLEX_DEPTH:
        return f"depth:{pos}", f"{pos} depth", (pos,)
    # Upside adds and deeper stashes at the SAME position are substitutes for
    # one another and share a key. Across positions they are not: a superflex
    # QB stash and a WR stash solve different problems, and keying them
    # together meant only one speculative add per league could ever clear —
    # backups in a group inherit the lead's drop. How many such groups a week
    # is worth is a plan decision (waiver_plan), not a key decision.
    return f"upside:{pos}", ("Upside bench add" if cls == UPSIDE_BENCH else "Speculative stash"), (pos,)


def _inside(rank: int | None, depth: int | None) -> bool:
    return rank is not None and depth is not None and rank <= depth


def _ros_rank(evidence: WaiverEvidence) -> tuple[str, int] | None:
    """The better of the two season/weekly positional ranks, and which source
    said it: (source, rank). These are never merged with a waiver board's
    rank — they answer a different question — and never averaged with each
    other; the pair is only compared with this league's own depth."""
    ranked = [
        (source, rank)
        for source, rank in (("FantasyPros ROS", evidence.fp_ros_pos_rank), ("Boone", evidence.boone_pos_rank))
        if rank is not None
    ]
    return min(ranked, key=lambda sr: (sr[1], sr[0])) if ranked else None


def _no_path_reason(evidence: WaiverEvidence) -> str:
    """Why an add with no lineup path and no support is a Pass, in the terms
    the sources actually used. "No waiver board recommends him" is simply
    false about a player a board ranks #38, or one FantasyPros has as the
    WR60 — those are real facts that happen to fall short here."""
    ros = _ros_rank(evidence)
    if ros is not None:
        source, rank = ros
        return f"no path onto this lineup, and {source} {evidence.pos_label(rank)} is depth here rather than an upgrade"
    if evidence.best_waiver_rank is not None:
        return (
            f"no path onto this lineup, and no waiver board has him inside its first {WAIVER_LISTED} "
            f"(best #{evidence.best_waiver_rank})"
        )
    return "no path onto this lineup and no waiver board recommends him"


def assess_candidate(entry: RosterEntry, evidence: WaiverEvidence, ctx: LeagueWaiverContext, *, open_spot_available: bool = False) -> AcquisitionCall:
    per_week = ctx.per_week
    roster, needs = ctx.roster, ctx.needs
    s_raw, s_after = _gain(roster, ctx.lineup, entry, nfl_week=None)
    structural_gain = s_raw / per_week
    if ctx.week_lineup is not None:
        w_raw, w_after = _gain(roster, ctx.week_lineup, entry, nfl_week=ctx.claim_week)
        week_gain = w_raw / per_week
    else:
        w_after, week_gain = None, structural_gain
    entered_slot = s_after.slot_by_player.get(entry.player_id) if s_after is not None else None
    week_slot = w_after.slot_by_player.get(entry.player_id) if w_after is not None else None
    pos = entry.position

    # -- class --------------------------------------------------------------------
    own_need = needs.for_position(pos)
    cover_best = _bench_cover(roster, ctx.lineup, pos, per_week)
    pw = evidence.weekly_projection
    ros = _ros_rank(evidence)
    ros_rank = ros[1] if ros is not None else None
    ros_startable = _inside(ros_rank, evidence.startable_depth)
    cls: str | None
    if entered_slot is not None and structural_gain >= STARTER_MIN_GAIN - DROP_TOLERANCE:
        cls = IMMEDIATE_STARTER
    elif week_slot is not None and week_gain >= COVER_MIN_GAIN - DROP_TOLERANCE:
        hole_group = needs.groups.get(group_of_slot(week_slot) or "")
        cls = STREAMER if pos in STREAM_POSITIONS and not (hole_group is not None and hole_group.claim_week_hole) else BYE_COVER
    elif entered_slot is not None and structural_gain > DROP_TOLERANCE:
        cls = FLEX_DEPTH
    elif _is_real_cover(pw, cover_best, own_need, pos, roster) and evidence.scarcity != ABUNDANT:
        # An Abundant market is its own depth: comparable production is on
        # the wire every week, so a pure cover add there is never worth a spot.
        cls = FLEX_DEPTH
    elif evidence.best_waiver_rank is not None and evidence.best_waiver_rank <= WAIVER_TOP:
        cls = UPSIDE_BENCH
    elif evidence.best_waiver_rank is not None and evidence.best_waiver_rank <= WAIVER_LISTED:
        cls = SPECULATIVE_STASH
    elif ros_startable:
        # A weekly waiver board is one question ("who should I claim this
        # week"); FantasyPros ROS and Boone answer another, and a player
        # either of them ranks as a STARTER in this league's own format while
        # he sits free is an add whatever the boards happen to be covering.
        cls = UPSIDE_BENCH
    elif _inside(ros_rank, evidence.rosterable_depth):
        cls = SPECULATIVE_STASH
    else:
        cls = None

    slot = entered_slot or week_slot
    if cls == IMMEDIATE_STARTER:
        displaced = _displaced(ctx.lineup, s_after)
    elif cls in (BYE_COVER, STREAMER):
        displaced = _displaced(ctx.week_lineup, w_after)
    else:
        displaced = None
    groups = [g for g in (group_of_slot(displaced.slot) if displaced is not None else None, group_of_slot(slot), pos) if g]
    need = needs.most_urgent(groups) or own_need
    key, label, positions = _problem(cls or SPECULATIVE_STASH, entry, slot, displaced, ctx.claim_week)

    call = AcquisitionCall(
        entry=entry, evidence=evidence, cls=cls, strength=PASS, problem=label, problem_positions=positions,
        need=need, structural_gain=round(structural_gain, 2), week_gain=round(week_gain, 2), entered_slot=slot,
        problem_key=key, replaces=displaced.name if displaced is not None else None,
        structural_after=s_after, week_after=w_after,
    )
    call.alternatives = alternative_density(
        entry, ctx.free_agents, positions=positions, current_week=ctx.current_week, ros_pos_rank=ctx.ros_pos_rank,
    )
    if cls is None:
        call.pass_reason = _no_path_reason(evidence)
        return call

    # -- strength before the drop -----------------------------------------------------------------
    need_label = need.label if need is not None else None
    own_label = own_need.label if own_need is not None else None
    if cls == IMMEDIATE_STARTER:
        strength = PRIORITY_ADD if need_label in (CRITICAL_NEED, WEAK) or structural_gain >= MAJOR_GAIN else STRONG_ADD
    elif cls in (BYE_COVER, STREAMER):
        strength = STRONG_ADD if need_label == CRITICAL_NEED else DEPTH_ADD
    elif cls == FLEX_DEPTH:
        strength = STRONG_ADD if evidence.support in (SUPPORT_BROAD, SUPPORT_AGREE) and need_label not in (STRONG, SURPLUS) else DEPTH_ADD
    elif cls == UPSIDE_BENCH:
        if own_label in (STRONG, SURPLUS) and evidence.support != SUPPORT_BROAD:
            strength = PASS
            call.pass_reason = f"your {pos} room is {own_label} and he has no path onto this lineup"
        elif evidence.support in (SUPPORT_BROAD, SUPPORT_AGREE):
            strength = DEPTH_ADD
        else:
            strength = SPECULATIVE_ADD
    else:  # SPECULATIVE_STASH
        if own_label in (STRONG, SURPLUS):
            strength = PASS
            call.pass_reason = f"your {pos} room is {own_label} and he is a deep stash with no path onto this lineup"
        else:
            strength = SPECULATIVE_ADD
    if DEEPER_LEAGUES_ONLY in evidence.labels and cls in SPECULATIVE_CLASSES and strength != PASS:
        strength = _DEMOTE[strength]
        if strength == PASS:
            call.pass_reason = f"RotoBaller tags him for deeper leagues ({evidence.rotoballer_note}) and he has no path onto this lineup"
    # A row tagged for 2QB/Superflex is a recommendation about a FORMAT this
    # league does not run, which is at least as disqualifying as a league-size
    # tag: it is the board's backup quarterback, named for someone else.
    if SUPERFLEX_ONLY in evidence.labels and cls in SPECULATIVE_CLASSES and strength != PASS:
        strength = _DEMOTE[strength]
        if strength == PASS:
            call.pass_reason = (
                f"RotoBaller tags him for Superflex/2QB leagues ({evidence.rotoballer_note}); "
                "this league starts one quarterback and he has no path onto this lineup"
            )
    if FANTASYPROS_HIGHER in evidence.labels and cls in SPECULATIVE_CLASSES and strength != PASS and not ros_startable:
        # The boring-known-quantity pass is right for a player the season
        # ranks as roster depth. It is wrong for one they rank as a STARTER
        # in this format — that player is the whole point of the depth rung
        # above, and passing on him was how the best free-agent TE in the
        # league stayed invisible.
        strength = PASS
        call.pass_reason = (
            f"{ros[0]} {evidence.pos_label(ros_rank)} is roster depth in this league, not a starter, "
            "and he has no path onto this lineup"
            if ros is not None else "a known quantity with no path onto this lineup"
        )
    if cls in SPECULATIVE_CLASSES and strength != PASS and evidence.scarcity == ABUNDANT and _single_starter_position(pos, roster):
        strength = PASS
        call.pass_reason = f"a bench {pos} in a league that starts one, with comparable {pos}s on waivers every week"
    call.strength = strength

    # -- drop --------------------------------------------------------------------------------------------------------
    if strength != PASS:
        if open_spot_available:
            call.needs_drop = False
        else:
            call.drop = pick_drop(call, ctx)
            if call.drop is None:
                call.strength = PASS
                call.pass_reason = NO_DROP_REASONS[0] if ctx.drops.options else NO_DROP_REASONS[1]

    _explain(call, need=need, claim_week=ctx.claim_week)
    return call


def _single_starter_position(pos: str | None, roster: ValuedRoster) -> bool:
    """A position this league can only ever start one of — where a stashed
    backup almost never plays.

    Counted over the real slot list, not literal slot names: a FLEX is a
    second TE slot every week someone wants to use it that way, and a
    SUPER_FLEX is a second QB slot. Counting the token "TE" instead called a
    two-FLEX league single-starter and passed on its best free-agent TE."""
    if not pos:
        return False
    return sum(1 for slot in starter_slots_for(roster) if pos in slot_eligibility(slot)) <= 1


def _is_real_cover(pw: float | None, cover_best: float, own_need: PositionNeed | None, pos: str | None, roster: ValuedRoster) -> bool:
    """A depth add has to be cover someone would actually start: a flex-
    eligible position (or QB in Superflex), clearly better than the bench
    cover already there, and a credible fill for the weakest starter. A
    backup QB in a 1QB league is not depth anyone starts."""
    if pw is None or own_need is None or own_need.weakest_projection is None:
        return False
    if pos not in ("RB", "WR", "TE") and not (pos == "QB" and roster.fmt.is_superflex):
        return False
    return pw >= cover_best + MATERIAL_COVER_POINTS and pw >= DEPTH_COVER_RATIO * own_need.weakest_projection


def _bench_cover(roster: ValuedRoster, lineup: LineupResult, position: str | None, per_week: int) -> float:
    starters = lineup.starter_ids
    bench = [e.value.proj_points / per_week for e in roster.entries
             if e.position == position and e.player_id not in starters and not (e.is_reserve or e.is_taxi) and e.value.proj_points is not None]
    return max(bench) if bench else 0.0


def drop_rejection(call: AcquisitionCall, opt: DropOption, ctx: LeagueWaiverContext) -> str | None:
    """Why `opt` can't be this add's drop, or None when the pairing passes
    the opportunity-cost guardrail (module docstring)."""
    add, d = call.entry, opt.entry
    if d.player_id == add.player_id:
        return f"{d.name}: the same player"
    if call.cls in SPECULATIVE_CLASSES and not opt.is_dead_spot:
        return f"{d.name}: a stash only takes a dead roster spot"
    add_rank = ctx.ros_pos_rank(add)
    if opt.ros_starter_calibre and not (
        add.position == d.position and add_rank is not None and opt.ros_pos_rank is not None and add_rank < opt.ros_pos_rank
    ):
        return f"{d.name}: ROS {d.position}{opt.ros_pos_rank} is starter calibre here"
    gain = max(call.structural_gain, call.week_gain)
    if opt.cover_value >= MATERIAL_COVER_POINTS:
        same_cover = add.position == d.position and (add.value.proj_points or 0) >= (d.value.proj_points or 0)
        if not same_cover and gain < opt.cover_value:
            return f"{d.name}: your best {d.position} cover ({opt.cover_value:.1f}/wk) is worth more than this add's gain ({gain:.1f}/wk)"
    # A drop who is not in either simulated lineup costs the lineup nothing;
    # only one who would have started gets the (expensive) re-simulation.
    for base, after, week in ((ctx.lineup, call.structural_after, None), (ctx.week_lineup, call.week_after, ctx.claim_week)):
        if base is None:
            continue
        started = after.starter_ids if after is not None else base.starter_ids
        if d.player_id not in started:
            continue
        raw, _ = _gain(ctx.roster, base, add, nfl_week=week, remove=(d.player_id,))
        wanted = call.structural_gain if week is None else call.week_gain
        if raw / ctx.per_week < wanted - DROP_TOLERANCE:
            return f"{d.name}: he starts for you" + (f" in week {week}" if week else "") + " even after the add"
    return None


def pick_drop(call: AcquisitionCall, ctx: LeagueWaiverContext, *, exclude_ids: Collection[str] = ()) -> DropOption | None:
    """The cheapest drop that passes the guardrail, skipping `exclude_ids`
    (drops an earlier claim in the plan already uses)."""
    excluded = set(exclude_ids)
    for opt in ctx.drops.options:
        if opt.entry.player_id in excluded:
            continue
        why = drop_rejection(call, opt, ctx)
        if why is None:
            return opt
        if why not in call.rejected_drops:
            call.rejected_drops.append(why)
    return None


def _explain(call: AcquisitionCall, *, need: PositionNeed | None, claim_week: int | None) -> None:
    ev = call.evidence
    why: list[str] = []
    if call.cls == IMMEDIATE_STARTER and call.entered_slot:
        over = f" over {call.replaces}" if call.replaces else ""
        why.append(f"enters your lineup at {slot_label(call.entered_slot)}{over} for +{call.structural_gain:.1f} projected pts/week")
    elif call.cls in (BYE_COVER, STREAMER) and call.entered_slot:
        why.append(f"starts at {slot_label(call.entered_slot)} in week {claim_week} for +{call.week_gain:.1f} pts")
    elif call.cls == FLEX_DEPTH:
        why.append(
            f"enters at {slot_label(call.entered_slot)} for +{call.structural_gain:.1f} pts/week" if call.entered_slot and call.structural_gain > 0
            else f"becomes your best {call.entry.position} cover"
        )
    if need is not None and need.label in (CRITICAL_NEED, WEAK):
        why.append(f"{need.group} is {need.label}: {need.reasons[0]}" if need.reasons else f"{need.group} is {need.label}")
    why.extend(ev.for_lines)
    call.why = why
    risks = list(ev.risk_lines)
    if need is not None and need.label in (STRONG, SURPLUS) and call.cls != IMMEDIATE_STARTER:
        risks.append(f"your {need.group} room is already {need.label}")
    if ev.scarcity == ABUNDANT:
        risks.append(f"{call.entry.position} wire is Abundant here — comparable production keeps appearing")
    elif ev.scarcity == NORMAL and call.strength in (PRIORITY_ADD, STRONG_ADD):
        risks.append(f"{call.entry.position} wire is Normal, not Scarce")
    if call.alternatives is not None and call.alternatives.count >= 2:
        risks.append(call.alternatives.describe())
    if ev.weekly_projection is None:
        risks.append("no projection in this league's sources yet")
    if call.drop is not None and call.drop.status_caution:
        risks.append(f"dropping {call.drop.entry.name}: {call.drop.status_caution}")
    call.risks = risks


def assess_all(
    candidates: Collection[RosterEntry], evidence: dict[str, WaiverEvidence], ctx: LeagueWaiverContext, *, open_spots: int = 0,
) -> list[AcquisitionCall]:
    """Open roster spots are not handed out here — which add takes one is a
    plan decision (waiver_plan) — so every call is judged as if it needs a
    drop; `open_spots` only lets an add with no viable drop survive."""
    calls = [assess_candidate(c, evidence[c.player_id], ctx) for c in candidates if c.player_id in evidence]
    if open_spots > 0:
        calls = [
            assess_candidate(c.entry, c.evidence, ctx, open_spot_available=True) if c.pass_reason in NO_DROP_REASONS else c
            for c in calls
        ]
    calls.sort(key=AcquisitionCall.order_key)
    return calls
