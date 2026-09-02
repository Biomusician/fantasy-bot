"""Contender Insurance — for a contending roster, which single starter
injury would crater the lineup, and is there a free agent who'd soften it?

A contender's highest-value move is sometimes not adding value but
removing a catastrophic depth failure, especially late in the season when
the waiver pool has thinned. The test, per projected starter:

  1. Remove him and re-optimize the lineup (shared lineup_optimizer, so
     cascading reshuffles — the FLEX sliding into his slot, etc. — are
     handled exactly, not by looking at one backup).
  2. Effective replacement = his projection minus the lineup's total
     drop. FRAGILE_REPLACEMENT_RATIO: if that's under 65% of what he
     produces, the slot is fragile.
  3. Fragile only matters if it's fixable from waivers: the best free
     agent who, added to the roster-without-him, restores at least
     INSURANCE_MIN_IMPROVEMENT (15%) of the replacement production. The
     top few free agents by projection at his position are tried — the
     best one dominates in practice, the rest guard against an
     eligibility edge case.

Output is capped at MAX_INSURANCE_PER_TEAM, most fragile first, and fed
into the waiver list as "Insurance" targets, ranked below the normal
high-upside adds unless the league's trade deadline has passed (at
which point depth can no longer be bought by trade). Contenders only —
a rebuild should be spending roster spots on upside, not on backups.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.config import LeagueInfo
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, optimize_lineup_after_moves, projection_of
from sleeper_tool.roster_analysis import SKILL_POSITIONS, RosterEntry, ValuedRoster, player_name
from sleeper_tool.storage import Storage
from sleeper_tool.team_status import CONTENDER
from sleeper_tool.trade_engine import identify_needs, value_currency
from sleeper_tool.valuation import PlayerValue, ValuationEngine, games_remaining
from sleeper_tool.waiver_engine import STASH, WaiverTarget, _find_drop_candidate, get_rostered_player_ids

FRAGILE_REPLACEMENT_RATIO = 0.65  # effective replacement / starter projection under this = Fragile
INSURANCE_MIN_IMPROVEMENT = 0.15  # free agent must restore at least this fraction of the replacement production
MAX_INSURANCE_PER_TEAM = 2
FREE_AGENTS_TRIED_PER_POSITION = 5

FRAGILE = "Fragile"
INSURANCE_TIER = "Insurance"  # a WaiverTarget.priority_tier value alongside waiver_engine's own tiers


@dataclass
class FreeAgent:
    player_id: str
    name: str
    position: str | None
    team: str | None
    years_exp: int | None
    value: PlayerValue

    def as_entry(self) -> RosterEntry:
        return RosterEntry(
            player_id=self.player_id, name=self.name, position=self.position, team=self.team, age=None,
            years_exp=self.years_exp, injury_status=None, status="Active",
            is_starter=False, is_taxi=False, is_reserve=False, value=self.value,
        )


@dataclass
class InsuranceRecommendation:
    starter: RosterEntry
    slot: str
    starter_projection: float
    replacement_projection: float  # effective, after re-optimizing without him
    candidate: FreeAgent
    restored_projection: float  # effective replacement with the candidate added
    label: str = FRAGILE

    @property
    def replacement_ratio(self) -> float:
        return self.replacement_projection / self.starter_projection if self.starter_projection else 0.0


def free_agent_candidates(storage: Storage, engine: ValuationEngine, league: LeagueInfo, roster: ValuedRoster) -> list[FreeAgent]:
    """Every unrostered, active, NFL-employed skill-position player with a
    projection in this league's format. A few hundred value lookups per
    league — cheap, and no network."""
    rostered = get_rostered_player_ids(storage, league)
    pool: list[FreeAgent] = []
    for pid, pdata in storage.get_all_players().items():
        if pid in rostered or pdata.get("position") not in SKILL_POSITIONS or not pdata.get("team"):
            continue
        if pdata.get("status") not in (None, "Active"):
            continue
        name = player_name(pdata)
        value = engine.value_player(name, roster.fmt, pdata.get("position"))
        if value.proj_points is None:
            continue
        pool.append(FreeAgent(pid, name, pdata.get("position"), pdata.get("team"), pdata.get("years_exp"), value))
    return pool


def identify_fragile_starters(
    roster: ValuedRoster,
    free_agents: list[FreeAgent],
    *,
    team_status: str,
    lineup: LineupResult | None = None,
    max_recommendations: int = MAX_INSURANCE_PER_TEAM,
) -> list[InsuranceRecommendation]:
    if team_status != CONTENDER or not roster.entries:
        return []
    lineup = lineup if lineup is not None else optimize_lineup(roster)
    by_id = {e.player_id: e for e in roster.entries}
    baseline = lineup.total_projected_points
    fa_by_position: dict[str, list[FreeAgent]] = {}
    for fa in sorted(free_agents, key=lambda f: -projection_of(f.as_entry())):
        fa_by_position.setdefault(fa.position or "", []).append(fa)

    found: list[InsuranceRecommendation] = []
    for a in lineup.assignments:
        if a.projection <= 0:
            continue
        without = optimize_lineup(roster, excluded_player_ids={a.player_id})
        replacement = a.projection - (baseline - without.total_projected_points)
        if replacement >= FRAGILE_REPLACEMENT_RATIO * a.projection:
            continue

        best: tuple[float, FreeAgent] | None = None
        for fa in fa_by_position.get(a.position or "", [])[:FREE_AGENTS_TRIED_PER_POSITION]:
            with_fa = optimize_lineup_after_moves(roster, add_entries=[fa.as_entry()], excluded_player_ids={a.player_id})
            restored = a.projection - (baseline - with_fa.total_projected_points)
            if best is None or restored > best[0]:
                best = (restored, fa)
        if best is None:
            continue
        restored, fa = best
        improvement = restored - replacement
        needed = INSURANCE_MIN_IMPROVEMENT * replacement if replacement > 0 else 0.0
        if improvement <= needed:
            continue
        found.append(
            InsuranceRecommendation(
                starter=by_id[a.player_id], slot=a.slot, starter_projection=a.projection,
                replacement_projection=replacement, candidate=fa, restored_projection=restored,
            )
        )

    found.sort(key=lambda r: r.replacement_ratio)  # most fragile first
    return found[:max_recommendations]


def merge_insurance_into_waiver_targets(
    targets: list[WaiverTarget],
    recommendations: list[InsuranceRecommendation],
    my_roster: ValuedRoster,
    *,
    current_week: int | None,
    deadline_passed: bool,
    clog_ids=(),
) -> list[WaiverTarget]:
    """The thin hook into the waiver list. Each insurance candidate becomes
    an "Insurance"-tier WaiverTarget with its own paired drop; a candidate
    who is ALREADY a trending target just gets the insurance note added
    to that row instead of a duplicate. Insurance ranks below the normal
    high-upside adds — unless the trade deadline has passed, when depth
    can no longer be bought and the fragility is the more urgent item.
    """
    if not recommendations:
        return targets
    currency = value_currency(my_roster)
    needs = identify_needs(my_roster)[:2]
    per_week = games_remaining(current_week)
    existing = {t.player_id: t for t in targets}
    taken_drops = {t.drop_candidate.player_id for t in targets if t.drop_candidate}

    # One row per candidate: the same free agent is often the best cover
    # for two starters at once (the lone decent RB on waivers), and two
    # rows telling the user to add him are one row too many.
    by_candidate: dict[str, list[InsuranceRecommendation]] = {}
    for rec in recommendations:
        by_candidate.setdefault(rec.candidate.player_id, []).append(rec)

    insurance_rows: list[WaiverTarget] = []
    for recs in by_candidate.values():
        candidate = recs[0].candidate
        covers = " and ".join(
            f"{r.starter.name} ({r.slot}, your lineup keeps only {r.replacement_ratio:.0%} of his production without him)"
            for r in recs
        )
        restored = max(r.restored_projection for r in recs)
        note = f"insurance for {covers}; {candidate.name} would restore that slot to ~{restored / per_week:.1f}/wk"
        row = existing.get(candidate.player_id)
        if row is not None:
            row.reason = f"{row.reason}; also {note}"
            continue
        drop = _find_drop_candidate(my_roster, candidate.position, needs, currency, exclude_ids=taken_drops, preferred_ids=clog_ids)
        if drop is not None:
            taken_drops.add(drop.player_id)
        insurance_rows.append(
            WaiverTarget(
                player_id=candidate.player_id, name=candidate.name, position=candidate.position,
                team=candidate.team, trend_count=0, value=candidate.value, fills_need=False, need_rank=None,
                reason=note[0].upper() + note[1:], priority_tier=INSURANCE_TIER, horizon=STASH, drop_candidate=drop,
            )
        )
    return insurance_rows + targets if deadline_passed else targets + insurance_rows
