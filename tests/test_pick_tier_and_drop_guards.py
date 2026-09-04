"""Night-build regressions: pick tiers use a league-relative rank, superflex
demand is QB demand, an optimizer starter is never a drop, and a pre-draft
league gets neither bye alerts nor a drop list."""
from __future__ import annotations

import pytest

from fake_storage import isolate_report_data, make_engine, make_storage, make_synthetic_league

from sleeper_tool.report_data import build_weekly_report_data
from sleeper_tool.roster_analysis import build_all_valued_rosters
from sleeper_tool.team_status import get_valued_picks_by_roster
from sleeper_tool.valuation import derive_league_format


def test_pick_tiers_span_early_to_late_across_a_league():
    # Raw starter-percentile averages sit at 65-95 for every real roster, which
    # read every pick as "Late"; tiers must come from the league-relative rank.
    synth = make_synthetic_league(teams=6)
    storage = make_storage(synth)
    engine = make_engine(synth.players)
    rosters = build_all_valued_rosters(storage, engine, synth.info)
    picks = get_valued_picks_by_roster(rosters, "dynasty", storage, engine)
    assert picks
    own_first = {}
    for rid, owned in picks.items():
        for p in owned:
            if p.round == 1 and p.original_roster_id == rid:
                own_first.setdefault(rid, p.tier)
    tiers = set(own_first.values())
    assert "Early" in tiers and "Late" in tiers, own_first


def test_superflex_slot_is_qb_demand():
    fmt = derive_league_format({"scoring_settings": {}, "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "BN"]})
    assert fmt.starter_slots["QB"] == pytest.approx(2.0)
    assert fmt.starter_slots["TE"] == pytest.approx(1 + 1 / 3)


@pytest.fixture(scope="module")
def report():
    patcher = pytest.MonkeyPatch()
    try:
        isolate_report_data(patcher)
        normal = make_synthetic_league()
        pre_draft = make_synthetic_league(name="Keepers", league_id="9000000000000000002", kind="keeper", status="pre_draft")
        storage = make_storage(normal, pre_draft)
        engine = make_engine(normal.players)
        yield build_weekly_report_data(storage, engine, [normal.info, pre_draft.info], with_nfl_schedule=False)
    finally:
        patcher.undo()


def test_an_optimizer_starter_is_never_a_drop_candidate(report):
    for ld in report.leagues:
        if ld.lineup is None:
            continue
        starters = set(ld.lineup.starter_ids)
        assert not [d.entry.name for d in ld.drop_candidates if d.entry.player_id in starters]


def test_pre_draft_league_has_no_bye_alert_and_no_drop_list(report):
    ld = next(l for l in report.leagues if l.league.name == "Keepers")
    assert ld.error is None
    assert ld.bye_collision is None
    assert ld.drop_candidates == []
    assert not [n for n in ld.time_sensitive if "bye hole" in n.player_name.lower()]
