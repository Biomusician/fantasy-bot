import datetime as dt

from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.market_velocity import DIRECTIONAL_MIN_MOVE, RISING, STABLE, Velocity
from sleeper_tool.source_disagreement import DYNASTY_PAIR, MARKET_ABOVE_PROJECTION, SOURCE_DISAGREEMENT as SD_LABEL, SourceView
from sleeper_tool.stash_board import PRIORITY_STASH, WATCH, StashCandidate
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.waiver_engine import MODERATE, MUST_ADD, STRONG_ADD, WaiverTarget
from sleeper_tool.watchlist import (
    INJURED_MAY_RETURN,
    NEW_TRIGGER,
    RESOLVE_AFTER_MISSES,
    RESOLVED,
    ROLE_RISING,
    ROLE_RISING_SHORT,
    ROLE_SURGING,
    SOURCE_DISAGREEMENT,
    STASH_BLOCKED,
    STILL_WATCHING,
    TRADE_PRICE_HIGH,
    VELOCITY_NEAR,
    VELOCITY_NEAR_RATIO,
    WAIVER_NO_DROP,
    WATCH_MAX_AGE_DAYS,
    WATCHLIST_SCHEMA,
    WatchItem,
    Watchlist,
    candidates,
    item_id,
    load_watchlist,
    metrics,
    promotions,
    render_lines,
    save_watchlist,
    update,
    watchlist_path,
)

DAY1 = dt.datetime(2026, 9, 2, 12, 0)
DAY2 = DAY1 + dt.timedelta(days=1)
DAY3 = DAY1 + dt.timedelta(days=2)


class FakeReport:
    def __init__(self, *, generated_at=DAY1, current_week=5):
        self.generated_at = generated_at
        self.current_week = current_week


class FakeLD:
    """A LeagueReportData with only the fields the watchlist reads."""

    def __init__(self, **kwargs):
        self.league = kwargs.pop("league", make_league_info(league_id="L1", name="Test League"))
        self.error = kwargs.pop("error", None)
        self.drafted = kwargs.pop("drafted", True)
        self.roster = kwargs.pop("roster", make_roster())
        self.waiver_targets = kwargs.pop("waiver_targets", [])
        self.proposals = kwargs.pop("proposals", [])
        self.velocity = kwargs.pop("velocity", {})
        self.source_views = kwargs.pop("source_views", {})
        self.stash = kwargs.pop("stash", [])
        self.conflicts = kwargs.pop("conflicts", [])
        self.insurance = kwargs.pop("insurance", [])
        self.drop_candidates = kwargs.pop("drop_candidates", [])
        self.defensive_add = kwargs.pop("defensive_add", None)
        self.replacement = kwargs.pop("replacement", None)
        self.role_trends = kwargs.pop("role_trends", {})
        assert not kwargs, kwargs


def make_target(*, player_id="w1", name="Waiver Guy", tier=MODERATE, drop=None, position="RB") -> WaiverTarget:
    return WaiverTarget(
        player_id=player_id, name=name, position=position, team="KC", trend_count=100,
        value=make_value(name=name, position=position), fills_need=True, need_rank=0,
        reason="trending", priority_tier=tier, drop_candidate=drop,
    )


def make_proposal(*, receive, my_value=120.0, their_value=100.0) -> TradeProposal:
    return TradeProposal(
        league_name="Test League", currency="dynasty", target_username="rival", target_team_name="Rival Team",
        give=[make_entry(player_id="g1", name="Give Guy")], receive=receive,
        my_value_total=my_value, their_value_total=their_value,
        rationale_for_me=[], rationale_for_them=[], caveats=[],
    )


def full_roster(entries):
    """A roster with exactly as many active slots as players — no open spot."""
    return make_roster(entries=entries, fmt=make_format(roster_positions=tuple("BN" for _ in entries)))


def open_roster(entries):
    return make_roster(entries=entries, fmt=make_format(roster_positions=tuple("BN" for _ in range(len(entries) + 1))))


