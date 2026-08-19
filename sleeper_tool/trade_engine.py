"""Buy-low/sell-high identification and trade proposal generation.

Works across dynasty, keeper, and redraft leagues by picking a different
"value currency" per league, since dynasty trade value is close to
meaningless for a league you're not keeping players in:

- Dynasty leagues: KTC dynasty value (`PlayerValue.dynasty_value`), the
  long-horizon market-value signal.
- Keeper and redraft leagues: RotoBaller's format-matched season point
  projection (`PlayerValue.proj_points`) — most of a keeper roster resets
  every year too (Primo Veterans only carries 3 keepers), so treating it
  like dynasty would overweight long-term asset value that mostly doesn't
  carry over. This is a deliberate simplification, not an oversight.

Approach, explicitly documented since it's a heuristic, not a black box:

- "Sell-high" = a corroborated, valuable player whose RotoBaller trend is
  "rising" — the market/recent performance is currently ahead of what the
  longer-horizon value signal (dynasty leagues) or a flat rank (redraft)
  would suggest, i.e. now's a good time to sell.
- "Buy-low" = a corroborated player still inside a rosterable percentile
  (>20th) whose trend is "down" — recent performance has dipped but their
  underlying value profile hasn't collapsed, meaning their owner may
  undervalue them right now.
- "Need" = a skill position where my best player's percentile (in this
  league's currency) is weaker than my other positions.

Every proposal only uses corroborated players (>=2 independent ranking
sources agreeing, per ValuationEngine.is_corroborated) specifically because
a single-source valuation is more likely to be a name-matching artifact
than a real signal — recommending a trade off of one shaky data point would
be irresponsible. Proposals also surface cross-source agreement and the
TE-premium caveat when relevant, rather than presenting a bare number as
ground truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from sleeper_tool.config import LeagueInfo, MY_USER_ID
from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.formatting import ordinal_pct
from sleeper_tool.owner_profiles import get_owner_profile
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.team_status import (
    CONTENDER,
    MIDDLING,
    REBUILD,
    TeamStatusResult,
    classify_team_status,
    get_valued_picks_by_roster,
    veteran_min_age,
    young_max_age,
)
from sleeper_tool.valuation import PlayerValue

SELL_HIGH_TREND = "rising"
BUY_LOW_TREND = "down"
MIN_ROSTERABLE_PERCENTILE = 20.0
VALUE_TOLERANCE = 0.20  # accept offers where value ratio is within +/-20%
UNTOUCHABLE_COUNT = 2  # skip each roster's top N players by value as trade targets/gives
ELITE_ASSET_PERCENTILE = 90.0  # bypass age filtering for a clear top-tier asset regardless of team timeline
DECLINE_CONFIRMATION_GAP = 10.0  # dynasty_pctl - redraft_pctl must clear this to call a dip a buy-low, not a real decline
POSITION_ORDER = ("QB", "RB", "WR", "TE")  # fixed order so need-ranking ties break deterministically

DYNASTY_CURRENCY = "dynasty"
REDRAFT_CURRENCY = "redraft"


def value_currency(roster: ValuedRoster) -> str:
    """Which value signal this league's trades should be evaluated on."""
    return DYNASTY_CURRENCY if roster.league.kind == "dynasty" else REDRAFT_CURRENCY


def value_for_currency(pv: PlayerValue, currency: str) -> float | None:
    return pv.dynasty_value if currency == DYNASTY_CURRENCY else pv.proj_points


def percentile_for_currency(pv: PlayerValue, currency: str) -> float | None:
    return pv.dynasty_value_percentile if currency == DYNASTY_CURRENCY else pv.redraft_ecr_percentile


def value_label_for_currency(currency: str) -> str:
    return "dynasty value" if currency == DYNASTY_CURRENCY else "projected season points"


