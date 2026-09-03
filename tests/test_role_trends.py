from sleeper_tool.role_trends import (
    ABS_CARRIES_RISE,
    ABS_TARGETS_RISE,
    CARRY_SHARE_RISE,
    COLLAPSING,
    CONFIRM,
    DESCRIBE_MAX_SIGNALS,
    FALLING,
    INSUFFICIENT,
    MARKET_AHEAD,
    NO_HISTORY_NOTE,
    OPPORTUNITY_SHARE_RISE,
    RISING,
    ROLE_AHEAD,
    SNAP_SHARE_RISE,
    STABLE,
    SURGE_ONE_WEEK_SNAP_JUMP,
    SURGING,
    TARGET_SHARE_RISE,
    TEAMMATE_MIN_LOSS,
    TEAMMATE_OVERTAKE_MIN_GAIN,
    VOLATILITY_MIN_GAMES,
    VOLATILITY_SNAP_STDEV,
    market_cross,
    prior_season_baseline,
    role_trend,
)
from usage_fixtures import make_player_week, make_team_week, make_usage


def _weekly(rows, *, team_targets=30.0, team_carries=20.0, gsis="g1", position="WR"):
    """rows: {week: dict of per-week stats} -> UsageData for one player."""
    weeks = sorted(rows)
    return make_usage(
        [make_player_week(gsis, w, position=position, **rows[w]) for w in weeks],
        team_weeks=[make_team_week("KC", w, targets=team_targets, carries=team_carries) for w in weeks],
    )


def _flat_four(*, baseline: dict, recent: dict, **kwargs):
    """Two identical baseline games then two identical recent games — the
    shape every threshold-boundary test uses."""
    return _weekly({1: baseline, 2: baseline, 3: recent, 4: recent}, **kwargs)


def test_no_season_and_one_game_are_both_insufficient_but_say_different_things():
    none_yet = role_trend(None, "g1")
    assert none_yet.label == INSUFFICIENT and none_yet.note == NO_HISTORY_NOTE
    assert none_yet.history_available is False and none_yet.games == 0

    one_game = role_trend(_weekly({1: {"targets": 8.0}}), "g1")
    assert one_game.label == INSUFFICIENT and one_game.history_available and one_game.games == 1
    assert "1 played game" in one_game.note


def test_two_identical_weeks_are_a_stable_role():
    trend = role_trend(_weekly({1: {"targets": 5.0, "snap_pct": 0.6}, 2: {"targets": 5.0, "snap_pct": 0.6}}), "g1")
    assert trend.label == STABLE and trend.games == 2 and trend.components == []


def test_a_one_game_spike_is_not_a_trend():
    # Targets quadruple, but one game is a game script, not a role.
    trend = role_trend(_weekly({1: {"targets": 3.0, "snap_pct": 0.50}, 2: {"targets": 12.0, "snap_pct": 0.55}}), "g1")
    assert trend.label == STABLE and "structural" in trend.note


def test_a_structural_one_week_snap_change_can_label_from_two_games():
    jump = _weekly({1: {"snap_pct": 0.40}, 2: {"snap_pct": 0.40 + SURGE_ONE_WEEK_SNAP_JUMP}})
    surging = role_trend(jump, "g1")
    assert surging.label == SURGING and surging.games == 2
    assert [c.name for c in surging.components] == ["snap share"]

    collapse = _weekly({1: {"snap_pct": 0.70}, 2: {"snap_pct": 0.70 - SURGE_ONE_WEEK_SNAP_JUMP}})
    assert role_trend(collapse, "g1").label == COLLAPSING

    just_short = _weekly({1: {"snap_pct": 0.40}, 2: {"snap_pct": 0.40 + SURGE_ONE_WEEK_SNAP_JUMP - 0.001}})
    assert role_trend(just_short, "g1").label == STABLE


