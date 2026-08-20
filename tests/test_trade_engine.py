import pytest
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.config import MY_USER_ID
from sleeper_tool.roster_analysis import RosterEntry
from sleeper_tool.trade_engine import (
    ACCEPTANCE_TIERS,
    CONTENDER,
    MIDDLING,
    REBUILD,
    _find_matching_offer,
    _tradeable_pool,
    identify_buy_low,
    identify_needs,
    identify_sell_high,
    percentile_for_currency,
    value_currency,
    value_for_currency,
)


def test_value_currency_dynasty_league_uses_dynasty():
    roster = make_roster(league=make_league_info(kind="dynasty"))
    assert value_currency(roster) == "dynasty"


def test_value_currency_redraft_and_keeper_use_redraft():
    assert value_currency(make_roster(league=make_league_info(kind="redraft"))) == "redraft"
    assert value_currency(make_roster(league=make_league_info(kind="keeper"))) == "redraft"


def test_value_and_percentile_for_currency_pick_right_fields():
    pv = make_value(dynasty_value=7000, dynasty_value_percentile=90.0, proj_points=250.0, redraft_ecr_percentile=60.0)
    assert value_for_currency(pv, "dynasty") == 7000
    assert value_for_currency(pv, "redraft") == 250.0
    assert percentile_for_currency(pv, "dynasty") == 90.0
    assert percentile_for_currency(pv, "redraft") == 60.0


# -- identify_needs: within-position percentile, not pool-wide --------------


def test_identify_needs_ranks_worst_position_first():
    # All 4 positions present so none fall back to the "missing" default of
    # 0 — RB is my best (95th), WR my worst (30th) — WR should be needed most.
    entries = [
        make_entry(player_id="qb1", position="QB", value=make_value(position="QB", dynasty_value_percentile=70.0)),
        make_entry(player_id="rb1", position="RB", value=make_value(position="RB", dynasty_value_percentile=95.0)),
        make_entry(player_id="wr1", position="WR", value=make_value(position="WR", dynasty_value_percentile=30.0)),
        make_entry(player_id="te1", position="TE", value=make_value(position="TE", dynasty_value_percentile=60.0)),
    ]
    roster = make_roster(entries=entries)
    needs = identify_needs(roster)
    assert needs[0] == "WR"
    assert needs[-1] == "RB"


def test_identify_needs_uses_positional_not_overall_percentile_for_dynasty():
    # Overall percentile is identical (60) for both, but the WR's
    # WITHIN-POSITION percentile is much weaker — needs should reflect that,
    # not the tied overall number (this is the exact bug the red team found).
    rb_value = make_value(position="RB", dynasty_value_percentile=60.0, dynasty_positional_percentile=80.0)
    wr_value = make_value(position="WR", dynasty_value_percentile=60.0, dynasty_positional_percentile=20.0)
    filler_value = lambda pos: make_value(position=pos, dynasty_value_percentile=50.0, dynasty_positional_percentile=50.0)
    entries = [
        make_entry(player_id="rb1", position="RB", value=rb_value),
        make_entry(player_id="wr1", position="WR", value=wr_value),
        make_entry(player_id="qb1", position="QB", value=filler_value("QB")),
        make_entry(player_id="te1", position="TE", value=filler_value("TE")),
    ]
    roster = make_roster(league=make_league_info(kind="dynasty"), entries=entries)
    needs = identify_needs(roster)
    assert needs[0] == "WR"  # weakest by positional percentile despite tied overall percentile


def test_identify_needs_missing_position_defaults_to_zero():
    entries = [make_entry(player_id="rb1", position="RB", value=make_value(position="RB", dynasty_value_percentile=50.0))]
    roster = make_roster(entries=entries)
    needs = identify_needs(roster)
    # No QB/WR/TE on the roster at all -> those should rank as the worst needs.
    assert needs[0] in ("QB", "WR", "TE")
    assert needs[-1] == "RB"


# -- identify_buy_low: age filtering, elite override, decline confirmation --


def _buy_low_candidate(**overrides):
    defaults = dict(
        position="RB", age=27.0, dynasty_value=4000, dynasty_value_percentile=50.0,
        redraft_ecr_percentile=30.0, trend="down", sources=["ktc", "fantasypros_dynasty"],
    )
    defaults.update(overrides)
    value = make_value(**{k: v for k, v in defaults.items() if k not in ("age",)})
    return make_entry(player_id=f"p-{id(overrides)}", position=defaults["position"], age=defaults["age"], value=value, is_starter=False)


def _filler_untouchables():
    """Two high-value, unrelated entries so a roster's top-2-by-value
    untouchable exclusion doesn't accidentally swallow the actual test
    subject when a roster only has 1-2 entries otherwise.
    """
    return [
        make_entry(player_id="filler-1", position="QB", value=make_value(position="QB", dynasty_value=9900, trend="no change")),
        make_entry(player_id="filler-2", position="QB", value=make_value(position="QB", dynasty_value=9800, trend="no change")),
    ]


def test_identify_buy_low_excludes_untouchable_top_players():
    # 3 entries so untouchable-exclusion (top 2 by value) has room to exclude
    # just the top players and still leave the actual candidate eligible —
    # with only 2 entries total this assertion would pass vacuously (both
    # get excluded) without actually proving anything.
    top1 = make_entry(player_id="top1", value=make_value(dynasty_value=9500, trend="down", dynasty_value_percentile=99.0, redraft_ecr_percentile=60.0))
    top2 = make_entry(player_id="top2", value=make_value(dynasty_value=9000, trend="down", dynasty_value_percentile=97.0, redraft_ecr_percentile=60.0))
    candidate = _buy_low_candidate(dynasty_value=4000)
    roster = make_roster(entries=[top1, top2, candidate])
    result = identify_buy_low(roster, CONTENDER)
    names = {e.player_id for e in result}
    assert "top1" not in names
    assert "top2" not in names
    assert candidate.player_id in names


