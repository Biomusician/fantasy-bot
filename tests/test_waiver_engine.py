from conftest import make_entry, make_league_info, make_roster, make_value

from sleeper_tool.waiver_engine import (
    BREAKOUT,
    MODERATE,
    MONITOR,
    MUST_ADD,
    SEASON_STARTER,
    SPECULATIVE,
    STASH,
    STREAMER,
    STRONG_ADD,
    TimeSensitiveNote,
    WaiverTarget,
    _find_drop_candidate,
    _horizon,
    _priority_tier,
    _roster_impact_note,
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


def test_horizon_season_starter_for_a_need_filling_rosterable_add():
    # A veteran, need-filling, rosterable-or-better add is a real hold,
    # not a this-week-only churn play -- regression for a bug where this
    # exact shape (fills_need + high percentile, not a young breakout)
    # fell through to STREAMER in both currencies, contradicting a
    # Must-Add/Strong-Add tier's own report row.
    v = make_value(trend="no change")
    assert _horizon(v, years_exp=8, currency="redraft", fills_need=True, pctl=60.0) == SEASON_STARTER


def test_horizon_streamer_as_fallback():
    v = make_value(trend="no change")
    assert _horizon(v, years_exp=8, currency="redraft", fills_need=False, pctl=25.0) == STREAMER


# -- _roster_impact_note -------------------------------------------------------


def test_roster_impact_note_names_the_weak_starter_it_would_beat():
    weak_starter = make_entry(player_id="s1", name="Joe Mixon", position="RB", is_starter=True,
        value=make_value(position="RB", dynasty_value_percentile=34.0))
    roster = make_roster(entries=[weak_starter])
    note = _roster_impact_note(roster, "RB", new_pctl=70.0, currency="dynasty")
    assert "Joe Mixon" in note
    assert "34" in note


def test_roster_impact_note_honest_depth_framing_when_add_would_not_beat_the_starter():
    # Previously silently returned None here, which made get_waiver_targets
    # fall back to the abstract "fills your Nth-worst need" framing even
    # though a concrete, honest answer ("this is depth, not a starter
    # upgrade") was computable -- matches trade_engine's equivalent
    # fallback instead of leaving the common non-upgrade case unstated.
    strong_starter = make_entry(player_id="s1", name="Star RB", position="RB", is_starter=True,
        value=make_value(position="RB", dynasty_value_percentile=90.0))
    roster = make_roster(entries=[strong_starter])
    note = _roster_impact_note(roster, "RB", new_pctl=40.0, currency="dynasty")
    assert "Star RB" in note
    assert "not an immediate upgrade" in note


def test_roster_impact_note_flags_an_empty_position():
    roster = make_roster(entries=[make_entry(player_id="s1", position="WR", is_starter=True, value=make_value(position="WR"))])
    note = _roster_impact_note(roster, "RB", new_pctl=50.0, currency="dynasty")
    assert "nobody currently starting" in note


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


def test_find_drop_candidate_still_suggests_same_position_bench_even_when_that_position_is_a_declared_need():
    # Regression: the need-avoidance guard previously excluded
    # target_position from consideration whenever it was ITSELF a
    # declared need -- exactly the common case (an add fills a need
    # position by definition), causing the tool to recommend cutting an
    # unrelated bench player instead of the obviously-weaker same-position
    # one the add is upgrading past.
    weak_same_pos = make_entry(player_id="rb-weak", position="RB", is_starter=False, value=make_value(position="RB", dynasty_value_percentile=15.0))
    unrelated_bench = make_entry(player_id="wr-fine", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=55.0))
    roster = make_roster(entries=[weak_same_pos, unrelated_bench])
    drop = _find_drop_candidate(roster, target_position="RB", my_needs=["TE", "RB"], currency="dynasty")
    assert drop.player_id == "rb-weak"


