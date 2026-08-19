import datetime as dt

import pytest

from sleeper_tool.rankings import cache as cache_module
from sleeper_tool.rankings.cache import get_or_fetch, save_snapshot


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)


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
