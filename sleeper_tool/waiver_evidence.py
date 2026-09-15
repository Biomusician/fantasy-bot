"""Waiver Evidence — what every source says about ONE available player, in
each source's own terms, plus the descriptive labels that fall out of
comparing them.

There is no universal "expert score" here and nothing is averaged. A
Ballers #3, a RotoBaller #11, a FantasyPros ROS WR41 and a Boone WR38
answer different questions (waiver priority this week, an independent
waiver board, rest-of-season consensus, one analyst's weekly ranks), so
the matrix keeps them side by side and the labels say only what the
comparison literally shows:

  Broad Analyst Conviction  BOTH waiver-specific boards rank him inside
                            WAIVER_TOP, and FantasyPros ROS or Boone has him
                            inside this league's rosterable depth. Two
                            ROS/weekly lists agreeing is never enough.
  Waiver Experts Agree      both waiver boards rank him inside WAIVER_TOP
  Ballers Conviction        Ballers rank inside WAIVER_TOP with Strong host agreement
  Ballers Split             the three hosts' ranks spread more than 12 places
  Waiver Specialists Higher a waiver board has him inside WAIVER_TOP while
                            FantasyPros ROS has him outside rosterable depth
                            (or unranked) — the emerging-role case
  FantasyPros Higher        FantasyPros ROS has him inside startable depth
                            while neither waiver board lists him inside
                            WAIVER_LISTED — the boring known quantity
  Deeper Leagues Only       RotoBaller tags him for leagues larger than this one
  Role Supports Add / Role Lags Hype   usage labels, context only (the role
                            heuristic is annotation-only until redesigned)
  Scarce Position / Replaceable Position   this league's replacement market

Depth thresholds are league-aware: startable depth is teams x starting
demand at the position (waiver_drops.startable_depth); rosterable depth is
ROSTERABLE_DEPTH_MULTIPLE times that. A WR41 is a starter in a 14-team
four-flex league and a waiver body in a 10-team two-flex one.
"""
from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass, field

from sleeper_tool.replacement_value import ABUNDANT, SCARCE, VERY_SCARCE
from sleeper_tool.roster_analysis import RosterEntry
from sleeper_tool.valuation import games_remaining

WAIVER_TOP = 12  # a waiver board's first dozen are its real recommendations
WAIVER_LISTED = 30  # outside this a board is naming depth, not endorsing
ROSTERABLE_DEPTH_MULTIPLE = 1.75

BROAD_CONVICTION = "Broad Analyst Conviction"
EXPERTS_AGREE = "Waiver Experts Agree"
BALLERS_CONVICTION = "Ballers Conviction"
BALLERS_SPLIT = "Ballers Split"
SPECIALISTS_HIGHER = "Waiver Specialists Higher"
FANTASYPROS_HIGHER = "FantasyPros Higher"
DEEPER_LEAGUES_ONLY = "Deeper Leagues Only"
ROLE_SUPPORTS = "Role Supports Add"
ROLE_LAGS = "Role Lags Hype"
SCARCE_POSITION = "Scarce Position"
REPLACEABLE_POSITION = "Replaceable Position"

# Support, strongest first: how much outside opinion backs the add at all.
SUPPORT_BROAD = "broad"
SUPPORT_AGREE = "agree"
SUPPORT_SINGLE = "single waiver board"
SUPPORT_ROS = "ROS consensus only"
SUPPORT_NONE = "none"
SUPPORT_ORDER = {SUPPORT_BROAD: 0, SUPPORT_AGREE: 1, SUPPORT_SINGLE: 2, SUPPORT_ROS: 3, SUPPORT_NONE: 4}

_STRONG_HOSTS = "Strong Ballers Agreement"
_SPLIT_HOSTS = "Ballers Split"
_ROLE_UP = ("Role Rising", "Role Surging")
_ROLE_DOWN = ("Role Falling", "Role Collapsing")