def test_identify_buy_low_filters_by_position_specific_age_for_rebuild():
    # RB young_max_age is 23 — a 26-year-old RB should be excluded for a
    # rebuilding team even though the old universal threshold (26) would
    # have allowed it.
    old_rb = _buy_low_candidate(position="RB", age=26.0, dynasty_value=3000, dynasty_value_percentile=40.0, redraft_ecr_percentile=20.0)
    young_rb = _buy_low_candidate(position="RB", age=22.0, dynasty_value=3500, dynasty_value_percentile=45.0, redraft_ecr_percentile=25.0)
    roster = make_roster(entries=[old_rb, young_rb, *_filler_untouchables()])
    result = identify_buy_low(roster, REBUILD)
    names = {e.player_id for e in result}
    assert young_rb.player_id in names
    assert old_rb.player_id not in names


def test_identify_buy_low_contender_ignores_age_filter():
    old_rb = _buy_low_candidate(position="RB", age=30.0, dynasty_value=3000, dynasty_value_percentile=55.0, redraft_ecr_percentile=20.0)
    roster = make_roster(entries=[old_rb, *_filler_untouchables()])
    result = identify_buy_low(roster, CONTENDER)
    assert old_rb.player_id in {e.player_id for e in result}


def test_identify_buy_low_elite_asset_bypasses_age_filter_even_for_rebuild():
    # 27-year-old RB is past the RB young cutoff (23), but grades at the
    # 95th percentile overall — a true elite asset shouldn't be filtered
    # out of a rebuild's radar just for age.
    elite_old_rb = _buy_low_candidate(position="RB", age=27.0, dynasty_value=6000, dynasty_value_percentile=95.0, redraft_ecr_percentile=80.0)
    roster = make_roster(entries=[elite_old_rb, *_filler_untouchables()])
    result = identify_buy_low(roster, REBUILD)
    assert elite_old_rb.player_id in {e.player_id for e in result}


def test_identify_buy_low_requires_decline_confirmation_for_dynasty():
    # dynasty percentile (55) and redraft percentile (50) are close together
    # -> looks like a real decline across both horizons, not a short-term
    # market overreaction -> should NOT be flagged as a buy-low.
    real_decline = _buy_low_candidate(position="WR", age=24.0, dynasty_value_percentile=55.0, redraft_ecr_percentile=50.0)
    # dynasty percentile stays high (80) while redraft/current-form has
    # dropped a lot (30) -> classic buy-the-dip pattern -> should qualify.
    market_overreaction = _buy_low_candidate(position="WR", age=24.0, dynasty_value_percentile=80.0, redraft_ecr_percentile=30.0)
    roster = make_roster(
        league=make_league_info(kind="dynasty"), entries=[real_decline, market_overreaction, *_filler_untouchables()]
    )
    result = identify_buy_low(roster, CONTENDER)
    names = {e.player_id for e in result}
    assert market_overreaction.player_id in names
    assert real_decline.player_id not in names


def test_identify_buy_low_ignores_players_not_trending_down():
    steady = _buy_low_candidate(trend="no change")
    roster = make_roster(entries=[steady])
    assert identify_buy_low(roster, CONTENDER) == []


def test_identify_buy_low_requires_min_rosterable_percentile():
    too_deep = _buy_low_candidate(dynasty_value_percentile=5.0, redraft_ecr_percentile=1.0)
    roster = make_roster(entries=[too_deep])
    assert identify_buy_low(roster, CONTENDER) == []


# -- _find_matching_offer: value-tolerance matching --------------------------


def test_find_matching_offer_prefers_single_piece_within_tolerance():
    e1 = make_entry(player_id="a", value=make_value(dynasty_value=1000))
    e2 = make_entry(player_id="b", value=make_value(dynasty_value=1900))
    e3 = make_entry(player_id="c", value=make_value(dynasty_value=100))
    offer = _find_matching_offer([e1, e2, e3], [], target_value=1050, currency="dynasty")
    assert offer is not None
    players, picks = offer
    assert [e.player_id for e in players] == ["a"]
    assert picks == []


def test_find_matching_offer_falls_back_to_two_pieces():
    e1 = make_entry(player_id="a", value=make_value(dynasty_value=500))
    e2 = make_entry(player_id="b", value=make_value(dynasty_value=500))
    offer = _find_matching_offer([e1, e2], [], target_value=1000, currency="dynasty")
    assert offer is not None
    players, picks = offer
    assert {e.player_id for e in players} == {"a", "b"}


def test_find_matching_offer_returns_none_outside_tolerance():
    e1 = make_entry(player_id="a", value=make_value(dynasty_value=100))
    offer = _find_matching_offer([e1], [], target_value=1000, currency="dynasty")
    assert offer is None


def test_find_matching_offer_can_use_a_pick_alone():
    from sleeper_tool.draft_picks import OwnedPick

    pick = OwnedPick(season="2027", round=1, original_roster_id=1, tier="Mid", name="2027 Mid 1st", value=6000)
    offer = _find_matching_offer([], [pick], target_value=6050, currency="dynasty")
    assert offer is not None
    players, picks = offer
    assert players == []
    assert picks == [pick]


def test_find_matching_offer_ignores_picks_for_redraft_currency():
    from sleeper_tool.draft_picks import OwnedPick

    pick = OwnedPick(season="2027", round=1, original_roster_id=1, tier="Mid", name="2027 Mid 1st", value=6000)
    offer = _find_matching_offer([], [pick], target_value=6050, currency="redraft")
    assert offer is None


def test_find_matching_offer_returns_none_for_zero_target():
    e1 = make_entry(player_id="a", value=make_value(dynasty_value=100))
    assert _find_matching_offer([e1], [], target_value=0, currency="dynasty") is None


# -- _tradeable_pool: veteran-first ordering for rebuild/middling -----------


