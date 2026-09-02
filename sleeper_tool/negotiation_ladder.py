"""Negotiation Ladder — the plan for AFTER the counter-offer.

The trade engine says what to send. This turns each of a league's top
LADDERS_PER_LEAGUE buy-low / pick-target proposals into three steps,
every one of which still fits the other manager's roster (the same
_recipient_need_fit test the engine applies) and is rated with the same
rate_acceptance rubric:

  Opening    the CHEAPEST package (by my outgoing value) that still rates
             at least MIN_OPENING_ACCEPTANCE — if nothing cheaper than
             the engine's own offer clears that bar, the engine's offer
             is the opening.
  Fallback   the opening plus exactly one added asset, or with exactly
             one asset substituted, that improves the acceptance rating
             while keeping my outgoing value within MAX_OUTGOING_RATIO of
             the baseline. The baseline is the reconciled value of what I
             RECEIVE in the base proposal, using the engine's own
             valuation of each asset.
  Walk Away  the most expensive package inside that same cap — guidance
             on where to stop, not a fourth offer. Past this line the
             engine is no longer calling the trade acceptable, and a
             "High" rating manufactured by overpaying isn't a win.

Only the opening gets a chat message; fallback and walk-away are
internal decision support. Sell-high proposals get no ladder: there I'm
the seller, the counter-offer dynamic inverts (the fallback is accepting
less, not paying more), and a ladder built on "what else do I add" would
be the wrong tool.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations

from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.owner_profiles import get_owner_profile
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.trade_engine import (
    ACCEPTANCE_TIERS,
    DYNASTY_CURRENCY,
    OpponentFit,
    TradeProposal,
    _pick_key,
    _recipient_need_fit,
    _status_fit,
    _tradeable_pool,
    generate_trade_message,
    rate_acceptance,
    value_for_currency,
)

LADDERS_PER_LEAGUE = 2
MIN_OPENING_ACCEPTANCE = "Moderate"
MAX_OUTGOING_RATIO = 1.10  # fallback / walk-away ceiling vs the baseline (value received)
MIN_OUTGOING_RATIO = 0.50  # don't bother evaluating insulting packages
LOWBALL_RATIO = 0.85  # below this the opening is flagged as a deliberate lowball (rate_acceptance's own cutoff)
MAX_PIECES = 2
LADDERED_TRADE_TYPES = frozenset({"buy_low", "pick_target"})

OPENING = "Opening"
FALLBACK = "Fallback"
WALK_AWAY = "Walk Away"


@dataclass
class LadderStep:
    name: str
    players: list[RosterEntry]
    picks: list[OwnedPick]
    outgoing_value: float
    ratio: float  # outgoing / baseline
    acceptance: str
    reasons: list[str]

    @property
    def asset_names(self) -> str:
        # Two different picks can share a display name ("2028 Late 4th" from
        # two original teams) — show "×2" rather than a confusing repeat.
        names = [*(e.name for e in self.players), *(p.name for p in self.picks)]
        counts: dict[str, int] = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        return ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in counts.items())

    @property
    def lowball(self) -> bool:
        return self.ratio < LOWBALL_RATIO

    def key(self) -> frozenset:
        return frozenset([*(("player", e.player_id) for e in self.players), *(("pick", _pick_key(p)) for p in self.picks)])


@dataclass
class NegotiationLadder:
    baseline_value: float
    opening: LadderStep
    fallback: LadderStep | None
    walk_away: LadderStep | None
    opening_message: str


def _tier(rating: str) -> int:
    return ACCEPTANCE_TIERS.index(rating)


def _packages(pool: list[RosterEntry], picks: list[OwnedPick], currency: str):
    items: list[tuple[str, object, float]] = [("player", e, value_for_currency(e.value, currency) or 0) for e in pool]
    if currency == DYNASTY_CURRENCY:
        items += [("pick", p, float(p.value)) for p in picks if p.value]
    for size in range(1, MAX_PIECES + 1):
        for combo in combinations(items, size):
            yield (
                [it[1] for it in combo if it[0] == "player"],
                [it[1] for it in combo if it[0] == "pick"],
                sum(it[2] for it in combo),
            )


def build_negotiation_ladder(
    proposal: TradeProposal,
    my_roster: ValuedRoster,
    their_roster: ValuedRoster,
    my_picks: list[OwnedPick],
    *,
    my_status: str,
    their_status: str,
) -> NegotiationLadder | None:
    if proposal.trade_type not in LADDERED_TRADE_TYPES:
        return None
    baseline = proposal.their_value_total
    if not baseline:
        return None
    currency = proposal.currency
    target = proposal.receive[0] if proposal.receive else None
    profile = get_owner_profile(their_roster.owner_username or "", proposal.league_name)
    pool = _tradeable_pool(my_roster, my_status)

    steps: list[LadderStep] = []
    for players, picks, outgoing in _packages(pool, my_picks, currency):
        ratio = outgoing / baseline
        if ratio < MIN_OUTGOING_RATIO or ratio > MAX_OUTGOING_RATIO:
            continue
        any_fit, all_fit, notes = _recipient_need_fit(
            their_roster, players, currency, exclude_player_id=target.player_id if target else None
        )
        if not any_fit:
            continue
        fit = OpponentFit(
            target_is_starter=bool(target and target.is_starter),
            would_upgrade_their_roster=all_fit,
            fit_notes=notes,
            opponent_status=their_status,
            status_fit=_status_fit(players, picks, their_status),
            piece_count=len(players) + len(picks),
        )
        rating, reasons = rate_acceptance(fit, ratio, profile, give=players)
        steps.append(LadderStep("", players, picks, outgoing, ratio, rating, reasons))
    if not steps:
        return None

    base_key = frozenset([*(("player", e.player_id) for e in proposal.give), *(("pick", _pick_key(p)) for p in proposal.give_picks)])
    base_step = next((s for s in steps if s.key() == base_key), None)
    openable = [s for s in steps if _tier(s.acceptance) >= _tier(MIN_OPENING_ACCEPTANCE)]
    opening = min(openable, key=lambda s: (s.outgoing_value, -_tier(s.acceptance))) if openable else base_step
    if opening is None:
        return None
    opening = replace(opening, name=OPENING)

    def _one_move_away(s: LadderStep) -> bool:
        a, b = opening.key(), s.key()
        added, removed = b - a, a - b
        return (len(added) == 1 and not removed) or (len(added) == 1 and len(removed) == 1)

    fallback_pool = [
        s for s in steps
        if _one_move_away(s) and s.outgoing_value > opening.outgoing_value and _tier(s.acceptance) > _tier(opening.acceptance)
    ]
    fallback = None
    if fallback_pool:
        fallback = replace(max(fallback_pool, key=lambda s: (_tier(s.acceptance), -s.outgoing_value)), name=FALLBACK)

    # The ceiling is the most expensive package the engine still rates
    # acceptable — a Low-rated package can't be "the most acceptable",
    # however much it costs. Omitted when it's just the opening or the
    # fallback again (the renderer then says the fallback IS the ceiling).
    acceptable = [s for s in steps if _tier(s.acceptance) >= _tier(MIN_OPENING_ACCEPTANCE)]
    walk_away = None
    if acceptable:
        ceiling = max(acceptable, key=lambda s: (s.outgoing_value, _tier(s.acceptance)))
        taken = {opening.key()} | ({fallback.key()} if fallback else set())
        if ceiling.key() not in taken:
            walk_away = replace(ceiling, name=WALK_AWAY)

    if opening.key() == base_key and proposal.message:
        message = proposal.message
    else:
        shadow = replace(proposal, give=opening.players, give_picks=opening.picks, my_value_total=opening.outgoing_value)
        message = generate_trade_message(shadow)
    return NegotiationLadder(baseline_value=baseline, opening=opening, fallback=fallback, walk_away=walk_away, opening_message=message)


def build_ladders(
    proposals: list[TradeProposal],
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    my_picks: list[OwnedPick],
    *,
    my_status: str,
    status_of: dict[int, str],
) -> dict[int, NegotiationLadder]:
    """Ladders for the league's top LADDERS_PER_LEAGUE ladder-able
    proposals, keyed by proposal index. `status_of` maps roster_id to the
    counterparty's contender/middling/rebuild status."""
    ladders: dict[int, NegotiationLadder] = {}
    by_username = {r.owner_username: r for r in rosters.values() if r.owner_username}
    for i, p in enumerate(proposals):
        if len(ladders) >= LADDERS_PER_LEAGUE:
            break
        their = by_username.get(p.target_username)
        if their is None:
            continue
        ladder = build_negotiation_ladder(
            p, my_roster, their, my_picks, my_status=my_status, their_status=status_of.get(their.roster_id, "middling")
        )
        if ladder is not None:
            ladders[i] = ladder
    return ladders
