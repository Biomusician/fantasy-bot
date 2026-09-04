from conftest import make_entry, make_value

from sleeper_tool.faab_strategy import (
    AFFORDABILITY_NOTE,
    AGGRESSIVE,
    ANCHOR_MIN_BIDS,
    ANCHOR_OVERSHOOT_RATIO,
    FEW_SUBSTITUTES,
    LATE_SEASON_WEEKS_LEFT,
    LOW_REMAINING_PCT,
    MANY_SUBSTITUTES,
    NORMAL,
    PRESERVE,
    PRIORITY_SPEND,
    PRIORITY_SPEND_MAX_PCT_OF_REMAINING,
    ROLE_SURGING,
    SOME_SUBSTITUTES,
    SUBSTITUTE_PERCENTILE_BAND,
    FaabContext,
    TargetFacts,
    advise,
    budget_plan,
    count_substitutes,
    status_note,
)
from sleeper_tool.replacement_value import ABUNDANT, SCARCE, VERY_SCARCE
from sleeper_tool.waiver_engine import MODERATE, MUST_ADD, SEASON_STARTER, STREAMER, STRONG_ADD


def ctx(**kwargs) -> FaabContext:
    base = {"waiver_type": 2, "budget": 100, "my_used": 0}
    base.update(kwargs)
    return FaabContext(**base)


def facts(**kwargs) -> TargetFacts:
    base = {"player_id": "p1", "name": "Target Player", "tier": MODERATE, "horizon": SEASON_STARTER, "suggested_pct": 10}
    base.update(kwargs)
    return TargetFacts(**base)


# -- gates ---------------------------------------------------------------------


def test_no_advice_when_league_is_not_faab():
    assert advise(ctx(waiver_type=0), facts()) is None
    assert "not FAAB" in status_note(ctx(waiver_type=0))


def test_no_advice_when_budget_is_missing_even_with_faab_waiver_type():
    assert advise(ctx(budget=None), facts()) is None


def test_no_advice_pre_draft():
    assert advise(ctx(pre_draft=True), facts()) is None
    assert status_note(ctx(pre_draft=True)) is None


# -- remaining budget ----------------------------------------------------------


def test_zero_remaining_says_so_instead_of_rendering_zero_percent():
    advice = advise(ctx(my_used=100), facts(tier=MUST_ADD, suggested_pct=0))
    assert advice.posture == PRESERVE
    assert advice.suggested_dollars == 0
    assert advice.remaining == 0
    assert any("out of FAAB" in n for n in advice.notes)


def test_negative_budget_used_leaves_more_than_the_league_budget():
    # FAAB acquired in a trade: Sleeper reports waiver_budget_used = -5.
    context = ctx(my_used=-5)
    assert context.remaining == 105
    advice = advise(context, facts(tier=MUST_ADD, suggested_pct=35))
    assert advice.remaining == 105
    assert any("acquired by trade" in n for n in advice.notes)


def test_bid_never_exceeds_remaining():
    advice = advise(ctx(my_used=90), facts(tier=MUST_ADD, scarcity=SCARCE, substitutes=0, suggested_pct=35))
    assert advice.suggested_dollars <= advice.remaining == 10


def test_share_of_remaining_is_of_remaining_not_the_total_budget():
    advice = advise(ctx(my_used=80), facts(tier=STRONG_ADD, horizon=SEASON_STARTER, suggested_pct=20))
    # 20% of a 100 budget is $20, but only $20 is left: 100% of remaining.
    assert advice.suggested_dollars == 20
    assert "approximately 100% of remaining budget" in advice.share_of_remaining_text


# -- postures at their boundary constants --------------------------------------


def test_priority_spend_at_the_few_substitutes_boundary():
    advice = advise(ctx(), facts(tier=MUST_ADD, scarcity=VERY_SCARCE, substitutes=FEW_SUBSTITUTES, suggested_pct=35))
    assert advice.posture == PRIORITY_SPEND


