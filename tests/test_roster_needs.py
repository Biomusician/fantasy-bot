from __future__ import annotations

import pytest
from waiver_fixtures import WEEK, market, player, roster

from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.roster_needs import (
    ADEQUATE,
    CRITICAL_BELOW_REPLACEMENT_RATIO,
    CRITICAL_NEED,
    DEPTH_COVER_RATIO,
    STRONG,
    STRONG_ABOVE_MEDIAN_RATIO,
    SURPLUS,
    WEAK,
    WEAK_BELOW_MEDIAN_RATIO,
    WEAK_BELOW_REPLACEMENT_RATIO,
    assess_roster_needs,
    group_of_slot,
)

FULL = ("QB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN")


def needs_for(r, *, lineups=None, mkt=None, free_agents=(), claim_week=None):
    return assess_roster_needs(
        r, lineup=optimize_lineup(r), lineups=lineups, market=mkt, free_agents=free_agents,
        current_week=WEEK, claim_week=claim_week,
    )


def base_roster(wr_b_proj, *, positions=FULL, extra=()):
    entries = [
        # rb2 is comfortably the best FLEX option, so a bench WR added by a
        # test stays on the bench instead of quietly entering the lineup.
        player("qb1", "QB", 22), player("rb1", "RB", 18), player("rb2", "RB", 20),
        player("wr_a", "WR", 30), player("wr_b", "WR", wr_b_proj), player("te1", "TE", 9),
        *extra,
    ]
    return roster(entries, positions=positions)


# -- groups ---------------------------------------------------------------------------


def test_group_of_slot_maps_superflex_to_qb_and_every_flex_variant_to_flex():
    assert group_of_slot("SUPER_FLEX") == "QB"
    assert group_of_slot("QB") == "QB"
    assert [group_of_slot(s) for s in ("FLEX", "WRRB_FLEX", "REC_FLEX")] == ["FLEX"] * 3
    assert group_of_slot("K") is None and group_of_slot(None) is None


def test_only_groups_the_league_actually_starts_are_assessed():
    r = base_roster(20, positions=("QB", "RB", "WR", "WR", "TE", "BN"))
    assert set(needs_for(r).groups) == {"QB", "RB", "WR", "TE"}
    flexed = base_roster(20)
    assert set(needs_for(flexed).groups) == {"QB", "RB", "WR", "TE", "FLEX"}


def test_a_non_qb_in_superflex_is_a_qb_hole_not_a_flex_one():
    r = roster(
        [player("qb1", "QB", 22), player("rb1", "RB", 18), player("wr1", "WR", 16), player("wr2", "WR", 14)],
        positions=("QB", "RB", "WR", "SUPER_FLEX", "BN"),
    )
    needs = needs_for(r)
    qb = needs.groups["QB"]
    assert qb.label == CRITICAL_NEED
    assert qb.reasons[0] == "no QB to start in 1 QB slot(s)"
    assert "FLEX" not in needs.groups


def test_an_unfilled_slot_names_the_slot_it_could_not_fill():
    r = roster([player("qb1", "QB", 22), player("rb1", "RB", 18)], positions=("QB", "RB", "TE", "BN"))
    te = needs_for(r).groups["TE"]
    assert te.label == CRITICAL_NEED
    assert te.reasons[0] == "no one to start at TE"


# -- the league's starter-replacement level ---------------------------------------------


@pytest.mark.parametrize(
    "weakest,expected",
    [
        (18.74, CRITICAL_NEED),                              # just under 0.75 x 25
        (CRITICAL_BELOW_REPLACEMENT_RATIO * 25, WEAK),       # exactly at it
        (22.99, WEAK),                                       # just under 0.92 x 25
        (WEAK_BELOW_REPLACEMENT_RATIO * 25, ADEQUATE),       # exactly at it
        (26.0, ADEQUATE),
    ],
)
def test_replacement_level_bands(weakest, expected):
    needs = needs_for(base_roster(weakest), mkt=market(WR=25.0))
    assert needs.groups["WR"].label == expected