def test_find_drop_candidate_respects_exclude_ids_for_cross_target_dedup():
    # Regression: multiple simultaneous waiver "Add" rows previously all
    # independently recommended cutting the SAME single weakest bench
    # player, which isn't actionable if more than one is followed.
    weakest = make_entry(player_id="wr-weakest", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=10.0))
    second_weakest = make_entry(player_id="wr-second", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=25.0))
    roster = make_roster(entries=[weakest, second_weakest])
    first_drop = _find_drop_candidate(roster, target_position="WR", my_needs=[], currency="dynasty")
    assert first_drop.player_id == "wr-weakest"
    second_drop = _find_drop_candidate(
        roster, target_position="WR", my_needs=[], currency="dynasty", exclude_ids={first_drop.player_id}
    )
    assert second_drop.player_id == "wr-second"


def test_find_drop_candidate_ranks_unknown_valuation_after_a_known_low_one():
    # A player with NO valuation data (pctl=None) isn't necessarily the
    # single worst asset on the roster -- it's a data gap, and shouldn't
    # be preferred as the cut ahead of a player with a real, if low,
    # percentile.
    unranked = make_entry(player_id="wr-unranked", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=None))
    known_low = make_entry(player_id="wr-known-low", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=20.0))
    roster = make_roster(entries=[unranked, known_low])
    drop = _find_drop_candidate(roster, target_position="WR", my_needs=[], currency="dynasty")
    assert drop.player_id == "wr-known-low"


def test_find_drop_candidate_never_suggests_a_rising_player():
    rising = make_entry(player_id="hot", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=5.0, trend="rising"))
    roster = make_roster(entries=[rising])
    assert _find_drop_candidate(roster, target_position="WR", my_needs=[], currency="dynasty") is None


def test_find_drop_candidate_none_when_bench_is_empty():
    starter_only = make_entry(player_id="s1", position="WR", is_starter=True, value=make_value(position="WR"))
    roster = make_roster(entries=[starter_only])
    assert _find_drop_candidate(roster, target_position="WR", my_needs=[], currency="dynasty") is None


# -- get_time_sensitive_notes: structured severity -----------------------------


def test_time_sensitive_notes_flags_long_term_injury_not_in_an_ir_slot():
    # The only injury alert that matters: the player is functionally done
    # for a while (IR/PUP/NFI/Suspended) but is STILL sitting in an active
    # roster spot instead of the roster's actual IR/reserve slot.
    stranded_ir = make_entry(player_id="p1", name="Hurt Guy", injury_status="IR", is_starter=False, is_reserve=False, value=make_value())
    roster = make_roster(entries=[stranded_ir])
    notes = get_time_sensitive_notes(None, roster)
    assert len(notes) == 1
    assert notes[0].player_name == "Hurt Guy"
    assert notes[0].severity == "high"


def test_time_sensitive_notes_silent_once_the_player_is_actually_on_reserve():
    already_stashed = make_entry(player_id="p1", name="Stashed Guy", injury_status="IR", is_starter=False, is_reserve=True, value=make_value())
    roster = make_roster(entries=[already_stashed])
    assert get_time_sensitive_notes(None, roster) == []


def test_time_sensitive_notes_ignores_routine_weekly_game_status():
    # Questionable/Doubtful/Out are normal weekly designations that
    # resolve on their own -- not an "IR and stranded" situation, and
    # shouldn't generate an alert every single week.
    questionable = make_entry(player_id="p1", name="Q Guy", injury_status="Questionable", is_starter=True, value=make_value())
    doubtful = make_entry(player_id="p2", name="D Guy", injury_status="Doubtful", is_starter=True, value=make_value())
    out = make_entry(player_id="p3", name="Out Guy", injury_status="Out", is_starter=True, value=make_value())
    roster = make_roster(entries=[questionable, doubtful, out])
    assert get_time_sensitive_notes(None, roster) == []