@dataclass
class WaiverEvidence:
    player_id: str
    name: str
    position: str | None
    team: str | None
    weekly_projection: float | None = None
    fp_ros_pos_rank: int | None = None
    fp_ros_overall_rank: int | None = None
    fp_ros_std: float | None = None
    ballers_rank: int | None = None
    ballers_hosts: tuple[int | None, int | None, int | None] = (None, None, None)  # Andy, Jason, Mike
    ballers_agreement: str | None = None
    rotoballer_rank: int | None = None
    rotoballer_note: str | None = None
    rotoballer_min_league_size: int | None = None
    boone_pos_rank: int | None = None
    role_label: str | None = None
    role_market: str | None = None
    velocity_label: str | None = None
    scarcity: str | None = None
    startable_depth: int | None = None
    rosterable_depth: int | None = None
    labels: list[str] = field(default_factory=list)
    support: str = SUPPORT_NONE
    disagreement: bool = False  # the sources pull in different directions (widens a FAAB window)
    for_lines: list[str] = field(default_factory=list)  # source facts that argue for the add
    risk_lines: list[str] = field(default_factory=list)  # source facts that argue against it

    @property
    def best_waiver_rank(self) -> int | None:
        ranks = [r for r in (self.ballers_rank, self.rotoballer_rank) if r is not None]
        return min(ranks) if ranks else None

    @property
    def expert_listed(self) -> bool:
        return self.best_waiver_rank is not None and self.best_waiver_rank <= WAIVER_LISTED

    def pos_label(self, rank: int | None) -> str:
        return f"{self.position}{rank}" if rank is not None and self.position else "—"

    def hosts_text(self) -> str:
        return "/".join(str(r) if r is not None else "—" for r in self.ballers_hosts)


def rosterable_depth(startable: int) -> int:
    return max(startable + 1, math.ceil(startable * ROSTERABLE_DEPTH_MULTIPLE))


def build_evidence(
    entry: RosterEntry,
    *,
    sources,  # waiver_sources.WaiverSources
    ppr: float,
    num_teams: int,
    startable: dict[str, int],
    current_week: int | None = None,
    scarcity: str | None = None,
    role_label: str | None = None,
    role_market: str | None = None,
    velocity_label: str | None = None,
    sources_present: Collection[str] = (),
) -> WaiverEvidence:
    """`sources_present` names the waiver sources that loaded this run
    (waiver_sources.BALLERS / ROTOBALLER / BOONE / FP_ROS), so "not ranked"
    can be told apart from "no board to be ranked on"."""
    pid = entry.player_id
    pos = entry.position
    ev = WaiverEvidence(player_id=pid, name=entry.name, position=pos, team=entry.team)
    if entry.value.proj_points is not None:
        ev.weekly_projection = entry.value.proj_points / games_remaining(current_week)
    fp = sources.fp_ros_for(ppr).get(pid)
    if fp is not None:
        ev.fp_ros_pos_rank, ev.fp_ros_overall_rank, ev.fp_ros_std = fp.pos_rank, fp.overall_rank, fp.rank_std
    b = sources.ballers_by_id.get(pid)
    if b is not None:
        ev.ballers_rank, ev.ballers_hosts, ev.ballers_agreement = b.rank, (b.andy, b.jason, b.mike), b.agreement
    rb = sources.rotoballer_by_id.get(pid)
    if rb is not None:
        ev.rotoballer_rank, ev.rotoballer_note, ev.rotoballer_min_league_size = rb.rank, rb.league_size_note, rb.min_league_size
    boone = sources.boone_for(ppr)
    if boone:
        ev.boone_pos_rank = boone.get(pid)
    ev.role_label, ev.role_market, ev.velocity_label, ev.scarcity = role_label, role_market, velocity_label, scarcity
    ev.startable_depth = startable.get(pos or "")
    ev.rosterable_depth = rosterable_depth(ev.startable_depth) if ev.startable_depth else None
    _label(ev, num_teams=num_teams, sources_present=set(sources_present))
    return ev


def _inside(rank: int | None, depth: int | None) -> bool:
    return rank is not None and depth is not None and rank <= depth


