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


@dataclass
class RankingSnapshot:
    source: str
    fetched_at: dt.datetime
    payload: Any

    def age(self) -> dt.timedelta:
        return dt.datetime.now(dt.timezone.utc) - self.fetched_at

    def to_json(self) -> dict:
        return {"source": self.source, "fetched_at": self.fetched_at.isoformat(), "payload": self.payload}

    @classmethod
    def from_json(cls, data: dict) -> "RankingSnapshot":
        return cls(
            source=data["source"],
            fetched_at=dt.datetime.fromisoformat(data["fetched_at"]),
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


def get_or_fetch(source: str, fetch_fn, *, max_age: dt.timedelta, force: bool = False) -> RankingSnapshot:
    """Return a cached snapshot if fresh enough, otherwise call fetch_fn() and cache the result.

    A live re-fetch failure (source down, page layout changed) falls back to
    a stale cached snapshot rather than propagating — for an unattended
    daily cron, "report built on N-hour-old data" (already surfaced via
    RankingSnapshot.age()/source_freshness()) is a far better failure mode
    than "no report at all". Only propagates if there's no cache to fall
    back to.
    """
    cached = load_snapshot(source)
    if not force and cached is not None and cached.age() <= max_age:
        return cached

    try:
        payload = fetch_fn()
    except Exception:
        if cached is not None:
            logger.warning("Live fetch failed for %s; falling back to cached snapshot from %s", source, cached.fetched_at)
            return cached
        raise
    return save_snapshot(source, payload)