# -- persistence ----------------------------------------------------------------


def _item(**kwargs) -> WatchItem:
    base = dict(
        item_id="abc123", league_id="L1", league_name="Test League", kind=ROLE_RISING_SHORT,
        player_id="p1", player_name="Rising Guy", reason="Role Rising but not on the board",
        first_seen="2026-09-01", last_seen="2026-09-01", snapshot={"role_label": ROLE_RISING},
    )
    base.update(kwargs)
    return WatchItem(**base)


def test_round_trip_preserves_every_field(tmp_path):
    watchlist = Watchlist(items={"abc123": _item(triggered_on={"role_improved": "2026-09-01"}, misses=1)}, generated_at="x")
    save_watchlist(watchlist, tmp_path)
    loaded = load_watchlist(tmp_path)
    assert loaded.items["abc123"] == watchlist.items["abc123"]
    assert loaded.generated_at == "x"


def test_missing_file_is_an_empty_watchlist(tmp_path):
    assert load_watchlist(tmp_path).items == {}


def test_corrupt_file_is_an_empty_watchlist_not_a_crash(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    watchlist_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert load_watchlist(tmp_path).items == {}


def test_old_schema_file_is_ignored(tmp_path):
    save_watchlist(Watchlist(items={"abc123": _item()}), tmp_path)
    path = watchlist_path(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace(f'"schema": {WATCHLIST_SCHEMA}', '"schema": 0'), encoding="utf-8")
    assert load_watchlist(tmp_path).items == {}


def test_retention_prunes_items_unseen_past_the_max_age():
    stale = (DAY1.date() - dt.timedelta(days=WATCH_MAX_AGE_DAYS + 1)).isoformat()
    fresh = (DAY1.date() - dt.timedelta(days=2)).isoformat()
    existing = Watchlist(items={
        "old": _item(item_id="old", last_seen=stale),
        "new": _item(item_id="new", last_seen=fresh),
    })
    # No league data for L1 this run: nothing is evaluated, only pruned.
    result = update(existing, [], now=DAY1, ld_by_league={})
    assert set(result.items) == {"new"}


def test_the_retention_cutoff_is_twenty_eight_days_at_absolute_dates():
    """Pinned by value and against literal dates rather than dates derived
    from WATCH_MAX_AGE_DAYS, so a change to the constant has to move these.

    The code's comparison is `last_seen < today - WATCH_MAX_AGE_DAYS`, so an
    item last seen exactly 28 days ago is KEPT and 29 days ago is dropped —
    one day looser than the module docstring's "unseen for
    WATCH_MAX_AGE_DAYS is dropped" reads.
    """
    assert WATCH_MAX_AGE_DAYS == 28
    # DAY1 is 2026-09-02: 27d ago is 2026-08-06, 28d is 2026-08-05, 29d is 2026-08-04.
    existing = Watchlist(items={
        "d27": _item(item_id="d27", last_seen="2026-08-06"),
        "d28": _item(item_id="d28", last_seen="2026-08-05"),
        "d29": _item(item_id="d29", last_seen="2026-08-04"),
    })
    result = update(existing, [], now=DAY1, ld_by_league={})
    assert set(result.items) == {"d27", "d28"}


# -- candidate kinds ------------------------------------------------------------


def test_role_rising_but_absent_from_the_waiver_board():
    ld = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine")]))
    found = candidates(ld, FakeReport())
    assert [c.kind for c in found] == [ROLE_RISING_SHORT]
    assert "not on the waiver board" in found[0].reason


def test_role_rising_with_only_a_moderate_waiver_row_is_still_a_candidate():
    ld = FakeLD(role_trends={"w1": ROLE_RISING}, waiver_targets=[make_target(tier=MODERATE, drop=make_entry(player_id="d1"))])
    kinds = {c.kind for c in candidates(ld, FakeReport())}
    assert ROLE_RISING_SHORT in kinds


def test_role_rising_that_is_already_a_strong_add_is_not_watched():
    ld = FakeLD(role_trends={"w1": ROLE_RISING}, waiver_targets=[make_target(tier=STRONG_ADD, drop=make_entry(player_id="d1"))])
    assert not [c for c in candidates(ld, FakeReport()) if c.kind == ROLE_RISING_SHORT]


def _disagreeing_view(name="Split Guy"):
    return SourceView(
        name=name, position="WR", consensus=SD_LABEL, consensus_gap=25, consensus_pair=DYNASTY_PAIR,
        direction=MARKET_ABOVE_PROJECTION, market_rank=12, projection_rank=41, expert_note=None,
    )


def test_source_disagreement_on_a_rostered_player():
    ld = FakeLD(roster=full_roster([make_entry(player_id="p1", name="Split Guy")]), source_views={"p1": _disagreeing_view()})
    assert [c.kind for c in candidates(ld, FakeReport())] == [SOURCE_DISAGREEMENT]


def test_source_disagreement_about_a_player_i_neither_own_nor_want_is_ignored():
    ld = FakeLD(source_views={"stranger": _disagreeing_view()})
    assert candidates(ld, FakeReport()) == []


def test_velocity_just_short_of_the_directional_threshold():
    near = DIRECTIONAL_MIN_MOVE - 0.02
    ld = FakeLD(velocity={"p1": Velocity(STABLE, 4, near, "2026-08-29", "2026-09-02")},
                roster=full_roster([make_entry(player_id="p1", name="Mover")]))
    assert [c.kind for c in candidates(ld, FakeReport())] == [VELOCITY_NEAR]


def test_velocity_nowhere_near_the_threshold_is_not_watched():
    ld = FakeLD(velocity={"p1": Velocity(STABLE, 4, 0.01, "2026-08-29", "2026-09-02")},
                roster=full_roster([make_entry(player_id="p1")]))
    assert candidates(ld, FakeReport()) == []


def test_the_velocity_near_band_is_symmetric_around_the_directional_bar():
    """VELOCITY_NEAR_RATIO is a band either side of DIRECTIONAL_MIN_MOVE
    (0.08 +/- 0.05 => [0.03, 0.13]), and it reads |total_move|, so a fall of
    the same size is watched exactly as a rise is. Four cases: inside and
    outside each edge."""
    assert VELOCITY_NEAR_RATIO == 0.05 and DIRECTIONAL_MIN_MOVE == 0.08

    def kinds(move):
        ld = FakeLD(velocity={"p1": Velocity(STABLE, 4, move, "2026-08-29", "2026-09-02")},
                    roster=full_roster([make_entry(player_id="p1", name="Mover")]))
        return [c.kind for c in candidates(ld, FakeReport())]

    assert kinds(0.03) == [VELOCITY_NEAR]  # lower edge, inclusive
    assert kinds(0.029) == []              # one step under the lower edge
    assert kinds(0.13) == [VELOCITY_NEAR]  # upper edge, inclusive
    assert kinds(0.131) == []              # one step over the upper edge
    # Symmetric in sign: the same magnitudes falling behave identically.
    assert kinds(-0.03) == [VELOCITY_NEAR] and kinds(-0.13) == [VELOCITY_NEAR]
    assert kinds(-0.029) == [] and kinds(-0.131) == []


def test_a_player_already_labelled_rising_is_not_a_near_miss():
    ld = FakeLD(velocity={"p1": Velocity(RISING, 4, 0.20, "2026-08-29", "2026-09-02")},
                roster=full_roster([make_entry(player_id="p1")]))
    assert candidates(ld, FakeReport()) == []


def test_waiver_target_with_no_drop_on_a_full_roster():
    ld = FakeLD(roster=full_roster([make_entry(player_id="mine")]), waiver_targets=[make_target(tier=MUST_ADD, drop=None)])
    found = [c for c in candidates(ld, FakeReport()) if c.kind == WAIVER_NO_DROP]
    assert len(found) == 1
    assert found[0].player_name == "Waiver Guy"


def test_no_drop_is_not_a_problem_when_the_roster_has_room():
    ld = FakeLD(roster=open_roster([make_entry(player_id="mine")]), waiver_targets=[make_target(drop=None)])
    assert not [c for c in candidates(ld, FakeReport()) if c.kind == WAIVER_NO_DROP]


def test_trade_receive_priced_as_an_overpay():
    wanted = make_entry(player_id="t1", name="Pricey Guy")
    ld = FakeLD(proposals=[make_proposal(receive=[wanted], my_value=130.0, their_value=100.0)])
    found = [c for c in candidates(ld, FakeReport()) if c.kind == TRADE_PRICE_HIGH]
    assert len(found) == 1
    assert "price too high" in found[0].reason


def test_a_balanced_trade_is_not_a_price_problem():
    ld = FakeLD(proposals=[make_proposal(receive=[make_entry(player_id="t1")], my_value=100.0, their_value=100.0)])
    assert not [c for c in candidates(ld, FakeReport()) if c.kind == TRADE_PRICE_HIGH]


def test_injured_rostered_player_who_may_return():
    ld = FakeLD(roster=full_roster([make_entry(player_id="p1", name="Hurt Guy", injury_status="Out")]))
    found = [c for c in candidates(ld, FakeReport()) if c.kind == INJURED_MAY_RETURN]
    assert len(found) == 1
    assert found[0].snapshot["injury_status"] == "Out"


def test_a_questionable_tag_is_not_watched():
    ld = FakeLD(roster=full_roster([make_entry(player_id="p1", injury_status="Questionable")]))
    assert candidates(ld, FakeReport()) == []


def _stash(label=WATCH, reasons=("60th percentile dynasty value", "no roster spot without cutting a real player")):
    return StashCandidate(make_entry(player_id="s1", name="Rookie Guy"), label, 62.0, list(reasons))


def test_stash_watch_blocked_only_by_a_roster_spot():
    ld = FakeLD(stash=[_stash()])
    found = [c for c in candidates(ld, FakeReport()) if c.kind == STASH_BLOCKED]
    assert len(found) == 1


def test_a_priority_stash_that_already_has_its_spot_is_not_watched():
    ld = FakeLD(stash=[_stash(label=PRIORITY_STASH, reasons=("60th percentile dynasty value",))])
    assert candidates(ld, FakeReport()) == []


def test_pre_draft_or_errored_leagues_produce_nothing():
    assert candidates(FakeLD(drafted=False, role_trends={"p1": ROLE_RISING}), FakeReport()) == []
    assert candidates(FakeLD(error="sync failed", role_trends={"p1": ROLE_RISING}), FakeReport()) == []


def test_candidates_are_deterministically_ordered():
    ld = FakeLD(
        roster=full_roster([make_entry(player_id="p1", name="Hurt Guy", injury_status="Out")]),
        role_trends={"p1": ROLE_RISING},
        stash=[_stash()],
        waiver_targets=[make_target(tier=MUST_ADD, drop=None)],
    )
    kinds = [c.kind for c in candidates(ld, FakeReport())]
    assert kinds == [ROLE_RISING_SHORT, WAIVER_NO_DROP, INJURED_MAY_RETURN, STASH_BLOCKED]
    assert kinds == [c.kind for c in candidates(ld, FakeReport())]


def test_item_id_is_stable_per_league_kind_and_player():
    assert item_id("L1", ROLE_RISING_SHORT, "p1") == item_id("L1", ROLE_RISING_SHORT, "p1")
    assert item_id("L1", ROLE_RISING_SHORT, "p1") != item_id("L2", ROLE_RISING_SHORT, "p1")


# -- promotion conditions -------------------------------------------------------


def test_promotion_role_label_improved():
    assert promotions({"role_label": ROLE_RISING}, {"role_label": ROLE_SURGING}) == ["role_improved"]
    assert promotions({"role_label": ROLE_SURGING}, {"role_label": ROLE_RISING}) == []


def test_promotion_velocity_crossed_the_threshold():
    assert promotions({"velocity_label": STABLE}, {"velocity_label": RISING}) == ["velocity_crossed"]
    assert promotions({"velocity_label": RISING}, {"velocity_label": RISING}) == []


def test_promotion_tier_reached_must_or_strong_add():
    assert promotions({"tier": MODERATE}, {"tier": MUST_ADD}) == ["tier_promoted"]
    assert promotions({"tier": STRONG_ADD}, {"tier": MUST_ADD}) == []


def test_promotion_favourable_trade_receive():
    assert promotions({"favourable_receive": False}, {"favourable_receive": True}) == ["favourable_receive"]


def test_promotion_roster_spot_and_drop_candidate():
    assert promotions({"open_spots": 0}, {"open_spots": 2}) == ["roster_spot"]
    assert promotions({"has_drop_candidate": False}, {"has_drop_candidate": True}) == ["drop_candidate"]


def test_promotion_scarcity_changed():
    assert promotions({"scarcity": "Normal"}, {"scarcity": "Very Scarce"}) == ["scarcity_changed"]
    assert promotions({"scarcity": None}, {"scarcity": "Very Scarce"}) == []  # nothing to compare against


def test_promotion_conflict_gone():
    assert promotions({"has_conflict": True}, {"has_conflict": False}) == ["conflict_gone"]
    assert promotions({"has_conflict": False}, {"has_conflict": True}) == []


def test_promotion_injury_cleared():
    assert promotions({"injury_status": "Out"}, {"injury_status": None}) == ["injury_cleared"]
    assert promotions({"injury_status": "Out"}, {"injury_status": "IR"}) == []


# -- update ---------------------------------------------------------------------


def test_a_brand_new_candidate_starts_as_still_watching():
    ld = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine")]))
    result = update(Watchlist(), candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld})
    item = next(iter(result.items.values()))
    assert item.trigger_state == STILL_WATCHING
    assert item.first_seen == item.last_seen == "2026-09-02"


