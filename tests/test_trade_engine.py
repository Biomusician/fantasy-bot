from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.trade_engine import (
    CONTENDER,
    MIDDLING,
    REBUILD,
    _find_matching_offer,
    _tradeable_pool,
    identify_buy_low,
    identify_needs,
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
    old_rb = _buy_low_candidate(position="RB", age=30.0, dynasty_value=3000, dynasty_value_percentile=40.0, redraft_ecr_percentile=20.0)
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
