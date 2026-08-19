"""Thin wrapper around the public, unauthenticated Sleeper API.

Docs: https://docs.sleeper.com/ — every endpoint here is read-only and needs
no API key. We still retry transient failures and keep a soft rate limit
(Sleeper asks to stay under 1000 req/min) since a full sync across many
leagues can issue a couple hundred requests back to back.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from sleeper_tool.config import SLEEPER_BASE_URL

logger = logging.getLogger(__name__)

USER_AGENT = "sleeper-dynasty-tool/0.1 (personal use; contact via Sleeper app)"


class SleeperAPIError(RuntimeError):
    """Raised when the Sleeper API returns an error we can't recover from."""


class SleeperClient:
    def __init__(
        self,
        base_url: str = SLEEPER_BASE_URL,
        session: requests.Session | None = None,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.5,
        min_request_interval_seconds: float = 0.06,  # ~1000/min ceiling
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self._last_request_at = 0.0

    # -- low level -----------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, *, allow_404: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=20)
                self._last_request_at = time.monotonic()
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("GET %s failed (attempt %d/%d): %s", url, attempt, self.max_retries, exc)
                time.sleep(self.retry_backoff_seconds * attempt)
                continue

            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code == 429:
                logger.warning("Rate limited on %s, backing off", url)
                time.sleep(self.retry_backoff_seconds * attempt * 2)
                continue
            if 500 <= resp.status_code < 600:
                last_exc = SleeperAPIError(f"{resp.status_code} from {url}")
                time.sleep(self.retry_backoff_seconds * attempt)
                continue
            if not resp.ok:
                raise SleeperAPIError(f"GET {url} -> {resp.status_code}: {resp.text[:300]}")

            if not resp.content:
                return None
            return resp.json()

        raise SleeperAPIError(f"GET {url} failed after {self.max_retries} attempts") from last_exc

    # -- users -----------------------------------------------------------

    def get_user(self, username_or_id: str) -> dict:
        return self._get(f"/user/{username_or_id}")

    def get_user_leagues(self, user_id: str, season: str, sport: str = "nfl") -> list[dict]:
        return self._get(f"/user/{user_id}/leagues/{sport}/{season}") or []

    # -- leagues -----------------------------------------------------------

    def get_league(self, league_id: str) -> dict:
        return self._get(f"/league/{league_id}")

    def get_rosters(self, league_id: str) -> list[dict]:
        return self._get(f"/league/{league_id}/rosters") or []

    def get_league_users(self, league_id: str) -> list[dict]:
        return self._get(f"/league/{league_id}/users") or []

    def get_matchups(self, league_id: str, week: int) -> list[dict]:
        return self._get(f"/league/{league_id}/matchups/{week}") or []

    def get_transactions(self, league_id: str, week: int) -> list[dict]:
        return self._get(f"/league/{league_id}/transactions/{week}") or []

    def get_traded_picks(self, league_id: str) -> list[dict]:
        return self._get(f"/league/{league_id}/traded_picks") or []

    def get_drafts(self, league_id: str) -> list[dict]:
        return self._get(f"/league/{league_id}/drafts") or []

    # -- players -----------------------------------------------------------

    def get_all_players(self, sport: str = "nfl") -> dict[str, dict]:
        """~5MB dictionary of every player Sleeper tracks. Cache this — see players_cache.py."""
        return self._get(f"/players/{sport}") or {}

    def get_trending_players(
        self,
        sport: str = "nfl",
        trend_type: str = "add",
        lookback_hours: int = 48,
        limit: int = 25,
    ) -> list[dict]:
        if trend_type not in ("add", "drop"):
            raise ValueError("trend_type must be 'add' or 'drop'")
        return self._get(
            f"/players/{sport}/trending/{trend_type}?lookback_hours={lookback_hours}&limit={limit}"
        ) or []

    # -- state -----------------------------------------------------------

    def get_nfl_state(self) -> dict:
        return self._get("/state/nfl")
