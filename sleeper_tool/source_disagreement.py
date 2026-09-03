"""Source Disagreement / Conviction — keeps the disagreement between KTC,
FantasyPros and RotoBaller visible instead of letting reconciliation erase
it. A reconciled value is one number; whether the three sources agree on
it is a second, independent piece of information a trader wants.

Comparisons are made in WITHIN-POSITION RANK space, never by dividing raw
numbers: a KTC dollar value, a FantasyPros ECR and a RotoBaller point
total are not commensurate, but "WR12 on KTC, WR41 on RotoBaller" is.

Two comparisons per player, chosen by the league's currency:
  consensus pair    dynasty: KTC vs FantasyPros dynasty ECR
                    redraft: FantasyPros redraft ECR vs RotoBaller
  market vs projection
                    market  = KTC (dynasty) / FantasyPros ECR (redraft)
                    projection = RotoBaller
Labels (positional rank places; named constants):
  Strong Consensus       consensus pair within STRONG_CONSENSUS_MAX_GAP
  Normal Consensus       under SIGNIFICANT_RANK_GAP
  Source Disagreement    >= SIGNIFICANT_RANK_GAP
  High Disagreement      >= HIGH_RANK_GAP
  Market Above Projection / Projection Above Market
                         market-vs-projection gap >= SIGNIFICANT_RANK_GAP,
                         in that direction
A player missing from one side of a pair gets no label for that pair —
"insufficient data" is not "consensus". Nor does a player ranked deeper
on one source than the other source's list even goes at that position:
the shallower list simply ends, which is not a disagreement. Gaps are
rank-scaled (RANK_GAP_SCALE_PER_PLACE) so deep-list noise stays quiet.

FantasyPros expert dispersion: the cached ECR rows carry rank_std (and,
once the cache has refreshed past 2026-09-02, rank_min/rank_max). The
existing rank-scaled panel-split test (valuation.is_panel_disagreement)
adds an "expert panel split" note; the min/max range is shown when
present. No extra scrape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sleeper_tool.name_matching import normalize_name
from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.trade_engine import DYNASTY_CURRENCY
from sleeper_tool.valuation import LeagueFormat, _ktc_value_for_format, is_panel_disagreement

SIGNIFICANT_RANK_GAP = 20  # positional rank places, at the top of a list
HIGH_RANK_GAP = 40
STRONG_CONSENSUS_MAX_GAP = 5
# Rank places mean less the deeper you go (WR120 vs WR150 is noise, WR5 vs
# WR35 is not), so a gap is scaled down by 1 + RANK_GAP_SCALE_PER_PLACE x the
# places below rank 1 of the better-ranked side before it meets the
# thresholds above: at rank 51 the effective Disagreement bar is 40 places,
# at rank 101 it is 60.
RANK_GAP_SCALE_PER_PLACE = 0.02

STRONG_CONSENSUS = "Strong Consensus"
NORMAL_CONSENSUS = "Normal Consensus"
SOURCE_DISAGREEMENT = "Source Disagreement"
HIGH_DISAGREEMENT = "High Disagreement"
MARKET_ABOVE_PROJECTION = "Market Above Projection"
PROJECTION_ABOVE_MARKET = "Projection Above Market"

_POS_RANK_RE = re.compile(r"^([A-Z]+)(\d+)$")
DYNASTY_PAIR = ("KTC", "FantasyPros dynasty")
REDRAFT_PAIR = ("FantasyPros", "RotoBaller")


@dataclass
class FPDispersion:
    """FantasyPros expert-panel spread for one player on one FP list."""
    rank_ecr: int | None = None  # overall, for the rank-scaled panel test
    rank_std: float | None = None
    rank_min: int | None = None
    rank_max: int | None = None


@dataclass
class SourceRanks:
    """One player's within-position rank on each source (None = not listed)."""
    ktc: int | None = None
    fp_dynasty: int | None = None
    fp_redraft: int | None = None
    rotoballer: int | None = None
    # Dispersion is kept per FP list ("fp_dynasty" / "fp_redraft"): the two
    # lists rank the same player very differently, so one must not
    # overwrite the other's spread.
    fp_dispersion: dict[str, FPDispersion] = field(default_factory=dict)
    # Shared by every entry of one table: how deep each source's list goes
    # per position ({source: {position: max rank}}).
    depth: dict[str, dict[str, int]] = field(default_factory=dict)
    position: str | None = None


