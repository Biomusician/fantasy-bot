import datetime as dt
import json

from conftest import make_entry, make_league_info, make_roster, make_value

from sleeper_tool.decision_delta import SNAPSHOTS_KEPT, build_snapshot, load_snapshots, save_snapshot
from sleeper_tool.market_velocity import (
    DIRECTIONAL_MIN_MOVE,
    FALLING,
    INSUFFICIENT_HISTORY,
    MIN_OBSERVATIONS,
    RAPID_MIN_MOVE,
    RAPIDLY_FALLING,
    RAPIDLY_RISING,
    RISING,
    STABLE,
    annotate_league,
    build_velocities,
    classify_velocity,
)
from sleeper_tool.report_data import LeagueReportData
from sleeper_tool.trade_engine import DropCandidate, TradeProposal
from sleeper_tool.waiver_engine import WaiverTarget


def _obs(values):
    return [(f"2026-09-{i + 1:02d}", v) for i, v in enumerate(values)]


def test_labels_and_boundaries():
    assert classify_velocity([]).label == INSUFFICIENT_HISTORY
    assert classify_velocity(_obs([100, 110])).label == INSUFFICIENT_HISTORY  # 2 < MIN_OBSERVATIONS
    assert MIN_OBSERVATIONS == 3
    assert classify_velocity(_obs([100, 100, 100])).label == STABLE
    # Rising: >= 8% total with 2 consecutive same-direction moves.
    rising = classify_velocity(_obs([100, 104, 100 * (1 + DIRECTIONAL_MIN_MOVE)]))
    assert rising.label == RISING and rising.rising and rising.notable
    assert classify_velocity(_obs([100, 104, 107.9])).label == STABLE  # under 8%
    assert classify_velocity(_obs([100, 112, 108])).label == STABLE  # 8% total but only one up move (then a down move)
    # Rapidly: >= 15% and every non-zero move the same way (flat days allowed).
    rapid = classify_velocity(_obs([100, 105, 105, 115]))
    assert rapid.label == RAPIDLY_RISING and rapid.total_move == 0.15
    assert classify_velocity(_obs([100, 112, 110, 120, 130])).label == RISING  # 30% up with a dip: not "rapidly"
    assert classify_velocity(_obs([100, 120, 118, 130])).label == STABLE  # 30% up but never two consecutive up-days
    assert classify_velocity(_obs([100, 94, 91, 92])).label == FALLING  # two consecutive down-days, a small bounce
    assert classify_velocity(_obs([100, 90, 85, 80])).label == RAPIDLY_FALLING
    assert classify_velocity(_obs([0, 0, 10])).label == INSUFFICIENT_HISTORY  # no base to measure from


def test_describe_is_bucketed_not_precise():
    v = classify_velocity(_obs([100, 104, 108.4]))
    assert v.describe() == "Rising (+8% over 3 daily observations since 2026-09-01)"
    assert classify_velocity(_obs([100])).describe() == "Insufficient History (1 of 3 daily observations)"


def _ld(entries, *, proposals=(), targets=(), drops=(), currency="dynasty"):
    roster = make_roster(roster_id=1, owner_id="me", owner_username="me", entries=entries, league=make_league_info(kind=currency, league_id="L1"))
    return LeagueReportData(
        league=make_league_info(name="L", league_id="L1", kind=currency), drafted=True, roster=roster, currency=currency,
        proposals=list(proposals), waiver_targets=list(targets), drop_candidates=list(drops),
    )


def _snap(date, values, tracked=None):
    return {
        "schema": 2, "generated_at": f"{date}T09:00:00+00:00", "current_week": 1,
        "leagues": {"L1": {"roster": {pid: {"name": pid, "value": v} for pid, v in values.items()}, "tracked": tracked or {}}},
    }


def test_history_plus_today_with_same_day_rerun_counted_once():
    star = make_entry(player_id="star", name="Star", position="WR", value=make_value(dynasty_value=6000))
    give = TradeProposal(league_name="L", currency="dynasty", target_username="r", target_team_name="r", give=[star], receive=[],
                         my_value_total=1, their_value_total=1, rationale_for_me=[], rationale_for_them=[], caveats=[])
    ld = _ld([star], proposals=[give])
    history = [
        _snap("2026-09-01", {"star": 5000}),
        _snap("2026-09-02", {"star": 5400}),
        _snap("2026-09-03", {"star": 9999}),  # today's earlier run: superseded by this run's 6000
    ]
    v = build_velocities(history, ld, current_week=1, today="2026-09-03")
    assert v["star"].observations == 3 and v["star"].label == RAPIDLY_RISING and v["star"].total_move == 0.2
    # Only actionable players are classified.
    bench = make_entry(player_id="bench", name="Bench", position="WR")
    assert "bench" not in build_velocities(history, _ld([star, bench], proposals=[give]), current_week=1, today="2026-09-03")


