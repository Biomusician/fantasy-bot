import pytest
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.contender_insurance import identify_fragile_starters, merge_insurance_into_waiver_targets
from sleeper_tool.team_status import CONTENDER, MIDDLING
from sleeper_tool.waiver_engine import INSURANCE as INSURANCE_TIER
from sleeper_tool.waiver_engine import WaiverTarget


def _p(pid, pos, proj, *, is_starter=False):
    # is_starter is Sleeper's set-lineup flag; the optimizer ignores it, but
    # waiver_engine's drop-candidate search only considers the bench.
    return make_entry(player_id=pid, name=pid, position=pos, is_starter=is_starter,
                      value=make_value(name=pid, position=pos, proj_points=proj))


def _fa(pid, pos, proj):
    return make_entry(player_id=pid, name=pid, position=pos, team="FA", is_starter=False,
                      value=make_value(name=pid, position=pos, proj_points=proj))


def _roster(entries, positions=("QB", "RB", "RB", "WR", "BN", "BN")):
    return make_roster(entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(kind="redraft"))


def test_flags_a_starter_whose_loss_drops_the_slot_below_65_percent_and_a_free_agent_fixes_it():
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("rb3", "RB", 50)])
    # Losing rb1: rb3 (50) steps in -> replacement 50/200 = 25% -> fragile.
    # Losing qb1: nobody -> replacement 0 -> fragile.
    recs = identify_fragile_starters(
        r, [_fa("fa_rb", "RB", 120), _fa("fa_qb", "QB", 200), _fa("fa_wr", "WR", 150)],
        team_status=CONTENDER, max_recommendations=10,
    )
    by_starter = {x.starter.player_id: x for x in recs}
    assert set(by_starter) == {"qb1", "rb1", "rb2", "wr1"}  # rb2's loss also leaves only rb3 (33%)
    assert by_starter["rb1"].replacement_projection == pytest.approx(50)
    assert by_starter["rb1"].replacement_ratio == pytest.approx(0.25)
    assert by_starter["rb1"].candidate.player_id == "fa_rb"
    assert by_starter["rb1"].restored_projection == pytest.approx(120)
    assert by_starter["qb1"].candidate.player_id == "fa_qb"
    assert recs[0].replacement_ratio == 0.0  # the unbackable slots (QB, WR) rank as most fragile


def test_a_well_backed_starter_is_not_fragile():
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("rb3", "RB", 140)])
    recs = identify_fragile_starters(r, [_fa("fa_rb", "RB", 500)], team_status=CONTENDER)
    assert all(x.starter.player_id != "rb1" for x in recs)  # rb3 covers 70% of rb1


def test_fragile_without_a_meaningful_free_agent_upgrade_is_not_reported():
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("rb3", "RB", 50)])
    # fa_rb at 55 improves the 50 replacement by 10% — under the 15% bar.
    recs = identify_fragile_starters(r, [_fa("fa_rb", "RB", 55)], team_status=CONTENDER)
    assert all(x.starter.player_id != "rb1" for x in recs)


def test_only_contenders_get_insurance():
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 200)])
    assert identify_fragile_starters(r, [_fa("fa_qb", "QB", 200)], team_status=MIDDLING) == []


def test_capped_at_two_most_fragile():
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180)])
    fas = [_fa("fa_qb", "QB", 200), _fa("fa_rb", "RB", 120), _fa("fa_wr", "WR", 150)]
    recs = identify_fragile_starters(r, fas, team_status=CONTENDER)
    assert len(recs) == 2
    assert [x.starter.player_id for x in recs] == ["qb1", "rb1"]  # every slot is at 0% replacement; tie-broken by lineup order


