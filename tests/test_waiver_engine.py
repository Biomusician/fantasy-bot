from conftest import make_entry, make_league_info, make_roster, make_value

from sleeper_tool.waiver_engine import (
    BREAKOUT,
    MODERATE,
    MONITOR,
    MUST_ADD,
    SPECULATIVE,
    STASH,
    STREAMER,
    STRONG_ADD,
    TimeSensitiveNote,
    WaiverTarget,
    _find_drop_candidate,
    _horizon,
    _priority_tier,
    _suggested_faab_pct,
    get_time_sensitive_notes,
    get_waiver_targets,
)


class FakeStorage:
    def __init__(self, players: dict, trending: list[dict], rostered: set | None = None):
        self._players = players
        self._trending = trending
        self._rostered = rostered or set()

    def get_all_players(self):
        return self._players

    def get_rosters(self, league_id):
        return [{"players": list(self._rostered)}] if self._rostered else []

    def get_trending(self, trend_type):
        return self._trending


class FakeEngine:
    def __init__(self, values_by_name: dict):
        self._values = values_by_name

    def value_player(self, name, fmt, position=None):
        return self._values.get(name, make_value(name=name, position=position))


def _player(pid, name, position, team="KC", years_exp=5):
    return {"player_id": pid, "full_name": name, "first_name": name, "last_name": "", "position": position, "team": team, "years_exp": years_exp}


# -- _priority_tier -----------------------------------------------------------


def test_priority_tier_must_add_when_fills_need_and_high_percentile():
    assert _priority_tier(fills_need=True, pctl=80.0, trend_rank=0) == MUST_ADD


def test_priority_tier_strong_add_without_need_but_very_high_percentile():
    assert _priority_tier(fills_need=False, pctl=85.0, trend_rank=0) == STRONG_ADD


def test_priority_tier_moderate_when_rosterable_but_not_a_need():
    assert _priority_tier(fills_need=False, pctl=50.0, trend_rank=20) == MODERATE


def test_priority_tier_speculative_for_top_trending_but_low_percentile():
    assert _priority_tier(fills_need=False, pctl=10.0, trend_rank=3) == SPECULATIVE


def test_priority_tier_monitor_for_low_percentile_and_low_buzz():
    assert _priority_tier(fills_need=False, pctl=10.0, trend_rank=40) == MONITOR


# -- _horizon -------------------------------------------------------------


def test_horizon_breakout_for_young_rising_player():
    v = make_value(trend="rising")
    assert _horizon(v, years_exp=1, currency="dynasty", fills_need=False, pctl=50.0) == BREAKOUT


def test_horizon_stash_for_dynasty_relevant_non_need_depth():
    v = make_value(trend="no change")
    assert _horizon(v, years_exp=5, currency="dynasty", fills_need=False, pctl=45.0) == STASH


def test_horizon_streamer_as_fallback():
    v = make_value(trend="no change")
    assert _horizon(v, years_exp=8, currency="redraft", fills_need=True, pctl=60.0) == STREAMER


# -- _suggested_faab_pct -------------------------------------------------------


def test_suggested_faab_pct_none_when_league_has_no_faab_budget():
    assert _suggested_faab_pct(MUST_ADD, waiver_budget=None, waiver_budget_used=0) is None


def test_suggested_faab_pct_scales_down_as_budget_is_spent():
    fresh = _suggested_faab_pct(MUST_ADD, waiver_budget=100, waiver_budget_used=0)
    half_spent = _suggested_faab_pct(MUST_ADD, waiver_budget=100, waiver_budget_used=60)
    assert fresh > half_spent


def test_suggested_faab_pct_zero_when_budget_exhausted():
    assert _suggested_faab_pct(MUST_ADD, waiver_budget=100, waiver_budget_used=100) == 0


# -- _find_drop_candidate -------------------------------------------------------


def test_find_drop_candidate_prefers_same_position_non_need_bench():
    weak_wr = make_entry(player_id="wr-weak", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=15.0))
    weak_rb = make_entry(player_id="rb-weak", position="RB", is_starter=False, value=make_value(position="RB", dynasty_value_percentile=25.0))
    roster = make_roster(entries=[weak_wr, weak_rb])
    drop = _find_drop_candidate(roster, target_position="WR", my_needs=["QB"], currency="dynasty")
    assert drop.player_id == "wr-weak"


