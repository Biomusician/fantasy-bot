from __future__ import annotations

import pytest

from sleeper_tool.faab_strategy import NORMAL as NORMAL_POSTURE
from sleeper_tool.faab_strategy import PRESERVE, PRIORITY_SPEND, FaabContext
from sleeper_tool.faab_window import (
    MIN_TOTAL_MULTIPLIER,
    NON_PRIORITY_MAX_PCT,
    POINT_LUXURY,
    POINT_NEUTRAL,
    POINT_TOP,
    POINT_WEAK,
    PRESERVE_MAX_PCT_OF_BUDGET,
    PRIORITY_MIN_HIGH_PCT,
    ROLE_MOVES_FAAB,
    SHIFT_BROAD_CONVICTION,
    STARTER_PREMIUM_MIN_GAIN,
    FaabWindow,
    WindowFacts,
    advice_from_window,
    build_window,
    outbid_text,
)
from sleeper_tool.replacement_value import ABUNDANT, SCARCE, VERY_SCARCE
from sleeper_tool.roster_needs import CRITICAL_NEED, STRONG, SURPLUS, WEAK
from sleeper_tool.waiver_acquisition import (
    BYE_COVER,
    DEPTH_ADD,
    MAJOR_GAIN,
    IMMEDIATE_STARTER,
    PASS,
    PRIORITY_ADD,
    SPECULATIVE_ADD,
    SPECULATIVE_STASH,
    STRONG_ADD,
    UPSIDE_BENCH,
)
from sleeper_tool.waiver_alternatives import FEW, MANY, SOME, UNIQUE
from sleeper_tool.waiver_engine import MUST_ADD


def ctx(**kw) -> FaabContext:
    base = {"waiver_type": 2, "budget": 100, "my_used": 0}
    base.update(kw)
    return FaabContext(**base)


def facts(strength=DEPTH_ADD, **kw) -> WindowFacts:
    return WindowFacts(strength=strength, **kw)


def win(strength=DEPTH_ADD, *, context=None, **kw) -> FaabWindow:
    return build_window(context or ctx(), facts(strength, **kw), name="Target", player_id="p1")


def band(window: FaabWindow) -> tuple[int, int, int]:
    return window.low, window.high, window.recommended


# -- when there is no window at all ------------------------------------------------------


def test_a_pass_a_pre_draft_league_and_a_non_faab_league_all_get_no_window():
    assert build_window(ctx(), facts(PASS)) is None
    assert build_window(ctx(pre_draft=True), facts()) is None
    assert build_window(ctx(waiver_type=0), facts()) is None
    assert build_window(ctx(budget=None), facts()) is None


def test_an_empty_budget_is_a_zero_window_not_an_absent_one():
    window = build_window(ctx(my_used=100), facts(PRIORITY_ADD))
    assert band(window) == (0, 0, 0)
    assert window.posture == PRESERVE
    assert window.reasons == ["out of FAAB — $0 claims only"]
    assert window.text() == "FAAB $0 · recommend $0"


# -- base bands ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strength,expected",
    [(PRIORITY_ADD, (14, 24, 19)), (STRONG_ADD, (7, 14, 10)), (DEPTH_ADD, (3, 8, 6)), (SPECULATIVE_ADD, (1, 4, 2))],
)
def test_base_band_is_a_share_of_remaining_by_strength(strength, expected):
    window = win(strength)
    assert band(window) == expected
    assert window.reasons[0].endswith("% of remaining")
    assert window.posture == NORMAL_POSTURE


def test_the_band_is_a_share_of_what_is_left_not_of_the_season_budget():
    assert band(win(PRIORITY_ADD, context=ctx(my_used=60))) == (5, 10, 8)  # 14-24% of $40


def test_a_window_never_exceeds_what_is_left():
    window = win(PRIORITY_ADD, context=ctx(my_used=97), need=CRITICAL_NEED, scarcity=VERY_SCARCE)
    assert window.high <= 3 and window.low <= 3 and window.recommended <= 3


