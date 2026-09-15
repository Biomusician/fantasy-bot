from __future__ import annotations

import pytest
from waiver_fixtures import player

from sleeper_tool.faab_window import FaabWindow
from sleeper_tool.waiver_acquisition import (
    IMMEDIATE_STARTER,
    PASS,
    PRIORITY_ADD,
    SPECULATIVE_ADD,
    STRONG_ADD,
    UPSIDE_BENCH,
    AcquisitionCall,
)
from sleeper_tool.waiver_alternatives import FEW, MANY, SOME, UNIQUE, AlternativeDensity
from sleeper_tool.waiver_drops import DropOption
from sleeper_tool.waiver_evidence import WaiverEvidence
from sleeper_tool.waiver_plan import (
    CONSIDER_PRIORITY,
    FAAB_MODE,
    HOLD_PRIORITY,
    IF_PREVIOUS_FAILS,
    INDEPENDENT,
    MAX_BACKUPS,
    MAX_DO_NOT_SPEND,
    MAX_GROUPS,
    ONLY_IF_DROP_AVAILABLE,
    PRIORITY_MODE,
    USE_PRIORITY,
    build_plan,
    priority_call,
)


def drop_option(pid, *, proj=1.0, cover=0.0, rank=None, calibre=False):
    return DropOption(player(pid, "WR", proj), proj, cover, rank, calibre, [f"{proj:.1f}/wk"])


def call(
    pid,
    key="upgrade:x",
    *,
    strength=STRONG_ADD,
    drop=None,
    rank=None,
    pass_reason=None,
    density=None,
    count=0,
    problem="WR upgrade",
    cls=IMMEDIATE_STARTER,
):
    entry = player(pid, "WR", 10.0)
    ev = WaiverEvidence(player_id=pid, name=pid, position="WR", team="KC", ballers_rank=rank)
    return AcquisitionCall(
        entry=entry, evidence=ev, cls=cls, strength=strength, problem=problem, problem_positions=("WR",),
        need=None, structural_gain=1.0, week_gain=0.0, entered_slot="WR", drop=drop, problem_key=key,
        pass_reason=pass_reason,
        alternatives=AlternativeDensity(pid, density, count) if density else None,
    )


def window(low=5, high=10, recommended=8):
    return FaabWindow(low, high, recommended, "Normal", ["base"])


def windows_for(mapping):
    return lambda c: mapping.get(c.player_id)


# -- grouping -----------------------------------------------------------------------------


def test_claims_solving_the_same_problem_become_one_ordered_group():
    a, b = call("a", "upgrade:x", drop=drop_option("d1")), call("b", "upgrade:x", drop=drop_option("d2"))
    plan = build_plan([a, b])
    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert [c.order for c in group.claims] == [1, 2]
    assert group.claims[0].dependency == INDEPENDENT
    assert group.claims[1].dependency == IF_PREVIOUS_FAILS and group.claims[1].depends_on == 1
    assert group.stop_after_success is True
    assert plan.top_claim is group.claims[0]
    assert plan.backups_for(group.claims[0]) == [group.claims[1]]


def test_different_problems_are_independent_groups_in_acquisition_order():
    a = call("a", "upgrade:x", drop=drop_option("d1"))
    b = call("b", "cover:5:RB", drop=drop_option("d2"))
    plan = build_plan([a, b])
    assert [g.claims[0].call.player_id for g in plan.groups] == ["a", "b"]
    assert all(g.claims[0].dependency == INDEPENDENT for g in plan.groups)
    assert plan.groups[0].stop_after_success is False


def test_a_lone_group_of_one_has_nothing_to_stop_after():
    plan = build_plan([call("a", drop=drop_option("d1"))])
    assert plan.groups[0].stop_after_success is False
    assert plan.backups_for(plan.top_claim) == []


def test_passes_never_enter_the_plan():
    plan = build_plan([call("a", strength=PASS, pass_reason="no"), call("b", drop=drop_option("d1"))])
    assert [c.call.player_id for c in plan.claims()] == ["b"]


# -- backups and their drop ------------------------------------------------------------------


def test_a_backup_inherits_the_first_choices_drop_so_only_one_can_clear():
    first_drop = drop_option("d1")
    a, b = call("a", drop=first_drop), call("b", drop=drop_option("d2"))
    plan = build_plan([a, b])
    assert [c.drop.player_id for c in plan.claims()] == ["d1", "d1"]


def test_a_backup_whose_pairing_the_guardrail_rejects_is_left_out():
    a = call("a", drop=drop_option("d1"))
    bad, good = call("bad", drop=drop_option("d2")), call("good", drop=drop_option("d3"))
    plan = build_plan([a, bad, good], drop_ok=lambda c, opt: c.entry.player_id != "bad")
    assert [c.call.player_id for c in plan.claims()] == ["a", "good"]
    assert plan.claims()[1].depends_on == 1  # it chains to the claim ahead of it, not to the skipped one


