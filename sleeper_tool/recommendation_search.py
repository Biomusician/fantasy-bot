"""Recommendation search — a developer/CLI query over one built report.

Answers "show me everything the report says about player X", "every Very
Scarce market", "every Role Ahead of Market", "every Conflicted Move",
"every urgent action" without re-deriving anything: every hit is a
sentence or object the report already holds, tagged with the league and
the section it came from. Pure over a WeeklyReportData; the script in
scripts/search_recommendations.py builds the report from the cache and
prints the hits.
"""
from __future__ import annotations

from dataclasses import dataclass

from sleeper_tool.action_priority import IMMEDIATE, THIS_WEEK
from sleeper_tool.name_matching import normalize_name
from sleeper_tool.recommendation_conflicts import TRADE, WAIVER, conflict_for
from sleeper_tool.replacement_value import VERY_SCARCE
from sleeper_tool.role_trends import ROLE_AHEAD

URGENT_LEVELS = (IMMEDIATE, THIS_WEEK)


@dataclass
class Hit:
    league: str
    section: str  # "trade" | "waiver" | "drop" | "stash" | "alert" | "market" | "role" | "best move" | "watchlist"
    text: str

    def describe(self) -> str:
        return f"[{self.league}] {self.section}: {self.text}"


def _mentions(name_query: str, *names: str | None) -> bool:
    q = normalize_name(name_query)
    return any(n and q in normalize_name(n) for n in names)


def search_player(report, name: str) -> list[Hit]:
    """Every recommendation, note and label that names the player (case-
    and suffix-insensitive, substring on the normalized name)."""
    hits: list[Hit] = []
    for ld in report.leagues:
        if ld.error or not ld.drafted:
            continue
        league = ld.league.name
        for i, p in enumerate(ld.proposals):
            if _mentions(name, *(e.name for e in (*p.give, *p.receive))):
                text = p.summary_line()
                conflict = conflict_for(ld.conflicts, TRADE, str(i))
                if conflict is not None:
                    text += " — Conflicted: " + "; ".join(conflict.reasons_against)
                hits.append(Hit(league, "trade", text))
        for t in ld.waiver_targets:
            if _mentions(name, t.name, t.drop_candidate.name if t.drop_candidate else None):
                drop = f", drop {t.drop_candidate.name}" if t.drop_candidate else ""
                hits.append(Hit(league, "waiver", f"{t.priority_tier} — add {t.name}{drop}: {t.reason}"))
        for d in ld.drop_candidates:
            if _mentions(name, d.entry.name):
                hits.append(Hit(league, "drop", f"{d.priority}: {d.entry.name} — {'; '.join(d.reasons)}"))
        for c in ld.stash:
            if _mentions(name, c.entry.name):
                hits.append(Hit(league, "stash", f"{c.label}: {c.describe()}"))
        for n in ld.time_sensitive:
            if _mentions(name, n.player_name):
                hits.append(Hit(league, "alert", f"{n.player_name}: {n.note} ({n.severity})"))
        if ld.roster is not None:
            for e in ld.roster.entries:
                if _mentions(name, e.name):
                    trend = ld.role_trends.get(e.player_id)
                    role = f"; role {trend.describe()}" if trend is not None else ""
                    cross = ld.role_market.get(e.player_id)
                    role += f" — {cross}" if cross else ""
                    clause = ld.replacement_clauses.get(e.player_id)
                    hits.append(Hit(league, "roster", f"{e.name} ({e.position or '?'}){'; ' + clause if clause else ''}{role}"))
    return hits


def very_scarce_markets(report) -> list[Hit]:
    hits: list[Hit] = []
    for ld in report.leagues:
        if ld.error or not ld.drafted or ld.replacement is None:
            continue
        for m in ld.replacement.positions.values():
            if m.scarcity == VERY_SCARCE:
                hits.append(Hit(ld.league.name, "market", m.describe()))
    return hits


def role_ahead_of_market(report) -> list[Hit]:
    hits: list[Hit] = []
    for ld in report.leagues:
        if ld.error or not ld.drafted:
            continue
        names = {e.player_id: e.name for e in (ld.roster.entries if ld.roster else [])}
        names.update({t.player_id: t.name for t in ld.waiver_targets})
        for pid, cross in ld.role_market.items():
            if cross == ROLE_AHEAD:
                trend = ld.role_trends.get(pid)
                hits.append(Hit(ld.league.name, "role", f"{names.get(pid, pid)}: {trend.describe() if trend else ''} — {cross}"))
    return hits


def conflicted_moves(report) -> list[Hit]:
    hits: list[Hit] = []
    for ld in report.leagues:
        if ld.error or not ld.drafted:
            continue
        for c in ld.conflicts:
            subject = c.key
            if c.kind == TRADE and c.key.isdigit() and int(c.key) < len(ld.proposals):
                subject = ld.proposals[int(c.key)].summary_line()
            elif c.kind == WAIVER:
                subject = next((f"add {t.name}" for t in ld.waiver_targets if t.player_id == c.key), c.key)
            hits.append(Hit(ld.league.name, c.kind, f"{subject} — against: {'; '.join(c.reasons_against)}"))
    return hits


def urgent_actions(report) -> list[Hit]:
    return [
        Hit(a.league_name, "best move", f"{a.headline} [{a.priority.urgency} · {a.priority.materiality}]")
        for a in report.priority_actions
        if a.priority is not None and a.priority.urgency in URGENT_LEVELS
    ]


def watchlist_hits(report, name: str | None = None) -> list[Hit]:
    watchlist = getattr(report, "watchlist", None)
    if watchlist is None:
        return []
    return [
        Hit(i.league_name, "watchlist", f"{i.player_name} — {i.trigger_state}: {i.trigger_reason or i.reason}")
        for i in watchlist.ordered()
        if name is None or _mentions(name, i.player_name)
    ]
