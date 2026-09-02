"""Roster Clog Detector — players occupying a roster spot with no plausible
path into the lineup, no meaningful market value, and no near-term
strategic use. Distinct from trade_engine.identify_drop_candidates (which
flags weak/buried/aging bench pieces on value percentile signals): a clog
must fail ALL of a stricter, lineup-aware test, so it's the "this spot is
simply dead" list, not the "you could probably do better here" list.

A player is a Roster Clog only when every one of these holds:
  - Not a starter in the tool's own best legal lineup (lineup_optimizer),
    not in an IR/reserve or taxi slot (deliberate stashes, and IR is exempt
    by rule), and not injury-designated IR.
  - Outside the top DYNASTY_CLOG_RANK_CUTOFF by reconciled overall rank
    (dynasty currency) or the top REDRAFT_CLOG_RANK_CUTOFF by FantasyPros
    rest-of-season rank (redraft currency, which is also what the keeper
    league is valued in throughout this codebase).
  - Projected below every started player at his position AND below the
    best non-started player there — i.e. he isn't even the primary backup.
  - Not among Sleeper's current trending adds (the waiver engine is already
    treating those as live assets, and a trending player is buzz, not
    dead weight).
  - Dynasty only: not a developmental player — years_exp at most
    DEVELOPMENTAL_MAX_YEARS_EXP (or unknown) and no older than his
    position's young-player age (team_status.AGE_THRESHOLDS). A young
    player's value is his upside, which rank/projection data captures
    poorly in his first seasons; a 23-year-old second-year WR at KTC 160
    is contingent value, not dead weight. (The spec said rookies only;
    the first real run flagged exactly that case, so it was widened.)
Uncorroborated or unranked players are skipped rather than flagged: a
name-matching miss looks exactly like a worthless player, and the
established discipline everywhere else in the tool is to never recommend
a cut off that ambiguity.

Consumers: the weekly report lists at most MAX_CLOGS_PER_ROSTER per team,
and waiver_engine prefers a clog as the drop paired with an add.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, projection_of
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.team_status import young_max_age
from sleeper_tool.trade_engine import DYNASTY_CURRENCY, value_currency
from sleeper_tool.valuation import composite_overall_rank

DYNASTY_CLOG_RANK_CUTOFF = 150  # outside the top-N reconciled overall dynasty rank
REDRAFT_CLOG_RANK_CUTOFF = 120  # outside the top-N FantasyPros rest-of-season rank
MAX_CLOGS_PER_ROSTER = 3
DEVELOPMENTAL_MAX_YEARS_EXP = 2  # dynasty: a player this early in his career is upside, not a clog


@dataclass
class RosterClog:
    entry: RosterEntry
    reasons: list[str]
    composite_rank: float


def _is_dynasty_developmental(entry: RosterEntry, currency: str) -> bool:
    if currency != DYNASTY_CURRENCY:
        return False
    if entry.years_exp is None or entry.years_exp <= DEVELOPMENTAL_MAX_YEARS_EXP:
        return entry.age is None or entry.age <= young_max_age(entry.position)
    return False


def identify_roster_clogs(
    roster: ValuedRoster,
    *,
    trending_add_ids: Collection[str] = (),
    exclude_ids: Collection[str] = (),
    lineup: LineupResult | None = None,
    max_clogs: int = MAX_CLOGS_PER_ROSTER,
) -> list[RosterClog]:
    """`exclude_ids`: players already spoken for elsewhere in the same
    report (e.g. give-pieces in a live trade proposal), so the report never
    calls the same player both trade capital and dead weight. `lineup` can
    be passed to reuse an already-computed optimizer result.
    """
    if not roster.entries:
        return []
    currency = value_currency(roster)
    cutoff = DYNASTY_CLOG_RANK_CUTOFF if currency == DYNASTY_CURRENCY else REDRAFT_CLOG_RANK_CUTOFF
    rank_label = "reconciled dynasty rank" if currency == DYNASTY_CURRENCY else "rest-of-season rank"
    lineup = lineup if lineup is not None else optimize_lineup(roster)
    starters = lineup.starter_ids
    trending = set(trending_add_ids)
    excluded = set(exclude_ids)

    clogs: list[RosterClog] = []
    for entry in roster.entries:
        pid = entry.player_id
        if pid in starters or pid in excluded or pid in trending:
            continue
        if entry.is_reserve or entry.is_taxi or entry.injury_status == "IR":
            continue
        if _is_dynasty_developmental(entry, currency) or not entry.value.is_corroborated:
            continue
        if entry.value.proj_points is None:
            continue  # no projection: "projects below everyone" would be a data gap, not a finding
        rank = composite_overall_rank(entry.value, currency)
        if rank is None or rank <= cutoff:
            continue

        # Everyone at his position who actually competes for a lineup slot
        # (taxi/reserve players don't), split into started vs not.
        competitors = [
            e for e in roster.by_position(entry.position or "")
            if e.player_id != pid and not e.is_taxi and not e.is_reserve
        ]
        started_here = [e for e in competitors if e.player_id in starters]
        backups = [e for e in competitors if e.player_id not in starters]
        if not started_here or not backups:
            continue  # nobody ahead of him, or he IS the only backup — that's depth, not a clog
        my_proj = projection_of(entry)
        primary_backup = max(backups, key=projection_of)
        if any(projection_of(e) <= my_proj for e in started_here) or projection_of(primary_backup) <= my_proj:
            continue

        pos = entry.position or "?"
        ahead = (
            f"the starting {pos} ({started_here[0].name})"
            if len(started_here) == 1
            else f"all {len(started_here)} starting {pos}s"
        )
        clogs.append(
            RosterClog(
                entry=entry,
                composite_rank=rank,
                reasons=[
                    f"outside the top {cutoff} by {rank_label} (~{rank:.0f})",
                    f"no path to the lineup — projects below {ahead} and the primary backup ({primary_backup.name})",
                ],
            )
        )

    clogs.sort(key=lambda c: -c.composite_rank)  # deepest-ranked (clearest cut) first
    return clogs[:max_clogs]
