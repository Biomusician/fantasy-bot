import datetime as dt
import json
from dataclasses import asdict

from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.market_velocity import DIRECTIONAL_MIN_MOVE, FALLING, RAPIDLY_RISING, RISING, STABLE, Velocity
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, SCARCE
from sleeper_tool.role_trends import COLLAPSING as ROLE_COLLAPSING
from sleeper_tool.role_trends import FALLING as ROLE_FALLING
from sleeper_tool.role_trends import STABLE as ROLE_STABLE
from sleeper_tool.source_disagreement import DYNASTY_PAIR, MARKET_ABOVE_PROJECTION, SOURCE_DISAGREEMENT as SD_LABEL, SourceView
from sleeper_tool.stash_board import PRIORITY_STASH, WATCH, StashCandidate
from sleeper_tool.trade_types import TradeProposal
from sleeper_tool.waiver_engine import MODERATE, MONITOR, MUST_ADD, SPECULATIVE, STRONG_ADD, WaiverTarget
from sleeper_tool.watchlist import (
    DISAGREEMENT_GAP_STEP,
    INJURED_MAY_RETURN,
    INVALIDATED,
    NEW_TRIGGER,
    NO_NEED_SCARCITY,
    PERCENTILE_STEP,
    PRICE_CAUGHT_UP_PERCENTILE,
    RESOLVE_AFTER_MISSES,
    RESOLVED,
    RETURN_DEAD_STATUSES,
    ROLE_INVALIDATING_LABELS,
    ROLE_RISING,
    ROLE_RISING_SHORT,
    ROLE_SURGING,
    SECTION_INVALIDATED,
    SECTION_STRENGTHENED,
    SECTION_TRIGGERED,
    SECTION_WEAKENED,
    SOURCE_DISAGREEMENT,
    STASH_BLOCKED,
    STASH_INVALIDATE_PERCENTILE_DROP,
    STILL_WATCHING,
    THESIS_STRENGTHENED,
    THESIS_UNCHANGED,
    THESIS_WEAKENED,
    TRADE_PRICE_HIGH,
    TRIGGERED,
    VELOCITY_MOVE_STEP,
    VELOCITY_NEAR,
    VELOCITY_NEAR_RATIO,
    WAIVER_NO_DROP,
    WATCH_MAX_AGE_DAYS,
    WATCHLIST_SCHEMA,
    WatchItem,
    Watchlist,
    assess,
    candidates,
    invalidation,
    item_id,
    load_watchlist,
    metrics,
    promotions,
    render_lines,
    render_sections,
    save_watchlist,
    thesis_text,
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
        thesis="usage rising (Role Rising), not on the waiver board",
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

    An item unseen for WATCH_MAX_AGE_DAYS is dropped: 27 days kept, 28 gone.
    """
    assert WATCH_MAX_AGE_DAYS == 28
    # DAY1 is 2026-09-02: 27d ago is 2026-08-06, 28d is 2026-08-05, 29d is 2026-08-04.
    existing = Watchlist(items={
        "d27": _item(item_id="d27", last_seen="2026-08-06"),
        "d28": _item(item_id="d28", last_seen="2026-08-05"),
        "d29": _item(item_id="d29", last_seen="2026-08-04"),
    })
    result = update(existing, [], now=DAY1, ld_by_league={})
    assert set(result.items) == {"d27"}


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


def test_render_lines_prints_triggers_and_counts_the_unchanged_rest():
    watchlist = Watchlist(items={
        "a": _item(item_id="a", trigger_state=NEW_TRIGGER, trigger_reason="role label improved to Role Surging",
                   thesis_state=TRIGGERED, thesis_note="role label improved to Role Surging"),
        "b": _item(item_id="b", player_name="Quiet Guy", trigger_state=STILL_WATCHING),
        "c": _item(item_id="c", player_name="Other Guy", trigger_state=STILL_WATCHING),
    })
    lines, still_watching = render_lines(watchlist)
    assert still_watching == 2
    assert len(lines) == 1
    assert lines[0].startswith("Triggered: Test League: Rising Guy — role label improved")
    assert "(watched since 2026-09-01: usage rising (Role Rising), not on the waiver board)" in lines[0]
    assert "Quiet Guy" not in " ".join(lines)


# -- thesis: schema and text ----------------------------------------------------


def test_schema_stays_one_and_a_pre_thesis_file_loads_with_defaults(tmp_path):
    """A file written before the thesis fields has none of them. It must
    load (schema unchanged: nothing already stored changed meaning), and
    each row's thesis is rebuilt from the snapshot it already carries."""
    assert WATCHLIST_SCHEMA == 1
    old_row = {
        "item_id": "old1", "league_id": "L1", "league_name": "Test League", "kind": WAIVER_NO_DROP,
        "player_id": "w1", "player_name": "Waiver Guy", "reason": "Must Add add with no droppable player and a full roster",
        "first_seen": "2026-08-30", "last_seen": "2026-09-01", "trigger_state": STILL_WATCHING, "trigger_reason": "",
        "snapshot": {"tier": MUST_ADD, "open_spots": 0, "has_drop_candidate": False, "scarcity": "Scarce",
                     "role_label": None, "velocity_label": STABLE},
        "misses": 1, "triggered_on": {}, "last_run_on": "2026-09-01", "resolved_on": "",
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    watchlist_path(tmp_path).write_text(json.dumps({"schema": 1, "generated_at": "x", "items": [old_row]}), encoding="utf-8")
    item = load_watchlist(tmp_path).items["old1"]
    assert item.misses == 1 and item.last_run_on == "2026-09-01"
    assert item.thesis_state == THESIS_UNCHANGED and item.thesis_note == "" and item.resolved_reason == ""
    assert item.last_metrics == {}
    assert item.thesis == "Must Add add, roster full, no drop candidate, no role read, value flat (Stable), replacement market Scarce"


def test_thesis_text_names_the_evidence_for_every_kind():
    base = {"role_label": ROLE_RISING, "tier": MODERATE, "velocity_label": STABLE, "velocity_move": 0.06,
            "open_spots": 0, "has_drop_candidate": False, "scarcity": SCARCE, "injury_status": "Out",
            "on_my_roster": False, "percentile": 62.0, "receive_balance": "Overpay", "receive_ratio": 1.3,
            "disagreement_gap": 25}
    assert thesis_text(ROLE_RISING_SHORT, base) == "usage rising (Role Rising), Moderate waiver row, value flat (Stable), roster full, no drop candidate"
    assert thesis_text(WAIVER_NO_DROP, base) == "Moderate add, roster full, no drop candidate, usage rising (Role Rising), value flat (Stable), replacement market Scarce"
    assert thesis_text(STASH_BLOCKED, base) == "stash-worthy at the 62nd percentile, roster full, no drop candidate, value flat (Stable), usage rising (Role Rising)"
    assert thesis_text(VELOCITY_NEAR, base) == "value moved +6% against a 8% bar (Stable), usage rising (Role Rising), not on my roster"
    assert thesis_text(SOURCE_DISAGREEMENT, base) == "sources split by 25 places, not on my roster, value flat (Stable)"
    assert thesis_text(TRADE_PRICE_HIGH, base) == "priced as Overpay (ratio 1.30), 62nd percentile, value flat (Stable)"
    assert thesis_text(INJURED_MAY_RETURN, base) == "injury Out, not on my roster, usage rising (Role Rising), value flat (Stable)"
    # Missing inputs are named as unknown, never dropped silently.
    assert thesis_text(ROLE_RISING_SHORT, {}) == "no role read, not on the waiver board, value unmeasured, roster full, no drop candidate"
    assert thesis_text(ROLE_RISING_SHORT, {"open_spots": 2, "has_drop_candidate": True}).endswith("2 open roster spots")


def test_every_candidate_carries_a_thesis_built_from_its_snapshot():
    ld = FakeLD(
        roster=full_roster([make_entry(player_id="p1", name="Hurt Guy", injury_status="Out")]),
        role_trends={"p1": ROLE_RISING},
        stash=[_stash()],
        waiver_targets=[make_target(tier=MUST_ADD, drop=None)],
        proposals=[make_proposal(receive=[make_entry(player_id="t1", name="Pricey Guy")], my_value=130.0, their_value=100.0)],
        velocity={"p1": Velocity(STABLE, 4, 0.06, "2026-08-29", "2026-09-02")},
        source_views={"p1": _disagreeing_view()},
    )
    found = candidates(ld, FakeReport())
    assert {c.kind for c in found} == {ROLE_RISING_SHORT, WAIVER_NO_DROP, VELOCITY_NEAR, SOURCE_DISAGREEMENT,
                                       TRADE_PRICE_HIGH, INJURED_MAY_RETURN, STASH_BLOCKED}
    for c in found:
        assert c.thesis == thesis_text(c.kind, c.snapshot)
        assert c.thesis and c.thesis.lower() != "watch"
        assert c.last_metrics == c.snapshot
        assert c.thesis_state == THESIS_UNCHANGED
    by_kind = {c.kind: c for c in found}
    assert by_kind[TRADE_PRICE_HIGH].thesis.startswith("priced as Overpay (ratio 1.30)")
    assert by_kind[SOURCE_DISAGREEMENT].thesis.startswith("sources split by 25 places, on my roster")
    assert by_kind[INJURED_MAY_RETURN].thesis.startswith("injury Out, on my roster")


def test_metrics_carries_the_price_and_disagreement_inputs():
    wanted = make_entry(player_id="t1", name="Pricey Guy")
    ld = FakeLD(
        proposals=[
            make_proposal(receive=[wanted], my_value=130.0, their_value=100.0),   # Overpay
            make_proposal(receive=[wanted], my_value=112.0, their_value=100.0),   # Slight overpay — the better price
        ],
        source_views={"t1": _disagreeing_view()},
    )
    m = metrics(ld, "t1")
    assert m["receive_balance"] == "Slight overpay" and m["receive_ratio"] == 1.12
    assert m["disagrees"] is True and m["disagreement_gap"] == 25
    assert m["roster_known"] is True
    quiet = metrics(FakeLD(), "t1")
    assert quiet["receive_balance"] is None and quiet["receive_ratio"] is None
    assert quiet["disagrees"] is None and quiet["disagreement_gap"] is None


# -- thesis: strengthened / weakened per kind (assess reads the metrics dict) ---


def test_role_thesis_moves():
    kind = ROLE_RISING_SHORT
    assert assess(kind, {"tier": SPECULATIVE}, {"tier": MODERATE}) == (THESIS_STRENGTHENED, "waiver tier Speculative → Moderate")
    assert assess(kind, {"tier": MODERATE}, {"tier": None}) == (THESIS_WEAKENED, "waiver tier Moderate → off the board")
    assert assess(kind, {"velocity_label": FALLING}, {"velocity_label": STABLE}) == (THESIS_STRENGTHENED, "velocity Falling → Stable")
    assert assess(kind, {"role_label": ROLE_SURGING}, {"role_label": ROLE_RISING}) == (THESIS_WEAKENED, "role Role Surging → Role Rising")
    assert assess(kind, {"role_label": ROLE_RISING}, {"role_label": ROLE_STABLE}) == (THESIS_WEAKENED, "role Role Rising → Stable Role")
    assert assess(kind, {"injury_status": None}, {"injury_status": "Out"}) == (THESIS_WEAKENED, "injury healthy → Out")
    assert assess(kind, {"has_drop_candidate": True}, {"has_drop_candidate": False}) == (THESIS_WEAKENED, "drop candidate exists → none")
    assert assess(kind, {"open_spots": 1}, {"open_spots": 0}) == (THESIS_WEAKENED, "open roster spot yes → no")
    assert assess(kind, {"scarcity": NORMAL}, {"scarcity": SCARCE}) == (THESIS_STRENGTHENED, "replacement market Normal → Scarce")
    # An unknown label on either side is not a level and never a move.
    assert assess(kind, {"role_label": None}, {"role_label": ROLE_RISING}) == (THESIS_UNCHANGED, "")
    assert assess(kind, {"velocity_label": STABLE}, {"velocity_label": None}) == (THESIS_UNCHANGED, "")


def test_waiver_no_drop_thesis_moves():
    assert assess(WAIVER_NO_DROP, {"tier": MUST_ADD}, {"tier": MODERATE}) == (THESIS_WEAKENED, "waiver tier Must Add → Moderate")
    assert assess(WAIVER_NO_DROP, {"tier": STRONG_ADD}, {"tier": MUST_ADD}) == (THESIS_STRENGTHENED, "waiver tier Strong Add → Must Add")
    assert assess(WAIVER_NO_DROP, {"has_drop_candidate": False}, {"has_drop_candidate": True}) == (THESIS_STRENGTHENED, "drop candidate none → exists")


def test_stash_thesis_moves_on_percentile_at_the_step():
    assert PERCENTILE_STEP == 5.0
    assert assess(STASH_BLOCKED, {"percentile": 60.0}, {"percentile": 65.0}) == (THESIS_STRENGTHENED, "percentile 60 → 65")
    assert assess(STASH_BLOCKED, {"percentile": 60.0}, {"percentile": 64.9}) == (THESIS_UNCHANGED, "")
    assert assess(STASH_BLOCKED, {"percentile": 60.0}, {"percentile": 55.0}) == (THESIS_WEAKENED, "percentile 60 → 55")
    assert assess(STASH_BLOCKED, {"percentile": None}, {"percentile": 55.0}) == (THESIS_UNCHANGED, "")


def test_velocity_near_thesis_moves_on_the_size_of_the_move():
    assert VELOCITY_MOVE_STEP == 0.01
    assert assess(VELOCITY_NEAR, {"velocity_move": 0.06}, {"velocity_move": 0.07}) == (THESIS_STRENGTHENED, "value move +6% → +7%")
    assert assess(VELOCITY_NEAR, {"velocity_move": 0.06}, {"velocity_move": 0.069}) == (THESIS_UNCHANGED, "")
    assert assess(VELOCITY_NEAR, {"velocity_move": 0.06}, {"velocity_move": 0.05}) == (THESIS_WEAKENED, "value move +6% → +5%")
    # Read in magnitude: a fall growing is just as close to its label.
    assert assess(VELOCITY_NEAR, {"velocity_move": -0.06}, {"velocity_move": -0.07}) == (THESIS_STRENGTHENED, "value move -6% → -7%")


def test_source_disagreement_thesis_moves_on_the_gap_at_the_step():
    assert DISAGREEMENT_GAP_STEP == 5
    assert assess(SOURCE_DISAGREEMENT, {"disagreement_gap": 20}, {"disagreement_gap": 25}) == (THESIS_STRENGTHENED, "source gap 20 places → 25 places")
    assert assess(SOURCE_DISAGREEMENT, {"disagreement_gap": 20}, {"disagreement_gap": 24}) == (THESIS_UNCHANGED, "")
    assert assess(SOURCE_DISAGREEMENT, {"disagreement_gap": 20}, {"disagreement_gap": 15}) == (THESIS_WEAKENED, "source gap 20 places → 15 places")


def test_trade_price_thesis_moves_the_other_way_round():
    """For a trade target, a rising market is the thesis weakening: the
    same velocity move that strengthens an add thesis weakens this one."""
    kind = TRADE_PRICE_HIGH
    assert assess(kind, {"receive_balance": "Overpay"}, {"receive_balance": "Slight overpay"}) == (THESIS_STRENGTHENED, "trade price Overpay → Slight overpay")
    assert assess(kind, {"receive_balance": "Slight overpay"}, {"receive_balance": "Overpay"}) == (THESIS_WEAKENED, "trade price Slight overpay → Overpay")
    assert assess(kind, {"percentile": 60.0}, {"percentile": 65.0}) == (THESIS_WEAKENED, "percentile 60 → 65")
    assert assess(kind, {"percentile": 60.0}, {"percentile": 55.0}) == (THESIS_STRENGTHENED, "percentile 60 → 55")
    assert assess(kind, {"velocity_label": RISING}, {"velocity_label": RAPIDLY_RISING}) == (THESIS_WEAKENED, "velocity Rising → Rapidly Rising")
    assert assess(kind, {"velocity_label": STABLE}, {"velocity_label": FALLING}) == (THESIS_STRENGTHENED, "velocity Stable → Falling")
    assert assess(ROLE_RISING_SHORT, {"velocity_label": RISING}, {"velocity_label": RAPIDLY_RISING}) == (THESIS_STRENGTHENED, "velocity Rising → Rapidly Rising")
    # Not in any proposal this run is unknown, not a worse price.
    assert assess(kind, {"receive_balance": "Overpay"}, {"receive_balance": None}) == (THESIS_UNCHANGED, "")


def test_injury_return_thesis_moves_on_the_designation():
    kind = INJURED_MAY_RETURN
    assert assess(kind, {"injury_status": "Out"}, {"injury_status": "Doubtful"}) == (THESIS_STRENGTHENED, "injury Out → Doubtful")
    assert assess(kind, {"injury_status": "Doubtful"}, {"injury_status": "Out"}) == (THESIS_WEAKENED, "injury Doubtful → Out")
    assert assess(kind, {"injury_status": "IR"}, {"injury_status": "Out"}) == (THESIS_STRENGTHENED, "injury IR → Out")
    assert assess(kind, {"velocity_label": STABLE}, {"velocity_label": FALLING}) == (THESIS_WEAKENED, "velocity Stable → Falling")


def test_a_move_away_outranks_a_move_toward_and_the_note_keeps_both():
    state, note = assess(ROLE_RISING_SHORT, {"tier": SPECULATIVE, "injury_status": None}, {"tier": MODERATE, "injury_status": "Out"})
    assert state == THESIS_WEAKENED
    assert note == "injury healthy → Out (though waiver tier Speculative → Moderate)"


def test_nothing_moved_is_unchanged_with_an_empty_note():
    m = {"tier": MODERATE, "role_label": ROLE_RISING, "velocity_label": STABLE, "percentile": 60.0}
    for kind in (ROLE_RISING_SHORT, WAIVER_NO_DROP, STASH_BLOCKED, VELOCITY_NEAR, SOURCE_DISAGREEMENT, TRADE_PRICE_HIGH, INJURED_MAY_RETURN):
        assert assess(kind, m, dict(m)) == (THESIS_UNCHANGED, "")


# -- thesis: invalidation conditions -------------------------------------------


def test_role_thesis_dies_when_the_role_falls():
    assert ROLE_INVALIDATING_LABELS == (ROLE_FALLING, ROLE_COLLAPSING)
    for kind in (ROLE_RISING_SHORT, WAIVER_NO_DROP):
        assert invalidation(kind, {"role_label": ROLE_RISING}, {"role_label": ROLE_FALLING}) == "role fell to Role Falling"
        assert invalidation(kind, {"role_label": ROLE_RISING}, {"role_label": ROLE_COLLAPSING}) == "role fell to Role Collapsing"
        assert invalidation(kind, {"role_label": ROLE_RISING}, {"role_label": ROLE_STABLE}) is None  # weakened, not dead
    # A stash is a value thesis; its role falling is a weakening at most.
    assert invalidation(STASH_BLOCKED, {"role_label": ROLE_RISING}, {"role_label": ROLE_COLLAPSING}) is None


def test_add_thesis_dies_when_the_position_stops_being_a_need():
    assert NO_NEED_SCARCITY == ABUNDANT
    was, now = {"scarcity": NORMAL}, {"scarcity": ABUNDANT, "has_drop_candidate": False, "open_spots": 0, "on_my_roster": False}
    for kind in (ROLE_RISING_SHORT, WAIVER_NO_DROP, STASH_BLOCKED):
        assert invalidation(kind, was, now) == "no need at his position: replacement market Abundant, no drop candidate, no open spot"
    assert invalidation(TRADE_PRICE_HIGH, was, now) is None
    # It must be a MOVE to Abundant: an item watched at Abundant is not invalidated by staying there.
    assert invalidation(WAIVER_NO_DROP, {"scarcity": ABUNDANT}, now) is None
    assert invalidation(WAIVER_NO_DROP, {"scarcity": None}, now) is None
    # And only when there is genuinely no room and he is not mine.
    assert invalidation(WAIVER_NO_DROP, was, {**now, "has_drop_candidate": True}) is None
    assert invalidation(WAIVER_NO_DROP, was, {**now, "open_spots": 1}) is None
    assert invalidation(ROLE_RISING_SHORT, {**was, "on_my_roster": True}, {**now, "on_my_roster": True}) is None


def test_stash_thesis_dies_at_the_percentile_drop_boundary():
    assert STASH_INVALIDATE_PERCENTILE_DROP == 15.0
    assert invalidation(STASH_BLOCKED, {"percentile": 60.0}, {"percentile": 45.0}) == "no longer stash-worthy: percentile 60 → 45"
    assert invalidation(STASH_BLOCKED, {"percentile": 60.0}, {"percentile": 45.1}) is None
    assert invalidation(STASH_BLOCKED, {"percentile": None}, {"percentile": 10.0}) is None


def test_velocity_near_thesis_dies_when_the_value_settles_outside_the_band():
    edge = DIRECTIONAL_MIN_MOVE - VELOCITY_NEAR_RATIO  # 0.03
    assert invalidation(VELOCITY_NEAR, {}, {"velocity_move": edge}) is None
    assert invalidation(VELOCITY_NEAR, {}, {"velocity_move": 0.029}) == "value settled to +3%, outside the near band"
    assert invalidation(VELOCITY_NEAR, {}, {"velocity_move": -0.02}) == "value settled to -2%, outside the near band"
    assert invalidation(VELOCITY_NEAR, {}, {"velocity_move": None}) is None


def test_source_disagreement_thesis_dies_when_the_sources_agree():
    assert invalidation(SOURCE_DISAGREEMENT, {}, {"disagrees": False}) == "sources agree again"
    assert invalidation(SOURCE_DISAGREEMENT, {}, {"disagrees": None}) is None  # no view this run is not agreement
    assert invalidation(SOURCE_DISAGREEMENT, {}, {"disagrees": True}) is None


def test_price_thesis_dies_when_the_market_catches_up_at_the_boundary():
    assert PRICE_CAUGHT_UP_PERCENTILE == 15.0
    assert invalidation(TRADE_PRICE_HIGH, {"percentile": 60.0}, {"percentile": 75.0}) == "market caught up: percentile 60 → 75"
    assert invalidation(TRADE_PRICE_HIGH, {"percentile": 60.0}, {"percentile": 74.9}) is None
    assert assess(TRADE_PRICE_HIGH, {"percentile": 60.0}, {"percentile": 74.9}) == (THESIS_WEAKENED, "percentile 60 → 75")
    assert invalidation(STASH_BLOCKED, {"percentile": 60.0}, {"percentile": 75.0}) is None


def test_return_thesis_dies_on_a_move_to_ir():
    assert RETURN_DEAD_STATUSES == ("IR", "PUP")
    assert invalidation(INJURED_MAY_RETURN, {"injury_status": "Out"}, {"injury_status": "IR"}) == "moved to IR: out past the watch window"
    assert invalidation(INJURED_MAY_RETURN, {"injury_status": "Doubtful"}, {"injury_status": "PUP"}) == "moved to PUP: out past the watch window"
    assert invalidation(INJURED_MAY_RETURN, {"injury_status": "IR"}, {"injury_status": "IR"}) is None  # watched on IR: staying is not news
    assert invalidation(INJURED_MAY_RETURN, {"injury_status": "Out"}, {"injury_status": "Out"}) is None
    assert invalidation(ROLE_RISING_SHORT, {"injury_status": "Out"}, {"injury_status": "IR"}) is None


def test_any_thesis_dies_when_i_drop_the_player():
    for kind in (ROLE_RISING_SHORT, SOURCE_DISAGREEMENT, VELOCITY_NEAR, INJURED_MAY_RETURN):
        assert invalidation(kind, {"on_my_roster": True}, {"on_my_roster": False, "roster_known": True}) == "dropped from your roster"
    assert invalidation(ROLE_RISING_SHORT, {"on_my_roster": False}, {"on_my_roster": False, "roster_known": True}) is None
    # A run with no roster to read is not evidence that he left it.
    assert invalidation(ROLE_RISING_SHORT, {"on_my_roster": True}, {"on_my_roster": False, "roster_known": False}) is None


# -- thesis: update end to end --------------------------------------------------


def _only(watchlist: Watchlist) -> WatchItem:
    assert len(watchlist.items) == 1, list(watchlist.items.values())
    return next(iter(watchlist.items.values()))


def _rising_ld(tier, *, role=ROLE_RISING, day_roster=None):
    """One ROLE_RISING_SHORT item for w1: Role Rising, a weak waiver row, an
    open roster spot (so he is not also a WAIVER_NO_DROP item)."""
    return FakeLD(
        role_trends={"w1": role},
        roster=day_roster or open_roster([make_entry(player_id="mine")]),
        waiver_targets=[make_target(player_id="w1", tier=tier, drop=None)],
    )


def test_strengthened_then_unchanged_on_an_identical_rerun():
    day1 = _rising_ld(SPECULATIVE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    assert _only(stored).thesis_state == THESIS_UNCHANGED

    day2 = _rising_ld(MODERATE)
    stored = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    item = _only(stored)
    assert (item.trigger_state, item.thesis_state) == (STILL_WATCHING, THESIS_STRENGTHENED)
    assert item.thesis_note == "waiver tier Speculative → Moderate"
    assert item.last_metrics["tier"] == MODERATE
    assert item.snapshot["tier"] == SPECULATIVE  # the promotion baseline is untouched
    lines, still = render_lines(stored)
    assert lines == [f"Strengthened: Test League: Waiver Guy — waiver tier Speculative → Moderate (watched since 2026-09-02: {item.thesis})"]
    assert still == 0

    day3 = _rising_ld(MODERATE)
    stored = update(stored, candidates(day3, FakeReport(generated_at=DAY3)), now=DAY3, ld_by_league={"L1": day3})
    item = _only(stored)
    assert (item.thesis_state, item.thesis_note) == (THESIS_UNCHANGED, "")
    assert render_lines(stored) == ([], 1)


def test_weakened_then_unchanged_then_weakened_again_only_when_it_moves_again():
    day1 = _rising_ld(MODERATE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = _rising_ld(SPECULATIVE)
    stored = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    assert (_only(stored).thesis_state, _only(stored).thesis_note) == (THESIS_WEAKENED, "waiver tier Moderate → Speculative")
    day3 = _rising_ld(SPECULATIVE)
    stored = update(stored, candidates(day3, FakeReport(generated_at=DAY3)), now=DAY3, ld_by_league={"L1": day3})
    assert _only(stored).thesis_state == THESIS_UNCHANGED
    day4, DAY4 = _rising_ld(MONITOR), DAY3 + dt.timedelta(days=1)
    stored = update(stored, candidates(day4, FakeReport(generated_at=DAY4)), now=DAY4, ld_by_league={"L1": day4})
    assert (_only(stored).thesis_state, _only(stored).thesis_note) == (THESIS_WEAKENED, "waiver tier Speculative → Monitor")


def test_three_identical_runs_print_nothing():
    ld = _rising_ld(MODERATE)
    stored = Watchlist()
    for day in (DAY1, DAY2, DAY3):
        stored = update(stored, candidates(ld, FakeReport(generated_at=day)), now=day, ld_by_league={"L1": ld})
        assert render_lines(stored) == ([], 1)
        assert _only(stored).thesis_state == THESIS_UNCHANGED
    assert render_sections(stored) == {SECTION_TRIGGERED: [], SECTION_INVALIDATED: [], SECTION_STRENGTHENED: [], SECTION_WEAKENED: []}


def test_same_day_rerun_keeps_the_morning_thesis_verdict():
    day1 = _rising_ld(SPECULATIVE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = _rising_ld(MODERATE)
    morning = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    rerun = update(morning, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    assert _only(rerun).thesis_state == THESIS_STRENGTHENED
    assert _only(rerun).thesis_note == "waiver tier Speculative → Moderate"
    assert render_lines(rerun) == render_lines(morning)


def test_a_promotion_is_triggered_and_the_note_is_the_trigger_text():
    day1 = _rising_ld(MODERATE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = _rising_ld(MODERATE, role=ROLE_SURGING)
    stored = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    item = _only(stored)
    assert (item.trigger_state, item.thesis_state) == (NEW_TRIGGER, TRIGGERED)
    assert item.thesis_note == item.trigger_reason == "role label improved to Role Surging"
    lines, still = render_lines(stored)
    assert lines[0].startswith("Triggered: Test League: Waiver Guy — role label improved to Role Surging (watched since 2026-09-02: usage rising (Role Rising)")
    assert still == 0
    # The day after, the trigger is spent and the thesis is judged afresh.
    day3 = _rising_ld(MODERATE, role=ROLE_SURGING)
    stored = update(stored, candidates(day3, FakeReport(generated_at=DAY3)), now=DAY3, ld_by_league={"L1": day3})
    assert (_only(stored).trigger_state, _only(stored).thesis_state) == (STILL_WATCHING, THESIS_UNCHANGED)


def test_an_already_spent_promotion_falls_through_to_the_thesis_rules():
    """drop_candidate fires once; when the drop candidate later vanishes
    and reappears, the thesis rules (not a second trigger) describe it."""
    roster = full_roster([make_entry(player_id="mine")])
    day1 = FakeLD(role_trends={"w1": ROLE_RISING}, roster=roster, waiver_targets=[make_target(player_id="w1", tier=MODERATE, drop=None)])
    found = [c for c in candidates(day1, FakeReport()) if c.kind == ROLE_RISING_SHORT]
    stored = update(Watchlist(), found, now=DAY1, ld_by_league={"L1": day1})
    day2 = FakeLD(role_trends={"w1": ROLE_RISING}, roster=roster, waiver_targets=[make_target(player_id="w1", tier=MODERATE, drop=make_entry(player_id="d1"))])
    stored = update(stored, [c for c in candidates(day2, FakeReport(generated_at=DAY2)) if c.kind == ROLE_RISING_SHORT], now=DAY2, ld_by_league={"L1": day2})
    assert _only(stored).triggered_on == {"drop_candidate": "2026-09-03"}
    day3 = FakeLD(role_trends={"w1": ROLE_RISING}, roster=roster, waiver_targets=[make_target(player_id="w1", tier=MODERATE, drop=None)])
    stored = update(stored, [c for c in candidates(day3, FakeReport(generated_at=DAY3)) if c.kind == ROLE_RISING_SHORT], now=DAY3, ld_by_league={"L1": day3})
    assert (_only(stored).thesis_state, _only(stored).thesis_note) == (THESIS_WEAKENED, "drop candidate exists → none")
    day4 = day2
    DAY4 = DAY3 + dt.timedelta(days=1)
    stored = update(stored, [c for c in candidates(day4, FakeReport(generated_at=DAY4)) if c.kind == ROLE_RISING_SHORT], now=DAY4, ld_by_league={"L1": day4})
    assert (_only(stored).trigger_state, _only(stored).thesis_state, _only(stored).thesis_note) == (STILL_WATCHING, THESIS_STRENGTHENED, "drop candidate none → exists")


def test_an_invalidated_thesis_resolves_with_its_reason_and_is_pruned_after():
    day1 = _rising_ld(MODERATE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = _rising_ld(MODERATE, role=ROLE_FALLING)
    stored = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    item = _only(stored)
    assert (item.trigger_state, item.thesis_state) == (RESOLVED, INVALIDATED)
    assert item.resolved_reason == item.thesis_note == item.trigger_reason == "role fell to Role Falling"
    assert item.resolved_on == "2026-09-03"
    lines, still = render_lines(stored)
    assert lines == [f"Invalidated: Test League: Waiver Guy — role fell to Role Falling (watched since 2026-09-02: {item.thesis})"]
    assert still == 0
    later = update(stored, candidates(day2, FakeReport(generated_at=DAY3)), now=DAY3, ld_by_league={"L1": day2})
    assert later.items == {}


def test_invalidation_outranks_a_promotion_on_the_same_run():
    day1 = _rising_ld(MODERATE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = _rising_ld(MUST_ADD, role=ROLE_COLLAPSING)  # tier_promoted would fire, but the role thesis is dead
    stored = update(stored, [], now=DAY2, ld_by_league={"L1": day2})
    item = _only(stored)
    assert (item.trigger_state, item.thesis_state) == (RESOLVED, INVALIDATED)
    assert item.triggered_on == {}
    assert item.resolved_reason == "role fell to Role Collapsing"


def test_dropping_my_own_watched_player_invalidates_immediately():
    mine = make_entry(player_id="p1", name="Mover")
    day1 = FakeLD(velocity={"p1": Velocity(STABLE, 4, 0.06, "2026-08-29", "2026-09-02")}, roster=full_roster([mine]))
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    assert _only(stored).snapshot["on_my_roster"] is True
    day2 = FakeLD(velocity={"p1": Velocity(STABLE, 4, 0.06, "2026-08-29", "2026-09-03")}, roster=full_roster([make_entry(player_id="other")]))
    stored = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    item = _only(stored)
    assert (item.trigger_state, item.thesis_state, item.resolved_reason) == (RESOLVED, INVALIDATED, "dropped from your roster")


def test_two_misses_resolve_as_invalidated_with_the_reason_recorded():
    ld = _rising_ld(MODERATE)
    stored = update(Watchlist(), candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld})
    quiet = FakeLD(roster=open_roster([make_entry(player_id="mine")]))
    stored = update(stored, [], now=DAY2, ld_by_league={"L1": quiet})
    assert (_only(stored).thesis_state, _only(stored).thesis_note) == (THESIS_UNCHANGED, "")
    assert render_lines(stored) == ([], 1)
    stored = update(stored, [], now=DAY3, ld_by_league={"L1": quiet})
    item = _only(stored)
    assert (item.trigger_state, item.thesis_state) == (RESOLVED, INVALIDATED)
    assert item.resolved_reason == f"no longer a candidate for {RESOLVE_AFTER_MISSES} consecutive runs"
    assert render_lines(stored)[0][0].startswith("Invalidated: Test League: Waiver Guy — no longer a candidate")


def test_acquiring_the_player_is_a_triggered_resolution():
    day1 = _rising_ld(MODERATE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = FakeLD(role_trends={"w1": ROLE_RISING}, roster=open_roster([make_entry(player_id="mine"), make_entry(player_id="w1", name="Waiver Guy")]))
    stored = update(stored, [], now=DAY2, ld_by_league={"L1": day2})
    item = _only(stored)
    assert (item.trigger_state, item.thesis_state) == (RESOLVED, TRIGGERED)
    assert item.resolved_reason == "acquired — he is on your roster now"
    assert render_lines(stored)[0][0].startswith("Triggered: Test League: Waiver Guy — acquired")


def test_a_pre_thesis_item_gets_its_thesis_on_the_first_run_that_sees_it():
    ld = _rising_ld(MODERATE)
    fresh = _only(update(Watchlist(), candidates(ld, FakeReport()), now=DAY1, ld_by_league={"L1": ld}))
    legacy = WatchItem(**{**asdict(fresh), "thesis": "", "last_metrics": {}})
    stored = update(Watchlist(items={legacy.item_id: legacy}), candidates(ld, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": ld})
    item = _only(stored)
    assert item.thesis == fresh.thesis
    assert item.thesis_state == THESIS_UNCHANGED  # compared against the snapshot when there is no last_metrics


def test_thesis_fields_survive_a_round_trip(tmp_path):
    day1 = _rising_ld(SPECULATIVE)
    stored = update(Watchlist(), candidates(day1, FakeReport()), now=DAY1, ld_by_league={"L1": day1})
    day2 = _rising_ld(MODERATE)
    stored = update(stored, candidates(day2, FakeReport(generated_at=DAY2)), now=DAY2, ld_by_league={"L1": day2})
    save_watchlist(stored, tmp_path)
    loaded = load_watchlist(tmp_path)
    assert loaded.items == stored.items
    assert _only(loaded).thesis_state == THESIS_STRENGTHENED


# -- thesis: rendering ---------------------------------------------------------


def test_render_sections_and_lines_are_grouped_in_a_fixed_order():
    watchlist = Watchlist(items={
        "w": _item(item_id="w", kind=STASH_BLOCKED, player_name="Weak Guy", thesis_state=THESIS_WEAKENED, thesis_note="percentile 60 → 55"),
        "s": _item(item_id="s", kind=TRADE_PRICE_HIGH, player_name="Strong Guy", thesis_state=THESIS_STRENGTHENED, thesis_note="trade price Overpay → Slight overpay"),
        "i": _item(item_id="i", kind=VELOCITY_NEAR, player_name="Dead Guy", trigger_state=RESOLVED, thesis_state=INVALIDATED, thesis_note="sources agree again"),
        "t": _item(item_id="t", kind=WAIVER_NO_DROP, player_name="Hot Guy", trigger_state=NEW_TRIGGER, thesis_state=TRIGGERED, thesis_note="a roster spot opened up"),
        "u": _item(item_id="u", player_name="Quiet Guy"),
        "u2": _item(item_id="u2", player_name="Quiet Guy 2"),
    })
    sections = render_sections(watchlist)
    assert list(sections) == [SECTION_TRIGGERED, SECTION_INVALIDATED, SECTION_STRENGTHENED, SECTION_WEAKENED]
    assert [len(v) for v in sections.values()] == [1, 1, 1, 1]
    lines, still = render_lines(watchlist)
    assert [l.split(":")[0] for l in lines] == ["Triggered", "Invalidated", "Strengthened", "Weakened"]
    assert lines[2] == "Strengthened: Test League: Strong Guy — trade price Overpay → Slight overpay (watched since 2026-09-01: usage rising (Role Rising), not on the waiver board)"
    assert still == 2
    assert "Quiet Guy" not in " ".join(lines)


def test_render_order_within_a_section_is_kind_then_league_then_player():
    watchlist = Watchlist(items={
        "b": _item(item_id="b", kind=STASH_BLOCKED, player_name="Zed", thesis_state=THESIS_STRENGTHENED, thesis_note="n"),
        "a": _item(item_id="a", kind=STASH_BLOCKED, player_name="Abe", thesis_state=THESIS_STRENGTHENED, thesis_note="n"),
        "c": _item(item_id="c", kind=ROLE_RISING_SHORT, player_name="Zed", thesis_state=THESIS_STRENGTHENED, thesis_note="n"),
        "d": _item(item_id="d", kind=ROLE_RISING_SHORT, league_name="Aardvark League", player_name="Zed", thesis_state=THESIS_STRENGTHENED, thesis_note="n"),
    })
    names = [l.split("Strengthened: ")[1].split(" — ")[0] for l in render_lines(watchlist)[0]]
    assert names == ["Aardvark League: Zed", "Test League: Zed", "Test League: Abe", "Test League: Zed"]
    assert names == [l.split("Strengthened: ")[1].split(" — ")[0] for l in render_lines(watchlist)[0]]


def test_a_new_trigger_from_a_pre_thesis_file_still_renders_as_triggered():
    legacy = _item(trigger_state=NEW_TRIGGER, trigger_reason="injury status cleared", thesis="")
    lines, still = render_lines(Watchlist(items={legacy.item_id: legacy}))
    assert lines == ["Triggered: Test League: Rising Guy — injury status cleared (watched since 2026-09-01: Role Rising but not on the board)"]
    assert still == 0
