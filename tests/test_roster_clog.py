import dataclasses

from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.roster_clog import DYNASTY_CLOG_RANK_CUTOFF, identify_roster_clogs
from sleeper_tool.waiver_engine import _find_drop_candidate

SLOTS = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN", "BN")


def _p(pid, pos, proj, *, dyn_rank=50, ecr=50, years_exp=3, sources=None, **kw):
    kw.setdefault("is_starter", False)
    entry = make_entry(
        player_id=pid, name=pid, position=pos,
        value=make_value(
            name=pid, position=pos, proj_points=proj, dynasty_rank=dyn_rank, dynasty_ecr_rank=dyn_rank,
            redraft_ecr_rank=ecr, sources=sources,
        ),
        **kw,
    )
    return dataclasses.replace(entry, years_exp=years_exp)


def _roster(entries, kind="dynasty"):
    return make_roster(entries=entries, fmt=make_format(roster_positions=SLOTS), league=make_league_info(kind=kind))


def _base_entries():
    # Full lineup so every slot is filled by someone better than the bench.
    return [
        _p("qb1", "QB", 300), _p("rb1", "RB", 250), _p("rb2", "RB", 220), _p("wr1", "WR", 240),
        _p("wr2", "WR", 230), _p("te1", "TE", 150), _p("wr3", "WR", 200),  # wr3 takes FLEX
        _p("rb3", "RB", 120),  # primary RB backup
    ]


def test_flags_a_deep_ranked_bench_player_with_no_path_to_the_lineup():
    clog = _p("rb_dead", "RB", 40, dyn_rank=260)
    clogs = identify_roster_clogs(_roster(_base_entries() + [clog]))
    assert [c.entry.player_id for c in clogs] == ["rb_dead"]
    assert any("top 150" in r for r in clogs[0].reasons)
    assert any("primary backup (rb3)" in r for r in clogs[0].reasons)


def test_a_player_inside_the_rank_cutoff_is_never_a_clog():
    kept = _p("rb_ok", "RB", 40, dyn_rank=DYNASTY_CLOG_RANK_CUTOFF)
    assert identify_roster_clogs(_roster(_base_entries() + [kept])) == []


def test_the_primary_backup_is_depth_not_a_clog():
    # rb3 is the only backup RB: nobody behind the starters projects above him.
    entries = [e for e in _base_entries()]
    entries = [dataclasses.replace(e, value=dataclasses.replace(e.value, dynasty_rank=300, dynasty_ecr_rank=300)) if e.player_id == "rb3" else e for e in entries]
    assert identify_roster_clogs(_roster(entries)) == []


def test_dynasty_developmental_players_are_exempt_but_redraft_uses_its_own_cutoff():
    rookie = _p("rookie", "RB", 40, dyn_rank=260, ecr=200, years_exp=0)
    assert identify_roster_clogs(_roster(_base_entries() + [rookie], kind="dynasty")) == []
    # A 23-year-old second-year WR is contingent value, not dead weight...
    soph = dataclasses.replace(_p("soph", "WR", 40, dyn_rank=200, years_exp=1), age=23.0)
    assert identify_roster_clogs(_roster(_base_entries() + [soph], kind="dynasty")) == []
    # ...but a 28-year-old with two years' experience (late bloomer) isn't developmental.
    old_soph = dataclasses.replace(_p("old_soph", "WR", 40, dyn_rank=200, years_exp=1), age=28.0)
    wr_backup = _p("wr4", "WR", 60)  # a real primary backup ahead of him
    clogs = identify_roster_clogs(_roster(_base_entries() + [wr_backup, old_soph], kind="dynasty"))
    assert [c.entry.player_id for c in clogs] == ["old_soph"]
    # Redraft currency: rank test is the rest-of-season ECR (120), rookie status irrelevant.
    clogs = identify_roster_clogs(_roster(_base_entries() + [rookie], kind="redraft"))
    assert [c.entry.player_id for c in clogs] == ["rookie"]
    assert any("rest-of-season rank" in r for r in clogs[0].reasons)


def test_ir_taxi_trending_and_excluded_players_are_skipped():
    entries = _base_entries() + [
        _p("ir", "RB", 40, dyn_rank=260, is_reserve=True),
        _p("taxi", "RB", 40, dyn_rank=260, is_taxi=True),
        _p("ir_status", "RB", 40, dyn_rank=260, injury_status="IR"),
        _p("hot", "RB", 40, dyn_rank=260),
        _p("traded", "RB", 40, dyn_rank=260),
    ]
    clogs = identify_roster_clogs(_roster(entries), trending_add_ids={"hot"}, exclude_ids={"traded"})
    assert clogs == []


def test_unranked_unprojected_or_single_source_players_are_not_flagged_off_a_data_gap():
    entries = _base_entries() + [
        _p("unranked", "RB", 40, dyn_rank=None),
        _p("single", "RB", 40, dyn_rank=260, sources=["ktc"]),
        _p("unprojected", "RB", None, dyn_rank=260),  # 0.0 would "project below everyone"
    ]
    assert identify_roster_clogs(_roster(entries)) == []


def test_caps_at_three_and_orders_deepest_ranked_first():
    entries = _base_entries() + [_p(f"dead{i}", "RB", 40 - i, dyn_rank=200 + i * 10) for i in range(5)]
    clogs = identify_roster_clogs(_roster(entries))
    assert [c.entry.player_id for c in clogs] == ["dead4", "dead3", "dead2"]


def test_waiver_drop_candidate_prefers_a_clog_over_a_same_position_backup():
    # Adding a WR: the same-position bench WR is the primary backup; the
    # RB clog is the better cut and should be chosen even across positions.
    my_roster = _roster([
        _p("wr1", "WR", 240, is_starter=True),
        _p("wr_backup", "WR", 100),
        _p("rb_clog", "RB", 20, dyn_rank=300),
    ])
    pick = _find_drop_candidate(my_roster, "WR", [], "dynasty", preferred_ids={"rb_clog"})
    assert pick.player_id == "rb_clog"
    pick = _find_drop_candidate(my_roster, "WR", [], "dynasty")
    assert pick.player_id == "wr_backup"
