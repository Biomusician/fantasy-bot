"""Generic on-disk cache for scraped ranking snapshots, with a fetch date so
callers always know how fresh the data is. Ranking sites don't move fast
enough to justify hitting them on every single report run.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "rankings_cache"

# source -> outcome of the most recent get_or_fetch call in this process:
# "fresh"     the source was re-fetched, validated, and the cache rewritten
# "cached"    the cache was young enough that no fetch was attempted
# "fallback"  the fetch or parse failed and a cache was served in its place
# "rejected"  the refresh produced something that failed validation; the
#             PREVIOUS cache was kept and served rather than overwritten
# "unsaved"   the refresh was good but could not be written; it is served
#             from memory and the previous file is left as it was
# "failed"    no usable cache and nothing to serve; get_or_fetch raised
# Process-local and deliberately not persisted — it describes THIS run, and
# signal_health reads it to tell "served from a fallback" apart from "the
# cache was simply still fresh", which the snapshot alone can't distinguish.
last_fetch_outcome: dict[str, str] = {}

# source -> the exception text behind a "fallback"/"rejected"/"failed"
# outcome. Same lifetime and purpose as last_fetch_outcome: a run that
# serves yesterday's board should be able to say WHY, and "Unavailable"
# with no reason is what made the last source break take an afternoon to
# diagnose instead of a minute.
last_fetch_error: dict[str, str] = {}


def _aware(stamp: dt.datetime) -> dt.datetime:
    """A hand-edited or older cache file may carry a naive timestamp; read
    it as UTC rather than failing every age comparison downstream."""
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=dt.timezone.utc)


@dataclass
class RankingSnapshot:
    source: str
    fetched_at: dt.datetime
    payload: Any
    # Set when get_or_fetch served this snapshot because a live re-fetch
    # failed, not because it was still fresh. Never written to disk: it's a
    # fact about how this object was obtained, not about the cached data.
    served_from_fallback: bool = False

    def age(self) -> dt.timedelta:
        return dt.datetime.now(dt.timezone.utc) - self.fetched_at

    def to_json(self) -> dict:
        return {"source": self.source, "fetched_at": self.fetched_at.isoformat(), "payload": self.payload}

    @classmethod
    def from_json(cls, data: dict) -> "RankingSnapshot":
        return cls(
            source=data["source"],
            fetched_at=_aware(dt.datetime.fromisoformat(data["fetched_at"])),
            payload=data["payload"],
        )


def _cache_path(source: str, *, create: bool = True) -> Path:
    """The file this source caches to. `create=False` for read paths: a
    directory that cannot be created is a reason for ONE source to have no
    cache, not for the run to die inside a health check."""
    if create:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = source.replace("/", "_")
    return CACHE_DIR / f"{safe_name}.json"


# Parsed cache files, keyed on the resolved PATH (never the source name, so
# a test pointing CACHE_DIR at a tmp_path can't collide with the real one)
# and validated against mtime + size. In-season the nflverse identity files
# are several megabytes and get asked for two or three times a run;
# re-parsing them was costing more than every ranking source put together.
# Process-local, and bounded — one entry per distinct file, wiped wholesale
# if that ever runs away (a long test session sweeping temp directories).
_PARSED_LIMIT = 64
_parsed_cache: dict[str, tuple[int, int, Any]] = {}
# Windows stamps last-write times from the coarse system clock (~15ms
# ticks), so two rewrites of the same length inside one tick can share an
# mtime and a memo keyed on it alone would serve the first one's content.
# A file must therefore have been sitting still for longer than any
# plausible tick before we trust the memo. Real cache files are hours old
# and always qualify; a test that writes, reads and rewrites in the same
# millisecond simply re-parses, which for a fixture-sized file is free.
_SETTLED_NS = 1_000_000_000

# See _replace_with_retry: a scanner's handle on the destination clears in
# well under a second, so a few short attempts cover it without making a
# genuinely locked file slow to fail.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF = 0.05


def save_snapshot(source: str, payload: Any) -> RankingSnapshot:
    """Write atomically: serialize to a temp file in the same directory and
    os.replace it into place.

    A partial write is not a harmless one. `load_snapshot` treats an
    unparseable file as no cache at all, so a process killed mid-write (or
    a full disk) turns a good snapshot into an Unavailable source and
    suppresses everything that rested on it.
    """
    snapshot = RankingSnapshot(source=source, fetched_at=dt.datetime.now(dt.timezone.utc), payload=payload)
    path = _cache_path(source)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot.to_json()))
            handle.flush()
            # Without this, NTFS can commit the directory entry while the
            # data blocks are still unflushed: after a power loss the file
            # exists and is empty, which load_snapshot reads as no cache.
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _parsed_cache.pop(str(path), None)  # don't lean on mtime for our own writes
    return snapshot


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """os.replace, retried briefly.

    The replace is atomic when it succeeds, but on Windows it fails outright
    with PermissionError while any handle is open on the destination — an
    antivirus scanner holding a just-written .json is the ordinary case, not
    an exotic one. Without the retry a scan lands on the one write a day the
    cron makes, the write raises, and a freshly fetched board is thrown away.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF)


