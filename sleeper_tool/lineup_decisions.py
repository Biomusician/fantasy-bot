"""Lineup Decisions — the short list of things about THIS WEEK's lineup
that actually need a human call. Everything obvious is left out: a WR1
who starts in every lineup is never mentioned, and a slot the optimizer
and Sleeper's set lineup already agree on says nothing.

Sources, all reused rather than re-derived:
  - the shared lineup optimizer (the only owner of "best legal lineup"),
    called again with one player excluded whenever a what-if needs a
    number — never projection arithmetic that ignores cascades
  - lineup_leverage's Toss-Up / Lean Start labels for close calls
  - bye_collision's weak-fill ratio for holes
  - matchup_leverage's gap for the "these calls decide the matchup" line

Item kinds, in the order they are shown (this order is the priority):
  1. Set-lineup mismatch   Sleeper's SET lineup (RosterEntry.is_starter)
                           differs from the optimizer's this-week lineup.
                           The single most actionable line — a click on
                           Sleeper fixes it — so it always leads.
  2. Toss-Up / Lean Start  lineup_leverage's close calls, with the true
                           cost of benching the starter (optimizer re-run
                           with him excluded, so a cascade is measured).
  3. Injury risk           a this-week starter tagged Questionable or
                           Doubtful (or Out, if the caller's lineup kept
                           him), with the optimizer's next man up.
  4. Empty slot / Bye hole a slot no rostered player can legally fill
                           this week, or a structural starter on bye /
                           ruled out whose replacement projects under
                           BYE_HOLE_REPLACEMENT_RATIO of him; the best
                           free-agent fill when the caller passes a pool.
  5. Flex explanation      a FLEX/SUPER_FLEX holds a player of one
                           position while a different-position candidate
                           within SURPRISE_RATIO of him sits — the reader
                           will wonder, so say what the swap would cost.
  6. Matchup               one line when the close calls' combined stake
                           exceeds this week's projected matchup gap.

All per-week numbers divide rest-of-season projections by
valuation.games_remaining(current_week), exactly as lineup_leverage does.
No probabilities. The list is capped at MAX_ITEMS, most material first
within each kind; None means "nothing needs deciding" and the renderer
may say "Lineup is set" if it wants to.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from sleeper_tool.bye_collision import BYE_HOLE_REPLACEMENT_RATIO, ByeCollision
from sleeper_tool.lineup_leverage import LEAN_START, TOSS_UP, LineupLeverage, StartSitDecision, build_lineup_leverage
from sleeper_tool.lineup_optimizer import slot_label
from sleeper_tool.lineup_optimizer import (
    DEDICATED_POSITIONS,
    FLEX_ELIGIBILITY,
    LineupResult,
    optimize_lineup,
    projection_of,
    slot_eligibility,
    unavailability_reason,
)
from sleeper_tool.matchup_leverage import MatchupLeverage
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.schedule_window import build_windows, schedule_tiebreak
from sleeper_tool.valuation import games_remaining

# Per-week projected points. A set-lineup difference worth no more than
# this is a coin flip the reader shouldn't be told to act on.
EPSILON = 0.1
# A different-position candidate for a flex slot projecting at least this
# fraction of the occupant is close enough that the reader will wonder why
# he sits; below it the projection answers the question on its own.
SURPRISE_RATIO = 0.75
MAX_ITEMS = 8
# Game-day tags that make a this-week starter a risk worth a next-man-up
# line. IR/PUP/Sus/Out are already excluded by the optimizer's rules.
RISK_STATUSES = frozenset({"Questionable", "Doubtful", "Out"})

SET_LINEUP_MISMATCH = "Set-lineup mismatch"
INJURY_RISK = "Injury risk"
EMPTY_SLOT = "Empty slot"
BYE_HOLE = "Bye hole"
FLEX_EXPLANATION = "Flex explanation"
MATCHUP = "Matchup"
# Toss-Up and Lean Start reuse lineup_leverage's own labels as kinds.
KIND_ORDER = (SET_LINEUP_MISMATCH, TOSS_UP, LEAN_START, INJURY_RISK, EMPTY_SLOT, BYE_HOLE, FLEX_EXPLANATION, MATCHUP)
_FLOAT_EPS = 1e-9


@dataclass
class LineupDecision:
    kind: str
    slot: str | None
    players: list[RosterEntry]  # primary first (the starter / the player to act on)
    projections: list[float]  # per week, aligned with `players`
    delta: float  # per-week points at stake, always >= 0; the ordering key within a kind
    summary: str
    what_if: str | None = None
    context: list[str] = field(default_factory=list)  # role / schedule strings the caller supplied

    def describe(self) -> str:
        return f"{self.kind}: {self.summary}"


@dataclass
class LineupDecisions:
    week: int | None
    games_left: int
    items: list[LineupDecision]  # KIND_ORDER first, then most material, capped at MAX_ITEMS
    close_call_stake: float  # per-week points riding on the Toss-Up / Lean Start items

    def kinds(self) -> list[str]:
        return [i.kind for i in self.items]


def _weekly(points: float, games_left: int) -> float:
    return points / games_left


def _above_epsilon(delta: float) -> bool:
    return delta - EPSILON > _FLOAT_EPS


def _rerun_without(roster: ValuedRoster, week: LineupResult, player_id: str) -> LineupResult:
    """The optimizer's answer to "what if he can't go?" — same week, same
    game-day-out rule, one more exclusion. Never hand-rolled."""
    return optimize_lineup(
        roster, nfl_week=week.nfl_week, exclude_game_day_out=True, excluded_player_ids={player_id}
    )


def _entrant(week: LineupResult, rerun: LineupResult) -> str | None:
    """The one player the re-run started who wasn't starting before (a
    cascade moves existing starters around but only one new body enters).
    None when the re-run left a slot empty instead."""
    new = sorted(rerun.starter_ids - week.starter_ids)
    return new[0] if new else None


def _proj_desc(e: RosterEntry) -> tuple:
    return (-projection_of(e), e.player_id)


# --- 1. set-lineup mismatches ------------------------------------------------


def _set_lineup_mismatches(
    roster: ValuedRoster, week: LineupResult, by_id: dict[str, RosterEntry], games_left: int
) -> list[LineupDecision]:
    set_ids = {e.player_id for e in roster.entries if e.is_starter}
    if not set_ids:
        return []  # no lineup set on Sleeper at all (pre-season) — nothing to compare against
    optimal = week.starter_ids
    should_sit = sorted((by_id[p] for p in set_ids - optimal), key=_proj_desc)
    should_start = sorted((week.assignment_for(p) for p in optimal - set_ids), key=lambda a: (-a.projection, a.slot_index))

    items: list[LineupDecision] = []
    unpaired = list(should_sit)
    for a in should_start:
        entrant = by_id[a.player_id]
        entrant_weekly = _weekly(a.projection, games_left)
        # RosterEntry doesn't carry Sleeper's slot for a set starter, so
        # the pairing is "the best set starter who could legally hold the
        # slot the optimizer gives the entrant" — best-to-best, like the
        # bye planner does.
        partner = next((e for e in unpaired if e.position in slot_eligibility(a.slot)), None)
        if partner is None:
            items.append(
                LineupDecision(
                    SET_LINEUP_MISMATCH, a.slot, [entrant], [entrant_weekly], entrant_weekly,
                    f"{entrant.name} is benched on Sleeper but the optimizer starts him at {slot_label(a.slot)} ({entrant_weekly:.1f}/wk)",
                )
            )
            continue
        unpaired.remove(partner)
        blocked = week.unavailable.get(partner.player_id)
        partner_weekly = 0.0 if blocked else _weekly(projection_of(partner), games_left)
        delta = entrant_weekly - partner_weekly
        if not _above_epsilon(delta):
            continue  # a tie-break flip, not a decision
        if blocked:
            summary = (
                f"{partner.name} is set to start at {slot_label(a.slot)} but is {blocked}; "
                f"{entrant.name} projects {entrant_weekly:.1f}/wk in his place (+{delta:.1f}/wk)"
            )
        else:
            summary = (
                f"{partner.name} is set to start but {entrant.name} projects higher this week at {slot_label(a.slot)} "
                f"({entrant_weekly:.1f} vs {partner_weekly:.1f}, +{delta:.1f}/wk)"
            )
        items.append(LineupDecision(SET_LINEUP_MISMATCH, a.slot, [partner, entrant], [partner_weekly, entrant_weekly], delta, summary))

    for e in unpaired:
        # A set starter with no optimizer counterpart is only worth a line
        # when Sleeper will score him at zero — on bye or ruled out with no
        # legal fill. An available player the optimizer simply benches
        # always has a counterpart above.
        blocked = week.unavailable.get(e.player_id)
        if blocked:
            items.append(
                LineupDecision(
                    SET_LINEUP_MISMATCH, None, [e], [0.0], 0.0,
                    f"{e.name} is set to start but is {blocked} — no legal fill on the roster",
                )
            )
    return items


# --- 2. close calls -----------------------------------------------------------


def _this_week_close_calls(
    roster: ValuedRoster, week: LineupResult, leverage: LineupLeverage, current_week: int | None
) -> list[StartSitDecision]:
    """report_data builds leverage on the STRUCTURAL lineup. When this
    week's byes and game-day outs change who starts, the close calls are
    re-labelled on the week lineup — the same function over this week's
    bench, never a second labelling rule — and the schedule tiebreaks
    report_data attached are carried across by (starter, alternative)."""
    same_lineup = (
        leverage.lineup.starter_ids == week.starter_ids
        and set(leverage.lineup.bench_player_ids) == set(week.bench_player_ids)
    )
    if same_lineup:
        return leverage.close_calls
    weekly = build_lineup_leverage(roster, lineup=week, current_week=current_week)
    if weekly is None:
        return []
    notes = {
        (d.starter.player_id, d.alternative.player_id): d.schedule_note
        for d in leverage.decisions if d.alternative is not None and d.schedule_note
    }
    for d in weekly.close_calls:
        if d.alternative is not None and d.schedule_note is None:
            d.schedule_note = notes.get((d.starter.player_id, d.alternative.player_id))
    return weekly.close_calls


def _close_calls(
    roster: ValuedRoster, week: LineupResult, leverage: LineupLeverage, by_id: dict[str, RosterEntry],
    games_left: int, schedule, current_week: int | None,
) -> list[LineupDecision]:
    windows = build_windows(schedule, {}, current_week) if schedule is not None else None
    bench = set(week.bench_player_ids)
    items: list[LineupDecision] = []
    for d in _this_week_close_calls(roster, week, leverage, current_week):
        if d.alternative is None or d.label not in (TOSS_UP, LEAN_START):
            continue
        # Belt and braces after _this_week_close_calls: a starter who isn't
        # in this week's lineup is a hole (kind 4), not a start/sit call,
        # and an alternative who isn't on this week's bench can't be
        # started instead.
        if d.starter.player_id not in week.starter_ids or d.alternative.player_id not in bench:
            continue
        slot = week.slot_by_player[d.starter.player_id]
        rerun = _rerun_without(roster, week, d.starter.player_id)
        cost = _weekly(week.total_projected_points - rerun.total_projected_points, games_left)
        entrant_id = _entrant(week, rerun)
        entrant = by_id[entrant_id] if entrant_id is not None else None
        starter_weekly = _weekly(d.starter_projection, games_left)
        alt_weekly = _weekly(d.alternative_projection, games_left)
        if entrant is None or entrant.player_id == d.alternative.player_id:
            who = d.alternative.name
        else:
            # The optimizer's cascade brings in someone else once the
            # starter is out; that is the real alternative this week.
            who = f"{d.alternative.name} (the optimizer would actually bring in {entrant.name})"
        verb = "costs" if cost >= 0 else "gains"
        what_if = f"Starting {who} instead {verb} {abs(cost):.1f}/wk"
        context: list[str] = []
        note = d.schedule_note
        if note is None and schedule is not None and windows is not None:
            note = schedule_tiebreak(
                d.starter.name, d.starter.team, d.starter_projection,
                d.alternative.name, d.alternative.team, d.alternative_projection, schedule, windows,
            )
        if note:
            context.append(note)
        items.append(
            LineupDecision(
                d.label, slot, [d.starter, d.alternative], [starter_weekly, alt_weekly], abs(cost),
                f"{slot_label(slot)}: {d.starter.name} ({starter_weekly:.1f}/wk) over {d.alternative.name} ({alt_weekly:.1f}/wk)",
                what_if, context,
            )
        )
    return items


# --- 3. injury risk ------------------------------------------------------------


def _injury_risks(
    roster: ValuedRoster, week: LineupResult, by_id: dict[str, RosterEntry], games_left: int
) -> list[LineupDecision]:
    items: list[LineupDecision] = []
    for a in week.assignments:
        starter = by_id[a.player_id]
        if starter.injury_status not in RISK_STATUSES:
            continue
        rerun = _rerun_without(roster, week, a.player_id)
        loss = _weekly(week.total_projected_points - rerun.total_projected_points, games_left)
        entrant_id = _entrant(week, rerun)
        starter_weekly = _weekly(a.projection, games_left)
        if entrant_id is None:
            summary = f"{slot_label(a.slot)}: {starter.name} is {starter.injury_status} and no rostered player can legally fill the slot if he sits (-{loss:.1f}/wk)"
            players, projections = [starter], [starter_weekly]
        else:
            entrant = by_id[entrant_id]
            entrant_weekly = _weekly(projection_of(entrant), games_left)
            summary = f"{slot_label(a.slot)}: {starter.name} is {starter.injury_status} — next man up is {entrant.name} ({entrant_weekly:.1f}/wk, -{loss:.1f}/wk)"
            players, projections = [starter, entrant], [starter_weekly, entrant_weekly]
        items.append(LineupDecision(INJURY_RISK, a.slot, players, projections, loss, summary))
    return items


# --- 4. empty slots and bye holes ---------------------------------------------


def _available_this_week(e: RosterEntry, current_week: int | None) -> bool:
    return unavailability_reason(e, current_week, exclude_game_day_out=True) is None


def _best_free_agent(free_agents: Collection[RosterEntry], positions: Collection[str], current_week: int | None) -> RosterEntry | None:
    # An unprojected body (K/DEF have no projection source here) still
    # beats an empty slot, the same rule the optimizer applies on-roster;
    # any projected candidate outranks him.
    pool = [fa for fa in free_agents if fa.position in positions and _available_this_week(fa, current_week)]
    return min(pool, key=_proj_desc) if pool else None


def _fill_line(fa: RosterEntry | None, games_left: int) -> str | None:
    if fa is None:
        return None
    if projection_of(fa) <= 0:
        return f"A free agent can fill it: {fa.name} ({fa.position}) — no projection to rank the options by"
    return f"Best free-agent fill: {fa.name} ({fa.position}, {_weekly(projection_of(fa), games_left):.1f}/wk)"


def _holes(
    week: LineupResult, structural: LineupResult, bye_collision: ByeCollision | None, by_id: dict[str, RosterEntry],
    games_left: int, free_agents: Collection[RosterEntry], current_week: int | None,
) -> list[LineupDecision]:
    items: list[LineupDecision] = []
    for slot in week.unfilled_slots:
        fa = _best_free_agent(free_agents, slot_eligibility(slot), current_week)
        items.append(
            LineupDecision(
                EMPTY_SLOT, slot, [], [], 0.0,
                f"{slot_label(slot)} is empty this week — no rostered player can legally fill it",
                _fill_line(fa, games_left),
            )
        )

    # Displaced structural starters paired best-to-best with this week's
    # entrants, the way bye_collision measures a look-ahead week. The
    # planner deliberately never scans the current week, so a caller's
    # ByeCollision only applies when it happens to be for this week.
    if bye_collision is not None and bye_collision.week == current_week:
        holes = [(h.slot, h.normal_starter, h.normal_projection, h.replacement, h.replacement_projection) for h in bye_collision.holes]
    else:
        displaced = sorted(
            (a for a in structural.assignments if a.player_id in week.unavailable and a.projection > 0),
            key=lambda a: (-a.projection, a.slot_index),
        )
        entrants = sorted((a for a in week.assignments if a.player_id not in structural.starter_ids), key=lambda a: (-a.projection, a.slot_index))
        holes = []
        for i, a in enumerate(displaced):
            filler = entrants[i] if i < len(entrants) else None
            if filler is None:
                holes.append((a.slot, by_id[a.player_id], a.projection, None, 0.0))
            elif filler.projection < BYE_HOLE_REPLACEMENT_RATIO * a.projection:
                holes.append((a.slot, by_id[a.player_id], a.projection, by_id[filler.player_id], filler.projection))

    unfilled = set(week.unfilled_slots)
    for slot, starter, starter_proj, replacement, replacement_proj in holes:
        reason = week.unavailable.get(starter.player_id, "unavailable this week")
        starter_weekly = _weekly(starter_proj, games_left)
        if replacement is None:
            if slot in unfilled:
                continue  # already reported as an empty slot above
            positions = {slot_label(slot)} if slot in DEDICATED_POSITIONS else {starter.position} if starter.position else set()
            items.append(
                LineupDecision(
                    BYE_HOLE, slot, [starter], [starter_weekly], starter_weekly,
                    f"{slot_label(slot)}: {starter.name} is {reason} and no rostered player can legally cover him",
                    _fill_line(_best_free_agent(free_agents, positions, current_week), games_left),
                )
            )
            continue
        replacement_weekly = _weekly(replacement_proj, games_left)
        delta = starter_weekly - replacement_weekly
        ratio = replacement_proj / starter_proj if starter_proj else 0.0
        positions = {slot_label(slot)} if slot in DEDICATED_POSITIONS else {starter.position} if starter.position else set()
        fa = _best_free_agent(free_agents, positions, current_week)
        if fa is not None and projection_of(fa) <= replacement_proj:
            fa = None  # the wire doesn't beat what's already filling in
        items.append(
            LineupDecision(
                BYE_HOLE, slot, [starter, replacement], [starter_weekly, replacement_weekly], delta,
                f"{slot_label(slot)}: {starter.name} is {reason}; {replacement.name} fills in at {ratio:.0%} of his projection (-{delta:.1f}/wk)",
                _fill_line(fa, games_left),
            )
        )
    return items


# --- 5. flex / superflex explanations ---------------------------------------


def _flex_explanations(
    roster: ValuedRoster, week: LineupResult, by_id: dict[str, RosterEntry], games_left: int, covered_slots: Collection[str]
) -> list[LineupDecision]:
    items: list[LineupDecision] = []
    current_week = week.nfl_week
    for a in week.assignments:
        if a.slot not in FLEX_ELIGIBILITY or a.slot in covered_slots or a.projection <= 0:
            continue
        occupant = by_id[a.player_id]
        eligible = slot_eligibility(a.slot)
        # Cheap pre-check: nobody of another position is close enough for
        # the reader to wonder, so don't spend a re-run on it.
        wonder = [
            e for e in roster.entries
            if e.player_id != occupant.player_id and e.position in eligible and e.position != occupant.position
            and projection_of(e) + _FLOAT_EPS >= SURPRISE_RATIO * a.projection and _available_this_week(e, current_week)
        ]
        if not wonder:
            continue
        rerun = _rerun_without(roster, week, occupant.player_id)
        new = next((r for r in rerun.assignments if r.slot_index == a.slot_index), None)
        if new is None:
            continue
        alt = by_id[new.player_id]
        if alt.position == occupant.position or new.projection + _FLOAT_EPS < SURPRISE_RATIO * a.projection:
            continue
        reduction = _weekly(week.total_projected_points - rerun.total_projected_points, games_left)
        if not _above_epsilon(reduction):
            continue  # dead even — that is a Toss-Up, and leverage already said so
        occupant_weekly = _weekly(a.projection, games_left)
        alt_weekly = _weekly(new.projection, games_left)
        items.append(
            LineupDecision(
                FLEX_EXPLANATION, a.slot, [occupant, alt], [occupant_weekly, alt_weekly], reduction,
                f"{occupant.name} ({occupant.position}) occupies {slot_label(a.slot)} because moving {alt.name} ({alt.position}) there "
                f"would reduce the lineup by {reduction:.1f}/wk",
            )
        )
    return items


# --- 6. matchup ----------------------------------------------------------------


def _matchup_note(matchup: MatchupLeverage | None, stake: float) -> LineupDecision | None:
    if matchup is None or stake <= 0 or stake <= abs(matchup.gap):
        return None
    return LineupDecision(
        MATCHUP, None, [], [], stake,
        f"The close calls above carry {stake:.1f}/wk between them, more than the {abs(matchup.gap):.1f}-point "
        f"projected gap vs {matchup.opponent_name} — the lineup calls decide this matchup",
    )


# --- assembly ---------------------------------------------------------------


def build_lineup_decisions(
    my_roster: ValuedRoster,
    *,
    structural_lineup: LineupResult | None = None,
    week_lineup: LineupResult | None = None,
    leverage: LineupLeverage | None = None,
    bye_collision: ByeCollision | None = None,
    matchup: MatchupLeverage | None = None,
    current_week: int | None = None,
    schedule=None,
    free_agents: Collection[RosterEntry] = (),
    context_lines: dict[str, list[str]] | None = None,
) -> LineupDecisions | None:
    """What needs a decision about this week's lineup, or None when the
    roster is empty or nothing does. Any lineup input the caller doesn't
    have is solved by the shared optimizer; `week_lineup` is the current
    week with byes and game-day outs applied (matchup_leverage's
    `my_lineup` is exactly that)."""
    if not my_roster.entries:
        return None
    structural = structural_lineup if structural_lineup is not None else optimize_lineup(my_roster)
    week = (
        week_lineup if week_lineup is not None
        else optimize_lineup(my_roster, nfl_week=current_week, exclude_game_day_out=True)
    )
    if leverage is None:
        leverage = build_lineup_leverage(my_roster, lineup=structural, current_week=current_week)
    by_id = {e.player_id: e for e in my_roster.entries}
    games_left = games_remaining(current_week)

    mismatches = _set_lineup_mismatches(my_roster, week, by_id, games_left)
    close = _close_calls(my_roster, week, leverage, by_id, games_left, schedule, current_week) if leverage is not None else []
    risks = _injury_risks(my_roster, week, by_id, games_left)
    holes = _holes(week, structural, bye_collision, by_id, games_left, free_agents, current_week)
    flex = _flex_explanations(my_roster, week, by_id, games_left, {c.slot for c in close})
    stake = sum(c.delta for c in close)

    order = {k: i for i, k in enumerate(KIND_ORDER)}
    items = sorted(
        mismatches + close + risks + holes + flex,
        key=lambda i: (order[i.kind], -round(i.delta, 6), i.slot or "", [p.player_id for p in i.players]),
    )
    note = _matchup_note(matchup, stake)
    if note is not None:
        items = items[: MAX_ITEMS - 1] + [note]  # the matchup line keeps the last slot rather than being capped away
    else:
        items = items[:MAX_ITEMS]
    if not items:
        return None

    if context_lines:
        for item in items:
            for p in item.players:
                for line in context_lines.get(p.player_id, ()):
                    if line not in item.context:
                        item.context.append(line)
    return LineupDecisions(week=current_week, games_left=games_left, items=items, close_call_stake=stake)
