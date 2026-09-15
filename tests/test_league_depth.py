from __future__ import annotations

import pytest
from waiver_fixtures import player, roster

from sleeper_tool.league_depth import BASELINE_SKILL_PER_TEAM, effective_league_size, rostered_skill_players

# The four real leagues the rule was measured against: (label, teams,
# skill players rostered league-wide, effective size).
REAL_LEAGUES = [
    ("Primo Veterans", 8, 145, 10.4),
    ("This League Sucks", 12, 127, 9.1),
    ("Disco", 12, 228, 16.3),
    ("The Surfeit", 10, 129, 9.2),
]


def league(num_teams: int, *, skill: int = 0, kickers: int = 0, taxi: int = 0, reserve: int = 0):
    """`skill` skill players spread over `num_teams` rosters, plus bodies
    that must not be counted (K/DEF) and ones that must be (IR/taxi)."""
    rosters = [roster([], roster_id=i + 1) for i in range(num_teams)]
    for i in range(skill):
        rosters[i % num_teams].entries.append(player(f"s{i}", "WR", 10.0))
    for i in range(kickers):
        rosters[i % num_teams].entries.append(player(f"k{i}", "K" if i % 2 else "DEF", 8.0))
    for i in range(taxi):
        rosters[i % num_teams].entries.append(player(f"t{i}", "RB", 5.0, is_taxi=True))
    for i in range(reserve):
        rosters[i % num_teams].entries.append(player(f"r{i}", "TE", 5.0, is_reserve=True))
    return rosters


def test_the_baseline_is_a_standard_sixteen_slot_team_minus_kicker_and_defense():
    assert BASELINE_SKILL_PER_TEAM == 14


@pytest.mark.parametrize("name,num_teams,skill,expected", REAL_LEAGUES, ids=[r[0] for r in REAL_LEAGUES])
def test_effective_size_reproduces_the_four_real_leagues(name, num_teams, skill, expected):
    assert round(effective_league_size(league(num_teams, skill=skill)), 1) == expected


def test_a_league_rostering_exactly_the_baseline_has_an_effective_size_of_its_team_count():
    assert effective_league_size(league(12, skill=12 * 14)) == 12.0


def test_kickers_and_defenses_are_not_players_taken_out_of_the_skill_pool():
    assert rostered_skill_players(league(10, skill=140, kickers=40)) == 140
    assert effective_league_size(league(10, skill=140, kickers=40)) == 10.0


def test_a_stashed_player_on_ir_or_taxi_is_still_off_the_waiver_wire_and_counts():
    assert rostered_skill_players(league(10, skill=120, taxi=10, reserve=10)) == 140
    assert effective_league_size(league(10, skill=120, taxi=10, reserve=10)) == 10.0


def test_shallow_and_deep_rosters_separate_two_leagues_of_the_same_team_count():
    shallow = effective_league_size(league(12, skill=127))
    deep = effective_league_size(league(12, skill=228))
    assert shallow < 12 < deep
    assert (round(shallow, 1), round(deep, 1)) == (9.1, 16.3)


def test_an_unsynced_league_with_no_rostered_players_falls_back_to_the_team_count():
    assert effective_league_size(league(12)) == 12.0
    assert effective_league_size([]) == 0.0


def test_effective_size_is_not_rounded_so_a_ten_point_four_league_is_not_a_ten_team_one():
    assert effective_league_size(league(8, skill=145)) > 10