# -- shifts --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,reason,expected",
    [
        ({"need": CRITICAL_NEED}, "Critical Need ×1.35", (4, 11)),
        ({"need": WEAK}, "Weak room ×1.15", (3, 10)),
        ({"need": STRONG}, "Strong room ×0.7", (2, 6)),
        ({"need": SURPLUS}, "Surplus room ×0.55", (1, 5)),
        ({"cls": IMMEDIATE_STARTER, "gain_per_week": 2.5}, "Immediate Starter (+2.5/wk) ×1.1", (3, 9)),
        ({"broad_conviction": True}, "every board recommends him ×1.2", (3, 10)),
        ({"scarcity": VERY_SCARCE}, "Very Scarce position ×1.25", (3, 10)),
        ({"scarcity": SCARCE}, "Scarce position ×1.1", (3, 9)),
        ({"scarcity": ABUNDANT}, "Abundant position ×0.75", (2, 6)),
        ({"alternatives": UNIQUE}, "Unique Opportunity ×1.25", (3, 10)),
        ({"alternatives": FEW}, "Few Alternatives ×1.1", (3, 9)),
        ({"alternatives": SOME}, "Some Alternatives ×0.9", (2, 8)),
        ({"alternatives": MANY}, "Many Alternatives ×0.65", (1, 6)),
        ({"cls": UPSIDE_BENCH}, "speculative add ×0.75", (2, 6)),
        ({"cls": SPECULATIVE_STASH}, "speculative add ×0.75", (2, 6)),
    ],
)
def test_every_shift_is_applied_to_both_ends_and_named(kwargs, reason, expected):
    window = win(**kwargs)
    assert reason in window.reasons
    assert (window.low, window.high) == expected


def test_a_strong_or_surplus_room_never_pulls_down_an_immediate_starter():
    window = win(need=SURPLUS, cls=IMMEDIATE_STARTER, gain_per_week=MAJOR_GAIN)
    assert not any("Surplus room" in r for r in window.reasons)
    assert f"Immediate Starter (+{MAJOR_GAIN:.1f}/wk) ×1.1" in window.reasons


def test_the_immediate_starter_premium_is_paid_for_a_real_upgrade_not_a_rounding_error():
    thin = win(cls=IMMEDIATE_STARTER, gain_per_week=STARTER_PREMIUM_MIN_GAIN - 0.1)
    assert not any("Immediate Starter" in r for r in thin.reasons)
    real = win(cls=IMMEDIATE_STARTER, gain_per_week=STARTER_PREMIUM_MIN_GAIN)
    assert any("Immediate Starter" in r for r in real.reasons)
    assert real.high > thin.high


def test_a_board_sweep_is_the_one_market_fact_the_window_has():
    plain = win(STRONG_ADD)
    swept = win(STRONG_ADD, broad_conviction=True)
    assert swept.high > plain.high
    assert f"every board recommends him ×{SHIFT_BROAD_CONVICTION:g}" in swept.reasons


def test_being_the_best_player_on_an_abundant_wire_is_not_also_scarcity():
    # The market is Abundant BECAUSE of him; charging the up-shift and the
    # down-shift for one fact nets to noise dressed as analysis.
    both = win(scarcity=ABUNDANT, alternatives=UNIQUE)
    assert not any("Unique Opportunity" in r for r in both.reasons)
    assert band(both) == band(win(scarcity=ABUNDANT))


def test_stacked_cuts_are_floored_so_the_bottom_of_the_scale_stays_meaningful():
    window = win(need=SURPLUS, scarcity=ABUNDANT, alternatives=MANY, cls=SPECULATIVE_STASH)
    assert f"floored at ×{MIN_TOTAL_MULTIPLIER:g} of the base band" in window.reasons
    # 0.55 x 0.75 x 0.65 x 0.75 = 0.20 unfloored; the floor keeps the band real.
    assert (window.low, window.high) == (1, 4)


def test_late_season_pulls_down_a_stash_and_leaves_a_real_add_alone():
    # A budget that expires unspent is worth nothing, so the thing to stop
    # paying for in week 12 is the lottery ticket, not this week's starter.
    late = ctx(current_week=12, playoff_week_start=15)
    stash = win(SPECULATIVE_ADD, context=late, cls=SPECULATIVE_STASH)
    assert "3 weeks to the playoffs ×0.6" in stash.reasons
    assert (stash.low, stash.high) == (1, 2)

    for strength in (PRIORITY_ADD, STRONG_ADD, DEPTH_ADD):
        assert not any("weeks to the playoffs ×" in r for r in win(strength, context=late).reasons)

    earlier = ctx(current_week=11, playoff_week_start=15)
    assert not any(
        "weeks to the playoffs ×" in r
        for r in win(SPECULATIVE_ADD, context=earlier, cls=SPECULATIVE_STASH).reasons
    )