def test_one_substitute_past_the_boundary_is_only_aggressive():
    advice = advise(ctx(), facts(tier=MUST_ADD, scarcity=VERY_SCARCE, substitutes=FEW_SUBSTITUTES + 1, suggested_pct=35))
    assert advice.posture == AGGRESSIVE


def test_surging_role_in_a_scarce_market_is_a_priority_spend():
    advice = advise(ctx(), facts(tier=MODERATE, role_label=ROLE_SURGING, scarcity=SCARCE, substitutes=9))
    assert advice.posture == PRIORITY_SPEND


def test_aggressive_at_the_some_substitutes_boundary():
    advice = advise(ctx(), facts(tier=STRONG_ADD, scarcity=SCARCE, substitutes=SOME_SUBSTITUTES, suggested_pct=20))
    assert advice.posture == AGGRESSIVE


def test_one_substitute_past_the_aggressive_boundary_is_normal():
    advice = advise(ctx(), facts(tier=STRONG_ADD, scarcity=SCARCE, substitutes=SOME_SUBSTITUTES + 1, suggested_pct=20))
    assert advice.posture == NORMAL


def test_aggressive_on_urgency_alone_without_a_scarce_market():
    advice = advise(ctx(), facts(tier=MUST_ADD, scarcity=ABUNDANT, need_urgency=True, substitutes=2, suggested_pct=35))
    assert advice.posture == AGGRESSIVE


def test_preserve_streamer_at_the_many_substitutes_boundary():
    advice = advise(ctx(), facts(tier=MODERATE, horizon=STREAMER, substitutes=MANY_SUBSTITUTES))
    assert advice.posture == PRESERVE


def test_streamer_one_below_the_boundary_is_not_preserved_for_that_reason():
    advice = advise(ctx(), facts(tier=MODERATE, horizon=STREAMER, substitutes=MANY_SUBSTITUTES - 1))
    assert advice.posture == NORMAL


def test_streamer_with_many_substitutes_is_never_aggressive_or_priority():
    # Everything else about this target screams spend: Must Add, Very
    # Scarce market, urgent need. The guardrail still wins.
    advice = advise(
        ctx(),
        facts(tier=MUST_ADD, horizon=STREAMER, scarcity=VERY_SCARCE, need_urgency=True,
              substitutes=MANY_SUBSTITUTES, suggested_pct=35),
    )
    assert advice.posture == PRESERVE


def test_preserve_late_season_for_a_marginal_target_at_the_week_boundary():
    late = ctx(current_week=12, playoff_week_start=12 + LATE_SEASON_WEEKS_LEFT)
    assert advise(late, facts(tier=MODERATE)).posture == PRESERVE


def test_one_week_earlier_a_marginal_target_is_still_normal():
    earlier = ctx(current_week=11, playoff_week_start=11 + LATE_SEASON_WEEKS_LEFT + 1)
    assert advise(earlier, facts(tier=MODERATE)).posture == NORMAL


def test_late_season_preserve_does_not_apply_to_a_must_add():
    late = ctx(current_week=12, playoff_week_start=12 + LATE_SEASON_WEEKS_LEFT)
    advice = advise(late, facts(tier=MUST_ADD, scarcity=SCARCE, substitutes=2, suggested_pct=35))
    assert advice.posture == AGGRESSIVE


def test_preserve_at_the_low_remaining_boundary():
    low = ctx(my_used=100 - LOW_REMAINING_PCT)
    advice = advise(low, facts(tier=STRONG_ADD, scarcity=SCARCE, substitutes=1, suggested_pct=20))
    assert advice.remaining == LOW_REMAINING_PCT
    assert advice.posture == PRESERVE


def test_one_dollar_above_the_low_remaining_boundary_still_bids():
    advice = advise(ctx(my_used=100 - LOW_REMAINING_PCT - 1), facts(tier=STRONG_ADD, scarcity=SCARCE, substitutes=1, suggested_pct=20))
    assert advice.posture == AGGRESSIVE


