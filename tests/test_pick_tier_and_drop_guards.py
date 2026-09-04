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


def test_a_kicker_a_defense_and_a_reserve_player_are_never_give_pieces():
    """A K/DEF give manufactures an empty required slot the optimizer then
    prices as a Major Lineup Cost; an IR/PUP player is not a package piece."""
    from conftest import make_entry, make_roster, make_value

    from sleeper_tool.roster_assets import tradeable_pool

    def entry(pid, pos, **kw):
        return make_entry(player_id=pid, name=pid, position=pos,
                          value=make_value(position=pos, dynasty_value=3000, dynasty_value_percentile=60.0), **kw)

    roster = make_roster(entries=[
        entry("wr1", "WR"), entry("wr2", "WR"), entry("wr3", "WR"),
        entry("k1", "K"), entry("def1", "DEF"),
        entry("hurt", "RB", injury_status="PUP"), entry("stashed", "RB", is_reserve=True),
    ])
    ids = {e.player_id for e in tradeable_pool(roster)}
    assert "k1" not in ids and "def1" not in ids
    assert "hurt" not in ids and "stashed" not in ids
    assert ids & {"wr1", "wr2", "wr3"}


def test_a_first_year_players_dynasty_premium_is_not_a_buy_low_dip():
    from conftest import make_entry, make_roster, make_value

    from sleeper_tool.trade_engine import identify_buy_low

    def rookie(pid, exp):
        return make_entry(
            player_id=pid, name=pid, position="WR", years_exp=exp, age=23,
            value=make_value(position="WR", trend="down", dynasty_value=4000,
                             dynasty_value_percentile=70.0, dynasty_positional_percentile=70.0,
                             redraft_ecr_percentile=45.0),
        )

    roster = make_roster(entries=[
        rookie("rookie", 0), rookie("sophomore", 1), rookie("veteran", 4),
        *[make_entry(player_id=f"f{i}", name=f"f{i}", position="RB",
                     value=make_value(position="RB", dynasty_value=9000, dynasty_value_percentile=95.0,
                                      dynasty_positional_percentile=95.0)) for i in range(3)],
    ])
    names = {e.player_id for e in identify_buy_low(roster)}
    assert "veteran" in names
    assert "rookie" not in names and "sophomore" not in names


def test_redraft_buy_low_must_beat_this_leagues_wire():
    from conftest import make_entry, make_league_info, make_roster, make_value

    from sleeper_tool.trade_engine import identify_buy_low

    def wr(pid, proj):
        return make_entry(player_id=pid, name=pid, position="WR", years_exp=5,
                          value=make_value(position="WR", trend="down", proj_points=proj * 17,
                                           redraft_ecr_percentile=70.0))

    # Two cornerstones so the buy-low pair isn't protected as untouchable.
    stars = [
        make_entry(player_id=f"star{i}", name=f"star{i}", position="RB", years_exp=5,
                   value=make_value(position="RB", proj_points=20 * 17, redraft_ecr_percentile=99.0))
        for i in range(2)
    ]
    roster = make_roster(entries=[*stars, wr("better", 12.0), wr("worse", 4.0)], league=make_league_info(kind="redraft"))
    floor = {"WR": 8.0}
    names = {e.player_id for e in identify_buy_low(roster, waiver_floor=floor, current_week=1)}
    assert names == {"better"}
    # Without a market to compare against, the gate never fires.
    assert {e.player_id for e in identify_buy_low(roster)} == {"better", "worse"}


def test_a_deep_position_is_not_a_need_however_weak_its_best_player_ranks():
    """Four rosterable QBs in a 1QB league were "need #1" because ranking
    positions against each other has to put somebody last."""
    from conftest import make_entry, make_format, make_roster, make_value

    from sleeper_tool.trade_engine import identify_needs

    def player(pid, pos, pctl):
        return make_entry(player_id=pid, name=pid, position=pos,
                          value=make_value(position=pos, dynasty_value=3000 + pctl,
                                           dynasty_value_percentile=pctl, dynasty_positional_percentile=pctl))

    from dataclasses import replace
    fmt = replace(make_format(), starter_slots={"QB": 1.0, "RB": 2.0, "WR": 2.0, "TE": 1.0})
    roster = make_roster(fmt=fmt, entries=[
        *[player(f"qb{i}", "QB", 70.0 + i) for i in range(4)],   # slots 1 + spare + spare
        player("rb1", "RB", 95.0), player("rb2", "RB", 94.0),
        player("wr1", "WR", 99.0), player("wr2", "WR", 98.0),
        player("te1", "TE", 96.0),
    ])
    needs = identify_needs(roster)
    assert needs[0] != "QB"          # depth-satisfied, so never the top need
    assert needs[-1] == "QB"