def test_find_drop_candidate_avoids_a_need_position_when_alternative_exists():
    need_position_bench = make_entry(player_id="te-need", position="TE", is_starter=False, value=make_value(position="TE", dynasty_value_percentile=10.0))
    non_need_bench = make_entry(player_id="wr-surplus", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=40.0))
    roster = make_roster(entries=[need_position_bench, non_need_bench])
    drop = _find_drop_candidate(roster, target_position="RB", my_needs=["TE"], currency="dynasty")
    assert drop.player_id == "wr-surplus"  # avoids cutting the TE (a declared need) even though it's weaker in raw value


def test_find_drop_candidate_never_suggests_a_rising_player():
    rising = make_entry(player_id="hot", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=5.0, trend="rising"))
    roster = make_roster(entries=[rising])
    assert _find_drop_candidate(roster, target_position="WR", my_needs=[], currency="dynasty") is None


def test_find_drop_candidate_none_when_bench_is_empty():
    starter_only = make_entry(player_id="s1", position="WR", is_starter=True, value=make_value(position="WR"))
    roster = make_roster(entries=[starter_only])
    assert _find_drop_candidate(roster, target_position="WR", my_needs=[], currency="dynasty") is None


# -- get_time_sensitive_notes: structured severity -----------------------------


def test_time_sensitive_notes_severity_reflects_injury_status_not_note_text():
    out_player = make_entry(player_id="p1", name="Out Guy", injury_status="Out", is_starter=True, value=make_value())
    questionable_player = make_entry(player_id="p2", name="Q Guy", injury_status="Questionable", is_starter=True, value=make_value())
    roster = make_roster(entries=[out_player, questionable_player])
    notes = get_time_sensitive_notes(None, roster)
    by_name = {n.player_name: n for n in notes}
    assert by_name["Out Guy"].severity == "high"
    assert by_name["Q Guy"].severity == "medium"


def test_time_sensitive_notes_bye_week_starter_is_medium_severity():
    starter_on_bye = make_entry(player_id="p1", name="Bye Guy", is_starter=True, value=make_value(bye_week=5))
    roster = make_roster(entries=[starter_on_bye])
    notes = get_time_sensitive_notes(None, roster, current_week=5)
    assert any(n.severity == "medium" and "bye" in n.note.lower() for n in notes)


# -- get_waiver_targets: end-to-end ---------------------------------------------


def test_get_waiver_targets_pairs_add_with_drop_and_tier():
    my_entries = [
        make_entry(player_id="my-qb", position="QB", is_starter=True, value=make_value(position="QB", dynasty_value_percentile=80.0)),
        make_entry(player_id="my-weak-wr", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=10.0)),
    ]
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=my_entries, league=league)

    players = {"new1": _player("new1", "Trending Guy", "WR")}
    trending = [{"player_id": "new1", "count": 50}]
    storage = FakeStorage(players, trending)
    engine = FakeEngine({"Trending Guy": make_value(name="Trending Guy", position="WR", dynasty_value_percentile=75.0, trend="rising")})

    targets = get_waiver_targets(storage, engine, league, my_roster, current_week=6)
    assert len(targets) == 1
    t = targets[0]
    assert t.name == "Trending Guy"
    assert t.drop_candidate is not None
    assert t.drop_candidate.player_id == "my-weak-wr"
    assert t.priority_tier in (MUST_ADD, STRONG_ADD)


def test_get_waiver_targets_excludes_already_rostered_players():
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=[make_entry(player_id="only", value=make_value())], league=league)
    players = {"rostered1": _player("rostered1", "Already Owned", "WR")}
    trending = [{"player_id": "rostered1", "count": 10}]
    storage = FakeStorage(players, trending, rostered={"rostered1"})
    engine = FakeEngine({})
    targets = get_waiver_targets(storage, engine, league, my_roster)
    assert targets == []


def test_get_waiver_targets_flags_bye_week_players():
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=[make_entry(player_id="only", value=make_value())], league=league)
    players = {"new1": _player("new1", "Bye Week Guy", "WR")}
    trending = [{"player_id": "new1", "count": 10}]
    storage = FakeStorage(players, trending)
    engine = FakeEngine({"Bye Week Guy": make_value(name="Bye Week Guy", position="WR", bye_week=7)})
    targets = get_waiver_targets(storage, engine, league, my_roster, current_week=7)
    assert "bye" in targets[0].reason.lower()


def test_get_waiver_targets_returns_empty_for_undrafted_roster():
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=[], league=league)
    assert get_waiver_targets(FakeStorage({}, []), FakeEngine({}), league, my_roster) == []