def test_redraft_uses_per_game_projection_so_shrinking_totals_are_not_falling():
    # proj_points is a rest-of-season total; per-game stays 10.0 across
    # weeks 1, 2 and 3 even though the totals shrink 170 -> 160 -> 150.
    target = WaiverTarget(player_id="w", name="W", position="RB", team="KC", trend_count=1, value=make_value(proj_points=150.0),
                          fills_need=False, need_rank=None, reason="r")
    ld = _ld([], targets=[target], currency="redraft")
    history = [_snap("2026-09-01", {}, {"w": {"name": "W", "value": 10.0}}), _snap("2026-09-02", {}, {"w": {"name": "W", "value": 10.0}})]
    v = build_velocities(history, ld, current_week=3, today="2026-09-03")
    assert v["w"].label == STABLE and v["w"].total_move == 0.0


def test_annotations_follow_the_direction_of_the_move():
    up = make_entry(player_id="up", name="Up", position="WR")
    down = make_entry(player_id="down", name="Down", position="RB")
    p = TradeProposal(league_name="L", currency="dynasty", target_username="r", target_team_name="r", give=[up], receive=[down],
                      my_value_total=1, their_value_total=1, rationale_for_me=[], rationale_for_them=[], caveats=[])
    target = WaiverTarget(player_id="up", name="Up", position="WR", team="KC", trend_count=1, value=make_value(), fills_need=False, need_rank=None, reason="r")
    drop = DropCandidate(entry=up, priority="Consider Dropping", reasons=["buried"])
    ld = _ld([up], proposals=[p], targets=[target], drops=[drop])
    vel = {"up": classify_velocity(_obs([100, 110, 120])), "down": classify_velocity(_obs([100, 92, 90]))}
    annotate_league(ld, vel)
    assert p.caveats == [
        "Market velocity: Up is Rapidly Rising (+20% over 3 daily observations since 2026-09-01) — you'd be selling a rising asset.",
        "Market velocity: Down is Falling (-10% over 3 daily observations since 2026-09-01) — check why before paying today's price.",
    ]
    assert p.rationale_for_me == []
    assert target.notes == ["Market velocity: Rapidly Rising (+20% over 3 daily observations since 2026-09-01)"]
    assert drop.reasons[-1].endswith("a rising player is worth a second look before cutting")
    # A Stable or Insufficient-History player adds nothing.
    quiet = TradeProposal(league_name="L", currency="dynasty", target_username="r", target_team_name="r", give=[down], receive=[],
                          my_value_total=1, their_value_total=1, rationale_for_me=[], rationale_for_them=[], caveats=[])
    annotate_league(_ld([down], proposals=[quiet]), {"down": classify_velocity(_obs([100, 100, 100]))})
    assert quiet.caveats == [] and quiet.rationale_for_me == []


def test_snapshot_carries_tracked_values_and_retention_keeps_28_days(tmp_path):
    star = make_entry(player_id="star", name="Star", position="WR", value=make_value(dynasty_value=6000))
    target_pv = make_value(dynasty_value=1500)
    receive = make_entry(player_id="tgt", name="Target", position="RB", value=make_value(dynasty_value=2500))
    p = TradeProposal(league_name="L", currency="dynasty", target_username="r", target_team_name="r", give=[star], receive=[receive],
                      my_value_total=1, their_value_total=1, rationale_for_me=[], rationale_for_them=[], caveats=[])
    t = WaiverTarget(player_id="w", name="W", position="RB", team="KC", trend_count=1, value=target_pv, fills_need=False, need_rank=None, reason="r")
    ld = _ld([star], proposals=[p], targets=[t])

    class _Report:
        leagues = [ld]
        current_week = 1
        generated_at = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)
        priority_actions = []

    snap = build_snapshot(_Report())
    assert snap["schema"] == 2  # additive field, no schema bump
    assert snap["leagues"]["L1"]["tracked"] == {"tgt": {"name": "Target", "value": 2500}, "w": {"name": "W", "value": 1500}}
    assert "star" not in snap["leagues"]["L1"]["tracked"]  # rostered players live in "roster"

    assert SNAPSHOTS_KEPT == 28
    for day in range(1, 31):
        s = dict(snap, generated_at=f"2026-08-{day:02d}T09:00:00+00:00")
        save_snapshot(s, tmp_path)
    kept = sorted(tmp_path.glob("*.json"))
    assert len(kept) == 28 and kept[0].stem == "20260803"
    loaded = load_snapshots(tmp_path, before_date="2026-08-30")
    assert len(loaded) == 27 and loaded[0]["generated_at"].startswith("2026-08-03")
    # Old-schema and unreadable files are skipped, not fatal.
    (tmp_path / "20260701.json").write_text(json.dumps({"schema": 1, "generated_at": "2026-07-01T00:00:00+00:00"}), encoding="utf-8")
    (tmp_path / "20260702.json").write_text("{not json", encoding="utf-8")
    assert len(load_snapshots(tmp_path)) == 28
