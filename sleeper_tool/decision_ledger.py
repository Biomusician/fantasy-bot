"""Decision Ledger — what the tool actually recommended, and what Sleeper
later shows happened.

Every run records the week's recommendations (trades, waiver adds, drops,
defensive adds, streamer adds, priority stashes) as fingerprinted entries
in one small JSON file. A later run reads the league's cached Sleeper
transactions and rosters back and stamps each open entry with a *factual*
outcome label — did the add land on my roster, did someone else take him,
is he still on the wire, did the trade go through with those exact assets.

Three things this deliberately is not:

  - It is not a scoreboard. Sleeper cannot tell you a trade offer was
    rejected: there is no "rejected" or "pending" transaction status (only
    "complete" and "failed", and "failed" only ever means a waiver claim
    that could not process). An offer never sent and an offer refused look
    identical from the outside, so the ledger never says "Rejected" and
    never scores a recommendation right or wrong.
  - It is not a re-derivation. Every label comes from a transaction row or
    a current roster list, never from a model.
  - It is not run-keyed. The fingerprint is the league + the assets + the
    counterparty, so the same recommendation surfacing again on Thursday
    updates Monday's entry instead of duplicating it. `run_id` is the run
    that first produced it; `last_seen` is the most recent one that did.

Entries stay open for OBSERVATION_WINDOW_DAYS. Before that window closes,
"nothing has happened yet" is not an outcome worth resolving on.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from sleeper_tool.decision_delta import stable_value

logger = logging.getLogger(__name__)

LEDGER_SCHEMA = 1
LEDGER_FILENAME = "ledger.json"
DEFAULT_LEDGER_DIR = Path(__file__).resolve().parent.parent / "data" / "decision_ledger"
# Enough for a full season of ten leagues with room to spare; resolved
# entries are dropped before open ones so a purge never loses something
# still being watched.
LEDGER_MAX_ENTRIES = 2000
# How long a recommendation stays open before "nothing happened" becomes a
# reportable fact rather than a wait.
OBSERVATION_WINDOW_DAYS = 14

# -- actions --------------------------------------------------------------------
TRADE = "trade"
CONSOLIDATION = "consolidation"
WAIVER = "waiver"
DROP = "drop"
DEFENSIVE_ADD = "defensive_add"
STREAMER = "streamer"
STASH = "stash"
ACTIONS = (TRADE, CONSOLIDATION, WAIVER, DROP, DEFENSIVE_ADD, STREAMER, STASH)
# Actions whose subject is a player I do not have yet.
_ADD_ACTIONS = (WAIVER, DEFENSIVE_ADD, STREAMER, STASH)
_TRADE_ACTIONS = (TRADE, CONSOLIDATION)

# -- status / outcomes ----------------------------------------------------------
OPEN = "open"
RESOLVED = "resolved"

COMPLETED = "Completed"
PARTIALLY_MATCHED = "Partially Matched"
ACQUIRED_BY_ANOTHER = "Acquired by Another Manager"
STILL_AVAILABLE = "Still Available"
NO_OBSERVED_ACTION = "No Observed Action"
UNABLE_TO_DETERMINE = "Unable to Determine"
OUTCOMES = (
    COMPLETED,
    PARTIALLY_MATCHED,
    ACQUIRED_BY_ANOTHER,
    STILL_AVAILABLE,
    NO_OBSERVED_ACTION,
    UNABLE_TO_DETERMINE,
)
# Seeing one of these is the end of the story; the rest can still change.
_TERMINAL = (COMPLETED, PARTIALLY_MATCHED, ACQUIRED_BY_ANOTHER)

PickKey = tuple[str, int, int]  # (season, round, ORIGINAL owner roster id)


@dataclass
class LedgerEntry:
    fingerprint: str
    run_id: str  # first run that produced this recommendation (ISO)
    last_seen: str  # most recent run that still produced it (ISO)
    league_id: str
    league_name: str
    action: str
    player_ids: tuple[str, ...] = ()
    player_names: tuple[str, ...] = ()
    give_ids: tuple[str, ...] = ()
    receive_ids: tuple[str, ...] = ()
    give_picks: tuple[PickKey, ...] = ()
    receive_picks: tuple[PickKey, ...] = ()
    counterparty_roster_id: int | None = None
    counterparty_name: str | None = None
    tier: str | None = None  # acceptance rating / waiver tier / label
    reason_labels: tuple[str, ...] = ()
    valuation_snapshot: dict[str, float | None] = field(default_factory=dict)  # as recommended
    latest_valuation: dict[str, float | None] = field(default_factory=dict)  # most recent run that re-made it
    role_signal: str | None = None  # filled by the orchestrator when a role source exists
    replacement_context: dict[str, str] = field(default_factory=dict)  # position -> scarcity
    projected_lineup_delta: float | None = None  # points/week the move was previewed to add
    faab_pct: int | None = None  # suggested, not paid
    currency: str | None = None
    team_status: str | None = None  # my contender/middling/rebuild status when recommended
    status: str = OPEN
    outcome: str | None = None
    outcome_detail: str | None = None
    observed_at: str | None = None
    failed_claim: bool = False  # a waiver claim of mine for this player failed to process
    paid_bid: int | None = None  # FAAB actually spent, when Completed via a waiver

    @property
    def subject(self) -> str:
        return ", ".join(self.player_names) or "(picks only)"


@dataclass
class Ledger:
    schema: int = LEDGER_SCHEMA
    updated_at: str | None = None
    entries: dict[str, LedgerEntry] = field(default_factory=dict)

    def open_entries(self) -> list[LedgerEntry]:
        return [e for e in self.ordered() if e.status == OPEN]

    def ordered(self) -> list[LedgerEntry]:
        """Deterministic order everywhere: oldest first, fingerprint breaks ties."""
        return sorted(self.entries.values(), key=lambda e: (e.run_id, e.fingerprint))


# -- fingerprint ----------------------------------------------------------------


def _pick_str(key: Sequence[Any]) -> str:
    season, rnd, original = key
    return f"{season}-{int(rnd)}-{int(original)}"


def fingerprint(
    *,
    league_id: str,
    action: str,
    give_ids: Iterable[str] = (),
    receive_ids: Iterable[str] = (),
    give_picks: Iterable[Sequence[Any]] = (),
    receive_picks: Iterable[Sequence[Any]] = (),
    counterparty: str | None = None,
) -> str:
    """Identity of a *logical* recommendation. Deliberately excludes the run
    id, the tier, and every derived label: the same offer to the same
    manager on a later day is the same decision, and re-recording it would
    make the ledger a run log instead of a decision log.

    Give and receive are kept apart (sending A for B is not the same
    decision as sending B for A) but each is sorted, so the order the
    engine happened to build its lists in never matters.
    """
    parts = [
        league_id,
        action,
        "give:" + ",".join(sorted(str(p) for p in give_ids)),
        "recv:" + ",".join(sorted(str(p) for p in receive_ids)),
        "gpicks:" + ",".join(sorted(_pick_str(k) for k in give_picks)),
        "rpicks:" + ",".join(sorted(_pick_str(k) for k in receive_picks)),
        "cp:" + (str(counterparty) if counterparty is not None else ""),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# -- building entries from a report ---------------------------------------------


def _pick_key(pick) -> PickKey:
    return (str(pick.season), int(pick.round), int(pick.original_roster_id))


def _scarcity(ld, positions: Iterable[str | None]) -> dict[str, str]:
    market = getattr(ld, "replacement", None)
    if market is None:
        return {}
    out: dict[str, str] = {}
    for pos in positions:
        if not pos:
            continue
        entry = market.positions.get(pos)
        if entry is not None:
            out[pos] = entry.scarcity
    return dict(sorted(out.items()))


def _values(entries, currency: str, current_week: int | None) -> dict[str, float | None]:
    return {e.player_id: stable_value(e.value, currency, current_week) for e in entries if e.value is not None}


def _conflict_labels(ld, kind: str, key: str) -> list[str]:
    for c in getattr(ld, "conflicts", ()) or ():
        if c.kind == kind and c.key == key:
            return ["conflict"]
    return []


def _counterparty_roster_id(ld, proposal) -> int | None:
    """The report doesn't put a roster id on a proposal, but league_economy
    indexes every manager by one — match on username first (unique in a
    Sleeper league), then team name."""
    direct = getattr(proposal, "target_roster_id", None)
    if direct is not None:
        return int(direct)
    economy = getattr(ld, "league_economy", None)
    if economy is None:
        return None
    for rid, manager in sorted(economy.managers.items()):
        if manager.username and manager.username == proposal.target_username:
            return int(rid)
    for rid, manager in sorted(economy.managers.items()):
        if manager.team_name and manager.team_name == proposal.target_team_name:
            return int(rid)
    return None


def _impact_delta(impact) -> float | None:
    if impact is None:
        return None
    return impact.after.weekly_points - impact.before.weekly_points


def build_entries(report) -> list[LedgerEntry]:
    """Ledger entries for every recommendation in a WeeklyReportData
    (duck-typed, like decision_delta, so this module stays out of
    report_data's import graph).

    Pre-draft leagues need no special case: `waivers_note` leagues have
    empty waiver/streamer/stash lists by construction, so nothing is built.
    """
    run_id = report.generated_at.isoformat()
    current_week = report.current_week
    out: list[LedgerEntry] = []
    seen: set[str] = set()

    def add(*, counterparty: Any = None, **kwargs: Any) -> None:
        fp = fingerprint(
            league_id=kwargs["league_id"],
            action=kwargs["action"],
            give_ids=kwargs.get("give_ids", ()),
            receive_ids=kwargs.get("receive_ids", ()),
            give_picks=kwargs.get("give_picks", ()),
            receive_picks=kwargs.get("receive_picks", ()),
            counterparty=counterparty,
        )
        if fp in seen:
            return  # e.g. an insurance row merged into waiver_targets
        seen.add(fp)
        out.append(LedgerEntry(fingerprint=fp, run_id=run_id, last_seen=run_id, **kwargs))

    for ld in report.leagues:
        if ld.error or not ld.drafted or ld.roster is None:
            continue
        league_id = ld.league.league_id
        league_name = ld.league.name
        currency = ld.currency
        team_status = ld.team_status.status if ld.team_status else None
        common = dict(league_id=league_id, league_name=league_name, currency=currency, team_status=team_status)

        for i, p in enumerate(ld.proposals):
            action = CONSOLIDATION if p.trade_type == CONSOLIDATION else TRADE
            labels = [f"trade_type:{p.trade_type}", f"balance:{p.balance_label}", f"confidence:{p.confidence}"]
            econ = ld.trade_economics[i] if i < len(ld.trade_economics) else None
            delta = None
            if econ is not None:
                labels.append(f"asset_economics:{econ.asset_economics}")
                if econ.roster_economics:
                    labels.append(f"roster_economics:{econ.roster_economics}")
                if econ.strategic_tradeoff:
                    labels.append("strategic_tradeoff")
                delta = econ.weekly_delta
            impact = ld.trade_impacts[i] if i < len(ld.trade_impacts) else None
            if impact is not None:
                delta = _impact_delta(impact)
            labels += _conflict_labels(ld, "trade", str(i))
            cp_roster = _counterparty_roster_id(ld, p)
            add(
                action=action,
                player_ids=tuple(e.player_id for e in (*p.give, *p.receive)),
                player_names=tuple(e.name for e in (*p.give, *p.receive)),
                give_ids=tuple(e.player_id for e in p.give),
                receive_ids=tuple(e.player_id for e in p.receive),
                give_picks=tuple(_pick_key(k) for k in p.give_picks),
                receive_picks=tuple(_pick_key(k) for k in p.receive_picks),
                counterparty=cp_roster if cp_roster is not None else p.target_username,
                counterparty_roster_id=cp_roster,
                counterparty_name=p.target_team_name or p.target_username,
                tier=p.acceptance_rating,
                reason_labels=tuple(labels),
                valuation_snapshot=_values((*p.give, *p.receive), currency, current_week),
                replacement_context=_scarcity(ld, {e.position for e in (*p.give, *p.receive)}),
                projected_lineup_delta=delta,
                **common,
            )

        for t in ld.waiver_targets:
            labels = [f"horizon:{t.horizon}"]
            if t.fills_need:
                labels.append("fills_need")
            if t.drop_candidate is not None:
                labels.append("paired_drop")
            labels += _conflict_labels(ld, "waiver", t.player_id)
            snapshot = _values([t], currency, current_week)
            if t.drop_candidate is not None:
                snapshot.update(_values([t.drop_candidate], currency, current_week))
            add(
                action=WAIVER,
                player_ids=(t.player_id,),
                player_names=(t.name,),
                give_ids=(t.drop_candidate.player_id,) if t.drop_candidate else (),
                receive_ids=(t.player_id,),
                counterparty=None,
                tier=t.priority_tier,
                reason_labels=tuple(labels),
                valuation_snapshot=snapshot,
                replacement_context=_scarcity(ld, [t.position]),
                projected_lineup_delta=_impact_delta(ld.waiver_impacts.get(t.player_id)),
                faab_pct=t.suggested_faab_pct,
                **common,
            )

        for d in ld.drop_candidates:
            add(
                action=DROP,
                player_ids=(d.entry.player_id,),
                player_names=(d.entry.name,),
                give_ids=(d.entry.player_id,),
                counterparty=None,
                tier=d.priority,
                reason_labels=("drop",),
                valuation_snapshot=_values([d.entry], currency, current_week),
                replacement_context=_scarcity(ld, [d.entry.position]),
                **common,
            )

        da = getattr(ld, "defensive_add", None)
        if da is not None:
            add(
                action=DEFENSIVE_ADD,
                player_ids=(da.target.player_id,),
                player_names=(da.target.name,),
                give_ids=(da.drop.player_id,) if da.drop else (),
                receive_ids=(da.target.player_id,),
                counterparty=None,
                tier="Defensive Add",
                reason_labels=(f"week:{da.week}", f"opponent_hole:{da.hole}"),
                valuation_snapshot=_values([da.target], currency, current_week),
                replacement_context=_scarcity(ld, [da.target.position]),
                projected_lineup_delta=da.my_gain,
                **common,
            )

        for plan in getattr(ld, "streamers", ()) or ():
            if plan.recommendation == "Hold":
                continue
            options = [plan.single] if plan.sequence is None else [plan.sequence.first, plan.sequence.second]
            adds = [o.entry for o in options if not o.rostered]
            if not adds:
                continue
            add(
                action=STREAMER,
                player_ids=tuple(e.player_id for e in adds),
                player_names=tuple(e.name for e in adds),
                receive_ids=tuple(e.player_id for e in adds),
                counterparty=None,
                tier=plan.recommendation,
                reason_labels=(f"position:{plan.position}", f"weeks:{','.join(str(w) for w in plan.weeks)}"),
                valuation_snapshot=_values(adds, currency, current_week),
                replacement_context=_scarcity(ld, [plan.position]),
                **common,
            )

        for s in getattr(ld, "stash", ()) or ():
            if s.label != "Priority Stash":
                continue
            add(
                action=STASH,
                player_ids=(s.entry.player_id,),
                player_names=(s.entry.name,),
                give_ids=(s.drop.player_id,) if s.drop else (),
                receive_ids=(s.entry.player_id,),
                counterparty=None,
                tier=s.label,
                reason_labels=(f"percentile:{s.percentile:.0f}",),
                valuation_snapshot=_values([s.entry], currency, current_week),
                replacement_context=_scarcity(ld, [s.entry.position]),
                **common,
            )
    return out


# -- merge / persist ------------------------------------------------------------


def merge_entries(ledger: Ledger, new_entries: Iterable[LedgerEntry], run_id: str) -> tuple[int, int]:
    """Fold this run's entries into the ledger. Returns (new, refreshed).

    A recommendation the tool has made before keeps the numbers it was
    first made with — the tier, the labels, the valuation snapshot are the
    record of the decision as it stood — and only gains a newer
    `last_seen` and a `latest_valuation`. A same-day re-run therefore
    changes nothing except the timestamps.
    """
    added = refreshed = 0
    for entry in new_entries:
        existing = ledger.entries.get(entry.fingerprint)
        if existing is None:
            entry.run_id = entry.run_id or run_id
            entry.last_seen = run_id
            ledger.entries[entry.fingerprint] = entry
            added += 1
            continue
        existing.last_seen = run_id
        existing.latest_valuation = dict(entry.valuation_snapshot)
        existing.league_name = entry.league_name
        if existing.role_signal is None and entry.role_signal is not None:
            existing.role_signal = entry.role_signal
        refreshed += 1
    ledger.updated_at = run_id
    _enforce_retention(ledger)
    return added, refreshed


def _enforce_retention(ledger: Ledger) -> None:
    """Drop oldest-resolved first, then oldest-open — a purge should never
    cost us an entry we're still waiting on."""
    excess = len(ledger.entries) - LEDGER_MAX_ENTRIES
    if excess <= 0:
        return
    order = sorted(
        ledger.entries.values(),
        key=lambda e: (0 if e.status == RESOLVED else 1, e.last_seen, e.run_id, e.fingerprint),
    )
    for entry in order[:excess]:
        del ledger.entries[entry.fingerprint]


def _entry_to_dict(entry: LedgerEntry) -> dict[str, Any]:
    data = dict(entry.__dict__)
    for key in ("player_ids", "player_names", "give_ids", "receive_ids", "reason_labels"):
        data[key] = list(data[key])
    for key in ("give_picks", "receive_picks"):
        data[key] = [list(k) for k in data[key]]
    return data


def _entry_from_dict(data: dict[str, Any]) -> LedgerEntry:
    kwargs = {k: v for k, v in data.items() if k in LedgerEntry.__dataclass_fields__}
    for key in ("player_ids", "player_names", "give_ids", "receive_ids", "reason_labels"):
        kwargs[key] = tuple(kwargs.get(key) or ())
    for key in ("give_picks", "receive_picks"):
        kwargs[key] = tuple((str(k[0]), int(k[1]), int(k[2])) for k in (kwargs.get(key) or ()))
    return LedgerEntry(**kwargs)


def ledger_path(ledger_dir: Path = DEFAULT_LEDGER_DIR) -> Path:
    return Path(ledger_dir) / LEDGER_FILENAME


def save_ledger(ledger: Ledger, ledger_dir: Path = DEFAULT_LEDGER_DIR) -> Path:
    path = ledger_path(ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": LEDGER_SCHEMA,
        "updated_at": ledger.updated_at,
        "entries": {fp: _entry_to_dict(e) for fp, e in sorted(ledger.entries.items())},
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def load_ledger(ledger_dir: Path = DEFAULT_LEDGER_DIR) -> Ledger:
    """Never raises. A missing, unreadable or older-schema file gives an
    empty ledger — losing history is annoying, but a crashed report is
    worse, and the ledger is a log, not an input to any recommendation."""
    path = ledger_path(ledger_dir)
    if not path.exists():
        return Ledger()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable decision ledger %s: %s", path, exc)
        return Ledger()
    if not isinstance(payload, dict) or payload.get("schema") != LEDGER_SCHEMA:
        logger.warning("Ignoring decision ledger %s: schema %s, expected %s", path, (payload or {}).get("schema"), LEDGER_SCHEMA)
        return Ledger()
    entries: dict[str, LedgerEntry] = {}
    for fp, data in (payload.get("entries") or {}).items():
        try:
            entries[fp] = _entry_from_dict(data)
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            logger.warning("Skipping malformed ledger entry %s: %s", fp, exc)
    return Ledger(schema=LEDGER_SCHEMA, updated_at=payload.get("updated_at"), entries=entries)


# -- observation ----------------------------------------------------------------


def _to_ms(iso: str) -> int | None:
    try:
        moment = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp() * 1000)


def _as_datetime(iso: str) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _effective_ms(t: dict) -> int:
    """When a transaction took effect: a waiver claim is queued at
    `created` and processed at `status_updated`, hours or days later."""
    return int(t.get("status_updated") or t.get("created") or 0)


def _sorted_transactions(transactions: Sequence[dict]) -> list[dict]:
    return sorted(transactions, key=lambda t: (_effective_ms(t), str(t.get("transaction_id") or "")))


def _rostered_by(rosters: Sequence[dict]) -> dict[str, int]:
    """player_id -> roster_id, for every player on any roster right now."""
    out: dict[str, int] = {}
    for roster in rosters:
        rid = roster.get("roster_id")
        for pid in roster.get("players") or []:
            out[str(pid)] = int(rid)
    return out


def _trade_sides(tx: dict, my_roster_id: int) -> tuple[set[str], set[str], set[PickKey], set[PickKey], set[int]]:
    """(players out, players in, picks out, picks in, counterparty rosters)
    from my side of a Sleeper trade row. `adds` maps player -> the roster
    he lands on; `drops` maps player -> the roster he left."""
    adds = tx.get("adds") or {}
    drops = tx.get("drops") or {}
    players_in = {str(pid) for pid, rid in adds.items() if _same_roster(rid, my_roster_id)}
    players_out = {str(pid) for pid, rid in drops.items() if _same_roster(rid, my_roster_id)}
    picks_in: set[PickKey] = set()
    picks_out: set[PickKey] = set()
    for pick in tx.get("draft_picks") or []:
        try:
            key = (str(pick["season"]), int(pick["round"]), int(pick["roster_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if _same_roster(pick.get("owner_id"), my_roster_id):
            picks_in.add(key)
        elif _same_roster(pick.get("previous_owner_id"), my_roster_id):
            picks_out.add(key)
    others = {int(r) for r in (tx.get("roster_ids") or []) if not _same_roster(r, my_roster_id)}
    return players_out, players_in, picks_out, picks_in, others


def _same_roster(value: Any, roster_id: int) -> bool:
    return value is not None and str(value) == str(roster_id)


def _resolve(entry: LedgerEntry, outcome: str, detail: str | None, now: dt.datetime, *, close: bool) -> None:
    entry.outcome = outcome
    entry.outcome_detail = detail
    entry.observed_at = now.isoformat()
    if close:
        entry.status = RESOLVED


def observe(
    ledger: Ledger,
    *,
    transactions_by_league: dict[str, list[dict]],
    rosters_by_league: dict[str, list[dict]],
    my_roster_ids: dict[str, int],
    now: dt.datetime,
) -> dict[str, int]:
    """Stamp every open entry with what Sleeper shows. Returns a count by
    outcome of the entries touched.

    Only transactions created at or after an entry's first_seen count: a
    player I already added the week before I was told to add him is not
    evidence the recommendation was followed.
    """
    counts: dict[str, int] = {}
    for entry in ledger.open_entries():
        txs = transactions_by_league.get(entry.league_id)
        rosters = rosters_by_league.get(entry.league_id)
        my_roster_id = my_roster_ids.get(entry.league_id)
        first_ms = _to_ms(entry.run_id)
        first_seen = _as_datetime(entry.run_id)
        expired = first_seen is not None and now >= first_seen + dt.timedelta(days=OBSERVATION_WINDOW_DAYS)

        # An EMPTY transaction list is an answer (nothing happened); only a
        # missing one, or missing rosters, is unable to determine.
        if txs is None or not rosters or my_roster_id is None or first_ms is None:
            _resolve(entry, UNABLE_TO_DETERMINE, _missing_detail(txs, rosters, my_roster_id), now, close=expired)
            counts[UNABLE_TO_DETERMINE] = counts.get(UNABLE_TO_DETERMINE, 0) + 1
            continue

        rows = [t for t in _sorted_transactions(txs) if _effective_ms(t) >= first_ms]
        rostered = _rostered_by(rosters)
        if entry.action in _TRADE_ACTIONS:
            outcome, detail = _observe_trade(entry, rows, my_roster_id)
        elif entry.action == DROP:
            outcome, detail = _observe_drop(entry, rows, my_roster_id)
        elif entry.action in _ADD_ACTIONS:
            outcome, detail = _observe_add(entry, rows, rostered, my_roster_id)
        else:  # an action from a newer schema this build doesn't know how to watch
            outcome, detail = UNABLE_TO_DETERMINE, f"unknown action {entry.action!r}"

        if outcome is None:
            outcome = NO_OBSERVED_ACTION if expired else None
            detail = None
        if outcome is None:
            continue
        _resolve(entry, outcome, detail, now, close=outcome in _TERMINAL or expired)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _missing_detail(txs, rosters, my_roster_id) -> str:
    missing = []
    if not txs:
        missing.append("no cached transactions")
    if not rosters:
        missing.append("no cached rosters")
    if my_roster_id is None:
        missing.append("my roster id unknown")
    return "; ".join(missing) or "missing data"


def _observe_add(
    entry: LedgerEntry, rows: list[dict], rostered: dict[str, int], my_roster_id: int
) -> tuple[str | None, str | None]:
    """Waiver / defensive add / streamer / stash: did the player land here?"""
    pids = [str(p) for p in entry.receive_ids] or [str(p) for p in entry.player_ids]
    if not pids:
        return None, None
    # Where he is NOW settles it before any transaction history does: a
    # rival's add that was later reversed must not close the entry.
    if any(_same_roster(rostered.get(pid), my_roster_id) for pid in pids if pid in rostered):
        mine = [tx for tx in rows if tx.get("status") == "complete" and any(_same_roster((tx.get("adds") or {}).get(pid), my_roster_id) for pid in pids)]
        if mine:
            bid = (mine[-1].get("settings") or {}).get("waiver_bid")
            if bid is not None:
                entry.paid_bid = int(bid)
            kind = mine[-1].get("type") or "transaction"
            return COMPLETED, f"added via {kind}" + (f" for ${entry.paid_bid} FAAB" if entry.paid_bid is not None else "")
    for tx in rows:
        adds = tx.get("adds") or {}
        for pid in pids:
            landed = adds.get(pid)
            if landed is None:
                continue
            if tx.get("status") == "failed":
                if _same_roster(landed, my_roster_id):
                    entry.failed_claim = True  # intent recorded; a failed claim is not an outcome
                continue
            if tx.get("status") != "complete":
                continue
            if _same_roster(landed, my_roster_id):
                bid = (tx.get("settings") or {}).get("waiver_bid")
                if bid is not None:
                    entry.paid_bid = int(bid)
                kind = tx.get("type") or "transaction"
                detail = f"added via {kind}" + (f" for ${entry.paid_bid} FAAB" if entry.paid_bid is not None else "")
                return COMPLETED, detail
            return ACQUIRED_BY_ANOTHER, f"added by roster {landed}"
    unrostered = [pid for pid in pids if pid not in rostered]
    if unrostered:
        return STILL_AVAILABLE, "not on any roster"
    return None, None


def _observe_drop(entry: LedgerEntry, rows: list[dict], my_roster_id: int) -> tuple[str | None, str | None]:
    pids = [str(p) for p in entry.give_ids] or [str(p) for p in entry.player_ids]
    for tx in rows:
        if tx.get("status") != "complete":
            continue
        drops = tx.get("drops") or {}
        for pid in pids:
            if _same_roster(drops.get(pid), my_roster_id):
                return COMPLETED, f"dropped via {tx.get('type') or 'transaction'}"
    return None, None


def _observe_trade(entry: LedgerEntry, rows: list[dict], my_roster_id: int) -> tuple[str | None, str | None]:
    want_out = {str(p) for p in entry.give_ids} | {("pick",) + tuple(k) for k in entry.give_picks}
    want_in = {str(p) for p in entry.receive_ids} | {("pick",) + tuple(k) for k in entry.receive_picks}
    best: tuple[str, str] | None = None
    for tx in rows:
        if tx.get("type") != "trade" or tx.get("status") != "complete":
            continue
        if not any(_same_roster(r, my_roster_id) for r in tx.get("roster_ids") or []):
            continue
        players_out, players_in, picks_out, picks_in, others = _trade_sides(tx, my_roster_id)
        got_out = players_out | {("pick",) + k for k in picks_out}
        got_in = players_in | {("pick",) + k for k in picks_in}
        if not (want_out <= got_out and want_in <= got_in):
            if want_out and want_out <= got_out:
                # Same players sent, a different return: the assets moved, the deal didn't.
                best = best or (PARTIALLY_MATCHED, f"sent the same pieces to roster {_first(others)} for a different return")
            continue
        same_counterparty = entry.counterparty_roster_id is None or entry.counterparty_roster_id in others
        exact = got_out == want_out and got_in == want_in
        if exact and same_counterparty:
            return COMPLETED, f"traded with roster {_first(others)}"
        if exact:
            return PARTIALLY_MATCHED, f"same assets, but with roster {_first(others)} instead of {entry.counterparty_roster_id}"
        detail = f"traded with roster {_first(others)}, with extra assets on one or both sides"
        if not same_counterparty:
            detail += f" (proposed counterparty was roster {entry.counterparty_roster_id})"
        return PARTIALLY_MATCHED, detail
    return best if best else (None, None)


def _first(values: set[int]) -> str:
    return str(sorted(values)[0]) if values else "?"


# -- reporting ------------------------------------------------------------------


def summary(ledger: Ledger) -> dict[str, dict[str, int]]:
    """Counts by action x outcome, for a diagnostics block. Open entries
    with no outcome yet are counted under "(open)"."""
    out: dict[str, dict[str, int]] = {}
    for entry in ledger.ordered():
        label = entry.outcome or "(open)"
        bucket = out.setdefault(entry.action, {})
        bucket[label] = bucket.get(label, 0) + 1
    return {action: dict(sorted(counts.items())) for action, counts in sorted(out.items())}


def describe_entry(entry: LedgerEntry) -> str:
    bits = [f"[{entry.league_name}] {entry.action}: {entry.subject}"]
    if entry.tier:
        bits.append(f"({entry.tier})")
    if entry.counterparty_name and entry.action in _TRADE_ACTIONS:
        bits.append(f"with {entry.counterparty_name}")
    bits.append(f"— first seen {entry.run_id[:10]}")
    if entry.outcome:
        tail = f"{entry.outcome}"
        if entry.outcome_detail:
            tail += f" ({entry.outcome_detail})"
        bits.append(f"— {tail}")
    if entry.failed_claim:
        bits.append("— a waiver claim of yours for him failed to process")
    return " ".join(bits)
