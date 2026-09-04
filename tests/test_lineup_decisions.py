import itertools

import pytest
from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.bye_collision import ByeCollision, ByeHole
from sleeper_tool.lineup_decisions import (
    BYE_HOLE,
    EMPTY_SLOT,
    EPSILON,
    FLEX_EXPLANATION,
    INJURY_RISK,
    KIND_ORDER,
    MATCHUP,
    MAX_ITEMS,
    SET_LINEUP_MISMATCH,
    SURPRISE_RATIO,
    build_lineup_decisions,
)
from sleeper_tool.lineup_leverage import CLEAR_START, LEAN_START, TOSS_UP, build_lineup_leverage
from sleeper_tool.lineup_optimizer import optimize_lineup, slot_eligibility
from sleeper_tool.matchup_leverage import NEAR_EVEN, MatchupLeverage

# Week 17 leaves one game, so per-week numbers equal the projections and
# the arithmetic in these tests reads directly.
WEEK = 17


def _p(pid, pos, proj, *, set_starter=False, injury=None, bye=None, team="KC"):
    return make_entry(
        player_id=pid, name=pid, position=pos, team=team, is_starter=set_starter, injury_status=injury,
        value=make_value(name=pid, position=pos, proj_points=proj, bye_week=bye),
    )


def _roster(entries, positions):
    return make_roster(entries=entries, fmt=make_format(roster_positions=positions), league=make_league_info(kind="redraft"))


def _build(roster, week=WEEK, **kw):
    return build_lineup_decisions(roster, current_week=week, **kw)


def _brute_force_total(roster, week, excluded=()):
    """Best legal lineup total by trying every assignment of available
    players to slots — the reference the optimizer-based what-ifs must
    match."""
    slots = [s for s in roster.fmt.roster_positions if s != "BN"]
    avail = [
        e for e in roster.entries
        if e.player_id not in excluded and not (e.value.bye_week == week) and e.injury_status != "Out"
    ]
    best = 0.0
    for k in range(1, min(len(slots), len(avail)) + 1):
        for chosen_slots in itertools.combinations(range(len(slots)), k):
            for players in itertools.permutations(avail, k):
                if all(p.position in slot_eligibility(slots[s]) for p, s in zip(players, chosen_slots)):
                    best = max(best, sum(p.value.proj_points or 0 for p in players))
    return best


# --- 1. set-lineup mismatches -------------------------------------------------


def test_set_lineup_mismatch_leads_and_pairs_the_set_starter_with_the_optimizer_entrant():
    r = _roster(
        [_p("rb1", "RB", 100), _p("rb2", "RB", 80, set_starter=True), _p("wr1", "WR", 100, set_starter=True)],
        ("RB", "WR", "BN"),
    )
    d = _build(r)
    assert d.kinds() == [SET_LINEUP_MISMATCH]
    item = d.items[0]
    assert item.slot == "RB"
    assert [p.player_id for p in item.players] == ["rb2", "rb1"]
    assert item.delta == pytest.approx(20.0)
    assert item.describe() == "Set-lineup mismatch: rb2 is set to start but rb1 projects higher this week at RB (100.0 vs 80.0, +20.0/wk)"


def test_mismatch_differences_at_or_under_epsilon_are_not_decisions():
    at = _roster([_p("rb1", "RB", 100 + EPSILON), _p("rb2", "RB", 100, set_starter=True)], ("RB", "BN"))
    assert _build(at).kinds() == [TOSS_UP]  # a near-tie is a Toss-Up, never a "fix your lineup"
    over = _roster([_p("rb1", "RB", 100 + 2 * EPSILON), _p("rb2", "RB", 100, set_starter=True)], ("RB", "BN"))
    assert _build(over).kinds() == [SET_LINEUP_MISMATCH, TOSS_UP]  # fix the lineup, and know it's a coin flip


def test_set_starter_on_bye_counts_the_entrant_in_full():
    r = _roster([_p("rb1", "RB", 60), _p("rb2", "RB", 100, set_starter=True, bye=WEEK)], ("RB", "BN"))
    d = _build(r)
    item = d.items[0]
    assert item.kind == SET_LINEUP_MISMATCH and item.delta == pytest.approx(60.0)
    assert "rb2 is set to start at RB but is on bye week 17" in item.summary
    assert d.items[0].projections == [0.0, 60.0]