@dataclass
class TradeProposal:
    league_name: str
    currency: str
    target_username: str
    target_team_name: str | None
    give: list[RosterEntry]
    receive: list[RosterEntry]
    my_value_total: float
    their_value_total: float
    rationale_for_me: list[str]
    rationale_for_them: list[str]
    caveats: list[str]
    give_picks: list[OwnedPick] = field(default_factory=list)
    receive_picks: list[OwnedPick] = field(default_factory=list)

    @property
    def value_ratio(self) -> float:
        """>1 means I'm giving up more value than I get (rare, unfavorable)."""
        if self.their_value_total == 0:
            return float("inf")
        return self.my_value_total / self.their_value_total

    def summary_line(self) -> str:
        give_names = ", ".join([*(e.name for e in self.give), *(p.name for p in self.give_picks)])
        receive_names = ", ".join([*(e.name for e in self.receive), *(p.name for p in self.receive_picks)])
        return f"Send {give_names} to {self.target_team_name or self.target_username} for {receive_names}"


def _corroborated(entry: RosterEntry, currency: str) -> bool:
    return entry.value.is_corroborated and value_for_currency(entry.value, currency) is not None


def identify_sell_high(roster: ValuedRoster) -> list[RosterEntry]:
    currency = value_currency(roster)
    return sorted(
        (e for e in roster.entries if _corroborated(e, currency) and e.value.trend == SELL_HIGH_TREND),
        key=lambda e: -(value_for_currency(e.value, currency) or 0),
    )


