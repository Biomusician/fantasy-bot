"""Waiver Drops — "who should I drop to make room?", answered for the whole
bench at once rather than per add.

waiver_engine.find_drop_candidate answers per row and looks at the add's
own position first, which is how a 6th-percentile QB came paired with a
starting-calibre TE and how a same-position backup kept being cut ahead of
a dead bench spot elsewhere. Here every droppable player is ordered by
what he is worth to THIS roster, and the pairing with a specific add is
checked later against the lineup (waiver_acquisition).

Never offered as a drop (each is a documented protection, not a score):
  - a structural optimizer starter, or anyone who starts in the claim-week
    lineup (a bye fill-in is a starter that week)
  - IR / reserve and taxi players — dropping them frees no bench spot
  - a live trade give-piece (the report is already pitching him for value)
  - a dynasty developmental player (roster_clog's definition)
  - an injured player the league has an open IR slot for — the move is IR,
    not a drop
A player whose projection is missing BECAUSE he is unavailable (Out, IR,
PUP, NA, suspended) sorts LAST rather than first: the blank is a data gap,
not a football fact, and "cut the top-12 back whose feed went blank" is
exactly the advice a projection-ordered board would give. He is still
droppable — a roster with nothing else to cut has to cut someone — but the
claim that pairs him says what the blank means.

Everyone else is ordered cheapest-to-drop first by, in order:
  1. ROS starter calibre — FantasyPros ROS positional rank inside this
     league's startable depth (teams x starting demand at the position).
     An injured starter with no weekly projection still has one.
  2. Cover value — how much worse my best emergency fill at his position
     gets without him, per week; at or above MATERIAL_COVER_POINTS he is
     real depth.
  3. Weekly projection.
  4. ROS positional rank (worse rank drops first).
Every step is a comparison a reader can check. No composite score.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from sleeper_tool.asset_value import value_currency
from sleeper_tool.lineup_optimizer import LineupResult
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.roster_clog import is_dynasty_developmental
from sleeper_tool.valuation import games_remaining

MATERIAL_COVER_POINTS = 1.5  # per week: losing this much emergency cover is a real cost
STARTABLE_DEPTH_MARGIN = 1.0  # ROS rank inside teams x demand x this is starter calibre

PROTECT_STARTER = "optimized starter"
PROTECT_WEEK_STARTER = "starts in the claim-week lineup"
PROTECT_RESERVE = "in an IR/reserve or taxi slot (dropping him frees no bench spot)"
PROTECT_TRADE_PIECE = "live trade give-piece"
PROTECT_DEVELOPMENTAL = "dynasty developmental hold"
PROTECT_IR_ELIGIBLE = "IR-eligible with an open IR slot — move him there instead of dropping"
# Which designations an IR slot accepts is a LEAGUE SETTING, not a league of
# the game. Disco carries two IR slots and reserve_allow_out = 0, so "move him
# to IR instead" about an Out player is advice the manager cannot take — and
# worse, it protects him from a drop for a reason that does not exist. IR and
# PUP are the baseline (Sleeper has no flag for them); everything else is a
# flag the API reports per league.
BASE_IR_STATUSES = frozenset({"IR", "PUP"})
RESERVE_ALLOW_FLAGS = {
    "Out": "reserve_allow_out",
    "Doubtful": "reserve_allow_doubtful",
    "NA": "reserve_allow_na",
    "Sus": "reserve_allow_sus",
    "DNR": "reserve_allow_dnr",
    "COV": "reserve_allow_cov",
}
# Every designation any league could allow — the default when no settings are
# supplied, which keeps a caller that has none from silently protecting nobody.
IR_ELIGIBLE_STATUSES = BASE_IR_STATUSES | frozenset(RESERVE_ALLOW_FLAGS)


def ir_eligible_statuses(settings: dict | None) -> frozenset[str]:
    """The injury designations THIS league's IR slots accept."""
    if not settings:
        return IR_ELIGIBLE_STATUSES
    allowed = set(BASE_IR_STATUSES)
    for status, flag in RESERVE_ALLOW_FLAGS.items():
        if settings.get(flag):
            allowed.add(status)
    return frozenset(allowed)


@dataclass
class DropOption:
    entry: RosterEntry
    weekly_projection: float | None
    cover_value: float  # per week; 0.0 when someone else on the bench covers as well
    ros_pos_rank: int | None
    ros_starter_calibre: bool
    reasons: list[str] = field(default_factory=list)  # why he is cheap (or not) to drop, in order
    status_caution: str | None = None  # his projection is missing because Sleeper says he is unavailable

    @property
    def is_dead_spot(self) -> bool:
        """No starter calibre and no real cover: the roster spot an upside
        stash is allowed to take."""
        return not self.ros_starter_calibre and self.cover_value < MATERIAL_COVER_POINTS

    def describe(self) -> str:
        return f"{self.entry.name} ({self.entry.position or '?'})" + (f" — {'; '.join(self.reasons)}" if self.reasons else "")