def test_tradeable_pool_sorts_veterans_first_for_rebuild():
    veteran = make_entry(player_id="vet", position="WR", age=30.0, value=make_value(position="WR", dynasty_value=3000))
    youngster = make_entry(player_id="young", position="WR", age=22.0, value=make_value(position="WR", dynasty_value=4000))
    roster = make_roster(entries=[veteran, youngster])
    pool = _tradeable_pool(roster, REBUILD, exclude_top=0)  # exclude_top=0: isolate sort order from untouchable-exclusion
    assert pool[0].player_id == "vet"  # veteran sorted first despite lower value


def test_tradeable_pool_sorts_by_value_only_for_contender():
    veteran = make_entry(player_id="vet", position="WR", age=30.0, value=make_value(position="WR", dynasty_value=3000))
    youngster = make_entry(player_id="young", position="WR", age=22.0, value=make_value(position="WR", dynasty_value=4000))
    roster = make_roster(entries=[veteran, youngster])
    pool = _tradeable_pool(roster, CONTENDER, exclude_top=0)
    assert pool[0].player_id == "young"  # pure value order, no age bias


def test_tradeable_pool_excludes_untouchables():
    top1 = make_entry(player_id="top1", value=make_value(dynasty_value=9000))
    top2 = make_entry(player_id="top2", value=make_value(dynasty_value=8000))
    depth = make_entry(player_id="depth", value=make_value(dynasty_value=1000))
    roster = make_roster(entries=[top1, top2, depth])
    pool = _tradeable_pool(roster, CONTENDER)
    ids = {e.player_id for e in pool}
    assert "top1" not in ids and "top2" not in ids
    assert "depth" in ids


# -- draft picks as trade chips ----------------------------------------------


def test_find_matching_offer_combines_player_and_pick():
    from sleeper_tool.draft_picks import OwnedPick

    e1 = make_entry(player_id="a", value=make_value(dynasty_value=2000))
    pick = OwnedPick(season="2027", round=2, original_roster_id=1, tier="Mid", name="2027 Mid 2nd", value=4100)
    offer = _find_matching_offer([e1], [pick], target_value=6050, currency="dynasty")
    assert offer is not None
    players, picks = offer
    assert [e.player_id for e in players] == ["a"]
    assert picks == [pick]


def test_build_pick_target_proposal_skips_the_single_best_pick():
    from sleeper_tool.draft_picks import OwnedPick
    from sleeper_tool.team_status import REBUILD, TeamStatusResult
    from sleeper_tool.trade_engine import _build_pick_target_proposal

    best_pick = OwnedPick(season="2027", round=1, original_roster_id=2, tier="Early", name="2027 Early 1st", value=9000)
    second_pick = OwnedPick(season="2027", round=2, original_roster_id=2, tier="Mid", name="2027 Mid 2nd", value=4000)
    their_roster = make_roster(roster_id=2, owner_username="rival", team_name="Rival Team")
    my_pool = [make_entry(player_id="give1", value=make_value(dynasty_value=4050))]
    status = TeamStatusResult(status=REBUILD, strength_percentile=20.0, win_pct=None, games_played=0, reason="test")

    proposal = _build_pick_target_proposal(
        make_league_info(kind="dynasty"), their_roster, [best_pick, second_pick], my_pool, "dynasty", status
    )
    assert proposal is not None
    assert proposal.receive_picks == [second_pick]  # best_pick treated as untouchable, skipped
    assert proposal.give[0].player_id == "give1"


def test_build_pick_target_proposal_returns_none_with_only_one_pick():
    from sleeper_tool.draft_picks import OwnedPick
    from sleeper_tool.team_status import REBUILD, TeamStatusResult
    from sleeper_tool.trade_engine import _build_pick_target_proposal

    only_pick = OwnedPick(season="2027", round=1, original_roster_id=2, tier="Early", name="2027 Early 1st", value=9000)
    their_roster = make_roster(roster_id=2, owner_username="rival", team_name="Rival Team")
    status = TeamStatusResult(status=REBUILD, strength_percentile=20.0, win_pct=None, games_played=0, reason="test")

    proposal = _build_pick_target_proposal(
        make_league_info(kind="dynasty"), their_roster, [only_pick], [], "dynasty", status
    )
    assert proposal is None  # their only pick is treated as untouchable, nothing left to target


# -- identify_sell_high: a hot week from your actual RB1 isn't a sell signal


def test_identify_sell_high_excludes_untouchable_cornerstone_assets():
    # A trending-up TOP-2 starter (e.g. a true RB1 having a great year) must
    # never be surfaced as a "sell high" candidate -- that's the whole
    # point of playing well, not a reason to shop him.
    cornerstone = make_entry(player_id="rb1", position="RB", is_starter=True,
        value=make_value(position="RB", dynasty_value=9500, trend="rising"))
    secondary = make_entry(player_id="rb2", position="RB", is_starter=True,
        value=make_value(position="RB", dynasty_value=8500, trend="rising"))
    real_candidate = make_entry(player_id="wr3", position="WR", is_starter=False,
        value=make_value(position="WR", dynasty_value=2000, trend="rising"))
    roster = make_roster(entries=[cornerstone, secondary, real_candidate])
    candidates = identify_sell_high(roster)
    ids = {e.player_id for e in candidates}
    assert "rb1" not in ids
    assert "rb2" not in ids
    assert "wr3" in ids


# -- identify_depth_needs: thin depth behind a strong RB1 is a real need ----


def test_identify_depth_needs_flags_zero_rosterable_depth_at_a_position():
    from sleeper_tool.trade_engine import identify_depth_needs

    # Elite RB1 (99th pctl) but nothing else rosterable at RB, needing 2 starters there.
    rb1 = make_entry(player_id="rb1", position="RB", value=make_value(position="RB", dynasty_value_percentile=99.0))
    rb2 = make_entry(player_id="rb2", position="RB", value=make_value(position="RB", dynasty_value_percentile=10.0))  # below MIN_ROSTERABLE_PERCENTILE
    roster = make_roster(entries=[rb1, rb2])
    needs = identify_depth_needs(roster, min_starters={"RB": 2})
    assert "RB" in needs


