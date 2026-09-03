"""Watchlist — the near-misses, remembered.

Every run the tool throws away everything that didn't quite clear a bar:
the player whose role is rising but whose waiver row is only Moderate,
the two sources that disagree about someone already on my roster, the
value that moved 7% when 8% is the bar, the add with no droppable player,
the trade piece priced just above what I'd pay, the starter who is Out but
not done for the year, the stash there is no roster spot for. None of
those are recommendations today. Several of them will be next week, and
without a memory the report has no way to say so — it can only ever
present today's list as if it were the first time.

So each of those is persisted as a WatchItem, and every later run asks one
question of each: has the specific thing I was waiting for happened? The
answer is one of three states and nothing in between:

  NEW_TRIGGER      a promotion condition fired that had not fired before
                   (the role label improved, velocity crossed the
                   threshold, he became a Must/Strong Add or a favourable
                   trade receive, a roster spot or a drop candidate
                   appeared, the replacement market moved, the conflict
                   went away, the injury cleared)
  STILL_WATCHING   still a candidate, nothing changed
  RESOLVED         acquired, or no longer a candidate for
                   RESOLVE_AFTER_MISSES consecutive runs

Only NEW_TRIGGER items get text; still-watching is a count, because a
watchlist that reprints thirty unchanged lines a week is the noise this
was built to replace.

Determinism, twice over: the promotion comparison is always against the
metrics stored WHEN THE ITEM WAS FIRST WATCHED (refreshed only when a
trigger fires, so a slow drift can't outrun its own baseline), and a
condition that has already triggered is recorded in `triggered_on` and
never fires again. A same-day re-run updates `last_seen` and nothing else.

Persistence follows decision_delta: one JSON file, a schema constant, a
`dir` parameter for tests, and a corrupt or old-schema file treated as an
empty watchlist rather than a crash.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sleeper_tool.asset_value import need_percentile
from sleeper_tool.market_velocity import DIRECTIONAL_MIN_MOVE, STABLE
from sleeper_tool.opponent_blocker import open_roster_spots
from sleeper_tool.stash_board import WATCH
from sleeper_tool.waiver_engine import MODERATE, MONITOR, MUST_ADD, SPECULATIVE, STRONG_ADD

logger = logging.getLogger(__name__)

WATCHLIST_SCHEMA = 1
DEFAULT_WATCHLIST_DIR = Path(__file__).resolve().parent.parent / "data" / "watchlist"
WATCHLIST_FILENAME = "watchlist.json"
WATCH_MAX_AGE_DAYS = 28
RESOLVE_AFTER_MISSES = 2
# A Stable velocity this close to the directional bar is one more day's
# move from being a Rising/Falling label.
VELOCITY_NEAR_RATIO = 0.05

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

# -- Trigger states ----------------------------------------------------------
NEW_TRIGGER = "NEW_TRIGGER"
STILL_WATCHING = "STILL_WATCHING"
RESOLVED = "RESOLVED"

# Waiver tiers that are NOT enough to make a rising role actionable — the
# whole reason to keep watching him.
WEAK_TIERS = (MODERATE, SPECULATIVE, MONITOR)
PROMOTED_TIERS = (MUST_ADD, STRONG_ADD)
FAVOURABLE_BALANCE = ("Favors me", "Balanced")
EXPENSIVE_BALANCE = ("Overpay", "Slight overpay")
INJURED_STATUSES = ("Out", "Doubtful", "IR")
ROLE_RISING = "Role Rising"
ROLE_SURGING = "Role Surging"
_ROLE_RANK = {ROLE_RISING: 1, ROLE_SURGING: 2}

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

    def describe(self) -> str:
        return f"{self.league_name}: {self.player_name} — {self.trigger_reason or self.reason}"


@dataclass
class Watchlist:
    items: dict[str, WatchItem] = field(default_factory=dict)
    generated_at: str = ""

    def ordered(self) -> list[WatchItem]:
        return sorted(self.items.values(), key=_sort_key)

    def by_state(self, state: str) -> list[WatchItem]:
        return [i for i in self.ordered() if i.trigger_state == state]


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
    that must still render without it."""
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
            items[row["item_id"]] = WatchItem(**row)
        except (TypeError, KeyError) as exc:
            logger.warning("Ignoring malformed watchlist item in %s: %s", path, exc)
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
    """The metrics a promotion is judged against, for ONE player in ONE
    league. Uniform across kinds on purpose: any watched player can be
    promoted by any of the conditions, so the stored baseline has to carry
    all of them. Every read is duck-typed off the report objects, and
    every one of them is optional — a league that only carries a roster
    still produces a usable metrics dict."""
    roles = _role_labels(ld, role_trends)
    target = next((t for t in _targets(ld) if t.player_id == player_id), None)
    velocity = (getattr(ld, "velocity", None) or {}).get(player_id)
    entry = _any_entry(ld, player_id)
    roster = getattr(ld, "roster", None)
    scarcity = ld.replacement.scarcity_of(_position_of(ld, player_id)) if getattr(ld, "replacement", None) is not None else None
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
        "favourable_receive": _is_favourable_receive(ld, player_id),
        "percentile": _percentile_of(entry, getattr(ld, "currency", None)),
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
    item's trigger state. `ld_by_league` maps league_id to that league's
    report data — an item whose league isn't in this run is left strictly
    alone (not a miss, not a trigger): a league that failed to sync is not
    evidence that anything changed.
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

        if current.get("on_my_roster") and not item.snapshot.get("on_my_roster"):
            item.trigger_state, item.trigger_reason = RESOLVED, "acquired — he is on your roster now"
            item.resolved_on, item.last_seen = today, today
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
            item.last_seen, item.misses = today, 0
        elif candidate is not None:
            if not (same_day and item.trigger_state == NEW_TRIGGER):
                item.trigger_state, item.trigger_reason = STILL_WATCHING, ""
            item.last_seen, item.misses = today, 0
        elif same_day:
            pass  # a re-run on the same day changes nothing
        else:
            item.misses += 1
            if item.misses >= RESOLVE_AFTER_MISSES:
                item.trigger_state = RESOLVED
                item.trigger_reason = f"no longer a candidate for {item.misses} consecutive runs"
                item.resolved_on = today
            else:
                item.trigger_state = STILL_WATCHING
                item.trigger_reason = ""
        out[item.item_id] = item

    for item_key, candidate in by_id.items():
        if item_key not in out:
            out[item_key] = _restart(candidate, today)

    return Watchlist(items=_prune(out, today), generated_at=now.isoformat())


def _copy(item: WatchItem) -> WatchItem:
    return WatchItem(**{**asdict(item), "snapshot": dict(item.snapshot), "triggered_on": dict(item.triggered_on)})


def _restart(candidate: WatchItem, today: str) -> WatchItem:
    item = _copy(candidate)
    item.first_seen = item.last_seen = item.last_run_on = today
    item.trigger_state, item.trigger_reason, item.resolved_on, item.misses = STILL_WATCHING, "", "", 0
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


def render_lines(watchlist: Watchlist) -> tuple[list[str], int]:
    """Text for the newly-triggered items only, plus how many are still
    being watched. The still-watching set is a count by design: it is the
    part of the list that has, by definition, nothing new to say."""
    new_triggers = [
        f"{i.league_name}: {i.player_name} — {i.trigger_reason} (watched since {i.first_seen}: {i.reason})"
        for i in watchlist.by_state(NEW_TRIGGER)
    ]
    return new_triggers, len(watchlist.by_state(STILL_WATCHING))