def test_set_starter_with_no_legal_fill_is_still_flagged():
    r = _roster([_p("rb2", "RB", 100, set_starter=True, bye=WEEK)], ("RB",))
    d = _build(r)
    assert d.kinds() == [SET_LINEUP_MISMATCH, EMPTY_SLOT]
    assert d.items[0].summary == "rb2 is set to start but is on bye week 17 — no legal fill on the roster"


def test_no_set_lineup_at_all_produces_no_mismatches():
    r = _roster([_p("rb1", "RB", 100), _p("rb2", "RB", 50)], ("RB", "BN"))
    assert _build(r) is None


def test_agreeing_lineups_produce_no_mismatches():
    r = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 50)], ("RB", "BN"))
    assert _build(r) is None


def test_per_week_numbers_divide_by_games_remaining():
    r = _roster([_p("rb1", "RB", 170), _p("rb2", "RB", 153, set_starter=True)], ("RB", "BN"))
    d = _build(r, week=1)  # 17 games left
    assert d.games_left == 17
    assert d.items[0].delta == pytest.approx(1.0)
    assert d.items[0].projections == pytest.approx([9.0, 10.0])


# --- 2. close calls -------------------------------------------------------------


def test_toss_up_what_if_is_the_optimizer_cost_of_benching_the_starter():
    r = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 97)], ("RB", "BN"))
    d = _build(r)
    assert d.kinds() == [TOSS_UP]
    item = d.items[0]
    assert item.slot == "RB" and [p.player_id for p in item.players] == ["rb1", "rb2"]
    assert item.what_if == "Starting rb2 instead costs 3.0/wk"
    week = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True)
    without = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True, excluded_player_ids={"rb1"})
    assert item.delta == pytest.approx(week.total_projected_points - without.total_projected_points)
    assert d.close_call_stake == pytest.approx(3.0)


def test_lean_start_uses_leverages_label_and_a_clear_start_says_nothing():
    lean = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 88)], ("RB", "BN"))
    assert _build(lean).kinds() == [LEAN_START]
    clear = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 60)], ("RB", "BN"))
    assert _build(clear) is None


def test_close_call_whose_starter_is_on_bye_this_week_is_not_a_start_sit_call():
    # Structural leverage says RB is a Toss-Up, but rb1 can't play this week:
    # the optimizer already starts rb2, so there's nothing to decide there.
    r = _roster([_p("rb1", "RB", 100, bye=WEEK, set_starter=True), _p("rb2", "RB", 97, set_starter=False)], ("RB", "BN"))
    d = _build(r)
    assert TOSS_UP not in d.kinds()


def test_context_lines_attach_to_the_players_of_an_item():
    r = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 97)], ("RB", "BN"))
    d = _build(r, context_lines={"rb2": ["Role: 60% snaps, rising"], "nobody": ["ignored"]})
    assert d.items[0].context == ["Role: 60% snaps, rising"]


def test_close_calls_are_relabelled_on_this_weeks_lineup_when_a_bye_changes_it():
    # Structurally FLEX is wr2 over wr3 (Clear Start). With wr2 on bye this
    # week the real call is wr3 vs te1, a Toss-Up the structural leverage
    # never saw.
    r = _roster(
        [_p("wr1", "WR", 200, set_starter=True), _p("wr2", "WR", 150, set_starter=True, bye=WEEK),
         _p("wr3", "WR", 100, set_starter=False), _p("te1", "TE", 95)],
        ("WR", "FLEX", "BN", "BN"),
    )
    structural = optimize_lineup(r)
    leverage = build_lineup_leverage(r, lineup=structural, current_week=WEEK)
    assert [d.label for d in leverage.decisions] == [CLEAR_START, CLEAR_START]
    d = build_lineup_decisions(r, structural_lineup=structural, leverage=leverage, current_week=WEEK)
    toss = next(i for i in d.items if i.kind == TOSS_UP)
    assert toss.slot == "FLEX" and [p.player_id for p in toss.players] == ["wr3", "te1"]
    assert toss.what_if == "Starting te1 instead costs 5.0/wk"


def test_schedule_notes_from_structural_leverage_carry_over_to_the_relabelled_calls():
    r = _roster(
        [_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 97),
         _p("wr1", "WR", 200, set_starter=True, bye=WEEK), _p("wr2", "WR", 100)],
        ("RB", "WR", "BN", "BN"),
    )
    structural = optimize_lineup(r)
    leverage = build_lineup_leverage(r, lineup=structural, current_week=WEEK)
    rb = next(d for d in leverage.decisions if d.slot == "RB")
    rb.schedule_note = "schedule tiebreak: rb2 plays 3 of the 3 upcoming weeks, rb1 plays 2"
    d = build_lineup_decisions(r, structural_lineup=structural, leverage=leverage, current_week=WEEK)
    toss = next(i for i in d.items if i.kind == TOSS_UP)
    assert toss.context == [rb.schedule_note]


