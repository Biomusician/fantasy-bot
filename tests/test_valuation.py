import datetime as dt

from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.valuation import (
    LeagueFormat,
    PlayerValue,
    ValuationEngine,
    derive_league_format,
    scale_proj_points_for_games_remaining,
)


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


# --- Missing ranking sources ---------------------------------------------
#
# rankings.cache now refuses to serve a cached snapshot past its freshness
# ceiling, so "this source is simply gone for this run" is a real state the
# engine has to survive rather than a hypothetical. `None` for a source means
# absent; omitting the argument entirely still means "fetch it".


def _ktc_snapshot(rows):
    return RankingSnapshot(source="ktc_dynasty", fetched_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc), payload=rows)


def _ktc_row(name, position, value, rank, pos_rank):
    block = {"value": value, "rank": rank, "positional_rank": pos_rank}
    return {"name": name, "position": position, "one_qb": block, "superflex": block}


def _snapshot(source, rows):
    return RankingSnapshot(source=source, fetched_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc), payload=rows)


def _fmt():
    return derive_league_format({"scoring_settings": {"rec": 1.0}, "roster_positions": ["QB", "RB", "WR", "TE", "BN"]})


def test_engine_with_every_source_absent_still_values_a_player():
    engine = ValuationEngine(ktc_snapshot=None, fp_snapshots=None, rb_snapshots=None)
    pv = engine.value_player("Ja'Marr Chase", _fmt(), position="WR")
    assert pv.dynasty_value is None
    assert pv.dynasty_rank is None
    assert pv.dynasty_value_percentile is None
    assert pv.proj_points is None
    assert pv.sources_used == []
    assert pv.cross_source_agreement == "insufficient_data"
    assert engine.missing_sources == ["ktc_dynasty"]
    assert engine.source_freshness() == {}


def test_absent_ktc_leaves_the_other_sources_working():
    engine = ValuationEngine(
        ktc_snapshot=None,
        fp_snapshots={"dynasty_1qb": _snapshot("fp", [{"name": "Bijan Robinson", "rank_ecr": 3, "position": "RB"}])},
        rb_snapshots={"full_ppr": _snapshot("rb", [{"name": "Bijan Robinson", "proj_points_ppr": 260.0}])},
    )
    pv = engine.value_player("Bijan Robinson", _fmt(), position="RB")
    assert pv.dynasty_value is None
    assert pv.dynasty_ecr_rank == 3
    assert pv.proj_points == 260.0
    assert set(pv.sources_used) == {"fantasypros_dynasty", "rotoballer"}
    assert engine.missing_sources == ["ktc_dynasty"]
    assert set(engine.source_freshness()) == {"fantasypros_dynasty_1qb", "rotoballer_full_ppr"}


def test_one_dead_fantasypros_page_does_not_take_the_others_down(monkeypatch):
    from sleeper_tool.rankings import fantasypros

    def fake_fp(page_key):
        if page_key == "dynasty_1qb":
            raise RuntimeError("cached snapshot is past its ceiling")
        return _snapshot(f"fp_{page_key}", [{"name": "Puka Nacua", "rank_ecr": 9, "position": "WR"}])

    monkeypatch.setattr(fantasypros, "get_fp_rankings", fake_fp)
    engine = ValuationEngine(
        ktc_snapshot=_ktc_snapshot([_ktc_row("Puka Nacua", "WR", 7000, 8, 4)]),
        rb_snapshots=None,
    )
    assert engine.missing_sources == ["fantasypros_dynasty_1qb"]
    assert engine.fp_snapshots["dynasty_1qb"] is None
    assert engine.fp_snapshots["dynasty_superflex"] is not None
    # The absent page is the dynasty one this 1QB league would have used, so
    # the ECR rank drops out while KTC and the other pages carry on.
    pv = engine.value_player("Puka Nacua", _fmt(), position="WR")
    assert pv.dynasty_value == 7000
    assert pv.dynasty_ecr_rank is None
    assert pv.redraft_ecr_rank == 9
    assert "fantasypros_dynasty_1qb" not in engine.source_freshness()


def test_absent_source_is_reported_absent_not_stale():
    engine = ValuationEngine(
        ktc_snapshot=_ktc_snapshot([_ktc_row("Jayden Daniels", "QB", 8000, 5, 2)]),
        fp_snapshots={"dynasty_1qb": None},
        rb_snapshots={"full_ppr": None},
    )
    # Omitted, never given a fabricated age.
    assert list(engine.source_freshness()) == ["ktc_dynasty"]
    assert engine.missing_sources == ["fantasypros_dynasty_1qb", "rotoballer_full_ppr"]
    assert engine.snapshots_for(_fmt())["fp_dynasty"] is None


def test_snapshots_for_reports_absent_ktc_as_none():
    engine = ValuationEngine(ktc_snapshot=None, fp_snapshots=None, rb_snapshots=None)
    assert engine.snapshots_for(_fmt())["ktc"] is None
