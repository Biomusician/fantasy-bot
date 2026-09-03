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
# A 10-12 team dynasty league rosters ~250-300 total players league-wide out
# of KTC's ~500-player pool. The old threshold (20.0, ~rank 401/500) let the
# engine treat deep-bench/practice-squad-caliber players — already flagged
# noisy by THIN_MARKET_RANK_THRESHOLD=150 in valuation.py — as legitimate
# buy-low targets. 45.0 (~rank 275/500) keeps eligibility inside the range
# a real roster in this size league would actually reach, and no longer
# contradicts the thin-market cutoff by reaching 2.5x deeper into it.
MIN_ROSTERABLE_PERCENTILE = 45.0
VALUE_TOLERANCE = 0.20  # accept offers where value ratio is within +/-20%
UNTOUCHABLE_COUNT = 2  # skip each roster's top N players by value as trade targets/gives
ELITE_ASSET_PERCENTILE = 90.0  # bypass age filtering for a clear top-tier asset regardless of team timeline
# A corroborated player who is their position's clear best asset (top decile
# within-position) is treated as untouchable even outside the top-2-overall-
# value cut — protects a scarce position's only real starter (e.g. a
# league's one rosterable TE) that raw cross-position dollar value would
# otherwise miss, since TE dollar values run far lower than RB/WR.
SCARCE_POSITION_PROTECTION_PERCENTILE = 90.0
DECLINE_CONFIRMATION_GAP = 10.0  # dynasty_pctl - redraft_pctl must clear this to call a dip a buy-low, not a real decline
POSITION_ORDER = ("QB", "RB", "WR", "TE")  # fixed order so need-ranking ties break deterministically
MAX_CANDIDATES_PER_OPPONENT = 3  # how many buy-low candidates to try matching per opponent before giving up on them
ACCEPTANCE_TIERS = ("Very Low", "Low", "Moderate", "Good", "High")
_TRADE_TYPE_LABELS = {"buy_low": "Buy low", "sell_high": "Sell high", "pick_target": "Pick target", "consolidation": "2-for-1 consolidation"}

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
    # "buy_low" | "sell_high" | "pick_target" — which generation path built
    # this proposal, so the UI/user can tell a reactive buy-low ask apart
    # from a proactive sell-before-regression pitch or a rebuild pick grab.
    trade_type: str = "buy_low"
    acceptance_rating: str = "Moderate"  # one of ACCEPTANCE_TIERS — a bucket, not a fabricated precise probability
    acceptance_reasons: list[str] = field(default_factory=list)
    confidence: str = "Medium"  # "Low"|"Medium"|"High" — confidence in the underlying valuations, separate from acceptance
    message: str = ""  # short, casual chat-offer text ready to send the opponent

    @property
    def value_ratio(self) -> float:
        """>1 means I'm giving up more value than I get (rare, unfavorable)."""
        if self.their_value_total == 0:
            return float("inf")
        return self.my_value_total / self.their_value_total

    @property
    def balance_kind(self) -> str:
        """UI styling bucket for value_ratio — the single source of truth
        both report.py and html_report.py should read from instead of each
        re-deriving their own thresholds (they'd previously drifted:
        different label casing/wording for the same trade)."""
        ratio = self.value_ratio
        if ratio <= 1.1:
            return "positive"
        if ratio <= 1.15:
            return "caution"
        return "negative"

    @property
    def balance_label(self) -> str:
        ratio = self.value_ratio
        if 0.9 <= ratio <= 1.1:
            return "Balanced"
        if ratio < 0.9:
            return "Favors me"
        if ratio <= 1.15:
            return "Slight overpay"
        return "Overpay"

    @property
    def trade_type_label(self) -> str:
        """Single source of truth for trade_type -> display text, so
        report.py and html_report.py can't drift the way balance_kind's
        docstring says they previously did for value-ratio wording."""
        return _TRADE_TYPE_LABELS.get(self.trade_type, self.trade_type)

    def summary_line(self) -> str:
        give_names = ", ".join([*(e.name for e in self.give), *(p.name for p in self.give_picks)])
        receive_names = ", ".join([*(e.name for e in self.receive), *(p.name for p in self.receive_picks)])
        return f"Send {give_names} to {self.target_team_name or self.target_username} for {receive_names}"


def _corroborated(entry: RosterEntry, currency: str) -> bool:
    return entry.value.is_corroborated and value_for_currency(entry.value, currency) is not None


def identify_sell_high(roster: ValuedRoster, exclude_top: int = UNTOUCHABLE_COUNT) -> list[RosterEntry]:
    """A trending-up player is only a sell-high CANDIDATE if he's not
    already one of my roster's true cornerstone assets — a top-2 starter
    or a scarce position's clear best piece (same _untouchable_ids
    protection identify_buy_low/_tradeable_pool apply to what I'd give up
    in a buy-low trade). Playing well is exactly what you want from your
    actual best players; "sell high" is meant for a SECONDARY asset whose
    recent hot streak may have outrun his real long-term outlook, not an
    invitation to shop your true RB1/WR1 the moment he has a good week.
    """
    currency = value_currency(roster)
    untouchable_ids = _untouchable_ids(roster, currency, exclude_top)
    return sorted(
        (
            e
            for e in roster.entries
            if _corroborated(e, currency) and e.value.trend == SELL_HIGH_TREND and e.player_id not in untouchable_ids
        ),
        key=lambda e: -(value_for_currency(e.value, currency) or 0),
    )


