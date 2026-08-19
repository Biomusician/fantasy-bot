"""Classifies my team in each league as a contender, middling, or a
recommended rebuild — used to bias trade strategy (age preference, which of
my own players are on the table) rather than just matching by raw value.

Inputs, blended:
  - Roster strength: a blend of my STARTERS' average value-percentile
    (85%, win-now signal) and my BENCH+TAXI's average value-percentile
    (15%, forward-looking signal — a starter-strong but bench-empty team
    reads as more win-now than one stockpiling depth for next year),
    ranked against the other rosters in the SAME league. Works from week
    one, including preseason.
  - Future draft capital (dynasty leagues only): the KTC value of picks I
    currently own (accounting for trades, via Sleeper's traded_picks),
    ranked the same way against the league. Weighted lightly (20%) since
    picks are a future asset, not this year's team.
  - Win/loss record: once enough games exist (>=5, ramping to full weight
    by game 9), record is blended in on top of the above and weighted more
    heavily, since actual results matter more than a pre-season value
    snapshot once they exist.
  - Playoff format: the CONTENDER/REBUILD thresholds shift with the
    league's playoff rate (playoff_teams / total_rosters) — in a league
    where most teams make the playoffs, a merely-average roster is a
    playoff team and should read as more contender-leaning; in a
    top-heavy league, it should read as more rebuild-leaning. A standard
    ~50% playoff rate produces no shift (same thresholds as before this
    was added).

This is a heuristic, documented rather than hidden: two teams a coin flip
apart in roster strength can land in different buckets, and the record
blend only kicks in after a small sample. It's meant to bias trade
strategy sensibly, not to be a precise power ranking.

Age thresholds are position-specific, not universal — dynasty consensus is
that RBs decline earliest, WRs/TEs peak later and decline more gradually,
and QBs have the longest shelf life (researched 2026-08-19 against
FantasyFootballBlueprint, RotoBaller, PFF, and Fantasy Footballers dynasty
aging-curve analysis):
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.draft_picks import OwnedPick, compute_owned_picks, relevant_seasons, value_owned_picks
from sleeper_tool.formatting import ordinal
from sleeper_tool.roster_analysis import ValuedRoster
from sleeper_tool.valuation import PlayerValue

CONTENDER_THRESHOLD = 62.0
REBUILD_THRESHOLD = 38.0
# A 3-game record in a 10-12 team league is mostly binomial noise (variance
# from one or two close games can flip a good roster's record), so instead
# of snapping straight to 65% weight at game 3, weight ramps in gradually
# starting at game 5 and reaches full weight around game 9 — full-season
# results still dominate the read, but only once there's enough sample to
# trust them over a single early-season snapshot of roster value.
MIN_GAMES_FOR_RECORD_BLEND = 5
RECORD_WEIGHT = 0.65  # weight once fully ramped in (see _record_weight_for_games)
RECORD_WEIGHT_RAMP_GAMES = 4  # games after MIN_GAMES_FOR_RECORD_BLEND to reach full RECORD_WEIGHT
PICK_CAPITAL_WEIGHT = 0.20  # vs 0.80 for current roster strength, dynasty leagues only
BENCH_STRENGTH_WEIGHT = 0.15  # vs 0.85 for starters, within the roster-strength component
PLAYOFF_RATE_SENSITIVITY = 20.0  # threshold shift (percentile points) per 1.0 deviation in playoff_rate from 0.5


def _record_weight_for_games(games: int) -> float:
    if games < MIN_GAMES_FOR_RECORD_BLEND:
        return 0.0
    ramp = min(1.0, (games - MIN_GAMES_FOR_RECORD_BLEND + 1) / RECORD_WEIGHT_RAMP_GAMES)
    return RECORD_WEIGHT * ramp


# (young_max_age, veteran_min_age) per position. Age <= young_max is a clear
# rebuild/middling buy target; age >= veteran_min is a proven win-now asset
# a contender should be comfortable acquiring. Ages between the two are a
# neutral zone (not filtered either way).
AGE_THRESHOLDS: dict[str, tuple[float, float]] = {
    "RB": (23.0, 27.0),  # earliest decline of any position, workload-driven
    "WR": (25.0, 29.0),  # peaks 25-28, declines gradually, WR1/2 odds hold until 30+
    "TE": (25.0, 29.0),  # late breakouts common, but non-elite TEs decline WR-like by 27-29
    "QB": (26.0, 32.0),  # longest shelf life; rushing value fades ~28-29 but arm talent lasts into late 30s
}
DEFAULT_AGE_THRESHOLD = (25.0, 29.0)  # fallback for K/DEF/unknown positions

CONTENDER = "contender"
MIDDLING = "middling"
REBUILD = "rebuild"


def young_max_age(position: str | None) -> float:
    return AGE_THRESHOLDS.get(position or "", DEFAULT_AGE_THRESHOLD)[0]


def veteran_min_age(position: str | None) -> float:
    return AGE_THRESHOLDS.get(position or "", DEFAULT_AGE_THRESHOLD)[1]


@dataclass
class TeamStatusResult:
    status: str
    strength_percentile: float
    win_pct: float | None
    games_played: int
    reason: str


def _percentile(pv: PlayerValue, currency: str) -> float | None:
    """Mirrors trade_engine._need_percentile without importing that module
    (trade_engine imports THIS module to bias trade strategy on team
    status, so importing back would be circular). Prefers the WITHIN-
    POSITION percentile for dynasty currency, for the same reason
    trade_engine's own docstring gives: pool-wide percentile makes a
    shallow position (TE) look weaker and a deep one (RB/WR) look stronger
    purely from pool-size, not real roster strength. Redraft currency has
    no positional percentile plumbed through yet (same known, smaller-
    impact gap trade_engine documents), so it falls back to pool-wide.
    """
    if currency == "dynasty" and pv.dynasty_positional_percentile is not None:
        return pv.dynasty_positional_percentile
    return pv.dynasty_value_percentile if currency == "dynasty" else pv.redraft_ecr_percentile


def _avg_percentile(entries, currency: str) -> float | None:
    pctls = [p for p in (_percentile(e.value, currency) for e in entries) if p is not None]
    return sum(pctls) / len(pctls) if pctls else None


def _roster_strength(roster: ValuedRoster, currency: str) -> float:
    starters = roster.starters() or roster.entries
    starter_pctl = _avg_percentile(starters, currency)
    if starter_pctl is None:
        return 0.0

    bench_pool = roster.bench() + [e for e in roster.entries if e.is_taxi]
    bench_pctl = _avg_percentile(bench_pool, currency)
    if bench_pctl is None:
        return starter_pctl
    return (1 - BENCH_STRENGTH_WEIGHT) * starter_pctl + BENCH_STRENGTH_WEIGHT * bench_pctl


def _rank_percentile(values: dict[int, float], target_id: int) -> float:
    ranked = sorted(values.values())
    my_value = values[target_id]
    n = len(ranked)
    return (100.0 * sum(1 for v in ranked if v <= my_value) / n) if n else 50.0


def get_valued_picks_by_roster(
    rosters: dict[int, ValuedRoster], currency: str, storage=None, engine=None
) -> dict[int, list[OwnedPick]] | None:
    """Every roster's currently-owned future draft picks, valued via KTC —
    or None if this league doesn't have usable draft-pick data (redraft
    format, no draft_rounds/season on record, etc — checked explicitly, not
    by swallowing exceptions, since those are legitimate "doesn't apply"
    cases rather than bugs). Dynasty leagues only: redraft leagues re-draft
    everyone annually (no persistent future picks) and the keeper league's
    picks don't map onto KTC's dynasty pick-value tiers the same way.

    Shared by classify_team_status (for scoring) and the trade engine (so
    picks can actually be offered/targeted as trade chips, not just used
    to bias a percentile).
    """
    if currency != "dynasty" or storage is None or engine is None:
        return None

    any_roster = next(iter(rosters.values()))
    league_id = any_roster.league.league_id
    league_data = storage.get_league(league_id)
    if league_data is None:
        return None
    settings = league_data.get("settings") or {}
    draft_rounds = settings.get("draft_rounds")
    season = league_data.get("season")
    if not draft_rounds or not season:
        return None

    strengths = {rid: _roster_strength(r, currency) for rid, r in rosters.items()}
    traded_picks = storage.get_traded_picks(league_id)
    owned = compute_owned_picks(
        roster_ids=list(rosters.keys()),
        traded_picks=traded_picks,
        draft_rounds=int(draft_rounds),
        seasons=relevant_seasons(season),
        strength_by_roster=strengths,
    )
    return value_owned_picks(owned, engine.ktc_snapshot, is_superflex=any_roster.fmt.is_superflex)


def _playoff_threshold_shift(rosters: dict[int, ValuedRoster], storage) -> float:
    """How much to shift CONTENDER/REBUILD thresholds based on this
    league's playoff rate. Returns 0 (no shift) if playoff format data
    isn't available — a standard ~50% playoff rate also produces ~0.
    """
    if storage is None:
        return 0.0
    any_roster = next(iter(rosters.values()))
    league_data = storage.get_league(any_roster.league.league_id)
    if league_data is None:
        return 0.0
    total_rosters = league_data.get("total_rosters")
    playoff_teams = (league_data.get("settings") or {}).get("playoff_teams")
    if not total_rosters or not playoff_teams:
        return 0.0
    playoff_rate = playoff_teams / total_rosters
    return (playoff_rate - 0.5) * PLAYOFF_RATE_SENSITIVITY


def classify_team_status(
    target_roster_id: int,
    rosters: dict[int, ValuedRoster],
    currency: str,
    *,
    storage=None,
    engine=None,
) -> TeamStatusResult:
    target = rosters[target_roster_id]

    strengths = {rid: _roster_strength(r, currency) for rid, r in rosters.items()}
    strength_pctl = _rank_percentile(strengths, target_roster_id)

    score = strength_pctl
    pick_note = ""
    valued_picks = get_valued_picks_by_roster(rosters, currency, storage, engine)
    if valued_picks is not None:
        pick_values = {rid: sum(p.value or 0 for p in picks) for rid, picks in valued_picks.items()}
        pick_pctl = _rank_percentile({rid: float(v) for rid, v in pick_values.items()}, target_roster_id)
        score = (1 - PICK_CAPITAL_WEIGHT) * strength_pctl + PICK_CAPITAL_WEIGHT * pick_pctl
        pick_note = f", {ordinal(round(pick_pctl))} percentile draft capital ({pick_values[target_roster_id]:,} KTC pick value owned)"

    win_pct = None
    games = target.games_played
    if games >= MIN_GAMES_FOR_RECORD_BLEND:
        win_pct = (target.wins + 0.5 * target.ties) / games
        record_weight = _record_weight_for_games(games)
        score = record_weight * (win_pct * 100) + (1 - record_weight) * score

    shift = _playoff_threshold_shift(rosters, storage)
    contender_threshold = CONTENDER_THRESHOLD - shift
    rebuild_threshold = REBUILD_THRESHOLD - shift
    playoff_note = ""
    if shift:
        playoff_note = f"; thresholds shifted {shift:+.0f} for this league's playoff rate"

    if score >= contender_threshold:
        status = CONTENDER
    elif score <= rebuild_threshold:
        status = REBUILD
    else:
        status = MIDDLING

    if win_pct is not None:
        reason = (
            f"{target.wins}-{target.losses}"
            f"{'-' + str(target.ties) if target.ties else ''} record "
            f"({win_pct*100:.0f}% win rate) blended with {ordinal(round(strength_pctl))} percentile roster strength"
            f"{pick_note}{playoff_note}"
        )
    else:
        reason = f"{ordinal(round(strength_pctl))} percentile roster strength in-league{pick_note} (no games played yet){playoff_note}"

    return TeamStatusResult(
        status=status, strength_percentile=strength_pctl, win_pct=win_pct, games_played=games, reason=reason
    )