# --- 3. injury risk ---------------------------------------------------------------


@pytest.mark.parametrize("status", ["Questionable", "Doubtful"])
def test_questionable_starter_names_the_optimizer_next_man_up(status):
    r = _roster([_p("rb1", "RB", 100, set_starter=True, injury=status), _p("rb2", "RB", 70)], ("RB", "BN"))
    d = _build(r)
    assert d.kinds() == [INJURY_RISK]
    item = d.items[0]
    assert [p.player_id for p in item.players] == ["rb1", "rb2"]
    week = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True)
    without = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True, excluded_player_ids={"rb1"})
    assert item.delta == pytest.approx(week.total_projected_points - without.total_projected_points) == pytest.approx(30.0)
    assert item.summary == f"RB: rb1 is {status} — next man up is rb2 (70.0/wk, -30.0/wk)"


def test_questionable_starter_with_no_backup_says_so():
    r = _roster([_p("rb1", "RB", 100, set_starter=True, injury="Questionable")], ("RB",))
    item = _build(r).items[0]
    assert item.kind == INJURY_RISK and item.players == [r.entries[0]] and item.delta == pytest.approx(100.0)
    assert "no rostered player can legally fill the slot" in item.summary


def test_out_starter_is_already_out_of_the_week_lineup_so_it_is_a_mismatch_not_a_risk():
    r = _roster([_p("rb1", "RB", 100, set_starter=True, injury="Out"), _p("rb2", "RB", 70)], ("RB", "BN"))
    d = _build(r)
    assert d.kinds() == [SET_LINEUP_MISMATCH]
    assert "rb1 is set to start at RB but is ruled out this week" in d.items[0].summary


# --- 4. empty slots and bye holes ------------------------------------------------


def test_empty_slot_offers_the_best_available_free_agent():
    r = _roster([_p("rb1", "RB", 100, set_starter=True)], ("QB", "RB"))
    fa_bye = _p("fa_bye", "QB", 300, bye=WEEK)
    fa_out = _p("fa_out", "QB", 250, injury="Out")
    fa = _p("fa", "QB", 200)
    d = _build(r, free_agents=[fa_bye, fa_out, fa])
    assert d.kinds() == [EMPTY_SLOT]
    assert d.items[0].slot == "QB"
    assert d.items[0].what_if == "Best free-agent fill: fa (QB, 200.0/wk)"
    assert _build(r).items[0].what_if is None


def test_empty_kicker_or_defense_slot_offers_an_unprojected_body_honestly():
    r = _roster([_p("rb1", "RB", 100, set_starter=True)], ("RB", "DEF"))
    d = _build(r, free_agents=[_p("fa_def", "DEF", None), _p("fa_wr", "WR", 90)])
    assert d.kinds() == [EMPTY_SLOT]
    assert d.items[0].what_if == "A free agent can fill it: fa_def (DEF) — no projection to rank the options by"


def test_bye_hole_this_week_when_the_fill_is_weak_and_nothing_next_week():
    this_week = _roster([_p("rb1", "RB", 100, bye=WEEK, set_starter=False), _p("rb2", "RB", 50)], ("RB", "BN"))
    d = _build(this_week)
    assert d.kinds() == [BYE_HOLE]
    item = d.items[0]
    assert [p.player_id for p in item.players] == ["rb1", "rb2"] and item.delta == pytest.approx(50.0)
    assert item.summary == "RB: rb1 is on bye week 17; rb2 fills in at 50% of his projection (-50.0/wk)"
    next_week = _roster([_p("rb1", "RB", 100, bye=WEEK + 1), _p("rb2", "RB", 50)], ("RB", "BN"))
    assert _build(next_week) is None


def test_bye_with_an_adequate_fill_is_not_a_decision():
    r = _roster([_p("rb1", "RB", 100, bye=WEEK), _p("rb2", "RB", 70)], ("RB", "BN"))  # exactly the 70% ratio
    assert _build(r) is None