@dataclass
class SourceView:
    name: str
    position: str | None
    consensus: str | None  # one of the four consensus labels, or None (insufficient data)
    consensus_gap: int | None
    consensus_pair: tuple[str, str]  # source names compared
    direction: str | None  # MARKET_ABOVE_PROJECTION / PROJECTION_ABOVE_MARKET / None
    market_rank: int | None
    projection_rank: int | None
    expert_note: str | None  # FantasyPros panel dispersion, when it says something
    labels: list[str] = field(default_factory=list)

    def describe(self) -> str | None:
        bits = []
        # In redraft the consensus pair IS market-vs-projection (FantasyPros
        # vs RotoBaller), so the direction clause already carries the gap.
        same_pair = self.consensus_pair == REDRAFT_PAIR and self.direction is not None
        if self.consensus and not same_pair:
            a, b = self.consensus_pair
            if self.consensus_gap is not None and self.consensus in (SOURCE_DISAGREEMENT, HIGH_DISAGREEMENT):
                bits.append(f"{self.consensus}: {a} vs {b} differ by {self.consensus_gap} {self.position or ''} places")
            else:
                bits.append(self.consensus)
        if self.direction:
            bits.append(f"{self.direction} ({self.position or ''}{self.market_rank} market vs {self.position or ''}{self.projection_rank} projection)")
        if self.expert_note:
            bits.append(self.expert_note)
        return "; ".join(bits) if bits else None

    @property
    def disagrees(self) -> bool:
        return self.consensus in (SOURCE_DISAGREEMENT, HIGH_DISAGREEMENT) or self.direction is not None


def _pos_rank_from_label(label: str | None) -> int | None:
    m = _POS_RANK_RE.match(label or "")
    return int(m.group(2)) if m else None


def build_source_rank_tables(
    snapshots: dict[str, RankingSnapshot | None], fmt: LeagueFormat
) -> dict[str, SourceRanks]:
    """Per normalized player name, the within-position rank on each source.
    `snapshots` is ValuationEngine.snapshots_for(fmt)."""
    table: dict[str, SourceRanks] = {}
    depth: dict[str, dict[str, int]] = {}

    def entry(name: str, position: str | None = None) -> SourceRanks:
        key = normalize_name(name)
        if key not in table:
            table[key] = SourceRanks(depth=depth)
        if position and not table[key].position:
            table[key].position = position
        return table[key]

    def note_depth(source: str, position: str | None, rank: int | None) -> None:
        if position and rank:
            d = depth.setdefault(source, {})
            d[position] = max(d.get(position, 0), rank)

    ktc = snapshots.get("ktc")
    if ktc is not None:
        for row in ktc.payload:
            try:
                _, _, pos_rank = _ktc_value_for_format(row, fmt)
            except (KeyError, TypeError):
                continue
            entry(row["name"], row.get("position")).ktc = pos_rank
            note_depth("ktc", row.get("position"), pos_rank)

    for key, attr in (("fp_dynasty", "fp_dynasty"), ("fp_redraft", "fp_redraft")):
        snap = snapshots.get(key)
        if snap is None:
            continue
        for row in snap.payload:
            e = entry(row["name"], row.get("position"))
            rank = _pos_rank_from_label(row.get("pos_rank"))
            setattr(e, attr, rank)
            note_depth(key, row.get("position"), rank)
            e.fp_dispersion[key] = FPDispersion(
                rank_ecr=row.get("rank_ecr"), rank_std=row.get("rank_std"),
                rank_min=row.get("rank_min"), rank_max=row.get("rank_max"),
            )

    rb = snapshots.get("rotoballer")
    if rb is not None:
        by_position: dict[str, list[dict]] = {}
        for row in rb.payload:
            if row.get("rank") is not None:
                by_position.setdefault(row.get("position") or "", []).append(row)
        for position, rows in by_position.items():
            for i, row in enumerate(sorted(rows, key=lambda r: r["rank"]), start=1):
                entry(row["name"], position).rotoballer = i
                note_depth("rotoballer", position, i)
    return table


