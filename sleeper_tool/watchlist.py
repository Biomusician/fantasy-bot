"""Watchlist — the near-misses, remembered, each as a thesis.

Every run the tool throws away everything that didn't quite clear a bar:
the player whose role is rising but whose waiver row is only Moderate,
the two sources that disagree about someone already on my roster, the
value that moved 7% when 8% is the bar, the add with no droppable player,
the trade piece priced just above what I'd pay, the starter who is Out but
not done for the year, the stash there is no roster spot for. None of
those are recommendations today. Several of them will be next week, and
without a memory the report has no way to say so — it can only ever
present today's list as if it were the first time.

So each of those is persisted as a WatchItem carrying a THESIS: the reason
he is watched, written in evidence terms from the metrics at watch time
("usage rising (Role Rising), Moderate waiver row, value flat (Stable),
roster full, no drop candidate"). Every later run re-reads the same
metrics and answers two questions about each item.

The first is the promotion question — has the specific thing I was
waiting for happened? — and its answer is one of three trigger states:

  NEW_TRIGGER      a promotion condition fired that had not fired before
                   (the role label improved, velocity crossed the
                   threshold, he became a Must/Strong Add or a favourable
                   trade receive, a roster spot or a drop candidate
                   appeared, the replacement market moved, the conflict
                   went away, the injury cleared)
  STILL_WATCHING   still a candidate, nothing promoted
  RESOLVED         acquired, invalidated, or no longer a candidate for
                   RESOLVE_AFTER_MISSES consecutive runs

The second is the thesis question — is the evidence moving toward or away
from the promotion, or has the thesis stopped being possible? — and its
answer is the per-run `thesis_state`:

  TRIGGERED            a promotion key fired (or he was acquired)
  INVALIDATED          the thesis can no longer be true: dropped from my
                       roster, role fell to Falling/Collapsing for a
                       role thesis, the market caught up for a price
                       thesis, the sources agree again, the value settled
                       back outside the near band, moved to IR for a
                       return thesis, my roster no longer needs the
                       position, or gone from the candidates for
                       RESOLVE_AFTER_MISSES runs. Invalidated items
                       resolve, with `resolved_reason` saying why.
  THESIS_STRENGTHENED  a metric moved toward the promotion condition
                       without reaching it (waiver tier Speculative ->
                       Moderate, value 6% -> 7% against an 8% bar, Out ->
                       Doubtful)
  THESIS_WEAKENED      a metric moved away (role Surging -> Rising, a
                       drop candidate disappeared, a trade target's
                       percentile rose)
  THESIS_UNCHANGED     nothing moved since the last run that looked

Every rule is categorical over the same metrics dict: role label rank,
velocity label rank, waiver tier rank, scarcity rank, injury rank, trade
balance rank, the booleans (drop candidate, open spot, on my roster,
conflict, favourable receive) and two numbers read through a named step
(percentile, consensus gap). `_KIND_AXES` is the per-kind table of which
metrics count and in which direction; `invalidation` is the per-kind list
of conditions that end a thesis.

Only Triggered, Invalidated, Strengthened and Weakened items get text;
unchanged is a count, because a watchlist that reprints thirty unchanged
lines a week is the noise this was built to replace. A strengthened or
weakened note does not repeat on the next run unless the metric moved
again: the comparison is against `last_metrics`, the metrics of the last
run that compared, while the promotion comparison stays against the
metrics stored WHEN THE ITEM WAS FIRST WATCHED (refreshed only when a
trigger fires, so a slow drift can't outrun its own baseline). A
condition that has already triggered is recorded in `triggered_on` and
never fires again. A same-day re-run updates `last_seen` and nothing else.

Persistence follows decision_delta: one JSON file, a schema constant, a
`dir` parameter for tests, and a corrupt or old-schema file treated as an
empty watchlist rather than a crash. The thesis fields were added to
schema 1 with defaults rather than bumping it: no stored field changed
meaning, so a file written before them loads as-is, with each item's
thesis rebuilt from the snapshot it already carries.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from sleeper_tool.formatting import ordinal_pct
from sleeper_tool.asset_value import need_percentile
from sleeper_tool.market_velocity import DIRECTIONAL_MIN_MOVE, FALLING, RAPIDLY_FALLING, RAPIDLY_RISING, RISING, STABLE
from sleeper_tool.opponent_blocker import open_roster_spots
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, SCARCE, VERY_SCARCE
from sleeper_tool.role_trends import COLLAPSING as ROLE_COLLAPSING
from sleeper_tool.role_trends import FALLING as ROLE_FALLING
from sleeper_tool.role_trends import STABLE as ROLE_STABLE
from sleeper_tool.stash_board import WATCH
from sleeper_tool.waiver_engine import MODERATE, MONITOR, MUST_ADD, SPECULATIVE, STRONG_ADD

logger = logging.getLogger(__name__)

# Schema 1 gained thesis, thesis_state, thesis_note, last_metrics and
# resolved_reason as defaulted fields. It was not bumped: nothing already
# stored changed meaning, and an old file must keep loading.
WATCHLIST_SCHEMA = 1
DEFAULT_WATCHLIST_DIR = Path(__file__).resolve().parent.parent / "data" / "watchlist"
WATCHLIST_FILENAME = "watchlist.json"
WATCH_MAX_AGE_DAYS = 28
RESOLVE_AFTER_MISSES = 2
# A Stable velocity this close to the directional bar is one more day's
# move from being a Rising/Falling label.
VELOCITY_NEAR_RATIO = 0.05

# -- Thesis thresholds (every categorical rule below reads one of these) ------
PERCENTILE_STEP = 5.0  # a percentile move under this is noise, not a strengthened/weakened note
PRICE_CAUGHT_UP_PERCENTILE = 15.0  # a trade target whose percentile rose this far since watched: the market caught up
STASH_INVALIDATE_PERCENTILE_DROP = 15.0  # a stash whose percentile fell this far since watched is no longer stash-worthy
DISAGREEMENT_GAP_STEP = 5  # rank places the consensus gap must widen or narrow to count
VELOCITY_MOVE_STEP = 0.01  # how much |total_move| must change toward/away from the directional bar to count
ROLE_INVALIDATING_LABELS = (ROLE_FALLING, ROLE_COLLAPSING)  # a role thesis cannot survive these
RETURN_DEAD_STATUSES = ("IR", "PUP")  # a return thesis moved here is out past the watch window
NO_NEED_SCARCITY = ABUNDANT  # an add thesis dies when the position's market moves here with no room

# -- Kinds -------------------------------------------------------------------
ROLE_RISING_SHORT = "role_rising_short"
SOURCE_DISAGREEMENT = "source_disagreement"
VELOCITY_NEAR = "velocity_near"
WAIVER_NO_DROP = "waiver_no_drop"
TRADE_PRICE_HIGH = "trade_price_high"
INJURED_MAY_RETURN = "injured_may_return"
STASH_BLOCKED = "stash_blocked"
_KIND_ORDER = {
    ROLE_RISING_SHORT: 0, WAIVER_NO_DROP: 1, VELOCITY_NEAR: 2, SOURCE_DISAGREEMENT: 3,
    TRADE_PRICE_HIGH: 4, INJURED_MAY_RETURN: 5, STASH_BLOCKED: 6,
}
ADD_KINDS = (ROLE_RISING_SHORT, WAIVER_NO_DROP, STASH_BLOCKED)  # theses that need a roster spot at his position

# -- Trigger states ----------------------------------------------------------
NEW_TRIGGER = "NEW_TRIGGER"
STILL_WATCHING = "STILL_WATCHING"
RESOLVED = "RESOLVED"

# -- Thesis states -----------------------------------------------------------
THESIS_STRENGTHENED = "THESIS_STRENGTHENED"
THESIS_UNCHANGED = "THESIS_UNCHANGED"
THESIS_WEAKENED = "THESIS_WEAKENED"
TRIGGERED = "TRIGGERED"
INVALIDATED = "INVALIDATED"

# -- Render sections (the order render_lines emits them in) -----------------
SECTION_TRIGGERED = "Triggered"
SECTION_INVALIDATED = "Invalidated"
SECTION_STRENGTHENED = "Strengthened"
SECTION_WEAKENED = "Weakened"
_SECTIONS = (
    (SECTION_TRIGGERED, TRIGGERED),
    (SECTION_INVALIDATED, INVALIDATED),
    (SECTION_STRENGTHENED, THESIS_STRENGTHENED),
    (SECTION_WEAKENED, THESIS_WEAKENED),
)

# Waiver tiers that are NOT enough to make a rising role actionable — the
# whole reason to keep watching him.
WEAK_TIERS = (MODERATE, SPECULATIVE, MONITOR)
PROMOTED_TIERS = (MUST_ADD, STRONG_ADD)
FAVOURABLE_BALANCE = ("Favors me", "Balanced")
EXPENSIVE_BALANCE = ("Overpay", "Slight overpay")
INJURED_STATUSES = ("Out", "Doubtful", "IR")
ROLE_RISING = "Role Rising"
ROLE_SURGING = "Role Surging"
_ROLE_RANK = {ROLE_RISING: 1, ROLE_SURGING: 2}  # promotion: only an improvement within these fires

# Categorical ranks the thesis rules compare. Higher is "more" of the
# thing named; which direction counts as toward the thesis is per kind.
_ROLE_RANK_FULL = {ROLE_COLLAPSING: -2, ROLE_FALLING: -1, ROLE_STABLE: 0, ROLE_RISING: 1, ROLE_SURGING: 2}
_VELOCITY_RANK = {RAPIDLY_FALLING: -2, FALLING: -1, STABLE: 0, RISING: 1, RAPIDLY_RISING: 2}
_TIER_RANK = {MONITOR: 1, SPECULATIVE: 2, MODERATE: 3, STRONG_ADD: 4, MUST_ADD: 5}  # None (off the board) is 0
_SCARCITY_RANK = {ABUNDANT: 0, NORMAL: 1, SCARCE: 2, VERY_SCARCE: 3}
_INJURY_RANK = {"Questionable": 1, "Doubtful": 2, "Out": 3, "IR": 4, "PUP": 4}  # None (no designation) is 0
_BALANCE_RANK = {"Overpay": 0, "Slight overpay": 1, "Balanced": 2, "Favors me": 3}

# Promotion reason keys (stable across runs — they are what `triggered_on`
# remembers) and the line each one renders as.
_PROMOTION_TEXT = {
    "role_improved": "role label improved",
    "velocity_crossed": "market velocity crossed the directional threshold",
    "tier_promoted": "now a Must/Strong Add on the waiver board",
    "favourable_receive": "now appears as a favourably-priced trade receive",
    "roster_spot": "a roster spot opened up",
    "drop_candidate": "a drop candidate now exists at his position",
    "scarcity_changed": "the replacement market at his position moved",
    "conflict_gone": "the recommendation conflict is gone",
    "injury_cleared": "injury status cleared",
}


@dataclass
class WatchItem:
    item_id: str
    league_id: str
    league_name: str
    kind: str
    player_id: str
    player_name: str
    reason: str  # why he went on the list, in the words of the run that added him
    first_seen: str  # YYYY-MM-DD
    last_seen: str
    trigger_state: str = STILL_WATCHING
    trigger_reason: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)  # the metrics when watched (or when last triggered)
    # Bookkeeping the three states need and the docstring explains:
    misses: int = 0  # consecutive runs he was not a candidate
    triggered_on: dict[str, str] = field(default_factory=dict)  # promotion key -> date it first fired
    last_run_on: str = ""  # last run that evaluated this item — makes a same-day re-run a no-op
    resolved_on: str = ""
    # Thesis tracking (added to schema 1 with defaults):
    thesis: str = ""  # the reason in evidence terms, from the metrics at watch time
    thesis_state: str = THESIS_UNCHANGED  # this run's verdict on the thesis
    thesis_note: str = ""  # the metric that moved, or the trigger/invalidation text
    last_metrics: dict[str, Any] = field(default_factory=dict)  # what the last comparing run saw
    resolved_reason: str = ""  # why a RESOLVED item resolved

    def describe(self) -> str:
        return f"{self.league_name}: {self.player_name} — {self.thesis_note or self.trigger_reason or self.reason}"


@dataclass
class Watchlist:
    items: dict[str, WatchItem] = field(default_factory=dict)
    generated_at: str = ""

    def ordered(self) -> list[WatchItem]:
        return sorted(self.items.values(), key=_sort_key)

    def by_state(self, state: str) -> list[WatchItem]:
        return [i for i in self.ordered() if i.trigger_state == state]

    def by_thesis_state(self, state: str) -> list[WatchItem]:
        return [i for i in self.ordered() if i.thesis_state == state]


def _sort_key(item: WatchItem) -> tuple:
    return (_KIND_ORDER.get(item.kind, 9), item.league_name, item.league_id, item.player_name, item.player_id)


def item_id(league_id: str, kind: str, asset: str) -> str:
    return hashlib.sha1(f"{league_id}|{kind}|{asset}".encode()).hexdigest()[:16]


# -- persistence --------------------------------------------------------------


def watchlist_path(watchlist_dir: Path = DEFAULT_WATCHLIST_DIR) -> Path:
    return watchlist_dir / WATCHLIST_FILENAME


def load_watchlist(watchlist_dir: Path = DEFAULT_WATCHLIST_DIR) -> Watchlist:
    """An unreadable, malformed or old-schema file is an EMPTY watchlist,
    never an exception: the watchlist is a convenience layer over a report
    that must still render without it. A schema-1 file written before the
    thesis fields loads with their defaults, and each such item gets its
    thesis rebuilt from the snapshot it already stores."""
    path = watchlist_path(watchlist_dir)
    if not path.exists():
        return Watchlist()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable watchlist %s: %s", path, exc)
        return Watchlist()
    if not isinstance(raw, dict) or raw.get("schema") != WATCHLIST_SCHEMA:
        logger.warning("Ignoring watchlist %s: schema %s, expected %s", path, (raw or {}).get("schema"), WATCHLIST_SCHEMA)
        return Watchlist()
    items: dict[str, WatchItem] = {}
    for row in raw.get("items") or []:
        try:
            item = WatchItem(**row)
        except (TypeError, KeyError) as exc:
            logger.warning("Ignoring malformed watchlist item in %s: %s", path, exc)
            continue
        if not item.thesis:
            item.thesis = thesis_text(item.kind, item.snapshot)
        items[item.item_id] = item
    return Watchlist(items=items, generated_at=raw.get("generated_at") or "")


def save_watchlist(watchlist: Watchlist, watchlist_dir: Path = DEFAULT_WATCHLIST_DIR) -> Path:
    watchlist_dir.mkdir(parents=True, exist_ok=True)
    path = watchlist_path(watchlist_dir)
    payload = {
        "schema": WATCHLIST_SCHEMA,
        "generated_at": watchlist.generated_at,
        "items": [asdict(i) for i in watchlist.ordered()],
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


# -- metrics ------------------------------------------------------------------


def _role_label(entry) -> str | None:
    if entry is None or isinstance(entry, str):
        return entry
    return getattr(entry, "label", None)


def _role_labels(ld, role_trends: dict | None) -> dict:
    return role_trends if role_trends is not None else (getattr(ld, "role_trends", None) or {})


def metrics(ld, player_id: str, *, role_trends: dict | None = None, week: int | None = None) -> dict[str, Any]:
    """The metrics a promotion and a thesis are judged against, for ONE
    player in ONE league. Uniform across kinds on purpose: any watched
    player can be promoted by any of the conditions, so the stored
    baseline has to carry all of them. Every read is duck-typed off the
    report objects, and every one of them is optional — a league that only
    carries a roster still produces a usable metrics dict."""
    roles = _role_labels(ld, role_trends)
    target = next((t for t in _targets(ld) if t.player_id == player_id), None)
    velocity = (getattr(ld, "velocity", None) or {}).get(player_id)
    entry = _any_entry(ld, player_id)
    roster = getattr(ld, "roster", None)
    scarcity = ld.replacement.scarcity_of(_position_of(ld, player_id)) if getattr(ld, "replacement", None) is not None else None
    view = (getattr(ld, "source_views", None) or {}).get(player_id)
    receive = _best_receive(ld, player_id)
    return {
        "week": week,
        "role_label": _role_label(roles.get(player_id)),
        "tier": target.priority_tier if target is not None else None,
        "velocity_label": velocity.label if velocity is not None else None,
        "velocity_move": velocity.total_move if velocity is not None else None,
        "open_spots": open_roster_spots(roster) if roster is not None else 0,
        "has_drop_candidate": bool(target is not None and target.drop_candidate is not None),
        "scarcity": scarcity,
        "has_conflict": any(c.key == player_id for c in getattr(ld, "conflicts", None) or []),
        "injury_status": getattr(entry, "injury_status", None),
        "on_my_roster": _roster_entry(ld, player_id) is not None,
        "roster_known": roster is not None,  # guards the "dropped" reading of on_my_roster
        "favourable_receive": _is_favourable_receive(ld, player_id),
        "percentile": _percentile_of(entry, getattr(ld, "currency", None)),
        # The price of getting him, as the best package that receives him.
        "receive_balance": receive.balance_label if receive is not None else None,
        "receive_ratio": round(receive.value_ratio, 3) if receive is not None else None,
        # The source split about him; None when no view exists this run.
        "disagrees": bool(view.disagrees) if view is not None else None,
        "disagreement_gap": view.consensus_gap if view is not None and view.disagrees else None,
        "label": None,  # kind-specific headline metric, filled by candidates()
        "value": None,  # kind-specific number, filled by candidates()
    }


def _targets(ld) -> list:
    return getattr(ld, "waiver_targets", None) or []


def _proposals(ld) -> list:
    return getattr(ld, "proposals", None) or []


def _roster_entry(ld, player_id: str):
    roster = getattr(ld, "roster", None)
    if roster is None:
        return None
    return next((e for e in roster.entries if e.player_id == player_id), None)


def _any_entry(ld, player_id: str):
    """The richest RosterEntry-shaped record for this player anywhere on
    the league's report data — my roster first, then trade pieces,
    insurance candidates, the stash board and the defensive add. A waiver
    target is NOT one of these (WaiverTarget carries no injury status), so
    a free agent's injury designation is only visible where one of these
    modules already built him an entry."""
    entry = _roster_entry(ld, player_id)
    if entry is not None:
        return entry
    pools = [
        (e for p in _proposals(ld) for e in (*p.give, *p.receive)),
        (rec.candidate for rec in getattr(ld, "insurance", None) or []),
        (c.entry for c in getattr(ld, "stash", None) or []),
        (d.entry for d in getattr(ld, "drop_candidates", None) or []),
    ]
    defensive = getattr(ld, "defensive_add", None)
    if defensive is not None:
        pools.append(iter([defensive.target]))
    for pool in pools:
        for e in pool:
            if e.player_id == player_id:
                return e
    return None


def _position_of(ld, player_id: str) -> str | None:
    entry = _any_entry(ld, player_id)
    if entry is not None:
        return entry.position
    target = next((t for t in _targets(ld) if t.player_id == player_id), None)
    return target.position if target is not None else None


def _percentile_of(entry, currency: str | None = None) -> float | None:
    """The league's own currency decides which percentile is the player's;
    without a currency the dynasty one is preferred, then redraft."""
    value = getattr(entry, "value", None)
    if value is None:
        return None
    if currency:
        pctl = need_percentile(value, currency)
        return float(pctl) if pctl is not None else None
    for attr in ("dynasty_positional_percentile", "dynasty_value_percentile", "redraft_ecr_percentile"):
        pctl = getattr(value, attr, None)
        if pctl is not None:
            return float(pctl)
    return None


def _is_favourable_receive(ld, player_id: str) -> bool:
    return any(
        p.balance_label in FAVOURABLE_BALANCE and any(e.player_id == player_id for e in p.receive)
        for p in _proposals(ld)
    )


def _best_receive(ld, player_id: str):
    """The proposal that receives him at the best balance for me, or None."""
    receiving = [p for p in _proposals(ld) if any(e.player_id == player_id for e in p.receive)]
    if not receiving:
        return None
    return max(receiving, key=lambda p: _BALANCE_RANK.get(p.balance_label, -1))


# -- candidates ---------------------------------------------------------------


def candidates(ld, report, *, role_trends: dict | None = None) -> list[WatchItem]:
    """This run's near-misses for one league, built only from objects
    report_data already assembled. Nothing here fetches, re-scores or
    re-ranks; a candidate is a fact about what the report DIDN'T say."""
    if getattr(ld, "error", None) or not getattr(ld, "drafted", False) or getattr(ld, "roster", None) is None:
        return []
    week = getattr(report, "current_week", None)
    today = _today(report)
    league_id, league_name = ld.league.league_id, ld.league.name
    roles = _role_labels(ld, role_trends)
    targets = {t.player_id: t for t in _targets(ld)}
    out: list[WatchItem] = []

    def add(kind: str, player_id: str, player_name: str, reason: str, extra: dict[str, Any]) -> None:
        snapshot = metrics(ld, player_id, role_trends=role_trends, week=week)
        snapshot.update(extra)
        out.append(
            WatchItem(
                item_id=item_id(league_id, kind, player_id), league_id=league_id, league_name=league_name,
                kind=kind, player_id=player_id, player_name=player_name, reason=reason,
                first_seen=today, last_seen=today, snapshot=snapshot,
                thesis=thesis_text(kind, snapshot), last_metrics=dict(snapshot),
            )
        )

    # 1. A rising role the waiver board hasn't caught up with.
    for pid, trend in roles.items():
        label = _role_label(trend)
        if label != ROLE_RISING:
            continue
        target = targets.get(pid)
        if target is not None and target.priority_tier not in WEAK_TIERS:
            continue
        name = getattr(trend, "name", None) or (target.name if target is not None else _name_of(ld, pid))
        where = f"only a {target.priority_tier} waiver row" if target is not None else "not on the waiver board at all"
        add(ROLE_RISING_SHORT, pid, name, f"{label} but {where}", {"label": label, "value": None})

    # 2. Sources that disagree about someone I own or am chasing.
    rostered = {e.player_id for e in ld.roster.entries}
    for pid, view in (getattr(ld, "source_views", None) or {}).items():
        if not view.disagrees or (pid not in rostered and pid not in targets):
            continue
        add(SOURCE_DISAGREEMENT, pid, view.name, f"Sources disagree: {view.describe()}",
            {"label": view.consensus or view.direction, "value": view.consensus_gap})

    # 3. A value moving, but not yet far enough to be labelled.
    for pid, velocity in (getattr(ld, "velocity", None) or {}).items():
        if velocity.label != STABLE or velocity.total_move is None:
            continue
        if abs(abs(velocity.total_move) - DIRECTIONAL_MIN_MOVE) > VELOCITY_NEAR_RATIO:
            continue
        add(VELOCITY_NEAR, pid, _name_of(ld, pid),
            f"Value has moved {velocity.total_move:+.0%} over {velocity.observations} observations — near the "
            f"{DIRECTIONAL_MIN_MOVE:.0%} bar for a direction label",
            {"label": velocity.label, "value": velocity.total_move})

    # 4. An add with nowhere to put him.
    if open_roster_spots(ld.roster) == 0:
        for t in _targets(ld):
            if t.drop_candidate is None:
                add(WAIVER_NO_DROP, t.player_id, t.name,
                    f"{t.priority_tier} add with no droppable player and a full roster",
                    {"label": t.priority_tier, "value": None})

    # 5. A trade piece I want at a price I wouldn't pay.
    for p in _proposals(ld):
        if p.balance_label not in EXPENSIVE_BALANCE:
            continue
        for e in p.receive:
            add(TRADE_PRICE_HIGH, e.player_id, e.name,
                f"Wanted from {p.target_team_name or p.target_username}, but the package is a {p.balance_label.lower()} — price too high",
                {"label": p.balance_label, "value": round(p.value_ratio, 3)})

    # 6. Out now, not necessarily out for the season.
    for pid in sorted(rostered | set(targets)):
        entry = _any_entry(ld, pid)
        status = getattr(entry, "injury_status", None)
        if status in INJURED_STATUSES:
            add(INJURED_MAY_RETURN, pid, entry.name, f"{status} — may return; worth re-checking before writing the spot off",
                {"label": status, "value": None})

    # 7. A stash there is no room for.
    for candidate in getattr(ld, "stash", None) or []:
        if candidate.label != WATCH or not any("no roster spot" in r for r in candidate.reasons):
            continue
        add(STASH_BLOCKED, candidate.entry.player_id, candidate.entry.name,
            f"Stash-worthy ({'; '.join(candidate.reasons)}) but no roster spot without cutting a real player",
            {"label": candidate.label, "value": candidate.percentile})

    deduped: dict[str, WatchItem] = {}
    for item in out:
        deduped.setdefault(item.item_id, item)
    return sorted(deduped.values(), key=_sort_key)


