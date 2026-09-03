from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.lineup_optimizer import optimize_lineup
from sleeper_tool.roster_consolidation import MAX_PER_TEAM, MIN_WEEKLY_IMPROVEMENT, STRONG_MIDDLING_MIN_PERCENTILE, eligible, find_consolidations
from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD, TeamStatusResult

POSITIONS = ("QB", "RB", "RB", "WR", "BN", "BN", "BN")


def _p(pid, pos, proj, value, pctl, *, starter=True, age=25.0):
    return make_entry(
        player_id=pid, name=pid, position=pos, is_starter=starter, age=age,
        value=make_value(name=pid, position=pos, proj_points=proj, dynasty_value=value, dynasty_value_percentile=pctl, dynasty_positional_percentile=pctl),
    )


def _roster(rid, entries, owner):
    return make_roster(roster_id=rid, owner_id=owner, owner_username=owner, team_name=f"Team {owner}", entries=entries,
                       fmt=make_format(roster_positions=POSITIONS), league=make_league_info(kind="dynasty"))


def _me():
    return _roster(1, [
        _p("qb1", "QB", 340, 8000, 95), _p("wr1", "WR", 250, 7000, 92),
        _p("rb1", "RB", 200, 4000, 70), _p("rb2", "RB", 100, 1500, 30),
        _p("rbA", "RB", 140, 2600, 52, starter=False), _p("rbB", "RB", 110, 2500, 50, starter=False), _p("wrB", "WR", 90, 1200, 25, starter=False),
    ], "me")


def _them(star_proj=300, star_value=5000):
    return _roster(2, [
        _p("qb2", "QB", 340, 8500, 96), _p("wr2", "WR", 250, 7500, 94),
        _p("rbStar", "RB", star_proj, star_value, 80), _p("rb_weak", "RB", 60, 500, 12), _p("wr_weak", "WR", 70, 600, 15),
    ], "them")


def _status(kind, pctl=80.0):
    return TeamStatusResult(status=kind, strength_percentile=pctl, win_pct=None, games_played=0, reason="r")


def _run(me, them, status=_status(CONTENDER), fas=()):
    rosters = {1: me, 2: them}
    return find_consolidations(
        make_league_info(kind="dynasty"), me, rosters, status_result=status, status_of={2: MIDDLING},
        lineup=optimize_lineup(me), free_agents=list(fas), current_week=1,
    )


def test_eligibility_contenders_and_strong_middling_only():
    assert eligible(_status(CONTENDER, 40.0))
    assert eligible(_status(MIDDLING, STRONG_MIDDLING_MIN_PERCENTILE))
    assert not eligible(_status(MIDDLING, STRONG_MIDDLING_MIN_PERCENTILE - 0.1))
    assert not eligible(_status(REBUILD, 90.0)) and not eligible(None)
    assert _run(_me(), _them(), status=_status(REBUILD)) == []


def test_two_bench_pieces_for_a_lineup_upgrade():
    out = _run(_me(), _them())
    assert len(out) == 1
    c = out[0]
    p = c.proposal
    assert [e.player_id for e in p.give] == ["rbA", "rbB"] and p.receive[0].player_id == "rbStar"
    assert p.trade_type == "consolidation" and len(p.give) == 2  # never 3-for-1
    assert "rbStar" in c.lineup_after.starter_ids
    # The optimizer starts rbA (140) over rb2 (100), so this is the
    # "one starter replaced" shape: rbStar (17.6/wk) takes rbA's slot (8.2/wk): +9.4/wk
    assert c.weekly_gain == 9.4
    assert 0.9 <= p.value_ratio <= 1.35
    assert p.rationale_for_me[1].startswith("rbA starts today but rbStar refills that slot; rbB is not costing you starting production")
    assert c.freed_slot_note == "frees one roster spot with no new depth need"
    assert c.describe() == "rbA + rbB for rbStar (Team them): +9.4/wk"
    assert p.message


def test_small_gain_or_no_lineup_entry_is_not_a_consolidation():
    # A target who would not enter my lineup: projects below my RB2.
    assert _run(_me(), _them(star_proj=90, star_value=5000)) == []
    # rbA (140) is the displaced starter: exactly MIN_WEEKLY_IMPROVEMENT over him qualifies, a hair under does not.
    at_bar = _them(star_proj=140 + MIN_WEEKLY_IMPROVEMENT * 17, star_value=5000)
    assert _run(_me(), at_bar) and _run(_me(), at_bar)[0].weekly_gain == MIN_WEEKLY_IMPROVEMENT
    under = _them(star_proj=140 + (MIN_WEEKLY_IMPROVEMENT - 0.2) * 17, star_value=5000)
    assert _run(_me(), under) == []


def test_two_true_bench_pieces_say_so():
    # Neither piece starts: rbB (110) and wrB (90) sit behind rbA/rb2 and wr1.
    me = _me()
    them = _roster(2, [
        _p("qb2", "QB", 340, 8500, 96), _p("wr2", "WR", 250, 7500, 94),
        _p("rbStar", "RB", 300, 3700, 80), _p("rb_weak", "RB", 60, 500, 12), _p("wr_weak", "WR", 70, 600, 15),
    ], "them")
    out = _run(me, them)
    assert out and [e.player_id for e in out[0].proposal.give] == ["rbB", "wrB"]
    assert out[0].proposal.rationale_for_me[1].startswith("rbB and wrB are not costing you starting production")


def test_value_must_be_matched_with_the_engines_numbers():
    assert _run(_me(), _them(star_value=9000)) == []  # my best two pieces (5100) would be a lowball
    assert _run(_me(), _them(star_value=1000)) == []  # even my cheapest pair (2700) is far past the 2-for-1 premium


def test_fragility_is_flagged_when_the_depth_sent_is_the_depth_behind_a_starter():
    fa = _p("fa_rb", "RB", 150, 900, 30, starter=False)
    out = _run(_me(), _them(), fas=[fa])
    assert out and out[0].fragility_note is not None and "Fragile" in out[0].fragility_note
    assert any("Fragile" in c for c in out[0].proposal.caveats)


def test_at_most_two_per_team_with_distinct_pieces():
    me = _roster(1, [
        _p("qb1", "QB", 340, 8000, 95), _p("wr1", "WR", 250, 7000, 92),
        _p("rb1", "RB", 200, 4000, 70), _p("rb2", "RB", 100, 1500, 30),
        _p("rbA", "RB", 140, 2600, 52, starter=False), _p("rbB", "RB", 110, 2500, 50, starter=False),
        _p("rbC", "RB", 120, 2550, 51, starter=False), _p("rbD", "RB", 115, 2450, 49, starter=False),
    ], "me")
    them2 = _roster(3, [
        _p("qb3", "QB", 340, 8500, 96), _p("wr3", "WR", 250, 7500, 94),
        _p("rbStar2", "RB", 290, 5000, 79), _p("rb_weak2", "RB", 60, 500, 12),
    ], "other")
    rosters = {1: me, 2: _them(), 3: them2}
    out = find_consolidations(
        make_league_info(kind="dynasty"), me, rosters, status_result=_status(CONTENDER), status_of={2: MIDDLING, 3: CONTENDER},
        lineup=optimize_lineup(me), free_agents=[], current_week=1,
    )
    assert len(out) == MAX_PER_TEAM
    pieces = [e.player_id for c in out for e in c.proposal.give]
    assert len(pieces) == len(set(pieces))
    assert {c.proposal.target_username for c in out} == {"them", "other"}