def test_the_one_week_structural_snap_jump_is_thirty_points():
    """Pinned by value and at absolute snap shares rather than at shares
    computed from the constant, so moving SURGE_ONE_WEEK_SNAP_JUMP has to
    move these numbers too."""
    assert SURGE_ONE_WEEK_SNAP_JUMP == 0.30
    assert role_trend(_weekly({1: {"snap_pct": 0.35}, 2: {"snap_pct": 0.65}}), "g1").label == SURGING  # +30 exactly
    assert role_trend(_weekly({1: {"snap_pct": 0.35}, 2: {"snap_pct": 0.64}}), "g1").label == STABLE   # +29
    assert role_trend(_weekly({1: {"snap_pct": 0.65}, 2: {"snap_pct": 0.35}}), "g1").label == COLLAPSING
    assert role_trend(_weekly({1: {"snap_pct": 0.64}, 2: {"snap_pct": 0.35}}), "g1").label == STABLE   # -29


def test_a_sustained_change_reads_rising_at_three_games_and_surging_when_it_is_big():
    rising = role_trend(_weekly({1: {"targets": 3.0, "snap_pct": 0.40}, 2: {"targets": 6.0, "snap_pct": 0.55}, 3: {"targets": 6.0, "snap_pct": 0.55}}), "g1")
    assert rising.label == RISING and rising.games == 3
    assert {c.direction for c in rising.components} == {"up"}

    # Two components at twice their threshold: snap share +20, targets +4/g.
    surging = role_trend(_weekly({1: {"targets": 3.0, "snap_pct": 0.40}, 2: {"targets": 7.0, "snap_pct": 0.60}, 3: {"targets": 7.0, "snap_pct": 0.60}}), "g1")
    assert surging.label == SURGING

    falling = role_trend(_weekly({1: {"targets": 6.0, "snap_pct": 0.55}, 2: {"targets": 3.0, "snap_pct": 0.40}, 3: {"targets": 3.0, "snap_pct": 0.40}}), "g1")
    assert falling.label == FALLING and {c.direction for c in falling.components} == {"down"}

    collapsing = role_trend(_weekly({1: {"targets": 7.0, "snap_pct": 0.60}, 2: {"targets": 3.0, "snap_pct": 0.40}, 3: {"targets": 3.0, "snap_pct": 0.40}}), "g1")
    assert collapsing.label == COLLAPSING


def test_each_component_threshold_fires_exactly_at_its_boundary():
    snap = _flat_four(baseline={"snap_pct": 0.50}, recent={"snap_pct": 0.50 + SNAP_SHARE_RISE})
    trend = role_trend(snap, "g1")
    assert [c.name for c in trend.components] == ["snap share"] and trend.label == RISING

    below = _flat_four(baseline={"snap_pct": 0.50}, recent={"snap_pct": 0.50 + SNAP_SHARE_RISE - 0.001})
    assert role_trend(below, "g1").label == STABLE

    # 30 team targets: +1.5 targets a game is exactly TARGET_SHARE_RISE and
    # stays under the absolute-targets and opportunity-share thresholds.
    targets_share = _flat_four(baseline={"targets": 3.0}, recent={"targets": 3.0 + TARGET_SHARE_RISE * 30})
    assert [c.name for c in role_trend(targets_share, "g1").components] == ["target share"]

    carry_share = _flat_four(baseline={"carries": 2.0}, recent={"carries": 2.0 + CARRY_SHARE_RISE * 20})
    assert [c.name for c in role_trend(carry_share, "g1").components] == ["carry share"]

    # +3.0 opportunities on a 50-opportunity offense, split so neither the
    # target-share nor the carry-share threshold is reached on its own.
    gain = OPPORTUNITY_SHARE_RISE * 50
    opportunity = _flat_four(baseline={"targets": 3.0, "carries": 2.0}, recent={"targets": 3.0 + gain * 0.497, "carries": 2.0 + gain * 0.503})
    assert [c.name for c in role_trend(opportunity, "g1").components] == ["opportunity share"]

    # Absolute counts, on an offense big enough that no share moves.
    abs_targets = _flat_four(baseline={"targets": 3.0}, recent={"targets": 3.0 + ABS_TARGETS_RISE}, team_targets=100.0)
    assert [c.name for c in role_trend(abs_targets, "g1").components] == ["targets"]

    abs_carries = _flat_four(baseline={"carries": 3.0}, recent={"carries": 3.0 + ABS_CARRIES_RISE}, team_carries=100.0)
    assert [c.name for c in role_trend(abs_carries, "g1").components] == ["carries"]


