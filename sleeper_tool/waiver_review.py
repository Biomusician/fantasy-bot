"""Waiver Review — once a league's waivers have run, "what happened?".

On a Wednesday the useful question stops being "what should I claim" and
becomes "what did I get, what did it cost, what did I lose and to whom".
Everything here is read off Sleeper's own transactions, so it is a record,
not a verdict:

  processed      a waiver transaction (complete or failed) stamped after the
                 claim window opened — the end of the previous NFL week's
                 last game day (NFL schedule), so last week's claims never
                 count as this week's
  won            a completed waiver claim adding a player to my roster
  lost           a FAILED waiver claim of mine — the only way this module
                 ever says I lost a player. A recommended player someone
                 else added, with no failed claim of mine on record, is
                 "taken by another team", never "lost": nothing shows I
                 submitted a claim.
  winning bid    the waiver_bid on the completed claim that took him
  next bid       the highest failed bid visible on the same player — so an
                 overpay can be stated as a fact ("won at $14; next visible
                 bid $6")

Recommended players come from the decision ledger: waiver entries recorded
for this league since the claim window opened, with the window and
recommended bid they were made with.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sleeper_tool.waiver_mode import local_now

WON = "won"
LOST = "lost"
TAKEN = "taken by another team"
STILL_AVAILABLE = "still available"
NOT_CLAIMED = "no claim of yours on record"


@dataclass
class ClaimOutcome:
    player_id: str
    name: str
    recommended: bool
    outcome: str
    winning_bid: int | None = None
    my_bid: int | None = None
    next_bid: int | None = None  # highest failed bid visible on the same player
    winner_roster_id: int | None = None
    recommended_bid: int | None = None
    window: tuple[int, int] | None = None

    def describe(self, names: dict[int, str] | None = None) -> str:
        who = (names or {}).get(self.winner_roster_id) or (f"roster {self.winner_roster_id}" if self.winner_roster_id else None)
        rec = f" (recommended ${self.recommended_bid}" + (f", window ${self.window[0]}–{self.window[1]}" if self.window else "") + ")" if self.recommended_bid is not None else ""
        if self.outcome == WON:
            nxt = f"; next visible bid ${self.next_bid}" if self.next_bid is not None else "; no competing bid visible"
            return f"Won {self.name} for ${self.winning_bid}{rec}{nxt}"
        if self.outcome == LOST:
            to = f" to {who}" if who else ""
            return f"Lost {self.name}{to} — your ${self.my_bid} vs winning ${self.winning_bid}{rec}"
        if self.outcome == TAKEN:
            bid = f" for ${self.winning_bid}" if self.winning_bid is not None else ""
            return f"{self.name} went to {who or 'another team'}{bid}{rec} — no claim of yours on record"
        return f"{self.name}: {self.outcome}{rec}"


@dataclass
class WaiverReview:
    processed: bool
    processed_at: dt.datetime | None = None
    outcomes: list[ClaimOutcome] = field(default_factory=list)
    my_spend: int = 0
    my_adds: list[str] = field(default_factory=list)  # names, every completed claim of mine this window
    notes: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        if not self.processed:
            return "Waivers have not run yet this week"
        won = sum(1 for o in self.outcomes if o.outcome == WON)
        lost = sum(1 for o in self.outcomes if o.outcome == LOST)
        return f"Waivers ran: {won} won, {lost} lost, ${self.my_spend} spent"


def claim_window_opened(schedule, claim_week: int | None) -> dt.datetime | None:
    """The day after the previous week's last game, midnight in the user's
    timezone, as UTC. None without a schedule or for week 1 (no previous
    week to close)."""
    if schedule is None or not claim_week or claim_week <= 1:
        return None
    days = [g.gameday for g in getattr(schedule, "games", ()) if g.game_type == "REG" and g.week == claim_week - 1 and g.gameday]
    if not days:
        return None
    last = max(dt.date.fromisoformat(d) for d in days)
    local_midnight = local_now(dt.datetime(last.year, last.month, last.day, 12, tzinfo=dt.timezone.utc)).replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
    return local_midnight.astimezone(dt.timezone.utc)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def _stamp(tx: dict) -> int:
    return int(tx.get("status_updated") or tx.get("created") or 0)


def _bid(tx: dict) -> int | None:
    bid = (tx.get("settings") or {}).get("waiver_bid")
    try:
        return int(bid) if bid is not None else None
    except (TypeError, ValueError):
        return None


def build_review(
    transactions: list[dict],
    *,
    my_roster_id: int,
    window_opened: dt.datetime | None,
    recommended: dict[str, dict] | None = None,  # player_id -> {"name", "recommended_bid", "window"}
    names: dict[str, str] | None = None,  # player_id -> name, for adds not in `recommended`
    rostered_ids: set[str] | None = None,
) -> WaiverReview:
    if window_opened is None:
        return WaiverReview(processed=False, notes=["no claim window to review (no NFL schedule, or week 1)"])
    since = _ms(window_opened)
    waivers = [t for t in transactions if t.get("type") == "waiver" and _stamp(t) >= since and t.get("status") in ("complete", "failed")]
    if not waivers:
        return WaiverReview(processed=False)
    review = WaiverReview(processed=True, processed_at=dt.datetime.fromtimestamp(max(_stamp(t) for t in waivers) / 1000, dt.timezone.utc))
    recommended = recommended or {}
    names = names or {}

    completed: dict[str, dict] = {}
    failed_bids: dict[str, list[tuple[int | None, int | None]]] = {}  # pid -> [(roster, bid)]
    for tx in waivers:
        for pid, roster in (tx.get("adds") or {}).items():
            pid = str(pid)
            if tx.get("status") == "complete":
                completed[pid] = {"roster": roster, "bid": _bid(tx)}
            else:
                failed_bids.setdefault(pid, []).append((roster, _bid(tx)))

    for pid, row in completed.items():
        if row["roster"] == my_roster_id:
            review.my_spend += row["bid"] or 0
            review.my_adds.append(recommended.get(pid, {}).get("name") or names.get(pid) or pid)

    for pid in sorted(set(recommended) | {p for p, rows in failed_bids.items() if any(r == my_roster_id for r, _ in rows)} | {p for p, r in completed.items() if r["roster"] == my_roster_id}):
        rec = recommended.get(pid, {})
        name = rec.get("name") or names.get(pid) or pid
        mine_failed = [b for r, b in failed_bids.get(pid, []) if r == my_roster_id]
        others_failed = [b for r, b in failed_bids.get(pid, []) if b is not None and r != my_roster_id]
        done = completed.get(pid)
        out = ClaimOutcome(
            player_id=pid, name=name, recommended=pid in recommended,
            outcome=NOT_CLAIMED, recommended_bid=rec.get("recommended_bid"), window=rec.get("window"),
        )
        if done is not None and done["roster"] == my_roster_id:
            out.outcome, out.winning_bid = WON, done["bid"]
            out.next_bid = max(others_failed) if others_failed else None
        elif done is not None:
            out.winning_bid, out.winner_roster_id = done["bid"], done["roster"]
            out.outcome = LOST if mine_failed else TAKEN
            out.my_bid = max((b for b in mine_failed if b is not None), default=None)
        elif mine_failed:
            # My claim failed and nobody's completed: an invalid claim (the
            # drop was already gone, the budget was spent) — still a record.
            out.outcome, out.my_bid = "claim failed without a winner", max((b for b in mine_failed if b is not None), default=None)
        elif rostered_ids is not None and pid not in rostered_ids:
            out.outcome = STILL_AVAILABLE
        review.outcomes.append(out)
    order = {WON: 0, LOST: 1, TAKEN: 2}
    review.outcomes.sort(key=lambda o: (order.get(o.outcome, 3), not o.recommended, o.name))
    return review