def identify_buy_low(
    roster: ValuedRoster, my_status: str = CONTENDER, exclude_top: int = UNTOUCHABLE_COUNT
) -> list[RosterEntry]:
    """Buy-low candidates on someone else's roster. `my_status` is MY team's
    status (contender/middling/rebuild), not theirs — a middling or
    rebuilding team should be targeting younger players specifically, while
    a contender can target any age as long as the value/need fits.
    """
    currency = value_currency(roster)
    ranked = sorted(
        (e for e in roster.entries if _corroborated(e, currency)), key=lambda e: -(value_for_currency(e.value, currency) or 0)
    )
    untouchable_ids = {e.player_id for e in ranked[:exclude_top]}

    def _age_ok(e: RosterEntry) -> bool:
        if my_status == CONTENDER:
            return True
        pctl = percentile_for_currency(e.value, currency) or 0
        if pctl >= ELITE_ASSET_PERCENTILE:
            # A clear top-tier asset is worth targeting regardless of
            # timeline — age filtering shouldn't cost a rebuild a true
            # bell-cow RB or WR1-caliber player just outside the cutoff.
            return True
        # Unknown age isn't penalized — we simply don't have the data to filter on.
        return e.age is None or e.age <= young_max_age(e.position)

    def _not_just_a_slump(e: RosterEntry) -> bool:
        """"trend == down" alone can't tell a market overreaction (buy the
        dip) from a real decline (lost job, aged out) — a genuine decline
        usually shows up in BOTH the long-horizon dynasty signal and the
        short-horizon redraft signal, while a real buy-low dip is short-
        horizon only (dynasty value holding, redraft/current-form down).
        Only applied for dynasty currency, where that long/short split
        actually exists; skipped if either percentile is unavailable.
        """
        if currency != DYNASTY_CURRENCY:
            return True
        dyn_pctl, rd_pctl = e.value.dynasty_value_percentile, e.value.redraft_ecr_percentile
        if dyn_pctl is None or rd_pctl is None:
            return True
        return (dyn_pctl - rd_pctl) >= DECLINE_CONFIRMATION_GAP

    return sorted(
        (
            e
            for e in ranked
            if e.player_id not in untouchable_ids
            and e.value.trend == BUY_LOW_TREND
            and (percentile_for_currency(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE
            and _age_ok(e)
            and _not_just_a_slump(e)
        ),
        key=lambda e: -(value_for_currency(e.value, currency) or 0),
    )


def _need_percentile(pv, currency: str) -> float | None:
    """Percentile used specifically for cross-position need comparison.

    Comparing positions by overall-pool percentile is an apples-to-oranges
    mistake — "70th percentile among all dynasty assets" means something
    very different at a shallow position (TE) than a deep one (RB/WR), so a
    team can look TE-needy or RB-loaded purely as an artifact of pool size,
    not real scarcity. For dynasty currency we use KTC's WITHIN-POSITION
    percentile instead. Redraft currency doesn't have a positional
    percentile plumbed through yet, so it falls back to the overall
    percentile — a known, smaller-impact limitation (redraft leagues here
    are 1 keeper league + several not-yet-drafted redraft leagues).
    """
    if currency == DYNASTY_CURRENCY and pv.dynasty_positional_percentile is not None:
        return pv.dynasty_positional_percentile
    return percentile_for_currency(pv, currency)


def identify_needs(roster: ValuedRoster) -> list[str]:
    """Positions where my best asset is weaker than my other positions,
    ranked worst-first. Uses whichever single player I have with the
    highest within-position percentile at each position as that position's
    strength (see _need_percentile for why within-position, not pool-wide).
    """
    currency = value_currency(roster)
    best_by_position: dict[str, float] = {}
    for pos in POSITION_ORDER:
        entries = [e for e in roster.by_position(pos) if _need_percentile(e.value, currency) is not None]
        best_by_position[pos] = max((_need_percentile(e.value, currency) for e in entries), default=0.0)
    return sorted(POSITION_ORDER, key=lambda p: best_by_position[p])


def _tradeable_pool(
    roster: ValuedRoster, my_status: str = CONTENDER, exclude_top: int = UNTOUCHABLE_COUNT
) -> list[RosterEntry]:
    """My own tradeable assets (the 'give' side). Untouchables (my top N by
    value) are never on the table. For a middling/rebuild team, aging
    veterans are sorted first so value-matching prefers shipping them out
    over young roster pieces when there's a choice — the goal is shedding
    win-now veterans for future value, not the reverse.
    """
    currency = value_currency(roster)
    untouchable_ids = {
        e.player_id
        for e in sorted(
            (e for e in roster.entries if _corroborated(e, currency)),
            key=lambda e: -(value_for_currency(e.value, currency) or 0),
        )[:exclude_top]
    }
    pool = [e for e in roster.entries if _corroborated(e, currency) and e.player_id not in untouchable_ids]

    if my_status in (MIDDLING, REBUILD):
        return sorted(
            pool,
            key=lambda e: (
                not (e.age is not None and e.age >= veteran_min_age(e.position)),
                -(value_for_currency(e.value, currency) or 0),
            ),
        )
    return sorted(pool, key=lambda e: -(value_for_currency(e.value, currency) or 0))


def _find_matching_offer(
    my_players: list[RosterEntry],
    my_picks: list[OwnedPick],
    target_value: float,
    currency: str,
    *,
    max_pieces: int = 2,
) -> tuple[list[RosterEntry], list[OwnedPick]] | None:
    """Finds the smallest combination (1 or 2 assets — players and/or, for
    dynasty currency, owned draft picks) of my tradeable pool whose
    combined value falls within VALUE_TOLERANCE of target_value. Prefers
    single-asset matches (cleaner offers) over multi-piece ones.
    """
    if not target_value:
        return None
    pool: list[tuple[str, RosterEntry | OwnedPick, float]] = [
        ("player", e, value_for_currency(e.value, currency) or 0) for e in my_players
    ]
    if currency == DYNASTY_CURRENCY:
        pool += [("pick", p, p.value or 0) for p in my_picks if p.value]

    best: tuple[tuple[str, RosterEntry | OwnedPick, float], ...] | None = None
    best_diff = float("inf")
    for size in range(1, max_pieces + 1):
        for combo in combinations(pool, size):
            total = sum(item[2] for item in combo)
            if total == 0:
                continue
            diff = abs(total - target_value) / target_value
            if diff <= VALUE_TOLERANCE and diff < best_diff:
                best, best_diff = combo, diff
        if best is not None:
            break  # prefer the smallest size that produced any match
    if best is None:
        return None
    players = [item[1] for item in best if item[0] == "player"]
    picks = [item[1] for item in best if item[0] == "pick"]
    return players, picks


ROOKIE_YEARS_EXP_THRESHOLD = 1  # years_exp <= this counts as "still hype-driven" for disagreement context


def _rookie_context_suffix(entry: RosterEntry) -> str:
    """Appended to an EXISTING disagreement caveat (cross-source or FantasyPros
    within-panel) when the disagreeing player is a rookie/2nd-year — not a
    standalone flag. A blanket "this is a young player" caveat on every rookie
    degenerates into boilerplate nobody reads; disagreement + youth together is
    a much sharper signal (crowd/expert hype cycles hit unproven players
    hardest, so a real split of opinion on a rookie is more likely to still be
    live than the same split on a proven veteran).
    """
    if entry.years_exp is not None and entry.years_exp <= ROOKIE_YEARS_EXP_THRESHOLD:
        return " Also a first/second-year player — dynasty value at this experience level is historically the most volatile, so weigh this disagreement extra heavily."
    return ""


def _age_note(entry: RosterEntry, my_status: str) -> str | None:
    if entry.age is None:
        return None
    if my_status in (MIDDLING, REBUILD) and entry.age <= young_max_age(entry.position):
        return f"age {entry.age:g} fits your team's youth priority for a {entry.position or 'skill'} player"
    if my_status == CONTENDER and entry.age >= veteran_min_age(entry.position):
        return f"age {entry.age:g} — a proven {entry.position or ''} veteran, fine for a win-now contender"
    return None


def _build_rationale(
    target_entry: RosterEntry,
    give: list[RosterEntry],
    currency: str,
    profile,
    status_result: TeamStatusResult,
    give_picks: list[OwnedPick] = (),
) -> tuple[list[str], list[str], list[str]]:
    mine: list[str] = []
    theirs: list[str] = []
    caveats: list[str] = []
    label = value_label_for_currency(currency)
    my_status = status_result.status

    status_labels = {CONTENDER: "a contender", MIDDLING: "middling", REBUILD: "a rebuild candidate"}
    mine.append(
        f"Your team profiles as {status_labels[my_status]} in this league ({status_result.reason})."
    )
    age_note = _age_note(target_entry, my_status)
    if age_note:
        mine.append(f"{target_entry.name}: {age_note}.")

    sources_str = ", ".join(target_entry.value.sources_used)
    if currency == DYNASTY_CURRENCY:
        agreement_labels = {
            "agree": "KTC and FantasyPros dynasty ranks agree",
            "moderate_disagreement": "KTC and FantasyPros dynasty ranks disagree moderately",
            "high_disagreement": "KTC and FantasyPros dynasty ranks disagree significantly",
            "insufficient_data": "only one dynasty ranking source available",
        }
        agreement_note = agreement_labels.get(
            target_entry.value.cross_source_agreement, target_entry.value.cross_source_agreement
        )
        corroboration = f"(matched by {len(target_entry.value.sources_used)} sources: {sources_str}; {agreement_note})"
    else:
        corroboration = f"(matched by {len(target_entry.value.sources_used)} sources: {sources_str})"
    mine.append(
        f"{target_entry.name} ({target_entry.position}) trended down recently but still grades out "
        f"at the {ordinal_pct(percentile_for_currency(target_entry.value, currency))} in {label} {corroboration}."
    )
    for give_entry in give:
        if give_entry.value.trend == SELL_HIGH_TREND:
            mine.append(
                f"{give_entry.name} is trending up right now — good time to sell before regression."
            )
        if my_status in (MIDDLING, REBUILD) and give_entry.age is not None and give_entry.age >= veteran_min_age(give_entry.position):
            mine.append(f"{give_entry.name} (age {give_entry.age:g}) is a win-now veteran worth shedding on a {status_labels[my_status]} team.")
        theirs.append(
            f"{give_entry.name} ({give_entry.position}, {ordinal_pct(percentile_for_currency(give_entry.value, currency))}) "
            f"is comparable {label} to {target_entry.name}."
        )
    for pick in give_picks:
        theirs.append(f"{pick.name} ({pick.value:,} KTC pick value) is comparable {label} to {target_entry.name}.")

    theirs.extend(profile.framing_notes())

    for entry in [target_entry, *give]:
        rookie_suffix = _rookie_context_suffix(entry)
        if entry.value.te_premium_caveat and entry.position == "TE":
            caveats.append(entry.value.te_premium_caveat)
        if entry.value.thin_market_caveat:
            caveats.append(f"{entry.name}: {entry.value.thin_market_caveat}")
        if currency == DYNASTY_CURRENCY and entry.value.cross_source_agreement == "moderate_disagreement":
            caveats.append(
                f"{entry.name}: KTC and FantasyPros disagree moderately on value "
                f"({ordinal_pct(entry.value.dynasty_value_percentile)} vs "
                f"{ordinal_pct(entry.value.dynasty_ecr_percentile)}) — treat this valuation with some caution."
                f"{rookie_suffix}"
            )
        if entry.value.panel_disagreement_caveat:
            caveats.append(f"{entry.name}: {entry.value.panel_disagreement_caveat}{rookie_suffix}")
    if give_picks:
        caveats.append(
            "Pick tier (Early/Mid/Late) is estimated from the original team's current roster strength, "
            "not a locked draft slot — it'll move as the season plays out, so treat pick values as "
            "approximate until closer to the actual draft."
        )
    if currency == REDRAFT_CURRENCY:
        caveats.append(
            "Redraft/keeper trade value here is season point projection (RotoBaller), not a dynasty "
            "trade-value market like KTC — there isn't an equivalent crowd-sourced trade-value site for "
            "single-season leagues, so treat these offers as more approximate."
        )

    return mine, theirs, caveats


def _pick_key(pick: OwnedPick) -> tuple:
    return (pick.season, pick.round, pick.original_roster_id)


def _build_pick_target_proposal(
    league: LeagueInfo,
    their_roster: ValuedRoster,
    their_picks: list[OwnedPick],
    my_pool: list[RosterEntry],
    currency: str,
    status_result: TeamStatusResult,
) -> TradeProposal | None:
    """A REBUILD-specific proposal shape: give a player, get a pick instead
    of a player back. Real rebuilding teams routinely ask for picks rather
    than matching value against a specific player — this generates that
    shape directly rather than only ever offering player-for-player.
    Skips each roster's single most valuable pick (their likely-untouchable
    top asset), same spirit as UNTOUCHABLE_COUNT for players.
    """
    sellable_picks = sorted(their_picks, key=lambda p: -(p.value or 0))[1:]
    if not sellable_picks:
        return None
    target_pick = sellable_picks[0]
    if not target_pick.value:
        return None

    offer = _find_matching_offer(my_pool, [], target_pick.value, currency)
    if offer is None:
        return None
    offer_players, _ = offer

    profile = get_owner_profile(their_roster.owner_username or "", league.name)
    label = value_label_for_currency(currency)
    status_labels = {CONTENDER: "a contender", MIDDLING: "middling", REBUILD: "a rebuild candidate"}
    mine = [
        f"Your team profiles as {status_labels[status_result.status]} in this league ({status_result.reason}).",
        f"{target_pick.name} ({target_pick.value:,} KTC pick value) is future draft capital worth prioritizing "
        "over a specific player on a rebuild timeline.",
    ]
    theirs = [
        f"{', '.join(e.name for e in offer_players)} is comparable {label} to {target_pick.name}.",
        *profile.framing_notes(),
    ]
    caveats = [
        "Pick tier (Early/Mid/Late) is estimated from the original team's current roster strength, "
        "not a locked draft slot — it'll move as the season plays out, so treat this pick's value as "
        "approximate until closer to the actual draft."
    ]
    for e in offer_players:
        if e.value.te_premium_caveat and e.position == "TE":
            caveats.append(e.value.te_premium_caveat)

    my_total = sum(value_for_currency(e.value, currency) or 0 for e in offer_players)
    return TradeProposal(
        league_name=league.name,
        currency=currency,
        target_username=their_roster.owner_username or "unknown",
        target_team_name=their_roster.team_name,
        give=offer_players,
        receive=[],
        receive_picks=[target_pick],
        my_value_total=my_total,
        their_value_total=target_pick.value,
        rationale_for_me=mine,
        rationale_for_them=theirs,
        caveats=caveats,
    )


def generate_trade_proposals(
    league: LeagueInfo,
    rosters: dict[int, ValuedRoster],
    *,
    max_proposals: int = 3,
    storage=None,
    engine=None,
    status_result: TeamStatusResult | None = None,
) -> list[TradeProposal]:
    """`storage`/`engine` are optional — when provided (and `status_result`
    isn't already supplied), team-status classification also factors in
    owned future draft capital (dynasty leagues only), and owned picks
    become literal offerable/targetable trade chips rather than only
    biasing a percentile. Pass a pre-computed `status_result` (e.g. from
    report_data.py, which needs it separately anyway) to avoid classifying
    twice.
    """
    my_roster = next((r for r in rosters.values() if r.owner_id == MY_USER_ID), None)
    if my_roster is None or not my_roster.entries:
        # No entries usually means the league hasn't drafted yet (redraft
        # leagues start empty) — nothing to trade, not an error.
        return []

    currency = value_currency(my_roster)
    if status_result is None:
        status_result = classify_team_status(my_roster.roster_id, rosters, currency, storage=storage, engine=engine)
    my_status = status_result.status
    my_needs = identify_needs(my_roster)
    my_pool = _tradeable_pool(my_roster, my_status)

    valued_picks = get_valued_picks_by_roster(rosters, currency, storage, engine)
    my_picks = sorted((valued_picks or {}).get(my_roster.roster_id, []), key=lambda p: -(p.value or 0))

    proposals: list[TradeProposal] = []
    targeted_owners: set[str] = set()

    other_rosters = [r for r in rosters.values() if r.owner_id != MY_USER_ID and r.owner_id is not None]
    for their_roster in other_rosters:
        if not their_roster.entries or their_roster.owner_username in targeted_owners:
            continue
        buy_low = identify_buy_low(their_roster, my_status)
        candidates = [e for e in buy_low if e.position in my_needs[:2]]
        if not candidates:
            continue
        target_entry = candidates[0]

        target_value = value_for_currency(target_entry.value, currency) or 0
        offer = _find_matching_offer(my_pool, my_picks, target_value, currency)
        if offer is None:
            continue
        offer_players, offer_picks = offer

        profile = get_owner_profile(their_roster.owner_username or "", league.name)
        rationale_mine, rationale_theirs, caveats = _build_rationale(
            target_entry, offer_players, currency, profile, status_result, offer_picks
        )

        my_total = sum(value_for_currency(e.value, currency) or 0 for e in offer_players) + sum(
            p.value or 0 for p in offer_picks
        )
        proposals.append(
            TradeProposal(
                league_name=league.name,
                currency=currency,
                target_username=their_roster.owner_username or "unknown",
                target_team_name=their_roster.team_name,
                give=offer_players,
                receive=[target_entry],
                give_picks=offer_picks,
                my_value_total=my_total,
                their_value_total=target_value,
                rationale_for_me=rationale_mine,
                rationale_for_them=rationale_theirs,
                caveats=caveats,
            )
        )
        targeted_owners.add(their_roster.owner_username or "")

        # Remove used pieces so later proposals don't re-offer the same assets.
        used_ids = {e.player_id for e in offer_players}
        my_pool = [e for e in my_pool if e.player_id not in used_ids]
        used_pick_keys = {_pick_key(p) for p in offer_picks}
        my_picks = [p for p in my_picks if _pick_key(p) not in used_pick_keys]

        if len(proposals) >= max_proposals:
            break

    if my_status == REBUILD and valued_picks and len(proposals) < max_proposals:
        for their_roster in other_rosters:
            if their_roster.owner_username in targeted_owners:
                continue
            their_picks = valued_picks.get(their_roster.roster_id, [])
            pick_proposal = _build_pick_target_proposal(league, their_roster, their_picks, my_pool, currency, status_result)
            if pick_proposal is not None:
                proposals.append(pick_proposal)
                break

    return proposals