def test_an_unprojected_player_never_gets_the_scarcity_of_alternatives_up_shift():
    for label in (UNIQUE, FEW):
        window = win(alternatives=label, projected=False)
        assert not any("×1.25" in r or "×1.1" in r for r in window.reasons)
        assert (window.low, window.high) == (3, 8)
    # A down-shift still applies: plenty of substitutes is measurable either way.
    assert "Many Alternatives ×0.65" in win(alternatives=MANY, projected=False).reasons


def test_source_disagreement_widens_the_window_instead_of_moving_it():
    plain = win(STRONG_ADD)
    split = win(STRONG_ADD, disagreement=True)
    assert split.low < plain.low and split.high > plain.high
    assert "sources disagree: window widened (low ×0.8, high ×1.1)" in split.reasons


def test_a_split_does_not_raise_the_bid_it_only_widens_what_is_reasonable():
    # A widened band whose recommendation sits at the same fraction of it
    # would mean "the experts disagree, so pay more", which is backwards.
    for kwargs in ({}, {"need": CRITICAL_NEED}, {"cls": SPECULATIVE_STASH}):
        plain = win(STRONG_ADD, **kwargs)
        split = win(STRONG_ADD, disagreement=True, **kwargs)
        assert split.recommended <= plain.recommended


def test_a_role_label_never_moves_the_money():
    assert ROLE_MOVES_FAAB is False
    plain = win(STRONG_ADD)
    ahead = win(STRONG_ADD, role_market="Role Ahead of Market")
    assert band(plain) == band(ahead)
    assert plain.reasons == ahead.reasons


# -- posture guardrails ----------------------------------------------------------------------


def test_preserve_caps_the_window_as_a_share_of_the_season_budget():
    # Capping a share of what is LEFT means a manager down to $10 can never
    # bid $2 for a starter; FAAB has no salvage value, so the cap is $3 of
    # the $100 he started with.
    thin = ctx(my_used=90)  # $10 left of $100
    window = win(PRIORITY_ADD, context=thin)
    assert window.posture == PRESERVE
    assert window.high == PRESERVE_MAX_PCT_OF_BUDGET
    assert band(window) == (1, 3, 2)


def test_preserve_also_stops_an_urgent_need_from_recommending_the_top_of_its_window():
    thin = ctx(my_used=90)
    window = win(PRIORITY_ADD, context=thin, need=CRITICAL_NEED)
    assert window.posture == PRESERVE
    assert window.point == POINT_NEUTRAL


def test_priority_spend_lifts_the_top_to_at_least_thirty_percent():
    # The shifts leave this band well under 30% on their own; Priority Spend lifts it.
    window = win(PRIORITY_ADD, need=SURPLUS, scarcity=SCARCE, alternatives=SOME, alternatives_count=1)
    assert window.posture == PRIORITY_SPEND
    assert window.high == PRIORITY_MIN_HIGH_PCT  # % of a $100 remaining budget


def test_priority_spend_still_stops_at_sixty_percent_of_remaining():
    window = win(
        PRIORITY_ADD, need=CRITICAL_NEED, cls=IMMEDIATE_STARTER, gain_per_week=MAJOR_GAIN,
        scarcity=VERY_SCARCE, alternatives=UNIQUE, alternatives_count=0, disagreement=True,
    )
    assert window.posture == PRIORITY_SPEND
    assert window.high == 60


def test_nothing_else_may_exceed_thirty_five_percent_of_remaining():
    window = win(
        STRONG_ADD, need=CRITICAL_NEED, cls=IMMEDIATE_STARTER, gain_per_week=MAJOR_GAIN,
        scarcity=VERY_SCARCE, alternatives=UNIQUE, alternatives_count=2, disagreement=True,
    )
    assert window.posture != PRIORITY_SPEND
    assert window.high == NON_PRIORITY_MAX_PCT


def test_the_posture_reasons_are_carried_into_the_windows_own_reasons():
    window = win(PRIORITY_ADD, scarcity=SCARCE, alternatives=UNIQUE, alternatives_count=1)
    assert any("comparable free agent" in r for r in window.reasons)