def _untouchable_ids(roster: ValuedRoster, currency: str, exclude_top: int) -> set[str]:
    """The top-`exclude_top` players a roster is treated as unwilling to
    move. Ranked among STARTERS first, falling back to all corroborated
    entries only if there aren't enough starters to fill the cut — a
    redundant, non-starting backup (e.g. a QB2 sitting behind a more
    valuable QB1) must never be "protected" purely by raw dollar value,
    since losing a bench piece costs a roster nothing competitively; only
    an active starter is genuinely hard to pry loose.

    Ranked by PERCENTILE, not raw value — for REDRAFT_CURRENCY, raw value
    is RotoBaller's un-normalized proj_points, which runs structurally
    higher for QBs than RB/WR/TE (see identify_buy_low's sort for the same
    fix). Ranking the "top-2 cornerstone" cut by raw value would let a
    true elite starting WR/RB get out-ranked by a merely-good QB's
    inflated point total and slip out of protection — confirmed to
    reproduce in practice before this fix.

    Also protects each position's clear best corroborated asset when it's
    a scarcity outlier (SCARCE_POSITION_PROTECTION_PERCENTILE), using the
    within-position percentile for dynasty (where it's available) or the
    pool-wide percentile for redraft (the closest available proxy) — a
    league's only real starting TE shouldn't be targetable just because TE
    dollar values run low relative to RB/WR. And protects any starter
    whose loss would drop the roster below the league's own required
    starter count at that position (LeagueFormat.starter_slots) — losing a
    lineup-critical starter (e.g. a Superflex league's only good QB2) is a
    real cost even when that player isn't a raw top-2/percentile outlier.
    """
    corroborated = [e for e in roster.entries if _corroborated(e, currency)]
    starters = [e for e in corroborated if e.is_starter]
    rank_pool = starters if len(starters) >= exclude_top else corroborated
    ids = {
        e.player_id
        for e in sorted(rank_pool, key=lambda e: -(percentile_for_currency(e.value, currency) or 0))[:exclude_top]
    }
    for pos in POSITION_ORDER:
        pos_entries = [e for e in corroborated if e.position == pos]
        if not pos_entries:
            continue
        if currency == DYNASTY_CURRENCY:
            best = max(pos_entries, key=lambda e: e.value.dynasty_positional_percentile or 0)
            best_pctl = best.value.dynasty_positional_percentile or 0
        else:
            best = max(pos_entries, key=lambda e: percentile_for_currency(e.value, currency) or 0)
            best_pctl = percentile_for_currency(best.value, currency) or 0
        if best_pctl >= SCARCE_POSITION_PROTECTION_PERCENTILE:
            ids.add(best.player_id)

        required = roster.fmt.starter_slots.get(pos)
        if required:
            rosterable = sorted(
                (e for e in pos_entries if (percentile_for_currency(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE),
                key=lambda e: -(percentile_for_currency(e.value, currency) or 0),
            )
            # Protect exactly the top `required` at this position -- losing
            # any of them would drop the roster below what the league's own
            # starting lineup needs there.
            ids.update(e.player_id for e in rosterable[: int(required)])
    return ids


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
        (e for e in roster.entries if _corroborated(e, currency)),
        key=lambda e: -(percentile_for_currency(e.value, currency) or 0),
    )
    untouchable_ids = _untouchable_ids(roster, currency, exclude_top)

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
            and (_need_percentile(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE
            and _age_ok(e)
            and _not_just_a_slump(e)
        ),
        # WITHIN-POSITION percentile (_need_percentile), not pool-wide — the
        # same apples-to-oranges problem _need_percentile's own docstring
        # describes for cross-position need comparison applies here too: a
        # merely-good TE could clear a pool-wide rosterable bar that a
        # genuinely weak-for-his-position RB doesn't, purely because TE
        # values run lower across the whole pool. Eligibility and ranking
        # use the same signal on purpose, so they never disagree with each
        # other about what counts as rosterable.
        key=lambda e: -(_need_percentile(e.value, currency) or 0),
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


def _pctl_phrase(pv, currency: str) -> str:
    """"87th percentile within-position" (dynasty, when the positional
    number is actually available) or plain "87th percentile" (redraft, or
    dynasty without a positional rank) — the same _need_percentile value
    used for eligibility/ranking everywhere else, rendered with an honest
    label so two rows never show the same-looking number off two different
    scales with no visible cue (the exact bug class fixed in waiver_engine
    this session)."""
    pctl = _need_percentile(pv, currency)
    if pctl is None:
        return "unknown"
    qualifier = " within-position" if currency == DYNASTY_CURRENCY and pv.dynasty_positional_percentile is not None else ""
    return f"{ordinal_pct(pctl)}{qualifier}"


def identify_needs(roster: ValuedRoster) -> list[str]:
    """Positions where my best asset is weaker than my other positions,
    ranked worst-first. Uses whichever single player I have with the
    highest within-position percentile at each position as that position's
    strength (see _need_percentile for why within-position, not pool-wide).
    """
    currency = value_currency(roster)
    best_by_position: dict[str, float] = {}
    for pos in POSITION_ORDER:
        # Corroborated only (>=2 sources) — same discipline every other
        # trade-engine function applies (_corroborated), so a single-source
        # name-matching artifact can't make a genuinely weak position read
        # as strong (or vice versa) purely off one shaky data point, which
        # would then wrongly steer which positions get targeted for trades.
        entries = [
            e for e in roster.by_position(pos) if _corroborated(e, currency) and _need_percentile(e.value, currency) is not None
        ]
        best_by_position[pos] = max((_need_percentile(e.value, currency) for e in entries), default=0.0)
    return sorted(POSITION_ORDER, key=lambda p: best_by_position[p])


def identify_depth_needs(roster: ValuedRoster, min_starters: dict[str, int] | None = None) -> list[str]:
    """Positions where I don't have ENOUGH rosterable bodies, independent of
    how good my single best player there is — the case identify_needs
    structurally can't see, since it only ever looks at the top player per
    position. `min_starters` (from LeagueFormat.starter_slots, when
    available) is how many of that position the league's roster_positions
    actually requires; without it, this only flags a position with ZERO
    rosterable depth at all, which is still a real signal on its own.
    """
    currency = value_currency(roster)
    thresholds = min_starters or {}
    needy: list[str] = []
    for pos in POSITION_ORDER:
        rosterable = sum(
            1
            for e in roster.by_position(pos)
            if _corroborated(e, currency) and (_need_percentile(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE
        )
        required = thresholds.get(pos, 1)  # at minimum, everyone needs >=1 real body at each core position
        if rosterable < required:
            needy.append(pos)
    return needy


DROP_LOW_VALUE_PERCENTILE = 25.0  # within-position percentile below which a bench player contributes little on his own merit
DROP_EXCESS_DEPTH_BUFFER = 1  # bench bodies beyond (required starters + this buffer) at a position count as excess


@dataclass
class DropCandidate:
    entry: RosterEntry
    priority: str  # "Strong Drop" | "Consider Dropping"
    reasons: list[str]


def identify_drop_candidates(
    roster: ValuedRoster, my_status: str, *, max_candidates: int = 5, exclude_ids: frozenset[str] = frozenset()
) -> list[DropCandidate]:
    """Bench players worth cutting for roster cleanup, independent of any
    specific replacement — unlike a waiver target's paired drop_candidate
    (reactive: "cut someone to make room for THIS add"), this is proactive:
    "here's who's dead weight right now." Three signals, matching the
    codebase's own established metrics — any one can flag a player:

    - Low ranking: within-position percentile (_need_percentile, the same
      metric identify_needs uses) below DROP_LOW_VALUE_PERCENTILE — one of
      the weakest rosterable assets on the roster in his own right.
    - Relative position depth: buried behind enough better same-position
      options that he's very unlikely to ever see the lineup, even in an
      injury/bye emergency. Only STARTERS and other BENCH players count as
      competing depth — a taxi-squad stash or an IR/reserve player isn't
      eligible to start this week and was never competing with a bench
      player for a lineup slot, so counting them inflates "buried" for any
      roster that stashes a taxi squad, which is the normal shape of a
      dynasty bench.
    - No upside: trend isn't rising and (dynasty only, where age/timeline
      is meaningful) he's past his position's prime window on a team
      that isn't win-now anyway — a middling/rebuild roster gains little
      holding an aging, non-trending veteran bench piece.

    A player currently trending up (identify_sell_high's own trigger) is
    excluded entirely, even if he'd otherwise clear the low-rank or
    buried-depth bar — he's live trade capital, not dead weight, and
    flagging him "Consider Dropping" in the same report that pitches
    selling him for value would be a direct self-contradiction.
    `exclude_ids` does the same for players already used as give-pieces in
    a live trade proposal this run — pass the same-report proposals' used
    asset ids so the report doesn't tell the user to both trade a player
    away for value and cut him for nothing.

    Starters, taxi-squad, and IR/reserve entries are never drop candidates
    — taxi/reserve are deliberate stashes, not accidental deadweight, and
    a starter is a lineup decision, not a cut. Single-source valuations
    are skipped rather than risk recommending a cut off a name-matching
    artifact.
    """
    currency = value_currency(roster)
    candidates: list[DropCandidate] = []
    for entry in roster.entries:
        if not entry.is_bench or not _corroborated(entry, currency):
            continue
        if entry.player_id in exclude_ids or entry.value.trend == SELL_HIGH_TREND:
            continue
        pctl = _need_percentile(entry.value, currency)
        if pctl is None:
            continue
        pos = entry.position or ""
        better_same_position = sum(
            1
            for other in roster.by_position(pos)
            if other.player_id != entry.player_id
            and (other.is_starter or other.is_bench)  # taxi/reserve aren't competing for a lineup slot
            and _corroborated(other, currency)
            and (_need_percentile(other.value, currency) or 0) > pctl
        )
        required = roster.fmt.starter_slots.get(pos) or 1  # a float when FLEX demand was distributed -- compare as-is, don't truncate

        reasons: list[str] = []
        if pctl <= DROP_LOW_VALUE_PERCENTILE:
            reasons.append(
                f"{ordinal_pct(pctl)} within-position {value_label_for_currency(currency)} — "
                "one of the weakest rosterable assets you own"
            )
        if better_same_position >= required + DROP_EXCESS_DEPTH_BUFFER:
            reasons.append(f"buried behind {better_same_position} better {pos} options — very unlikely to see your lineup")
        if (
            currency == DYNASTY_CURRENCY
            and entry.age is not None
            and entry.age >= veteran_min_age(pos)
            and my_status != CONTENDER
        ):
            reasons.append(
                f"age {entry.age:g} with no upside signal on a {my_status} roster — "
                "little reason to keep holding this on a team that isn't win-now"
            )

        if not reasons:
            continue
        priority = "Strong Drop" if len(reasons) >= 2 else "Consider Dropping"
        candidates.append(DropCandidate(entry=entry, priority=priority, reasons=reasons))

    candidates.sort(key=lambda c: (_need_percentile(c.entry.value, currency) or 0))
    return candidates[:max_candidates]


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
    untouchable_ids = _untouchable_ids(roster, currency, exclude_top)
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


def _roster_impact_note(
    roster: ValuedRoster,
    position: str | None,
    incoming_value,
    currency: str,
    *,
    exclude_ids: frozenset[str] = frozenset(),
    possessive: str = "your",
    has_verb: str = "You have",
) -> str | None:
    """The concrete "what actually changes on this roster" sentence — names
    the current weakest starter at the position, the WITHIN-POSITION
    percentile gap (not just a binary beats/doesn't), and its magnitude —
    a "beats your starter" claim with no number attached is an assertion,
    not evidence; "beats him by 37 points within position" is something a
    reader can actually weigh. Also honest when it ISN'T an immediate
    upgrade (depth, not a starter swap, with the gap stated either way) —
    the point is accuracy, not always finding a flattering angle.

    `possessive`/`has_verb` let the SAME computation describe either side
    of a trade ("your"/"You have" for my roster, "their"/"They have" for
    the recipient's) instead of a separate near-duplicate implementation
    per side. `exclude_ids` matters whenever a player still technically on
    `roster` is departing in THIS SAME trade (the trade hasn't executed,
    so the roster object still contains him) — without excluding them a
    pitch could compare the incoming piece against the very player(s)
    leaving in the same deal.
    """
    if not position or incoming_value is None:
        return None
    starters_here = [e for e in roster.by_position(position) if e.is_starter and e.player_id not in exclude_ids]
    incoming_pctl = _need_percentile(incoming_value, currency)
    if not starters_here:
        return f"{has_verb} nobody currently starting at {position} — this fills the slot outright."
    weakest = min(starters_here, key=lambda e: _need_percentile(e.value, currency) or 0)
    weak_pctl = _need_percentile(weakest.value, currency)
    if weak_pctl is None or incoming_pctl is None:
        return None
    weak_phrase = _pctl_phrase(weakest.value, currency)
    if incoming_pctl > weak_pctl:
        gap = round(incoming_pctl - weak_pctl)
        return f"This clears {possessive} current starting {position}, {weakest.name} ({weak_phrase}) — a {gap}-point jump, not a marginal swap."
    gap = round(weak_pctl - incoming_pctl)
    return f"This slots in as depth behind {weakest.name} ({weak_phrase}) at {position}, {gap} points back — not an immediate upgrade for {possessive} lineup."


def _value_annotated_names(entries: list[RosterEntry], picks: list[OwnedPick], currency: str) -> str:
    """Names for the rationale's value-comparison bullet, with an inline
    percentile per piece when there's MORE THAN ONE — a single-piece offer
    already has its value shown right in the trade card header, but the
    consolidated multi-piece bullet ("A, B is comparable value to C")
    otherwise gives a reader no way to tell which piece is doing the heavy
    lifting versus riding along as filler.
    """
    total_pieces = len(entries) + len(picks)
    parts: list[str] = []
    for e in entries:
        if total_pieces > 1:
            pctl = percentile_for_currency(e.value, currency)
            parts.append(f"{e.name} ({ordinal_pct(pctl)})" if pctl is not None else e.name)
        else:
            parts.append(e.name)
    for p in picks:
        parts.append(f"{p.name} ({p.value:,})" if total_pieces > 1 and p.value else p.name)
    return " + ".join(parts)


MAX_VALUATION_CAVEAT_BITS = 2  # cap how many sub-caveats fold into one entry's sentence -- past this it reads as a run-on


def _valuation_caveat(entry: RosterEntry, currency: str) -> str | None:
    """One consolidated valuation-confidence note per entry, not a
    separate bullet per caveat type — a 2-piece trade previously produced
    a wall of near-duplicate caveats (TE-premium, thin-market, cross-
    source disagreement, panel disagreement each as their own bullet).
    Capped at MAX_VALUATION_CAVEAT_BITS: when 3+ signals apply to the same
    entry (a realistic combination — a thin-market rookie TE with cross-
    source AND panel disagreement, say), concatenating all of them
    produced a single 400+ character run-on that was harder to scan than
    the original separate bullets. Prioritizes valuation-UNCERTAINTY
    signals (disagreement, thin market) over the TE-premium modeling
    footnote, and drops the rookie-context suffix first when at the cap —
    it's additive context, not a distinct caveat.
    """
    disagreement_bits: list[str] = []
    if currency == DYNASTY_CURRENCY and entry.value.cross_source_agreement == "moderate_disagreement":
        disagreement_bits.append(
            f"KTC and FantasyPros disagree moderately on value ({ordinal_pct(entry.value.dynasty_value_percentile)} "
            f"vs {ordinal_pct(entry.value.dynasty_ecr_percentile)})."
        )
    if entry.value.panel_disagreement_caveat:
        disagreement_bits.append(entry.value.panel_disagreement_caveat)
    other_bits: list[str] = []
    if entry.value.thin_market_caveat:
        other_bits.append(entry.value.thin_market_caveat)
    if entry.value.te_premium_caveat and entry.position == "TE":
        other_bits.append(entry.value.te_premium_caveat)

    all_bits = disagreement_bits + other_bits
    if not all_bits:
        return None
    bits = all_bits[:MAX_VALUATION_CAVEAT_BITS]
    rookie_suffix = _rookie_context_suffix(entry) if disagreement_bits and len(bits) < MAX_VALUATION_CAVEAT_BITS else ""
    return f"{entry.name}: " + " ".join(bits) + rookie_suffix


def _corroboration_note(value, currency: str) -> str | None:
    """A positive claim for when sources genuinely agree, mirroring
    _valuation_caveat's disagreement-only coverage — a "Confidence: High"
    tag already shown on every trade card is otherwise unexplained; this
    makes the reason for it part of the actual argument instead of a bare
    label nobody can verify. Dynasty only: cross_source_agreement is
    always computed off KTC vs FantasyPros DYNASTY percentiles
    (valuation.py), so citing it to support a redraft-currency trade would
    mix two different value scales.
    """
    if currency != DYNASTY_CURRENCY or value.cross_source_agreement != "agree":
        return None
    if value.dynasty_value_percentile is None or value.dynasty_ecr_percentile is None:
        return None
    gap = round(abs(value.dynasty_value_percentile - value.dynasty_ecr_percentile))
    return f"KTC and FantasyPros are within {gap} points of each other on him — not one site's outlier read."


def _buy_low_gap(entry: RosterEntry, currency: str) -> float | None:
    """The actual dynasty-vs-redraft percentile GAP that _not_just_a_slump
    already computes to gate buy-low eligibility (see identify_buy_low),
    factored out so both the rationale note and the short chat-message
    buzz clause read off the same real number instead of duplicating (and
    risking drifting) the same threshold check in two places.
    """
    if currency != DYNASTY_CURRENCY:
        return None
    dyn_pctl, rd_pctl = entry.value.dynasty_value_percentile, entry.value.redraft_ecr_percentile
    if dyn_pctl is None or rd_pctl is None:
        return None
    gap = dyn_pctl - rd_pctl
    return gap if gap >= DECLINE_CONFIRMATION_GAP else None


def _buy_low_timing_note(entry: RosterEntry, currency: str) -> str | None:
    """Surfaces the actual dynasty-vs-redraft percentile GAP instead of
    discarding the number and asserting a "buy-low window" label with
    nothing behind it in the rationale text.
    """
    gap = _buy_low_gap(entry, currency)
    if gap is None:
        return None
    dyn_pctl, rd_pctl = entry.value.dynasty_value_percentile, entry.value.redraft_ecr_percentile
    return (
        f"{entry.name}'s dynasty value has barely moved ({ordinal_pct(dyn_pctl)}) while his current-form/redraft "
        f"stock has dropped to {ordinal_pct(rd_pctl)} — a {round(gap)}-point gap. That split looks like a market "
        "overreaction to recent form, not a real decline in what he's worth long-term."
    )


SELL_HIGH_CONFIRMATION_GAP = 10.0  # KTC pctl - FantasyPros dynasty ECR pctl must clear this to call it hype-ahead-of-consensus, not just "trending"


def _sell_high_gap(entry: RosterEntry, currency: str) -> float | None:
    """The KTC-vs-FantasyPros-dynasty-consensus GAP _sell_high_timing_note
    and the chat-message buzz clause both read off — see that function's
    docstring for why this is a sharper signal than the flat trend label.
    """
    if currency != DYNASTY_CURRENCY:
        return None
    ktc_pctl, fp_pctl = entry.value.dynasty_value_percentile, entry.value.dynasty_ecr_percentile
    if ktc_pctl is None or fp_pctl is None:
        return None
    gap = ktc_pctl - fp_pctl
    return gap if gap >= SELL_HIGH_CONFIRMATION_GAP else None


def _sell_high_timing_note(entry: RosterEntry, currency: str) -> str | None:
    """The sell-high analog of _buy_low_timing_note. The plain "trending
    up — sell before regression" line has no magnitude behind it and is
    also used, unmodified, as the reasoning for BUYING a dip elsewhere in
    this same file — nothing distinguished a real breakout from a hype
    spike. This checks whether KTC's crowd-vote value has run ahead of
    FantasyPros' curated expert dynasty consensus: a real, data-grounded
    "the market may be pricing in more than the measured panel agrees
    with" signal, not just a flat trend arrow.
    """
    gap = _sell_high_gap(entry, currency)
    if gap is None:
        return None
    ktc_pctl, fp_pctl = entry.value.dynasty_value_percentile, entry.value.dynasty_ecr_percentile
    return (
        f"{entry.name}'s KTC crowd value ({ordinal_pct(ktc_pctl)}) has run {round(gap)} points ahead of FantasyPros' "
        f"expert dynasty consensus ({ordinal_pct(fp_pctl)}) — the market may be pricing in more hype than the "
        "measured panel agrees with, a pattern that often gives some of it back."
    )


def _buzz_clause_buy_low(entry: RosterEntry, currency: str) -> str | None:
    """"Recent buzz" for the chat message, buy-low direction — leads with
    the sharp dynasty-vs-redraft divergence when it clears the
    confirmation bar, falls back to the plain trend label otherwise
    (identify_buy_low's own eligibility filter guarantees trend=="down"
    for every candidate this is ever called on, so the fallback always
    has something honest to say).
    """
    if _buy_low_gap(entry, currency) is not None:
        return "he's cooled off a bit recently - might be able to grab him before that turns back around"
    if entry.value.trend == BUY_LOW_TREND:
        return "he's been a little quiet lately, buzz-wise"
    return None


def _buzz_clause_sell_high(entry: RosterEntry, currency: str) -> str | None:
    """The sell-high mirror of _buzz_clause_buy_low."""
    if _sell_high_gap(entry, currency) is not None:
        return "he's been heating up lately, more than the rankings have caught up to yet"
    if entry.value.trend == SELL_HIGH_TREND:
        return "he's been trending up lately"
    return None


def _timeline_clause(fit: "OpponentFit") -> str | None:
    """Names the recipient's contender/rebuild timeline in the chat
    message itself — but only when the ALREADY-COMPUTED status_fit
    genuinely says this trade matches it. Never claimed for "mismatch" or
    "neutral", matching the honest-degradation pattern used everywhere
    else in this file: a false "this fits your rebuild" pitch is worse
    than no timeline mention at all.
    """
    if fit.status_fit != "good_fit":
        return None
    if fit.opponent_status == REBUILD:
        return "figured it makes sense since you're rebuilding"
    if fit.opponent_status == CONTENDER:
        return "figured it makes sense since you're pushing to win now"
    return None


def _my_interest_clause(
    my_roster: ValuedRoster, position: str | None, incoming_value, currency: str, *, exclude_ids: frozenset[str] = frozenset()
) -> str | None:
    """First-person, chat-voice cousin of _roster_impact_note — same
    underlying comparison (my weakest starter at this position vs. the
    incoming piece), rendered as something a manager would actually type
    ("he'd start over X for me") rather than a report sentence. Answers
    "why are we interested in this player" directly in the message,
    instead of leaving that case implicit in the surrounding rationale
    bullets the recipient never sees.
    """
    if not position or incoming_value is None:
        return None
    starters_here = [e for e in my_roster.by_position(position) if e.is_starter and e.player_id not in exclude_ids]
    incoming_pctl = _need_percentile(incoming_value, currency)
    if not starters_here:
        return f"I don't have a real {position} right now so this fills a hole for me"
    weakest = min(starters_here, key=lambda e: _need_percentile(e.value, currency) or 0)
    weak_pctl = _need_percentile(weakest.value, currency)
    if weak_pctl is None or incoming_pctl is None:
        return None
    if incoming_pctl > weak_pctl:
        return f"he'd start over {weakest.name} for me at {position}"
    return f"I like him as depth behind {weakest.name} at {position}"


def _scarcity_note(fmt: LeagueFormat, roster: ValuedRoster, position: str | None, currency: str, *, possessive: str = "your") -> str | None:
    """Names an actual STRUCTURAL reason a position matters in THIS
    league's own format — read live from LeagueFormat, never hardcoded —
    instead of a generic value comparison. A Superflex league's startable
    QB is a second lineup slot every roster competes for, not bench depth;
    this league's own TE-premium bonus (if any) means TE production is
    worth more here than a standard dynasty ranking implies. Falls back to
    a real zero-rosterable-depth check (reusing the same MIN_ROSTERABLE_
    PERCENTILE bar and within-position percentile the rest of the trade
    engine uses) rather than manufacturing a scarcity argument the format
    doesn't actually support.
    """
    if not position:
        return None
    if position == "QB" and fmt.is_superflex:
        return "This is a Superflex league — a startable QB is a second lineup slot every roster competes for, not bench depth."
    if position == "TE" and fmt.te_premium_bonus > 0:
        return f"This league runs TE premium (+{fmt.te_premium_bonus:g}/rec) — TE production is worth more here than a standard dynasty ranking assumes."
    if _position_rosterable_count(roster, position, currency) == 0:
        return f"{possessive.capitalize()} roster has zero other rosterable bodies at {position} right now — this isn't marginal depth, it's closing a structural hole."
    return None


def _consolidation_note(give: list[RosterEntry]) -> str | None:
    """Frames a 2-(or more)-for-1-up trade as a deliberate roster-
    construction move on MY side — today `piece_count >= 2` is used ONLY
    as a negative signal against the recipient's acceptance ("some
    managers read this as a lowball"); nothing ever explains why
    consolidating bench depth into one better piece is good for ME. Never
    claimed if a starter is part of the package (real cost isn't "close to
    zero" if I'm also giving up a lineup spot).
    """
    if len(give) < 2 or any(e.is_starter for e in give):
        return None
    return (
        f"This turns {len(give)} bench spots you're deep at into one lineup-quality piece — real cost is close to "
        "zero, since none of what you're sending was starting for you anyway."
    )


def _build_rationale(
    target_entry: RosterEntry,
    give: list[RosterEntry],
    currency: str,
    profile,
    status_result: TeamStatusResult,
    my_roster: ValuedRoster,
    their_roster: ValuedRoster,
    give_picks: list[OwnedPick] = (),
) -> tuple[list[str], list[str], list[str]]:
    mine: list[str] = []
    theirs: list[str] = []
    caveats: list[str] = []
    label = value_label_for_currency(currency)
    my_status = status_result.status
    status_labels = {CONTENDER: "a contender", MIDDLING: "middling", REBUILD: "a rebuild candidate"}

    # Lead with the concrete roster impact — what actually changes for
    # ME, named against my actual current starter, the within-position
    # percentile gap, and its magnitude. Exclude the give-piece(s):
    # they're still technically on my_roster (the trade hasn't executed),
    # so without excluding them a same-position give could get compared
    # against itself as "the starter this beats".
    give_ids = frozenset(e.player_id for e in give)
    impact = _roster_impact_note(my_roster, target_entry.position, target_entry.value, currency, exclude_ids=give_ids)
    if impact:
        mine.append(impact)

    timing = _buy_low_timing_note(target_entry, currency)
    if timing:
        mine.append(timing)

    scarcity = _scarcity_note(my_roster.fmt, my_roster, target_entry.position, currency)
    if scarcity:
        mine.append(scarcity)

    if not mine:
        # Honest fallback so a proposal never ships with an empty "why it
        # works for me" list — this sentence used to run unconditionally
        # on EVERY trade for a team, producing the exact same long string
        # repeated 2-3 times per team section in the actual report; now
        # it only appears when nothing more specific to this trade fired.
        mine.append(f"Your team profiles as {status_labels[my_status]} in this league ({status_result.reason}).")

    age_note = _age_note(target_entry, my_status)
    if age_note:
        mine.append(f"{target_entry.name}: {age_note}.")
    for give_entry in give:
        if give_entry.value.trend == SELL_HIGH_TREND:
            mine.append(f"{give_entry.name} is trending up right now — good time to sell before regression.")
        if my_status in (MIDDLING, REBUILD) and give_entry.age is not None and give_entry.age >= veteran_min_age(give_entry.position):
            mine.append(f"{give_entry.name} (age {give_entry.age:g}) is a win-now veteran worth shedding on a {status_labels[my_status]} team.")
    consolidation = _consolidation_note(give)
    if consolidation:
        mine.append(consolidation)

    # "Why THEY say yes" — built from THIS trade's actual numbers against
    # THEIR roster, not a static per-manager trait dump. Previously this
    # was entirely profile.framing_notes(), which depends only on the
    # recipient's identity and is therefore identical across every trade
    # sent to that owner regardless of what's actually being offered
    # (confirmed against real report output: the same trait bullets
    # appeared verbatim for one owner across 5 different leagues and 5
    # different assets — the single biggest driver of the "bland" and
    # "copy-pasted" complaints). Lead with the concrete case; framing_notes
    # stays at the end as pitching ADVICE, not as the substance itself.
    if give:
        primary_give = max(give, key=lambda e: _need_percentile(e.value, currency) or 0)
        their_impact = _roster_impact_note(
            their_roster, primary_give.position, primary_give.value, currency,
            exclude_ids=frozenset({target_entry.player_id}), possessive="their", has_verb="They have",
        )
        if their_impact:
            theirs.append(their_impact)
        their_scarcity = _scarcity_note(their_roster.fmt, their_roster, primary_give.position, currency, possessive="their")
        if their_scarcity:
            theirs.append(their_scarcity)
        if profile.hot_streak_susceptible and any(e.value.trend == SELL_HIGH_TREND for e in give):
            theirs.append(f"{profile.username or 'This owner'} has taken the bait on a hot streak before — worth leading with whichever piece here is trending up.")

    # ONE consolidated value-comparison bullet, not one per give-piece —
    # a 2-piece offer previously produced near-duplicate "X is comparable
    # value to Y" bullets that added little beyond restating the trade
    # card's own give/receive line. Corroboration (when sources agree)
    # turns it from a restatement of the header numbers into an actual
    # claim about why the price is trustworthy.
    give_names = _value_annotated_names(give, give_picks, currency) or "This package"
    value_line = f"{give_names} is comparable {label} to {target_entry.name}."
    corroboration = _corroboration_note(target_entry.value, currency)
    if corroboration:
        value_line += f" {corroboration}"
    theirs.append(value_line)
    theirs.extend(profile.framing_notes())

    for entry in [target_entry, *give]:
        note = _valuation_caveat(entry, currency)
        if note:
            caveats.append(note)
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


def _build_sell_high_rationale(
    sell_entry: RosterEntry,
    receive: list[RosterEntry],
    currency: str,
    profile,
    status_result: TeamStatusResult,
    my_roster: ValuedRoster,
    their_roster: ValuedRoster,
    receive_picks: list[OwnedPick] = (),
) -> tuple[list[str], list[str], list[str]]:
    """Mirrors _build_rationale's structure and caveat logic, but for the
    opposite narrative direction: I'm SELLING a rising asset of mine, not
    buying a dip of theirs, so the framing (and trend direction) flips.
    """
    mine: list[str] = []
    theirs: list[str] = []
    caveats: list[str] = []
    label = value_label_for_currency(currency)

    timing = _sell_high_timing_note(sell_entry, currency)
    if timing:
        mine.append(timing)
    else:
        # Honest fallback: the sharper KTC-vs-FantasyPros divergence signal
        # isn't always available (redraft currency, or the sources happen
        # to agree) — the plain trend label still says something, just
        # without the magnitude the sharper check provides.
        mine.append(
            f"{sell_entry.name} is trending up right now ({ordinal_pct(percentile_for_currency(sell_entry.value, currency))} "
            f"in {label}) — a good window to sell before performance regresses toward his underlying value profile."
        )
    if receive:
        primary = max(receive, key=lambda e: _need_percentile(e.value, currency) or 0)
        impact = _roster_impact_note(
            my_roster, primary.position, primary.value, currency,
            exclude_ids=frozenset({sell_entry.player_id}),
        )
        if impact:
            mine.append(impact)

    # "Why THEY say yes" — the recipient's need is now SUBSTANTIATED
    # against their actual roster (previously just asserted: "fits a need
    # on your roster" with no way for a reader to verify it against data
    # the tool clearly already has, since it's exactly what selected this
    # opponent in the first place).
    # Exclude `receive` (what they'd send back to me): those pieces are
    # still technically on their_roster since the trade hasn't executed,
    # so without excluding them their own returning piece could get cited
    # as "their current starter" this trade beats — the same self-
    # reference bug class already fixed on the buy-low side this session,
    # just not yet on this newly-added recipient-side computation.
    their_impact = _roster_impact_note(
        their_roster, sell_entry.position, sell_entry.value, currency,
        exclude_ids=frozenset(e.player_id for e in receive), possessive="their", has_verb="They have",
    )
    if their_impact:
        theirs.append(their_impact)
    else:
        theirs.append(f"{sell_entry.name} fits a real need on their roster at {sell_entry.position}.")
    their_scarcity = _scarcity_note(their_roster.fmt, their_roster, sell_entry.position, currency, possessive="their")
    if their_scarcity:
        theirs.append(their_scarcity)
    if profile.hot_streak_susceptible and sell_entry.value.trend == SELL_HIGH_TREND:
        theirs.append(f"{profile.username or 'This owner'} has taken the bait on a hot streak before — this is exactly the kind of pitch that's worked on them.")

    receive_names = _value_annotated_names(receive, receive_picks, currency) or "This return"
    value_line = f"{receive_names} is comparable {label} to {sell_entry.name}."
    corroboration = _corroboration_note(sell_entry.value, currency)
    if corroboration:
        value_line += f" {corroboration}"
    theirs.append(value_line)
    theirs.extend(profile.framing_notes())

    for entry in [sell_entry, *receive]:
        note = _valuation_caveat(entry, currency)
        if note:
            caveats.append(note)
    if receive_picks:
        caveats.append(
            "Pick tier (Early/Mid/Late) is estimated from the original team's current roster strength, "
            "not a locked draft slot — treat pick values as approximate until closer to the actual draft."
        )
    if currency == REDRAFT_CURRENCY:
        caveats.append(
            "Redraft/keeper trade value here is season point projection (RotoBaller), not a dynasty "
            "trade-value market like KTC — treat this offer as more approximate."
        )
    return mine, theirs, caveats


def _pick_key(pick: OwnedPick) -> tuple:
    return (pick.season, pick.round, pick.original_roster_id)


# -- Opponent-fit assessment and acceptance rating ---------------------------
#
# Everything above this point answers "is the math balanced". None of it
# ever asks whether the RECEIVING team's roster actually has any use for
# what they'd get — the single most consequential gap an 8-reviewer audit
# of this codebase converged on independently. The functions below close
# that gap using data generate_trade_proposals already has in scope
# (their_roster, the shared `rosters` dict, owner_profiles) — no new data
# source is needed.


def _position_rosterable_count(roster: ValuedRoster, position: str, currency: str) -> int:
    return sum(
        1
        for e in roster.by_position(position)
        if _corroborated(e, currency) and (_need_percentile(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE
    )


def _weakest_rosterable_percentile(
    roster: ValuedRoster, position: str, currency: str, *, exclude_player_id: str | None = None
) -> float | None:
    """The bar a give-piece must clear to plausibly be an upgrade for this
    roster at this position — their worst currently-rosterable player
    there. None means they have no rosterable depth at all at this
    position, i.e. ANY reasonable piece would help. `exclude_player_id`
    matters when the piece being evaluated is going the OTHER direction in
    the same trade (e.g. the buy-low target leaving their roster, or the
    sell-high asset leaving mine) — the roster object still contains that
    departing player since the trade hasn't executed, so without excluding
    him he can prop up "their depth" against himself.
    """
    pctls = [
        percentile_for_currency(e.value, currency)
        for e in roster.by_position(position)
        if e.player_id != exclude_player_id
        and _corroborated(e, currency)
        and (percentile_for_currency(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE
    ]
    return min(pctls) if pctls else None


@dataclass
class OpponentFit:
    target_is_starter: bool
    would_upgrade_their_roster: bool
    fit_notes: list[str]
    opponent_status: str  # contender | middling | rebuild
    status_fit: str  # good_fit | neutral | mismatch
    piece_count: int  # len(give) + len(give_picks) — 1 = clean ask, 2+ = fragmented


def _piece_fits(their_roster: ValuedRoster, piece: RosterEntry, currency: str, *, exclude_player_id: str | None = None) -> bool:
    """Would this ONE piece plausibly help their_roster — beats their
    weakest currently-rosterable player at that position, or fills a
    position where they have zero rosterable depth at all."""
    weakest = _weakest_rosterable_percentile(their_roster, piece.position or "", currency, exclude_player_id=exclude_player_id)
    piece_pctl = percentile_for_currency(piece.value, currency) or 0
    return weakest is None or piece_pctl > weakest


def _recipient_need_fit(
    their_roster: ValuedRoster, give: list[RosterEntry], currency: str, *, exclude_player_id: str | None = None
) -> tuple[bool, bool, list[str]]:
    """Would sending `give` plausibly help their_roster, or is it roster
    clutter they have no use for? Returns (any_fits, all_fit, notes):
    any_fits is True if AT LEAST ONE piece clears the bar (used to decide
    whether an offer is worth proposing at all — a legitimate throw-in
    alongside a real upgrade is a normal trade shape); all_fit is True
    only if EVERY piece does (used to decide whether the offer should be
    scored as a clean, unqualified roster upgrade for them, vs. one that
    includes dead weight). A picks-only give (no players) always fits
    fully — draft capital helps any roster. `exclude_player_id` excludes a
    player still technically on their_roster but departing in this same
    trade (see _weakest_rosterable_percentile).
    """
    if not give:
        return True, True, []
    notes: list[str] = []
    fits_flags: list[bool] = []
    for piece in give:
        ok = _piece_fits(their_roster, piece, currency, exclude_player_id=exclude_player_id)
        fits_flags.append(ok)
        if not ok:
            notes.append(f"{piece.name} ({piece.position}) likely wouldn't beat their existing {piece.position} depth")
    return any(fits_flags), all(fits_flags), notes


MAX_OFFER_RETRIES = 3  # alternate combinations to try before giving up on a candidate/opponent pairing


def _find_fitting_offer(
    pool: list[RosterEntry],
    picks: list[OwnedPick],
    target_value: float,
    currency: str,
    recipient_roster: ValuedRoster,
    *,
    exclude_player_id: str | None = None,
) -> tuple[list[RosterEntry], list[OwnedPick], list[str], bool] | None:
    """_find_matching_offer returns the first value-tolerance match it
    finds and stops — if that combination includes a piece that's roster
    clutter for the recipient, there may still be a different combination
    at the same target value where every piece is a genuine fit. Prefers
    an all-fit combination: on an any-fit-but-not-all-fit result, retries
    with just the clutter piece(s) excluded (not the whole trial) to look
    for a cleaner alternative, falling back to the best any-fit result
    found if no fully-clean combination turns up within the retry budget.
    Returns (players, picks, fit_notes, all_fit) — all_fit tells the
    caller whether the returned offer is an unqualified fit or includes a
    weaker piece riding along (fit_notes explains which). `exclude_player_id`
    excludes a player still technically on recipient_roster but departing
    in this same trade (the buy-low target being acquired FROM them, or
    the sell-high asset leaving MY roster when recipient_roster is mine).
    """
    candidate_pool = pool
    best_any_fit: tuple[list[RosterEntry], list[OwnedPick], list[str]] | None = None
    for _ in range(MAX_OFFER_RETRIES):
        trial = _find_matching_offer(candidate_pool, picks, target_value, currency)
        if trial is None:
            break
        trial_players, trial_picks = trial
        any_fit, all_fit, fit_notes = _recipient_need_fit(recipient_roster, trial_players, currency, exclude_player_id=exclude_player_id)
        if all_fit:
            return trial_players, trial_picks, fit_notes, True
        if not any_fit:
            excluded_ids = {e.player_id for e in trial_players}
            candidate_pool = [e for e in candidate_pool if e.player_id not in excluded_ids]
            continue
        # Any-fit but not all-fit: keep this as a fallback, then retry
        # excluding just the clutter piece(s) to look for an all-fit combo.
        if best_any_fit is None:
            best_any_fit = (trial_players, trial_picks, fit_notes)
        clutter_ids = {
            e.player_id for e in trial_players if not _piece_fits(recipient_roster, e, currency, exclude_player_id=exclude_player_id)
        }
        candidate_pool = [e for e in candidate_pool if e.player_id not in clutter_ids]
    if best_any_fit is not None:
        players, picks_, notes = best_any_fit
        return players, picks_, notes, False
    return None


def _status_fit(give: list[RosterEntry], give_picks: list[OwnedPick], opponent_status: str) -> str:
    """Does what the OPPONENT receives match their apparent team timeline?
    Veteran, proven production fits a contender and cuts against a
    rebuild; picks/youth fit a rebuild and cut against a contender who
    wants to win now, not stockpile future assets.
    """
    has_picks = bool(give_picks)
    veteran_heavy = any(e.age is not None and e.age >= veteran_min_age(e.position) for e in give)
    young_heavy = (not veteran_heavy) and any(
        e.age is not None and e.age <= young_max_age(e.position) for e in give
    )
    if opponent_status == REBUILD:
        if has_picks or young_heavy:
            return "good_fit"
        if veteran_heavy:
            return "mismatch"
    elif opponent_status == CONTENDER:
        if veteran_heavy:
            return "good_fit"
        if has_picks and not give:
            return "mismatch"
    return "neutral"


def rate_acceptance(
    fit: OpponentFit, value_ratio: float, profile, give: list[RosterEntry] = ()
) -> tuple[str, list[str]]:
    """Buckets acceptance likelihood into ACCEPTANCE_TIERS rather than a
    fabricated precise probability — the underlying signals (value
    closeness, roster fit, timeline fit, owner tendencies) support a
    directional read, not a number with real statistical meaning.

    `value_ratio` is (value I give) / (value I receive) at every call site
    — below 1.0 means I'm lowballing them, above 1.0 means I'm overpaying
    (favorable to THEM). Only the lowball direction should ever reduce
    acceptance likelihood; a generous overpay should never look less
    likely to be accepted than a fair trade (the user's own "am I
    overpaying" signal is surfaced separately via balance_label, not here).

    `give` is the piece(s) the OPPONENT would receive — used only to check
    the offer's age profile against their documented youth/veteran
    preference (same veteran_heavy/young_heavy signal _status_fit uses).
    """
    score = 0
    reasons: list[str] = []
    if abs(value_ratio - 1.0) <= 0.05:
        score += 1
        reasons.append("Value is nearly dead-even.")
    elif value_ratio < 0.85:
        score -= 1
        reasons.append("Value sits near the edge of an acceptable range.")
    if fit.target_is_starter:
        score -= 1
        reasons.append("Asks for their current starter — expect more resistance than a bench piece.")
    if not fit.would_upgrade_their_roster:
        score -= 2
        reasons.append("What they'd receive doesn't clearly beat their existing depth at that position.")
    if fit.status_fit == "good_fit":
        score += 1
        reasons.append(f"Matches their apparent {fit.opponent_status} timeline.")
    elif fit.status_fit == "mismatch":
        score -= 1
        reasons.append(f"Cuts against their apparent {fit.opponent_status} timeline.")
    if fit.piece_count >= 2:
        if profile.dislikes_multi_piece:
            score -= 2
            reasons.append(f"{profile.username or 'This owner'} has specifically pushed back on lopsided multi-piece offers before — this needs to be a clean, close-to-even bundle or it likely stalls.")
        else:
            score -= 1
            reasons.append("Multi-piece offer — some managers read this as a lowball.")
    if profile.trades_often == "inactive":
        score = min(score, -2)
        reasons.append(f"{profile.username or 'This owner'} is documented as rarely completing trades.")
    elif profile.trades_often == "infrequent":
        score -= 1
        reasons.append(f"{profile.username or 'This owner'} doesn't complete trades often — even a fair offer may take patience and follow-up.")
    elif profile.trades_often == "active":
        score += 1
        reasons.append(f"{profile.username or 'This owner'} trades often.")
    if give:
        veteran_heavy = any(e.age is not None and e.age >= veteran_min_age(e.position) for e in give)
        young_heavy = (not veteran_heavy) and any(e.age is not None and e.age <= young_max_age(e.position) for e in give)
        if profile.youth_vs_veteran == "prefers_youth" and veteran_heavy:
            score -= 1
            reasons.append(f"{profile.username or 'This owner'} prefers young players — this leans veteran, which may need sweetening.")
        elif profile.youth_vs_veteran == "prefers_veteran" and young_heavy:
            score -= 1
            reasons.append(f"{profile.username or 'This owner'} buys veterans, not unproven youth — this leans young, which may need sweetening.")
    idx = max(0, min(len(ACCEPTANCE_TIERS) - 1, 2 + score))
    return ACCEPTANCE_TIERS[idx], reasons


def _player_confidence(pv: PlayerValue) -> str:
    if not pv.is_corroborated or pv.cross_source_agreement == "high_disagreement":
        return "Low"
    if pv.cross_source_agreement == "moderate_disagreement" or pv.thin_market_caveat or pv.panel_disagreement_caveat:
        return "Medium"
    return "High"


_CONFIDENCE_RANK = {"Low": 0, "Medium": 1, "High": 2}


def proposal_confidence(values: list[PlayerValue]) -> str:
    """Confidence in the VALUATIONS behind a proposal — distinct from
    acceptance_rating (confidence in whether they'd say yes). Rolls up
    is_corroborated/cross_source_agreement/thin_market/panel_disagreement,
    which were already computed per-player but previously only ever
    surfaced as free-text caveat prose, not a structured, sortable field.
    Takes the WORST confidence among all players in play — one shaky
    valuation is enough to make the whole proposal less trustworthy.
    """
    if not values:
        return "Medium"
    return min((_player_confidence(pv) for pv in values), key=lambda c: _CONFIDENCE_RANK[c])


# -- Outreach message generation ---------------------------------------------

_MESSAGE_OPENERS = {
    "buy_low": [
        "Hey, {give_line} for {receive_line}? ",
        "Would you do {give_line} for {receive_line}? ",
        "Hey, I'd offer {give_line} for {receive_line} if you're open to it. ",
    ],
    "sell_high": [
        "Hey, I'd move {give_line} for {receive_line}, interested? ",
        "I'd let go of {give_line} for {receive_line} if it helps you out. ",
    ],
    "pick_target": [
        "Hey, I'd send {give_line} for {receive_line}. ",
        "Thinking {give_line} for {receive_line}? ",
    ],
}

# Generic fallback closers — used only when no concrete benefit_reason
# could be computed (e.g. a caller that hasn't threaded roster data
# through). Prefer benefit_reason everywhere it's available: "fills a
# real need" says nothing an opposing manager doesn't already assume
# you'd claim; naming the actual position/starter gap is what makes an
# offer read as considered rather than mass-produced.
_GENERIC_CLOSERS = [
    "No pressure either way, just floating it.",
    "Let me know if it's not your speed.",
    "Totally open to a different mix if this isn't it.",
]


def _names_line(entries: list[RosterEntry], picks: list[OwnedPick]) -> str:
    names = [e.name for e in entries] + [p.name for p in picks]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return " + ".join(names)


def _content_seed(*parts: str) -> int:
    """A stable (not process-hash-randomized, unlike Python's built-in
    hash()) integer derived from actual message content, used to vary
    opener choice by what the trade actually IS rather than by piece-
    counts/username-length alone — the latter made two different real
    trades produce byte-identical messages whenever give/receive counts
    and username length happened to match, which is common for ordinary
    single-for-single offers.
    """
    return sum(ord(c) for c in "".join(parts))


def _benefit_reason(
    their_roster: ValuedRoster,
    pieces: list[RosterEntry],
    currency: str,
    *,
    exclude_player_id: str | None = None,
    exclude_player_ids: frozenset[str] = frozenset(),
    fit: "OpponentFit | None" = None,
) -> str | None:
    """A short, concrete clause naming exactly why the primary incoming
    piece helps THEIR roster — computed from their actual current depth
    at that position, not a templated "fills a need" guess. This is what
    makes the message specific to this opponent and this trade rather
    than interchangeable with any other offer.

    When `fit` (the already-computed OpponentFit) says the offer does NOT
    cleanly upgrade their roster — e.g. a multi-piece offer where a
    secondary piece is genuine clutter — this defers to that verdict
    instead of confidently claiming a clean upgrade based only on the
    single highest-value piece, which could otherwise contradict the same
    proposal's own acceptance_rating and caveats. `exclude_player_id`
    (buy-low: the single target being acquired FROM them) and
    `exclude_player_ids` (sell-high: the — possibly 1-2 — pieces they'd
    send back to me) both exclude players still technically on
    their_roster but departing in this same trade — without excluding
    them, a same-position piece could get measured against the very
    player(s) leaving their roster in this same deal. Only the LOCAL
    starters_here check below honors the plural form; the pool-wide
    _weakest_rosterable_percentile fallback still only takes the single
    `exclude_player_id` (that shared primitive is deliberately left
    untouched — see identify_buy_low's percentile-fix commit — so this is
    a narrower, lower-severity gap: a departing piece could still prop up
    the pool-wide "weakest rosterable" bar in that fallback branch, but
    can no longer be NAMED as "their current starter" this trade beats).
    """
    if not pieces:
        return "for the extra draft capital"
    if fit is not None and not fit.would_upgrade_their_roster:
        return "mixing in some depth alongside the headline piece" if len(pieces) > 1 else "as a depth flier - low cost either way"
    primary = max(pieces, key=lambda e: _need_percentile(e.value, currency) or 0)
    pos = primary.position
    if not pos:
        return None  # caller falls back to a generic closer rather than a "roster"-shaped sentence
    excluded = exclude_player_ids | ({exclude_player_id} if exclude_player_id else set())
    # Within-position percentile for the starter comparison (apples to
    # apples — both numbers are on the same scale), WITH the magnitude of
    # the gap: "start over what you've got" alone is a bare assertion,
    # "clear him by 22 points" is something the recipient can actually
    # weigh. Kept as a SEPARATE pool-wide comparison below against
    # _weakest_rosterable_percentile's own bar (that shared primitive is
    # deliberately pool-wide — see identify_buy_low's percentile-fix
    # commit — so the two branches don't mix scales against each other).
    starters_here = [e for e in their_roster.by_position(pos) if e.is_starter and e.player_id not in excluded]
    if starters_here:
        piece_pctl = _need_percentile(primary.value, currency) or 0
        weakest_starter = min(starters_here, key=lambda e: _need_percentile(e.value, currency) or 0)
        weakest_starter_pctl = _need_percentile(weakest_starter.value, currency) or 0
        if piece_pctl > weakest_starter_pctl:
            gap = round(piece_pctl - weakest_starter_pctl)
            return f"since he'd clear {weakest_starter.name} at {pos} by {gap} points, not just marginally better"
    weakest = _weakest_rosterable_percentile(their_roster, pos, currency, exclude_player_id=exclude_player_id)
    if weakest is None:
        return f"since you don't have a real {pos} right now"
    pool_pctl = percentile_for_currency(primary.value, currency) or 0
    if pool_pctl > weakest:
        return f"for real {pos} depth behind your starter"
    return f"as a {pos} flier - low cost either way"


def generate_trade_message(
    proposal: TradeProposal,
    fit: OpponentFit | None = None,
    *,
    benefit_reason: str | None = None,
    timeline_clause: str | None = None,
    buzz_clause: str | None = None,
    my_interest_clause: str | None = None,
) -> str:
    """A short, casual chat message to actually send — not a summary of the
    proposal's stats. Deliberately avoids AI-sounding phrasing ("according
    to my projections", "this benefits both parties", etc.) in favor of
    the way a real manager pitches a trade in a league chat: plain, a
    little informal, no hard sell.

    Four independent, optional clauses, each dropped silently when there's
    nothing honest to say (never padded with filler) — this is deliberately
    a short PITCH, not the full rationale restated:
    - `benefit_reason` (from _benefit_reason): the concrete roster-fit case
      for THEM — e.g. "since he'd start over what you've got at TE now".
    - `timeline_clause` (from _timeline_clause): why it fits THEIR
      contender/rebuild timeline, only when it genuinely does.
    - `buzz_clause` (from _buzz_clause_buy_low/_sell_high): recent
      market movement on the player driving this trade.
    - `my_interest_clause` (from _my_interest_clause): why *I* want what
      I'd be getting — the message previously only ever argued their side,
      never said anything about why the sender is actually interested.
    """
    trade_type = proposal.trade_type if proposal.trade_type in _MESSAGE_OPENERS else "buy_low"
    give_line = _names_line(proposal.give, proposal.give_picks)
    receive_line = _names_line(proposal.receive, proposal.receive_picks)
    seed = _content_seed(give_line, receive_line, proposal.target_username or "")
    opener_idx = seed % len(_MESSAGE_OPENERS[trade_type])
    opener = _MESSAGE_OPENERS[trade_type][opener_idx].format(give_line=give_line, receive_line=receive_line)

    def _sentence(clause: str | None) -> str | None:
        if not clause:
            return None
        text = clause[0].upper() + clause[1:]
        return text if text.endswith((".", "!", "?")) else text + "."

    parts = [opener.strip()]
    for clause in (benefit_reason, timeline_clause, buzz_clause, my_interest_clause):
        sentence = _sentence(clause)
        if sentence:
            parts.append(sentence)
    if len(parts) == 1:
        # Nothing concrete could be computed for ANY clause -- fall back
        # to a generic closer rather than shipping a bare opener with no
        # substance at all.
        closer_idx = (seed * 7 + 3) % len(_GENERIC_CLOSERS)
        parts.append(_GENERIC_CLOSERS[closer_idx])
    return " ".join(parts)


def _build_pick_target_proposal(
    league: LeagueInfo,
    their_roster: ValuedRoster,
    their_picks: list[OwnedPick],
    my_pool: list[RosterEntry],
    currency: str,
    status_result: TeamStatusResult,
    *,
    rosters: dict[int, "ValuedRoster"] | None = None,
    storage=None,
    engine=None,
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
    their_status_result = (
        classify_team_status(their_roster.roster_id, rosters, currency, storage=storage, engine=engine)
        if rosters is not None and storage and engine
        else None
    )
    mine = [
        f"Your team profiles as {status_labels[status_result.status]} in this league ({status_result.reason}).",
        f"{target_pick.name} ({target_pick.value:,} KTC pick value) is future draft capital worth prioritizing "
        "over a specific player on a rebuild timeline.",
    ]
    give_names = ", ".join(e.name for e in offer_players)
    value_line = f"{give_names} is comparable {label} to {target_pick.name}."
    if len(offer_players) == 1:
        corroboration = _corroboration_note(offer_players[0].value, currency)
        if corroboration:
            value_line += f" {corroboration}"
    theirs = [value_line]
    if their_status_result and their_status_result.status == REBUILD:
        theirs.append(
            f"{their_roster.owner_username or 'They'} profile as a rebuild candidate here too "
            f"({their_status_result.reason}) — a pick is worth more to that timeline than locking into a specific "
            "player at this roster spot."
        )
    theirs.extend(profile.framing_notes())
    caveats = [
        "Pick tier (Early/Mid/Late) is estimated from the original team's current roster strength, "
        "not a locked draft slot — it'll move as the season plays out, so treat this pick's value as "
        "approximate until closer to the actual draft."
    ]
    for e in offer_players:
        if e.value.te_premium_caveat and e.position == "TE":
            caveats.append(e.value.te_premium_caveat)

    my_total = sum(value_for_currency(e.value, currency) or 0 for e in offer_players)
    ratio = my_total / target_pick.value if target_pick.value else float("inf")
    fit = OpponentFit(
        target_is_starter=False,  # a pick has no lineup slot to protect
        would_upgrade_their_roster=True,  # draft capital always has value to any roster
        fit_notes=[],
        opponent_status=their_status_result.status if their_status_result else "unknown",
        status_fit="good_fit" if their_status_result and their_status_result.status == REBUILD else "neutral",
        piece_count=len(offer_players),
    )
    rating, reasons = rate_acceptance(fit, ratio, profile, give=offer_players)
    proposal = TradeProposal(
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
        trade_type="pick_target",
        acceptance_rating=rating,
        acceptance_reasons=reasons,
        confidence=proposal_confidence([e.value for e in offer_players]),
    )
    proposal.message = generate_trade_message(
        proposal, fit,
        benefit_reason=_benefit_reason(their_roster, offer_players, currency, fit=fit),
        timeline_clause=_timeline_clause(fit),
        my_interest_clause="I'd rather stockpile picks than lock into a specific player right now",
    )
    return proposal


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
    # Quality needs (worst single player) plus depth needs (not enough
    # rosterable bodies at a position regardless of the best one's
    # quality) — a strong RB1 sitting on zero rosterable RB2/3 depth
    # wouldn't otherwise ever surface as a target position.
    depth_needs = identify_depth_needs(my_roster, my_roster.fmt.starter_slots)
    target_positions = list(dict.fromkeys([*my_needs[:2], *depth_needs]))
    my_pool = _tradeable_pool(my_roster, my_status)

    valued_picks = get_valued_picks_by_roster(rosters, currency, storage, engine)
    my_picks = sorted((valued_picks or {}).get(my_roster.roster_id, []), key=lambda p: -(p.value or 0))

    other_rosters = [r for r in rosters.values() if r.owner_id != MY_USER_ID and r.owner_id is not None]

    # -- Pass 1: buy-low. Collect every viable (opponent, target, offer)
    # combination FIRST, then rank and select — not first-fit by whatever
    # order Sleeper's roster_ids happen to iterate in. A combination only
    # survives if it also passes _recipient_need_fit: an offer the
    # recipient's own roster has no actual use for is dropped here rather
    # than proposed and rated poorly, since it was never a real trade.
    Candidate = tuple  # (their_roster, target_entry, offer_players, offer_picks, fit, rating, reasons, profile, my_total, target_value)
    raw_candidates: list[Candidate] = []
    for their_roster in other_rosters:
        if not their_roster.entries:
            continue
        buy_low = identify_buy_low(their_roster, my_status)
        need_candidates = [e for e in buy_low if e.position in target_positions][:MAX_CANDIDATES_PER_OPPONENT]
        if not need_candidates:
            continue
        their_status_result = classify_team_status(their_roster.roster_id, rosters, currency, storage=storage, engine=engine)
        profile = get_owner_profile(their_roster.owner_username or "", league.name)
        for target_entry in need_candidates:
            target_value = value_for_currency(target_entry.value, currency) or 0
            fitting = _find_fitting_offer(
                my_pool, my_picks, target_value, currency, their_roster, exclude_player_id=target_entry.player_id
            )
            if fitting is None:
                continue  # no combination at this value would be anything but roster clutter for them
            offer_players, offer_picks, fit_notes, all_fit = fitting
            fit = OpponentFit(
                target_is_starter=target_entry.is_starter,
                would_upgrade_their_roster=all_fit,
                fit_notes=fit_notes,
                opponent_status=their_status_result.status,
                status_fit=_status_fit(offer_players, offer_picks, their_status_result.status),
                piece_count=len(offer_players) + len(offer_picks),
            )
            my_total = sum(value_for_currency(e.value, currency) or 0 for e in offer_players) + sum(
                p.value or 0 for p in offer_picks
            )
            ratio = (my_total / target_value) if target_value else float("inf")
            rating, reasons = rate_acceptance(fit, ratio, profile, give=offer_players)
            raw_candidates.append(
                (their_roster, target_entry, offer_players, offer_picks, fit, rating, reasons, profile, my_total, target_value)
            )

    def _rank_key(c: Candidate) -> tuple:
        _, _, _, _, _, rating, _, _, my_total, target_value = c
        tier_idx = ACCEPTANCE_TIERS.index(rating)
        ratio_gap = abs((my_total / target_value) - 1.0) if target_value else 999.0
        return (-tier_idx, ratio_gap)  # best acceptance tier first, tightest value match as tiebreak

    raw_candidates.sort(key=_rank_key)

    proposals: list[TradeProposal] = []
    targeted_owners: set[str] = set()
    used_player_ids: set[str] = set()
    used_pick_keys: set[tuple] = set()
    for their_roster, target_entry, offer_players, offer_picks, fit, rating, reasons, profile, my_total, target_value in raw_candidates:
        if len(proposals) >= max_proposals:
            break
        owner_key = their_roster.owner_username or ""
        if owner_key in targeted_owners:
            continue
        piece_collision = any(e.player_id in used_player_ids for e in offer_players) or any(
            _pick_key(p) in used_pick_keys for p in offer_picks
        )
        if piece_collision:
            # A higher-ranked proposal already spent one of this combo's
            # pieces — don't just drop this opponent, since a different,
            # still-viable combo for the SAME target may exist in the
            # remainder of my_pool (two opponents' best-fit offers landing
            # on the identical give-piece is common when bench-filler
            # values cluster tightly, which they do in most leagues).
            remaining = [e for e in my_pool if e.player_id not in used_player_ids]
            remaining_picks = [p for p in my_picks if _pick_key(p) not in used_pick_keys]
            retry = _find_fitting_offer(
                remaining, remaining_picks, target_value, currency, their_roster, exclude_player_id=target_entry.player_id
            )
            if retry is None:
                continue
            offer_players, offer_picks, retry_notes, retry_all_fit = retry
            fit = OpponentFit(
                target_is_starter=fit.target_is_starter,
                would_upgrade_their_roster=retry_all_fit,
                fit_notes=retry_notes,
                opponent_status=fit.opponent_status,
                status_fit=_status_fit(offer_players, offer_picks, fit.opponent_status),
                piece_count=len(offer_players) + len(offer_picks),
            )
            my_total = sum(value_for_currency(e.value, currency) or 0 for e in offer_players) + sum(
                p.value or 0 for p in offer_picks
            )
            ratio = (my_total / target_value) if target_value else float("inf")
            rating, reasons = rate_acceptance(fit, ratio, profile, give=offer_players)

        rationale_mine, rationale_theirs, caveats = _build_rationale(
            target_entry, offer_players, currency, profile, status_result, my_roster, their_roster, offer_picks
        )
        if fit.piece_count >= 2:
            caveats.append(
                "This is a multi-piece offer — some managers read fragmented value as a lowball; "
                "a single clean piece close in value may land better if you have one available."
            )
        for note in fit.fit_notes:
            caveats.append(f"{note} — this piece may read as a throw-in rather than real value to them.")
        confidence = proposal_confidence([target_entry.value, *(e.value for e in offer_players)])

        proposal = TradeProposal(
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
            trade_type="buy_low",
            acceptance_rating=rating,
            acceptance_reasons=reasons,
            confidence=confidence,
        )
        proposal.message = generate_trade_message(
            proposal, fit,
            benefit_reason=_benefit_reason(
                their_roster, offer_players, currency, exclude_player_id=target_entry.player_id, fit=fit
            ),
            timeline_clause=_timeline_clause(fit),
            buzz_clause=_buzz_clause_buy_low(target_entry, currency),
            my_interest_clause=_my_interest_clause(
                my_roster, target_entry.position, target_entry.value, currency,
                exclude_ids=frozenset(e.player_id for e in offer_players),
            ),
        )
        proposals.append(proposal)

        targeted_owners.add(owner_key)
        used_player_ids.update(e.player_id for e in offer_players)
        used_pick_keys.update(_pick_key(p) for p in offer_picks)

    # -- Pass 2: sell-high. Shop MY rising/valuable players to whichever
    # opponent has a real need there, mirroring the buy-low flow but
    # initiated from my side — previously identify_sell_high was fully
    # dead code with zero callers, so this half of the tool's own stated
    # design ("sell before regression, buy before recovery") never fired.
    if len(proposals) < max_proposals:
        sell_candidates = [e for e in identify_sell_high(my_roster) if e.player_id not in used_player_ids]
        for sell_entry in sell_candidates:
            if len(proposals) >= max_proposals:
                break
            sell_value = value_for_currency(sell_entry.value, currency) or 0
            if not sell_value:
                continue
            for their_roster in other_rosters:
                owner_key = their_roster.owner_username or ""
                if owner_key in targeted_owners or not their_roster.entries:
                    continue
                their_needs = identify_needs(their_roster)
                if sell_entry.position not in their_needs[:2]:
                    continue  # not a real need for them — don't pitch a sell into a position they don't need
                their_status_result = classify_team_status(
                    their_roster.roster_id, rosters, currency, storage=storage, engine=engine
                )
                their_pool = _tradeable_pool(their_roster, their_status_result.status)
                their_picks_sorted = sorted((valued_picks or {}).get(their_roster.roster_id, []), key=lambda p: -(p.value or 0))
                fitting = _find_fitting_offer(
                    their_pool, their_picks_sorted, sell_value, currency, my_roster, exclude_player_id=sell_entry.player_id
                )
                if fitting is None:
                    continue  # nothing they'd send back would actually help my roster either
                offer_players, offer_picks, my_side_fit_notes, _my_side_all_fit = fitting
                # would_upgrade_their_roster is about THEM, not me — `fitting`'s
                # all_fit describes whether their return package fits MY
                # roster (a separate, legitimate check kept above so I'm not
                # offered garbage back). Whether sell_entry itself is a real
                # upgrade for THEIR roster needs its own, correctly-directed
                # check.
                their_side_fits = _piece_fits(their_roster, sell_entry, currency)
                profile = get_owner_profile(owner_key, league.name)
                fit = OpponentFit(
                    target_is_starter=any(e.is_starter for e in offer_players),
                    would_upgrade_their_roster=their_side_fits,
                    fit_notes=my_side_fit_notes,
                    opponent_status=their_status_result.status,
                    status_fit=_status_fit([sell_entry], [], their_status_result.status),
                    piece_count=len(offer_players) + len(offer_picks),
                )
                their_total = sum(value_for_currency(e.value, currency) or 0 for e in offer_players) + sum(
                    p.value or 0 for p in offer_picks
                )
                ratio = (sell_value / their_total) if their_total else float("inf")
                rating, reasons = rate_acceptance(fit, ratio, profile, give=[sell_entry])
                rationale_mine, rationale_theirs, caveats = _build_sell_high_rationale(
                    sell_entry, offer_players, currency, profile, status_result, my_roster, their_roster, offer_picks
                )
                if not their_side_fits:
                    caveats.append(
                        f"{sell_entry.name} may not clearly beat their existing {sell_entry.position} depth — "
                        "this pitch leans on their stated need more than a guaranteed upgrade."
                    )
                for note in my_side_fit_notes:
                    caveats.append(f"{note} — this piece coming back may read as a throw-in.")
                confidence = proposal_confidence([sell_entry.value, *(e.value for e in offer_players)])
                proposal = TradeProposal(
                    league_name=league.name,
                    currency=currency,
                    target_username=their_roster.owner_username or "unknown",
                    target_team_name=their_roster.team_name,
                    give=[sell_entry],
                    receive=offer_players,
                    receive_picks=offer_picks,
                    my_value_total=sell_value,
                    their_value_total=their_total,
                    rationale_for_me=rationale_mine,
                    rationale_for_them=rationale_theirs,
                    caveats=caveats,
                    trade_type="sell_high",
                    acceptance_rating=rating,
                    acceptance_reasons=reasons,
                    confidence=confidence,
                )
                my_interest_clause = None
                if offer_players:
                    primary_return = max(offer_players, key=lambda e: _need_percentile(e.value, currency) or 0)
                    my_interest_clause = _my_interest_clause(
                        my_roster, primary_return.position, primary_return.value, currency,
                        exclude_ids=frozenset({sell_entry.player_id}),
                    )
                elif offer_picks:
                    my_interest_clause = "figured picks are useful for me too right now"
                proposal.message = generate_trade_message(
                    proposal, fit,
                    benefit_reason=_benefit_reason(
                        their_roster, [sell_entry], currency,
                        exclude_player_ids=frozenset(e.player_id for e in offer_players), fit=fit,
                    ),
                    timeline_clause=_timeline_clause(fit),
                    buzz_clause=_buzz_clause_sell_high(sell_entry, currency),
                    my_interest_clause=my_interest_clause,
                )
                proposals.append(proposal)
                targeted_owners.add(owner_key)
                used_player_ids.update(e.player_id for e in offer_players)
                used_pick_keys.update(_pick_key(p) for p in offer_picks)
                break  # one sell-high pitch per selling candidate

    # -- Pass 3: pick-target. REBUILD-specific: ask for a pick instead of a
    # player, when nothing above already filled the proposal budget.
    if my_status == REBUILD and valued_picks and len(proposals) < max_proposals:
        # Pass 1/2 already committed some of my_pool to other proposals --
        # without excluding those, the SAME player could be proposed away
        # twice in one report (once here, once in an earlier pass).
        remaining_pool = [e for e in my_pool if e.player_id not in used_player_ids]
        for their_roster in other_rosters:
            if (their_roster.owner_username or "") in targeted_owners:
                continue
            their_picks = valued_picks.get(their_roster.roster_id, [])
            pick_proposal = _build_pick_target_proposal(
                league, their_roster, their_picks, remaining_pool, currency, status_result,
                rosters=rosters, storage=storage, engine=engine,
            )
            if pick_proposal is not None:
                proposals.append(pick_proposal)
                break

    return proposals
