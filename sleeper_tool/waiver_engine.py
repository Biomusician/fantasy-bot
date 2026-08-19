"""Trending-add / free-agent waiver recommendations, cross-referenced
against my roster's positional needs.

Every recommendation pairs an add with a specific drop candidate, a
priority tier (how urgent), and a horizon (how long you'd expect to hold
the player) — a bare "add this player" name is not actionable on a full
roster, which every one of this tool's leagues effectively has by mid-
season.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.config import LeagueInfo
from sleeper_tool.formatting import ordinal
from sleeper_tool.roster_analysis import SKILL_POSITIONS, RosterEntry, ValuedRoster, player_name
from sleeper_tool.storage import Storage
from sleeper_tool.trade_engine import (
    DYNASTY_CURRENCY,
    MIN_ROSTERABLE_PERCENTILE,
    identify_needs,
    percentile_for_currency,
    value_currency,
    value_label_for_currency,
)
from sleeper_tool.valuation import PlayerValue, ValuationEngine

EARLY_SEASON_WEEK_CUTOFF = 4  # below this week, trending-adds are hype-driven more than usage-driven

# -- Priority tiers: how urgent is this add ----------------------------------
MUST_ADD = "Must Add"
STRONG_ADD = "Strong Add"
MODERATE = "Moderate"
SPECULATIVE = "Speculative"
MONITOR = "Monitor"

# -- Horizon: how long you'd expect to hold the player -----------------------
BREAKOUT = "Breakout"  # young, trending up -- worth a long-term dynasty stash
SEASON_STARTER = "Season Starter"  # fills a real need at a rosterable-or-better percentile -- expect to hold, not churn
STASH = "Stash"  # rosterable, dynasty-relevant, but not an immediate need
STREAMER = "Streamer"  # this-week/short-term only
BREAKOUT_YEARS_EXP_THRESHOLD = 2  # years_exp <= this counts as a true breakout candidate, not just "young-ish"
STASH_MIN_PERCENTILE = 40.0
SEASON_STARTER_MIN_PERCENTILE = 60.0

TOP_TREND_RANK_CUTOFF = 15  # top-N-of-fetched-batch counts as "real buzz" for the Speculative tier -- rank, not a raw
# count, since Sleeper's absolute add-counts drift week to week with the overall news cycle (a slow week's #1 trending
# player might have 30 adds; a big-news week's #1 might have 400) -- rank within the batch stays comparable across weeks.

# Suggested FAAB bid as (low, high) % of TOTAL season budget, before scaling
# down for how much budget is already spent. A heuristic bucketed by
# priority, not a market-clearing prediction.
_FAAB_PCT_BY_TIER = {MUST_ADD: (15, 35), STRONG_ADD: (8, 20), MODERATE: (3, 10), SPECULATIVE: (1, 5), MONITOR: (0, 2)}


@dataclass
class WaiverTarget:
    player_id: str
    name: str
    position: str | None
    team: str | None
    trend_count: int
    value: PlayerValue
    fills_need: bool
    need_rank: int | None  # index into identify_needs' worst-to-best list (0 = my single worst position); None if not a need
    reason: str
    priority_tier: str = MODERATE
    horizon: str = STREAMER
    drop_candidate: RosterEntry | None = None
    suggested_faab_pct: int | None = None  # None when the league isn't FAAB (no waiver_budget data)


def get_rostered_player_ids(storage: Storage, league: LeagueInfo) -> set[str]:
    rostered: set[str] = set()
    for roster in storage.get_rosters(league.league_id):
        rostered.update(roster.get("players") or [])
    return rostered


def _display_percentile(value: PlayerValue, currency: str) -> float | None:
    """WITHIN-POSITION percentile for dynasty currency when available —
    the same metric identify_needs itself uses to decide a position is a
    need (trade_engine._need_percentile), so the number shown/sorted-by
    here doesn't contradict the reasoning that surfaced this target in the
    first place. Falls back to the pool-wide percentile otherwise (same
    known gap as the rest of the codebase for redraft currency).
    """
    if currency == DYNASTY_CURRENCY and value.dynasty_positional_percentile is not None:
        return value.dynasty_positional_percentile
    return percentile_for_currency(value, currency)


def _find_drop_candidate(
    my_roster: ValuedRoster,
    target_position: str | None,
    my_needs: list[str],
    currency: str,
    *,
    exclude_ids: set[str] = frozenset(),
) -> RosterEntry | None:
    """The cheapest bench player to cut to make room for this add.

    Same-position bench players are considered FIRST, regardless of
    whether target_position is itself one of my declared needs — cutting
    my weakest bench player at a position to make room for a stronger add
    there is exactly the normal case (most adds target a need position by
    definition), not something to avoid. The need-avoidance filter only
    applies as a fallback when there's no bench player at target_position
    at all, to protect a position OTHER than the one being upgraded.

    `exclude_ids` lets callers dedup across multiple simultaneous waiver
    suggestions in the same table — without it, several different "Add"
    rows can all independently recommend cutting the single weakest bench
    player, which isn't actionable if a user tries to follow more than one
    of them in the same week.

    Never suggests cutting a player who's currently trending up himself,
    and ranks a player with no valuation data at all (pctl is None) AFTER
    one with a real, if low, percentile — a data gap isn't the same thing
    as "genuinely the worst player," and shouldn't look identical to it.
    """
    pool = [e for e in my_roster.bench() if e.value.trend != "rising" and e.player_id not in exclude_ids]
    if not pool:
        return None

    def _sort_key(e: RosterEntry) -> tuple:
        pctl = _display_percentile(e.value, currency)
        return (pctl is None, pctl or 0)

    same_position = [e for e in pool if e.position == target_position]
    if same_position:
        return min(same_position, key=_sort_key)
    non_need_pool = [e for e in pool if e.position not in my_needs] or pool
    return min(non_need_pool, key=_sort_key)


def _priority_tier(fills_need: bool, pctl: float | None, trend_rank: int) -> str:
    p = pctl or 0
    if fills_need and p >= 70:
        return MUST_ADD
    if (fills_need and p >= 50) or (not fills_need and p >= 80):
        return STRONG_ADD
    if fills_need or p >= MIN_ROSTERABLE_PERCENTILE:
        return MODERATE
    if trend_rank < TOP_TREND_RANK_CUTOFF:
        return SPECULATIVE
    return MONITOR


def _horizon(value: PlayerValue, years_exp: int | None, currency: str, fills_need: bool, pctl: float | None) -> str:
    if value.trend == "rising" and years_exp is not None and years_exp <= BREAKOUT_YEARS_EXP_THRESHOLD:
        return BREAKOUT
    # A need-filling, rosterable-or-better add is a real hold, not a
    # this-week-only churn play — regardless of currency. Previously this
    # fell through to STREAMER for every add that wasn't a young breakout,
    # including redraft/dynasty players tiered MUST_ADD/STRONG_ADD, which
    # directly contradicted STREAMER's own "this-week/short-term only"
    # definition on the report's most urgent recommendations.
    if fills_need and (pctl or 0) >= SEASON_STARTER_MIN_PERCENTILE:
        return SEASON_STARTER
    if currency == DYNASTY_CURRENCY and not fills_need and (pctl or 0) >= STASH_MIN_PERCENTILE:
        return STASH
    return STREAMER


def _suggested_faab_pct(tier: str, waiver_budget: int | None, waiver_budget_used: int) -> int | None:
    """None when the league isn't a FAAB league at all (no waiver_budget
    setting) — silently applying a FAAB % to a priority-waiver league
    would be actively wrong, not just imprecise, so this is scoped
    strictly to leagues where the underlying setting is actually present.
    """
    if not waiver_budget:
        return None
    remaining_pct = max(0, 100 - round(100 * waiver_budget_used / waiver_budget))
    if remaining_pct <= 0:
        return 0
    lo, hi = _FAAB_PCT_BY_TIER.get(tier, (0, 2))
    pct = hi if remaining_pct >= 50 else round((lo + hi) / 2)
    return min(pct, remaining_pct)


def get_waiver_targets(
    storage: Storage,
    engine: ValuationEngine,
    league: LeagueInfo,
    my_roster: ValuedRoster,
    *,
    top_n: int = 8,
    current_week: int | None = None,
    waiver_budget: int | None = None,
) -> list[WaiverTarget]:
    if not my_roster.entries:
        # No roster yet usually means the league hasn't drafted (redraft
        # leagues start empty) — nothing meaningful to recommend yet.
        return []

    all_players = storage.get_all_players()
    rostered_ids = get_rostered_player_ids(storage, league)
    trending = storage.get_trending("add")
    needs_ranked = identify_needs(my_roster)
    currency = value_currency(my_roster)

    targets: list[WaiverTarget] = []
    recommended_drop_ids: set[str] = set()
    for trend_rank, row in enumerate(trending):
        pid = row["player_id"]
        if pid in rostered_ids:
            continue  # already on a roster in this league — not a valid waiver target here
        pdata = all_players.get(pid)
        if not pdata or pdata.get("position") not in SKILL_POSITIONS:
            continue
        # Sleeper's trending list can include players who are inactive/retired
        # league-wide; a NULL team means they're not currently on an NFL roster.
        if not pdata.get("team"):
            continue

        name = player_name(pdata)
        position = pdata.get("position")
        value = engine.value_player(name, my_roster.fmt, position)
        need_rank = needs_ranked.index(position) if position in needs_ranked else None
        # < 2, matching trade_engine.generate_trade_proposals' own
        # my_needs[:2] definition of "a real need" -- POSITION_ORDER has
        # only 4 positions, so < 3 (the original threshold) covered 3 of
        # them, making "fills_need" true for nearly anything except a
        # user's single strongest position. Keeping the two definitions
        # in sync also stops the waiver and trade paths from silently
        # disagreeing about what counts as a need.
        fills_need = need_rank is not None and need_rank < 2

        # Sleeper's trending endpoint is platform-wide (all leagues, not just
        # this one) — there's no per-league trending data available via the API.
        reason_bits = [f"{row.get('count', 0)} adds across Sleeper in the last 48h"]
        if current_week is not None and current_week < EARLY_SEASON_WEEK_CUTOFF:
            # Early-season trending is hype/name-recognition driven more than
            # usage-driven — there just isn't enough game data yet for adds
            # to reflect real opportunity share the way they will by week 4+.
            reason_bits.append("small early-season sample, treat as hype risk")
        if fills_need:
            reason_bits.append(f"fills your {ordinal(need_rank + 1)}-worst need at {position}")
        if current_week is not None and value.bye_week == current_week:
            reason_bits.append(f"on bye week {current_week} — add for future weeks, not an immediate starter")
        pctl = _display_percentile(value, currency)
        if pctl is not None:
            reason_bits.append(f"{ordinal(round(pctl))} percentile {value_label_for_currency(currency)}")

        tier = _priority_tier(fills_need, pctl, trend_rank)
        horizon = _horizon(value, pdata.get("years_exp"), currency, fills_need, pctl)
        drop_candidate = _find_drop_candidate(
            my_roster, position, needs_ranked[:2], currency, exclude_ids=recommended_drop_ids
        )
        if drop_candidate is not None:
            recommended_drop_ids.add(drop_candidate.player_id)
        faab_pct = _suggested_faab_pct(tier, waiver_budget, my_roster.waiver_budget_used)

        targets.append(
            WaiverTarget(
                player_id=pid,
                name=name,
                position=position,
                team=pdata.get("team"),
                trend_count=row.get("count", 0),
                value=value,
                fills_need=fills_need,
                need_rank=need_rank,
                reason="; ".join(reason_bits),
                priority_tier=tier,
                horizon=horizon,
                drop_candidate=drop_candidate,
                suggested_faab_pct=faab_pct,
            )
        )

    _TIER_ORDER = {MUST_ADD: 0, STRONG_ADD: 1, MODERATE: 2, SPECULATIVE: 3, MONITOR: 4}
    targets.sort(key=lambda t: (_TIER_ORDER.get(t.priority_tier, 9), -(_display_percentile(t.value, currency) or 0)))
    return targets[:top_n]


@dataclass
class TimeSensitiveNote:
    player_name: str
    note: str
    severity: str = "medium"  # "high" | "medium" | "low" — drives UI treatment, not derived from note text


_HIGH_SEVERITY_INJURY_STATUSES = {"Out", "IR", "Doubtful", "PUP", "Suspended"}
_MEDIUM_SEVERITY_INJURY_STATUSES = {"Questionable"}


def _injury_severity(injury_status: str | None) -> str:
    if injury_status in _HIGH_SEVERITY_INJURY_STATUSES:
        return "high"
    if injury_status in _MEDIUM_SEVERITY_INJURY_STATUSES:
        return "medium"
    return "low"


def get_time_sensitive_notes(
    storage: Storage, my_roster: ValuedRoster, *, current_week: int | None = None
) -> list[TimeSensitiveNote]:
    """Injury/inactive/bye-week flags for my own roster — the "anything
    time-sensitive" part of the weekly report. Bye week comes from
    FantasyPros/RotoBaller (via PlayerValue.bye_week) since Sleeper's player
    objects don't carry it at all. `severity` is derived from the
    structured injury_status/roster-status value, not by pattern-matching
    the human-readable note text — an Out/IR player and a Questionable one
    read identically in the note string but must not read identically to
    the UI.
    """
    notes: list[TimeSensitiveNote] = []
    for entry in my_roster.entries:
        if entry.injury_status and entry.injury_status not in ("Healthy", None):
            notes.append(
                TimeSensitiveNote(entry.name, f"Injury status: {entry.injury_status}", severity=_injury_severity(entry.injury_status))
            )
        if entry.status and entry.status not in ("Active", "Inactive"):
            # "Inactive" alone is common/benign (e.g. practice squad); flag anything unusual instead.
            notes.append(TimeSensitiveNote(entry.name, f"Roster status: {entry.status}", severity="low"))
        if current_week is not None and entry.value.bye_week == current_week and entry.is_starter:
            notes.append(
                TimeSensitiveNote(entry.name, f"On bye week {current_week} — starting slot needs a fill-in", severity="medium")
            )
    return notes