def _name_of(ld, player_id: str) -> str:
    entry = _any_entry(ld, player_id)
    if entry is not None:
        return entry.name
    target = next((t for t in _targets(ld) if t.player_id == player_id), None)
    if target is not None:
        return target.name
    view = (getattr(ld, "source_views", None) or {}).get(player_id)
    return view.name if view is not None else player_id


def _today(report) -> str:
    generated = getattr(report, "generated_at", None)
    return generated.date().isoformat() if generated is not None else dt.date.today().isoformat()


# -- thesis text --------------------------------------------------------------

_ROLE_PHRASE = {
    ROLE_RISING: "usage rising (Role Rising)",
    ROLE_SURGING: "usage surging (Role Surging)",
    ROLE_STABLE: "usage flat (Stable Role)",
    ROLE_FALLING: "usage falling (Role Falling)",
    ROLE_COLLAPSING: "usage collapsing (Role Collapsing)",
}
_VELOCITY_PHRASE = {
    STABLE: "value flat (Stable)",
    RISING: "value rising (Rising)",
    RAPIDLY_RISING: "value rising fast (Rapidly Rising)",
    FALLING: "value falling (Falling)",
    RAPIDLY_FALLING: "value falling fast (Rapidly Falling)",
}


def _role_phrase(m: dict[str, Any]) -> str:
    return _ROLE_PHRASE.get(m.get("role_label") or "", "no role read")