def test_time_sensitive_notes_matches_sleepers_real_injury_and_status_values():
    # Regression: the original status set was {"IR", "PUP", "NFI",
    # "Suspended"} compared against injury_status -- but Sleeper's actual
    # cached data never uses "Suspended" (it uses "Sus") and never puts
    # "NFI" in injury_status at all (it's a separate `status` field value,
    # "Non Football Injury"). Both designations could never fire. Verified
    # against the real values queried from data/sleeper.sqlite3 this
    # session: injury_status in {COV, DNR, Doubtful, IR, NA, Out, PUP,
    # Questionable, Sus}, status in {Active, Inactive, Injured Reserve,
    # Non Football Injury, Physically Unable to Perform, Practice Squad}.
    suspended = make_entry(player_id="p1", name="Suspended Guy", injury_status="Sus", is_starter=False, is_reserve=False, value=make_value())
    nfi = make_entry(player_id="p2", name="NFI Guy", injury_status=None, status="Non Football Injury", is_starter=False, is_reserve=False, value=make_value())
    roster = make_roster(entries=[suspended, nfi])
    notes = get_time_sensitive_notes(None, roster)
    flagged_names = {n.player_name for n in notes}
    assert flagged_names == {"Suspended Guy", "NFI Guy"}


def test_time_sensitive_notes_silent_for_a_taxi_squad_stash():
    # Regression: a taxi-squad player, like a reserve-slotted one, isn't
    # occupying an active bench spot -- the alert's own advice ("move to
    # IR to free the slot") is nonsensical for a stash that never took
    # bench room to begin with, and firing it here reproduces the exact
    # noisy-alert complaint this round was meant to eliminate.
    taxi_stash = make_entry(player_id="p1", name="Taxi Guy", injury_status="IR", is_starter=False, is_reserve=False, is_taxi=True, value=make_value())
    roster = make_roster(entries=[taxi_stash])
    assert get_time_sensitive_notes(None, roster) == []


def test_time_sensitive_notes_bye_week_starter_is_medium_severity():
    starter_on_bye = make_entry(player_id="p1", name="Bye Guy", is_starter=True, value=make_value(bye_week=5))
    roster = make_roster(entries=[starter_on_bye])
    notes = get_time_sensitive_notes(None, roster, current_week=5)
    assert any(n.severity == "medium" and "bye" in n.note.lower() for n in notes)


# -- get_waiver_targets: end-to-end ---------------------------------------------


def test_get_waiver_targets_pairs_add_with_drop_and_tier():
    # WR must land in my top-2 worst positions for fills_need to fire
    # under the tightened (< 2) threshold -- give every position an entry
    # so none default to "missing = need_rank 0" ahead of WR.
    my_entries = [
        make_entry(player_id="my-qb", position="QB", is_starter=True, value=make_value(position="QB", dynasty_value_percentile=80.0)),
        make_entry(player_id="my-rb", position="RB", is_starter=True, value=make_value(position="RB", dynasty_value_percentile=75.0)),
        make_entry(player_id="my-te", position="TE", is_starter=True, value=make_value(position="TE", dynasty_value_percentile=70.0)),
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


def test_get_waiver_targets_gives_a_concrete_reason_even_for_a_non_need_position():
    # Regression: the roster-specific impact comparison was only computed
    # when fills_need was True -- every other row fell back to pure
    # trend-count + percentile boilerplate with zero connection to the
    # actual roster, the same generic-vs-concrete gap trade_engine's
    # _benefit_reason was rewritten to close for trade messages.
    my_entries = [
        make_entry(player_id="my-qb", position="QB", is_starter=True, value=make_value(position="QB", dynasty_value_percentile=90.0)),
        make_entry(player_id="my-rb", position="RB", is_starter=True, value=make_value(position="RB", dynasty_value_percentile=75.0)),
        make_entry(player_id="my-te", position="TE", is_starter=True, value=make_value(position="TE", dynasty_value_percentile=70.0)),
        make_entry(player_id="my-weak-wr", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=10.0)),
    ]
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=my_entries, league=league)

    players = {"new1": _player("new1", "Backup QB", "QB")}
    trending = [{"player_id": "new1", "count": 20}]
    storage = FakeStorage(players, trending)
    engine = FakeEngine({"Backup QB": make_value(name="Backup QB", position="QB", dynasty_value_percentile=50.0)})

    targets = get_waiver_targets(storage, engine, league, my_roster)
    assert len(targets) == 1
    assert targets[0].fills_need is False  # QB is my strongest position, not a top-2 need
    assert "depth behind your current starting QB" in targets[0].reason


