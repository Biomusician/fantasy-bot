import datetime as dt

from conftest import make_format

from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.source_disagreement import (
    HIGH_DISAGREEMENT,
    HIGH_RANK_GAP,
    MARKET_ABOVE_PROJECTION,
    NORMAL_CONSENSUS,
    PROJECTION_ABOVE_MARKET,
    SIGNIFICANT_RANK_GAP,
    SOURCE_DISAGREEMENT,
    STRONG_CONSENSUS,
    STRONG_CONSENSUS_MAX_GAP,
    build_source_rank_tables,
    consensus_label,
    direction_label,
    lookup,
    source_view,
)


def _snap(payload):
    return RankingSnapshot(source="x", fetched_at=dt.datetime.now(dt.timezone.utc), payload=payload)


def _ktc(name, pos, one_qb_pos_rank, sf_pos_rank=None):
    block = {"value": 5000, "rank": 10, "positional_rank": one_qb_pos_rank}
    sf = {"value": 5000, "rank": 10, "positional_rank": sf_pos_rank if sf_pos_rank is not None else one_qb_pos_rank}
    return {"name": name, "position": pos, "one_qb": block, "superflex": sf}


def _fp(name, pos, pos_rank, ecr, std=1.0, rmin=None, rmax=None):
    return {"name": name, "position": pos, "pos_rank": f"{pos}{pos_rank}", "rank_ecr": ecr, "rank_std": std, "rank_min": rmin, "rank_max": rmax}


def _rb(name, pos, rank):
    return {"name": name, "position": pos, "rank": rank}


def test_threshold_boundaries():
    assert consensus_label(None) is None
    assert consensus_label(STRONG_CONSENSUS_MAX_GAP) == STRONG_CONSENSUS
    assert consensus_label(STRONG_CONSENSUS_MAX_GAP + 1) == NORMAL_CONSENSUS
    assert consensus_label(SIGNIFICANT_RANK_GAP - 1) == NORMAL_CONSENSUS
    assert consensus_label(SIGNIFICANT_RANK_GAP) == SOURCE_DISAGREEMENT
    assert consensus_label(HIGH_RANK_GAP) == HIGH_DISAGREEMENT
    assert direction_label(10, 10 + SIGNIFICANT_RANK_GAP) == MARKET_ABOVE_PROJECTION
    assert direction_label(10 + SIGNIFICANT_RANK_GAP, 10) == PROJECTION_ABOVE_MARKET
    assert direction_label(10, 10 + SIGNIFICANT_RANK_GAP - 1) is None
    assert direction_label(None, 5) is None


def test_positional_ranks_are_built_per_source_and_rotoballer_ranks_within_position():
    snaps = {
        "ktc": _snap([_ktc("A Player", "WR", 12, 15)]),
        "fp_dynasty": _snap([_fp("A Player", "WR", 40, 101, std=19.2, rmin=60, rmax=140)]),
        "fp_redraft": _snap([_fp("A Player", "WR", 30, 80)]),
        "rotoballer": _snap([_rb("Some QB", "QB", 1), _rb("Other WR", "WR", 5), _rb("A Player", "WR", 9)]),
    }
    table = build_source_rank_tables(snaps, make_format(qb_format="SF"))
    r = lookup(table, "A. Player")  # name normalization applies
    assert (r.ktc, r.fp_dynasty, r.fp_redraft, r.rotoballer) == (15, 40, 30, 2)  # RB: 2nd WR by overall rank
    assert (r.fp_dispersion["fp_dynasty"].rank_min, r.fp_dispersion["fp_dynasty"].rank_max) == (60, 140)
    assert r.fp_dispersion["fp_redraft"].rank_min is None  # the redraft list's spread never overwrites the dynasty one
    assert build_source_rank_tables(snaps, make_format(qb_format="1QB"))[list(table)[0]].ktc == 12


def test_identical_rankings_are_strong_consensus_and_no_direction():
    r = lookup(build_source_rank_tables({
        "ktc": _snap([_ktc("X", "RB", 3)]), "fp_dynasty": _snap([_fp("X", "RB", 3, 5)]),
        "fp_redraft": _snap([_fp("X", "RB", 3, 5)]), "rotoballer": _snap([_rb("X", "RB", 3)]),
    }, make_format()), "X")
    v = source_view("X", "RB", r, "dynasty")
    assert v.consensus == STRONG_CONSENSUS and v.direction is None and v.expert_note is None
    assert v.describe() == STRONG_CONSENSUS and not v.disagrees


def test_extreme_disagreement_and_direction_by_currency():
    snaps = {
        "ktc": _snap([_ktc("X", "WR", 5)]),
        "fp_dynasty": _snap([_fp("X", "WR", 50, 150, std=30.0, rmin=20, rmax=200)]),
        "fp_redraft": _snap([_fp("X", "WR", 8, 20)]),
        "rotoballer": _snap([_rb(f"WR{i}", "WR", i) for i in range(1, 60)] + [_rb("X", "WR", 61)]),
    }
    r = lookup(build_source_rank_tables(snaps, make_format()), "X")
    dyn = source_view("X", "WR", r, "dynasty")
    assert dyn.consensus == HIGH_DISAGREEMENT and dyn.consensus_gap == 45
    assert dyn.direction == MARKET_ABOVE_PROJECTION  # KTC WR5 vs RotoBaller WR60
    assert "experts range #20-#200" in dyn.expert_note and "panel split" in dyn.expert_note
    assert dyn.disagrees
    rd = source_view("X", "WR", r, "redraft")
    assert rd.consensus == HIGH_DISAGREEMENT and rd.consensus_pair == ("FantasyPros", "RotoBaller")
    assert rd.direction == MARKET_ABOVE_PROJECTION and rd.market_rank == 8


def test_projection_above_market_and_missing_sources():
    r = lookup(build_source_rank_tables({
        "ktc": _snap([_ktc("X", "TE", 30)]), "fp_dynasty": None, "fp_redraft": None,
        "rotoballer": _snap([_rb("X", "TE", 1)]),
    }, make_format()), "X")
    v = source_view("X", "TE", r, "dynasty")
    assert v.consensus is None  # FP side missing: no consensus claim either way
    assert v.direction == PROJECTION_ABOVE_MARKET
    assert source_view("Nobody", "QB", None, "dynasty").describe() is None


def test_old_cache_rows_without_dispersion_fields_still_work():
    r = lookup(build_source_rank_tables({
        "ktc": _snap([_ktc("X", "QB", 4)]),
        "fp_dynasty": _snap([{"name": "X", "position": "QB", "pos_rank": "QB6", "rank_ecr": 7, "rank_std": 12.0}]),
        "fp_redraft": None, "rotoballer": None,
    }, make_format()), "X")
    v = source_view("X", "QB", r, "dynasty")
    assert v.consensus == STRONG_CONSENSUS  # KTC QB4 vs FP QB6: gap 2
    assert v.expert_note.startswith("expert panel split")