def test_a_projection_found_free_agent_is_a_candidate_without_a_trend_count():
    """The best free agent by projection is often not on Sleeper's
    platform-wide trending list; he still has to be a row."""
    from conftest import make_entry, make_value
    from fake_storage import make_engine, make_player_pool, make_storage, make_synthetic_league

    from sleeper_tool.roster_analysis import build_all_valued_rosters
    from sleeper_tool.waiver_engine import get_waiver_targets

    synth = make_synthetic_league()
    storage = make_storage(synth)
    engine = make_engine(synth.players)
    rosters = build_all_valued_rosters(storage, engine, synth.info)
    mine = rosters[synth.my_roster["roster_id"]]

    trending_ids = {row["player_id"] for row in storage.get_trending("add")}
    free_agent_id = next(
        pid for pid in synth.players
        if pid not in trending_ids and not any(pid in (r.get("players") or []) for r in synth.rosters)
    )
    extra = make_entry(player_id=free_agent_id, name="Wire Find", position="RB", value=make_value(position="RB"))

    without = {t.player_id for t in get_waiver_targets(storage, engine, synth.info, mine, top_n=50)}
    rows = get_waiver_targets(storage, engine, synth.info, mine, top_n=50, extra_candidates=[extra])
    assert free_agent_id not in without
    row = next(t for t in rows if t.player_id == free_agent_id)
    assert row.trend_count == 0 and "found by projection" in row.reason


def test_an_abundant_market_add_that_beats_no_starter_is_capped_at_moderate():
    from sleeper_tool.waiver_engine import MODERATE, MUST_ADD, STRONG_ADD, _priority_tier

    # Same inputs, three markets: the tier only changes because the wire does.
    assert _priority_tier(True, 85.0, 0, upgrades_starter=None) == MUST_ADD
    assert _priority_tier(True, 85.0, 0, upgrades_starter=None, scarcity="Abundant") == MODERATE
    assert _priority_tier(True, 85.0, 0, upgrades_starter=None, scarcity="Scarce") == MUST_ADD
    # Beating a starter is exactly the case an Abundant market does not cap.
    assert _priority_tier(True, 85.0, 0, upgrades_starter=True, scarcity="Abundant") == MUST_ADD
    assert _priority_tier(False, 82.0, 0, upgrades_starter=False, scarcity="Abundant") == MODERATE
    assert _priority_tier(False, 82.0, 0, upgrades_starter=False) == STRONG_ADD


def test_a_waiver_drop_is_never_a_better_player_than_the_add_even_across_positions():
    from conftest import make_entry, make_value
    from fake_storage import make_engine, make_storage, make_synthetic_league

    from sleeper_tool.asset_value import value_currency
    from sleeper_tool.roster_analysis import build_all_valued_rosters
    from sleeper_tool.waiver_engine import _display_percentile, get_waiver_targets

    synth = make_synthetic_league()
    storage = make_storage(synth)
    engine = make_engine(synth.players)
    rosters = build_all_valued_rosters(storage, engine, synth.info)
    mine = rosters[synth.my_roster["roster_id"]]
    currency = value_currency(mine)
    for t in get_waiver_targets(storage, engine, synth.info, mine, top_n=50):
        if t.drop_candidate is None:
            continue
        drop = _display_percentile(t.drop_candidate.value, currency)
        add = _display_percentile(t.value, currency)
        if drop is not None and add is not None:
            assert drop <= add, f"{t.name} ({add}) paired with dropping {t.drop_candidate.name} ({drop})"


def test_a_trade_that_downgrades_my_team_status_is_a_conflict():
    from sleeper_tool.recommendation_conflicts import _status_downgrade
    from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD

    class Side:
        def __init__(self, status, displayed=None):
            self.status, self.displayed_status = status, displayed

    class Impact:
        def __init__(self, before, after, pure_add=False):
            self.before, self.after, self.pure_add = before, after, pure_add

    assert _status_downgrade(Impact(Side(CONTENDER), Side(MIDDLING))) == "drops this team from contender to middling"
    assert _status_downgrade(Impact(Side(MIDDLING), Side(REBUILD))) is not None
    # Improvements, no-ops and pure adds are not conflicts.
    assert _status_downgrade(Impact(Side(MIDDLING), Side(CONTENDER))) is None
    assert _status_downgrade(Impact(Side(CONTENDER), Side(CONTENDER))) is None
    assert _status_downgrade(Impact(Side(CONTENDER), Side(MIDDLING), pure_add=True)) is None
    assert _status_downgrade(Impact(Side(None), Side(MIDDLING))) is None
    # The headline (set-lineup) status is what the reader saw, so it wins.
    assert _status_downgrade(Impact(Side(MIDDLING, displayed=CONTENDER), Side(MIDDLING))) is not None