def _velocity_phrase(m: dict[str, Any]) -> str:
    return _VELOCITY_PHRASE.get(m.get("velocity_label") or "", "value unmeasured")


def _tier_phrase(m: dict[str, Any]) -> str:
    return f"{m['tier']} waiver row" if m.get("tier") else "not on the waiver board"


def _room_phrase(m: dict[str, Any]) -> str:
    spots = m.get("open_spots") or 0
    if spots > 0:
        return f"{spots} open roster spot{'s' if spots != 1 else ''}"
    return "roster full, " + ("a drop candidate exists" if m.get("has_drop_candidate") else "no drop candidate")


def _where_phrase(m: dict[str, Any]) -> str:
    return "on my roster" if m.get("on_my_roster") else "not on my roster"


def _percentile_phrase(m: dict[str, Any]) -> str:
    p = m.get("percentile")
    return ordinal_pct(p) if p is not None else "percentile unknown"


def thesis_text(kind: str, m: dict[str, Any]) -> str:
    """The reason in evidence terms, from the metrics dict at watch time.
    Every kind names the metrics its rules will later compare, so the
    strengthened/weakened note always refers back to something the thesis
    stated."""
    if kind == ROLE_RISING_SHORT:
        bits = [_role_phrase(m), _tier_phrase(m), _velocity_phrase(m), _room_phrase(m)]
    elif kind == WAIVER_NO_DROP:
        bits = [f"{m.get('tier') or 'waiver'} add", _room_phrase(m), _role_phrase(m), _velocity_phrase(m)]
        if m.get("scarcity"):
            bits.append(f"replacement market {m['scarcity']}")
    elif kind == STASH_BLOCKED:
        bits = [f"stash-worthy at the {_percentile_phrase(m)}", _room_phrase(m), _velocity_phrase(m), _role_phrase(m)]
    elif kind == VELOCITY_NEAR:
        move = m.get("velocity_move")
        moved = f"value moved {move:+.0%}" if move is not None else "value moving"
        bits = [f"{moved} against a {DIRECTIONAL_MIN_MOVE:.0%} bar (Stable)", _role_phrase(m), _where_phrase(m)]
    elif kind == SOURCE_DISAGREEMENT:
        gap = m.get("disagreement_gap")
        split = f"sources split by {gap} places" if gap is not None else "sources split on direction"
        bits = [split, _where_phrase(m), _velocity_phrase(m)]
    elif kind == TRADE_PRICE_HIGH:
        balance = m.get("receive_balance") or "too high"
        ratio = m.get("receive_ratio")
        priced = f"priced as {balance}" + (f" (ratio {ratio:.2f})" if ratio is not None else "")
        bits = [priced, _percentile_phrase(m), _velocity_phrase(m)]
    elif kind == INJURED_MAY_RETURN:
        bits = [f"injury {m.get('injury_status') or 'unknown'}", _where_phrase(m), _role_phrase(m), _velocity_phrase(m)]
    else:
        bits = [_role_phrase(m), _velocity_phrase(m), _where_phrase(m)]
    return ", ".join(bits)


