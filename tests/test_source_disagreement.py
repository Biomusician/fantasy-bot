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
    comparable,
    consensus_label,
    scaled_gap,
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


def _fill(pos, n, source):
    """Filler rows so a source's list at `pos` runs `n` deep (real lists do)."""
    if source == "ktc":
        return [_ktc(f"{source} filler {pos}{i}", pos, i) for i in range(1, n + 1)]
    if source == "rb":
        return [_rb(f"{source} filler {pos}{i}", pos, i) for i in range(1, n + 1)]
    return [_fp(f"{source} filler {pos}{i}", pos, i, i) for i in range(1, n + 1)]


def test_threshold_boundaries():
    assert consensus_label(None) is None
    assert consensus_label(STRONG_CONSENSUS_MAX_GAP) == STRONG_CONSENSUS
    assert consensus_label(STRONG_CONSENSUS_MAX_GAP + 1) == NORMAL_CONSENSUS
    assert consensus_label(SIGNIFICANT_RANK_GAP - 1) == NORMAL_CONSENSUS
    assert consensus_label(SIGNIFICANT_RANK_GAP) == SOURCE_DISAGREEMENT
    assert consensus_label(HIGH_RANK_GAP - 1) == SOURCE_DISAGREEMENT
    assert consensus_label(HIGH_RANK_GAP) == HIGH_DISAGREEMENT
    # At the top of a list the raw gap is the gap; deeper, the same gap counts for less.
    assert direction_label(1, 1 + SIGNIFICANT_RANK_GAP) == MARKET_ABOVE_PROJECTION
    assert direction_label(1 + SIGNIFICANT_RANK_GAP, 1) == PROJECTION_ABOVE_MARKET
    assert direction_label(1, 1 + SIGNIFICANT_RANK_GAP - 1) is None
    assert direction_label(51, 51 + SIGNIFICANT_RANK_GAP) is None  # 20 places at rank 51 is noise
    assert scaled_gap(51, 91) == 20.0  # 40 raw places at rank 51 = 20 top-of-list places
    assert direction_label(51, 91) == MARKET_ABOVE_PROJECTION
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
        "ktc": _snap([_ktc("X", "RB", 3), *_fill("RB", 30, "ktc")]), "fp_dynasty": _snap([_fp("X", "RB", 3, 5), *_fill("RB", 30, "fp")]),
        "fp_redraft": _snap([_fp("X", "RB", 3, 5), *_fill("RB", 30, "fp")]), "rotoballer": _snap([_rb("X", "RB", 3), *_fill("RB", 30, "rb")]),
    }, make_format()), "X")
    v = source_view("X", "RB", r, "dynasty")
    assert v.consensus == STRONG_CONSENSUS and v.direction is None and v.expert_note is None
    assert v.describe() == STRONG_CONSENSUS and not v.disagrees


def test_extreme_disagreement_and_direction_by_currency():
    snaps = {
        "ktc": _snap([_ktc("X", "WR", 5), *_fill("WR", 80, "ktc")]),
        "fp_dynasty": _snap([_fp("X", "WR", 50, 150, std=30.0, rmin=20, rmax=200), *_fill("WR", 80, "fp")]),
        "fp_redraft": _snap([_fp("X", "WR", 8, 20), *_fill("WR", 80, "fp")]),
        "rotoballer": _snap([_rb(f"WR{i}", "WR", i) for i in range(1, 60)] + [_rb("X", "WR", 61)]),
    }
    r = lookup(build_source_rank_tables(snaps, make_format()), "X")
    dyn = source_view("X", "WR", r, "dynasty")
    assert dyn.consensus == HIGH_DISAGREEMENT and dyn.consensus_gap == 45  # raw gap shown; 45/1.08 = 41.7 scaled clears 40
    assert dyn.direction == MARKET_ABOVE_PROJECTION  # KTC WR5 vs RotoBaller WR60
    assert "experts range #20-#200" in dyn.expert_note and "panel split" in dyn.expert_note
    assert dyn.disagrees
    rd = source_view("X", "WR", r, "redraft")
    assert rd.consensus == HIGH_DISAGREEMENT and rd.consensus_pair == ("FantasyPros", "RotoBaller")
    assert rd.direction == MARKET_ABOVE_PROJECTION and rd.market_rank == 8
    # In redraft the consensus pair IS the market-vs-projection pair: one clause, not two.
    assert rd.describe() == "Market Above Projection (WR8 market vs WR60 projection)"
    assert dyn.describe().startswith("High Disagreement: KTC vs FantasyPros dynasty differ by 45 WR places; Market Above Projection")


def test_a_rank_beyond_the_other_lists_depth_is_not_a_disagreement():
    # FantasyPros ranks 150 WRs, RotoBaller only 60: a WR120 on FP simply
    # doesn't exist on RotoBaller's list, so there is nothing to compare.
    snaps = {
        "ktc": None, "fp_dynasty": None,
        "fp_redraft": _snap([_fp("Deep", "WR", 120, 300), *_fill("WR", 150, "fp")]),
        "rotoballer": _snap([_rb("Deep", "WR", 60), *_fill("WR", 59, "rb")]),
    }
    r = lookup(build_source_rank_tables(snaps, make_format()), "Deep")
    assert not comparable(r, "fp_redraft", r.fp_redraft, "rotoballer", r.rotoballer)
    v = source_view("Deep", "WR", r, "redraft")
    assert v.consensus is None and v.direction is None and v.describe() is None


def test_projection_above_market_and_missing_sources():
    r = lookup(build_source_rank_tables({
        "ktc": _snap([_ktc("X", "TE", 30), *_fill("TE", 40, "ktc")]), "fp_dynasty": None, "fp_redraft": None,
        "rotoballer": _snap([_rb("X", "TE", 1), *_fill("TE", 40, "rb")]),
    }, make_format()), "X")
    v = source_view("X", "TE", r, "dynasty")
    assert v.consensus is None  # FP side missing: no consensus claim either way
    assert v.direction == PROJECTION_ABOVE_MARKET
    assert source_view("Nobody", "QB", None, "dynasty").describe() is None


def test_old_cache_rows_without_dispersion_fields_still_work():
    r = lookup(build_source_rank_tables({
        "ktc": _snap([_ktc("X", "QB", 4), *_fill("QB", 20, "ktc")]),
        "fp_dynasty": _snap([{"name": "X", "position": "QB", "pos_rank": "QB6", "rank_ecr": 7, "rank_std": 12.0}, *_fill("QB", 20, "fp")]),
        "fp_redraft": None, "rotoballer": None,
    }, make_format()), "X")
    v = source_view("X", "QB", r, "dynasty")
    assert v.consensus == STRONG_CONSENSUS  # KTC QB4 vs FP QB6: gap 2
    assert v.expert_note.startswith("expert panel split")