def test_callers_bye_collision_is_used_only_for_the_current_week():
    r = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 40)], ("RB", "BN"))
    hole = ByeHole(WEEK, "RB", r.entries[0], 100.0, r.entries[1], 40.0)
    plan = ByeCollision(week=WEEK, holes=[hole], starters_on_bye=[r.entries[0]], weeks_scanned=[WEEK])
    d = _build(r, bye_collision=plan)
    assert d.kinds() == [BYE_HOLE] and d.items[0].delta == pytest.approx(60.0)
    later = ByeCollision(week=WEEK + 1, holes=[hole], starters_on_bye=[r.entries[0]], weeks_scanned=[WEEK + 1])
    assert _build(r, bye_collision=later) is None


def test_bye_hole_with_no_cover_names_a_free_agent_of_the_displaced_position():
    r = _roster([_p("rb1", "RB", 100, bye=WEEK), _p("wr1", "WR", 90)], ("FLEX", "BN"))
    # wr1 slides into FLEX; the FLEX isn't empty, so this is a hole with a fill, not an empty slot.
    d = _build(r, free_agents=[_p("fa_rb", "RB", 95), _p("fa_wr", "WR", 99)])
    assert d is None  # 90/100 clears the ratio: nothing to decide
    weak = _roster([_p("rb1", "RB", 100, bye=WEEK), _p("wr1", "WR", 50)], ("FLEX", "BN"))
    d = _build(weak, free_agents=[_p("fa_rb", "RB", 95), _p("fa_wr", "WR", 99)])
    assert d.kinds() == [BYE_HOLE]
    assert d.items[0].what_if == "Best free-agent fill: fa_rb (RB, 95.0/wk)"


# --- 5. flex / superflex explanations ------------------------------------------


def _sf_roster(qb2_proj):
    return _roster(
        [_p("qb1", "QB", 200, set_starter=True), _p("wr1", "WR", 150, set_starter=True),
         _p("wr2", "WR", 100, set_starter=True), _p("qb2", "QB", qb2_proj)],
        ("QB", "WR", "SUPER_FLEX", "BN"),
    )


def test_flex_explanation_at_and_below_the_surprise_ratio():
    d = _build(_sf_roster(100 * SURPRISE_RATIO))
    assert d.kinds() == [FLEX_EXPLANATION]
    item = d.items[0]
    assert item.slot == "SUPER_FLEX" and [p.player_id for p in item.players] == ["wr2", "qb2"]
    assert item.delta == pytest.approx(25.0)
    assert item.summary == "wr2 (WR) occupies Superflex because moving qb2 (QB) there would reduce the lineup by 25.0/wk"
    assert _build(_sf_roster(100 * SURPRISE_RATIO - 0.1)) is None


def test_flex_explanation_defers_to_a_close_call_on_the_same_slot():
    d = _build(_sf_roster(90))  # 10% gap: a Lean Start already covers SUPER_FLEX
    assert d.kinds() == [LEAN_START]


def test_flex_explanation_needs_a_different_position():
    r = _roster(
        [_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 80, set_starter=True), _p("rb3", "RB", 62)],
        ("RB", "FLEX", "BN"),
    )
    assert _build(r) is None


def test_flex_explanation_measures_a_cascade_with_the_optimizer():
    # Pulling wr2 from SUPER_FLEX lets the optimizer move a different starter
    # rather than plugging the bench QB straight in; the reduction is the
    # optimizer's, not (occupant - candidate).
    r = _roster(
        [_p("qb1", "QB", 200, set_starter=True), _p("wr1", "WR", 150, set_starter=True),
         _p("wr2", "WR", 100, set_starter=True), _p("qb2", "QB", 80), _p("wr3", "WR", 95)],
        ("QB", "WR", "SUPER_FLEX", "BN", "BN"),
    )
    d = _build(r)
    # wr3 is the leverage alternative (Toss-Up at 5%), so the slot is covered by that call, not a surprise.
    assert d.kinds() == [TOSS_UP]
    assert d.items[0].what_if == "Starting wr3 instead costs 5.0/wk"


# --- 6. matchup ------------------------------------------------------------------


def _matchup(gap, week_lineup):
    return MatchupLeverage(
        week=WEEK, opponent_roster_id=2, opponent_name="Opp", my_points=100.0, opponent_points=100.0 - gap,
        gap=gap, label=NEAR_EVEN, my_lineup=week_lineup, opponent_lineup=week_lineup,
    )


