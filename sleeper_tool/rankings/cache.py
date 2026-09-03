"""Generic on-disk cache for scraped ranking snapshots, with a fetch date so
callers always know how fresh the data is. Ranking sites don't move fast
enough to justify hitting them on every single report run.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "rankings_cache"

# source -> outcome of the most recent get_or_fetch call in this process:
# "fresh"    the source was re-fetched and the cache rewritten
# "cached"   the cache was young enough that no fetch was attempted
# "fallback" the fetch failed and a stale cache was served in its place
# "failed"   the fetch failed with no usable cache; get_or_fetch raised
# Process-local and deliberately not persisted — it describes THIS run, and
# signal_health reads it to tell "served from a fallback" apart from "the
# cache was simply still fresh", which the snapshot alone can't distinguish.
last_fetch_outcome: dict[str, str] = {}


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


def _cache_path(source: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = source.replace("/", "_")
    return CACHE_DIR / f"{safe_name}.json"


def save_snapshot(source: str, payload: Any) -> RankingSnapshot:
    snapshot = RankingSnapshot(source=source, fetched_at=dt.datetime.now(dt.timezone.utc), payload=payload)
    _cache_path(source).write_text(json.dumps(snapshot.to_json()), encoding="utf-8")
    return snapshot


def load_snapshot(source: str) -> RankingSnapshot | None:
    path = _cache_path(source)
    if not path.exists():
        return None
    try:
        return RankingSnapshot.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def get_or_fetch(
    source: str,
    fetch_fn,
    *,
    max_age: dt.timedelta,
    force: bool = False,
    ceiling: dt.timedelta | None = None,
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

    The returned snapshot carries `served_from_fallback` and the outcome is
    recorded in the module-level `last_fetch_outcome` registry.
    """
    cached = load_snapshot(source)
    if not force and cached is not None and cached.age() <= max_age:
        last_fetch_outcome[source] = "cached"
        return cached

    try:
        payload = fetch_fn()
    except Exception:
        if cached is not None and (ceiling is None or cached.age() <= ceiling):
            logger.warning("Live fetch failed for %s; falling back to cached snapshot from %s", source, cached.fetched_at)
            cached.served_from_fallback = True
            last_fetch_outcome[source] = "fallback"
            return cached
        if cached is not None:
            logger.error(
                "Live fetch failed for %s and the cached snapshot from %s is past its %s ceiling; "
                "treating the source as unavailable rather than serving it",
                source,
                cached.fetched_at,
                ceiling,
            )
        last_fetch_outcome[source] = "failed"
        raise
    last_fetch_outcome[source] = "fresh"
    return save_snapshot(source, payload)