def test_a_priority_spend_survives_a_low_remaining_budget():
    low = ctx(my_used=100 - LOW_REMAINING_PCT)
    advice = advise(low, facts(tier=MUST_ADD, scarcity=VERY_SCARCE, substitutes=0, suggested_pct=35))
    assert advice.posture == PRIORITY_SPEND
    assert advice.suggested_dollars == LOW_REMAINING_PCT  # capped at what's left


# -- bid adjustment ------------------------------------------------------------


def test_preserve_bids_the_speculative_floor_not_the_tiers_own_low_bound():
    """Preserve is a decision not to pay this tier's price, so it must not
    inherit this tier's floor — a Strong Add's low bound is 8% of budget."""
    moderate = advise(ctx(), facts(tier=MODERATE, horizon=STREAMER, substitutes=MANY_SUBSTITUTES, suggested_pct=10))
    assert moderate.suggested_dollars == 1  # SPECULATIVE's low bound on a $100 budget
    strong = advise(ctx(), facts(tier=STRONG_ADD, horizon=STREAMER, substitutes=MANY_SUBSTITUTES, suggested_pct=8))
    assert strong.posture == PRESERVE and strong.suggested_dollars == 1


def test_aggressive_bids_the_tier_high_bound():
    advice = advise(ctx(), facts(tier=STRONG_ADD, scarcity=SCARCE, substitutes=1, suggested_pct=8))
    assert advice.suggested_dollars == 20  # STRONG_ADD's high bound


def test_priority_spend_caps_at_sixty_percent_of_remaining():
    advice = advise(ctx(), facts(tier=MUST_ADD, scarcity=VERY_SCARCE, substitutes=0, suggested_pct=35))
    assert advice.suggested_dollars == PRIORITY_SPEND_MAX_PCT_OF_REMAINING  # 60% of $100 remaining


def test_priority_spend_never_bids_below_the_tier_high_bound():
    # 60% of $40 remaining is $24, under MUST_ADD's $35 high bound.
    advice = advise(ctx(my_used=60), facts(tier=MUST_ADD, scarcity=VERY_SCARCE, substitutes=0, suggested_pct=35))
    assert advice.suggested_dollars == 35


def test_normal_carries_the_waiver_engines_own_suggestion_through():
    advice = advise(ctx(), facts(tier=MODERATE, suggested_pct=10))
    assert advice.posture == NORMAL
    assert advice.suggested_dollars == 10


# -- leverage and anchor facts -------------------------------------------------


def test_leverage_counts_only_managers_who_hold_more_than_the_bid():
    advice = advise(ctx(others_used=[0, 50, 95]), facts(tier=MODERATE, suggested_pct=20))
    assert advice.suggested_dollars == 20
    assert "Only 2 of 3 other managers can outbid $20" == advice.leverage_text


def test_leverage_says_all_or_none_rather_than_only_n_of_n():
    assert "All 3 other managers can outbid $10" == advise(ctx(others_used=[0, 0, 0]), facts(suggested_pct=10)).leverage_text
    assert "No other manager can outbid $10" == advise(ctx(others_used=[95, 95]), facts(suggested_pct=10)).leverage_text


def test_anchor_overshoot_waits_for_enough_bids_to_be_a_market():
    thin = advise(ctx(league_bids=[1, 1]), facts(tier=MUST_ADD, scarcity=SCARCE, substitutes=2, suggested_pct=35))
    assert "max $1" in thin.anchor_text  # still reported as a fact
    assert not any("largest winning bid" in n for n in thin.notes)


def test_no_leverage_line_without_other_managers_budgets():
    assert advise(ctx(), facts()).leverage_text is None


