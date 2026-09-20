"""The gate on replacing a cached snapshot.

Every source here can fail in a way that raises nothing. A renamed column,
a truncated download, one position list 502ing, a CDN serving a 404 that the
fetcher turns into an "absent" marker — each parses cleanly to a fraction of
a board, and without a gate each overwrites the good snapshot and reports
success. That is worse than an outage, because an outage is legible: a
suppressed feature says so, while a board with six of five hundred rows
produces confident advice about a league that does not exist.

Two rules cover all of it, and the second is the one that matters:

  an ABSOLUTE floor   — a whole board has at least this many rows. Cheap,
                        and catches the catastrophic cases.
  a RELATIVE floor    — a refresh that is drastically smaller than what it
                        replaces is a parse failure, whatever its absolute
                        size. This is the rule that works for feeds whose
                        right size changes every week (the nflverse files
                        grow all season), where no fixed number is correct.

A validator returns True, or a short string saying what is wrong; the string
becomes the recorded reason, and the previous snapshot is kept and served.
"""
from __future__ import annotations

from typing import Any, Callable

# Below this share of the previous snapshot, a refresh is a parse failure
# rather than a smaller board. Generous on purpose: ranking lists legitimately
# shrink week to week (players retire, lists get trimmed), and the failures
# this exists to catch are order-of-magnitude, not marginal.
MIN_SHARE_OF_PREVIOUS = 0.5


def rows_of(payload: Any) -> int | None:
    """Row count for the payload shapes used here: a bare list, or a dict
    carrying "rows". None when the shape is not countable at all."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        rows = payload.get("rows")
        return len(rows) if isinstance(rows, list) else None
    if isinstance(payload, (list, tuple)):
        return len(payload)
    return None


def by_row_count(
    *,
    label: str,
    floor: int = 0,
    extra: Callable[[Any], str | None] | None = None,
) -> Callable[[Any, Any], bool | str]:
    """A validator: an absolute floor, the relative rule, then `extra`.

    `extra(payload)` is for the checks a row count cannot make — a board
    whose ranks all collapsed to zero has the right number of rows and is
    still not a board.
    """

    def validate(payload: Any, previous: Any) -> bool | str:
        before = rows_of(getattr(previous, "payload", None))
        if not before:
            # Nothing to lose. This gate's contract is "never replace a good
            # snapshot with a worse one", and on a first fetch there is no
            # good snapshot — refusing here would leave the source with
            # nothing at all, which is strictly worse than caching a short
            # list that signal_health will grade Partial against its own
            # coverage floor. (KTC is the exception and validates
            # unconditionally: its parse path refuses a short board before
            # the cache is ever reached.)
            return True
        rows = rows_of(payload)
        if rows is None:
            return f"{label} payload is {type(payload).__name__}, with no rows to count"
        if rows < floor:
            return f"{label} refresh has {rows} rows, fewer than the {floor} a whole one carries"
        if rows < before * MIN_SHARE_OF_PREVIOUS:
            return (
                f"{label} refresh has {rows} rows against {before} in the cached one — "
                "a parse failure, not a smaller list"
            )
        # `extra` returns a reason or None; None means it passed, and only a
        # literal True is read as approval upstream.
        return (extra(payload) or True) if extra is not None else True

    return validate


def absent_marker_validator(
    *, label: str, floor: int = 0, extra: Callable[[Any], str | None] | None = None
) -> Callable[[Any, Any], bool | str]:
    """As `by_row_count`, but for the feeds that cache an explicit "this file
    is not published yet" marker.

    A 404 from the CDN is indistinguishable from "the season has not started"
    at the fetch layer, and the marker is what makes the preseason case cheap.
    The danger is the same marker arriving on a transient 404 in week 10 and
    replacing a season of data — which then renders as normal preseason
    behaviour rather than as a fault, because the health layer treats an
    absent file as expected. So a marker may only be written when there is
    nothing real to lose.
    """
    base = by_row_count(label=label, floor=floor, extra=extra)

    def validate(payload: Any, previous: Any) -> bool | str:
        if isinstance(payload, dict) and payload.get("absent"):
            before = rows_of(getattr(previous, "payload", None))
            if before:
                return (
                    f"{label} came back absent while the cached one holds {before} rows — "
                    "refusing to replace real data with a not-published marker"
                )
            return True
        return base(payload, previous)

    return validate
