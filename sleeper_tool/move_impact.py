"""Move Impact Preview — what a recommended move actually changes on the
roster, as opposed to whether it "wins" on value.

For a trade rated at least Moderate acceptance, or a Must-Add waiver
recommendation, the post-move roster is built (roster_after_moves — never
the cached one) and the affected pieces recomputed: the best legal lineup
(shared optimizer), projected starter points, positional depth needs,
contender/middling/rebuild status, starter age profile, and total roster
value. Only MATERIAL deltas are reported, as descriptive statements —
never a predicted win total:

  starter points   |delta| >= MATERIAL_WEEKLY_POINTS per week
  lineup           anyone entering or leaving the optimized lineup
  depth needs      the identify_depth_needs list changing
  team status      the classification changing
  roster value     |relative delta| >= MATERIAL_VALUE_RATIO
  starter age      |delta| >= MATERIAL_AGE_YEARS

Team status is compared like with like: team_status ranks roster strength
on Sleeper's is_starter flags, which a hypothetical roster's incoming
players don't have, so BOTH the before and after leagues are re-flagged
from the optimizer's lineups (with_optimized_starters) before
classifying. The "before" status shown here can therefore differ from
the report's headline team status (which uses the set lineup); the delta
only reports a change between the two like-for-like classifications.

Known approximation: draft picks in a trade aren't reflected in the
status or value preview (pick ownership lives in Sleeper's traded_picks,
not on the roster object). A pick-heavy trade's preview therefore reads
as player-only; the trade card still shows the picks.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.asset_value import value_currency, value_for_currency
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, roster_after_moves, with_optimized_starters
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.team_status import classify_team_status
from sleeper_tool.trade_engine import identify_depth_needs
from sleeper_tool.trade_rating import ACCEPTANCE_TIERS
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.valuation import games_remaining

MATERIAL_WEEKLY_POINTS = 2.0
MATERIAL_VALUE_RATIO = 0.05
MATERIAL_AGE_YEARS = 1.0
# A status bucket flip only counts when the underlying in-league strength
# percentile also moved this much — a roster sitting on the contender/
# middling line otherwise "changes status" on every bench tweak.
MATERIAL_STATUS_PERCENTILE = 10.0
MIN_ACCEPTANCE_FOR_PREVIEW = "Moderate"
PREVIEWED_WAIVER_TIERS = frozenset({"Must Add"})


@dataclass
class RosterSnapshot:
    lineup: LineupResult
    weekly_points: float
    depth_needs: list[str]
    status: str | None  # classified on optimizer-flagged starters (like-for-like with hypothetical rosters)
    strength_percentile: float | None
    roster_value: float
    avg_starter_age: float | None
    displayed_status: str | None = None  # the report's headline status (set-lineup based), for the "before" side


@dataclass
class MoveImpact:
    label: str
    before: RosterSnapshot
    after: RosterSnapshot
    lineup_in: list[str] = field(default_factory=list)  # names entering the optimized lineup
    lineup_out: list[str] = field(default_factory=list)
    matchup_note: str | None = None  # set by report_data from matchup_leverage; how the weekly delta relates to this week's gap

    @property
    def weekly_points_delta(self) -> float:
        return self.after.weekly_points - self.before.weekly_points

    def material_deltas(self) -> list[str]:
        out: list[str] = []
        d = self.weekly_points_delta
        if abs(d) >= MATERIAL_WEEKLY_POINTS:
            out.append(f"projected starter points {d:+.1f}/wk ({self.before.weekly_points:.0f} → {self.after.weekly_points:.0f})")
        if self.lineup_in or self.lineup_out:
            enters = f"{', '.join(self.lineup_in)} enter{'s' if len(self.lineup_in) == 1 else ''} the lineup" if self.lineup_in else ""
            leaves = f"{', '.join(self.lineup_out)} drop{'s' if len(self.lineup_out) == 1 else ''} out" if self.lineup_out else ""
            out.append("; ".join(b for b in (enters, leaves) if b))
        if self.before.depth_needs != self.after.depth_needs:
            b = ", ".join(self.before.depth_needs) or "none"
            a = ", ".join(self.after.depth_needs) or "none"
            out.append(f"depth needs {b} → {a}")
        if self._status_change_is_material():
            out.append(f"team status {self.before.displayed_status or self.before.status} → {self.after.status}")
        if self.before.roster_value:
            rel = (self.after.roster_value - self.before.roster_value) / self.before.roster_value
            if abs(rel) >= MATERIAL_VALUE_RATIO:
                out.append(f"total roster value {rel:+.0%}")
        if self.before.avg_starter_age is not None and self.after.avg_starter_age is not None:
            age_d = self.after.avg_starter_age - self.before.avg_starter_age
            if abs(age_d) >= MATERIAL_AGE_YEARS:
                out.append(f"average starter age {age_d:+.1f} yrs ({self.after.avg_starter_age:.1f})")
        return out

    def _status_change_is_material(self) -> bool:
        b, a = self.before, self.after
        if not b.status or not a.status or b.status == a.status:
            return False
        if b.displayed_status is not None and a.status == b.displayed_status:
            return False  # "changes" to what the report already says the team is
        if b.strength_percentile is not None and a.strength_percentile is not None:
            return abs(a.strength_percentile - b.strength_percentile) >= MATERIAL_STATUS_PERCENTILE
        return True

    @property
    def is_material(self) -> bool:
        return bool(self.material_deltas())


@dataclass
class PreviewContext:
    """Everything a preview needs about the league, computed once per
    league: the real rosters re-flagged from optimizer lineups (so status
    classification treats real and hypothetical rosters alike)."""
    rosters: dict[int, ValuedRoster]  # real rosters
    normalized: dict[int, ValuedRoster]  # same rosters, is_starter from the optimizer
    current_week: int | None
    storage: object = None
    engine: object = None

    @classmethod
    def build(
        cls,
        rosters: dict[int, ValuedRoster],
        *,
        current_week: int | None,
        storage=None,
        engine=None,
        lineups: dict[int, LineupResult] | None = None,
    ) -> "PreviewContext":
        """`lineups` may carry already-optimized STRUCTURAL lineups by
        roster_id (what with_optimized_starters would compute anyway); any
        roster missing from it is optimized here. Passing the report's
        shared map avoids re-solving every roster's lineup a third time."""
        lineups = lineups or {}
        normalized = {
            rid: with_optimized_starters(r, lineups.get(rid)) if r.entries else r
            for rid, r in rosters.items()
        }
        return cls(rosters=rosters, normalized=normalized, current_week=current_week, storage=storage, engine=engine)


