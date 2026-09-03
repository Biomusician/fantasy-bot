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
"insufficient data" is not "consensus".

FantasyPros expert dispersion: the cached ECR rows carry rank_std (and,
once the cache has refreshed past 2026-09-02, rank_min/rank_max). The
existing rank-scaled panel-split test (valuation.is_panel_disagreement)
adds an "expert panel split" note; the min/max range is shown when
present. No extra scrape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sleeper_tool.name_matching import build_name_index, normalize_name
from sleeper_tool.rankings.cache import RankingSnapshot
from sleeper_tool.trade_engine import DYNASTY_CURRENCY
from sleeper_tool.valuation import LeagueFormat, _ktc_value_for_format, is_panel_disagreement

SIGNIFICANT_RANK_GAP = 20  # positional rank places
HIGH_RANK_GAP = 40
STRONG_CONSENSUS_MAX_GAP = 5

STRONG_CONSENSUS = "Strong Consensus"
NORMAL_CONSENSUS = "Normal Consensus"
SOURCE_DISAGREEMENT = "Source Disagreement"
HIGH_DISAGREEMENT = "High Disagreement"
MARKET_ABOVE_PROJECTION = "Market Above Projection"
PROJECTION_ABOVE_MARKET = "Projection Above Market"

_POS_RANK_RE = re.compile(r"^([A-Z]+)(\d+)$")


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
        if self.consensus:
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

    def entry(name: str) -> SourceRanks:
        key = normalize_name(name)
        if key not in table:
            table[key] = SourceRanks()
        return table[key]

    ktc = snapshots.get("ktc")
    if ktc is not None:
        for row in ktc.payload:
            try:
                _, _, pos_rank = _ktc_value_for_format(row, fmt)
            except (KeyError, TypeError):
                continue
            entry(row["name"]).ktc = pos_rank

    for key, attr in (("fp_dynasty", "fp_dynasty"), ("fp_redraft", "fp_redraft")):
        snap = snapshots.get(key)
        if snap is None:
            continue
        for row in snap.payload:
            e = entry(row["name"])
            setattr(e, attr, _pos_rank_from_label(row.get("pos_rank")))
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
        for rows in by_position.values():
            for i, row in enumerate(sorted(rows, key=lambda r: r["rank"]), start=1):
                entry(row["name"]).rotoballer = i
    return table


def consensus_label(gap: int | None) -> str | None:
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
    if projection_rank - market_rank >= SIGNIFICANT_RANK_GAP:
        return MARKET_ABOVE_PROJECTION  # market ranks him better than his projection does
    if market_rank - projection_rank >= SIGNIFICANT_RANK_GAP:
        return PROJECTION_ABOVE_MARKET
    return None


def _expert_note(d: FPDispersion | None) -> str | None:
    if d is None:
        return None
    split = bool(d.rank_ecr) and d.rank_std is not None and is_panel_disagreement(d.rank_ecr, d.rank_std)
    if d.rank_min is not None and d.rank_max is not None and (split or d.rank_max - d.rank_min >= HIGH_RANK_GAP):
        return f"experts range #{d.rank_min}-#{d.rank_max} overall{' (panel split)' if split else ''}"
    if split:
        return f"expert panel split (±{d.rank_std:.0f} places around ECR {d.rank_ecr})"
    return None


def source_view(name: str, position: str | None, ranks: SourceRanks | None, currency: str) -> SourceView:
    r = ranks or SourceRanks()
    if currency == DYNASTY_CURRENCY:
        pair, a, b = ("KTC", "FantasyPros dynasty"), r.ktc, r.fp_dynasty
        market, fp_key = r.ktc, "fp_dynasty"
    else:
        pair, a, b = ("FantasyPros", "RotoBaller"), r.fp_redraft, r.rotoballer
        market, fp_key = r.fp_redraft, "fp_redraft"
    gap = abs(a - b) if a is not None and b is not None else None
    consensus = consensus_label(gap)
    direction = direction_label(market, r.rotoballer)
    view = SourceView(
        name=name, position=position, consensus=consensus, consensus_gap=gap, consensus_pair=pair,
        direction=direction, market_rank=market, projection_rank=r.rotoballer,
        expert_note=_expert_note(r.fp_dispersion.get(fp_key)),
    )
    view.labels = [x for x in (consensus, direction) if x]
    return view


def lookup(table: dict[str, SourceRanks], name: str) -> SourceRanks | None:
    return table.get(normalize_name(name))