# -- thesis moves -------------------------------------------------------------
#
# An axis reads one metric off the previous and current dicts and reports
# whether it moved up (+1), down (-1) or not at all (None), with the two
# values as text. Which direction is "toward the thesis" is decided per
# kind in _KIND_AXES, so the same axis strengthens an add thesis and
# weakens a price thesis.

Move = tuple[int, str, str] | None


def _rank_move(key: str, ranks: dict, prev: dict, cur: dict, *, none_rank: int | None, none_text: str = "none") -> Move:
    a, b = prev.get(key), cur.get(key)
    if (a is None or b is None) and none_rank is None:
        return None  # a missing label is unknown, not a level
    ra = none_rank if a is None else ranks.get(a)
    rb = none_rank if b is None else ranks.get(b)
    if ra is None or rb is None or ra == rb:
        return None
    return (1 if rb > ra else -1, none_text if a is None else str(a), none_text if b is None else str(b))


def _number_move(key: str, step: float, prev: dict, cur: dict, *, fmt: Callable[[float], str], absolute: bool = False) -> Move:
    a, b = prev.get(key), cur.get(key)
    if a is None or b is None:
        return None
    ma, mb = (abs(a), abs(b)) if absolute else (a, b)
    if round(abs(mb - ma), 9) < step:  # rounded so 0.06 -> 0.05 is a full 0.01 step, not 0.00999
        return None
    return (1 if mb > ma else -1, fmt(a), fmt(b))


