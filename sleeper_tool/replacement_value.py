"""League Replacement Value — a player's value relative to what THIS league
can actually replace him with, rather than his generic rank.

This is not WAR and is never described as wins added. Two replacement
levels per position (QB/RB/WR/TE), both in projected points per week:

  waiver_replacement_projection   the best unrostered, startable player at
                                  the position (the same free-agent pool
                                  the waiver/insurance features use)
  starter_replacement_projection  the lowest-projected player at the
                                  position currently occupying ANY starting
                                  slot — dedicated or flex — across the
                                  league's optimized lineups, ignoring
                                  starters who project below the best free
                                  agent (an abandoned or injured roster's
                                  placeholder is not the league's
                                  replacement level; that roster simply
                                  hasn't picked the free agent up)

For each rostered player: projection over each of those, and value over
the waiver replacement in the league's currency (dynasty value or
projected points). None where an input is missing — a position with no
startable free agent has no waiver replacement, not a zero.

Scarcity is league-relative, from the gap between the two levels: how far
the best free agent sits below the worst current starter, as a share of
that starter's projection. Nothing says "QB is scarce in Superflex"; a
Superflex league's second starting QB slot makes the worst starting QB a
weak one and the best free-agent QB far below him, so the gap does.
  Abundant     gap <= ABUNDANT_MAX_GAP     (a free agent is nearly a starter)
  Normal       gap <= NORMAL_MAX_GAP
  Scarce       gap <= SCARCE_MAX_GAP
  Very Scarce  larger, or no free agent at all
A position the league doesn't start (no slot can hold it) is skipped.

V1 surfaces annotations only; no existing formula is rewritten around it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.asset_value import value_currency, value_for_currency
from sleeper_tool.lineup_optimizer import LineupResult, optimize_lineup, projection_of, slot_eligibility, starter_slots_for
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.valuation import CORE_SKILL_POSITIONS, composite_overall_rank, games_remaining

ABUNDANT_MAX_GAP = 0.10
NORMAL_MAX_GAP = 0.30
SCARCE_MAX_GAP = 0.50
# "Generic rank understates/overstates replacement advantage": how many
# places a player's rank-by-projection-over-waiver must differ from his
# rank-by-generic-rank within my roster before it's worth saying — and the
# advantage itself must be real (understated) or small (overstated), so a
# player who is merely less-bad than another never makes either list.
RANK_DIVERGENCE_MIN = 3
UNDERSTATED_MIN_OVER_WAIVER = 2.0  # points per week
OVERSTATED_MAX_OVER_WAIVER = 1.0
MAX_HIGHLIGHTED = 3

ABUNDANT = "Abundant"
NORMAL = "Normal"
SCARCE = "Scarce"
VERY_SCARCE = "Very Scarce"
_SCARCITY_ORDER = {VERY_SCARCE: 0, SCARCE: 1, NORMAL: 2, ABUNDANT: 3}


@dataclass
class PositionMarket:
    position: str
    waiver_replacement: RosterEntry | None  # best startable free agent, or None
    waiver_replacement_projection: float | None  # per week
    starter_replacement: RosterEntry | None  # worst current starter at the position league-wide
    starter_replacement_projection: float | None  # per week
    scarcity: str
    gap: float | None  # (starter - waiver) / starter

    def describe(self) -> str:
        if self.waiver_replacement is None:
            return f"{self.position}: {self.scarcity} — no startable free agent at all"
        starter = (
            f"worst current starter {self.starter_replacement.name} at {self.starter_replacement_projection:.1f}/wk"
            if self.starter_replacement is not None
            else "no current starter league-wide"
        )
        return (
            f"{self.position}: {self.scarcity} — best free agent {self.waiver_replacement.name} projects "
            f"{self.waiver_replacement_projection:.1f}/wk vs {starter}"
        )


@dataclass
class PlayerReplacementContext:
    entry: RosterEntry
    weekly_projection: float | None
    projection_over_waiver: float | None
    projection_over_starter_replacement: float | None
    value_over_waiver: float | None
    scarcity: str  # of his position in this league

    def clause(self) -> str | None:
        """A short inline annotation, or None when nothing is measurable."""
        if self.projection_over_waiver is None:
            return f"{self.entry.position} market is {self.scarcity}"
        pow_ = round(self.projection_over_waiver, 1) + 0.0  # + 0.0 turns -0.0 into 0.0
        if pow_ < 0:
            return f"{-pow_:.1f}/wk below the best free-agent {self.entry.position} ({self.scarcity} market)"
        return f"+{pow_:.1f}/wk over the best free-agent {self.entry.position} ({self.scarcity} market)"


@dataclass
class ReplacementMarket:
    positions: dict[str, PositionMarket]
    players: dict[str, PlayerReplacementContext]  # my rostered players by player_id
    understated: list[PlayerReplacementContext] = field(default_factory=list)  # generic rank understates replacement advantage
    overstated: list[PlayerReplacementContext] = field(default_factory=list)

    def scarcity_of(self, position: str | None) -> str | None:
        m = self.positions.get(position or "")
        return m.scarcity if m else None

    def scarcest(self) -> list[PositionMarket]:
        return sorted(self.positions.values(), key=lambda m: _SCARCITY_ORDER[m.scarcity])


def scarcity_label(gap: float | None) -> str:
    if gap is None:
        return VERY_SCARCE
    if gap <= ABUNDANT_MAX_GAP:
        return ABUNDANT
    if gap <= NORMAL_MAX_GAP:
        return NORMAL
    if gap <= SCARCE_MAX_GAP:
        return SCARCE
    return VERY_SCARCE


def _startable_positions(roster: ValuedRoster) -> set[str]:
    eligible: set[str] = set()
    for slot in starter_slots_for(roster):
        eligible |= slot_eligibility(slot)
    return eligible & set(CORE_SKILL_POSITIONS)


def build_replacement_market(
    my_roster: ValuedRoster,
    rosters: dict[int, ValuedRoster],
    free_agents: list[RosterEntry],
    *,
    current_week: int | None,
    lineups: dict[int, LineupResult] | None = None,
) -> ReplacementMarket:
    """`lineups` may carry already-optimized lineups by roster_id; the rest
    are optimized here (structural lineups, so a starter on bye this week
    is still the starter he replaces)."""
    per_week = games_remaining(current_week)
    currency = value_currency(my_roster)
    lineups = dict(lineups or {})
    for rid, r in rosters.items():
        if rid not in lineups and r.entries:
            lineups[rid] = optimize_lineup(r)
    by_id = {rid: {e.player_id: e for e in r.entries} for rid, r in rosters.items()}

    positions: dict[str, PositionMarket] = {}
    for pos in sorted(_startable_positions(my_roster), key=CORE_SKILL_POSITIONS.index):
        fas = [fa for fa in free_agents if fa.position == pos and fa.value.proj_points is not None]
        best_fa = max(fas, key=projection_of) if fas else None
        fa_proj = projection_of(best_fa) if best_fa is not None else None
        starters: list[tuple[float, RosterEntry]] = []
        for rid, lineup in lineups.items():
            for a in lineup.assignments:
                e = by_id[rid].get(a.player_id)
                if e is not None and e.position == pos and e.value.proj_points is not None:
                    starters.append((a.projection, e))
        real = [s for s in starters if fa_proj is None or s[0] >= fa_proj] or starters
        worst_proj, worst_starter = min(real, key=lambda s: (s[0], s[1].name)) if real else (None, None)
        waiver_weekly = fa_proj / per_week if fa_proj is not None else None
        starter_weekly = worst_proj / per_week if worst_proj is not None else None
        gap = None
        if waiver_weekly is not None and starter_weekly:
            gap = max(0.0, (starter_weekly - waiver_weekly) / starter_weekly)
        positions[pos] = PositionMarket(
            position=pos, waiver_replacement=best_fa, waiver_replacement_projection=waiver_weekly,
            starter_replacement=worst_starter, starter_replacement_projection=starter_weekly,
            scarcity=scarcity_label(gap), gap=gap,
        )

    market = ReplacementMarket(positions=positions, players={})
    for e in my_roster.entries:
        ctx = player_context(market, e, currency=currency, per_week=per_week)
        if ctx is not None:
            market.players[e.player_id] = ctx
    market.understated, market.overstated = _rank_divergence(market.players, currency)
    return market


def player_context(
    market: ReplacementMarket, entry: RosterEntry, *, currency: str, per_week: int
) -> PlayerReplacementContext | None:
    """Replacement context for ANY player (mine, a trade target on another
    roster, a free agent) against this league's markets. None when the
    league doesn't start his position."""
    m = market.positions.get(entry.position or "")
    if m is None:
        return None
    weekly = entry.value.proj_points / per_week if entry.value.proj_points is not None else None
    pow_ = weekly - m.waiver_replacement_projection if weekly is not None and m.waiver_replacement_projection is not None else None
    posr = weekly - m.starter_replacement_projection if weekly is not None and m.starter_replacement_projection is not None else None
    my_value = value_for_currency(entry.value, currency)
    fa_value = value_for_currency(m.waiver_replacement.value, currency) if m.waiver_replacement is not None else None
    vow = my_value - fa_value if my_value is not None and fa_value is not None else None
    return PlayerReplacementContext(entry, weekly, pow_, posr, vow, m.scarcity)


