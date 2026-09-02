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

Known approximation: draft picks in a trade aren't reflected in the
status or value preview (pick ownership lives in Sleeper's traded_picks,
not on the roster object). A pick-heavy trade's preview therefore reads
as player-only; the trade card still shows the picks.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, roster_after_moves
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.team_status import classify_team_status
from sleeper_tool.trade_engine import ACCEPTANCE_TIERS, TradeProposal, identify_depth_needs, value_currency, value_for_currency
from sleeper_tool.valuation import games_remaining

MATERIAL_WEEKLY_POINTS = 2.0
MATERIAL_VALUE_RATIO = 0.05
MATERIAL_AGE_YEARS = 1.0
MIN_ACCEPTANCE_FOR_PREVIEW = "Moderate"
PREVIEWED_WAIVER_TIERS = frozenset({"Must Add"})


@dataclass
class RosterSnapshot:
    lineup: LineupResult
    weekly_points: float
    depth_needs: list[str]
    status: str | None
    roster_value: float
    avg_starter_age: float | None


@dataclass
class MoveImpact:
    label: str
    before: RosterSnapshot
    after: RosterSnapshot
    lineup_in: list[str] = field(default_factory=list)  # names entering the optimized lineup
    lineup_out: list[str] = field(default_factory=list)

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
        if self.before.status and self.after.status and self.before.status != self.after.status:
            out.append(f"team status {self.before.status} → {self.after.status}")
        if self.before.roster_value:
            rel = (self.after.roster_value - self.before.roster_value) / self.before.roster_value
            if abs(rel) >= MATERIAL_VALUE_RATIO:
                out.append(f"total roster value {rel:+.0%}")
        if self.before.avg_starter_age is not None and self.after.avg_starter_age is not None:
            age_d = self.after.avg_starter_age - self.before.avg_starter_age
            if abs(age_d) >= MATERIAL_AGE_YEARS:
                out.append(f"average starter age {age_d:+.1f} yrs ({self.after.avg_starter_age:.1f})")
        return out

    @property
    def is_material(self) -> bool:
        return bool(self.material_deltas())


def snapshot_roster(
    roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    *,
    current_week: int | None,
    storage=None,
    engine=None,
    lineup: LineupResult | None = None,
    status: str | None = None,
) -> RosterSnapshot:
    currency = value_currency(roster)
    lineup = lineup if lineup is not None else optimize_lineup(roster)
    if status is None and rosters:
        status = classify_team_status(roster.roster_id, rosters, currency, storage=storage, engine=engine).status
    ages = [e.age for e in roster.entries if e.player_id in lineup.starter_ids and e.age is not None]
    return RosterSnapshot(
        lineup=lineup,
        weekly_points=lineup.total_projected_points / games_remaining(current_week),
        depth_needs=identify_depth_needs(roster, roster.fmt.starter_slots),
        status=status,
        roster_value=sum(value_for_currency(e.value, currency) or 0 for e in roster.entries),
        avg_starter_age=sum(ages) / len(ages) if ages else None,
    )


def _impact(
    label: str,
    before_roster: ValuedRoster,
    after_roster: ValuedRoster,
    before: RosterSnapshot,
    after_rosters: dict[int, ValuedRoster],
    *,
    current_week: int | None,
    storage=None,
    engine=None,
) -> MoveImpact:
    after = snapshot_roster(after_roster, after_rosters, current_week=current_week, storage=storage, engine=engine)
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
    proposal: TradeProposal,
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    before: RosterSnapshot,
    *,
    current_week: int | None,
    storage=None,
    engine=None,
) -> MoveImpact | None:
    """None when the proposal is below the acceptance bar for previewing —
    a preview of a trade nobody would accept is noise."""
    if ACCEPTANCE_TIERS.index(proposal.acceptance_rating) < ACCEPTANCE_TIERS.index(MIN_ACCEPTANCE_FOR_PREVIEW):
        return None
    give_ids = [e.player_id for e in proposal.give]
    my_after = roster_after_moves(my_roster, add_entries=proposal.receive, remove_player_ids=give_ids)
    after_rosters = dict(rosters)
    after_rosters[my_roster.roster_id] = my_after
    # The counterparty's roster changes too, which matters for the
    # in-league strength ranking behind team status.
    their = next((r for r in rosters.values() if r.owner_username == proposal.target_username), None)
    if their is not None:
        after_rosters[their.roster_id] = roster_after_moves(
            their, add_entries=proposal.give, remove_player_ids=[e.player_id for e in proposal.receive]
        )
    return _impact(
        f"Trade with {proposal.target_team_name or proposal.target_username}", my_roster, my_after, before, after_rosters,
        current_week=current_week, storage=storage, engine=engine,
    )


def preview_add_drop(
    label: str,
    add_entry: RosterEntry,
    drop_player_id: str | None,
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    before: RosterSnapshot,
    *,
    current_week: int | None,
    storage=None,
    engine=None,
) -> MoveImpact:
    my_after = roster_after_moves(
        my_roster, add_entries=[add_entry], remove_player_ids=[drop_player_id] if drop_player_id else []
    )
    after_rosters = dict(rosters)
    after_rosters[my_roster.roster_id] = my_after
    return _impact(label, my_roster, my_after, before, after_rosters, current_week=current_week, storage=storage, engine=engine)