def test_a_promotion_on_a_later_run_is_a_new_trigger():
    roster = full_roster([make_entry(player_id="mine")])
    day1 = FakeLD(role_trends={"p1": ROLE_RISING}, roster=roster)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})

    day2 = FakeLD(role_trends={"p1": ROLE_SURGING}, roster=roster)
    result = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    item = next(iter(result.items.values()))
    assert item.trigger_state == NEW_TRIGGER
    assert ROLE_SURGING in item.trigger_reason
    assert item.triggered_on == {"role_improved": "2026-09-03"}


def test_the_same_condition_never_triggers_twice():
    roster = full_roster([make_entry(player_id="mine")])
    day1 = FakeLD(role_trends={"p1": ROLE_RISING}, roster=roster)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = FakeLD(role_trends={"p1": ROLE_SURGING}, roster=roster)
    stored = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    day3 = FakeLD(role_trends={"p1": ROLE_SURGING}, roster=roster)
    result = update(stored, candidates(day3, FakeReport(generated_at=DAY3)), now=DAY3, ld_by_league={"L1": day3})
    assert next(iter(result.items.values())).trigger_state == STILL_WATCHING


def test_same_day_rerun_adds_no_duplicate_and_re_triggers_nothing():
    ld = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine")]))
    first = update(Watchlist(), candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld})
    second = update(first, candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld})
    assert len(second.items) == len(first.items) == 1
    item = next(iter(second.items.values()))
    assert item.trigger_state == STILL_WATCHING
    assert item.misses == 0
    assert item.last_seen == "2026-09-02"