@dataclass
class DropBoard:
    options: list[DropOption]  # droppable, cheapest first
    protected: dict[str, str]  # player_id -> the protection that applies
    open_spots: int = 0

    def option_for(self, player_id: str) -> DropOption | None:
        return next((o for o in self.options if o.entry.player_id == player_id), None)


def _unavailable_status(entry: RosterEntry) -> str | None:
    """The Sleeper designation that explains a missing projection, if any."""
    if entry.injury_status:
        return entry.injury_status
    return entry.status if entry.status not in (None, "Active") else None


def startable_depth(roster: ValuedRoster, num_teams: int) -> dict[str, int]:
    """How deep a position runs before it stops being startable in this
    league: teams x the league's starting demand at the position (flex
    demand already distributed by valuation.derive_league_format)."""
    return {
        pos: max(1, math.ceil(num_teams * demand * STARTABLE_DEPTH_MARGIN))
        for pos, demand in roster.fmt.starter_slots.items()
    }


def build_drop_board(
    my_roster: ValuedRoster,
    *,
    lineup: LineupResult,
    week_lineup: LineupResult | None = None,
    num_teams: int,
    ros_pos_rank: Callable[[RosterEntry], int | None] = lambda e: None,
    trade_piece_ids: Collection[str] = (),
    current_week: int | None = None,
    open_spots: int = 0,
    reserve_slots: int = 0,
    league_settings: dict | None = None,
) -> DropBoard:
    """`reserve_slots` is the league's IR capacity — Sleeper reports it as
    `settings.reserve_slots`, never as an IR entry in roster_positions.
    `league_settings` is that same settings dict, read for which injury
    designations this league's IR slots actually accept."""
    per_week = games_remaining(current_week)
    currency = value_currency(my_roster)
    depth = startable_depth(my_roster, num_teams)
    starters = set(lineup.starter_ids)
    open_ir = max(0, reserve_slots - sum(1 for e in my_roster.entries if e.is_reserve))
    ir_statuses = ir_eligible_statuses(league_settings)
    week_starters = set(week_lineup.starter_ids) if week_lineup is not None else set()
    trade_pieces = set(trade_piece_ids)

    protected: dict[str, str] = {}
    pool: list[RosterEntry] = []
    for e in my_roster.entries:
        if e.player_id in starters:
            protected[e.player_id] = PROTECT_STARTER
        elif e.player_id in week_starters:
            protected[e.player_id] = PROTECT_WEEK_STARTER
        elif e.is_reserve or e.is_taxi:
            protected[e.player_id] = PROTECT_RESERVE
        elif e.player_id in trade_pieces:
            protected[e.player_id] = PROTECT_TRADE_PIECE
        elif is_dynasty_developmental(e, currency):
            protected[e.player_id] = PROTECT_DEVELOPMENTAL
        elif open_ir and e.injury_status in ir_statuses:
            protected[e.player_id] = PROTECT_IR_ELIGIBLE
        else:
            pool.append(e)

    # Cover is measured against the whole available bench, not just the
    # droppable pool: a protected bench player still covers.
    bench = [e for e in my_roster.entries if e.player_id not in starters and not (e.is_reserve or e.is_taxi)]

    def weekly(e: RosterEntry) -> float | None:
        return e.value.proj_points / per_week if e.value.proj_points is not None else None

    options: list[DropOption] = []
    for e in pool:
        pw = weekly(e)
        others = [weekly(o) or 0.0 for o in bench if o.player_id != e.player_id and o.position == e.position]
        next_best = max(others, default=0.0)
        # A position the league never starts is covered by nobody and covers nothing.
        cover = max(0.0, (pw or 0.0) - next_best) if e.position in depth else 0.0
        rank = ros_pos_rank(e)
        calibre = rank is not None and e.position in depth and rank <= depth[e.position]
        reasons: list[str] = []
        if calibre:
            reasons.append(f"ROS {e.position}{rank} — starter calibre in a {num_teams}-team league")
        if cover >= MATERIAL_COVER_POINTS:
            reasons.append(f"your best {e.position} cover by {cover:.1f}/wk")
        elif pw is not None:
            reasons.append(f"{pw:.1f}/wk" + (", and another bench player covers his position as well" if others else ""))
        else:
            status = _unavailable_status(e)
            reasons.append(
                f"no projection in this league's sources (Sleeper: {status}) — confirm he isn't returning soon"
                if status else "no projection in this league's sources"
            )
        caution = (
            f"no projection because Sleeper has him {_unavailable_status(e)} — confirm he isn't returning before you cut him"
            if pw is None and _unavailable_status(e) else None
        )
        options.append(DropOption(e, pw, round(cover, 2), rank, calibre, reasons, caution))

    options.sort(key=lambda o: (
        o.ros_starter_calibre,
        o.status_caution is not None,  # a blank projection an injury explains is the LAST thing to cut
        o.cover_value >= MATERIAL_COVER_POINTS,
        o.weekly_projection if o.weekly_projection is not None else -1.0,
        -(o.ros_pos_rank or 10_000),
        o.entry.name,
    ))
    return DropBoard(options=options, protected=protected, open_spots=max(0, open_spots))
