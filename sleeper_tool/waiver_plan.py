"""Waiver Plan — the ordered claim sequence I would submit for one league.

Decision support only: nothing is ever submitted.

Claims are grouped by the roster problem they solve (waiver_acquisition's
`problem_key`: adds that would push the same starter out of the lineup,
cover the same claim-week hole, stream the same position, or take the
speculative-upside spot). Within a group the claims are ordered
substitutes; across groups they are independent.

Dependencies:
  INDEPENDENT                the first claim of a group — pursue regardless
  IF PREVIOUS FAILS          a backup in the same group. It carries the SAME
                             drop as the claim ahead of it, so once either
                             clears the other has nothing left to drop and
                             cannot process — the chain enforces itself.
  STOP AFTER SUCCESS         stated once per group with backups: one success
                             ends that problem for the week
  ONLY IF DROP STILL AVAILABLE  a group whose best drop an earlier group
                             already uses, and no other viable drop exists:
                             it can only process if that earlier claim fails

Rules a plan never breaks:
  - no player is planned as a drop by two independent groups
  - a backup's recommended bid stays below the claim ahead of it
    (processing order on bid-ordered waivers must reach the first choice
    first); the note says when a backup's own window was cut to do that
  - independent bids that together exceed the remaining budget get a
    note — nothing is silently re-priced
  - an open roster spot goes to the first group that needs a drop

A league can run on FAAB or on waiver priority. Priority leagues get
Use Priority / Consider Priority / Hold Priority from the same acquisition
strength and alternatives; the plan is only built for such a league when
its priority state is actually available (see report_data).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sleeper_tool.faab_window import FaabWindow
from sleeper_tool.roster_analysis import RosterEntry
from sleeper_tool.waiver_acquisition import (
    PRIORITY_ADD,
    SPECULATIVE_ADD,
    STRONG_ADD,
    AcquisitionCall,
)
from sleeper_tool.waiver_alternatives import FEW, UNIQUE
from sleeper_tool.waiver_drops import DropOption
from sleeper_tool.waiver_evidence import WAIVER_TOP

FAAB_MODE = "FAAB"
PRIORITY_MODE = "PRIORITY"

INDEPENDENT = "INDEPENDENT"
IF_PREVIOUS_FAILS = "IF PREVIOUS FAILS"
STOP_AFTER_SUCCESS = "STOP AFTER SUCCESS"
ONLY_IF_DROP_AVAILABLE = "ONLY IF DROP STILL AVAILABLE"

USE_PRIORITY = "Use Priority"
CONSIDER_PRIORITY = "Consider Priority"
HOLD_PRIORITY = "Hold Priority"

MAX_GROUPS = 4
MAX_BACKUPS = 2  # per group, after the first choice
MAX_SPECULATIVE_GROUPS = 1  # at most one speculative-upside spot a week
MAX_DO_NOT_SPEND = 5


@dataclass
class Claim:
    order: int  # 1-based, in submission order
    call: AcquisitionCall
    dependency: str
    depends_on: int | None = None  # the claim order this one waits on
    drop_option: DropOption | None = None
    window: FaabWindow | None = None
    priority_call: str | None = None  # PRIORITY leagues only
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.call.entry.name

    @property
    def drop(self) -> RosterEntry | None:
        return self.drop_option.entry if self.drop_option is not None else None


@dataclass
class ClaimGroup:
    problem: str
    claims: list[Claim]

    @property
    def stop_after_success(self) -> bool:
        return len(self.claims) > 1


@dataclass
class WaiverPlan:
    mode: str
    groups: list[ClaimGroup] = field(default_factory=list)
    do_not_spend: list[AcquisitionCall] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def claims(self) -> list[Claim]:
        return [c for g in self.groups for c in g.claims]

    @property
    def top_claim(self) -> Claim | None:
        return self.groups[0].claims[0] if self.groups else None

    def backups_for(self, claim: Claim) -> list[Claim]:
        return [c for c in self.claims() if c.depends_on == claim.order and c.dependency == IF_PREVIOUS_FAILS]


def priority_call(call: AcquisitionCall) -> str:
    density = call.alternatives.label if call.alternatives is not None else None
    if call.strength == PRIORITY_ADD or (call.strength == STRONG_ADD and density in (UNIQUE, FEW)):
        return USE_PRIORITY
    if call.strength == STRONG_ADD:
        return CONSIDER_PRIORITY
    return HOLD_PRIORITY


def build_plan(
    calls: list[AcquisitionCall],
    *,
    mode: str = FAAB_MODE,
    open_spots: int = 0,
    remaining_budget: int | None = None,
    window_for: Callable[[AcquisitionCall], FaabWindow | None] = lambda c: None,
    choose_drop: Callable[[AcquisitionCall, set[str]], DropOption | None] | None = None,
    drop_ok: Callable[[AcquisitionCall, DropOption], bool] | None = None,
    bid_min: int = 0,
) -> WaiverPlan:
    """`calls` in acquisition order. `choose_drop(call, used_ids)` finds the
    next viable drop that isn't already used; `drop_ok(call, option)` checks
    one specific pairing (a backup inheriting the first choice's drop)."""
    plan = WaiverPlan(mode=mode)
    claims = [c for c in calls if c.is_claim]
    by_key: dict[str, list[AcquisitionCall]] = {}
    key_order: list[str] = []
    for c in claims:
        if c.problem_key not in by_key:
            by_key[c.problem_key] = []
            key_order.append(c.problem_key)
        by_key[c.problem_key].append(c)

    used_drops: dict[str, int] = {}  # drop player_id -> the claim order that first uses it
    order = 0
    speculative_groups = 0
    spots = max(0, open_spots)
    for key in key_order:
        if len(plan.groups) >= MAX_GROUPS:
            break
        members = by_key[key]
        lead = members[0]
        if lead.strength == SPECULATIVE_ADD:
            if speculative_groups >= MAX_SPECULATIVE_GROUPS:
                continue
            speculative_groups += 1

        order += 1
        first = Claim(order=order, call=lead, dependency=INDEPENDENT, window=window_for(lead))
        drop_opt: DropOption | None = None
        if spots > 0:
            spots -= 1
            first.notes.append("uses an open roster spot — no drop needed")
        else:
            drop_opt = lead.drop
            if choose_drop is not None and (drop_opt is None or drop_opt.entry.player_id in used_drops):
                # The lead's own best drop is taken (or he was judged against an
                # open spot the plan has already given away): the next viable
                # one, else share and wait.
                drop_opt = choose_drop(lead, set(used_drops)) or drop_opt
            if drop_opt is None:
                order -= 1
                continue
            if drop_opt.entry.player_id in used_drops:
                first.dependency = ONLY_IF_DROP_AVAILABLE
                first.depends_on = used_drops[drop_opt.entry.player_id]
                first.notes.append(
                    f"shares {drop_opt.entry.name} with claim {first.depends_on} — only processes if that claim fails"
                )
            else:
                used_drops[drop_opt.entry.player_id] = order
            first.drop_option = drop_opt
        if mode == PRIORITY_MODE:
            first.priority_call = priority_call(lead)
        group = ClaimGroup(problem=lead.problem, claims=[first])

        # A chain needs room under the first claim for every backup, or the
        # fallbacks are priced at nothing and cannot win.
        backups_wanted = min(MAX_BACKUPS, max(0, len(members) - 1))
        if first.window is not None and backups_wanted:
            headroom = bid_min + backups_wanted
            if first.window.recommended < headroom:
                first.window.recommended = min(max(first.window.recommended, headroom), first.window.high, max(first.window.high, headroom))
        previous = first
        for backup in members[1:1 + MAX_BACKUPS]:
            # A backup inherits the first choice's drop so that only one of
            # them can ever clear; a pairing the guardrail rejects for the
            # backup means he is not a real substitute for this claim.
            if first.drop_option is None:
                # An open-spot claim has no drop to share; a backup would
                # process alongside it rather than instead of it.
                break
            if drop_ok is not None and not drop_ok(backup, first.drop_option):
                continue
            order += 1
            claim = Claim(order=order, call=backup, dependency=IF_PREVIOUS_FAILS, depends_on=previous.order,
                          drop_option=first.drop_option, window=window_for(backup))
            if mode == PRIORITY_MODE:
                claim.priority_call = priority_call(backup)
            _keep_bid_below(claim, previous, bid_min)
            group.claims.append(claim)
            previous = claim
        plan.groups.append(group)

    plan.do_not_spend = [
        c for c in calls
        if not c.is_claim and c.pass_reason and c.evidence.best_waiver_rank is not None and c.evidence.best_waiver_rank <= WAIVER_TOP
    ][:MAX_DO_NOT_SPEND]

    if mode == FAAB_MODE and remaining_budget is not None:
        independent = [c for c in plan.claims() if c.dependency == INDEPENDENT and c.window is not None]
        total = sum(c.window.recommended for c in independent)
        if independent and total > remaining_budget:
            plan.notes.append(
                f"If every independent claim clears, the recommended bids total ${total} against ${remaining_budget} left — "
                "the lowest claim in the plan may not be affordable"
            )
    return plan


def _keep_bid_below(claim: Claim, previous: Claim, bid_min: int) -> None:
    """A backup bids under the claim ahead of it. At the league minimum the
    two can only tie, and the note says so rather than implying an order the
    league's tiebreak (waiver priority) decides."""
    if claim.window is None or previous.window is None:
        return
    ceiling = previous.window.recommended - 1
    if claim.window.recommended <= ceiling:
        return
    if ceiling < bid_min:
        claim.window.recommended = bid_min
        claim.notes.append(
            f"both claims sit at the ${bid_min} minimum — which processes first is the league's tiebreak (waiver order), not this plan"
        )
        return
    was = claim.window.recommended
    claim.window.recommended = ceiling
    claim.notes.append(
        f"worth ${was} on its own; bid ${ceiling} so claim {previous.order} processes first"
    )