def test_same_day_rerun_keeps_a_trigger_that_already_fired_today():
    roster = full_roster([make_entry(player_id="mine")])
    day1 = FakeLD(role_trends={"p1": ROLE_RISING}, roster=roster)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = FakeLD(role_trends={"p1": ROLE_SURGING}, roster=roster)
    triggered = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    rerun = update(triggered, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    assert next(iter(rerun.items.values())).trigger_state == NEW_TRIGGER


def test_resolved_after_two_consecutive_misses():
    ld = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine")]))
    stored = update(Watchlist(), candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld})
    quiet = FakeLD(roster=full_roster([make_entry(player_id="mine")]))

    after_one = update(stored, [], now=DAY2, ld_by_league={"L1": quiet})
    item = next(iter(after_one.items.values()))
    assert (item.trigger_state, item.misses) == (STILL_WATCHING, 1)

    after_two = update(after_one, [], now=DAY3, ld_by_league={"L1": quiet})
    item = next(iter(after_two.items.values()))
    assert item.trigger_state == RESOLVED
    assert str(RESOLVE_AFTER_MISSES) in item.trigger_reason


def test_a_resolved_item_is_dropped_on_the_following_run():
    ld = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine")]))
    stored = update(Watchlist(), candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld})
    quiet = FakeLD(roster=full_roster([make_entry(player_id="mine")]))
    stored = update(stored, [], now=DAY2, ld_by_league={"L1": quiet})
    stored = update(stored, [], now=DAY3, ld_by_league={"L1": quiet})
    assert stored.items  # resolved, still reportable on the run that resolved it
    later = update(stored, [], now=DAY3 + dt.timedelta(days=1), ld_by_league={"L1": quiet})
    assert later.items == {}