def test_falls_are_symmetrical_with_rises_at_the_boundary():
    fall = _flat_four(baseline={"snap_pct": 0.60}, recent={"snap_pct": 0.60 - SNAP_SHARE_RISE})
    trend = role_trend(fall, "g1")
    assert trend.label == FALLING and trend.components[0].direction == "down"


def test_a_volatile_snap_share_is_reported_as_its_own_component():
    # pstdev of (m-d, m, m+d) is d * sqrt(2/3); solve for exactly the threshold.
    d = VOLATILITY_SNAP_STDEV * (1.5 ** 0.5)
    usage = _weekly({
        1: {"snap_pct": 0.50}, 2: {"snap_pct": 0.50},
        3: {"snap_pct": 0.50 - d}, 4: {"snap_pct": 0.50}, 5: {"snap_pct": 0.50 + d},
    })
    trend = role_trend(usage, "g1")
    assert "snap volatility" in [c.name for c in trend.components]
    assert trend.label == STABLE  # volatility describes, it doesn't direct


def test_volatility_needs_three_snap_readings_in_the_window():
    """VOLATILITY_MIN_GAMES is a count of READINGS, not of games: a window
    of three games where one has no snap row cannot be called volatile,
    however far the two it does have are apart."""
    assert VOLATILITY_MIN_GAMES == 3
    d = VOLATILITY_SNAP_STDEV * (1.5 ** 0.5)  # a spread that would clear the bar with three
    usage = _weekly({
        1: {"snap_pct": 0.50}, 2: {"snap_pct": 0.50},
        3: {"snap_pct": 0.50 - d}, 4: {"snap_pct": None}, 5: {"snap_pct": 0.50 + d},
    })
    assert "snap volatility" not in [c.name for c in role_trend(usage, "g1").components]
    # The same three weeks with the middle reading present do clear it.
    with_all_three = _weekly({
        1: {"snap_pct": 0.50}, 2: {"snap_pct": 0.50},
        3: {"snap_pct": 0.50 - d}, 4: {"snap_pct": 0.50}, 5: {"snap_pct": 0.50 + d},
    })
    assert "snap volatility" in [c.name for c in role_trend(with_all_three, "g1").components]


def test_a_snap_spread_one_step_under_the_stdev_bar_is_not_volatile():
    assert VOLATILITY_SNAP_STDEV == 0.15
    under = (VOLATILITY_SNAP_STDEV - 0.001) * (1.5 ** 0.5)
    usage = _weekly({
        1: {"snap_pct": 0.50}, 2: {"snap_pct": 0.50},
        3: {"snap_pct": 0.50 - under}, 4: {"snap_pct": 0.50}, 5: {"snap_pct": 0.50 + under},
    })
    assert "snap volatility" not in [c.name for c in role_trend(usage, "g1").components]


def test_a_teammate_can_be_shown_taking_the_role():
    usage = make_usage(
        [make_player_week("g1", w, position="RB", targets=0.0, carries=15.0 if w <= 2 else 5.0) for w in (1, 2, 3, 4)]
        + [make_player_week("g2", w, position="RB", targets=0.0, carries=2.0 if w <= 2 else 14.0, name="Backfield Riser") for w in (1, 2, 3, 4)],
    )
    trend = role_trend(usage, "g1")
    overtaking = [c for c in trend.components if c.name == "teammate overtaking"]
    assert trend.label == COLLAPSING
    assert overtaking and "Backfield Riser" in overtaking[0].magnitude_text

    # The riser himself is not "overtaken" by anyone.
    assert [c for c in role_trend(usage, "g2").components if c.name == "teammate overtaking"] == []