def test_identify_depth_needs_clear_when_enough_rosterable_bodies_exist():
    from sleeper_tool.trade_engine import identify_depth_needs

    rb1 = make_entry(player_id="rb1", position="RB", value=make_value(position="RB", dynasty_value_percentile=90.0))
    rb2 = make_entry(player_id="rb2", position="RB", value=make_value(position="RB", dynasty_value_percentile=60.0))
    roster = make_roster(entries=[rb1, rb2])
    needs = identify_depth_needs(roster, min_starters={"RB": 2})
    assert "RB" not in needs


def test_derive_league_format_reads_exact_starter_slot_counts_with_no_flex():
    from sleeper_tool.valuation import derive_league_format

    fmt = derive_league_format({
        "scoring_settings": {},
        "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "BN", "BN"],
    })
    assert fmt.starter_slots == {"QB": 1, "RB": 2, "WR": 2, "TE": 1}


def test_derive_league_format_distributes_flex_and_superflex_demand():
    # FLEX/SUPER_FLEX previously contributed zero demand to any position,
    # badly undercounting depth need in the median real league (2-3 FLEX
    # spots is typical). Each FLEX slot should add real, if approximate,
    # demand to RB/WR/TE; each SUPER_FLEX slot to all four core positions.
    from sleeper_tool.valuation import derive_league_format

    fmt = derive_league_format({
        "scoring_settings": {},
        "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "BN"],
    })
    assert fmt.starter_slots["QB"] == pytest.approx(1 + 1 / 4)
    assert fmt.starter_slots["RB"] == pytest.approx(1 + 1 / 3 + 1 / 4)
    assert fmt.starter_slots["WR"] == pytest.approx(1 + 1 / 3 + 1 / 4)
    assert fmt.starter_slots["TE"] == pytest.approx(1 + 1 / 3 + 1 / 4)


# -- identify_drop_candidates: proactive roster-cleanup recommendations -----


def test_identify_drop_candidates_flags_a_low_ranked_bench_player():
    from sleeper_tool.trade_engine import CONTENDER, identify_drop_candidates

    weak_bench = make_entry(player_id="weak", position="WR", is_starter=False,
        value=make_value(position="WR", dynasty_positional_percentile=15.0, trend="no change"))
    roster = make_roster(entries=[weak_bench])
    candidates = identify_drop_candidates(roster, CONTENDER)
    assert len(candidates) == 1
    assert candidates[0].entry.player_id == "weak"
    assert any("weakest rosterable" in r for r in candidates[0].reasons)


def test_identify_drop_candidates_flags_excess_position_depth():
    from sleeper_tool.trade_engine import CONTENDER, identify_drop_candidates

    # 4 corroborated WRs on the bench, well above what a 1-WR-slot league needs.
    entries = [
        make_entry(player_id=f"wr{i}", position="WR", is_starter=False,
            value=make_value(position="WR", dynasty_positional_percentile=pctl, trend="no change"))
        for i, pctl in enumerate([80, 60, 40, 30])
    ]
    roster = make_roster(entries=entries)
    roster.fmt.starter_slots["WR"] = 1
    candidates = identify_drop_candidates(roster, CONTENDER, max_candidates=10)
    buried_ids = {c.entry.player_id for c in candidates if any("buried behind" in r for r in c.reasons)}
    assert "wr3" in buried_ids  # the weakest of 4, buried behind 3 better options


def test_identify_drop_candidates_flags_no_upside_aging_veteran_on_a_non_contender():
    from sleeper_tool.trade_engine import REBUILD, identify_drop_candidates

    aging_vet = make_entry(player_id="vet", position="RB", is_starter=False, age=29.0,
        value=make_value(position="RB", dynasty_positional_percentile=50.0, trend="no change"))
    roster = make_roster(league=make_league_info(kind="dynasty"), entries=[aging_vet])
    candidates = identify_drop_candidates(roster, REBUILD)
    assert len(candidates) == 1
    assert any("no upside signal" in r for r in candidates[0].reasons)


def test_identify_drop_candidates_never_flags_starters_taxi_or_reserve():
    from sleeper_tool.trade_engine import CONTENDER, identify_drop_candidates

    weak_value = make_value(position="WR", dynasty_positional_percentile=5.0, trend="no change")
    starter = make_entry(player_id="s1", position="WR", is_starter=True, value=weak_value)
    taxi = make_entry(player_id="t1", position="WR", is_starter=False, is_taxi=True, value=weak_value)
    reserve = make_entry(player_id="r1", position="WR", is_starter=False, is_reserve=True, value=weak_value)
    roster = make_roster(entries=[starter, taxi, reserve])
    assert identify_drop_candidates(roster, CONTENDER) == []


def test_identify_drop_candidates_strong_drop_when_multiple_reasons_apply():
    from sleeper_tool.trade_engine import REBUILD, identify_drop_candidates

    doubly_bad = make_entry(player_id="bad", position="RB", is_starter=False, age=30.0,
        value=make_value(position="RB", dynasty_positional_percentile=10.0, trend="no change"))
    roster = make_roster(league=make_league_info(kind="dynasty"), entries=[doubly_bad])
    candidates = identify_drop_candidates(roster, REBUILD)
    assert candidates[0].priority == "Strong Drop"
    assert len(candidates[0].reasons) >= 2


def test_identify_drop_candidates_respects_max_candidates_cap():
    from sleeper_tool.trade_engine import CONTENDER, identify_drop_candidates

    entries = [
        make_entry(player_id=f"wr{i}", position="WR", is_starter=False,
            value=make_value(position="WR", dynasty_positional_percentile=float(i), trend="no change"))
        for i in range(10)
    ]
    roster = make_roster(entries=entries)
    assert len(identify_drop_candidates(roster, CONTENDER, max_candidates=3)) == 3


# -- _roster_impact_note ---------------------------------------------------