def _bool_move(key: str, prev: dict, cur: dict, *, yes: str, no: str) -> Move:
    a, b = bool(prev.get(key)), bool(cur.get(key))
    if a == b:
        return None
    return (1 if b else -1, yes if a else no, yes if b else no)


def _pct(x: float) -> str:
    return f"{x:.0f}"


_AXES: dict[str, tuple[str, Callable[[dict, dict], Move]]] = {
    "role": ("role", lambda p, c: _rank_move("role_label", _ROLE_RANK_FULL, p, c, none_rank=None)),
    "tier": ("waiver tier", lambda p, c: _rank_move("tier", _TIER_RANK, p, c, none_rank=0, none_text="off the board")),
    "velocity": ("velocity", lambda p, c: _rank_move("velocity_label", _VELOCITY_RANK, p, c, none_rank=None)),
    "scarcity": ("replacement market", lambda p, c: _rank_move("scarcity", _SCARCITY_RANK, p, c, none_rank=None)),
    "injury": ("injury", lambda p, c: _rank_move("injury_status", _INJURY_RANK, p, c, none_rank=0, none_text="healthy")),
    "balance": ("trade price", lambda p, c: _rank_move("receive_balance", _BALANCE_RANK, p, c, none_rank=None)),
    "drop": ("drop candidate", lambda p, c: _bool_move("has_drop_candidate", p, c, yes="exists", no="none")),
    "spots": ("open roster spot", lambda p, c: _bool_move("open_spots", p, c, yes="yes", no="no")),
    "percentile": ("percentile", lambda p, c: _number_move("percentile", PERCENTILE_STEP, p, c, fmt=_pct)),
    "gap": ("source gap", lambda p, c: _number_move("disagreement_gap", DISAGREEMENT_GAP_STEP, p, c, fmt=lambda x: f"{x:.0f} places")),
    "near": ("value move", lambda p, c: _number_move("velocity_move", VELOCITY_MOVE_STEP, p, c, fmt=lambda x: f"{x:+.0%}", absolute=True)),
}