def test_teammate_overtaking_needs_the_gain_to_cover_the_whole_loss():
    # g1 loses exactly 0.05 of opportunity share; g2 gains exactly 0.05.
    exact = make_usage(
        [make_player_week("g1", w, position="RB", targets=0.0, carries=5.0 if w <= 2 else 2.5) for w in (1, 2, 3, 4)]
        + [make_player_week("g2", w, position="RB", targets=0.0, carries=1.0 if w <= 2 else 3.5, name="Exact Riser") for w in (1, 2, 3, 4)],
    )
    assert any(c.name == "teammate overtaking" for c in role_trend(exact, "g1").components)

    smaller = make_usage(
        [make_player_week("g1", w, position="RB", targets=0.0, carries=5.0 if w <= 2 else 2.5) for w in (1, 2, 3, 4)]
        + [make_player_week("g2", w, position="RB", targets=0.0, carries=1.0 if w <= 2 else 3.0, name="Small Riser") for w in (1, 2, 3, 4)],
    )
    assert not any(c.name == "teammate overtaking" for c in role_trend(smaller, "g1").components)

    # A different position never counts as taking his role.
    other_position = make_usage(
        [make_player_week("g1", w, position="RB", targets=0.0, carries=5.0 if w <= 2 else 2.5) for w in (1, 2, 3, 4)]
        + [make_player_week("g2", w, position="WR", targets=0.0, carries=1.0 if w <= 2 else 3.5, name="Wideout") for w in (1, 2, 3, 4)],
    )
    assert not any(c.name == "teammate overtaking" for c in role_trend(other_position, "g1").components)


def _backfield(mine_before, mine_after, his_before, his_after, *, his_weeks=(1, 2, 3, 4), name="Riser"):
    """Two RBs on a 50-opportunity offense, so a carry is exactly 2 share
    points: carries 5.0 -> 2.5 is a 5-point loss."""
    return make_usage(
        [make_player_week("g1", w, position="RB", targets=0.0, carries=mine_before if w <= 2 else mine_after) for w in (1, 2, 3, 4)]
        + [make_player_week("g2", w, position="RB", targets=0.0, carries=his_before if w <= 2 else his_after, name=name) for w in his_weeks],
    )


def _overtaken(usage) -> bool:
    return any(c.name == "teammate overtaking" for c in role_trend(usage, "g1").components)


def test_a_trivial_loss_is_never_someone_taking_the_role():
    """TEAMMATE_MIN_LOSS: he has to have actually given something up. A
    teammate surging while this player holds steady is the offense growing,
    not a demotion — however large the teammate's gain."""
    assert TEAMMATE_MIN_LOSS == 0.02
    # 5.0 -> 4.5 carries is a 1-point loss, under the 2-point bar; the
    # teammate gains 10 points, which would otherwise be a loud signal.
    assert not _overtaken(_backfield(5.0, 4.5, 1.0, 6.0))
    # Exactly 2 points lost, with a gain that clears both bars: it fires.
    assert _overtaken(_backfield(5.0, 4.0, 1.0, 3.5))


def test_a_gain_that_covers_the_loss_but_is_itself_tiny_is_not_an_overtake():
    """TEAMMATE_OVERTAKE_MIN_GAIN: covering a 3-point loss with a 4-point
    gain is inside the noise of a single game script."""
    assert TEAMMATE_OVERTAKE_MIN_GAIN == 0.05
    assert not _overtaken(_backfield(5.0, 3.5, 1.0, 3.0))  # loses 3, teammate gains 4
    assert _overtaken(_backfield(5.0, 3.5, 1.0, 3.5))      # loses 3, teammate gains 5


