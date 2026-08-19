from sleeper_tool.valuation import LeagueFormat, PlayerValue, derive_league_format, scale_proj_points_for_games_remaining


def test_derive_league_format_handles_explicit_none_scoring_settings():
    # Regression: Sleeper can return the key present but explicitly null
    # (e.g. a league mid-creation) -- `.get(key, default)` doesn't catch
    # that, only `.get(key) or default` does. Previously crashed with
    # AttributeError: 'NoneType' object has no attribute 'get'.
    fmt = derive_league_format({"scoring_settings": None, "roster_positions": ["QB", "RB"]})
    assert fmt.ppr == 0.0
    assert fmt.pass_td_pts == 4.0


def test_derive_league_format_handles_explicit_none_roster_positions():
    # Regression: previously crashed with
    # TypeError: argument of type 'NoneType' is not iterable
    fmt = derive_league_format({"scoring_settings": {"rec": 1.0}, "roster_positions": None})
    assert fmt.qb_format == "1QB"
    assert fmt.ppr == 1.0
    assert fmt.starter_slots == {}


def test_derive_league_format_1qb():
    league_data = {
        "scoring_settings": {"rec": 1.0, "pass_td": 4.0},
        "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN"],
    }
    fmt = derive_league_format(league_data)
    assert fmt.qb_format == "1QB"
    assert fmt.is_superflex is False
    assert fmt.ppr == 1.0


def test_derive_league_format_detects_superflex_slot():
    league_data = {
        "scoring_settings": {"rec": 0.5},
        "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "BN"],
    }
    fmt = derive_league_format(league_data)
    assert fmt.is_superflex is True


def test_derive_league_format_detects_two_qb_slots_as_superflex():
    # Some leagues run true 2-QB rather than a SUPER_FLEX slot — same effect.
    league_data = {"scoring_settings": {}, "roster_positions": ["QB", "QB", "RB", "WR"]}
    fmt = derive_league_format(league_data)
    assert fmt.is_superflex is True


def test_derive_league_format_reads_te_premium_and_rush_bonus():
    league_data = {
        "scoring_settings": {"rec": 1.0, "bonus_rec_te": 0.5, "bonus_rush_yd_100": 2.0, "pass_td": 6.0},
        "roster_positions": ["QB"],
    }
    fmt = derive_league_format(league_data)
    assert fmt.te_premium_bonus == 0.5
    assert fmt.rush_100_bonus == 2.0
    assert fmt.pass_td_pts == 6.0


def test_derive_league_format_defaults_pass_td_to_4_when_missing():
    fmt = derive_league_format({"scoring_settings": {}, "roster_positions": []})
    assert fmt.pass_td_pts == 4.0


def test_te_premium_tier_snaps_to_nearest():
    base = dict(qb_format="1QB", ppr=1.0, rush_100_bonus=0.0, pass_td_pts=4.0)
    assert LeagueFormat(te_premium_bonus=0.5, **base).te_premium_tier == "tep"
    assert LeagueFormat(te_premium_bonus=1.0, **base).te_premium_tier == "tepp"
    assert LeagueFormat(te_premium_bonus=1.5, **base).te_premium_tier == "teppp"
    # An unusual value snaps to whichever fixed tier is closest.
    assert LeagueFormat(te_premium_bonus=0.75, **base).te_premium_tier == "tep"
    assert LeagueFormat(te_premium_bonus=0.9, **base).te_premium_tier == "tepp"  # closer to 1.0 than 0.5
    assert LeagueFormat(te_premium_bonus=1.3, **base).te_premium_tier == "teppp"  # closer to 1.5 than 1.0


def test_te_premium_tier_is_none_when_no_bonus():
    fmt = LeagueFormat(qb_format="1QB", ppr=1.0, te_premium_bonus=0.0, rush_100_bonus=0.0, pass_td_pts=4.0)
    assert fmt.te_premium_tier is None


def test_player_value_is_corroborated_requires_two_sources():
    single = PlayerValue(
        player_name="X", position="WR", dynasty_value=100, dynasty_rank=1, dynasty_positional_rank=1,
        dynasty_ecr_rank=None, redraft_ecr_rank=None, proj_points=None, ff_dynasty_rank=None, sources_used=["ktc"],
    )
    double = PlayerValue(
        player_name="X", position="WR", dynasty_value=100, dynasty_rank=1, dynasty_positional_rank=1,
        dynasty_ecr_rank=1, redraft_ecr_rank=None, proj_points=None, ff_dynasty_rank=None,
        sources_used=["ktc", "fantasypros_dynasty"],
    )
    assert single.is_corroborated is False
    assert double.is_corroborated is True


def test_scale_proj_points_week_1_is_unchanged():
    assert scale_proj_points_for_games_remaining(300.0, 1) == 300.0
    assert scale_proj_points_for_games_remaining(300.0, None) == 300.0


def test_scale_proj_points_shrinks_as_season_progresses():
    week5 = scale_proj_points_for_games_remaining(340.0, 5)  # 13 games left of 17
    week15 = scale_proj_points_for_games_remaining(340.0, 15)  # 3 games left of 17
    assert week5 == 340.0 * (13 / 17)
    assert week15 == 340.0 * (3 / 17)
    assert week15 < week5  # later in the season, less of the projection is still "ahead"


def test_scale_proj_points_floors_at_zero_past_season_end():
    # Week 18+ shouldn't produce a negative games-remaining count.
    assert scale_proj_points_for_games_remaining(300.0, 25) == 0.0


def test_thin_market_rank_threshold_is_150():
    from sleeper_tool.valuation import THIN_MARKET_RANK_THRESHOLD

    assert THIN_MARKET_RANK_THRESHOLD == 150


def test_panel_disagreement_does_not_flag_top_of_board():
    # Real-data regression: a naive ratio (std/rank) flags rank-2 players
    # with trivial std (near-total expert agreement) purely because the
    # denominator is small. The scaled-threshold formula must not do that.
    from sleeper_tool.valuation import is_panel_disagreement

    assert is_panel_disagreement(rank_ecr=2, rank_std=1.3) is False
    assert is_panel_disagreement(rank_ecr=7, rank_std=2.3) is False


def test_panel_disagreement_flags_genuine_disputes_deeper_in_the_board():
    from sleeper_tool.valuation import is_panel_disagreement

    # A rookie WR with real range-of-outcomes (real example from live data).
    assert is_panel_disagreement(rank_ecr=39, rank_std=11.5) is True
    # A steady, boring veteran at similar depth shouldn't flag.
    assert is_panel_disagreement(rank_ecr=39, rank_std=3.0) is False


def test_panel_disagreement_threshold_grows_with_rank_depth():
    from sleeper_tool.valuation import panel_disagreement_threshold

    assert panel_disagreement_threshold(10) > panel_disagreement_threshold(2)
    assert panel_disagreement_threshold(100) > panel_disagreement_threshold(10)