def scaled_gap(a: int | None, b: int | None) -> float | None:
    """Rank gap in top-of-list places: the raw gap shrunk by how deep the
    better-ranked of the two sits."""
    if a is None or b is None:
        return None
    return abs(a - b) / (1 + RANK_GAP_SCALE_PER_PLACE * (min(a, b) - 1))


def comparable(ranks: SourceRanks, source_a: str, rank_a: int | None, source_b: str, rank_b: int | None) -> bool:
    """False when either rank lies beyond the OTHER source's list depth at
    the position — the shallower list ending is not a disagreement."""
    if rank_a is None or rank_b is None:
        return False
    pos = ranks.position or ""
    depth_a = ranks.depth.get(source_a, {}).get(pos)
    depth_b = ranks.depth.get(source_b, {}).get(pos)
    if depth_a is not None and rank_b > depth_a:
        return False
    if depth_b is not None and rank_a > depth_b:
        return False
    return True


def consensus_label(gap: float | None) -> str | None:
    """`gap` is a scaled gap (scaled_gap) or, for a top-of-list player, the
    raw one — the thresholds are stated in top-of-list places."""
    if gap is None:
        return None
    if gap >= HIGH_RANK_GAP:
        return HIGH_DISAGREEMENT
    if gap >= SIGNIFICANT_RANK_GAP:
        return SOURCE_DISAGREEMENT
    if gap <= STRONG_CONSENSUS_MAX_GAP:
        return STRONG_CONSENSUS
    return NORMAL_CONSENSUS


def direction_label(market_rank: int | None, projection_rank: int | None) -> str | None:
    if market_rank is None or projection_rank is None:
        return None
    gap = scaled_gap(market_rank, projection_rank)
    if gap < SIGNIFICANT_RANK_GAP:
        return None
    if projection_rank > market_rank:
        return MARKET_ABOVE_PROJECTION  # market ranks him better than his projection does
    return PROJECTION_ABOVE_MARKET


def _expert_note(d: FPDispersion | None) -> str | None:
    if d is None:
        return None
    split = bool(d.rank_ecr) and d.rank_std is not None and is_panel_disagreement(d.rank_ecr, d.rank_std)
    if split and d.rank_min is not None and d.rank_max is not None:
        return f"experts range #{d.rank_min}-#{d.rank_max} overall (panel split)"
    if split:
        return f"expert panel split (±{d.rank_std:.0f} places around ECR {d.rank_ecr})"
    return None


def source_view(name: str, position: str | None, ranks: SourceRanks | None, currency: str) -> SourceView:
    r = ranks or SourceRanks()
    if currency == DYNASTY_CURRENCY:
        pair, a, b = DYNASTY_PAIR, r.ktc, r.fp_dynasty
        sources = ("ktc", "fp_dynasty")
        market, market_source, fp_key = r.ktc, "ktc", "fp_dynasty"
    else:
        pair, a, b = REDRAFT_PAIR, r.fp_redraft, r.rotoballer
        sources = ("fp_redraft", "rotoballer")
        market, market_source, fp_key = r.fp_redraft, "fp_redraft", "fp_redraft"
    gap = abs(a - b) if comparable(r, sources[0], a, sources[1], b) else None
    consensus = consensus_label(scaled_gap(a, b)) if gap is not None else None
    direction = direction_label(market, r.rotoballer) if comparable(r, market_source, market, "rotoballer", r.rotoballer) else None
    view = SourceView(
        name=name, position=position, consensus=consensus, consensus_gap=gap, consensus_pair=pair,
        direction=direction, market_rank=market, projection_rank=r.rotoballer,
        expert_note=_expert_note(r.fp_dispersion.get(fp_key)),
    )
    view.labels = [x for x in (consensus, direction) if x]
    return view


def lookup(table: dict[str, SourceRanks], name: str) -> SourceRanks | None:
    return table.get(normalize_name(name))