def test_matchup_line_appears_only_when_the_close_calls_outweigh_the_gap():
    r = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 97)], ("RB", "BN"))
    week = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True)
    close = _build(r, matchup=_matchup(1.0, week))
    assert close.kinds() == [TOSS_UP, MATCHUP]
    assert close.items[-1].describe() == (
        "Matchup: The close calls above carry 3.0/wk between them, more than the 1.0-point projected gap vs Opp"
        " — the lineup calls decide this matchup"
    )
    wide = _build(r, matchup=_matchup(-5.0, week))
    assert wide.kinds() == [TOSS_UP]
    no_calls = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 50)], ("RB", "BN"))
    assert _build(no_calls, matchup=_matchup(0.0, week)) is None


# --- cap, order, determinism ----------------------------------------------------


def _many_mismatches(n):
    good = [_p(f"good{i}", "RB", 100 + i) for i in range(n)]
    bad = [_p(f"bad{i}", "RB", 10 + i, set_starter=True) for i in range(n)]
    return _roster(good + bad, ("RB",) * n + ("BN",) * n)


def test_view_is_capped_at_max_items_most_material_first():
    d = _build(_many_mismatches(MAX_ITEMS + 2))
    assert len(d.items) == MAX_ITEMS
    deltas = [i.delta for i in d.items]
    assert deltas == sorted(deltas, reverse=True)


def test_matchup_line_keeps_the_last_slot_under_the_cap():
    r = _many_mismatches(MAX_ITEMS + 2)
    r.entries.append(_p("rbx", "RB", 99))  # a Toss-Up against good0 (100)
    week = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True)
    d = _build(r, matchup=_matchup(0.5, week))
    assert len(d.items) == MAX_ITEMS and d.items[-1].kind == MATCHUP
    assert d.items[0].kind == SET_LINEUP_MISMATCH


def test_kinds_follow_the_documented_order_and_runs_are_deterministic():
    r = _roster(
        [
            _p("qb1", "QB", 200, set_starter=True),
            _p("rb1", "RB", 100, set_starter=True, injury="Questionable"), _p("rb2", "RB", 70),
            _p("wr1", "WR", 100), _p("wr2", "WR", 80, set_starter=True),  # mismatch at WR
            _p("te1", "TE", 60, set_starter=True), _p("te2", "TE", 58),  # Toss-Up at TE
            _p("k1", "K", None, set_starter=True, bye=WEEK),  # K on bye, nobody to fill
        ],
        ("QB", "RB", "WR", "TE", "K", "BN", "BN", "BN"),
    )
    week = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True)
    first = _build(r, matchup=_matchup(0.5, week))
    second = _build(r, matchup=_matchup(0.5, week))
    assert [i.describe() for i in first.items] == [i.describe() for i in second.items]
    kinds = first.kinds()
    assert kinds == [SET_LINEUP_MISMATCH, SET_LINEUP_MISMATCH, TOSS_UP, INJURY_RISK, EMPTY_SLOT, MATCHUP]
    assert kinds == sorted(kinds, key=KIND_ORDER.index)


def test_what_if_deltas_match_a_brute_force_search_with_the_swap_forced():
    r = _roster(
        [
            _p("rb1", "RB", 100, set_starter=True, injury="Questionable"), _p("rb2", "RB", 96),
            _p("wr1", "WR", 90, set_starter=True), _p("wr2", "WR", 85, set_starter=True), _p("te1", "TE", 84),
        ],
        ("RB", "WR", "FLEX", "BN", "BN"),
    )
    d = _build(r)
    full = _brute_force_total(r, WEEK)
    assert full == pytest.approx(optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True).total_projected_points)
    checked = 0
    for item in d.items:
        if item.kind in (TOSS_UP, LEAN_START, INJURY_RISK):
            starter = item.players[0]
            assert item.delta == pytest.approx(full - _brute_force_total(r, WEEK, excluded={starter.player_id}))
            checked += 1
    assert checked >= 2


def test_empty_roster_and_missing_inputs():
    assert _build(_roster([], ("RB",))) is None
    r = _roster([_p("rb1", "RB", 100, set_starter=True), _p("rb2", "RB", 97)], ("RB", "BN"))
    structural = optimize_lineup(r)
    week = optimize_lineup(r, nfl_week=WEEK, exclude_game_day_out=True)
    explicit = build_lineup_decisions(r, structural_lineup=structural, week_lineup=week, current_week=WEEK)
    assert [i.describe() for i in explicit.items] == [i.describe() for i in _build(r).items]
