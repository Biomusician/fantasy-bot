"""Waiver calibration — what the recommended bids actually met.

Four questions, answered from the decision ledger's waiver entries and
nothing else. Every line is a count or a median of observations; nothing
here tunes a threshold, and a sample under MIN_SAMPLE says so instead of
implying a finding:

  Recommended FAAB vs observed clearing bids   for every recommended player
      whose claim later settled at a visible price
  Top-claim hit rate                           of the Must/Strong Add rows,
      how many were acquired, taken by another team, or still unclaimed
  Lost below the clearing bid                  a failed claim of mine where
      the winning bid was above the recommendation
  Overpays against the next visible bid        a claim of mine that won with
      a lower failed bid visible on the same player

A waiver claim's price is only visible when the league runs FAAB and
Sleeper reports the bid, so every count states its own denominator.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from sleeper_tool.decision_ledger import ACQUIRED_BY_ANOTHER, COMPLETED, STILL_AVAILABLE, WAIVER, Ledger

MIN_SAMPLE = 5
MAX_EXAMPLES = 5
TOP_TIERS = ("Must Add", "Strong Add")


@dataclass
class WaiverBidDiagnostics:
    recommended_vs_clearing: list[tuple[str, int, int]] = field(default_factory=list)  # (name, recommended, clearing)
    top_claims: dict[str, int] = field(default_factory=dict)  # outcome -> count
    lost_below_clearing: list[tuple[str, int, int]] = field(default_factory=list)  # (name, recommended, winning)
    overpays: list[tuple[str, int, int]] = field(default_factory=list)  # (name, paid, next visible bid)
    sample: int = 0

    @property
    def median_gap(self) -> float | None:
        gaps = [clearing - rec for _, rec, clearing in self.recommended_vs_clearing]
        return statistics.median(gaps) if gaps else None


def collect(ledger: Ledger) -> WaiverBidDiagnostics:
    out = WaiverBidDiagnostics()
    for entry in ledger.entries.values():
        if entry.action != WAIVER:
            continue
        name = entry.player_names[0] if entry.player_names else entry.subject
        rec = entry.recommended_bid
        out.sample += 1
        if entry.tier in TOP_TIERS:
            label = entry.outcome or "(awaiting outcome)"
            out.top_claims[label] = out.top_claims.get(label, 0) + 1
        if rec is not None and entry.winning_bid is not None:
            out.recommended_vs_clearing.append((name, rec, entry.winning_bid))
            if entry.failed_claim and entry.winning_bid > rec:
                out.lost_below_clearing.append((name, rec, entry.winning_bid))
        if entry.paid_bid is not None and entry.next_bid is not None and entry.paid_bid > entry.next_bid:
            out.overpays.append((name, entry.paid_bid, entry.next_bid))
    out.recommended_vs_clearing.sort(key=lambda r: -(r[2] - r[1]))
    out.overpays.sort(key=lambda r: -(r[1] - r[2]))
    return out


def render_markdown(diag: WaiverBidDiagnostics) -> list[str]:
    lines = ["## Waiver bids", ""]
    if not diag.sample:
        return lines + ["No waiver recommendations recorded yet.", ""]
    lines.append(f"_{diag.sample} waiver recommendation(s) recorded._")
    if diag.sample < MIN_SAMPLE:
        lines.append(f"_Under {MIN_SAMPLE} observations: descriptive only, nothing to read into._")
    lines.append("")
    if diag.recommended_vs_clearing:
        gap = diag.median_gap
        lines += [
            f"**Recommended vs clearing bid** ({len(diag.recommended_vs_clearing)} settled; median clearing bid "
            f"{'+' if gap and gap > 0 else ''}{gap:g} vs recommended)" if gap is not None else "**Recommended vs clearing bid**",
            "",
        ]
        lines += [f"- {name}: recommended ${rec}, cleared at ${clearing}" for name, rec, clearing in diag.recommended_vs_clearing[:MAX_EXAMPLES]]
        lines.append("")
    if diag.top_claims:
        lines += ["**Top-claim outcomes** (Must/Strong Add rows)", "",
                  "- " + ", ".join(f"{label}: {n}" for label, n in sorted(diag.top_claims.items())), ""]
    if diag.lost_below_clearing:
        lines += ["**Lost below the clearing bid**", ""]
        lines += [f"- {name}: recommended ${rec}, won at ${winning}" for name, rec, winning in diag.lost_below_clearing[:MAX_EXAMPLES]]
        lines.append("")
    if diag.overpays:
        lines += ["**Won above the next visible bid**", ""]
        lines += [f"- {name}: paid ${paid}, next visible bid ${nxt}" for name, paid, nxt in diag.overpays[:MAX_EXAMPLES]]
        lines.append("")
    return lines


def section(ledger: Ledger | None) -> str:
    return "\n".join(render_markdown(collect(ledger))) if ledger is not None else ""