# Per kind: the axes that count and the sign that is "toward" the thesis
# (+1: up is toward; -1: down is toward). The table the report reads as
# "what would strengthen or weaken this".
_KIND_AXES: dict[str, tuple[tuple[str, int], ...]] = {
    ROLE_RISING_SHORT: (("role", 1), ("tier", 1), ("velocity", 1), ("drop", 1), ("spots", 1), ("scarcity", 1), ("injury", -1)),
    WAIVER_NO_DROP: (("tier", 1), ("role", 1), ("velocity", 1), ("drop", 1), ("spots", 1), ("scarcity", 1), ("injury", -1)),
    STASH_BLOCKED: (("percentile", 1), ("velocity", 1), ("role", 1), ("drop", 1), ("spots", 1), ("scarcity", 1), ("injury", -1)),
    VELOCITY_NEAR: (("near", 1), ("injury", -1)),
    SOURCE_DISAGREEMENT: (("gap", 1), ("injury", -1)),
    # A trade target gets cheaper when the package balance improves, his
    # percentile falls or his value is falling; the market catching up is
    # the reverse of all three.
    TRADE_PRICE_HIGH: (("balance", 1), ("percentile", -1), ("velocity", -1)),
    INJURED_MAY_RETURN: (("injury", -1), ("velocity", 1), ("role", 1)),
}


