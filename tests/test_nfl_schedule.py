import datetime as dt

from sleeper_tool.nfl_schedule import Schedule, load_schedule, normalize_team, parse_schedule_csv, schedule_from_rows
from sleeper_tool.rankings import cache as cache_mod

CSV = """game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,home_team,home_score,spread_line
2026_01_KC_LA,2026,REG,1,2026-09-10,Thursday,20:20,KC,,LA,,-2.5
2026_01_DAL_PHI,2026,REG,1,2026-09-13,Sunday,13:00,DAL,,PHI,,3
2026_02_LA_DAL,2026,REG,2,2026-09-20,Sunday,13:00,LA,,DAL,,1
2026_02_PHI_KC,2026,REG,2,2026-09-20,Sunday,16:25,PHI,,KC,,-3
2026_03_KC_DAL,2026,REG,3,2026-09-27,Sunday,13:00,KC,,DAL,,-1
2026_19_KC_PHI,2026,POST,19,2027-01-10,Sunday,13:00,KC,,PHI,,0
2025_01_KC_LA,2025,REG,1,2025-09-07,Sunday,13:00,KC,,LA,,-2.5
"""


def _schedule():
    return schedule_from_rows(parse_schedule_csv(CSV, 2026), 2026)


def test_parse_keeps_only_the_season_and_normalizes_the_rams():
    rows = parse_schedule_csv(CSV, 2026)
    assert len(rows) == 6 and all(r["season"] == 2026 for r in rows)
    assert {r["home"] for r in rows} >= {"LAR", "PHI", "DAL", "KC"}
    assert normalize_team("la") == "LAR" and normalize_team("KC") == "KC" and normalize_team(None) is None


def test_opponents_byes_and_regular_weeks():
    s = _schedule()
    assert s.regular_weeks() == [1, 2, 3] and s.last_regular_week() == 3
    assert s.opponent("LAR", 1) == "KC" and s.opponent("KC", 1) == "LAR"
    assert s.opponent("LAR", 3) is None and s.is_bye("LAR", 3)  # scheduled week, no game
    assert s.opponent("PHI", 3) is None and s.is_bye("PHI", 3)
    assert not s.is_bye("LAR", 19)  # postseason week is not a regular-season bye
    assert s.bye_weeks("LAR") == {3} and s.bye_weeks("KC") == set()


def test_unknown_team_or_week_is_not_a_bye():
    s = _schedule()
    assert not s.is_bye("NOPE", 1) and s.bye_weeks("NOPE") == set() and s.opponent(None, 1) is None
    assert not s.is_bye("KC", 9)  # week 9 isn't in this (fixture) schedule: unknown, not bye


def test_load_schedule_uses_the_daily_cache_and_refetches_a_different_season(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path)
    calls = []

    def fake_fetch(season):
        calls.append(season)
        return {"season": season, "rows": parse_schedule_csv(CSV, season)}

    import sleeper_tool.nfl_schedule as mod
    monkeypatch.setattr(mod, "fetch_schedule_rows", fake_fetch)
    first = load_schedule(2026)
    assert isinstance(first, Schedule) and first.regular_weeks() == [1, 2, 3] and calls == [2026]
    again = load_schedule(2026)  # fresh cache: no second fetch
    assert calls == [2026] and again.fetched_at == first.fetched_at
    load_schedule(2025)  # a different season in the cache is stale, whatever its age
    assert calls == [2026, 2025]
    load_schedule(2025, force=True)
    assert calls == [2026, 2025, 2025]


def test_load_schedule_is_none_when_nothing_is_available(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path)
    import sleeper_tool.nfl_schedule as mod

    def boom(season):
        raise ConnectionError("offline")

    monkeypatch.setattr(mod, "fetch_schedule_rows", boom)
    assert load_schedule(2026) is None


def _league_csv(teams: int = 32, weeks: int = 7) -> str:
    codes = [f"T{i:02d}" for i in range(teams)]
    lines = ["game_id,season,game_type,week,gameday,away_team,home_team"]
    for week in range(1, weeks + 1):
        for i in range(teams // 2):
            away, home = codes[i], codes[teams - 1 - i]
            lines.append(f"2026_{week:02d}_{away}_{home},2026,REG,{week},2026-09-10,{away},{home}")
    return "\n".join(lines) + "\n"


class _Prev:
    def __init__(self, payload):
        self.payload = payload


def test_a_whole_league_refresh_replaces_the_cached_schedule():
    import sleeper_tool.nfl_schedule as mod
    payload = {"season": 2026, "rows": parse_schedule_csv(_league_csv(), 2026)}
    assert mod._validate_schedule(payload, _Prev(payload)) is True


def test_a_refresh_missing_most_teams_is_refused():
    import sleeper_tool.nfl_schedule as mod
    full = {"season": 2026, "rows": parse_schedule_csv(_league_csv(), 2026)}
    partial = {"season": 2026, "rows": parse_schedule_csv(_league_csv(teams=8, weeks=30), 2026)}
    verdict = mod._validate_schedule(partial, _Prev(full))
    assert isinstance(verdict, str) and "8 teams" in verdict


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Client Error")


def test_fetch_falls_back_to_the_source_repo_when_the_release_asset_404s(monkeypatch):
    import sleeper_tool.nfl_schedule as mod
    asked = []

    def fake_get(url, **kwargs):
        asked.append(url)
        return _Resp(404) if url == mod.SCHEDULE_URL else _Resp(200, CSV)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    payload = mod.fetch_schedule_rows(2026)
    assert asked == [mod.SCHEDULE_URL, mod.SCHEDULE_MIRROR_URL]
    assert payload["season"] == 2026 and len(payload["rows"]) == 6


def test_fetch_raises_naming_every_source_when_all_fail(monkeypatch):
    import sleeper_tool.nfl_schedule as mod
    monkeypatch.setattr(mod.requests, "get", lambda url, **kwargs: _Resp(404))
    try:
        mod.fetch_schedule_rows(2026)
    except RuntimeError as exc:
        assert mod.SCHEDULE_URL in str(exc) and mod.SCHEDULE_MIRROR_URL in str(exc)
    else:
        raise AssertionError("expected every source to fail")


def test_stale_cache_survives_a_failed_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path)
    import sleeper_tool.nfl_schedule as mod
    old = cache_mod.save_snapshot(mod.SCHEDULE_SOURCE, {"season": 2026, "rows": parse_schedule_csv(CSV, 2026)})
    old.fetched_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    cache_mod._cache_path(mod.SCHEDULE_SOURCE).write_text(__import__("json").dumps(old.to_json()), encoding="utf-8")

    def boom(season):
        raise ConnectionError("offline")

    monkeypatch.setattr(mod, "fetch_schedule_rows", boom)
    s = load_schedule(2026)
    assert s is not None and s.regular_weeks() == [1, 2, 3]
