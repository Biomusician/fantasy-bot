"""Waiver Command Center — one league's waiver decision, assembled.

This is orchestration, not logic: every judgment lives in its own module
and is only called here, in order:

  roster_needs        how badly each position group needs help
  waiver_drops        who is cheapest to drop, and who is protected
  waiver_sources      what the outside boards say (loaded once per run)
  waiver_evidence     each candidate's matrix row and descriptive labels
  waiver_acquisition  what each candidate is worth to THIS roster
  faab_window         the bid range and recommended bid
  waiver_plan         the ordered, dependency-aware claim sequence

report_data builds one per redraft/keeper league (dynasty leagues keep the
existing waiver table) and both renderers read it. It is also where the
league's waiver mode is read: FAAB from Sleeper's settings, priority only
when the league reports a waiver order for my roster — a Yahoo league's
priority is never guessed.
"""
from __future__ import annotations

import copy
from collections.abc import Collection
from dataclasses import dataclass, field

from sleeper_tool.faab_strategy import FAAB_WAIVER_TYPE, FaabContext
from sleeper_tool.faab_window import FaabWindow, WindowFacts, build_window
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup
from sleeper_tool.replacement_value import ReplacementMarket
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster, player_name
from sleeper_tool.roster_needs import RosterNeeds, assess_roster_needs
from sleeper_tool.waiver_acquisition import (
    AcquisitionCall,
    LeagueWaiverContext,
    assess_all,
    candidate_universe,
    drop_rejection,
    pick_drop,
)
from sleeper_tool.waiver_drops import DropBoard, build_drop_board, startable_depth
from sleeper_tool.waiver_evidence import WaiverEvidence, build_evidence
from sleeper_tool.waiver_plan import FAAB_MODE, PRIORITY_MODE, WaiverPlan, build_plan
from sleeper_tool.waiver_sources import BALLERS, BOONE, FP_ROS, LOADED, ROTOBALLER, WaiverSources
from sleeper_tool.waiver_engine import MODERATE, MUST_ADD, SEASON_STARTER, SPECULATIVE, STASH, STREAMER, WaiverTarget
from sleeper_tool.waiver_engine import STRONG_ADD as ENGINE_STRONG_ADD
from sleeper_tool.waiver_acquisition import BYE_COVER, DEPTH_ADD, IMMEDIATE_STARTER, PRIORITY_ADD, SPECULATIVE_ADD, STRONG_ADD
from sleeper_tool.waiver_acquisition import STREAMER as STREAMER_CLASS
from sleeper_tool.roster_needs import CRITICAL_NEED, WEAK

COMMAND_CENTER_KINDS = ("redraft", "keeper")
MATRIX_MAX_ROWS = 15
SKILL = ("QB", "RB", "WR", "TE")
PRIORITY_WAIVER_TYPES = (0, 1)  # Sleeper: rolling priority, reverse standings


@dataclass
class WaiverCommandCenter:
    mode: str | None  # FAAB_MODE / PRIORITY_MODE; None when the league's waiver state isn't available
    claim_week: int | None
    needs: RosterNeeds
    drops: DropBoard
    calls: list[AcquisitionCall]
    plan: WaiverPlan
    windows: dict[str, FaabWindow] = field(default_factory=dict)  # by player_id, after the plan's bid ordering
    matrix: list[AcquisitionCall] = field(default_factory=list)
    source_lines: list[str] = field(default_factory=list)
    priority_position: tuple[int, int] | None = None  # (my waiver position, teams) in a priority league
    faab_context: FaabContext | None = None  # this league's budgets, for the outbid and anchor facts
    mode_note: str | None = None
    remaining_budget: int | None = None

    def call_for(self, player_id: str) -> AcquisitionCall | None:
        return next((c for c in self.calls if c.player_id == player_id), None)


def league_mode(league_data: dict, my_raw_roster: dict | None) -> tuple[str | None, str | None, tuple[int, int] | None]:
    """(mode, note, priority position). FAAB when Sleeper says FAAB with a
    budget; PRIORITY only when Sleeper reports a priority league AND my
    roster's waiver position — otherwise no mode, and the note says why."""
    settings = league_data.get("settings") or {}
    waiver_type = settings.get("waiver_type")
    if waiver_type == FAAB_WAIVER_TYPE and settings.get("waiver_budget"):
        return FAAB_MODE, None, None
    if waiver_type in PRIORITY_WAIVER_TYPES:
        position = ((my_raw_roster or {}).get("settings") or {}).get("waiver_position")
        teams = settings.get("num_teams") or len(league_data.get("roster_positions") or []) or None
        if position:
            return PRIORITY_MODE, None, (int(position), int(teams) if teams else 0)
        return None, "League runs on waiver priority, but Sleeper did not report your waiver position — no priority advice.", None
    return None, "League waiver settings are not available — no bid or priority advice.", None