def snapshot_roster(
    roster: ValuedRoster, ctx: PreviewContext, *, lineup: LineupResult | None = None, displayed_status: str | None = None
) -> RosterSnapshot:
    """Snapshot of `roster` (real or hypothetical) inside ctx's league. The
    roster is re-flagged from `lineup` and classified against the league's
    normalized rosters. `displayed_status` is the report's headline status
    for the real roster (see MoveImpact._status_change_is_material)."""
    currency = value_currency(roster)
    lineup = lineup if lineup is not None else optimize_lineup(roster)
    normalized = with_optimized_starters(roster, lineup)
    league = dict(ctx.normalized)
    league[roster.roster_id] = normalized
    result = classify_team_status(roster.roster_id, league, currency, storage=ctx.storage, engine=ctx.engine) if league else None
    ages = [e.age for e in roster.entries if e.player_id in lineup.starter_ids and e.age is not None]
    return RosterSnapshot(
        lineup=lineup,
        weekly_points=lineup.total_projected_points / games_remaining(ctx.current_week),
        depth_needs=identify_depth_needs(roster, roster.fmt.starter_slots),
        status=result.status if result else None,
        strength_percentile=result.strength_percentile if result else None,
        roster_value=sum(value_for_currency(e.value, currency) or 0 for e in roster.entries),
        avg_starter_age=sum(ages) / len(ages) if ages else None,
        displayed_status=displayed_status,
    )


def _impact(label: str, after_roster: ValuedRoster, before: RosterSnapshot, ctx: PreviewContext) -> MoveImpact:
    after = snapshot_roster(after_roster, ctx)
    names_before = {a.player_id: a.name for a in before.lineup.assignments}
    names_after = {a.player_id: a.name for a in after.lineup.assignments}
    return MoveImpact(
        label=label,
        before=before,
        after=after,
        lineup_in=[n for pid, n in names_after.items() if pid not in names_before],
        lineup_out=[n for pid, n in names_before.items() if pid not in names_after],
    )


def preview_trade(
    proposal: TradeProposal, my_roster: ValuedRoster, before: RosterSnapshot, ctx: PreviewContext
) -> MoveImpact | None:
    """None when the proposal is below the acceptance bar for previewing —
    a preview of a trade nobody would accept is noise."""
    if ACCEPTANCE_TIERS.index(proposal.acceptance_rating) < ACCEPTANCE_TIERS.index(MIN_ACCEPTANCE_FOR_PREVIEW):
        return None
    give_ids = [e.player_id for e in proposal.give]
    my_after = roster_after_moves(my_roster, add_entries=proposal.receive, remove_player_ids=give_ids)
    # The counterparty's roster changes too, which matters for the
    # in-league strength ranking behind team status.
    their = next((r for r in ctx.rosters.values() if r.owner_username == proposal.target_username), None)
    trade_ctx = ctx
    if their is not None:
        their_after = roster_after_moves(their, add_entries=proposal.give, remove_player_ids=[e.player_id for e in proposal.receive])
        normalized = dict(ctx.normalized)
        normalized[their.roster_id] = with_optimized_starters(their_after)
        trade_ctx = PreviewContext(ctx.rosters, normalized, ctx.current_week, ctx.storage, ctx.engine)
    return _impact(f"Trade with {proposal.target_team_name or proposal.target_username}", my_after, before, trade_ctx)


def preview_add_drop(
    label: str,
    add_entry: RosterEntry,
    drop_player_id: str | None,
    my_roster: ValuedRoster,
    before: RosterSnapshot,
    ctx: PreviewContext,
) -> MoveImpact:
    my_after = roster_after_moves(
        my_roster, add_entries=[add_entry], remove_player_ids=[drop_player_id] if drop_player_id else []
    )
    return _impact(label, my_after, before, ctx)