def thesis_moves(kind: str, prev: dict[str, Any], cur: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(toward, away): one note per axis that moved, each naming the
    metric and both values, e.g. "waiver tier Speculative → Moderate"."""
    toward: list[str] = []
    away: list[str] = []
    for axis, sign in _KIND_AXES.get(kind, ()):
        label, mover = _AXES[axis]
        move = mover(prev, cur)
        if move is None:
            continue
        direction, before, after = move
        (toward if direction == sign else away).append(f"{label} {before} → {after}")
    return toward, away


def assess(kind: str, prev: dict[str, Any], cur: dict[str, Any]) -> tuple[str, str]:
    """The thesis state and note for a run that neither triggered nor
    invalidated. Any move away is a WEAKENED verdict even if something
    else moved toward — the conservative reading, with the toward moves
    kept in the note so nothing is hidden."""
    toward, away = thesis_moves(kind, prev, cur)
    if away:
        note = "; ".join(away)
        if toward:
            note += " (though " + "; ".join(toward) + ")"
        return THESIS_WEAKENED, note
    if toward:
        return THESIS_STRENGTHENED, "; ".join(toward)
    return THESIS_UNCHANGED, ""


# -- invalidation -------------------------------------------------------------


def invalidation(kind: str, snapshot: dict[str, Any], now: dict[str, Any]) -> str | None:
    """The first condition under which this thesis can no longer be true,
    as text, or None. Level checks read `now`; the ones that must be a
    CHANGE (so an item is never invalidated by the state it was watched
    in) compare against the watch-time snapshot."""
    if snapshot.get("on_my_roster") and now.get("roster_known", True) and not now.get("on_my_roster"):
        return "dropped from your roster"
    if kind in (ROLE_RISING_SHORT, WAIVER_NO_DROP) and now.get("role_label") in ROLE_INVALIDATING_LABELS:
        return f"role fell to {now['role_label']}"
    if (
        kind in ADD_KINDS
        and not now.get("on_my_roster")
        and snapshot.get("scarcity") not in (None, NO_NEED_SCARCITY)
        and now.get("scarcity") == NO_NEED_SCARCITY
        and not now.get("has_drop_candidate")
        and not (now.get("open_spots") or 0)
    ):
        return f"no need at his position: replacement market {NO_NEED_SCARCITY}, no drop candidate, no open spot"
    before, after = snapshot.get("percentile"), now.get("percentile")
    if kind == STASH_BLOCKED and before is not None and after is not None and before - after >= STASH_INVALIDATE_PERCENTILE_DROP:
        return f"no longer stash-worthy: percentile {before:.0f} → {after:.0f}"
    if kind == VELOCITY_NEAR:
        move = now.get("velocity_move")
        if move is not None and abs(move) < DIRECTIONAL_MIN_MOVE - VELOCITY_NEAR_RATIO:
            return f"value settled to {move:+.0%}, outside the near band"
    if kind == SOURCE_DISAGREEMENT and now.get("disagrees") is False:
        return "sources agree again"
    if kind == TRADE_PRICE_HIGH and before is not None and after is not None and after - before >= PRICE_CAUGHT_UP_PERCENTILE:
        return f"market caught up: percentile {before:.0f} → {after:.0f}"
    if (
        kind == INJURED_MAY_RETURN
        and snapshot.get("injury_status") not in RETURN_DEAD_STATUSES
        and now.get("injury_status") in RETURN_DEAD_STATUSES
    ):
        return f"moved to {now['injury_status']}: out past the watch window"
    return None


# -- promotion ----------------------------------------------------------------


def promotions(snapshot: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """The promotion keys that fire comparing the stored baseline against
    this run. Each is a state CHANGE, never a level — an item watched
    while already a Must Add would otherwise trigger forever."""
    fired: list[str] = []
    before_role = _ROLE_RANK.get(snapshot.get("role_label") or "", 0)
    if _ROLE_RANK.get(now.get("role_label") or "", 0) > before_role:
        fired.append("role_improved")
    if now.get("velocity_label") not in (None, STABLE) and snapshot.get("velocity_label") in (None, STABLE):
        fired.append("velocity_crossed")
    if now.get("tier") in PROMOTED_TIERS and snapshot.get("tier") not in PROMOTED_TIERS:
        fired.append("tier_promoted")
    if now.get("favourable_receive") and not snapshot.get("favourable_receive"):
        fired.append("favourable_receive")
    if (now.get("open_spots") or 0) > 0 and not (snapshot.get("open_spots") or 0):
        fired.append("roster_spot")
    if now.get("has_drop_candidate") and not snapshot.get("has_drop_candidate"):
        fired.append("drop_candidate")
    if now.get("scarcity") and snapshot.get("scarcity") and now["scarcity"] != snapshot["scarcity"]:
        fired.append("scarcity_changed")
    if snapshot.get("has_conflict") and not now.get("has_conflict"):
        fired.append("conflict_gone")
    if snapshot.get("injury_status") in INJURED_STATUSES and now.get("injury_status") not in INJURED_STATUSES:
        fired.append("injury_cleared")
    return fired


def _promotion_text(keys: list[str], now: dict[str, Any]) -> str:
    bits = []
    for key in keys:
        text = _PROMOTION_TEXT[key]
        if key == "role_improved" and now.get("role_label"):
            text = f"{text} to {now['role_label']}"
        elif key == "velocity_crossed" and now.get("velocity_label"):
            text = f"market velocity is now {now['velocity_label']}"
        elif key == "tier_promoted" and now.get("tier"):
            text = f"now a {now['tier']} on the waiver board"
        elif key == "scarcity_changed" and now.get("scarcity"):
            text = f"{text} to {now['scarcity']}"
        bits.append(text)
    return "; ".join(bits)


def update(existing: Watchlist, candidate_items: list[WatchItem], *, now: dt.datetime, ld_by_league: dict) -> Watchlist:
    """Fold this run's candidates into the stored watchlist and set every
    item's trigger and thesis state. `ld_by_league` maps league_id to that
    league's report data — an item whose league isn't in this run is left
    strictly alone (not a miss, not a trigger, not a thesis verdict): a
    league that failed to sync is not evidence that anything changed.

    Per item, in order: acquired resolves; an invalidation condition
    resolves; a new promotion key triggers; otherwise a candidate is
    compared against the last comparing run for a thesis verdict; a
    non-candidate counts a miss. A same-day re-run re-derives the same
    already-recorded trigger keys and leaves the thesis verdict as the
    morning left it.
    """
    today = now.date().isoformat()
    by_id = {i.item_id: i for i in candidate_items}
    out: dict[str, WatchItem] = {}

    for item in existing.items.values():
        item = _copy(item)
        ld = ld_by_league.get(item.league_id)
        candidate = by_id.get(item.item_id)
        if ld is None:
            out[item.item_id] = item
            continue
        if item.trigger_state == RESOLVED:
            # Terminal: it survives only the run that resolved it (so that
            # run can say so) and is pruned after. A player who becomes a
            # candidate again later starts a fresh watch rather than
            # reopening the closed one.
            out[item.item_id] = _restart(candidate, today) if candidate is not None and item.resolved_on != today else item
            continue
        current = metrics(ld, item.player_id, week=item.snapshot.get("week"))
        same_day = item.last_run_on == today
        item.last_run_on = today
        if not item.thesis:
            item.thesis = thesis_text(item.kind, item.snapshot)

        if current.get("on_my_roster") and not item.snapshot.get("on_my_roster"):
            _resolve(item, today, TRIGGERED, "acquired — he is on your roster now", seen=True)
            out[item.item_id] = item
            continue
        why = invalidation(item.kind, item.snapshot, current)
        if why is not None:
            _resolve(item, today, INVALIDATED, why, seen=True)
            out[item.item_id] = item
            continue

        # `triggered_on` — not the calendar — is what stops a re-trigger:
        # a same-day re-run re-derives the same (already recorded) keys and
        # fires nothing, while a genuinely new condition still gets to fire.
        fired = [k for k in promotions(item.snapshot, current) if k not in item.triggered_on]
        if fired:
            item.trigger_state = NEW_TRIGGER
            item.trigger_reason = _promotion_text(fired, current)
            for key in fired:
                item.triggered_on[key] = today
            # Re-baseline only on a trigger: an untriggered item keeps the
            # metrics it was watched with, so a slow drift still fires.
            item.snapshot = {**item.snapshot, **current}
            item.thesis_state, item.thesis_note = TRIGGERED, item.trigger_reason
            item.last_metrics = dict(current)
            item.last_seen, item.misses = today, 0
        elif candidate is not None:
            if not (same_day and item.trigger_state == NEW_TRIGGER):
                item.trigger_state, item.trigger_reason = STILL_WATCHING, ""
            if not same_day:
                item.thesis_state, item.thesis_note = assess(item.kind, item.last_metrics or item.snapshot, current)
                item.last_metrics = dict(current)
            item.last_seen, item.misses = today, 0
        elif same_day:
            pass  # a re-run on the same day changes nothing
        else:
            item.misses += 1
            if item.misses >= RESOLVE_AFTER_MISSES:
                _resolve(item, today, INVALIDATED, f"no longer a candidate for {item.misses} consecutive runs", seen=False)
            else:
                item.trigger_state, item.trigger_reason = STILL_WATCHING, ""
                item.thesis_state, item.thesis_note = THESIS_UNCHANGED, ""
        out[item.item_id] = item

    for item_key, candidate in by_id.items():
        if item_key not in out:
            out[item_key] = _restart(candidate, today)

    return Watchlist(items=_prune(out, today), generated_at=now.isoformat())


def _resolve(item: WatchItem, today: str, thesis_state: str, reason: str, *, seen: bool) -> None:
    item.trigger_state, item.trigger_reason = RESOLVED, reason
    item.thesis_state, item.thesis_note = thesis_state, reason
    item.resolved_reason, item.resolved_on = reason, today
    if seen:
        item.last_seen = today


def _copy(item: WatchItem) -> WatchItem:
    return WatchItem(**{
        **asdict(item),
        "snapshot": dict(item.snapshot),
        "triggered_on": dict(item.triggered_on),
        "last_metrics": dict(item.last_metrics),
    })


def _restart(candidate: WatchItem, today: str) -> WatchItem:
    item = _copy(candidate)
    item.first_seen = item.last_seen = item.last_run_on = today
    item.trigger_state, item.trigger_reason, item.resolved_on, item.misses = STILL_WATCHING, "", "", 0
    item.thesis_state, item.thesis_note, item.resolved_reason = THESIS_UNCHANGED, "", ""
    if not item.thesis:
        item.thesis = thesis_text(item.kind, item.snapshot)
    item.last_metrics = dict(item.snapshot)
    return item


def _prune(items: dict[str, WatchItem], today: str) -> dict[str, WatchItem]:
    """A resolved item survives only the run that resolved it (so this run
    can say so); anything unseen for WATCH_MAX_AGE_DAYS is dropped."""
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=WATCH_MAX_AGE_DAYS)).isoformat()
    kept: dict[str, WatchItem] = {}
    for key, item in items.items():
        if item.trigger_state == RESOLVED and item.resolved_on != today:
            continue
        if item.last_seen <= cutoff:
            continue  # unseen for WATCH_MAX_AGE_DAYS: dropped, as the docstring says
        kept[key] = item
    return kept


# -- rendering ----------------------------------------------------------------


def _section_of(item: WatchItem) -> str | None:
    """Which section an item renders in, or None for the unchanged count.
    A NEW_TRIGGER is Triggered whatever its thesis_state says, so an item
    from a file written before the thesis fields still renders."""
    if item.thesis_state == TRIGGERED or item.trigger_state == NEW_TRIGGER:
        return SECTION_TRIGGERED
    for name, state in _SECTIONS[1:]:
        if item.thesis_state == state:
            return name
    return None


def _line(item: WatchItem) -> str:
    note = item.thesis_note or item.trigger_reason or item.reason
    return f"{item.league_name}: {item.player_name} — {note} (watched since {item.first_seen}: {item.thesis or item.reason})"


def render_sections(watchlist: Watchlist) -> dict[str, list[str]]:
    """The lines with something to say, grouped: Triggered, Invalidated,
    Strengthened, Weakened (every key present, possibly empty). Within a
    section the order is the watchlist's own — kind, league, player."""
    sections: dict[str, list[str]] = {name: [] for name, _ in _SECTIONS}
    for item in watchlist.ordered():
        name = _section_of(item)
        if name is not None:
            sections[name].append(_line(item))
    return sections


def render_lines(watchlist: Watchlist) -> tuple[list[str], int]:
    """The same lines flattened, each prefixed with its section name
    ("Triggered: League: Player — note (watched since D: thesis)"), in
    section order, plus how many items are still watched with nothing to
    say. The unchanged set is a count by design: it is the part of the
    list that has, by definition, nothing new to report."""
    sections = render_sections(watchlist)
    lines = [f"{name}: {line}" for name, _ in _SECTIONS for line in sections[name]]
    still = sum(1 for i in watchlist.items.values() if i.trigger_state == STILL_WATCHING and _section_of(i) is None)
    return lines, still