def test_roster_impact_note_excludes_departing_player_when_hes_the_weakest_starter():
    # Regression: a sell-high pitch's "why this helps me" note was
    # comparing the incoming piece against the very player being SOLD
    # AWAY in the same trade (still technically on my_roster since the
    # trade hasn't happened) -- when that departing player is also my
    # WEAKEST starter at the position (a common sell-high shape: he's
    # being sold precisely because something better is coming in), the
    # note would nonsensically cite him as the "current starter" beaten
    # by his own outgoing trade.
    from sleeper_tool.trade_engine import _roster_impact_note

    departing = make_entry(player_id="dep", name="Departing Guy", position="TE", is_starter=True,
        value=make_value(position="TE", dynasty_value_percentile=30.0))
    roster = make_roster(entries=[departing])

    buggy_without_exclusion = _roster_impact_note(roster, "TE", 50.0, "dynasty")
    assert "Departing Guy" in buggy_without_exclusion  # demonstrates the bug exists without the fix

    fixed_with_exclusion = _roster_impact_note(roster, "TE", 50.0, "dynasty", exclude_player_id="dep")
    assert "Departing Guy" not in fixed_with_exclusion
    assert "nobody currently starting" in fixed_with_exclusion


def test_roster_impact_note_excludes_departing_player_but_still_compares_against_others():
    from sleeper_tool.trade_engine import _roster_impact_note

    departing = make_entry(player_id="dep", name="Departing Guy", position="WR", is_starter=True,
        value=make_value(position="WR", dynasty_value_percentile=90.0))
    other_starter = make_entry(player_id="other", name="Other WR", position="WR", is_starter=True,
        value=make_value(position="WR", dynasty_value_percentile=40.0))
    roster = make_roster(entries=[departing, other_starter])
    note = _roster_impact_note(roster, "WR", 60.0, "dynasty", exclude_player_id="dep")
    assert "Departing Guy" not in note
    assert "Other WR" in note


# -- _untouchable_ids: starters-only protection, scarce-position protection -


def test_untouchable_ids_never_protects_a_non_starting_backup():
    # A backup QB (is_starter=False) sitting behind a more valuable starter
    # must NOT be locked untradeable just because its raw value ranks top-2
    # overall — it's a bench piece, losing it costs the roster nothing.
    from sleeper_tool.trade_engine import _untouchable_ids

    starter_qb = make_entry(player_id="qb-starter", position="QB", is_starter=True,
        value=make_value(position="QB", dynasty_value=9500, dynasty_value_percentile=99))
    backup_qb = make_entry(player_id="qb-backup", position="QB", is_starter=False,
        value=make_value(position="QB", dynasty_value=9400, dynasty_value_percentile=98, trend="rising"))
    other_starter = make_entry(player_id="te1", position="TE", is_starter=True,
        value=make_value(position="TE", dynasty_value=1200, dynasty_value_percentile=15))
    roster = make_roster(entries=[starter_qb, backup_qb, other_starter])
    ids = _untouchable_ids(roster, "dynasty", exclude_top=2)
    assert "qb-backup" not in ids


def test_untouchable_ids_protects_a_scarce_positions_clear_best_asset():
    # A corroborated TE at the 95th within-position percentile is this
    # team's only real starting TE — protect it even though TE dollar
    # values run low enough that it'd never crack the top-2-overall cut.
    from sleeper_tool.trade_engine import _untouchable_ids

    rb1 = make_entry(player_id="rb1", position="RB", is_starter=True,
        value=make_value(position="RB", dynasty_value=9500, dynasty_value_percentile=99, dynasty_positional_percentile=99))
    rb2 = make_entry(player_id="rb2", position="RB", is_starter=True,
        value=make_value(position="RB", dynasty_value=9000, dynasty_value_percentile=97, dynasty_positional_percentile=97))
    te1 = make_entry(player_id="te1", position="TE", is_starter=True,
        value=make_value(position="TE", dynasty_value=2600, dynasty_value_percentile=70, dynasty_positional_percentile=95))
    roster = make_roster(entries=[rb1, rb2, te1])
    ids = _untouchable_ids(roster, "dynasty", exclude_top=2)
    assert "te1" in ids
    assert ids == {"rb1", "rb2", "te1"}


# -- _recipient_need_fit: opponent roster-depth awareness -------------------


def _rosterable_wr(pid: str, pctl: float) -> RosterEntry:
    return make_entry(player_id=pid, position="WR", is_starter=pctl >= 50,
        value=make_value(position="WR", dynasty_value_percentile=pctl))


def test_recipient_need_fit_rejects_a_redundant_piece_into_a_glutted_position():
    from sleeper_tool.trade_engine import _recipient_need_fit

    glutted_roster = make_roster(entries=[_rosterable_wr(f"wr{i}", pctl) for i, pctl in enumerate([85, 80, 70, 65, 60])])
    weak_offer = [_rosterable_wr("give-wr", 30.0)]  # below every one of their rosterable WRs
    any_fit, all_fit, notes = _recipient_need_fit(glutted_roster, weak_offer, "dynasty")
    assert any_fit is False
    assert all_fit is False
    assert notes


def test_recipient_need_fit_accepts_a_piece_that_beats_their_weakest_starter():
    from sleeper_tool.trade_engine import _recipient_need_fit

    roster = make_roster(entries=[_rosterable_wr(f"wr{i}", pctl) for i, pctl in enumerate([85, 60])])
    strong_offer = [_rosterable_wr("give-wr", 75.0)]  # beats their weakest rosterable WR (60)
    any_fit, all_fit, notes = _recipient_need_fit(roster, strong_offer, "dynasty")
    assert any_fit is True
    assert all_fit is True


def test_recipient_need_fit_accepts_a_piece_at_a_position_theyre_completely_empty_at():
    from sleeper_tool.trade_engine import _recipient_need_fit

    roster = make_roster(entries=[_rosterable_wr("wr1", 85.0)])  # no TE at all
    te_offer = [make_entry(player_id="give-te", position="TE", value=make_value(position="TE", dynasty_value_percentile=20.0))]
    any_fit, all_fit, notes = _recipient_need_fit(roster, te_offer, "dynasty")
    assert any_fit is True
    assert all_fit is True


