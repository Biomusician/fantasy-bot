from __future__ import annotations

import pytest
from waiver_fixtures import player

from sleeper_tool.roster_needs import CRITICAL_NEED, WEAK, PositionNeed, RosterNeeds
from sleeper_tool.waiver_acquisition import (
    BYE_COVER,
    DEPTH_ADD,
    IMMEDIATE_STARTER,
    PASS,
    PRIORITY_ADD,
    SPECULATIVE_ADD,
    SPECULATIVE_STASH,
    STRONG_ADD,
    AcquisitionCall,
)
from sleeper_tool.waiver_command_center import (
    MAX_TARGETS,
    WaiverCommandCenter,
    league_mode,
    targets_from_command_center,
)
from sleeper_tool.waiver_drops import DropBoard, DropOption
from sleeper_tool.waiver_engine import MODERATE, MUST_ADD, SEASON_STARTER, SPECULATIVE, STASH, STREAMER
from sleeper_tool.waiver_engine import STRONG_ADD as ENGINE_STRONG_ADD
from sleeper_tool.waiver_evidence import WaiverEvidence
from sleeper_tool.waiver_plan import FAAB_MODE, PRIORITY_MODE, build_plan


def drop_option(pid):
    return DropOption(player(pid, "WR", 1.0), 1.0, 0.0, None, False, ["1.0/wk"])


def call(
    pid,
    *,
    key="upgrade:x",
    strength=STRONG_ADD,
    cls=IMMEDIATE_STARTER,
    drop=None,
    need=None,
    why=(),
    problem="WR upgrade",
    pass_reason=None,
):
    entry = player(pid, "WR", 10.0)
    ev = WaiverEvidence(player_id=pid, name=pid, position="WR", team="KC")
    return AcquisitionCall(
        entry=entry, evidence=ev, cls=cls, strength=strength, problem=problem, problem_positions=("WR",),
        need=PositionNeed(group="WR", label=need) if need else None, structural_gain=2.0, week_gain=0.0,
        entered_slot="WR", drop=drop, problem_key=key, why=list(why), pass_reason=pass_reason,
    )


def command_center(calls, *, plan=None, mode=FAAB_MODE):
    return WaiverCommandCenter(
        mode=mode, claim_week=2, needs=RosterNeeds(groups={}), drops=DropBoard(options=[], protected={}),
        calls=calls, plan=plan if plan is not None else build_plan(calls),
    )


# -- league mode -------------------------------------------------------------------------


def test_faab_is_only_faab_when_sleeper_reports_a_budget():
    assert league_mode({"settings": {"waiver_type": 2, "waiver_budget": 100}}, None) == (FAAB_MODE, None, None)

    mode, note, position = league_mode({"settings": {"waiver_type": 2, "waiver_budget": 0}}, None)
    assert (mode, position) == (None, None)
    assert note == "League waiver settings are not available — no bid or priority advice."


@pytest.mark.parametrize("waiver_type", [0, 1])
def test_priority_needs_my_own_waiver_position(waiver_type):
    league = {"settings": {"waiver_type": waiver_type, "num_teams": 12}}
    assert league_mode(league, {"settings": {"waiver_position": 3}}) == (PRIORITY_MODE, None, (3, 12))

    mode, note, position = league_mode(league, {"settings": {}})
    assert (mode, position) == (None, None)
    assert note == (
        "League runs on waiver priority, but Sleeper did not report your waiver position — no priority advice."
    )
    assert league_mode(league, None)[0] is None


def test_team_count_falls_back_to_the_roster_positions_payload_then_to_zero():
    roster = {"settings": {"waiver_position": 2}}
    no_teams = {"settings": {"waiver_type": 0}, "roster_positions": ["QB", "RB", "WR"]}
    assert league_mode(no_teams, roster)[2] == (2, 3)
    assert league_mode({"settings": {"waiver_type": 0}}, roster)[2] == (2, 0)


def test_an_unknown_or_missing_waiver_setting_says_so():
    assert league_mode({}, None)[1] == "League waiver settings are not available — no bid or priority advice."
    assert league_mode({"settings": None}, None)[1] is not None
    assert league_mode({"settings": {"waiver_type": 9}}, None)[0] is None


