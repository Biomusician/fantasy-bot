"""Format-aware player valuation.

Pulls each league's *actual* scoring_settings/roster_positions from Sleeper
(never assumed) and uses that to pick the right slice of KTC/FantasyPros/
RotoBaller/FF-CSV data for that specific league.

Sleeper scoring_settings keys this relies on (confirmed against real league
data on 2026-08-18, not guessed):
  - rec: points per reception (0 / 0.5 / 1.0 = standard/half/full PPR)
  - bonus_rec_te: extra points per TE reception, on top of `rec` (TE premium)
  - bonus_rush_yd_100: bonus for 100+ rush yards in a game
  - pass_td: points per passing TD
Superflex is detected from roster_positions containing "SUPER_FLEX" (or a
flex slot in {"SUPER_FLEX", "QB"}), not from the handoff doc's labels.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sleeper_tool.name_matching import normalize_name
from sleeper_tool.rankings import fantasypros, ktc, rotoballer
from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.rankings.ff_dynasty_pass import FFRankingRow

# Documented approximations for scoring axes KTC/RotoBaller don't natively
# model — see the comment where they're applied in ValuationEngine.value_player.
QB_HIGH_PASSING_TD_MULTIPLIER = 1.10  # 6+pt passing-TD leagues boost elite/SF QB value
RB_RUSH_100_BONUS_MULTIPLIER = 1.05  # 100+yd rush bonus favors bell-cow RBs
SUPERFLEX_QB_PROJ_MULTIPLIER = 1.35  # RotoBaller proj_points isn't SF-aware; approximates SF QB scarcity
NFL_REGULAR_SEASON_WEEKS = 17
# rank_std above this (rank-scaled) threshold flags wide disagreement WITHIN
# FantasyPros' own 100+-expert panel. Scales with rank rather than a flat
# number OR a flat ratio: a std of 6 at rank 5 is a real split, a std of 6 at
# rank 140 is noise at that depth — but a naive ratio (std/rank) overcorrects
# at the very top of the board, where even trivial disagreement (e.g. rank 2
# with std 1.3 — everyone agrees he's rank 1-4) produces a large ratio purely
# because the denominator is small. Calibrated against live FantasyPros data
# (2026-08-19): rank_std naturally grows with rank depth (median ~1.5 at
# rank 1-10, ~10 at rank 51-100, ~40 at rank 200+), so this threshold tracks
# roughly the 90th percentile of *actual* std at each depth — flagging only
# genuinely contested players (~12% of the board), not just "any" disagreement,
# which would flag ~50% of the board and train users to ignore the caveat.
PANEL_DISAGREEMENT_BASE = 2.0
PANEL_DISAGREEMENT_SLOPE = 0.16


def panel_disagreement_threshold(rank_ecr: int) -> float:
    return PANEL_DISAGREEMENT_BASE + rank_ecr * PANEL_DISAGREEMENT_SLOPE


def is_panel_disagreement(rank_ecr: int, rank_std: float) -> bool:
    return rank_std >= panel_disagreement_threshold(rank_ecr)
# KTC's crowd-vote volume for a rank this deep is a fraction of what a top-100
# player gets — a handful of votes can swing a deep-bench rank meaningfully.
THIN_MARKET_RANK_THRESHOLD = 150


def scale_proj_points_for_games_remaining(proj_points: float, current_week: int | None) -> float:
    """RotoBaller's proj_points is a full-season total, which doesn't
    shrink as the season progresses — comparing it against another
    full-season total is internally consistent, but USING it to judge
    redraft/keeper trade value in week 12 overstates what's actually left
    to gain from a player whose season is mostly already banked. Scales
    down to a rest-of-season number once the season is underway.
    current_week is 1-indexed (week 1 = all 17 games remaining, no scaling).
    """
    if not current_week or current_week <= 1:
        return proj_points
    games_remaining = max(0, NFL_REGULAR_SEASON_WEEKS - (current_week - 1))
    return proj_points * (games_remaining / NFL_REGULAR_SEASON_WEEKS)


@dataclass(frozen=True)
class LeagueFormat:
    qb_format: str  # "1QB" or "SF"
    ppr: float  # points per reception
    te_premium_bonus: float  # extra pts per TE reception, 0 if none
    rush_100_bonus: float  # bonus for 100+ rush yd games, 0 if none
    pass_td_pts: float
    # Exact QB/RB/WR/TE slot counts read directly off roster_positions
    # (excluding BN/IR/TAXI/FLEX/SUPER_FLEX). A known undercount: FLEX and
    # SUPER_FLEX slots can be filled by RB/WR/TE (or QB for SUPER_FLEX) but
    # aren't attributed to any one position here, so this is a floor on
    # "how many you're guaranteed to need", not the true total demand.
    starter_slots: dict[str, int] = field(default_factory=dict)

    @property
    def is_superflex(self) -> bool:
        return self.qb_format == "SF"

    @property
    def te_premium_tier(self) -> str | None:
        """Maps this league's TE bonus to the closest KTC TE-premium variant.
        KTC only models three fixed tiers (+0.5/+1.0/+1.5 per TE reception),
        so a league with an unusual value (e.g. +0.75) snaps to the nearest.
        """
        if self.te_premium_bonus <= 0:
            return None
        tiers = [(0.5, "tep"), (1.0, "tepp"), (1.5, "teppp")]
        return min(tiers, key=lambda t: abs(t[0] - self.te_premium_bonus))[1]


CORE_SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


def derive_league_format(league_data: dict) -> LeagueFormat:
    # `or {}`/`or []`, not `.get(key, default)` — Sleeper can return the key
    # present but explicitly null (e.g. a league mid-creation), and `.get`'s
    # default only covers a MISSING key, not an explicit None.
    scoring = league_data.get("scoring_settings") or {}
    roster_positions = league_data.get("roster_positions") or []
    is_sf = "SUPER_FLEX" in roster_positions or roster_positions.count("QB") > 1
    starter_slots = {pos: roster_positions.count(pos) for pos in CORE_SKILL_POSITIONS if roster_positions.count(pos)}
    return LeagueFormat(
        qb_format="SF" if is_sf else "1QB",
        ppr=float(scoring.get("rec", 0) or 0),
        te_premium_bonus=float(scoring.get("bonus_rec_te", 0) or 0),
        rush_100_bonus=float(scoring.get("bonus_rush_yd_100", 0) or 0),
        pass_td_pts=float(scoring.get("pass_td", 4) or 4),
        starter_slots=starter_slots,
    )


@dataclass
class PlayerValue:
    player_name: str
    position: str | None
    dynasty_value: int | None  # KTC 0-9999 scale, format-adjusted
    dynasty_rank: int | None
    dynasty_positional_rank: int | None
    dynasty_ecr_rank: int | None  # FantasyPros dynasty consensus rank (cross-check)
    redraft_ecr_rank: int | None  # FantasyPros redraft/ROS consensus rank
    proj_points: float | None  # RotoBaller redraft points projection (format-matched)
    ff_dynasty_rank: int | None  # optional Fantasy Footballers CSV rank
    sources_used: list[str]
    dynasty_value_percentile: float | None = None  # KTC rank -> percentile, 100 = best
    dynasty_ecr_percentile: float | None = None  # FantasyPros dynasty rank -> percentile
    cross_source_agreement: str = "insufficient_data"  # agree | moderate_disagreement | high_disagreement | insufficient_data
    te_premium_caveat: str | None = None
    trend: str | None = None  # RotoBaller literal_trend: "rising" | "down" | "no change" | None
    bye_week: int | None = None  # from FantasyPros/RotoBaller — Sleeper's player objects don't carry this
    redraft_ecr_percentile: float | None = None  # FantasyPros redraft/ROS rank -> percentile
    dynasty_positional_percentile: float | None = None  # KTC positional rank -> percentile WITHIN position
    panel_disagreement_caveat: str | None = None  # wide spread among the FantasyPros expert panel itself
    thin_market_caveat: str | None = None  # KTC crowd-vote volume thins out hard outside the top ~150

    @property
    def is_corroborated(self) -> bool:
        """True if at least two independent sources back this valuation up.
        Trade recommendations should prefer corroborated players and flag
        ones that aren't (single-source values are more likely to be a name-
        matching miss or a source's quirk, not a real signal).
        """
        return len(self.sources_used) >= 2


def _ktc_value_for_format(ktc_player: dict, fmt: LeagueFormat) -> tuple[int, int, int]:
    """Returns (value, overall_rank, positional_rank) for this league's exact format."""
    side = "superflex" if fmt.is_superflex else "one_qb"
    tier = fmt.te_premium_tier
    if tier is None:
        block = ktc_player[side]
    else:
        block = ktc_player[f"{side}_{tier}"]
    return block["value"], block["rank"], block["positional_rank"]