def test_at_most_two_backups_per_group():
    calls = [call(f"c{i}", drop=drop_option(f"d{i}")) for i in range(5)]
    plan = build_plan(calls)
    assert len(plan.groups) == 1
    assert len(plan.groups[0].claims) == 1 + MAX_BACKUPS


def test_backups_chain_to_the_claim_immediately_ahead_of_them():
    calls = [call(f"c{i}", drop=drop_option(f"d{i}")) for i in range(3)]
    plan = build_plan(calls)
    assert [(c.order, c.depends_on) for c in plan.claims()] == [(1, None), (2, 1), (3, 2)]


# -- open roster spots ---------------------------------------------------------------------------


def test_an_open_spot_goes_to_the_first_group_and_carries_no_backups():
    a, b = call("a", "upgrade:x", drop=drop_option("d1")), call("b", "upgrade:x", drop=drop_option("d2"))
    other = call("c", "cover:5:RB", drop=drop_option("d3"))
    plan = build_plan([a, b, other], open_spots=1)
    first = plan.groups[0].claims[0]
    assert first.drop_option is None
    assert first.notes == ["uses an open roster spot — no drop needed"]
    assert len(plan.groups[0].claims) == 1  # no backup can chain to a claim with no drop to share
    assert plan.groups[1].claims[0].drop.player_id == "d3"


def test_two_open_spots_cover_two_groups():
    a = call("a", "upgrade:x", drop=drop_option("d1"))
    b = call("b", "cover:5:RB", drop=drop_option("d2"))
    plan = build_plan([a, b], open_spots=2)
    assert all(c.drop_option is None for c in plan.claims())


# -- one drop, one claim ---------------------------------------------------------------------------


def test_a_second_group_is_given_a_different_drop_when_one_exists():
    shared = drop_option("d1")
    a = call("a", "upgrade:x", drop=shared)
    b = call("b", "cover:5:RB", drop=shared)
    plan = build_plan([a, b], choose_drop=lambda c, used: drop_option("d2"))
    assert [c.drop.player_id for c in plan.claims()] == ["d1", "d2"]
    assert all(c.dependency == INDEPENDENT for c in plan.claims())


def test_sharing_the_only_viable_drop_makes_the_second_claim_conditional():
    shared = drop_option("d1")
    a = call("a", "upgrade:x", drop=shared)
    b = call("b", "cover:5:RB", drop=shared)
    plan = build_plan([a, b], choose_drop=lambda c, used: None)
    second = plan.groups[1].claims[0]
    assert second.dependency == ONLY_IF_DROP_AVAILABLE and second.depends_on == 1
    assert second.notes == ["shares d1 with claim 1 — only processes if that claim fails"]


def test_a_claim_with_no_drop_at_all_is_dropped_from_the_plan_without_burning_an_order():
    a = call("a", "upgrade:x", drop=None)
    b = call("b", "cover:5:RB", drop=drop_option("d2"))
    plan = build_plan([a, b])
    assert [(c.order, c.call.player_id) for c in plan.claims()] == [(1, "b")]


def test_a_claim_judged_against_an_open_spot_asks_for_a_drop_when_the_spot_is_gone():
    a = call("a", "upgrade:x", drop=drop_option("d1"))
    b = call("b", "cover:5:RB", drop=None)  # judged with an open spot in hand
    plan = build_plan([a, b], open_spots=1, choose_drop=lambda c, used: drop_option("d9"))
    assert plan.groups[0].claims[0].drop_option is None  # took the open spot
    assert plan.groups[1].claims[0].drop.player_id == "d9"


# -- caps ------------------------------------------------------------------------------------------


def test_at_most_four_problem_groups_are_planned():
    calls = [call(f"c{i}", f"key{i}", drop=drop_option(f"d{i}")) for i in range(6)]
    plan = build_plan(calls)
    assert len(plan.groups) == MAX_GROUPS
    assert [g.claims[0].call.player_id for g in plan.groups] == ["c0", "c1", "c2", "c3"]


def test_only_one_speculative_group_a_week():
    calls = [
        call("s1", "upside", strength=SPECULATIVE_ADD, cls=UPSIDE_BENCH, drop=drop_option("d1")),
        call("s2", "upside:other", strength=SPECULATIVE_ADD, cls=UPSIDE_BENCH, drop=drop_option("d2")),
        call("real", "upgrade:x", drop=drop_option("d3")),
    ]
    plan = build_plan(calls)
    assert [g.claims[0].call.player_id for g in plan.groups] == ["s1", "real"]
    assert [c.order for c in plan.claims()] == [1, 2]


# -- bids -------------------------------------------------------------------------------------------


def test_a_backup_bids_under_the_claim_ahead_of_it():
    a, b = call("a", drop=drop_option("d1")), call("b", drop=drop_option("d2"))
    plan = build_plan([a, b], window_for=windows_for({"a": window(5, 10, 8), "b": window(5, 10, 8)}))
    backup = plan.claims()[1]
    assert backup.window.recommended == 7
    # The window is what he is worth; only the bid moves, so the reader can see
    # what the cut cost and overrule it if the first choice is already lost.
    assert (backup.window.low, backup.window.high) == (5, 10)
    assert backup.notes == ["worth $8 on its own; bid $7 so claim 1 processes first"]


