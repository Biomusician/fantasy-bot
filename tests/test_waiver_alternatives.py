from __future__ import annotations

import pytest
from waiver_fixtures import WEEK, player

from sleeper_tool.waiver_alternatives import (
    ALT_ROS_RANK_BAND,
    FEW,
    MANY,
    SOME,
    UNIQUE,
    alternative_density,
    density_label,
)


def density(target, fas, **kw):
    return alternative_density(target, fas, current_week=WEEK, **kw)


# -- labels ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,label",
    [(0, UNIQUE), (1, FEW), (2, SOME), (3, SOME), (4, MANY), (9, MANY)],
)
def test_density_label_boundaries(count, label):
    assert density_label(count) == label


def test_label_boundaries_end_to_end():
    target = player("t", "WR", 10.0)
    pool = [player(f"fa{i}", "WR", 10.0) for i in range(4)]
    assert density(target, []).label == UNIQUE
    assert density(target, pool[:1]).label == FEW
    assert density(target, pool[:2]).label == SOME
    assert density(target, pool[:3]).label == SOME
    assert density(target, pool).label == MANY


# -- the projection band -------------------------------------------------------------


def test_relative_share_sets_the_band_for_a_well_projected_target():
    target = player("t", "WR", 10.0)  # band = max(1.0, 0.12 x 10) = 1.2
    assert density(target, [player("fa", "WR", 8.8)]).count == 1
    assert density(target, [player("fa", "WR", 8.79)]).count == 0


def test_the_absolute_floor_takes_over_for_a_low_projected_target():
    target = player("t", "WR", 5.0)  # band = max(1.0, 0.6) = 1.0
    assert density(target, [player("fa", "WR", 4.0)]).count == 1
    assert density(target, [player("fa", "WR", 3.99)]).count == 0


def test_better_players_count_as_substitutes_too():
    target = player("t", "WR", 10.0)
    d = density(target, [player("fa_better", "WR", 30.0)])
    assert (d.count, d.substitutes) == (1, ["fa_better"])


def test_a_free_agent_with_no_projection_is_not_a_substitute():
    assert density(player("t", "WR", 10.0), [player("fa", "WR", None)]).count == 0


# -- the ROS rank band ----------------------------------------------------------------


def test_same_position_substitute_must_sit_inside_the_ros_rank_band():
    target = player("t", "WR", 10.0)
    ranks = {"t": 30, "inside": 30 + ALT_ROS_RANK_BAND, "outside": 30 + ALT_ROS_RANK_BAND + 1}
    fas = [player("inside", "WR", 10.0), player("outside", "WR", 10.0)]
    d = density(target, fas, ros_pos_rank=lambda e: ranks.get(e.player_id))
    assert d.substitutes == ["inside"]


def test_a_better_ranked_free_agent_is_never_excluded_by_the_band():
    ranks = {"t": 60, "fa": 2}
    d = density(player("t", "WR", 10.0), [player("fa", "WR", 10.0)], ros_pos_rank=lambda e: ranks.get(e.player_id))
    assert d.count == 1


def test_the_rank_band_only_applies_within_the_targets_own_position():
    ranks = {"t": 10, "fa_rb": 900}
    d = density(
        player("t", "WR", 10.0), [player("fa_rb", "RB", 10.0)],
        positions=("RB", "WR"), ros_pos_rank=lambda e: ranks.get(e.player_id),
    )
    assert d.substitutes == ["fa_rb"]


def test_a_missing_rank_on_either_side_leaves_the_projection_test_alone():
    target = player("t", "WR", 10.0)
    fas = [player("fa", "WR", 10.0)]
    assert density(target, fas, ros_pos_rank=lambda e: None).count == 1
    assert density(target, fas, ros_pos_rank=lambda e: 400 if e.player_id == "fa" else None).count == 1


# -- eligibility ------------------------------------------------------------------------


def test_positions_defaults_to_the_targets_own_position():
    target = player("t", "WR", 10.0)
    fas = [player("fa_rb", "RB", 10.0), player("fa_wr", "WR", 10.0)]
    assert density(target, fas).substitutes == ["fa_wr"]


def test_the_roster_problems_positions_widen_the_pool():
    target = player("t", "WR", 10.0)
    fas = [player("fa_rb", "RB", 12.0), player("fa_te", "TE", 11.0), player("fa_qb", "QB", 40.0)]
    d = density(target, fas, positions=("RB", "WR", "TE"))
    assert d.substitutes == ["fa_rb", "fa_te"]


def test_the_target_himself_and_excluded_ids_never_count():
    target = player("t", "WR", 10.0)
    fas = [target, player("mine", "WR", 10.0), player("other", "WR", 10.0)]
    d = density(target, fas, exclude_ids=["mine"])
    assert d.substitutes == ["other"]


# -- reporting ---------------------------------------------------------------------------


def test_substitutes_are_listed_best_projection_first_then_by_name():
    target = player("t", "WR", 10.0)
    fas = [player("c", "WR", 11.0), player("a", "WR", 12.0), player("b", "WR", 11.0)]
    assert density(target, fas).substitutes == ["a", "b", "c"]


def test_describe_shows_three_names_and_counts_the_rest():
    target = player("t", "WR", 10.0)
    fas = [player(n, "WR", 10.0) for n in ("a", "b", "c", "d", "e")]
    assert density(target, fas).describe() == "Many Alternatives: a, b, c and 2 more"
    assert density(target, []).describe() == "Unique Opportunity: no comparable free agent on this wire"
    assert density(target, fas[:2]).describe() == "Some Alternatives: a, b"


# -- the unprojected target ----------------------------------------------------------------


def test_an_unprojected_target_reads_unique_with_no_measured_substitutes():
    target = player("t", "WR", None)
    d = density(target, [player("fa", "WR", 10.0)])
    assert (d.label, d.count, d.substitutes, d.player_id) == (UNIQUE, 0, [], "t")