def load_snapshot(source: str) -> RankingSnapshot | None:
    try:
        path = _cache_path(source, create=False)
        stat = path.stat()
    except OSError:  # missing, unreadable, or vanished between check and read
        return None
    key = str(path)
    hit = _parsed_cache.get(key)
    settled = time.time_ns() - stat.st_mtime_ns > _SETTLED_NS
    if hit is not None and settled and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
        data = hit[2]
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            _parsed_cache.pop(key, None)
            return None
        if len(_parsed_cache) >= _PARSED_LIMIT:
            _parsed_cache.clear()
        _parsed_cache[key] = (stat.st_mtime_ns, stat.st_size, data)
    # A fresh RankingSnapshot per call: get_or_fetch flips
    # served_from_fallback on the object it returns, which must not leak
    # into the next caller. Only the (read-only) payload is shared.
    try:
        return RankingSnapshot.from_json(data)
    except (KeyError, ValueError):
        return None


def get_or_fetch(
    source: str,
    fetch_fn,
    *,
    max_age: dt.timedelta,
    force: bool = False,
    ceiling: dt.timedelta | None = None,
    validate=None,
) -> RankingSnapshot:
    """Return a cached snapshot if fresh enough, otherwise call fetch_fn() and cache the result.

    A live re-fetch failure (source down, page layout changed) falls back to
    a stale cached snapshot rather than propagating — for an unattended
    daily cron, "report built on N-hour-old data" (already surfaced via
    RankingSnapshot.age()/source_freshness()) is a far better failure mode
    than "no report at all".

    `ceiling` bounds that generosity. Without one, a source that has been
    dead for a month keeps quietly serving month-old numbers and the report
    keeps looking normal. Past the ceiling the fallback is refused and the
    exception propagates, so the caller can treat the source as Unavailable
    and suppress what depended on it rather than publishing stale advice.
    A snapshot exactly AT the ceiling is still served — the ceiling is the
    oldest acceptable age, not the first unacceptable one.

    `validate(payload)` is the gate on WRITING. A fetch that succeeds and
    parses to something implausible — three players where a board carries
    five hundred — is a worse outcome than a fetch that raises, because it
    replaces a good snapshot and still reports success. A payload that fails
    validation is refused: the previous cache is kept and served (subject to
    the same ceiling), and the outcome is "rejected". With no cache to keep,
    the refusal raises like any other failure.

    `validate(payload, previous)` is given the snapshot it would replace,
    because the rule that catches most of these is relative: a refresh
    drastically smaller than what is already cached is a parse failure
    whatever its absolute size, and for feeds that grow all season no fixed
    floor is correct. Return True, or a short string saying what is wrong —
    that string becomes the recorded reason, and "6 rows against 500 in the
    cached one" is worth considerably more at 9am than "failed validation".

    The returned snapshot carries `served_from_fallback` and the outcome is
    recorded in `last_fetch_outcome`, with the reason in `last_fetch_error`.
    """
    cached = load_snapshot(source)
    if not force and cached is not None and cached.age() <= max_age:
        last_fetch_outcome[source] = "cached"
        last_fetch_error.pop(source, None)
        return cached

    def _keep_cached(outcome: str, reason: str) -> RankingSnapshot | None:
        """Serve the snapshot we already have, if policy still allows it."""
        if cached is not None and (ceiling is None or cached.age() <= ceiling):
            logger.warning(
                "Refresh of %s did not produce a usable board (%s); keeping the cached snapshot from %s",
                source, reason, cached.fetched_at,
            )
            cached.served_from_fallback = True
            last_fetch_outcome[source] = outcome
            last_fetch_error[source] = reason
            return cached
        if cached is not None:
            logger.error(
                "Refresh of %s failed (%s) and the cached snapshot from %s is past its %s ceiling; "
                "treating the source as unavailable rather than serving it",
                source, reason, cached.fetched_at, ceiling,
            )
        last_fetch_outcome[source] = "failed"
        last_fetch_error[source] = reason
        return None

    try:
        payload = fetch_fn()
    except Exception as exc:
        if (kept := _keep_cached("fallback", f"{type(exc).__name__}: {exc}")) is not None:
            return kept
        raise

    if validate is not None:
        try:
            verdict = validate(payload, cached)
        except Exception as exc:  # a broken validator must not take down a good source
            logger.exception("Validator for %s raised; treating the refresh as unusable", source)
            verdict = f"validator raised {type(exc).__name__}: {exc}"
        if verdict is not True:
            reason = verdict if isinstance(verdict, str) and verdict else f"refreshed payload failed validation for {source}"
            if (kept := _keep_cached("rejected", reason)) is not None:
                return kept
            raise ValueError(reason)

    try:
        snapshot = save_snapshot(source, payload)
    except Exception as exc:
        # The board itself is good and already in hand; only the write
        # failed. Throwing it away would turn a disk hiccup into an
        # Unavailable source for the whole run.
        reason = f"fetched board could not be cached — {type(exc).__name__}: {exc}"
        logger.error("Could not write the %s cache: %s", source, exc)
        last_fetch_outcome[source] = "unsaved"
        last_fetch_error[source] = reason
        return RankingSnapshot(source=source, fetched_at=dt.datetime.now(dt.timezone.utc), payload=payload)
    last_fetch_outcome[source] = "fresh"
    last_fetch_error.pop(source, None)
    return snapshot