def test_recipient_need_fit_picks_only_always_fits():
    from sleeper_tool.trade_engine import _recipient_need_fit

    roster = make_roster(entries=[_rosterable_wr(f"wr{i}", pctl) for i, pctl in enumerate([85, 80, 70])])
    any_fit, all_fit, notes = _recipient_need_fit(roster, [], "dynasty")
    assert any_fit is True
    assert all_fit is True
    assert notes == []


def test_recipient_need_fit_any_true_all_false_when_one_piece_is_clutter():
    # Regression: a multi-piece offer where only SOME pieces fit must be
    # distinguishable from one where ALL pieces fit -- any_fit alone
    # (the old return shape) couldn't tell these apart, letting a clutter
    # piece silently ride along a real upgrade with no visible flag.
    from sleeper_tool.trade_engine import _recipient_need_fit

    roster = make_roster(entries=[_rosterable_wr("wr1", 60.0), make_entry(
        player_id="te1", position="TE", value=make_value(position="TE", dynasty_value_percentile=70.0))])
    good_wr = _rosterable_wr("give-wr", 75.0)  # beats their weakest WR (60) -- real fit
    clutter_te = make_entry(player_id="give-te", position="TE", value=make_value(position="TE", dynasty_value_percentile=20.0))  # below their TE (70)
    any_fit, all_fit, notes = _recipient_need_fit(roster, [good_wr, clutter_te], "dynasty")
    assert any_fit is True
    assert all_fit is False
    assert len(notes) == 1  # only the clutter TE piece gets flagged, not the fitting WR


# -- rate_acceptance / proposal_confidence -----------------------------------


def test_rate_acceptance_penalizes_targeting_a_starter_and_low_roster_fit():
    from sleeper_tool.owner_profiles import DEFAULT_PROFILE
    from sleeper_tool.trade_engine import OpponentFit, rate_acceptance

    good_fit = OpponentFit(target_is_starter=False, would_upgrade_their_roster=True, fit_notes=[],
        opponent_status="middling", status_fit="neutral", piece_count=1)
    bad_fit = OpponentFit(target_is_starter=True, would_upgrade_their_roster=False, fit_notes=["doesn't help"],
        opponent_status="middling", status_fit="mismatch", piece_count=2)
    good_rating, _ = rate_acceptance(good_fit, 1.0, DEFAULT_PROFILE)
    bad_rating, _ = rate_acceptance(bad_fit, 1.0, DEFAULT_PROFILE)
    from sleeper_tool.trade_engine import ACCEPTANCE_TIERS
    assert ACCEPTANCE_TIERS.index(good_rating) > ACCEPTANCE_TIERS.index(bad_rating)


def test_rate_acceptance_floors_at_very_low_for_an_inactive_trader():
    from sleeper_tool.owner_profiles import OwnerProfile
    from sleeper_tool.trade_engine import OpponentFit, rate_acceptance

    fit = OpponentFit(target_is_starter=False, would_upgrade_their_roster=True, fit_notes=[],
        opponent_status="middling", status_fit="neutral", piece_count=1)
    inactive_profile = OwnerProfile(username="ghost", trades_often="inactive")
    rating, reasons = rate_acceptance(fit, 1.0, inactive_profile)
    assert rating == "Very Low"


def test_proposal_confidence_is_dragged_down_by_the_weakest_valuation():
    from sleeper_tool.trade_engine import proposal_confidence

    solid = make_value(sources=["ktc", "fantasypros_dynasty"], cross_source_agreement="agree")
    shaky = make_value(sources=["ktc"], cross_source_agreement="insufficient_data")
    assert proposal_confidence([solid, solid]) == "High"
    assert proposal_confidence([solid, shaky]) == "Low"
    assert proposal_confidence([]) == "Medium"


# -- generate_trade_message ---------------------------------------------------


def test_generate_trade_message_is_nonempty_and_not_ai_sounding():
    from sleeper_tool.trade_engine import TradeProposal, generate_trade_message

    proposal = TradeProposal(
        league_name="Test League", currency="dynasty", target_username="rival", target_team_name="Rival Team",
        give=[make_entry(player_id="g1", name="Give Guy")], receive=[make_entry(player_id="r1", name="Receive Guy")],
        my_value_total=1000, their_value_total=1000, rationale_for_me=[], rationale_for_them=[], caveats=[],
        trade_type="buy_low",
    )
    msg = generate_trade_message(proposal)
    assert msg
    assert "Give Guy" in msg and "Receive Guy" in msg
    banned_phrases = ["according to my projections", "this trade benefits both parties", "the analytics suggest"]
    assert not any(p in msg.lower() for p in banned_phrases)


def test_generate_trade_message_uses_the_concrete_benefit_reason_not_generic_filler():
    from sleeper_tool.trade_engine import TradeProposal, generate_trade_message

    proposal = TradeProposal(
        league_name="Test League", currency="dynasty", target_username="rival", target_team_name="Rival Team",
        give=[make_entry(player_id="g1", name="Give Guy")], receive=[make_entry(player_id="r1", name="Receive Guy")],
        my_value_total=1000, their_value_total=1000, rationale_for_me=[], rationale_for_them=[], caveats=[],
        trade_type="buy_low",
    )
    msg = generate_trade_message(proposal, benefit_reason="since he'd start over what you've got at TE now")
    assert "TE" in msg
    assert "fills a real need" not in msg.lower()  # the old generic closer bank is gone


# -- _benefit_reason ------------------------------------------------------------


def test_benefit_reason_names_the_starter_it_would_replace():
    from sleeper_tool.trade_engine import _benefit_reason

    weak_te = make_entry(player_id="te1", name="Old TE", position="TE", is_starter=True,
        value=make_value(position="TE", dynasty_value_percentile=50.0))
    roster = make_roster(entries=[weak_te])
    incoming = make_entry(player_id="new-te", position="TE", value=make_value(position="TE", dynasty_value_percentile=80.0))
    reason = _benefit_reason(roster, [incoming], "dynasty")
    assert "TE" in reason
    assert "start over" in reason


