"""Determines who currently owns which future rookie draft picks in a
dynasty league, and values them using KTC's own pick valuations (KTC prices
picks the same way it prices players — 0-9999 scale, position "RDP" — for
rounds 1-4 and three years out, in Early/Mid/Late tiers per round).

Dynasty-only: redraft leagues re-draft everyone every year (no persistent
future picks), and the keeper league's picks don't map onto KTC's dynasty
pick-value tiers the same way, so this module is only wired into
team_status.py for currency == "dynasty".

Ownership: Sleeper's traded_picks endpoint only lists picks that have
changed hands — a pick not in that list is still held by the team it
originally belonged to. Tier (Early/Mid/Late) isn't known until the season
plays out, so we estimate it from the ORIGINAL team's current roster
strength percentile (a weak team's own future 1st is more likely to land
early in the round, and vice versa) — an approximation, not a guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.name_matching import normalize_name
from sleeper_tool.rankings.ktc import index_by_name
from sleeper_tool.rankings.cache import RankingSnapshot

ROUND_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
FUTURE_SEASONS_AHEAD = 2  # current season + this many future draft classes
EARLY_THRESHOLD = 33.0
LATE_THRESHOLD = 67.0


@dataclass(frozen=True)
class OwnedPick:
    season: str
    round: int
    original_roster_id: int
    tier: str  # Early | Mid | Late (estimated)
    name: str
    value: int | None


def pick_key(pick: OwnedPick) -> tuple:
    """Identity of a pick for de-duplication across proposals. Deliberately
    NOT the tier or display name: the tier is an estimate that moves as the
    season plays out, so two reads of the same pick can disagree on it while
    still being the same asset.
    """
    return (pick.season, pick.round, pick.original_roster_id)


def estimate_tier(original_team_strength_percentile: float) -> str:
    """A weak team (low percentile) is more likely to pick early in the
    round; a strong team, late. Percentile here is 0-100, 100 = strongest.
    """
    if original_team_strength_percentile <= EARLY_THRESHOLD:
        return "Early"
    if original_team_strength_percentile >= LATE_THRESHOLD:
        return "Late"
    return "Mid"


def pick_display_name(season: str, round_num: int, tier: str) -> str:
    ordinal = ROUND_ORDINAL.get(round_num, f"{round_num}th")
    return f"{season} {tier} {ordinal}"


def relevant_seasons(current_season: str) -> list[str]:
    start = int(current_season)
    return [str(start + i) for i in range(FUTURE_SEASONS_AHEAD + 1)]


def compute_owned_picks(
    *,
    roster_ids: list[int],
    traded_picks: list[dict],
    draft_rounds: int,
    seasons: list[str],
    strength_by_roster: dict[int, float],
) -> dict[int, list[OwnedPick]]:
    """Returns {current_owner_roster_id: [OwnedPick, ...]}. `strength_by_roster`
    is each roster's average-starter dynasty percentile (0-100), used only
    to estimate pick tier for the ORIGINAL owning team.
    """
    # Index traded picks by (round, season, original_roster_id) -> current owner roster_id.
    traded_index: dict[tuple[int, str, int], int] = {}
    for tp in traded_picks:
        key = (tp["round"], tp["season"], tp["roster_id"])
        traded_index[key] = tp["owner_id"]

    owned: dict[int, list[OwnedPick]] = {rid: [] for rid in roster_ids}
    for original_rid in roster_ids:
        for season in seasons:
            for round_num in range(1, draft_rounds + 1):
                current_owner = traded_index.get((round_num, season, original_rid), original_rid)
                if current_owner not in owned:
                    # Pick traded to a roster_id not in our current roster set
                    # (e.g. co-owner bookkeeping quirk) — skip rather than guess.
                    continue
                strength = strength_by_roster.get(original_rid, 50.0)
                tier = estimate_tier(strength)
                name = pick_display_name(season, round_num, tier)
                owned[current_owner].append(
                    OwnedPick(season=season, round=round_num, original_roster_id=original_rid, tier=tier, name=name, value=None)
                )
    return owned


def value_owned_picks(
    owned: dict[int, list[OwnedPick]], ktc_snapshot: RankingSnapshot, *, is_superflex: bool
) -> dict[int, list[OwnedPick]]:
    """Fills in `.value` for each pick from KTC's RDP valuations. Picks KTC
    doesn't price (round 5+, or years beyond its ~3-year window) are kept
    with value=None rather than guessed at.
    """
    pick_index = index_by_name(ktc_snapshot)
    side = "superflex" if is_superflex else "one_qb"

    result: dict[int, list[OwnedPick]] = {}
    for owner_rid, picks in owned.items():
        valued = []
        for p in picks:
            match = pick_index.get(normalize_name(p.name))
            value = match[side]["value"] if match else None
            valued.append(OwnedPick(season=p.season, round=p.round, original_roster_id=p.original_roster_id, tier=p.tier, name=p.name, value=value))
        result[owner_rid] = valued
    return result