def test_anchor_reports_the_seasons_actual_winning_bids():
    # $0 claims went through unopposed; only contested bids anchor the price.
    advice = advise(ctx(league_bids=[0, 0, 2, 4, 16]), facts(tier=MODERATE, suggested_pct=5))
    assert "median $4" in advice.anchor_text
    assert "max $16" in advice.anchor_text


def test_anchor_overshoot_is_a_note_not_a_cap():
    advice = advise(
        ctx(league_bids=[0, 1, 2, 16]),
        facts(tier=MUST_ADD, scarcity=SCARCE, substitutes=2, suggested_pct=35),
    )
    assert advice.suggested_dollars == 35  # unchanged: the anchor never caps
    assert any("largest winning bid" in n for n in advice.notes)
    assert 35 > 16 * ANCHOR_OVERSHOOT_RATIO


def test_the_overshoot_multiple_is_exactly_two_times_the_largest_bid():
    """ANCHOR_OVERSHOOT_RATIO pinned by value and at literal dollars: a $35
    bid clears 2x a $17 max and does not clear 2x an $18 one. Both lists
    carry a $0 claim (excluded from the anchor) and three contested bids
    (ANCHOR_MIN_BIDS), so only the max is doing the work."""
    assert ANCHOR_OVERSHOOT_RATIO == 2.0 and ANCHOR_MIN_BIDS == 3

    def notes(bids):
        advice = advise(ctx(league_bids=bids), facts(tier=MUST_ADD, scarcity=SCARCE, substitutes=2, suggested_pct=35))
        assert advice.suggested_dollars == 35
        return [n for n in advice.notes if "largest winning bid" in n]

    assert notes([0, 1, 2, 17])  # 35 > 34
    assert not notes([0, 1, 2, 18])  # 35 < 36
    # A $0 claim can never become the anchor, however many there are.
    assert not notes([0, 0, 0, 0, 0])
    # Two contested bids are not a market, even at an absurd multiple.
    assert not notes([0, 1, 1])


def test_the_late_season_preserve_starts_three_weeks_out():
    """LATE_SEASON_WEEKS_LEFT pinned by value, at literal weeks: playoffs
    start in week 15, so weeks 12, 13 and 14 preserve and week 11 does not."""
    assert LATE_SEASON_WEEKS_LEFT == 3
    postures = {
        week: advise(ctx(current_week=week, playoff_week_start=15), facts(tier=MODERATE)).posture
        for week in (11, 12, 13, 14)
    }
    assert postures == {11: NORMAL, 12: PRESERVE, 13: PRESERVE, 14: PRESERVE}


def test_few_substitutes_is_exactly_one():
    """FEW_SUBSTITUTES pinned by value, at literal counts."""
    assert FEW_SUBSTITUTES == 1

    def posture(subs):
        return advise(ctx(), facts(tier=MUST_ADD, scarcity=VERY_SCARCE, substitutes=subs, suggested_pct=35)).posture

    assert posture(0) == PRIORITY_SPEND
    assert posture(1) == PRIORITY_SPEND
    assert posture(2) == AGGRESSIVE


def test_no_overshoot_note_when_the_bid_is_within_the_seasons_range():
    advice = advise(ctx(league_bids=[10, 30, 40]), facts(tier=MUST_ADD, scarcity=SCARCE, substitutes=2, suggested_pct=35))
    assert not any("largest winning bid" in n for n in advice.notes)


# -- substitutes helper --------------------------------------------------------


def _fa(pid: str, position: str, pctl: float):
    return make_entry(player_id=pid, position=position, value=make_value(position=position, dynasty_value_percentile=pctl))


def test_count_substitutes_counts_only_the_same_position_inside_the_band():
    pool = [
        _fa("a", "RB", 60.0),  # inside
        _fa("b", "RB", 60.0 - SUBSTITUTE_PERCENTILE_BAND),  # exactly on the band edge
        _fa("c", "RB", 60.0 - SUBSTITUTE_PERCENTILE_BAND - 1),  # outside
        _fa("d", "WR", 60.0),  # wrong position
    ]
    assert count_substitutes(pool, "RB", 60.0) == 2


