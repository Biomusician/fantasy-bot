import datetime as dt
import json

import pytest

from sleeper_tool.rankings import cache as cache_module
from sleeper_tool.rankings.cache import get_or_fetch, load_snapshot, save_snapshot


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache_module, "last_fetch_outcome", {})


def _age_snapshot(source: str, payload, age: dt.timedelta) -> None:
    """Write a snapshot whose fetched_at is `age` in the past."""
    snapshot = save_snapshot(source, payload)
    snapshot.fetched_at = dt.datetime.now(dt.timezone.utc) - age
    cache_module._cache_path(source).write_text(
        json.dumps(snapshot.to_json()), encoding="utf-8"
    )


@pytest.fixture
def frozen_age(monkeypatch):
    """Pin RankingSnapshot.age() to an exact value.

    Boundary cases here are exact comparisons (age == ceiling), and a
    wall-clock age computed from a stamped fetched_at is always a few
    microseconds past whatever it was set to.
    """

    def _set(age: dt.timedelta):
        monkeypatch.setattr(cache_module.RankingSnapshot, "age", lambda self: age)

    return _set


def _boom():
    raise RuntimeError("source unreachable")


def test_get_or_fetch_returns_stale_cache_when_live_fetch_fails():
    # Regression: a live fetch failure (source down, page layout changed)
    # previously propagated uncaught, aborting the ENTIRE daily report for
    # a single ranking source hiccup. It should fall back to whatever's
    # cached, even if stale.
    save_snapshot("test_source", {"players": ["stale data"]})

    def failing_fetch():
        raise RuntimeError("source unreachable")

    result = get_or_fetch("test_source", failing_fetch, max_age=dt.timedelta(seconds=0))
    assert result.payload == {"players": ["stale data"]}


def test_get_or_fetch_propagates_when_no_cache_exists_at_all():
    def failing_fetch():
        raise RuntimeError("source unreachable")

    with pytest.raises(RuntimeError):
        get_or_fetch("never_cached_source", failing_fetch, max_age=dt.timedelta(hours=1))


def test_get_or_fetch_uses_fresh_cache_without_calling_fetch_fn():
    save_snapshot("fresh_source", {"players": ["fresh"]})
    calls = []

    def fetch_fn():
        calls.append(1)
        return {"players": ["should not be called"]}

    result = get_or_fetch("fresh_source", fetch_fn, max_age=dt.timedelta(hours=1))
    assert result.payload == {"players": ["fresh"]}
    assert calls == []


def test_get_or_fetch_refetches_when_cache_is_stale_and_fetch_succeeds():
    save_snapshot("stale_source", {"players": ["old"]})
    result = get_or_fetch("stale_source", lambda: {"players": ["new"]}, max_age=dt.timedelta(seconds=-1))
    assert result.payload == {"players": ["new"]}


# -- stale-cache ceiling ----------------------------------------------------


def test_fallback_is_served_when_cache_age_exactly_equals_the_ceiling(frozen_age):
    # The ceiling is the oldest acceptable age, not the first unacceptable
    # one — an age exactly at the boundary is still served.
    ceiling = dt.timedelta(days=7)
    save_snapshot("boundary_source", {"players": ["old"]})
    frozen_age(ceiling)

    result = get_or_fetch(
        "boundary_source", _boom, max_age=dt.timedelta(hours=20), ceiling=ceiling
    )
    assert result.payload == {"players": ["old"]}
    assert result.served_from_fallback is True


def test_fallback_is_refused_one_microsecond_past_the_ceiling(frozen_age):
    ceiling = dt.timedelta(days=7)
    save_snapshot("edge_source", {"players": ["old"]})
    frozen_age(ceiling + dt.timedelta(microseconds=1))

    with pytest.raises(RuntimeError):
        get_or_fetch("edge_source", _boom, max_age=dt.timedelta(hours=20), ceiling=ceiling)


def test_fallback_is_refused_past_the_ceiling_so_the_source_reads_as_unavailable():
    ceiling = dt.timedelta(days=7)
    _age_snapshot("expired_source", {"players": ["ancient"]}, ceiling + dt.timedelta(hours=1))

    with pytest.raises(RuntimeError):
        get_or_fetch("expired_source", _boom, max_age=dt.timedelta(hours=20), ceiling=ceiling)
    assert cache_module.last_fetch_outcome["expired_source"] == "failed"
    # The cache itself is left alone — a later successful fetch overwrites it.
    assert load_snapshot("expired_source") is not None


def test_no_ceiling_keeps_the_old_unbounded_fallback_behaviour():
    _age_snapshot("unbounded_source", {"players": ["very old"]}, dt.timedelta(days=400))
    result = get_or_fetch("unbounded_source", _boom, max_age=dt.timedelta(hours=20))
    assert result.payload == {"players": ["very old"]}
    assert result.served_from_fallback is True


# -- served_from_fallback and the outcome registry --------------------------


def test_fallback_flag_is_false_on_the_fresh_and_cached_paths():
    fresh = get_or_fetch("flag_fresh", lambda: {"a": 1}, max_age=dt.timedelta(hours=1))
    assert fresh.served_from_fallback is False
    assert cache_module.last_fetch_outcome["flag_fresh"] == "fresh"

    cached = get_or_fetch("flag_fresh", _boom, max_age=dt.timedelta(hours=1))
    assert cached.served_from_fallback is False
    assert cache_module.last_fetch_outcome["flag_fresh"] == "cached"


def test_fallback_flag_is_set_and_recorded_on_the_fallback_path():
    _age_snapshot("flag_fallback", {"a": 1}, dt.timedelta(days=2))
    result = get_or_fetch(
        "flag_fallback", _boom, max_age=dt.timedelta(hours=20), ceiling=dt.timedelta(days=7)
    )
    assert result.served_from_fallback is True
    assert cache_module.last_fetch_outcome["flag_fallback"] == "fallback"


def test_outcome_is_failed_when_there_is_no_cache_at_all():
    with pytest.raises(RuntimeError):
        get_or_fetch("nothing_cached", _boom, max_age=dt.timedelta(hours=1))
    assert cache_module.last_fetch_outcome["nothing_cached"] == "failed"


def test_fallback_flag_is_not_persisted_to_disk():
    _age_snapshot("not_persisted", {"a": 1}, dt.timedelta(days=2))
    served = get_or_fetch(
        "not_persisted", _boom, max_age=dt.timedelta(hours=20), ceiling=dt.timedelta(days=7)
    )
    assert served.served_from_fallback is True
    # A fresh read of the same file knows nothing about how it was once served.
    assert load_snapshot("not_persisted").served_from_fallback is False
