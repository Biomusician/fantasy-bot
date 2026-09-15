"""League Depth — how deep this league's waiver pool actually is, expressed
as the team count a standard league would need to roster as many players.

Team count alone is a bad proxy for depth, and every waiver source that
scopes advice by league size ("Add in 12+ Team Leagues") is really scoping
by how many players are off the board league-wide. Those two only agree
when roster size is standard, and none of these leagues is standard: an
8-team keeper with 20-man rosters takes more players out of circulation
than a 12-team redraft with 13-man rosters.

So: count the skill players (QB/RB/WR/TE) rostered league-wide and divide
by what one standard team rosters. The result is a float "effective size"
in the same units the boards tag in — a 12-team league whose owners hold
228 skill players rosters like a 16-team league, and one holding 127 like
a 9-team league.

Measured against the real leagues this reproduces 145 -> 10.4 (Primo
Veterans, 8 teams), 127 -> 9.1 (This League Sucks, 12), 228 -> 16.3
(Disco, 12) and 129 -> 9.2 (The Surfeit, 10).
"""
from __future__ import annotations

from collections.abc import Iterable

from sleeper_tool.roster_analysis import SKILL_POSITIONS, ValuedRoster

# A standard 16-slot redraft team minus its K and DEF: the roster shape the
# waiver boards write their league-size tags against.
BASELINE_SKILL_PER_TEAM = 14


def rostered_skill_players(rosters: Iterable[ValuedRoster]) -> int:
    """Skill players held league-wide, IR and taxi included: a stashed
    player is still off the waiver wire, which is the only thing this
    count is measuring."""
    return sum(1 for r in rosters for e in r.entries if e.position in SKILL_POSITIONS)


def effective_league_size(rosters: Iterable[ValuedRoster]) -> float:
    """This league's depth in league-size units — how many standard teams'
    worth of skill players its owners are holding.

    Never rounded here: the callers that show it to a reader round it, and
    the callers that compare it against a board's "12+" tag want the raw
    ratio so a 10.4 league is not silently a 10-team one.
    """
    teams = list(rosters)
    skill = rostered_skill_players(teams)
    if not skill:
        # No roster data (an unsynced league). Fall back to the team count,
        # which is what every caller assumed before this module existed.
        return float(len(teams))
    return skill / BASELINE_SKILL_PER_TEAM
