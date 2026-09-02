import datetime as dt
import json

from sleeper_tool.decision_delta import (
    RECOMMENDATION,
    ROSTER,
    STATUS,
    VALUATION,
    compute_delta,
    load_latest_snapshot,
    save_snapshot,
)


def _snap(generated="2026-09-01T12:00:00+00:00", **league):
    base = {
        "name": "League A", "team_status": "contender",
        "trade_targets": {"t1": "Target One"}, "waiver_targets": {"w1": "Waiver One"},
        "roster": {"p1": {"name": "Player One", "value": 100.0}, "p2": {"name": "Player Two", "value": 50.0}},
    }
    base.update(league)
    return {"schema": 2, "generated_at": generated, "current_week": 3, "leagues": {"L1": base}, "best_moves": []}


def test_no_previous_snapshot_means_no_delta():
    assert compute_delta(None, _snap()) is None


def test_unchanged_snapshots_produce_an_empty_delta_with_the_baseline_time():
    delta = compute_delta(_snap(), _snap(generated="2026-09-02T12:00:00+00:00"))
    assert delta.items == []
    assert delta.since == dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)


def test_status_recommendation_roster_and_valuation_changes_are_reported():
    before = _snap()
    after = _snap(
        team_status="middling",
        trade_targets={"t2": "Target Two"},
        waiver_targets={"w1": "Waiver One", "w3": "Waiver Three"},
        roster={"p1": {"name": "Player One", "value": 116.0}, "p3": {"name": "Player Three", "value": 10.0}},
    )
    delta = compute_delta(before, after)
    texts = {(i.kind, i.text) for i in delta.items}
    assert (STATUS, "Team status contender → middling") in texts
    assert (RECOMMENDATION, "New trade target: Target Two") in texts
    assert (RECOMMENDATION, "No longer a trade target: Target One") in texts
    assert (RECOMMENDATION, "New waiver target: Waiver Three") in texts
    assert (ROSTER, "Joined your roster: Player Three") in texts
    assert (ROSTER, "Left your roster: Player Two") in texts
    assert (VALUATION, "Player One value +16%") in texts
    assert [i.kind for i in delta.items][0] == STATUS  # status changes lead


def test_valuation_moves_under_15_percent_are_ignored_and_relative_not_rank_based():
    after = _snap(roster={"p1": {"name": "Player One", "value": 114.0}, "p2": {"name": "Player Two", "value": 42.0}})
    delta = compute_delta(_snap(), after)
    assert [i.text for i in delta.by_kind(VALUATION)] == ["Player Two value -16%"]


def test_a_league_missing_from_the_previous_run_is_not_diffed():
    previous = _snap()
    current = _snap()
    current["leagues"]["L2"] = dict(current["leagues"]["L1"], name="League B")
    assert compute_delta(previous, current).items == []


def test_snapshots_persist_and_only_the_latest_two_are_kept(tmp_path):
    for day in (1, 2, 3):
        save_snapshot(_snap(generated=f"2026-09-0{day}T12:00:00+00:00"), tmp_path)
    kept = sorted(p.name for p in tmp_path.glob("*.json"))
    assert kept == ["20260902.json", "20260903.json"]
    assert load_latest_snapshot(tmp_path)["generated_at"] == "2026-09-03T12:00:00+00:00"


def test_same_day_rerun_overwrites_and_still_diffs_against_the_previous_day(tmp_path):
    save_snapshot(_snap(generated="2026-09-01T12:00:00+00:00"), tmp_path)
    save_snapshot(_snap(generated="2026-09-02T09:00:00+00:00"), tmp_path)
    save_snapshot(_snap(generated="2026-09-02T09:10:00+00:00"), tmp_path)  # re-run ten minutes later
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["20260901.json", "20260902.json"]
    assert load_latest_snapshot(tmp_path)["generated_at"] == "2026-09-02T09:10:00+00:00"
    baseline = load_latest_snapshot(tmp_path, before_date="2026-09-02")
    assert baseline["generated_at"] == "2026-09-01T12:00:00+00:00"
    assert load_latest_snapshot(tmp_path, before_date="2026-09-01") ["generated_at"].startswith("2026-09-02")


def test_unreadable_or_older_schema_snapshot_is_ignored(tmp_path):
    (tmp_path / "20260901.json").write_text("{not json", encoding="utf-8")
    assert load_latest_snapshot(tmp_path) is None
    assert load_latest_snapshot(tmp_path / "does-not-exist") is None
    old = _snap()
    old["schema"] = 1
    (tmp_path / "20260901.json").write_text(json.dumps(old), encoding="utf-8")
    assert load_latest_snapshot(tmp_path) is None


def test_saved_snapshot_round_trips(tmp_path):
    path = save_snapshot(_snap(), tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["leagues"]["L1"]["name"] == "League A"


def test_snapshot_value_is_per_game_for_redraft_so_week_rollover_is_not_a_swing():
    from conftest import make_value

    from sleeper_tool.decision_delta import _stable_value

    pv = make_value(dynasty_value=4000, proj_points=170.0)  # ROS total as valued at week 1
    assert _stable_value(pv, "dynasty", 1) == 4000
    assert _stable_value(pv, "redraft", 1) == 10.0  # 170 over 17 games
    # The same player a week later, ROS total rescaled to 16 games: same per-game number.
    later = make_value(dynasty_value=4000, proj_points=160.0)
    assert _stable_value(later, "redraft", 2) == 10.0