def test_a_teammate_with_no_baseline_rows_is_skipped_not_read_as_a_zero_share():
    """A call-up who did not play the baseline weeks has no share to
    compare against. Treating his absence as 0% would make every promotion
    look like a takeover."""
    call_up = _backfield(5.0, 2.5, 0.0, 4.0, his_weeks=(3, 4), name="Call Up")
    assert not _overtaken(call_up)
    # The identical gain, with baseline rows behind it, does register.
    assert _overtaken(_backfield(5.0, 2.5, 0.0, 4.0, his_weeks=(1, 2, 3, 4), name="Full Season"))


def test_market_cross_reads_labels_only():
    rising = role_trend(_weekly({1: {"targets": 3.0, "snap_pct": 0.40}, 2: {"targets": 6.0, "snap_pct": 0.55}, 3: {"targets": 6.0, "snap_pct": 0.55}}), "g1")
    stable = role_trend(_weekly({1: {"targets": 5.0}, 2: {"targets": 5.0}, 3: {"targets": 5.0}}), "g1")
    nothing = role_trend(None, "g1")

    assert market_cross(rising, value_direction=None, velocity_label="Rapidly Rising", source_direction=None) == CONFIRM
    assert market_cross(rising, value_direction=None, velocity_label="Stable", source_direction=None) == ROLE_AHEAD
    assert market_cross(rising, value_direction="down", velocity_label=None, source_direction=None) == ROLE_AHEAD
    assert market_cross(stable, value_direction=None, velocity_label="Rising", source_direction=None) == MARKET_AHEAD
    assert market_cross(stable, value_direction=None, velocity_label="no change", source_direction=None) is None
    assert market_cross(rising, value_direction=None, velocity_label=None, source_direction=None) is None
    assert market_cross(nothing, value_direction="up", velocity_label="Rising", source_direction="up") is None
    # Labels that carry no direction ("Insufficient History", "Unmeasurable") are silence, not flat.
    assert market_cross(rising, value_direction=None, velocity_label="Insufficient History", source_direction=None) is None
    # One market vote: the measured direction of travel (velocity) outranks the value direction.
    assert market_cross(rising, value_direction="up", velocity_label="Falling", source_direction=None) == ROLE_AHEAD
    assert market_cross(rising, value_direction=None, velocity_label="Insufficient History", source_direction="up") == CONFIRM


def test_prior_season_baseline_is_labelled_and_never_reaches_a_trend():
    prior = _weekly({1: {"targets": 6.0, "snap_pct": 0.70}, 2: {"targets": 6.0, "snap_pct": 0.72}})
    text = prior_season_baseline(prior, "g1")
    assert text.startswith("2025 baseline:") and "snaps" in text and "2 games" in text
    assert prior_season_baseline(prior, "nobody") is None
    assert prior_season_baseline(None, "g1") is None


def test_describe_is_sparse_and_deterministic():
    usage = _weekly({1: {"targets": 3.0, "snap_pct": 0.40}, 2: {"targets": 7.0, "snap_pct": 0.60}, 3: {"targets": 7.0, "snap_pct": 0.60}})
    trend = role_trend(usage, "g1")
    text = trend.describe()
    # Falsifiable only if there is something to leave out: this trend has
    # more components than describe() is allowed to print.
    assert len(trend.components) > DESCRIBE_MAX_SIGNALS
    assert text.startswith(f"{SURGING} (3 games):")
    assert text.count(",") == DESCRIBE_MAX_SIGNALS - 1  # one separator per shown signal beyond the first
    shown = [c for c in trend.components if c.describe() in text]
    assert shown == trend.components[:DESCRIBE_MAX_SIGNALS]  # the first N, in order, and no others

    reversed_rows = make_usage(list(reversed(usage.player_weeks)), team_weeks=list(reversed(usage.team_weeks)))
    assert role_trend(reversed_rows, "g1") == trend