def _label(ev: WaiverEvidence, *, num_teams: int, sources_present: set[str]) -> None:
    from sleeper_tool.waiver_sources import BALLERS, BOONE, FP_ROS, ROTOBALLER

    ballers_top = ev.ballers_rank is not None and ev.ballers_rank <= WAIVER_TOP
    roto_top = ev.rotoballer_rank is not None and ev.rotoballer_rank <= WAIVER_TOP
    fp_supports = _inside(ev.fp_ros_pos_rank, ev.rosterable_depth)
    boone_supports = _inside(ev.boone_pos_rank, ev.rosterable_depth)
    fp_startable = _inside(ev.fp_ros_pos_rank, ev.startable_depth)

    if ballers_top and roto_top:
        if fp_supports or boone_supports:
            ev.labels.append(BROAD_CONVICTION)
            ev.support = SUPPORT_BROAD
        else:
            ev.labels.append(EXPERTS_AGREE)
            ev.support = SUPPORT_AGREE
    elif ballers_top or roto_top:
        ev.support = SUPPORT_SINGLE
    elif fp_startable:
        ev.support = SUPPORT_ROS

    if ballers_top and ev.ballers_agreement == _STRONG_HOSTS:
        ev.labels.append(BALLERS_CONVICTION)
    if ev.ballers_agreement == _SPLIT_HOSTS and ev.ballers_rank is not None and ev.ballers_rank <= WAIVER_LISTED:
        ev.labels.append(BALLERS_SPLIT)
        ev.disagreement = True
    if (ballers_top or roto_top) and FP_ROS in sources_present and not fp_supports:
        ev.labels.append(SPECIALISTS_HIGHER)
        ev.disagreement = True
    listed_by_waiver_board = any(r is not None and r <= WAIVER_LISTED for r in (ev.ballers_rank, ev.rotoballer_rank))
    waiver_boards_loaded = bool({BALLERS, ROTOBALLER} & sources_present)
    if fp_startable and waiver_boards_loaded and not listed_by_waiver_board:
        ev.labels.append(FANTASYPROS_HIGHER)
    if ev.rotoballer_min_league_size is not None and ev.rotoballer_min_league_size > num_teams:
        ev.labels.append(DEEPER_LEAGUES_ONLY)
    if (ballers_top or roto_top) and BOONE in sources_present and ev.boone_pos_rank is None or (
        (ballers_top or roto_top) and ev.boone_pos_rank is not None and not boone_supports
    ):
        ev.disagreement = True
    if ev.role_label in _ROLE_UP or ev.role_market == "Role Ahead of Market":
        ev.labels.append(ROLE_SUPPORTS)
    if (ballers_top or roto_top) and (ev.role_label in _ROLE_DOWN or ev.role_market == "Market Ahead of Role"):
        ev.labels.append(ROLE_LAGS)
    if ev.scarcity in (SCARCE, VERY_SCARCE):
        ev.labels.append(SCARCE_POSITION)
    elif ev.scarcity == ABUNDANT:
        ev.labels.append(REPLACEABLE_POSITION)

    # The source facts, in each source's own units.
    if ev.ballers_rank is not None:
        hosts = f" ({ev.ballers_agreement.replace(' Ballers', '').lower()}: {ev.hosts_text()})" if ev.ballers_agreement else ""
        (ev.for_lines if ev.ballers_rank <= WAIVER_TOP else ev.risk_lines if ev.ballers_rank > WAIVER_LISTED else ev.for_lines).append(
            f"Ballers #{ev.ballers_rank}{hosts}"
        )
    if ev.rotoballer_rank is not None:
        tag = f", {ev.rotoballer_note}" if ev.rotoballer_note else ""
        line = f"RotoBaller #{ev.rotoballer_rank}{tag}"
        (ev.risk_lines if DEEPER_LEAGUES_ONLY in ev.labels else ev.for_lines).append(
            line + (f" — deeper than this {num_teams}-team league" if DEEPER_LEAGUES_ONLY in ev.labels else "")
        )
    if ev.fp_ros_pos_rank is not None:
        line = f"FantasyPros ROS {ev.pos_label(ev.fp_ros_pos_rank)}"
        (ev.for_lines if fp_supports else ev.risk_lines).append(line)
    elif FP_ROS in sources_present and (ballers_top or roto_top):
        ev.risk_lines.append("unranked in FantasyPros ROS")
    if ev.boone_pos_rank is not None:
        line = f"Boone {ev.pos_label(ev.boone_pos_rank)} this week"
        (ev.for_lines if boone_supports else ev.risk_lines).append(line)
    if BALLERS_SPLIT in ev.labels:
        ev.risk_lines.append(f"Ballers hosts split ({ev.hosts_text()})")