def test_get_waiver_targets_assigns_drop_candidates_in_final_priority_order():
    # Regression: drop-candidate dedup was reserved while iterating
    # Sleeper's trending-feed (add-count) order, BEFORE the final
    # priority-tier sort -- so a lower-priority target processed earlier
    # in the feed could claim the one available same-position bench cut
    # ahead of a higher-priority target at the same position. Deliberately
    # list the weaker target FIRST in the trending feed to prove the fix
    # assigns drops by final display order, not feed order.
    my_entries = [
        make_entry(player_id="my-qb", position="QB", is_starter=True, value=make_value(position="QB", dynasty_value_percentile=80.0)),
        make_entry(player_id="my-rb", position="RB", is_starter=True, value=make_value(position="RB", dynasty_value_percentile=75.0)),
        make_entry(player_id="my-te", position="TE", is_starter=True, value=make_value(position="TE", dynasty_value_percentile=70.0)),
        make_entry(player_id="my-weak-wr", position="WR", is_starter=False, value=make_value(position="WR", dynasty_value_percentile=10.0)),
    ]
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=my_entries, league=league)

    players = {
        "low": _player("low", "Low Priority WR", "WR"),
        "high": _player("high", "High Priority WR", "WR"),
    }
    # Low-priority add listed FIRST in the trending feed on purpose.
    trending = [{"player_id": "low", "count": 40}, {"player_id": "high", "count": 30}]
    storage = FakeStorage(players, trending)
    engine = FakeEngine({
        "Low Priority WR": make_value(name="Low Priority WR", position="WR", dynasty_value_percentile=55.0),
        "High Priority WR": make_value(name="High Priority WR", position="WR", dynasty_value_percentile=85.0),
    })

    targets = get_waiver_targets(storage, engine, league, my_roster)
    by_name = {t.name: t for t in targets}
    assert by_name["High Priority WR"].priority_tier == MUST_ADD
    assert by_name["Low Priority WR"].priority_tier == STRONG_ADD
    assert by_name["High Priority WR"].drop_candidate is not None
    assert by_name["High Priority WR"].drop_candidate.player_id == "my-weak-wr"
    assert by_name["Low Priority WR"].drop_candidate is None  # the one available cut was already spent on the higher-priority row


def test_get_waiver_targets_labels_within_position_percentile_for_dynasty():
    # Regression: the trailing percentile in the reason string silently
    # switched between within-position (dynasty, when available) and
    # pool-wide, but both were labeled identically -- two rows showing the
    # same-looking number could mean different things with no visible cue.
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=[make_entry(player_id="only", value=make_value())], league=league)
    players = {"new1": _player("new1", "Positional Guy", "WR")}
    trending = [{"player_id": "new1", "count": 10}]
    storage = FakeStorage(players, trending)
    engine = FakeEngine({"Positional Guy": make_value(name="Positional Guy", position="WR", dynasty_positional_percentile=65.0)})
    targets = get_waiver_targets(storage, engine, league, my_roster)
    assert "within-position" in targets[0].reason


def test_get_waiver_targets_returns_empty_for_undrafted_roster():
    league = make_league_info(kind="dynasty")
    my_roster = make_roster(entries=[], league=league)
    assert get_waiver_targets(FakeStorage({}, []), FakeEngine({}), league, my_roster) == []