class ValuationEngine:
    """Loads all ranking sources once and answers per-league, per-player
    valuation queries against them. Construct one per report run.
    """

    def __init__(
        self,
        *,
        ktc_snapshot: RankingSnapshot | None = None,
        fp_snapshots: dict[str, RankingSnapshot] | None = None,
        rb_snapshots: dict[str, RankingSnapshot] | None = None,
        ff_rows: list[FFRankingRow] | None = None,
        current_week: int | None = None,
    ) -> None:
        # RotoBaller's proj_points is a full-season total, which doesn't
        # shrink as the season progresses — comparing it against another
        # full-season total is internally consistent, but USING it to judge
        # redraft/keeper trade value in week 12 overstates what's actually
        # left to gain from a player whose season is mostly already banked.
        # Scaling by games remaining turns it into a rest-of-season number.
        # current_week is 1-indexed (week 1 = 17 games remaining).
        self.current_week = current_week
        self.ktc_snapshot = ktc_snapshot or ktc.get_ktc_rankings()
        self._ktc_index = ktc.index_by_name(self.ktc_snapshot)

        self.fp_snapshots = fp_snapshots or {
            key: fantasypros.get_fp_rankings(key) for key in fantasypros.FANTASYPROS_PAGES
        }
        self._fp_indexes = {key: fantasypros.index_by_name(snap) for key, snap in self.fp_snapshots.items()}

        self.rb_snapshots = rb_snapshots or {
            key: rotoballer.get_rb_rankings(key) for key in rotoballer.ROTOBALLER_SPREADSHEETS
        }
        self._rb_indexes = {key: rotoballer.index_by_name(snap) for key, snap in self.rb_snapshots.items()}

        self._ff_index: dict[str, FFRankingRow] = {}
        if ff_rows:
            for row in ff_rows:
                self._ff_index[normalize_name(row.player_name)] = row

        self._ktc_pool_size = len(self.ktc_snapshot.payload)
        self._fp_pool_sizes = {key: len(snap.payload) for key, snap in self.fp_snapshots.items()}
        self._ktc_position_pool_sizes: dict[str, int] = {}
        for p in self.ktc_snapshot.payload:
            pos = p.get("position")
            if pos:
                self._ktc_position_pool_sizes[pos] = self._ktc_position_pool_sizes.get(pos, 0) + 1

    @staticmethod
    def _percentile(rank: int | None, pool_size: int) -> float | None:
        """Converts a 1-indexed rank into a 0-100 percentile, 100 = best player."""
        if not rank or not pool_size:
            return None
        return round(100 * (1 - (rank - 1) / pool_size), 1)

    def _fp_dynasty_key(self, fmt: LeagueFormat) -> str:
        return "dynasty_superflex" if fmt.is_superflex else "dynasty_1qb"

    def _fp_redraft_key(self, fmt: LeagueFormat) -> str:
        if fmt.is_superflex:
            return "redraft_superflex"
        return "redraft_full_ppr" if fmt.ppr >= 0.75 else "redraft_half_ppr"

    def _rb_key(self, fmt: LeagueFormat) -> str:
        if fmt.ppr >= 0.75:
            return "full_ppr"
        if fmt.ppr >= 0.25:
            return "half_ppr"
        return "standard"

    def value_player(self, player_name: str, fmt: LeagueFormat, position: str | None = None) -> PlayerValue:
        key = normalize_name(player_name)
        sources: list[str] = []

        dynasty_value = dynasty_rank = dynasty_pos_rank = None
        ktc_player = self._ktc_index.get(key)
        if ktc_player is not None:
            dynasty_value, dynasty_rank, dynasty_pos_rank = _ktc_value_for_format(ktc_player, fmt)
            sources.append("ktc")

        dynasty_ecr_rank = None
        fp_dyn = self._fp_indexes.get(self._fp_dynasty_key(fmt), {}).get(key)
        if fp_dyn is not None:
            dynasty_ecr_rank = fp_dyn.get("rank_ecr")
            sources.append("fantasypros_dynasty")

        redraft_ecr_rank = None
        fp_rd = self._fp_indexes.get(self._fp_redraft_key(fmt), {}).get(key)
        if fp_rd is not None:
            redraft_ecr_rank = fp_rd.get("rank_ecr")
            sources.append("fantasypros_redraft")

        bye_week = (fp_dyn or {}).get("bye_week") or (fp_rd or {}).get("bye_week")

        proj_points = None
        trend = None
        rb_player = self._rb_indexes.get(self._rb_key(fmt), {}).get(key)
        if rb_player is not None:
            trend = rb_player.get("trend")
            if fmt.te_premium_bonus > 0 and rb_player.get("proj_points_te_premium") is not None:
                proj_points = rb_player["proj_points_te_premium"]
            elif fmt.ppr >= 0.75:
                proj_points = rb_player.get("proj_points_ppr")
            else:
                proj_points = rb_player.get("proj_points_standard")
            if proj_points is not None:
                sources.append("rotoballer")
            if bye_week is None:
                bye_week = rb_player.get("bye_week")

        ff_dynasty_rank = None
        ff_row = self._ff_index.get(key)
        if ff_row is not None:
            ff_dynasty_rank = ff_row.rank
            sources.append("ff_dynasty_pass")

        resolved_position = position or (ktc_player or {}).get("position") or (fp_dyn or {}).get("position")

        panel_disagreement_caveat = None
        primary_fp = fp_dyn or fp_rd
        if primary_fp is not None:
            panel_rank_std = primary_fp.get("rank_std")
            panel_rank_ecr = primary_fp.get("rank_ecr")
            if panel_rank_std is not None and panel_rank_ecr and is_panel_disagreement(panel_rank_ecr, panel_rank_std):
                panel_disagreement_caveat = (
                    f"FantasyPros' own expert panel disagrees widely on this player (ECR {panel_rank_ecr}, "
                    f"±{panel_rank_std:.1f} spread among 100+ experts) — the consensus number may be "
                    "splitting a real difference of opinion (e.g. breakout-or-bust rookie, injury-recovery "
                    "uncertainty) rather than reflecting broad agreement."
                )

        thin_market_caveat = None
        if dynasty_rank is not None and dynasty_rank > THIN_MARKET_RANK_THRESHOLD:
            thin_market_caveat = (
                f"KTC rank {dynasty_rank} is well outside the startup-relevant player pool, where crowd-sourced "
                "vote volume thins out fast — treat this value as noisier/less reliable than a top-150 player's."
            )

        # Neither KTC nor RotoBaller natively model 6pt-passing, 100yd-rush
        # bonuses, or superflex QB scarcity within their point projections —
        # these multipliers are explicit, documented approximations (like
        # the TE-premium tier-snapping above), not precise recomputation.
        # Percentile/rank fields are untouched since those come from each
        # source's own rank ordering, not from these adjusted cardinal values.
        if resolved_position == "QB":
            if fmt.pass_td_pts >= 6:
                if dynasty_value is not None:
                    dynasty_value = min(9999, round(dynasty_value * QB_HIGH_PASSING_TD_MULTIPLIER))
                if proj_points is not None:
                    proj_points = proj_points * QB_HIGH_PASSING_TD_MULTIPLIER
            if fmt.is_superflex and proj_points is not None:
                # RotoBaller's projections aren't superflex-aware (confirmed
                # identical regardless of league param — see rotoballer.py),
                # so redraft/keeper trade currency would otherwise undervalue
                # SF-relevant QBs relative to their real scarcity premium.
                proj_points = proj_points * SUPERFLEX_QB_PROJ_MULTIPLIER
        elif resolved_position == "RB" and fmt.rush_100_bonus > 0:
            if dynasty_value is not None:
                dynasty_value = min(9999, round(dynasty_value * RB_RUSH_100_BONUS_MULTIPLIER))
            if proj_points is not None:
                proj_points = proj_points * RB_RUSH_100_BONUS_MULTIPLIER

        if proj_points is not None:
            proj_points = scale_proj_points_for_games_remaining(proj_points, self.current_week)

        ktc_pctl = self._percentile(dynasty_rank, self._ktc_pool_size)
        ktc_positional_pctl = self._percentile(
            dynasty_pos_rank, self._ktc_position_pool_sizes.get(resolved_position, 0)
        )
        fp_pctl = self._percentile(
            dynasty_ecr_rank, self._fp_pool_sizes.get(self._fp_dynasty_key(fmt), 0)
        )
        redraft_pctl = self._percentile(
            redraft_ecr_rank, self._fp_pool_sizes.get(self._fp_redraft_key(fmt), 0)
        )
        if ktc_pctl is not None and fp_pctl is not None:
            diff = abs(ktc_pctl - fp_pctl)
            if diff <= 10:
                agreement = "agree"
            elif diff <= 25:
                agreement = "moderate_disagreement"
            else:
                agreement = "high_disagreement"
        else:
            agreement = "insufficient_data"

        te_caveat = None
        if fmt.te_premium_bonus > 0 and dynasty_value is not None:
            te_caveat = (
                f"League TE bonus is +{fmt.te_premium_bonus:g}/rec; KTC only models fixed "
                f"+0.5/+1.0/+1.5 tiers, so this value uses the nearest tier ({fmt.te_premium_tier}) "
                "as an approximation, not this league's exact scoring."
            )

        return PlayerValue(
            player_name=player_name,
            position=resolved_position,
            dynasty_value=dynasty_value,
            dynasty_rank=dynasty_rank,
            dynasty_positional_rank=dynasty_pos_rank,
            dynasty_ecr_rank=dynasty_ecr_rank,
            redraft_ecr_rank=redraft_ecr_rank,
            proj_points=proj_points,
            ff_dynasty_rank=ff_dynasty_rank,
            sources_used=sources,
            dynasty_value_percentile=ktc_pctl,
            dynasty_ecr_percentile=fp_pctl,
            cross_source_agreement=agreement,
            te_premium_caveat=te_caveat,
            trend=trend,
            bye_week=bye_week,
            redraft_ecr_percentile=redraft_pctl,
            dynasty_positional_percentile=ktc_positional_pctl,
            panel_disagreement_caveat=panel_disagreement_caveat,
            thin_market_caveat=thin_market_caveat,
        )

    def source_freshness(self) -> dict[str, dt.timedelta]:
        freshness = {"ktc_dynasty": self.ktc_snapshot.age()}
        for key, snap in self.fp_snapshots.items():
            freshness[f"fantasypros_{key}"] = snap.age()
        for key, snap in self.rb_snapshots.items():
            freshness[f"rotoballer_{key}"] = snap.age()
        return freshness