def _expert_entries(
    sources: WaiverSources, rostered_ids: set[str], known_ids: set[str], all_players: dict[str, dict], engine, roster: ValuedRoster,
) -> list[RosterEntry]:
    """Available players a waiver board names who aren't in the projected
    free-agent pool (no projection yet, a rookie, a practice-squad call-up).
    They must still be evaluated: a board naming him is the signal."""
    out: list[RosterEntry] = []
    for pid in {*sources.ballers_by_id, *sources.rotoballer_by_id}:
        if pid in rostered_ids or pid in known_ids:
            continue
        pdata = all_players.get(pid) or {}
        if pdata.get("position") not in SKILL or not pdata.get("team"):
            continue
        name = player_name(pdata)
        out.append(RosterEntry(
            player_id=pid, name=name, position=pdata.get("position"), team=pdata.get("team"), age=pdata.get("age"),
            years_exp=pdata.get("years_exp"), injury_status=pdata.get("injury_status"), status=pdata.get("status"),
            is_starter=False, is_taxi=False, is_reserve=False, value=engine.value_player(name, roster.fmt, pdata.get("position")),
        ))
    return out


def build_command_center(
    *,
    roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    lineup: LineupResult,
    lineups: dict[int, LineupResult],
    market: ReplacementMarket | None,
    free_agents: Collection[RosterEntry],
    all_players: dict[str, dict],
    rostered_ids: set[str],
    engine,
    sources: WaiverSources,
    faab_ctx: FaabContext,
    league_data: dict,
    my_raw_roster: dict | None,
    current_week: int | None,
    claim_week: int | None,
    trending_ids: Collection[str] = (),
    role_labels: dict[str, str] | None = None,
    role_market: dict[str, str] | None = None,
    trade_piece_ids: Collection[str] = (),
    open_spots: int = 0,
) -> WaiverCommandCenter:
    num_teams = len([r for r in rosters.values()]) or 1
    # The claim-week lineup is a THIS-WEEK lineup, so a player Sleeper has
    # ruled Out does not fill a slot in it. Leaving him in is how a FLEX the
    # roster cannot actually field reads as "already covered", which hides
    # the very claim the waiver run exists to find.
    week_lineup = (
        optimize_lineup(roster, nfl_week=claim_week, exclude_game_day_out=True)
        if claim_week is not None else None
    )
    skill_fas = [fa for fa in free_agents if fa.position in SKILL]
    needs = assess_roster_needs(
        roster, lineup=lineup, lineups=lineups, market=market, free_agents=skill_fas,
        current_week=current_week, claim_week=claim_week,
    )
    fp_ros = sources.fp_ros_for(roster.fmt.ppr)

    def ros_pos_rank(e: RosterEntry) -> int | None:
        view = fp_ros.get(e.player_id)
        return view.pos_rank if view is not None else None

    drops = build_drop_board(
        roster, lineup=lineup, week_lineup=week_lineup, num_teams=num_teams, ros_pos_rank=ros_pos_rank,
        trade_piece_ids=trade_piece_ids, current_week=current_week, open_spots=open_spots,
        reserve_slots=int((league_data.get("settings") or {}).get("reserve_slots") or 0),
    )
    extras = _expert_entries(sources, rostered_ids, {fa.player_id for fa in free_agents}, all_players, engine, roster)
    pool = skill_fas + extras
    expert_rank: dict[str, int] = {}
    for pid, row in sources.ballers_by_id.items():
        expert_rank[pid] = min(expert_rank.get(pid, 999), row.rank)
    for pid, row in sources.rotoballer_by_id.items():
        expert_rank[pid] = min(expert_rank.get(pid, 999), row.rank)
    universe = candidate_universe(pool, expert_rank=expert_rank, trending_ids=trending_ids)

    present = set()
    if sources.has(BALLERS):
        present.add(BALLERS)
    if sources.has(ROTOBALLER):
        present.add(ROTOBALLER)
    if any(s.label == LOADED and s.source.startswith(FP_ROS) for s in sources.statuses) and fp_ros:
        present.add(FP_ROS)
    if sources.boone_for(roster.fmt.ppr):
        present.add(BOONE)
    startable = startable_depth(roster, num_teams)
    evidence: dict[str, WaiverEvidence] = {
        c.player_id: build_evidence(
            c, sources=sources, ppr=roster.fmt.ppr, num_teams=num_teams, startable=startable, current_week=current_week,
            scarcity=market.scarcity_of(c.position) if market is not None else None,
            role_label=(role_labels or {}).get(c.player_id), role_market=(role_market or {}).get(c.player_id),
            sources_present=present,
        )
        for c in universe
    }
    ctx = LeagueWaiverContext(
        roster=roster, lineup=lineup, week_lineup=week_lineup, needs=needs, drops=drops, free_agents=pool,
        current_week=current_week, claim_week=claim_week, ros_pos_rank=ros_pos_rank,
    )
    calls = assess_all(universe, evidence, ctx, open_spots=open_spots)

    mode, mode_note, priority_position = league_mode(league_data, my_raw_roster)
    raw_windows: dict[str, FaabWindow] = {}
    if mode == FAAB_MODE:
        for c in calls:
            if not c.is_claim:
                continue
            w = build_window(faab_ctx, WindowFacts(
                strength=c.strength, need=c.need.label if c.need is not None else None, cls=c.cls,
                scarcity=c.evidence.scarcity, alternatives=c.alternatives.label if c.alternatives else None,
                alternatives_count=c.alternatives.count if c.alternatives else 0, disagreement=c.evidence.disagreement,
                projected=c.entry.value.proj_points is not None,
            ), name=c.entry.name, player_id=c.player_id)
            if w is not None:
                raw_windows[c.player_id] = w

    plan = build_plan(
        calls, mode=mode or FAAB_MODE, open_spots=open_spots,
        remaining_budget=faab_ctx.remaining if mode == FAAB_MODE else None,
        window_for=lambda c: copy.deepcopy(raw_windows.get(c.player_id)),
        choose_drop=lambda c, used: pick_drop(c, ctx, exclude_ids=used),
        drop_ok=lambda c, option: drop_rejection(c, option, ctx) is None,
        bid_min=int((league_data.get("settings") or {}).get("waiver_bid_min") or 0),
    )
    windows = dict(raw_windows)
    for claim in plan.claims():
        if claim.window is not None:
            windows[claim.call.player_id] = claim.window

    in_plan = {c.call.player_id for c in plan.claims()}
    matrix = [c for c in calls if c.player_id in in_plan]
    matrix += sorted(
        (c for c in calls if c.player_id not in in_plan and (c.evidence.expert_listed or c in plan.do_not_spend)),
        key=lambda c: (c.evidence.best_waiver_rank or 999, c.entry.name),
    )
    return WaiverCommandCenter(
        mode=mode, claim_week=claim_week, needs=needs, drops=drops, calls=calls, plan=plan, windows=windows,
        matrix=matrix[:MATRIX_MAX_ROWS], source_lines=[s.describe() for s in sources.statuses],
        priority_position=priority_position, mode_note=mode_note, faab_context=faab_ctx,
        remaining_budget=faab_ctx.remaining if mode == FAAB_MODE else None,
    )