def test_critical_and_weak_replacement_sentences_quote_both_numbers():
    critical = needs_for(base_roster(10.0), mkt=market(WR=25.0)).groups["WR"]
    assert critical.reasons[0] == (
        "weakest starter wr_b projects 10.0/wk, far under the league's starter-replacement level (25.0/wk)"
    )
    weak = needs_for(base_roster(22.0), mkt=market(WR=25.0)).groups["WR"]
    assert weak.reasons[0] == "weakest starter wr_b (22.0/wk) is under the league's starter-replacement level (25.0/wk)"


def test_a_starter_with_no_projection_is_critical_without_claiming_he_projects_zero():
    needs = needs_for(base_roster(None), mkt=market(WR=25.0))
    wr = needs.groups["WR"]
    assert wr.label == CRITICAL_NEED
    assert wr.reasons[0] == (
        "weakest starter wr_b has no projection in this league's sources and nobody projected can replace him"
    )
    assert "projects 0.0/wk" not in " ".join(wr.reasons)
    assert wr.weakest_projection == 0.0


def test_no_market_means_no_replacement_rule_at_all():
    needs = needs_for(base_roster(1.0))
    assert needs.groups["WR"].label == ADEQUATE
    assert needs.groups["WR"].replacement_projection is None


# -- the league median ------------------------------------------------------------------


def other_lineups(*weakest_wr):
    out = {}
    for i, w in enumerate(weakest_wr, start=2):
        r = roster(
            [player(f"o{i}qb", "QB", 22), player(f"o{i}rb", "RB", 18), player(f"o{i}rb2", "RB", 17),
             player(f"o{i}wra", "WR", 40), player(f"o{i}wrb", "WR", w), player(f"o{i}te", "TE", 9)],
            positions=FULL, roster_id=i,
        )
        out[i] = optimize_lineup(r)
    return out


@pytest.mark.parametrize(
    "weakest,expected",
    [
        (WEAK_BELOW_MEDIAN_RATIO * 20 - 0.01, WEAK),
        (WEAK_BELOW_MEDIAN_RATIO * 20, ADEQUATE),
        (21.0, ADEQUATE),
    ],
)
def test_median_band(weakest, expected):
    needs = needs_for(base_roster(weakest), lineups=other_lineups(20.0, 20.0))
    wr = needs.groups["WR"]
    assert wr.league_median_projection == 20.0
    assert wr.label == expected


def test_trailing_the_median_names_the_league_typical_starter():
    needs = needs_for(base_roster(10.0), lineups=other_lineups(20.0, 20.0))
    assert needs.groups["WR"].reasons[0] == (
        "weakest starter wr_b (10.0/wk) trails the league's typical WR starter (20.0/wk)"
    )


def test_strong_needs_both_the_median_margin_and_one_cover_and_surplus_needs_two():
    weakest = STRONG_ABOVE_MEDIAN_RATIO * 20
    cover = DEPTH_COVER_RATIO * weakest
    lineups = other_lineups(20.0, 20.0)

    no_depth = needs_for(base_roster(weakest), lineups=lineups).groups["WR"]
    assert (no_depth.label, no_depth.depth) == (ADEQUATE, 0)

    one = base_roster(weakest, extra=[player("bench_wr1", "WR", cover)])
    strong = needs_for(one, lineups=lineups).groups["WR"]
    assert (strong.label, strong.depth) == (STRONG, 1)
    assert "1 bench player who can cover" in strong.reasons[0]

    two = base_roster(weakest, extra=[player("bench_wr1", "WR", cover), player("bench_wr2", "WR", cover)])
    surplus = needs_for(two, lineups=lineups).groups["WR"]
    assert (surplus.label, surplus.depth) == (SURPLUS, 2)
    assert "2 bench players who can cover" in surplus.reasons[0]


def test_a_bench_player_just_under_the_cover_ratio_is_not_depth():
    weakest = STRONG_ABOVE_MEDIAN_RATIO * 20
    r = base_roster(weakest, extra=[player("bench_wr1", "WR", DEPTH_COVER_RATIO * weakest - 0.01)])
    need = needs_for(r, lineups=other_lineups(20.0, 20.0)).groups["WR"]
    assert (need.depth, need.label) == (0, ADEQUATE)