# -- the recommended point --------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,point",
    [
        ({"need": CRITICAL_NEED}, POINT_TOP),
        ({"cls": IMMEDIATE_STARTER, "gain_per_week": MAJOR_GAIN}, POINT_TOP),
        ({"cls": IMMEDIATE_STARTER, "gain_per_week": 1.2}, POINT_WEAK),
        ({"need": WEAK}, POINT_WEAK),
        ({"cls": UPSIDE_BENCH}, POINT_LUXURY),
        ({"cls": SPECULATIVE_STASH}, POINT_LUXURY),
        ({"cls": BYE_COVER}, POINT_NEUTRAL),
        ({}, POINT_NEUTRAL),
    ],
)
def test_the_recommended_point_follows_the_need_and_the_class(kwargs, point):
    window = win(**kwargs)
    assert window.point == point
    assert window.recommended == max(window.low, min(window.high, round(window.low + point * (window.high - window.low))))


def test_a_critical_need_recommends_near_the_top_and_a_stash_near_the_bottom():
    top = win(PRIORITY_ADD, need=CRITICAL_NEED)
    luxury = win(SPECULATIVE_ADD, cls=SPECULATIVE_STASH)
    assert top.recommended > top.low + (top.high - top.low) / 2
    assert luxury.recommended <= luxury.low + (luxury.high - luxury.low) / 2


# -- opponent budgets ---------------------------------------------------------------------------


def test_outbid_text_is_a_fact_about_budgets():
    assert outbid_text(ctx(), 10) is None  # no other managers reported
    assert outbid_text(ctx(others_used=[95, 96]), 10) == "No other manager has more than $10 remaining"
    assert outbid_text(ctx(others_used=[0, 95, 96]), 10) == "Only 1 manager has more than $10 remaining"
    assert outbid_text(ctx(others_used=[0, 10, 95]), 10) == "2 managers can outbid $10"
    assert outbid_text(ctx(others_used=[0, 10, 20]), 10) == "All 3 other managers can outbid $10"


def test_a_manager_holding_exactly_the_bid_is_not_counted_as_richer():
    assert outbid_text(ctx(others_used=[90]), 10) == "No other manager has more than $10 remaining"


# -- the advice record ---------------------------------------------------------------------------


def test_advice_from_window_carries_the_window_and_the_recommended_bid():
    context = ctx(others_used=[0, 90], league_bids=[3, 5, 9])
    window = win(PRIORITY_ADD, need=CRITICAL_NEED, context=context)
    advice = advice_from_window(context, window, player_id="p1", name="Target", tier=MUST_ADD)
    assert advice.suggested_dollars == window.recommended
    assert (advice.window_low, advice.window_high) == (window.low, window.high)
    assert advice.window_reasons == window.reasons and advice.window_reasons is not window.reasons
    assert advice.posture == window.posture
    assert advice.suggested_pct is None
    assert advice.tier == MUST_ADD and advice.name == "Target"
    assert advice.share_of_remaining_text == (
        f"Recommended bid uses approximately {round(100 * window.recommended / 100)}% of remaining budget "
        f"(${window.recommended} of $100)"
    )
    assert advice.leverage_text == f"Only 1 manager has more than ${window.recommended} remaining"
    assert advice.anchor_text == "Winning bids this season: median $5, max $9 (3 bids)"
    assert advice.window_text == f"${window.low}–{window.high} · recommend ${window.recommended}"


def test_a_bid_far_past_what_this_league_has_ever_paid_gets_a_check_it_note():
    context = ctx(league_bids=[2, 3, 4])
    window = win(PRIORITY_ADD, need=CRITICAL_NEED, context=context)
    advice = advice_from_window(context, window, player_id="p1", name="Target", tier=MUST_ADD)
    assert any("check it is what you meant" in n for n in advice.notes)

    modest = ctx(league_bids=[40, 45, 50])
    window = win(DEPTH_ADD, context=modest)
    advice = advice_from_window(modest, window, player_id="p1", name="Target", tier=MUST_ADD)
    assert advice.notes == []


def test_advice_on_an_empty_budget_says_nothing_about_shares():
    context = ctx(my_used=100)
    window = build_window(context, facts(PRIORITY_ADD))
    advice = advice_from_window(context, window, player_id="p1", name="Target", tier=MUST_ADD)
    assert advice.suggested_dollars == 0
    assert advice.share_of_remaining_text is None
