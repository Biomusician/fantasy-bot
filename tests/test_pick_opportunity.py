from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.pick_opportunity import SPENDABLE, STRATEGIC, USEFUL, assess_picks, position_units
from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD

POSITIONS = ("QB", "RB", "WR", "TE", "BN")


def _p(pid, pos, *, age, pctl):
    return make_entry(
        player_id=pid, name=pid, position=pos, age=age, is_starter=False,
        value=make_value(name=pid, position=pos, proj_points=100, dynasty_positional_percentile=pctl, dynasty_value_percentile=pctl),
    )


def _roster(rid, *, rb_age, rb_pctl, kind="dynasty"):
    entries = [
        _p(f"{rid}qb", "QB", age=27, pctl=70), _p(f"{rid}rb", "RB", age=rb_age, pctl=rb_pctl),
        _p(f"{rid}wr", "WR", age=26, pctl=70), _p(f"{rid}te", "TE", age=26, pctl=70),
    ]
    return make_roster(roster_id=rid, entries=entries, fmt=make_format(roster_positions=POSITIONS), league=make_league_info(kind=kind))


def _league(my_rb_age, my_rb_pctl, kind="dynasty"):
    mine = _roster(1, rb_age=my_rb_age, rb_pctl=my_rb_pctl, kind=kind)
    others = {rid: _roster(rid, rb_age=24, rb_pctl=80 - rid) for rid in range(2, 7)}  # 5 strong, young RB units
    return mine, {1: mine, **others}


def _pick(round_, season="2027"):
    return OwnedPick(season=season, round=round_, original_roster_id=1, tier="Mid", name=f"{season} Mid {round_}", value=3000)


def test_weak_aging_unit_is_bottom_three_and_older_than_the_league_median():
    mine, rosters = _league(my_rb_age=29, my_rb_pctl=20)
    units = {u.position: u for u in position_units(mine, rosters)}
    assert units["RB"].bottom_three and units["RB"].weak_aging
    assert units["RB"].strength_rank == 6 and units["RB"].teams == 6
    assert not units["QB"].weak_aging  # identical to everyone else's: rank 1 by tie order, not bottom-three...
    young_weak, rosters2 = _league(my_rb_age=22, my_rb_pctl=20)
    assert not {u.position: u for u in position_units(young_weak, rosters2)}["RB"].weak_aging


def test_weak_aging_respects_the_position_veteran_threshold():
    # RB at 29 vs a 24 median: old for an RB -> weak-aging. A QB unit at 29
    # vs a 24 median is bottom-three but not old for a QB (threshold 32).
    mine, rosters = _league(my_rb_age=29, my_rb_pctl=20)
    assert {u.position: u for u in position_units(mine, rosters)}["RB"].weak_aging
    for r in rosters.values():
        qb = next(e for e in r.entries if e.position == "QB")
        qb.age = 24.0
    my_qb = next(e for e in mine.entries if e.position == "QB")
    my_qb.age, my_qb.value.dynasty_positional_percentile = 29.0, 10.0
    qb_unit = {u.position: u for u in position_units(mine, rosters)}["QB"]
    assert qb_unit.bottom_three and qb_unit.avg_age > qb_unit.league_median_age
    assert not qb_unit.weak_aging


def test_first_round_pick_is_strategic_for_a_rebuilder_or_a_weak_aging_contender():
    mine, rosters = _league(my_rb_age=29, my_rb_pctl=20)
    assert assess_picks(mine, rosters, [_pick(1)], team_status=CONTENDER).assessments[0].classification == STRATEGIC
    healthy, rosters2 = _league(my_rb_age=24, my_rb_pctl=85)
    assert assess_picks(healthy, rosters2, [_pick(1)], team_status=CONTENDER).assessments[0].classification == USEFUL
    assert assess_picks(healthy, rosters2, [_pick(1)], team_status=REBUILD).assessments[0].classification == STRATEGIC


def test_second_round_pick_is_useful_with_a_bottom_three_unit_and_never_strategic():
    mine, rosters = _league(my_rb_age=29, my_rb_pctl=20)
    result = assess_picks(mine, rosters, [_pick(2)], team_status=REBUILD)
    assert result.assessments[0].classification == USEFUL
    healthy, rosters2 = _league(my_rb_age=24, my_rb_pctl=85)
    assert assess_picks(healthy, rosters2, [_pick(2)], team_status=MIDDLING).assessments[0].classification == SPENDABLE


def test_missing_unit_counts_as_weak_but_cannot_trigger_the_age_test():
    mine, rosters = _league(my_rb_age=24, my_rb_pctl=85)
    mine.entries = [e for e in mine.entries if e.position != "TE"]
    result = assess_picks(mine, rosters, [_pick(1), _pick(2)], team_status=CONTENDER)
    te = next(u for u in result.units if u.position == "TE")
    assert te.starters == 0 and te.bottom_three and not te.weak_aging
    by_round = {a.pick.round: a.classification for a in result.assessments}
    assert by_round == {1: USEFUL, 2: USEFUL}


def test_only_dynasty_leagues_and_only_rounds_one_and_two():
    mine, rosters = _league(my_rb_age=29, my_rb_pctl=20, kind="redraft")
    assert assess_picks(mine, rosters, [_pick(1)], team_status=CONTENDER) is None
    mine, rosters = _league(my_rb_age=29, my_rb_pctl=20)
    result = assess_picks(mine, rosters, [_pick(3), _pick(1)], team_status=CONTENDER)
    assert [a.pick.round for a in result.assessments] == [1]
    assert result.classification_for(_pick(1)) == STRATEGIC and result.classification_for(_pick(3)) is None


def test_a_pick_acquired_from_another_team_is_labelled_with_its_origin():
    mine, rosters = _league(my_rb_age=29, my_rb_pctl=20)
    rosters[2].team_name = "Bishop Sycamores"
    theirs = OwnedPick(season="2027", round=1, original_roster_id=2, tier="Early", name="2027 Early 1st", value=6000)
    result = assess_picks(mine, rosters, [theirs, _pick(1)], team_status=CONTENDER)
    names = [a.display_name for a in result.assessments]
    assert names == ["2027 Mid 1", "2027 Early 1st (via Bishop Sycamores)"] or names == ["2027 Early 1st (via Bishop Sycamores)", "2027 Mid 1"]
