from sleeper_tool.nfl_schedule import parse_schedule_csv, schedule_from_rows
from sleeper_tool.schedule_window import NEXT_GAMES_WINDOW, TIEBREAK_MAX_VALUE_GAP, build_windows, playoff_weeks, schedule_tiebreak, team_window

# 18 regular-season weeks; KC plays every week except 7; LA (Rams) byes weeks 3 and 16;
# DAL and PHI play every week.
_rows = []
for w in range(1, 19):
    if w != 7:
        _rows.append(f"g{w}a,2026,REG,{w},2026-09-01,KC,DAL")
    else:
        _rows.append(f"g{w}a,2026,REG,{w},2026-09-01,DAL,PHI")
    if w not in (3, 16):
        _rows.append(f"g{w}b,2026,REG,{w},2026-09-01,LA,PHI" if w != 7 else f"g{w}b,2026,REG,{w},2026-09-01,LA,NYG")
CSV = "game_id,season,game_type,week,gameday,away_team,home_team\n" + "\n".join(_rows) + "\n"
SCHED = schedule_from_rows(parse_schedule_csv(CSV, 2026), 2026)


def test_playoff_weeks_come_from_league_settings_not_a_constant():
    assert playoff_weeks({"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 0}, SCHED) == [15, 16, 17]
    assert playoff_weeks({"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 1}, SCHED) == [15, 16, 17, 18]
    assert playoff_weeks({"playoff_week_start": 15, "playoff_teams": 4, "playoff_round_type": 2}, SCHED) == [15, 16, 17, 18]  # 2 rounds x 2 weeks
    assert playoff_weeks({"playoff_week_start": 16, "playoff_teams": 4}, SCHED) == [16, 17]
    assert playoff_weeks({"playoff_week_start": 14, "playoff_teams": 8}, SCHED) == [14, 15, 16]  # nonstandard start
    assert playoff_weeks({"playoff_week_start": 17, "playoff_teams": 12, "playoff_round_type": 2}, SCHED) == [17, 18]  # clamped to the schedule
    assert playoff_weeks({"playoff_teams": 6}, SCHED) is None and playoff_weeks({"playoff_week_start": "x", "playoff_teams": 6}, SCHED) is None
    assert playoff_weeks({"playoff_week_start": 15, "playoff_teams": 6}, None) == [15, 16, 17]  # no schedule: unclamped


def test_windows_and_team_views():
    w = build_windows(SCHED, {"playoff_week_start": 15, "playoff_teams": 6}, 2)
    assert w.next_weeks == [2, 3, 4] and w.remaining_weeks[-1] == 18 and w.playoff_weeks == [15, 16, 17]
    assert w.describe() == "next 3 (from this week): weeks 2-4 · regular season through week 18 · fantasy playoffs weeks 15-17 (6 teams)"
    kc, lar = team_window(SCHED, "KC", w), team_window(SCHED, "LAR", w)
    assert (kc.next_games, kc.next_byes, kc.remaining_games, kc.remaining_byes, kc.playoff_games) == (3, [], 16, [7], 3)
    assert (lar.next_games, lar.next_byes, lar.playoff_games, lar.playoff_byes) == (2, [3], 2, [16])
    assert kc.note() is None
    assert lar.note() == "bye week 3 inside the next 3 (this week included); bye in the fantasy playoffs (week 16)"
    assert team_window(SCHED, "NOPE", w) is None and team_window(SCHED, None, w) is None
    late = build_windows(SCHED, {}, 17)
    assert late.next_weeks == [17, 18] and late.playoff_weeks is None and "not set in league settings" in late.describe()
    assert build_windows(None, {}, 2) is None and build_windows(SCHED, {}, None) is None


def test_schedule_only_breaks_near_ties():
    w = build_windows(SCHED, {"playoff_week_start": 15, "playoff_teams": 6}, 2)
    close = schedule_tiebreak("A", "KC", 100, "B", "LAR", 100 * (1 - TIEBREAK_MAX_VALUE_GAP), SCHED, w)
    assert close == f"schedule tiebreak (values within 10%): A plays 3 of the {NEXT_GAMES_WINDOW} upcoming weeks, B plays 2"
    assert schedule_tiebreak("A", "KC", 100, "B", "LAR", 89, SCHED, w) is None  # value decides
    assert schedule_tiebreak("A", "KC", 100, "B", "DAL", 100, SCHED, w) is None  # schedule doesn't separate them
    assert schedule_tiebreak("A", "KC", 100, "B", "LAR", 100, SCHED, w, horizon="playoffs") == "schedule tiebreak (values within 10%): A plays 3 of the 3 fantasy playoff weeks, B plays 2"
    assert schedule_tiebreak("A", "KC", None, "B", "LAR", 100, SCHED, w) is None
    assert schedule_tiebreak("A", "KC", 100, "B", "NOPE", 100, SCHED, w) is None
