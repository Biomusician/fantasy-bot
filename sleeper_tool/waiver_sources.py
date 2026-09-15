"""Waiver Sources — every external waiver opinion for one run, loaded once
and pinned to Sleeper ids once, with each source keeping its own identity.

Four sources, four different questions, never averaged into one number:

  FantasyPros ROS   broad expert consensus, rest of season (positional rank
                    plus the panel's spread) — already fetched by the
                    valuation engine; read here, not re-fetched
  Fantasy Footballers  waiver-specific judgment from three hosts, from the
                    CSV the user drops into data/manual every Tuesday
  RotoBaller waivers   an independent acquisition-priority board, with the
                    league sizes it says an add is for
  Justin Boone      one analyst's weekly positional ranks, a cross-check

Any of them may be missing. A missing source is recorded with the reason
(not provided, wrong week, fetch failed) so the report can say "Ballers:
not provided" instead of implying the Ballers had no opinion. Nothing here
decides anything; waiver_evidence reads these lookups.

Historical accuracy (Boone first and RotoBaller's Jamie Calandro third in
FantasyPros' 2025 in-season accuracy) is why these sources are in the set.
It is deliberately NOT a weight anywhere: past ranking accuracy is not a
calibrated coefficient for this week's waiver decision.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from sleeper_tool.name_matching import normalize_name
from sleeper_tool.rankings.source_matching import MatchResult, SourceName, build_sleeper_name_index, match_source_names

logger = logging.getLogger(__name__)

LOADED = "Loaded"
NOT_PROVIDED = "Not provided"
WRONG_WEEK = "Wrong week"
UNAVAILABLE = "Unavailable"
NOT_APPLICABLE = "Not applicable"

FP_ROS_FULL = "ros_full_ppr"
FP_ROS_HALF = "ros_half_ppr"
_POS_RANK_RE = re.compile(r"^([A-Z]+)(\d+)$")


@dataclass(frozen=True)
class FPRosView:
    pos_rank: int | None
    overall_rank: int | None
    rank_std: float | None
    rank_min: int | None
    rank_max: int | None
    page: str


@dataclass
class SourceStatus:
    source: str  # display name
    label: str  # LOADED / NOT_PROVIDED / WRONG_WEEK / UNAVAILABLE / NOT_APPLICABLE
    detail: str = ""
    rows: int = 0
    matched: int = 0
    week: int | None = None
    loaded_at: dt.datetime | None = None

    def describe(self) -> str:
        bits = [self.source, self.label]
        if self.label == LOADED:
            bits.append(f"week {self.week}" if self.week else "week not stated")
            bits.append(f"{self.matched}/{self.rows} players matched")
        if self.detail:
            bits.append(self.detail)
        return " · ".join(bits)


@dataclass
class WaiverSources:
    claim_week: int | None
    ballers: object | None = None  # rankings.fantasyfootballers.BallersBoard
    ballers_by_id: dict[str, object] = field(default_factory=dict)  # player_id -> BallersRow
    rotoballer: object | None = None  # rankings.rotoballer_waivers.WaiverBoard
    rotoballer_by_id: dict[str, object] = field(default_factory=dict)  # player_id -> WaiverBoardRow
    boone_by_id: dict[str, dict[str, int]] = field(default_factory=dict)  # scoring -> player_id -> positional rank
    fp_ros_by_id: dict[str, dict[str, FPRosView]] = field(default_factory=dict)  # page -> player_id -> view
    statuses: list[SourceStatus] = field(default_factory=list)
    unmatched: dict[str, list[str]] = field(default_factory=dict)  # source -> names that could not be pinned to one Sleeper id

    def status(self, source: str) -> SourceStatus | None:
        return next((s for s in self.statuses if s.source == source), None)

    def has(self, source: str) -> bool:
        s = self.status(source)
        return s is not None and s.label == LOADED

    def fp_ros_for(self, ppr: float) -> dict[str, FPRosView]:
        return self.fp_ros_by_id.get(fp_ros_page_for(ppr), {})

    def boone_for(self, ppr: float) -> dict[str, int] | None:
        scoring = _boone_scoring(ppr)
        return self.boone_by_id.get(scoring) if scoring else None


BALLERS = "Fantasy Footballers"
ROTOBALLER = "RotoBaller waivers"
BOONE = "Justin Boone"
FP_ROS = "FantasyPros ROS"


def fp_ros_page_for(ppr: float) -> str:
    """FantasyPros publishes full- and half-PPR ROS pages. A standard league
    reads the half-PPR page — positional ROS order barely moves between the
    two, and the report names the page it used."""
    return FP_ROS_FULL if ppr >= 0.75 else FP_ROS_HALF


def _boone_scoring(ppr: float) -> str | None:
    from sleeper_tool.rankings.boone import scoring_for_ppr

    return scoring_for_ppr(ppr)


def _pos_rank(label: str | None) -> int | None:
    m = _POS_RANK_RE.match(label or "")
    return int(m.group(2)) if m else None


def _matched_ids(result: MatchResult, rows: list) -> dict[str, object]:
    return {pid: rows[i] for i, pid in result.matched.items()}


def _unmatched_names(result: MatchResult) -> list[str]:
    return [r.name for r in (*result.unmatched, *result.ambiguous)]


def load_waiver_sources(
    all_players: dict[str, dict],
    engine,
    *,
    claim_week: int | None,
    scoring_needed: set[str] | None = None,
    allow_fetch: bool = True,
    now: dt.datetime | None = None,
) -> WaiverSources:
    """`scoring_needed`: which Boone variants the redraft/keeper leagues use
    ("ppr", "half_ppr"); None skips Boone. `allow_fetch=False` reads caches
    only (the test suite's path). Never raises: a source that fails is
    recorded Unavailable."""
    out = WaiverSources(claim_week=claim_week)
    index = build_sleeper_name_index(all_players)

    # -- FantasyPros ROS (already loaded by the engine) -----------------------
    for page in (FP_ROS_FULL, FP_ROS_HALF):
        snap = (getattr(engine, "fp_snapshots", None) or {}).get(page)
        if snap is None:
            out.statuses.append(SourceStatus(f"{FP_ROS} ({page.replace('ros_', '').replace('_', '-')})", UNAVAILABLE, "not loaded this run"))
            continue
        rows = list(snap.payload or [])
        result = match_source_names([SourceName(r.get("name", ""), r.get("team"), r.get("position")) for r in rows], all_players, index=index)
        views: dict[str, FPRosView] = {}
        for i, pid in result.matched.items():
            r = rows[i]
            views[pid] = FPRosView(
                pos_rank=_pos_rank(r.get("pos_rank")), overall_rank=r.get("rank_ecr"), rank_std=r.get("rank_std"),
                rank_min=r.get("rank_min"), rank_max=r.get("rank_max"), page=page,
            )
        out.fp_ros_by_id[page] = views
        out.statuses.append(SourceStatus(
            f"{FP_ROS} ({page.replace('ros_', '').replace('_', '-')})", LOADED, rows=len(rows), matched=len(views),
            loaded_at=snap.fetched_at,
        ))

    # -- Fantasy Footballers CSV ------------------------------------------------
    try:
        from sleeper_tool.rankings.fantasyfootballers import MANUAL_DIR, find_waiver_csv, load_ballers_board

        _path, reason = find_waiver_csv(MANUAL_DIR, target_week=claim_week, now=now)
        board = load_ballers_board(MANUAL_DIR, target_week=claim_week, now=now)
        if board is None:
            label = WRONG_WEEK if "not week" in reason else NOT_PROVIDED
            out.statuses.append(SourceStatus(BALLERS, label, reason))
        else:
            result = match_source_names([SourceName(r.name, r.team) for r in board.rows], all_players, index=index)
            out.ballers = board
            out.ballers_by_id = _matched_ids(result, board.rows)
            out.unmatched[BALLERS] = _unmatched_names(result)
            out.statuses.append(SourceStatus(
                BALLERS, LOADED, board.source_file or "", rows=len(board.rows), matched=len(out.ballers_by_id),
                week=board.week, loaded_at=board.loaded_at,
            ))
    except Exception as exc:  # a manual file must never fail the run
        logger.exception("Fantasy Footballers board skipped")
        out.statuses.append(SourceStatus(BALLERS, UNAVAILABLE, f"could not be read: {exc}"))

    # -- RotoBaller weekly waiver board ----------------------------------------------
    try:
        from sleeper_tool.rankings.rotoballer_waivers import load_rotoballer_waiver_board

        board = load_rotoballer_waiver_board(allow_fetch=allow_fetch)
        if board is None:
            out.statuses.append(SourceStatus(ROTOBALLER, UNAVAILABLE, "no current board (fetch failed or not cached)"))
        elif claim_week is not None and board.week is not None and board.week != claim_week:
            out.statuses.append(SourceStatus(ROTOBALLER, WRONG_WEEK, f"newest board is for week {board.week}, not week {claim_week}", week=board.week))
        else:
            result = match_source_names([SourceName(r.name, r.team, r.position) for r in board.rows], all_players, index=index)
            out.rotoballer = board
            out.rotoballer_by_id = _matched_ids(result, board.rows)
            out.unmatched[ROTOBALLER] = _unmatched_names(result)
            out.statuses.append(SourceStatus(
                ROTOBALLER, LOADED, rows=len(board.rows), matched=len(out.rotoballer_by_id), week=board.week, loaded_at=board.fetched_at,
            ))
    except Exception as exc:
        logger.exception("RotoBaller waiver board skipped")
        out.statuses.append(SourceStatus(ROTOBALLER, UNAVAILABLE, f"could not be read: {exc}"))

    # -- Justin Boone weekly ranks ----------------------------------------------------------
    for scoring in sorted(scoring_needed or ()):
        name = f"{BOONE} ({'full PPR' if scoring == 'ppr' else 'half PPR'})"
        try:
            from sleeper_tool.rankings.boone import ENABLED_BY_DEFAULT, load_boone_board

            board = load_boone_board(scoring, week=claim_week, allow_fetch=allow_fetch and ENABLED_BY_DEFAULT)
            if board is None:
                out.statuses.append(SourceStatus(name, UNAVAILABLE, f"no week {claim_week} board (fetch failed, disabled, or not cached)"))
                continue
            skill = [r for r in board.rows if r.position_list in ("QB", "RB", "WR", "TE")]
            result = match_source_names([SourceName(r.name, r.team, r.position_list) for r in skill], all_players, index=index)
            out.boone_by_id[scoring] = {pid: skill[i].rank for i, pid in result.matched.items()}
            out.unmatched[name] = _unmatched_names(result)
            out.statuses.append(SourceStatus(
                name, LOADED, rows=len(skill), matched=len(out.boone_by_id[scoring]), week=board.week, loaded_at=board.fetched_at,
            ))
        except Exception as exc:
            logger.exception("Boone board skipped")
            out.statuses.append(SourceStatus(name, UNAVAILABLE, f"could not be read: {exc}"))
    return out


def normalized(name: str) -> str:
    return normalize_name(name)