def test_a_waiver_position_of_zero_is_not_a_reported_position():
    league = {"settings": {"waiver_type": 1, "num_teams": 10}}
    assert league_mode(league, {"settings": {"waiver_position": 0}})[0] is None


# -- targets ----------------------------------------------------------------------------------


def test_plan_claims_come_first_in_submission_order_and_carry_the_plans_drop():
    planned = call("planned", key="upgrade:x", drop=drop_option("d1"))
    other = call("other", key="cover:2:RB", drop=drop_option("d2"))
    cc = command_center([planned, other])
    targets = targets_from_command_center(cc, {"planned": 42})
    assert [t.player_id for t in targets] == ["planned", "other"]
    assert [t.drop_candidate.player_id for t in targets] == ["d1", "d2"]
    assert targets[0].trend_count == 42 and targets[1].trend_count == 0


def test_a_claim_the_plan_had_no_room_for_still_becomes_a_target():
    planned = [call(f"c{i}", key=f"key{i}", drop=drop_option(f"d{i}")) for i in range(5)]
    cc = command_center(planned)
    assert len(cc.plan.groups) == 4  # MAX_GROUPS
    targets = targets_from_command_center(cc, {})
    assert [t.player_id for t in targets] == ["c0", "c1", "c2", "c3", "c4"]


def test_passes_never_become_targets():
    claim = call("claim", drop=drop_option("d1"))
    passed = call("passed", key="other", strength=PASS, pass_reason="room is full")
    cc = command_center([claim, passed])
    assert [t.player_id for t in targets_from_command_center(cc, {})] == ["claim"]


@pytest.mark.parametrize(
    "strength,tier",
    [(PRIORITY_ADD, MUST_ADD), (STRONG_ADD, ENGINE_STRONG_ADD), (DEPTH_ADD, MODERATE), (SPECULATIVE_ADD, SPECULATIVE)],
)
def test_strength_maps_onto_the_waiver_engines_own_tier_vocabulary(strength, tier):
    cc = command_center([call("a", strength=strength, drop=drop_option("d1"))])
    assert targets_from_command_center(cc, {})[0].priority_tier == tier


@pytest.mark.parametrize(
    "cls,horizon",
    [(IMMEDIATE_STARTER, SEASON_STARTER), (BYE_COVER, STREAMER), (SPECULATIVE_STASH, STASH), (None, STASH)],
)
def test_class_maps_onto_a_horizon(cls, horizon):
    cc = command_center([call("a", cls=cls, drop=drop_option("d1"))])
    assert targets_from_command_center(cc, {})[0].horizon == horizon


def test_the_reason_is_the_first_two_why_lines_or_the_problem_itself():
    with_why = call("a", why=["enters your lineup at WR", "WR is Weak", "Ballers #3"], drop=drop_option("d1"))
    assert targets_from_command_center(command_center([with_why]), {})[0].reason == (
        "enters your lineup at WR; WR is Weak"
    )
    bare = call("b", why=[], problem="Week 2 RB cover", drop=drop_option("d1"))
    assert targets_from_command_center(command_center([bare]), {})[0].reason == "Week 2 RB cover"


@pytest.mark.parametrize("need,fills", [(CRITICAL_NEED, True), (WEAK, True), ("Adequate", False), (None, False)])
def test_fills_need_follows_the_groups_own_label(need, fills):
    cc = command_center([call("a", need=need, drop=drop_option("d1"))])
    target = targets_from_command_center(cc, {})[0]
    assert target.fills_need is fills
    assert target.need_rank is None


def test_no_more_than_eight_targets_are_produced():
    calls = [call(f"c{i}", key=f"key{i}", drop=drop_option(f"d{i}")) for i in range(12)]
    assert len(targets_from_command_center(command_center(calls), {})) == MAX_TARGETS


def test_the_target_carries_the_players_own_identity_and_value():
    entry_call = call("a", drop=drop_option("d1"))
    target = targets_from_command_center(command_center([entry_call]), {})[0]
    assert (target.name, target.position, target.team) == ("a", "WR", "KC")
    assert target.value is entry_call.entry.value
    assert target.suggested_faab_pct is None  # the window carries the bid, not the target


def test_call_for_finds_a_call_by_player_id():
    a = call("a", drop=drop_option("d1"))
    cc = command_center([a])
    assert cc.call_for("a") is a
    assert cc.call_for("nobody") is None