def test_flex_replacement_level_is_the_lowest_flex_starter_in_the_league():
    lineups = other_lineups(20.0, 20.0)  # each other roster flexes its RB2 at 17
    needs = needs_for(base_roster(20.0), lineups=lineups, mkt=market(WR=10.0))
    assert needs.groups["FLEX"].replacement_projection == 17.0


# -- the claim week ---------------------------------------------------------------------


def cover_roster(fill_proj):
    entries = [player("wr1", "WR", 20, bye_week=5), player("wr2", "WR", 15)]
    if fill_proj is not None:
        entries.append(player("wr3", "WR", fill_proj))
    return roster(entries, positions=("WR", "WR", "BN", "BN"))


@pytest.mark.parametrize(
    "fill,expected",
    [(9.99, CRITICAL_NEED), (10.0, WEAK), (13.99, WEAK), (14.0, ADEQUATE), (18.0, ADEQUATE)],
)
def test_claim_week_cover_bands(fill, expected):
    needs = needs_for(cover_roster(fill), claim_week=5)
    assert needs.groups["WR"].label == expected
    assert needs.groups["WR"].claim_week_hole is True


def test_claim_week_sentences_name_the_week_the_starter_and_the_fill():
    critical = needs_for(cover_roster(6.0), claim_week=5).groups["WR"]
    assert critical.reasons[0] == "wr1 is out for week 5 and the best fill, wr3, projects 30% of him"
    weak = needs_for(cover_roster(12.0), claim_week=5).groups["WR"]
    assert weak.reasons[0] == "wr1 is out for week 5; the best fill, wr3, projects 60% of him"


def test_nothing_to_fill_the_slot_says_so():
    needs = needs_for(cover_roster(None), claim_week=5)
    assert needs.groups["WR"].reasons[0] == "wr1 is out for week 5 and nothing on the roster can fill his slot"


def test_no_claim_week_means_no_hole_check_at_all():
    needs = needs_for(cover_roster(6.0))
    assert needs.groups["WR"].claim_week_hole is False
    assert needs.groups["WR"].label == ADEQUATE
    assert needs.claim_week is None


# -- improvable -------------------------------------------------------------------------


def test_improvable_only_when_a_free_agent_beats_my_weakest_starter():
    fas = [player("fa_wr", "WR", 21), player("fa_te", "TE", 1)]
    needs = needs_for(base_roster(20.0), free_agents=fas)
    wr = needs.groups["WR"]
    assert (wr.best_free_agent, wr.best_free_agent_projection, wr.improvable) == ("fa_wr", 21.0, True)

    equal = needs_for(base_roster(21.0), free_agents=fas).groups["WR"]
    assert equal.improvable is False


def test_a_weak_group_the_wire_cannot_fix_is_still_weak():
    needs = needs_for(base_roster(10.0), mkt=market(WR=25.0), free_agents=[player("fa_wr", "WR", 2)])
    wr = needs.groups["WR"]
    assert wr.label == CRITICAL_NEED and wr.improvable is False


def test_flex_free_agents_include_every_flex_eligible_position():
    needs = needs_for(base_roster(20.0), free_agents=[player("fa_te", "TE", 31), player("fa_qb", "QB", 99)])
    assert needs.groups["FLEX"].best_free_agent == "fa_te"
    assert needs.groups["QB"].best_free_agent == "fa_qb"


# -- lookups ----------------------------------------------------------------------------


def test_lookup_helpers_and_describe():
    needs = needs_for(base_roster(10.0), mkt=market(WR=25.0))
    assert needs.label_for("WR") == CRITICAL_NEED
    assert needs.label_for("K") is None and needs.label_for(None) is None
    assert needs.for_position("WR") is needs.groups["WR"]
    assert needs.most_urgent(("QB", "WR", "TE")).group == "WR"
    assert needs.most_urgent(("K",)) is None
    assert needs.groups["WR"].describe().startswith("WR: Critical Need — weakest starter wr_b projects 10.0/wk")


def test_an_adequate_group_still_reports_its_weakest_starter():
    needs = needs_for(base_roster(20.0))
    assert needs.groups["WR"].reasons == ["weakest starter wr_b projects 20.0/wk"]