def test_the_substitute_band_is_symmetric_and_inclusive_on_both_edges():
    """SUBSTITUTE_PERCENTILE_BAND is +/- 10 points around the target, and
    the edge itself counts. Pinned at literal percentiles either side."""
    assert SUBSTITUTE_PERCENTILE_BAND == 10.0
    pool = [
        _fa("below_edge", "RB", 50.0),   # exactly 10 under: inside
        _fa("below_out", "RB", 49.0),    # 11 under: outside
        _fa("above_edge", "RB", 70.0),   # exactly 10 over: inside
        _fa("above_out", "RB", 71.0),    # 11 over: outside
        _fa("dead_on", "RB", 60.0),
    ]
    assert count_substitutes(pool, "RB", 60.0) == 3
    # ... and each edge case on its own, so the count above can't hide one.
    assert count_substitutes([_fa("a", "RB", 50.0)], "RB", 60.0) == 1
    assert count_substitutes([_fa("a", "RB", 49.0)], "RB", 60.0) == 0
    assert count_substitutes([_fa("a", "RB", 70.0)], "RB", 60.0) == 1
    assert count_substitutes([_fa("a", "RB", 71.0)], "RB", 60.0) == 0


def test_count_substitutes_excludes_the_target_himself():
    pool = [_fa("a", "RB", 60.0), _fa("b", "RB", 58.0)]
    assert count_substitutes(pool, "RB", 60.0, exclude_ids={"a"}) == 1


def test_count_substitutes_is_zero_without_a_measurable_target_percentile():
    assert count_substitutes([_fa("a", "RB", 60.0)], "RB", None) == 0


# -- table-level plan ----------------------------------------------------------


def test_budget_plan_marks_the_rows_that_stop_being_affordable():
    rows = [
        advise(ctx(my_used=50), facts(player_id=f"p{i}", tier=MUST_ADD, scarcity=SCARCE, substitutes=2, suggested_pct=35))
        for i in range(3)
    ]
    assert [r.suggested_dollars for r in rows] == [35, 35, 35]
    notes = budget_plan(rows, remaining=50)
    assert "p0" not in notes  # the first claim is affordable
    assert notes["p1"] == AFFORDABILITY_NOTE
    assert notes["p2"] == AFFORDABILITY_NOTE


def test_budget_plan_is_silent_when_the_rows_fit():
    rows = [advise(ctx(), facts(player_id=f"p{i}", tier=STRONG_ADD, suggested_pct=8)) for i in range(2)]
    assert budget_plan(rows, remaining=100) == {}


def test_budget_plan_ignores_speculative_rows():
    rows = [advise(ctx(), facts(player_id=f"p{i}", tier=MODERATE, suggested_pct=10)) for i in range(20)]
    assert budget_plan(rows, remaining=10) == {}


def test_describe_names_the_posture_and_the_dollars():
    text = advise(ctx(), facts(tier=MUST_ADD, scarcity=VERY_SCARCE, substitutes=0, suggested_pct=35)).describe()
    assert "Target Player" in text
    assert PRIORITY_SPEND in text
    assert "$60" in text


def test_the_streamer_guardrail_is_checked_before_the_abundant_one():
    """Both preserves would fire for a streamer in an Abundant market. The
    order is deliberate (the streamer rule is first, so no later rule can
    talk the tool into paying up), and the reason the user reads should say
    which rule actually decided."""
    advice = advise(
        ctx(),
        facts(tier=MODERATE, horizon=STREAMER, scarcity=ABUNDANT, substitutes=MANY_SUBSTITUTES, suggested_pct=10),
    )
    assert advice.posture == PRESERVE
    assert len(advice.notes) == 1
    assert advice.notes[0].startswith("a streamer with")
    assert "Abundant market" not in advice.notes[0]