# acquisition strength -> the waiver engine's tier vocabulary every other
# consumer already speaks (Best Moves promotes Must Add, previews run for
# Must/Strong Add, the ledger records the tier).
TIER_FOR_STRENGTH = {PRIORITY_ADD: MUST_ADD, STRONG_ADD: ENGINE_STRONG_ADD, DEPTH_ADD: MODERATE, SPECULATIVE_ADD: SPECULATIVE}
MAX_TARGETS = 8


def _horizon(cls: str | None) -> str:
    if cls == IMMEDIATE_STARTER:
        return SEASON_STARTER
    if cls in (BYE_COVER, STREAMER_CLASS):
        return STREAMER
    return STASH


def targets_from_command_center(cc: WaiverCommandCenter, trend_counts: dict[str, int]) -> list[WaiverTarget]:
    """The plan's claims, in submission order, then any remaining claim-worthy
    calls, as WaiverTargets carrying the plan's drop. Passes never become
    targets (they live in the plan's Do Not Spend list)."""
    ordered: list[tuple[AcquisitionCall, RosterEntry | None]] = [(c.call, c.drop) for c in cc.plan.claims()]
    seen = {call.player_id for call, _ in ordered}
    ordered += [(call, call.drop.entry if call.drop is not None else None) for call in cc.calls if call.is_claim and call.player_id not in seen]
    targets: list[WaiverTarget] = []
    for call, drop in ordered[:MAX_TARGETS]:
        e = call.entry
        lead = call.why[:2] or [call.problem]
        targets.append(WaiverTarget(
            player_id=e.player_id, name=e.name, position=e.position, team=e.team,
            trend_count=trend_counts.get(e.player_id, 0), value=e.value,
            fills_need=call.need is not None and call.need.label in (CRITICAL_NEED, WEAK), need_rank=None,
            reason="; ".join(lead), priority_tier=TIER_FOR_STRENGTH[call.strength], horizon=_horizon(call.cls),
            drop_candidate=drop, suggested_faab_pct=None,
        ))
    return targets