def test_merge_makes_one_waiver_row_per_candidate_and_ranks_it_after_normal_adds():
    r = _roster([
        _p("qb1", "QB", 300, is_starter=True), _p("rb1", "RB", 200, is_starter=True),
        _p("rb2", "RB", 150, is_starter=True), _p("wr1", "WR", 180, is_starter=True), _p("rb3", "RB", 50),
    ])
    recs = identify_fragile_starters(r, [_fa("fa_rb", "RB", 120)], team_status=CONTENDER, max_recommendations=10)
    assert {x.starter.player_id for x in recs} == {"rb1", "rb2"}  # same free agent covers both
    normal = WaiverTarget(player_id="hot", name="Hot Add", position="WR", team="KC", trend_count=9, value=None,
                          fills_need=True, need_rank=0, reason="trending", priority_tier="Must Add")
    merged = merge_insurance_into_waiver_targets([normal], recs, r, current_week=1, deadline_passed=False, waiver_budget=100)
    assert [t.player_id for t in merged] == ["hot", "fa_rb"]
    row = merged[1]
    assert row.priority_tier == INSURANCE_TIER
    assert "rb1" in row.reason and "rb2" in row.reason and row.reason.startswith("Insurance for")
    assert row.drop_candidate.player_id == "rb3"  # the paired cut is the bench body he'd replace
    assert row.suggested_faab_pct == 10  # a FAAB league gets a bid, not the "not a FAAB league" None
    no_faab = merge_insurance_into_waiver_targets([normal], recs, r, current_week=1, deadline_passed=False)
    assert no_faab[1].suggested_faab_pct is None
    # After the trade deadline, insurance is the more urgent row.
    merged = merge_insurance_into_waiver_targets([normal], recs, r, current_week=1, deadline_passed=True)
    assert [t.player_id for t in merged] == ["fa_rb", "hot"]


def test_merge_annotates_an_existing_trending_row_instead_of_duplicating_it():
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("rb3", "RB", 50)])
    recs = identify_fragile_starters(r, [_fa("fa_rb", "RB", 120)], team_status=CONTENDER)
    already = WaiverTarget(player_id="fa_rb", name="fa_rb", position="RB", team="FA", trend_count=9, value=None,
                           fills_need=True, need_rank=0, reason="trending", priority_tier="Strong Add")
    merged = merge_insurance_into_waiver_targets([already], recs, r, current_week=1, deadline_passed=False)
    assert len(merged) == 1 and merged[0].priority_tier == "Strong Add"
    assert "also insurance for" in merged[0].reason


def test_cascading_reshuffle_is_handled_by_the_optimizer_not_a_single_backup():
    # Losing rb1 pulls wr2 out of FLEX? No: FLEX-eligible wr2 stays; rb2 slides
    # into RB and the lineup loses only the weakest link. Effective
    # replacement therefore reflects the whole reshuffle.
    r = _roster(
        [_p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("wr2", "WR", 100), _p("rb3", "RB", 90)],
        positions=("RB", "RB", "WR", "FLEX", "BN"),
    )
    recs = identify_fragile_starters(r, [_fa("fa_rb", "RB", 170)], team_status=CONTENDER)
    rb1 = next(x for x in recs if x.starter.player_id == "rb1")
    # Baseline 200+150+180+100=630; without rb1: 150+90+180+100=520 -> drop 110 -> replacement 90.
    assert rb1.replacement_projection == pytest.approx(90)
    assert rb1.restored_projection == pytest.approx(170)


def test_a_free_agent_who_out_projects_the_starter_is_an_upgrade_not_insurance():
    """Insurance is cover for losing a starter. A free agent who would
    restore the slot to at least what the starter projects is a straight
    upgrade the waiver board already handles — calling him "insurance" would
    file a starter swap under injury planning."""
    r = _roster([_p("qb1", "QB", 300), _p("rb1", "RB", 200), _p("rb2", "RB", 150), _p("wr1", "WR", 180), _p("rb3", "RB", 50)])

    # Losing rb1 (200) leaves rb3 (50) — fragile. A 199-projection free
    # agent restores 199 < 200: cover.
    cover = identify_fragile_starters(r, [_fa("fa_rb", "RB", 199)], team_status=CONTENDER, max_recommendations=10)
    rb1_cover = [x for x in cover if x.starter.player_id == "rb1"]
    assert rb1_cover and rb1_cover[0].restored_projection == pytest.approx(199)

    # At exactly the starter's own projection it stops being insurance.
    equal = identify_fragile_starters(r, [_fa("fa_rb", "RB", 200)], team_status=CONTENDER, max_recommendations=10)
    assert all(x.starter.player_id != "rb1" for x in equal)

    # And above it, likewise.
    better = identify_fragile_starters(r, [_fa("fa_rb", "RB", 260)], team_status=CONTENDER, max_recommendations=10)
    assert all(x.starter.player_id != "rb1" for x in better)