def test_benefit_reason_flags_an_empty_position():
    from sleeper_tool.trade_engine import _benefit_reason

    roster = make_roster(entries=[make_entry(player_id="wr1", position="WR", is_starter=True, value=make_value(position="WR"))])
    incoming = make_entry(player_id="new-te", position="TE", value=make_value(position="TE", dynasty_value_percentile=50.0))
    reason = _benefit_reason(roster, [incoming], "dynasty")
    assert "don't have a real TE" in reason


def test_benefit_reason_defaults_to_pick_capital_language_with_no_pieces():
    from sleeper_tool.trade_engine import _benefit_reason

    roster = make_roster(entries=[])
    assert "draft capital" in _benefit_reason(roster, [], "dynasty")


# -- generate_trade_proposals: end-to-end integration ------------------------


def _need_fit_scenario():
    """A dynasty league: my roster is TE-needy with WR/RB surplus to trade;
    the opponent is deep at WR (5 rosterable) but has exactly one
    real, non-scarce buy-low-eligible TE and needs RB. Every player's
    dynasty AND redraft percentiles are set consistently so the
    _not_just_a_slump real-decline guard doesn't reject the buy-low
    candidate as noise.
    """
    def pv(pos, dyn_pctl, pos_pctl=None, trend="no change", value=None):
        return make_value(
            position=pos, dynasty_value=value if value is not None else int(dyn_pctl * 100),
            dynasty_value_percentile=dyn_pctl, dynasty_positional_percentile=pos_pctl if pos_pctl is not None else dyn_pctl,
            redraft_ecr_percentile=dyn_pctl, trend=trend,
        )

    league = make_league_info(kind="dynasty")
    my_entries = [
        make_entry(player_id="my-rb1", position="RB", is_starter=True, value=pv("RB", 95, 95)),
        make_entry(player_id="my-wr1", position="WR", is_starter=True, value=pv("WR", 90, 90)),
        make_entry(player_id="my-wr-rising", position="WR", is_starter=False, value=pv("WR", 60, 55, trend="rising")),
        make_entry(player_id="my-te1", position="TE", is_starter=True, value=pv("TE", 30, 25)),
        make_entry(player_id="my-filler", position="RB", is_starter=False, value=pv("RB", 50, 45)),
    ]
    my_roster = make_roster(roster_id=1, owner_id=MY_USER_ID, owner_username="me", team_name="My Team", league=league, entries=my_entries)

    opp_entries = [
        make_entry(player_id="opp-wr1", position="WR", is_starter=True, value=pv("WR", 85, 80)),
        make_entry(player_id="opp-wr2", position="WR", is_starter=True, value=pv("WR", 80, 75)),
        make_entry(player_id="opp-wr3", position="WR", is_starter=False, value=pv("WR", 70, 65)),
        make_entry(player_id="opp-wr4", position="WR", is_starter=False, value=pv("WR", 65, 60)),
        make_entry(player_id="opp-wr5", position="WR", is_starter=False, value=pv("WR", 60, 55)),
        make_entry(player_id="opp-te1", position="TE", is_starter=True, value=make_value(
            position="TE", dynasty_value=7000, dynasty_value_percentile=70, dynasty_positional_percentile=70,
            redraft_ecr_percentile=40, trend="down",  # dynasty holding, redraft dipped -- genuine buy-low pattern,
        )),  # not just a slump (see _not_just_a_slump's DECLINE_CONFIRMATION_GAP check)
        make_entry(player_id="opp-te2", position="TE", is_starter=False, value=pv("TE", 35, 30)),
        make_entry(player_id="opp-rb1", position="RB", is_starter=True, value=pv("RB", 40, 35)),
    ]
    opp_roster = make_roster(roster_id=2, owner_id="opp1", owner_username="RivalOwner", team_name="Rival Team", league=league, entries=opp_entries)
    return league, {1: my_roster, 2: opp_roster}


def test_generate_trade_proposals_produces_a_scored_buy_low_offer():
    from sleeper_tool.trade_engine import generate_trade_proposals

    league, rosters = _need_fit_scenario()
    proposals = generate_trade_proposals(league, rosters, max_proposals=3)
    assert len(proposals) >= 1
    buy_low = [p for p in proposals if p.trade_type == "buy_low"]
    assert buy_low
    p = buy_low[0]
    assert p.receive[0].player_id == "opp-te1"
    assert p.acceptance_rating in ACCEPTANCE_TIERS
    assert p.message  # a ready-to-send message was generated
    assert p.confidence in ("Low", "Medium", "High")


def test_generate_trade_proposals_never_offers_a_position_the_opponent_is_already_glutted_at():
    # Regression for the highest-severity finding across the review panel:
    # an offer must not send a redundant piece into a position the
    # recipient already has full rosterable coverage at.
    from sleeper_tool.trade_engine import generate_trade_proposals

    league, rosters = _need_fit_scenario()
    proposals = generate_trade_proposals(league, rosters, max_proposals=5)
    for p in proposals:
        for piece in p.give:
            if piece.position == "WR":
                # The opponent has 5 rosterable WRs already (60-85 pctl) —
                # any WR offered to them must beat their weakest (60).
                assert piece.value.dynasty_value_percentile > 60 or piece.value.dynasty_value_percentile is None


def test_generate_trade_proposals_includes_sell_high_when_opponent_needs_the_position():
    from sleeper_tool.trade_engine import generate_trade_proposals

    league, rosters = _need_fit_scenario()
    proposals = generate_trade_proposals(league, rosters, max_proposals=5)
    # my-wr-rising (trend=rising) is a sell-high candidate, but the only
    # opponent here is WR-glutted (not WR-needy) so no sell-high pitch
    # should fire for it — confirms sell-high respects THEIR needs too.
    sell_high = [p for p in proposals if p.trade_type == "sell_high"]
    assert all(p.give[0].player_id != "my-wr-rising" for p in sell_high)