def test_a_backup_already_under_the_first_choice_is_left_alone():
    a, b = call("a", drop=drop_option("d1")), call("b", drop=drop_option("d2"))
    plan = build_plan([a, b], window_for=windows_for({"a": window(5, 10, 8), "b": window(1, 4, 3)}))
    backup = plan.claims()[1]
    assert (backup.window.low, backup.window.high, backup.window.recommended) == (1, 4, 3)
    assert backup.notes == []


def test_at_the_league_minimum_the_two_claims_can_only_tie_and_the_note_says_so():
    a, b = call("a", drop=drop_option("d1")), call("b", drop=drop_option("d2"))
    plan = build_plan([a, b], window_for=windows_for({"a": window(1, 1, 1), "b": window(1, 3, 2)}), bid_min=1)
    backup = plan.claims()[1]
    assert (backup.window.low, backup.window.high, backup.window.recommended) == (1, 3, 1)
    assert backup.notes == [
        "both claims sit at the $1 minimum — which processes first is the league's tiebreak (waiver order), not this plan"
    ]


def test_independent_bids_over_the_remaining_budget_get_a_note_not_a_re_pricing():
    a = call("a", "upgrade:x", drop=drop_option("d1"))
    b = call("b", "cover:5:RB", drop=drop_option("d2"))
    wins = {"a": window(5, 10, 8), "b": window(5, 10, 8)}
    plan = build_plan([a, b], remaining_budget=10, window_for=windows_for(wins))
    assert plan.notes == [
        "If every independent claim clears, the recommended bids total $16 against $10 left — "
        "the lowest claim in the plan may not be affordable"
    ]
    assert [c.window.recommended for c in plan.claims()] == [8, 8]


def test_no_budget_note_when_the_bids_fit_or_the_league_is_not_faab():
    a = call("a", "upgrade:x", drop=drop_option("d1"))
    wins = {"a": window(5, 10, 8)}
    assert build_plan([a], remaining_budget=10, window_for=windows_for(wins)).notes == []
    assert build_plan([a], mode=PRIORITY_MODE, remaining_budget=None, window_for=windows_for(wins)).notes == []


def test_only_independent_claims_count_towards_the_budget_note():
    a, b = call("a", drop=drop_option("d1")), call("b", drop=drop_option("d2"))
    wins = {"a": window(5, 10, 8), "b": window(5, 10, 8)}
    plan = build_plan([a, b], remaining_budget=10, window_for=windows_for(wins))
    assert plan.notes == []  # b is a backup: the two can never both clear


# -- do not spend -------------------------------------------------------------------------------------


def test_do_not_spend_lists_board_recommended_players_the_tool_passes_on():
    passes = [call(f"p{i}", strength=PASS, pass_reason="room is full", rank=i + 1) for i in range(6)]
    kept = call("k", strength=PASS, pass_reason="room is full", rank=13)  # outside WAIVER_TOP
    no_reason = call("n", strength=PASS, rank=2)
    plan = build_plan([*passes, kept, no_reason, call("claim", drop=drop_option("d1"))])
    assert [c.entry.player_id for c in plan.do_not_spend] == ["p0", "p1", "p2", "p3", "p4"]
    assert len(plan.do_not_spend) == MAX_DO_NOT_SPEND


def test_a_pass_no_board_recommends_is_not_worth_printing():
    plan = build_plan([call("p", strength=PASS, pass_reason="nothing there")])
    assert plan.do_not_spend == []


# -- priority leagues -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strength,density,expected",
    [
        (PRIORITY_ADD, MANY, USE_PRIORITY),
        (STRONG_ADD, UNIQUE, USE_PRIORITY),
        (STRONG_ADD, FEW, USE_PRIORITY),
        (STRONG_ADD, SOME, CONSIDER_PRIORITY),
        (STRONG_ADD, None, CONSIDER_PRIORITY),
        (SPECULATIVE_ADD, UNIQUE, HOLD_PRIORITY),
    ],
)
def test_priority_call(strength, density, expected):
    assert priority_call(call("a", strength=strength, density=density)) == expected


def test_priority_mode_sets_a_priority_call_on_every_claim_and_faab_mode_does_not():
    a, b = call("a", drop=drop_option("d1")), call("b", drop=drop_option("d2"))
    priority = build_plan([a, b], mode=PRIORITY_MODE)
    assert [c.priority_call for c in priority.claims()] == [CONSIDER_PRIORITY, CONSIDER_PRIORITY]
    assert priority.mode == PRIORITY_MODE
    faab = build_plan([a, b], mode=FAAB_MODE)
    assert [c.priority_call for c in faab.claims()] == [None, None]


# -- the claim record ------------------------------------------------------------------------------------


def test_a_claim_exposes_its_name_and_its_drop_entry():
    claim = build_plan([call("a", drop=drop_option("d1"))]).top_claim
    assert claim.name == "a"
    assert claim.drop.player_id == "d1"
    assert build_plan([]).top_claim is None