def test_acquiring_the_player_resolves_the_item():
    watched = make_entry(player_id="p1", name="Rising Guy")
    day1 = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine")]))
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine"), watched]))
    result = update(stored, [], now=DAY2, ld_by_league={"L1": day2})
    item = next(iter(result.items.values()))
    assert item.trigger_state == RESOLVED
    assert "acquired" in item.trigger_reason


def test_a_league_missing_from_this_run_is_left_alone():
    ld = FakeLD(role_trends={"p1": ROLE_RISING}, roster=full_roster([make_entry(player_id="mine")]))
    stored = update(Watchlist(), candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld})
    result = update(stored, [], now=DAY2, ld_by_league={})
    item = next(iter(result.items.values()))
    assert (item.trigger_state, item.misses) == (STILL_WATCHING, 0)


def test_metrics_reads_every_promotion_input_off_the_report_objects():
    target = make_target(player_id="w1", tier=MUST_ADD, drop=make_entry(player_id="d1"))
    ld = FakeLD(
        roster=open_roster([make_entry(player_id="mine")]),
        waiver_targets=[target],
        velocity={"w1": Velocity(RISING, 4, 0.2, "2026-08-29", "2026-09-02")},
        role_trends={"w1": ROLE_SURGING},
    )
    m = metrics(ld, "w1", week=5)
    assert m["tier"] == MUST_ADD
    assert m["velocity_label"] == RISING
    assert m["role_label"] == ROLE_SURGING
    assert m["has_drop_candidate"] is True
    assert m["open_spots"] == 1
    assert m["on_my_roster"] is False
    assert m["week"] == 5


# -- rendering ------------------------------------------------------------------


def test_render_lines_only_prints_new_triggers_and_counts_the_rest():
    watchlist = Watchlist(items={
        "a": _item(item_id="a", trigger_state=NEW_TRIGGER, trigger_reason="role label improved to Role Surging"),
        "b": _item(item_id="b", player_name="Quiet Guy", trigger_state=STILL_WATCHING),
        "c": _item(item_id="c", player_name="Other Guy", trigger_state=STILL_WATCHING),
    })
    lines, still_watching = render_lines(watchlist)
    assert still_watching == 2
    assert len(lines) == 1
    assert "Rising Guy" in lines[0]
    assert "role label improved" in lines[0]
    assert "Quiet Guy" not in " ".join(lines)