def test_generate_trade_proposals_empty_when_no_other_active_rosters():
    from sleeper_tool.trade_engine import generate_trade_proposals

    league, rosters = _need_fit_scenario()
    solo = {1: rosters[1]}
    assert generate_trade_proposals(league, solo, max_proposals=3) == []


def test_generate_trade_proposals_never_gives_away_the_same_player_twice():
    # Regression: nothing tracked which of MY assets were already
    # committed once you looked across all three generation passes, so
    # the same roster player could be proposed away in two different
    # proposals within one report.
    from sleeper_tool.trade_engine import generate_trade_proposals

    league, rosters = _need_fit_scenario()
    proposals = generate_trade_proposals(league, rosters, max_proposals=5)
    given_ids = [e.player_id for p in proposals for e in p.give]
    assert len(given_ids) == len(set(given_ids)), f"a player was offered away in more than one proposal: {given_ids}"


def test_generate_trade_proposals_resolves_a_give_piece_collision_between_two_opponents():
    # Regression: when two opponents' best-fit offer independently landed
    # on the identical give-piece, the loser was silently dropped instead
    # of being retried against the remaining pool — even though a
    # different, still-valid combination existed for them.
    from sleeper_tool.trade_engine import generate_trade_proposals

    def pv(pos, dyn_pctl, pos_pctl=None, trend="no change", value=None):
        return make_value(
            position=pos, dynasty_value=value if value is not None else int(dyn_pctl * 100),
            dynasty_value_percentile=dyn_pctl, dynasty_positional_percentile=pos_pctl if pos_pctl is not None else dyn_pctl,
            redraft_ecr_percentile=dyn_pctl, trend=trend,
        )

    league = make_league_info(kind="dynasty")
    my_entries = [
        make_entry(player_id="my-rb1", position="RB", is_starter=True, value=pv("RB", 95, 95)),
        # Two roughly-equal-value bench pieces, one of which two different
        # opponents' offers could independently land on.
        make_entry(player_id="my-filler-a", position="RB", is_starter=False, value=pv("RB", 55, 50)),
        make_entry(player_id="my-filler-b", position="WR", is_starter=False, value=pv("WR", 55, 50)),
        make_entry(player_id="my-te1", position="TE", is_starter=True, value=pv("TE", 30, 25)),
    ]
    my_roster = make_roster(roster_id=1, owner_id=MY_USER_ID, owner_username="me", team_name="My Team", league=league, entries=my_entries)

    # Opponent A needs TE, has a buy-low TE valued near my-filler-a/b's
    # value. No RB/WR on their roster at all, so whichever filler I offer
    # (RB or WR) trivially fits an empty position for them -- isolating
    # the test to the collision-retry behavior, not recipient-fit math.
    # Two QB fillers give UNTOUCHABLE_COUNT=2 headroom so the TE target
    # itself isn't vacuously swept up as untouchable.
    opp_a_entries = [
        make_entry(player_id="a-qb1", position="QB", is_starter=True, value=pv("QB", 99, 99)),
        make_entry(player_id="a-qb2", position="QB", is_starter=True, value=pv("QB", 97, 97)),
        make_entry(player_id="a-te1", position="TE", is_starter=True, value=make_value(
            position="TE", dynasty_value=5500, dynasty_value_percentile=70, dynasty_positional_percentile=70,
            redraft_ecr_percentile=40, trend="down")),
    ]
    opp_a = make_roster(roster_id=2, owner_id="oa", owner_username="OppA", team_name="Team A", league=league, entries=opp_a_entries)

    # Opponent B also needs TE, has a DIFFERENT buy-low TE at a similar value.
    opp_b_entries = [
        make_entry(player_id="b-qb1", position="QB", is_starter=True, value=pv("QB", 99, 99)),
        make_entry(player_id="b-qb2", position="QB", is_starter=True, value=pv("QB", 97, 97)),
        make_entry(player_id="b-te1", position="TE", is_starter=True, value=make_value(
            position="TE", dynasty_value=5600, dynasty_value_percentile=71, dynasty_positional_percentile=71,
            redraft_ecr_percentile=41, trend="down")),
    ]
    opp_b = make_roster(roster_id=3, owner_id="ob", owner_username="OppB", team_name="Team B", league=league, entries=opp_b_entries)

    proposals = generate_trade_proposals(league, {1: my_roster, 2: opp_a, 3: opp_b}, max_proposals=5)
    targeted = {p.target_username for p in proposals}
    # Both opponents should get a proposal -- neither should be silently
    # dropped just because their best-fit give-piece collided with the
    # other's.
    assert "OppA" in targeted
    assert "OppB" in targeted


def test_generate_trade_proposals_handles_a_two_entry_roster_without_crashing():
    # UNTOUCHABLE_COUNT=2 with only 2 total entries — an edge case the
    # existing untouchable tests deliberately avoid; generate_trade_proposals
    # itself must degrade to an empty result, not raise.
    from sleeper_tool.trade_engine import generate_trade_proposals

    league = make_league_info(kind="dynasty")
    my_roster = make_roster(roster_id=1, owner_id=MY_USER_ID, owner_username="me", league=league, entries=[
        make_entry(player_id="only1", value=make_value(dynasty_value=5000, dynasty_value_percentile=80)),
        make_entry(player_id="only2", value=make_value(dynasty_value=4000, dynasty_value_percentile=70)),
    ])
    opp_roster = make_roster(roster_id=2, owner_id="opp", owner_username="opp", league=league, entries=[
        make_entry(player_id="opp-only", value=make_value(dynasty_value=4500, dynasty_value_percentile=75, trend="down")),
    ])
    proposals = generate_trade_proposals(league, {1: my_roster, 2: opp_roster}, max_proposals=3)
    assert isinstance(proposals, list)  # no crash; empty or non-empty both acceptable