def _rank_divergence(
    players: dict[str, PlayerReplacementContext], currency: str
) -> tuple[list[PlayerReplacementContext], list[PlayerReplacementContext]]:
    """Within my roster: order by generic (reconciled) rank and by projection
    over waiver; a player far better by the second than the first is one
    whose rank understates what he's worth HERE, and vice versa."""
    measurable = [
        p for p in players.values()
        if p.projection_over_waiver is not None and composite_overall_rank(p.entry.value, currency) is not None
    ]
    if len(measurable) < 2:
        return [], []
    by_generic = sorted(measurable, key=lambda p: composite_overall_rank(p.entry.value, currency))
    by_pow = sorted(measurable, key=lambda p: -p.projection_over_waiver)
    generic_pos = {p.entry.player_id: i for i, p in enumerate(by_generic)}
    pow_pos = {p.entry.player_id: i for i, p in enumerate(by_pow)}
    understated = [
        p for p in measurable
        if generic_pos[p.entry.player_id] - pow_pos[p.entry.player_id] >= RANK_DIVERGENCE_MIN
        and p.projection_over_waiver >= UNDERSTATED_MIN_OVER_WAIVER
    ]
    overstated = [
        p for p in measurable
        if pow_pos[p.entry.player_id] - generic_pos[p.entry.player_id] >= RANK_DIVERGENCE_MIN
        and p.projection_over_waiver <= OVERSTATED_MAX_OVER_WAIVER
    ]
    understated.sort(key=lambda p: -(generic_pos[p.entry.player_id] - pow_pos[p.entry.player_id]))
    overstated.sort(key=lambda p: -(pow_pos[p.entry.player_id] - generic_pos[p.entry.player_id]))
    return understated[:MAX_HIGHLIGHTED], overstated[:MAX_HIGHLIGHTED]
