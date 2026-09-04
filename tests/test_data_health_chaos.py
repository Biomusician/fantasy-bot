"""Every input source missing, stale, partial, malformed or duplicated —
through the whole report path, on the synthetic league, out both renderers.

The other suites test the health layer's grammar (test_signal_health) and
each fetcher's own cache (test_nfl_usage, test_nfl_schedule,
test_rankings_cache). This one asks the question a reader has on a bad
morning: with THIS source gone, does the report still say what it can,
say what it can't, and never dress a gap up as a number?

The bar for every case is the same five things:

  1. no league errors — one bad source never blanks a league;
  2. the health block names the missing / stale / partial signal, in the
     Markdown and in the HTML;
  3. the suppression map names the features that rest on it;
  4. everything that does NOT rest on it still produces output;
  5. nothing renders "None"/"nan", nothing reads like a probability, and
     a run with a ranking source Unavailable or served from a fallback is
     never persisted as a complete baseline.

Every build is memoized per scenario (one build, several tests), because
the suite's budget is a few seconds and a full build is ~40ms.

No network, no `data/`: the rankings cache is pointed at a temp directory
for every build, the nflverse fetchers are replaced with in-memory CSV,
and the watchlist/ledger/snapshot loaders are stubbed exactly as in
test_end_to_end.

Genuine bugs found while writing this are `xfail(strict=True)` at the
bottom, each with its repro in the docstring.
"""
from __future__ import annotations

import datetime as dt
import gzip
import html as html_lib
import json
import logging
import re
from pathlib import Path

import pytest

from fake_storage import (
    NFL_TEAMS,
    FakeStorage,
    _sorted_skill_ids,
    build_fp_payload,
    build_ktc_payload,
    build_rb_payload,
    make_snapshot,
    make_storage,
    make_synthetic_league,
)

import sleeper_tool.report_data as rd
import sleeper_tool.signal_health as sh
from sleeper_tool import nfl_schedule, nfl_usage
from sleeper_tool.decision_delta import is_complete_run, load_latest_snapshot, load_snapshots
from sleeper_tool.html_report import render_dashboard_html
from sleeper_tool.nfl_usage import AssetAbsent
from sleeper_tool.rankings import cache as cache_mod
from sleeper_tool.rankings import ktc
from sleeper_tool.rankings.fantasypros import FANTASYPROS_PAGES
from sleeper_tool.rankings.freshness import MIN_COVERAGE, SOURCE_WINDOWS
from sleeper_tool.rankings.rotoballer import ROTOBALLER_SPREADSHEETS
from sleeper_tool.report import render_weekly_report
from sleeper_tool.report_data import build_weekly_report_data
from sleeper_tool.role_trends import INSUFFICIENT
from sleeper_tool.signal_health import FRESH, PARTIAL, STALE, UNAVAILABLE, USABLE
from sleeper_tool.sync import save_trending_if_nonempty
from sleeper_tool.valuation import ValuationEngine

SEASON = 2026
NOW = dt.datetime.now(dt.timezone.utc)

# With the schedule and usage fetches switched off, these two are always
# suppressed; a scenario's own suppressions are asserted on top of them.
OFFLINE_SUPPRESSED = {"role_trends", "schedule_windows"}


# ---------------------------------------------------------------------------
# rendered-text checks shared by every scenario
# ---------------------------------------------------------------------------

_NONE_IN_HTML_CELL = re.compile(r">([^<>]*\b(?:None|nan|NaN|inf)\b[^<>]*)<")
_NONE_IN_MD = re.compile(r"\b(?:None|nan|NaN|inf)\b")
# "62% chance", "acceptance: 40%", "probability" — anything that turns a
# bucketed rating into a number. Legit percentages (percentiles, "+20%
# value", "85% of your weakest starter") never sit next to these words.
_PROBABILITY = re.compile(
    r"\d{1,3}(?:\.\d+)?\s?%\W{0,3}(?:chance|likel|probab|odds|accept)"
    r"|(?:accept|chance|probab|odds)\w*\W{0,3}\d{1,3}(?:\.\d+)?\s?%"
    r"|\bprobability\b",
    re.I,
)


def _honest_renders(report) -> tuple[str, str]:
    """Both renderers, with the leak checks every scenario shares."""
    md = render_weekly_report(report)
    html = render_dashboard_html(report)
    leaked_md = [line for line in md.splitlines() if _NONE_IN_MD.search(line)]
    assert not leaked_md, f"literal None/nan in the Markdown: {leaked_md[:3]}"
    leaked_html = [m.group(1) for m in _NONE_IN_HTML_CELL.finditer(html)]
    assert not leaked_html, f"literal None/nan in an HTML cell: {leaked_html[:3]}"
    prob = [line for line in md.splitlines() if _PROBABILITY.search(line)] + _PROBABILITY.findall(html)
    assert not prob, f"probability-looking text: {prob[:3]}"
    return md, html


def _health_line(report, source: str) -> str:
    line = next(line for line in report.freshness_lines if line.startswith(sh._display_name(source) + " ·"))
    assert f"- {line}" in report.md, f"health line missing from the Markdown: {line}"
    # The HTML renders chips, not the joined line; the display name and
    # the label both have to be there.
    assert sh._display_name(source) in report.html_text
    return line


def _note_in_both(report, fragment: str) -> str:
    note = next((n for n in report.health.notes if fragment in n), None)
    assert note is not None, f"no health note containing {fragment!r}: {report.health.notes}"
    assert note in report.md and note in report.html_text
    return note


# ---------------------------------------------------------------------------
# building a scenario
# ---------------------------------------------------------------------------


def _engine(players, *, ktc_on=True, fp_on=True, rb_on=True, ktc_rows=None, fp_rows=None, rb_rows=None,
            age_hours: float = 2.0, current_week: int | None = 3) -> ValuationEngine:
    """Like fake_storage.make_engine, but any family can be switched off
    (None: the ValuationEngine's own "this source is gone" value)."""
    ktc_snap = make_snapshot("ktc_dynasty", ktc_rows if ktc_rows is not None else build_ktc_payload(players), age_hours=age_hours) if ktc_on else None
    fp = {
        key: make_snapshot(f"fantasypros_{key}", fp_rows if fp_rows is not None else build_fp_payload(players, offset=i), age_hours=age_hours)
        for i, key in enumerate(FANTASYPROS_PAGES)
    } if fp_on else None
    rb = {
        key: make_snapshot(f"rotoballer_{key}", rb_rows if rb_rows is not None else build_rb_payload(players, ppr=key != "standard"), age_hours=age_hours)
        for key in ROTOBALLER_SPREADSHEETS
    } if rb_on else None
    return ValuationEngine(ktc_snapshot=ktc_snap, fp_snapshots=fp, rb_snapshots=rb, ff_rows=[], current_week=current_week)


def _padded_ktc_rows(players, floor: int) -> list[dict]:
    """The synthetic pool is 96 players, below every coverage floor, so the
    ranking sources grade Partial by default. Filler rows appended AFTER
    the real ones (ranks continue, names nobody rosters) lift the row count
    over the floor without touching a single real player's rank."""
    rows = build_ktc_payload(players)
    block = {"value": 300, "rank": 0, "positional_rank": 0}
    for i in range(floor - len(rows) + 5):
        rank = len(rows) + 1
        rows.append({
            "name": f"Pad Filler{i}", "position": "WR", "team": "FA", "age": 24.0, "is_rookie": False,
            **{key: {**block, "rank": rank, "positional_rank": rank} for key in
               ("one_qb", "superflex", "one_qb_tep", "one_qb_tepp", "one_qb_teppp", "superflex_tep", "superflex_tepp", "superflex_teppp")},
        })
    return rows


def _write_cached(source: str, payload, age: dt.timedelta) -> None:
    """A rankings-cache file whose fetched_at is `age` in the past."""
    snap = cache_mod.save_snapshot(source, payload)
    snap.fetched_at = NOW - age
    cache_mod._cache_path(source).write_text(json.dumps(snap.to_json()), encoding="utf-8")


def schedule_csv(season: int = SEASON, weeks=range(1, 18), teams=NFL_TEAMS) -> str:
    """nflverse games.csv for the synthetic league's twelve NFL teams: six
    games a week, two teams on bye in weeks 5-10."""
    lines = ["game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,home_team,home_score"]
    for w in weeks:
        order = list(teams[w % len(teams):]) + list(teams[: w % len(teams)])
        pairs = list(zip(order[0::2], order[1::2]))
        if 5 <= w <= 10:
            pairs = pairs[:-1]
        for away, home in pairs:
            lines.append(f"{season}_{w:02d}_{away}_{home},{season},REG,{w},{season}-09-{w:02d},Sunday,13:00,{away},,{home},")
    return "\n".join(lines) + "\n"


def usage_csvs(pool, *, season: int = SEASON, weeks=range(1, 6), positions=("QB", "RB", "WR", "TE"),
               rising=(), duplicate: bool = False, limit: int | None = None) -> tuple[str, str]:
    """(stats_player_week, stats_team_week) CSV text for the synthetic
    pool. `rising` players go from 3 targets a game to 9 from week 3 —
    against a flat 30-target team week that is a Role Surging label."""
    head = ("player_id,player_display_name,position,season,week,season_type,team,targets,receptions,receiving_yards,"
            "receiving_air_yards,carries,rushing_yards,attempts,target_share,air_yards_share")
    ids = [pid for pid in _sorted_skill_ids(pool) if pool[pid]["position"] in positions]
    if limit is not None:
        ids = ids[:limit]
    rows = []
    for pid in ids:
        p = pool[pid]
        for w in weeks:
            targets = 9 if (pid in rising and w >= 3) else (3 if pid in rising else 5)
            attempts = 30 if p["position"] == "QB" else 0
            line = (f"{p['gsis_id']},{p['full_name']},{p['position']},{season},{w},REG,{p['team']},{targets},{targets - 1},"
                    f"{targets * 9},{targets * 11},2,8,{attempts},,")
            rows.append(line)
            if duplicate:
                rows.append(line)
    player = head + "\n" + "\n".join(rows) + "\n"
    team = "season,week,team,season_type,targets,carries,attempts\n" + "\n".join(
        f"{season},{w},{t},REG,30,20,30" for t in NFL_TEAMS for w in weeks
    ) + "\n"
    return player, team


def fake_nflverse(player_csv: str, team_csv: str, db_csv: str | None = None):
    """nfl_usage._http_get_bytes over in-memory CSV. Snap counts and the
    nflverse players file 404 (AssetAbsent) — the synthetic players carry
    Sleeper's own gsis id, the crosswalk's strongest rung, unless a test
    supplies DynastyProcess rows."""
    def fetch(url: str) -> bytes:
        if "stats_player_week" in url:
            return gzip.compress(player_csv.encode("utf-8"))
        if "stats_team_week" in url:
            return gzip.compress(team_csv.encode("utf-8"))
        if "db_playerids" in url and db_csv is not None:
            return db_csv.encode("utf-8")
        raise AssetAbsent(url)
    return fetch


class _Built:
    """One scenario's outputs: the report, both renders, and the league."""

    def __init__(self, report, synth):
        self.report = report
        self.synth = synth
        self.ld = report.leagues[0]
        self.md, self.html = _honest_renders(report)
        self.html_text = html_lib.unescape(self.html)
        self.health = report.health
        self.freshness_lines = report.freshness_lines

    def labels(self, family: str) -> set[str]:
        return {s.label for s in self.health.by_family(family)}


class _Lab:
    """Builds each scenario once. Every build gets its own temp rankings
    cache, an empty fetch-outcome registry, and the same off-`data/`
    stubs test_end_to_end uses — then the patches are undone, so a built
    report never depends on the next scenario's patches."""

    def __init__(self, tmp_factory):
        self._tmp = tmp_factory
        self._memo: dict[str, _Built] = {}

    def build(self, key: str, builder) -> _Built:
        if key not in self._memo:
            mp = pytest.MonkeyPatch()
            try:
                # Windows rejects ':' and ',' in a directory name; scenario keys use both.
                cache_dir = self._tmp.mktemp("".join(c if c.isalnum() else "_" for c in key))
                self._isolate(mp, cache_dir)
                self._memo[key] = _Built(*builder(mp, cache_dir))
            finally:
                mp.undo()
        return self._memo[key]

    @staticmethod
    def _isolate(mp, cache_dir: Path) -> None:
        from sleeper_tool.decision_ledger import Ledger
        from sleeper_tool.watchlist import Watchlist

        mp.setattr(rd, "load_watchlist", lambda *a, **k: Watchlist())
        mp.setattr(rd, "load_ledger", lambda *a, **k: Ledger())
        mp.setattr(rd, "load_snapshots", lambda *a, **k: [])
        mp.setattr(rd, "load_latest_snapshot", lambda *a, **k: None)
        mp.setattr(rd, "ff_dynasty_status", lambda *a, **k: "absent (test)")
        mp.setattr(sh, "ff_dynasty_status", lambda *a, **k: "not provided (optional)")
        # rd.load_snapshot is left REAL: it reads whatever this temp cache holds.
        mp.setattr(cache_mod, "CACHE_DIR", cache_dir)
        mp.setattr(cache_mod, "last_fetch_outcome", {})


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    return _Lab(tmp_path_factory)


def _run(synth, engine, *, current_week: int | None = 3, with_nfl_schedule: bool = False, with_usage: bool | None = None, storage=None):
    storage = storage if storage is not None else make_storage(synth, current_week=current_week)
    return build_weekly_report_data(storage, engine, [synth.info], with_nfl_schedule=with_nfl_schedule, with_usage=with_usage)


def _baseline(lab) -> _Built:
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        return _run(synth, _engine(synth.players)), synth
    return lab.build("baseline", build)


def _absent(lab, *families: str) -> _Built:
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        engine = _engine(synth.players, ktc_on="ktc" not in families, fp_on="fantasypros" not in families, rb_on="rotoballer" not in families)
        assert sorted(engine.missing_sources) == sorted(
            [s for fam, srcs in (("ktc", ["ktc_dynasty"]), ("fantasypros", []), ("rotoballer", [])) if fam in families for s in srcs]
        )
        return _run(synth, engine), synth
    return lab.build("absent:" + ",".join(families), build)


# ---------------------------------------------------------------------------
# the healthy baseline these scenarios are measured against
# ---------------------------------------------------------------------------


def test_the_baseline_is_a_complete_run_with_only_the_offline_suppressions(lab):
    base = _baseline(lab)
    assert base.ld.error is None
    assert set(base.report.suppressed) == OFFLINE_SUPPRESSED
    assert is_complete_run(base.report) is True
    assert base.ld.proposals and base.ld.waiver_targets and base.ld.lineup is not None and base.ld.replacement is not None
    assert base.ld.streamers and base.ld.matchup is not None and base.ld.team_status is not None
    # The synthetic pool is 96 players: every ranking family is honestly
    # Partial, and Partial alone never blocks the snapshot.
    for family in ("ktc", "fantasypros", "rotoballer"):
        assert base.labels(family) == {PARTIAL}
        _note_in_both(base, "loaded only")


# ---------------------------------------------------------------------------
# a whole ranking family absent
# ---------------------------------------------------------------------------


# The display name is the SOURCE's, not the family's: "ktc_dynasty" renders
# "KTC dynasty" (one source in the family), FantasyPros and RotoBaller have
# several and each is listed.
@pytest.mark.parametrize("family, display, note", [
    ("fantasypros", "FantasyPros", "FantasyPros unavailable (engine has no fantasypros snapshots)"),
    ("ktc", "KTC dynasty", "KTC unavailable (engine has no KTC snapshot)"),
    ("rotoballer", "RotoBaller", "RotoBaller unavailable (engine has no rotoballer snapshots)"),
])
def test_an_absent_ranking_family_is_named_and_the_run_is_not_complete(lab, family, display, note):
    built = _absent(lab, family)
    assert built.ld.error is None
    assert built.labels(family) == {UNAVAILABLE}
    assert family in built.health.unavailable_families
    assert _note_in_both(built, note) == note
    assert f"{display} · Unavailable · no data" in built.md
    assert "Signal health: degraded" in built.md and "Signal health: degraded" in built.html_text
    assert is_complete_run(built.report) is False
    # The other two families are untouched — still there, still Partial.
    for other in {"ktc", "fantasypros", "rotoballer"} - {family}:
        assert built.labels(other) == {PARTIAL}


def test_fantasypros_absent_keeps_trades_waivers_status_alerts_and_every_lineup_feature(lab):
    """RotoBaller is the only projection source; FantasyPros contributes
    ECR ranks and bye weeks. Losing it costs the source-disagreement view
    and the redraft currency's ECR percentiles — nothing lineup-shaped."""
    built = _absent(lab, "fantasypros")
    ld = built.ld
    assert {"source_disagreement", "redraft_currency"} <= set(built.report.suppressed)
    assert ld.proposals and ld.waiver_targets and ld.time_sensitive
    assert ld.team_status is not None and ld.team_status.status in ("contender", "middling", "rebuild")
    assert ld.lineup is not None and ld.lineup.total_projected_points > 0
    assert ld.replacement is not None and ld.replacement.positions
    assert ld.matchup is not None and ld.lineup_leverage is not None and ld.streamers
    assert ld.source_views == {}  # the suppressed feature really is skipped
    assert all(e.value.dynasty_ecr_rank is None and e.value.redraft_ecr_rank is None for e in ld.roster.entries)
    assert all(e.value.dynasty_value is not None for e in ld.roster.entries if e.position in ("QB", "RB", "WR", "TE"))
    assert "Best starting lineup" in built.md and "Best starting lineup" in built.html_text


def test_ktc_absent_offers_no_dynasty_trades_but_keeps_waivers_lineup_and_status(lab):
    built = _absent(lab, "ktc")
    ld = built.ld
    assert {"dynasty_values", "source_disagreement"} <= set(built.report.suppressed)
    # Dynasty trades rest on KTC: none are invented from nothing, and the
    # empty state is spoken rather than left blank.
    assert ld.proposals == []
    assert "No trade offers cleared the value-match bar this week." in built.md
    assert all(e.value.dynasty_value is None and e.value.dynasty_rank is None for e in ld.roster.entries)
    assert ld.waiver_targets and ld.time_sensitive and ld.team_status is not None
    assert ld.lineup is not None and ld.lineup.total_projected_points > 0
    assert ld.replacement is not None and ld.streamers and ld.matchup is not None


def test_ktc_absent_in_a_redraft_league_still_builds_on_projections(lab):
    def build(mp, cache_dir):
        synth = make_synthetic_league(kind="redraft", league_id="9000000000000000014")
        return _run(synth, _engine(synth.players, ktc_on=False)), synth
    built = lab.build("redraft:ktc-absent", build)
    ld = built.ld
    assert ld.error is None and ld.currency == "redraft"
    assert ld.waiver_targets and ld.lineup is not None and ld.lineup.total_projected_points > 0
    assert ld.team_status is not None
    assert is_complete_run(built.report) is False


def test_rotoballer_absent_keeps_waivers_and_status_and_drops_everything_projection_shaped(lab):
    built = _absent(lab, "rotoballer")
    ld = built.ld
    assert {"lineup_optimizer", "matchup_leverage", "replacement_value", "streamer_planner", "redraft_currency"} <= set(built.report.suppressed)
    assert ld.waiver_targets and ld.team_status is not None and ld.drop_candidates is not None
    assert all(e.value.proj_points is None for e in ld.roster.entries)
    assert ld.insurance == [] and ld.streamers == []
    assert all(t.value is None or t.value.proj_points is None for t in ld.waiver_targets)


def test_every_ranking_family_absent_still_yields_a_report_with_waivers_from_trending(lab):
    built = _absent(lab, "ktc", "fantasypros", "rotoballer")
    ld = built.ld
    assert ld.error is None
    assert built.health.unavailable_families >= {"ktc", "fantasypros", "rotoballer"}
    assert {"dynasty_values", "lineup_optimizer", "matchup_leverage", "replacement_value", "streamer_planner",
            "redraft_currency", "source_disagreement"} <= set(built.report.suppressed)
    assert is_complete_run(built.report) is False
    # Trending adds come from Sleeper, not a ranking source: still there.
    assert ld.waiver_targets
    assert all(t.value is None or (t.value.dynasty_value is None and t.value.proj_points is None) for t in ld.waiver_targets)
    assert ld.proposals == [] and ld.insurance == [] and ld.streamers == []
    assert built.report.snapshot is not None and ld.league.league_id in built.report.snapshot["leagues"]
    for name in ("KTC dynasty", "FantasyPros", "RotoBaller"):
        assert f"{name} · Unavailable" in built.md


# ---------------------------------------------------------------------------
# the parser returned nothing / too little
# ---------------------------------------------------------------------------


def test_an_empty_ktc_payload_is_unavailable_not_a_500_row_source(lab):
    """A scraper that ran but found no players (a layout change the parser
    survived) is a dead source: Unavailable, suppressed, snapshot withheld."""
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        return _run(synth, _engine(synth.players, ktc_rows=[])), synth
    built = lab.build("ktc:empty", build)
    assert built.labels("ktc") == {UNAVAILABLE}
    assert _note_in_both(built, "KTC unavailable (payload empty or unreadable)")
    assert "dynasty_values" in built.report.suppressed
    assert built.ld.proposals == []
    assert is_complete_run(built.report) is False


def test_one_empty_fantasypros_page_leaves_the_family_available(lab):
    """Seven FantasyPros lists; losing the superflex dynasty page must not
    read as 'FantasyPros is down' — but the run still isn't a baseline."""
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        fp = {
            key: make_snapshot(f"fantasypros_{key}", [] if key == "dynasty_superflex" else build_fp_payload(synth.players, offset=i))
            for i, key in enumerate(FANTASYPROS_PAGES)
        }
        engine = ValuationEngine(
            ktc_snapshot=make_snapshot("ktc_dynasty", build_ktc_payload(synth.players)), fp_snapshots=fp,
            rb_snapshots={k: make_snapshot(f"rotoballer_{k}", build_rb_payload(synth.players)) for k in ROTOBALLER_SPREADSHEETS},
            ff_rows=[], current_week=3,
        )
        return _run(synth, engine), synth
    built = lab.build("fp:one-page-empty", build)
    by_source = {s.source: s.label for s in built.health.by_family("fantasypros")}
    assert by_source["fantasypros_dynasty_superflex"] == UNAVAILABLE
    assert all(label == PARTIAL for src, label in by_source.items() if src != "fantasypros_dynasty_superflex")
    assert "fantasypros" not in built.health.unavailable_families
    assert set(built.report.suppressed) == OFFLINE_SUPPRESSED
    assert "FantasyPros dynasty superflex · Unavailable" in built.md
    # The league is superflex, so its dynasty ECR really is gone...
    assert all(e.value.dynasty_ecr_rank is None for e in built.ld.roster.entries)
    # ...while the redraft ECR from a healthy page is still there.
    assert any(e.value.redraft_ecr_rank is not None for e in built.ld.roster.entries)
    assert built.ld.proposals and built.ld.lineup is not None
    assert is_complete_run(built.report) is False


def test_a_short_list_is_partial_and_the_shortfall_is_stated(lab):
    base = _baseline(lab)
    line = _health_line(base, "ktc_dynasty")
    assert f"KTC dynasty · Partial · 2.0h · 96 rows" == line
    assert _note_in_both(base, "KTC dynasty loaded only 96 rows")
    assert MIN_COVERAGE["ktc"] == 400  # the floor the 96 is measured against


# ---------------------------------------------------------------------------
# stale ranking snapshots
# ---------------------------------------------------------------------------


def test_stale_but_within_the_ceiling_still_builds_and_is_still_a_complete_run(lab):
    """Five days: past the 3-day usable window, inside the 7-day ceiling.
    Stale is flagged on every line and in the banner; the numbers are
    still real numbers, so the snapshot may be written."""
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        return _run(synth, _engine(synth.players, age_hours=24 * 5)), synth
    built = lab.build("rankings:5d", build)
    assert SOURCE_WINDOWS["ktc"][1] == dt.timedelta(days=3) and SOURCE_WINDOWS["ktc"][2] == dt.timedelta(days=7)
    for family in ("ktc", "fantasypros", "rotoballer"):
        assert built.labels(family) == {STALE}
    assert _note_in_both(built, "KTC dynasty is 5.0d old")
    assert "KTC dynasty · Stale · 5.0d · 96 rows" in built.md
    assert set(built.report.suppressed) == OFFLINE_SUPPRESSED
    assert built.ld.proposals and built.ld.lineup is not None
    assert is_complete_run(built.report) is True
    # Provenance carries the label onto the reasons that rest on it.
    stale_text = {v for v in sh.freshness_by_source(built.health).values()}
    assert any("Stale" in t for t in stale_text)


def test_a_snapshot_older_than_the_ceiling_is_unavailable_even_when_supplied(lab):
    """The cache layer refuses to serve past the ceiling; if a snapshot
    that old reaches the engine anyway, the health layer still grades it
    Unavailable and the run is not a baseline."""
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        return _run(synth, _engine(synth.players, age_hours=24 * 8)), synth
    built = lab.build("rankings:8d", build)
    for family in ("ktc", "fantasypros", "rotoballer"):
        assert built.labels(family) == {UNAVAILABLE}
    assert built.health.unavailable_families >= {"ktc", "fantasypros", "rotoballer"}
    assert is_complete_run(built.report) is False
    assert "KTC dynasty · Unavailable · 8.0d · 96 rows" in built.md


# ---------------------------------------------------------------------------
# rankings.cache: the re-fetch fails
# ---------------------------------------------------------------------------


def _ktc_from_cache(lab, key: str, age: dt.timedelta) -> _Built:
    """A KTC cache file `age` old, a KTC fetch that raises, and an engine
    left to resolve KTC itself — the exact path a dead site takes."""
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        _write_cached("ktc_dynasty", _padded_ktc_rows(synth.players, MIN_COVERAGE["ktc"]), age)

        def down():
            raise RuntimeError("KTC down")
        mp.setattr(ktc, "fetch_ktc_html", down)
        fp = {k: make_snapshot(f"fantasypros_{k}", build_fp_payload(synth.players, offset=i)) for i, k in enumerate(FANTASYPROS_PAGES)}
        rb = {k: make_snapshot(f"rotoballer_{k}", build_rb_payload(synth.players)) for k in ROTOBALLER_SPREADSHEETS}
        engine = ValuationEngine(fp_snapshots=fp, rb_snapshots=rb, ff_rows=[], current_week=3)
        report = _run(synth, engine)
        report.fetch_outcome = dict(cache_mod.last_fetch_outcome)  # read before the patch is undone
        report.engine_missing = list(engine.missing_sources)
        return report, synth
    return lab.build(key, build)


def test_a_cache_past_the_ceiling_whose_refetch_raises_makes_ktc_unavailable(lab):
    built = _ktc_from_cache(lab, "cache:ktc-past-ceiling", dt.timedelta(days=8))
    assert built.report.fetch_outcome == {"ktc_dynasty": "failed"}
    assert built.report.engine_missing == ["ktc_dynasty"]
    assert built.ld.error is None
    assert built.labels("ktc") == {UNAVAILABLE}
    assert "dynasty_values" in built.report.suppressed
    assert _note_in_both(built, "KTC unavailable (engine has no KTC snapshot)")
    assert built.ld.proposals == [] and built.ld.waiver_targets and built.ld.lineup is not None
    assert is_complete_run(built.report) is False


def test_a_cache_inside_the_ceiling_whose_refetch_raises_is_served_and_labelled_a_fallback(lab):
    built = _ktc_from_cache(lab, "cache:ktc-5d-fallback", dt.timedelta(days=5))
    assert built.report.fetch_outcome == {"ktc_dynasty": "fallback"}
    assert built.report.engine_missing == []
    signal = built.health.by_family("ktc")[0]
    assert signal.label == STALE and signal.fallback is True
    assert _note_in_both(built, "KTC dynasty served from cache after a failed re-fetch")
    assert "dynasty_values" not in built.report.suppressed
    assert built.ld.proposals  # yesterday's prices still price a trade...
    assert is_complete_run(built.report) is False  # ...but never become tomorrow's baseline


def test_a_young_fallback_is_usable_never_fresh(lab):
    """Thirty hours old with a full list: the age alone would say Usable
    and the row count Fresh — but the site is down right now, and the
    label must not hide that."""
    built = _ktc_from_cache(lab, "cache:ktc-30h-fallback", dt.timedelta(hours=30))
    signal = built.health.by_family("ktc")[0]
    assert signal.coverage >= MIN_COVERAGE["ktc"]
    assert signal.fallback is True and signal.label == USABLE
    assert built.health.degraded is True
    assert "Signal health: degraded" in built.md
    assert is_complete_run(built.report) is False


# ---------------------------------------------------------------------------
# a source missing half a position / degenerate projections
# ---------------------------------------------------------------------------


def test_ktc_with_no_tight_ends_leaves_te_rows_unranked_and_the_market_intact(lab):
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        rows = [r for r in build_ktc_payload(synth.players) if r["position"] != "TE"]
        return _run(synth, _engine(synth.players, ktc_rows=rows)), synth
    built = lab.build("ktc:no-te", build)
    ld = built.ld
    assert ld.error is None
    tes = [e for e in ld.roster.entries if e.position == "TE"]
    assert tes and all(e.value.dynasty_value is None and e.value.dynasty_rank is None for e in tes)
    assert all(e.value.proj_points is not None for e in tes)  # RotoBaller still projects them
    others = [e for e in ld.roster.entries if e.position in ("QB", "RB", "WR")]
    assert all(e.value.dynasty_value is not None for e in others)
    assert ld.replacement is not None and "TE" in ld.replacement.positions
    assert ld.lineup is not None and any(a.slot == "TE" for a in ld.lineup.assignments)
    assert built.labels("ktc") == {PARTIAL}
    for e in tes:
        assert e.name in built.md and e.name in built.html_text


def _zero_projections(lab) -> _Built:
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        rows = build_rb_payload(synth.players)
        for r in rows:
            r["proj_points_ppr"] = r["proj_points_standard"] = r["proj_points_te_premium"] = 0.0
        return _run(synth, _engine(synth.players, rb_rows=rows)), synth
    return lab.build("rb:all-zero", build)


def test_zero_projections_everywhere_never_divide_by_zero(lab):
    built = _zero_projections(lab)
    ld = built.ld
    assert ld.error is None
    assert ld.lineup is not None and ld.lineup.total_projected_points == 0.0
    assert len(ld.lineup.assignments) == len(_baseline(lab).ld.lineup.assignments)  # every slot still filled
    assert ld.insurance == []
    assert all(p.recommendation == "Hold" for p in ld.streamers)  # nothing "adds 3+" over a 0
    assert ld.lineup_leverage is not None and ld.lineup_leverage.close_calls == [] and ld.lineup_leverage.bench_surplus == []
    assert "Projects ~0.0 pts/wk." in built.md
    assert ld.proposals  # dynasty values are untouched by a projection outage
    # RotoBaller is present and current: Partial (96-row pool), never Unavailable.
    assert built.labels("rotoballer") == {PARTIAL}
    assert is_complete_run(built.report) is True


# ---------------------------------------------------------------------------
# the nflverse schedule
# ---------------------------------------------------------------------------


def _schedule_scenario(lab, key: str, *, age: dt.timedelta | None, payload=None, fetch_ok: bool = False) -> _Built:
    def build(mp, cache_dir):
        if age is not None:
            _write_cached("nflverse_schedule", payload or {"season": SEASON, "rows": nfl_schedule.parse_schedule_csv(schedule_csv(), SEASON)}, age)
        if fetch_ok:
            mp.setattr(nfl_schedule, "fetch_schedule_rows", lambda season: {"season": season, "rows": nfl_schedule.parse_schedule_csv(schedule_csv(), season)})
        else:
            def offline(season):
                raise ConnectionError("offline")
            mp.setattr(nfl_schedule, "fetch_schedule_rows", offline)
        synth = make_synthetic_league()
        return _run(synth, _engine(synth.players), with_nfl_schedule=True, with_usage=False), synth
    return lab.build(key, build)


def test_a_fresh_schedule_fetch_feeds_windows_and_schedule_notes(lab):
    built = _schedule_scenario(lab, "schedule:fresh", age=None, fetch_ok=True)
    # The synthetic schedule CSV is far shorter than the coverage floor, so a
    # live fetch reads Partial (short, not old) — the point of the scenario is
    # that a working fetch still feeds windows and per-target schedule notes.
    assert built.labels("nflverse_schedule") == {PARTIAL}
    assert "schedule_windows" not in built.report.suppressed
    assert built.ld.windows is not None and built.ld.windows.next_weeks == [3, 4, 5]
    # One shared schedule line for the whole report (the NFL calendar is the
    # same in every league), the per-league section only where it differs.
    assert "Schedule: " in built.md and "Schedule: " in built.html_text
    assert any(n.startswith("Schedule:") for t in built.ld.waiver_targets for n in t.notes)


def test_a_schedule_past_its_usable_window_is_served_from_cache_and_labelled_stale(lab):
    """Twenty days: past the 14-day usable window, inside the 60-day
    ceiling. The failed re-fetch falls back to the file; the label says
    Stale AND that it was a fallback; every consumer still gets a schedule."""
    assert SOURCE_WINDOWS["nflverse_schedule"] == (dt.timedelta(hours=24), dt.timedelta(days=14), dt.timedelta(days=60))
    built = _schedule_scenario(lab, "schedule:20d", age=dt.timedelta(days=20))
    signal = built.health.by_family("nflverse_schedule")[0]
    assert signal.label == STALE and signal.fallback is True
    assert _note_in_both(built, "NFL schedule served from cache after a failed re-fetch")
    assert "NFL schedule · Stale · 20.0d" in built.md
    assert "schedule_windows" not in built.report.suppressed
    assert built.ld.windows is not None and built.ld.windows.next_weeks == [3, 4, 5]
    assert built.ld.streamers
    # A schedule fallback is not a ranking fallback: the snapshot may still be written.
    assert is_complete_run(built.report) is True


def test_a_schedule_past_its_ceiling_is_refused_and_the_windows_are_suppressed(lab):
    built = _schedule_scenario(lab, "schedule:61d", age=dt.timedelta(days=61))
    signal = built.health.by_family("nflverse_schedule")[0]
    assert signal.label == UNAVAILABLE and signal.fallback is False
    assert built.report.suppressed["schedule_windows"] == "requires NFL schedule, which is unavailable"
    assert "Suppressed this run: schedule windows" in built.md and "Suppressed this run: schedule windows" in built.html_text
    assert built.ld.windows is None
    assert "Schedule windows" not in built.md
    # No schedule at all is the path the streamer planner was built for.
    assert built.ld.streamers and built.ld.lineup is not None and built.ld.proposals


@pytest.mark.parametrize("key, payload", [
    ("schedule:no-rows-key", {"season": SEASON}),
    ("schedule:rows-not-a-list", {"season": SEASON, "rows": "garbage"}),
])
def test_a_schedule_payload_with_no_row_list_is_unavailable(lab, key, payload):
    built = _schedule_scenario(lab, key, age=dt.timedelta(hours=1), payload=payload)
    signal = built.health.by_family("nflverse_schedule")[0]
    assert signal.label == UNAVAILABLE and signal.coverage is None
    assert _note_in_both(built, "NFL schedule unavailable (payload empty or unreadable)")
    assert "schedule_windows" in built.report.suppressed
    assert built.ld.error is None and built.ld.proposals and built.ld.waiver_targets


def test_a_schedule_with_some_malformed_rows_keeps_the_good_ones(lab):
    rows = nfl_schedule.parse_schedule_csv(schedule_csv(), SEASON)
    bad = [{"week": "x"}, {"season": SEASON, "week": "three", "game_type": "REG", "home": "KC", "away": "BUF"}, {"week": 1, "home": "KC"}]
    built = _schedule_scenario(lab, "schedule:half-bad", age=dt.timedelta(hours=1), payload={"season": SEASON, "rows": rows + bad * 30})
    assert built.ld.windows is not None and built.ld.windows.next_weeks == [3, 4, 5]
    assert built.ld.streamers
    assert built.labels("nflverse_schedule") <= {FRESH, PARTIAL}  # a real label, not a crash


# ---------------------------------------------------------------------------
# the nflverse usage feed and the id crosswalk
# ---------------------------------------------------------------------------


def _usage_scenario(lab, key: str, *, current_week: int = 6, positions=("QB", "RB", "WR", "TE"), weeks=range(1, 6),
                    duplicate: bool = False, db_csv: str | None = None, mutate=None, limit: int | None = None) -> _Built:
    def build(mp, cache_dir):
        synth = make_synthetic_league(current_week=current_week)
        if mutate is not None:
            mutate(synth)
        wr_ids = [pid for pid in _sorted_skill_ids(synth.players) if synth.players[pid]["position"] == "WR"]
        player_csv, team_csv = usage_csvs(synth.players, positions=positions, weeks=weeks, rising=set(wr_ids[:6]), duplicate=duplicate, limit=limit)
        mp.setattr(nfl_usage, "_http_get_bytes", fake_nflverse(player_csv, team_csv, db_csv(synth) if db_csv else None))
        mp.setattr(nfl_schedule, "fetch_schedule_rows", lambda season: {"season": season, "rows": nfl_schedule.parse_schedule_csv(schedule_csv(), season)})
        report = _run(synth, _engine(synth.players, current_week=current_week), current_week=current_week, with_nfl_schedule=True)
        report.usage_loaded = nfl_usage.load_usage(SEASON)  # a cache hit: no second fetch
        return report, synth
    return lab.build(key, build)


def _by_position(built) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pid, trend in built.ld.role_trends.items():
        out.setdefault(built.synth.players[pid]["position"], set()).add(trend.label)
    return out


def test_a_full_usage_file_is_fresh_and_labels_the_surging_receivers(lab):
    built = _usage_scenario(lab, "usage:full")
    signal = built.health.by_family("nflverse_usage")[0]
    assert signal.label == FRESH and signal.latest_week == 5 and signal.coverage == 480
    assert "role_trends" not in built.report.suppressed and set(built.report.suppressed) == set()
    assert built.report.usage_note is None
    assert built.report.crosswalk_note.startswith("Player id crosswalk: 120/120 matched")
    assert built.report.crosswalk_note in built.md and built.report.crosswalk_note in built.html_text
    assert "Role Surging" in _by_position(built)["WR"]
    assert "Role: Role Surging" in built.md
    assert is_complete_run(built.report) is True


def test_a_usage_file_covering_one_position_is_partial_and_trends_only_those_players(lab):
    """24 receivers x 5 weeks = 120 player-weeks, under the 200 floor."""
    built = _usage_scenario(lab, "usage:wr-only", positions=("WR",))
    signal = built.health.by_family("nflverse_usage")[0]
    assert signal.label == PARTIAL and signal.coverage == 120
    assert _note_in_both(built, "NFL usage loaded only 120 rows")
    assert "NFL usage · Partial" in built.md
    assert "role_trends" not in built.report.suppressed  # Partial still computes; Unavailable suppresses
    labels = _by_position(built)
    assert "Role Surging" in labels["WR"]
    for position in ("QB", "RB", "TE"):
        assert labels[position] == {INSUFFICIENT}, position
    assert built.ld.role_trends  # the players that matter still got a line each
    assert is_complete_run(built.report) is True


@pytest.mark.parametrize("through, expected, detail", [
    (5, FRESH, "through week 5"),           # this week's games not yet played: current
    (4, STALE, "2 weeks behind the league's current week"),
    (3, STALE, "3 weeks behind the league's current week"),
])
def test_a_usage_file_behind_the_league_week_is_stale_however_fresh_the_download(lab, through, expected, detail):
    """USAGE_MAX_WEEKS_BEHIND is 1: through week N-1 at week N is the
    normal state; through week N-2 means a week of games went missing."""
    assert sh.USAGE_MAX_WEEKS_BEHIND == 1
    built = _usage_scenario(lab, f"usage:through-{through}", weeks=range(1, through + 1))
    signal = built.health.by_family("nflverse_usage")[0]
    assert signal.label == expected and signal.latest_week == through
    assert detail in signal.detail
    if expected == STALE:
        assert _note_in_both(built, "NFL usage is 0.0h old")  # the age is honest: it was just fetched
        assert "NFL usage · Stale" in built.md
        assert built.health.degraded is True
    assert built.ld.role_trends  # stale still computes; the label rides on provenance


def test_duplicate_player_weeks_count_once(lab):
    """nflverse has shipped a release with every row twice. One
    (gsis, week) is one game — the trends must match a clean file's."""
    clean = _usage_scenario(lab, "usage:full")
    doubled = _usage_scenario(lab, "usage:duplicated", duplicate=True)
    assert len(doubled.report.usage_loaded.player_weeks) == len(clean.report.usage_loaded.player_weeks) == 480
    assert {pid: t.label for pid, t in doubled.ld.role_trends.items()} == {pid: t.label for pid, t in clean.ld.role_trends.items()}
    assert doubled.health.by_family("nflverse_usage")[0].label == FRESH


def _crosswalk_mutation(synth) -> None:
    """Four of my players lose their Sleeper gsis id: one is recoverable
    through DynastyProcess, one is in DynastyProcess with no sleeper_id
    (unreachable), and two share a gsis id (a collision)."""
    mine = synth.my_roster["players"]
    orphan, dp_only, twin_a, twin_b = mine[0], mine[1], mine[2], mine[3]
    synth.crosswalk_case = {"orphan": orphan, "dp_only": dp_only, "twins": (twin_a, twin_b),
                            "orphan_gsis": synth.players[orphan]["gsis_id"], "dp_only_gsis": synth.players[dp_only]["gsis_id"]}
    synth.players[orphan]["gsis_id"] = None
    synth.players[dp_only]["gsis_id"] = None
    synth.players[twin_b]["gsis_id"] = synth.players[twin_a]["gsis_id"]


def _crosswalk_db(synth) -> str:
    c = synth.crosswalk_case
    return (
        "mfl_id,gsis_id,sleeper_id,pfr_id,name,merge_name,position,team\n"
        f"1,{c['orphan_gsis']},,Orph00,{synth.players[c['orphan']]['full_name']},x,WR,KC\n"  # no sleeper_id: dropped
        f"2,{c['dp_only_gsis']},{c['dp_only']},Dpon00,{synth.players[c['dp_only']]['full_name']},y,WR,KC\n"
    )


def test_crosswalk_gaps_and_collisions_are_demoted_and_counted_not_guessed(lab):
    built = _usage_scenario(lab, "usage:crosswalk", db_csv=_crosswalk_db, mutate=_crosswalk_mutation)
    c = built.synth.crosswalk_case
    trends = built.ld.role_trends
    assert c["dp_only"] in trends  # the second rung of the ladder held
    assert c["orphan"] not in trends  # a DynastyProcess row with no sleeper_id reaches nobody
    assert c["twins"][0] not in trends and c["twins"][1] not in trends  # both claims demoted
    note = built.report.crosswalk_note
    assert "117/120 matched" in note and "3 unmatched" in note and "1 gsis collision(s)" in note
    assert "dynastyprocess 1" in note
    assert note in built.md and note in built.html_text
    assert built.ld.error is None and built.labels("nflverse_usage") == {FRESH}
    # The demoted players are still valued, started and tradeable — only the role line is withheld.
    ids = {c["orphan"], *c["twins"]}
    assert ids <= {e.player_id for e in built.ld.roster.entries}


# ---------------------------------------------------------------------------
# the snapshot history
# ---------------------------------------------------------------------------


def _snapshot_scenario(lab, key: str, files: dict[str, object], caplog=None) -> _Built:
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        snapshot_dir = cache_dir / "run_snapshots"
        snapshot_dir.mkdir()
        # A real complete run's snapshot, dated two days back, as the valid baseline.
        first = _run(synth, _engine(synth.players))
        valid = dict(first.snapshot)
        valid["generated_at"] = (NOW - dt.timedelta(days=2)).isoformat()
        for name, content in {"20260901.json": valid, **files}.items():
            text = content if isinstance(content, str) else json.dumps(content if content is not ... else valid)
            (snapshot_dir / name).write_text(text, encoding="utf-8")
        mp.setattr(rd, "load_latest_snapshot", lambda **k: load_latest_snapshot(snapshot_dir, **k))
        mp.setattr(rd, "load_snapshots", lambda **k: load_snapshots(snapshot_dir, **k))
        report = _run(synth, _engine(synth.players))
        report.baseline_since = dt.datetime.fromisoformat(valid["generated_at"])
        return report, synth
    return lab.build(key, build)


def test_a_corrupted_snapshot_beside_a_valid_one_still_yields_a_delta_from_the_valid_one(lab, caplog):
    with caplog.at_level(logging.WARNING, logger="sleeper_tool.decision_delta"):
        built = _snapshot_scenario(lab, "snapshots:corrupt", {"20260902.json": "{not json"})
    assert built.report.delta is not None
    assert built.report.delta.since == built.report.baseline_since
    assert built.report.delta.items == []  # same inputs, same decisions
    assert any("Ignoring unreadable snapshot" in r.message and "20260902.json" in r.message for r in caplog.records)
    assert "No meaningful changes" in built.html_text


def test_a_schema_one_snapshot_is_ignored_with_a_warning(lab, caplog):
    with caplog.at_level(logging.WARNING, logger="sleeper_tool.decision_delta"):
        built = _snapshot_scenario(lab, "snapshots:schema1", {
            "20260902.json": {"schema": 1, "generated_at": (NOW - dt.timedelta(days=1)).isoformat(), "leagues": {"9000000000000000001": {"name": "Old", "roster": {"x": {"name": "Ghost", "value": 1}}}}},
        })
    # The newest file is schema 1: skipped, and the schema-2 file before it is the baseline.
    assert built.report.delta is not None and built.report.delta.since == built.report.baseline_since
    assert not any("Ghost" in i.text for i in built.report.delta.items)
    assert any("schema 1, expected 2" in r.message for r in caplog.records)


def test_only_a_schema_one_snapshot_means_no_delta_at_all(lab):
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        snapshot_dir = cache_dir / "run_snapshots"
        snapshot_dir.mkdir()
        (snapshot_dir / "20260902.json").write_text(json.dumps({"schema": 1, "generated_at": (NOW - dt.timedelta(days=1)).isoformat(), "leagues": {}}), encoding="utf-8")
        mp.setattr(rd, "load_latest_snapshot", lambda **k: load_latest_snapshot(snapshot_dir, **k))
        mp.setattr(rd, "load_snapshots", lambda **k: load_snapshots(snapshot_dir, **k))
        return _run(synth, _engine(synth.players)), synth
    built = lab.build("snapshots:only-schema1", build)
    assert built.report.delta is None
    # Velocity still names every tracked player — with one observation and no
    # usable history, every one reads Insufficient History rather than vanishing.
    assert built.ld.velocity and {v.label for v in built.ld.velocity.values()} == {"Insufficient History"}
    assert {v.observations for v in built.ld.velocity.values()} == {1}


# ---------------------------------------------------------------------------
# Sleeper's own tables
# ---------------------------------------------------------------------------


def test_a_league_with_no_waiver_type_or_budget_gets_a_non_faab_note_and_no_bids(lab):
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        del synth.league["settings"]["waiver_type"]
        del synth.league["settings"]["waiver_budget"]
        return _run(synth, _engine(synth.players)), synth
    built = lab.build("league:no-faab-settings", build)
    ld = built.ld
    assert ld.error is None and ld.waiver_targets
    assert ld.faab == {} and ld.faab_context is not None and ld.faab_context.is_faab is False
    assert ld.faab_note == "League is not FAAB — waiver claims here run on priority order, so there is no bid to size."
    assert ld.faab_note in built.md and ld.faab_note in built.html_text
    assert all(t.suggested_faab_pct is None for t in ld.waiver_targets)
    assert "$" not in built.md.split("## Waiver targets")[1].split("|---|")[1].split("</details>")[0]
    assert "FAAB:" not in built.md


def test_only_my_roster_missing_its_settings_block(lab):
    """The others have records; mine has no `settings` at all. Everything
    keyed off it defaults; nobody else's standing is disturbed."""
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        del synth.my_roster["settings"]
        return _run(synth, _engine(synth.players)), synth
    built = lab.build("roster:mine-no-settings", build)
    ld = built.ld
    assert ld.error is None
    assert ld.roster.wins == 0 and ld.roster.losses == 0 and ld.roster.points_for == 0.0 and ld.roster.waiver_budget_used == 0
    assert ld.faab_context.my_used == 0 and ld.faab_context.others_used and all(u > 0 for u in ld.faab_context.others_used)
    assert ld.faab  # FAAB advice still sizes: the budget is the league's, the spend defaults to 0
    assert ld.proposals and ld.waiver_targets and ld.lineup is not None
    assert built.labels("sleeper_league") == {FRESH}
    assert is_complete_run(built.report) is True


class _AgedTrendingStorage(FakeStorage):
    """FakeStorage stamps every table with one fetched_at; sync keeps
    yesterday's trending list when Sleeper returns an empty one, so that
    table needs its own age."""

    trending_fetched_at: dt.datetime | None = None

    def save_trending(self, trend_type: str, rows: list[dict]) -> None:
        self._trending[trend_type] = list(rows)

    def table_last_fetched(self, table: str) -> dt.datetime | None:
        if table == "trending":
            return self.trending_fetched_at if self.row_count("trending") else None
        return super().table_last_fetched(table)


def _trending_scenario(lab, key: str, age: dt.timedelta) -> _Built:
    def build(mp, cache_dir):
        synth = make_synthetic_league()
        storage = _AgedTrendingStorage()
        storage.add_league(synth)
        storage.set_meta("current_week", "3")
        storage.trending_fetched_at = NOW - age
        yesterday = list(storage.get_trending("add"))
        # Sleeper returned nothing this morning: the stored list survives.
        assert save_trending_if_nonempty(storage, "add", []) is False
        assert storage.get_trending("add") == yesterday and yesterday
        report = _run(synth, _engine(synth.players), storage=storage)
        return report, synth
    return lab.build(key, build)


def test_an_empty_trending_response_keeps_yesterdays_list_and_grades_it_by_its_own_age(lab):
    built = _trending_scenario(lab, "trending:9d", dt.timedelta(days=9))
    trending = built.health.by_family("sleeper_trending")[0]
    weekly = built.health.by_family("sleeper_weekly")[0]
    assert trending.label == STALE and weekly.label == FRESH  # a MAX across the weekly tables would have masked it
    assert _note_in_both(built, "is 9.0d old")
    assert "waiver_trending" not in built.report.suppressed
    assert built.ld.waiver_targets  # built from the kept list
    assert is_complete_run(built.report) is True  # not a ranking source


def test_a_trending_list_past_its_ceiling_is_unavailable_and_waiver_trending_is_suppressed(lab):
    built = _trending_scenario(lab, "trending:22d", dt.timedelta(days=22))
    assert built.health.by_family("sleeper_trending")[0].label == UNAVAILABLE
    assert "sleeper_trending" in built.health.unavailable_families
    assert built.report.suppressed["waiver_trending"] == "requires sleeper trending, which is unavailable"
    assert built.health.by_family("sleeper_weekly")[0].label == FRESH


# ---------------------------------------------------------------------------
# bugs found while writing this suite (failing on purpose)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=(
    "signal_health.FEATURE_REQUIREMENTS makes lineup_optimizer, matchup_leverage, replacement_value and "
    "streamer_planner depend on FantasyPros, but RotoBaller is the only projection source: with FantasyPros "
    "absent the lineup builds with real totals and renders right under a health block that says it was suppressed"
))
def test_fantasypros_absent_does_not_name_lineup_features_as_suppressed(lab):
    """Repro: `_engine(players, fp_on=False)` through build_weekly_report_data;
    `report.suppressed` names lineup_optimizer / matchup_leverage /
    replacement_value / streamer_planner while `ld.lineup.total_projected_points > 0`
    and 'Best starting lineup' is in both renders."""
    built = _absent(lab, "fantasypros")
    assert built.ld.lineup is not None and built.ld.lineup.total_projected_points > 0
    assert "Best starting lineup" in built.md
    assert not {"lineup_optimizer", "matchup_leverage", "replacement_value", "streamer_planner"} & set(built.report.suppressed)


@pytest.mark.xfail(strict=True, reason=(
    "report_data honours `suppressed` only for source_disagreement and role_trends: with RotoBaller Unavailable "
    "the health block says 'Suppressed this run: lineup optimizer / replacement value' and the report still renders "
    "a 'Projects ~0.0 pts/wk' lineup and a replacement market in which every position is Very Scarce"
))
def test_a_feature_the_health_block_names_as_suppressed_is_not_rendered(lab):
    """Repro: `_engine(players, rb_on=False)`; 'Suppressed this run: lineup optimizer'
    and 'Best starting lineup' / 'Projects ~0.0 pts/wk.' appear in the same Markdown."""
    built = _absent(lab, "rotoballer")
    assert "Suppressed this run: lineup optimizer" in built.md
    assert "Suppressed this run: replacement value" in built.md
    assert "Best starting lineup" not in built.md and "Projects ~0.0 pts/wk." not in built.md
    assert "Replacement market" not in built.md
    assert built.ld.lineup is None and built.ld.replacement is None


@pytest.mark.xfail(strict=True, reason=(
    "replacement_value.scarcity_label(None) returns Very Scarce: when no starter projects above zero the gap is "
    "unknown, and 'unknown' becomes a Very Scarce verdict that then drives waiver notes ('an add at this position "
    "matters more than his rank alone suggests') and FAAB postures — a claim manufactured from no projection data"
))
def test_zero_projections_do_not_yield_a_scarcity_verdict(lab):
    """Repro: every RotoBaller proj_points 0.0; `ld.replacement.positions[pos].gap is None`
    for every position yet `.scarcity == 'Very Scarce'`, and waiver notes say 'market is Very Scarce here'."""
    built = _zero_projections(lab)
    markets = built.ld.replacement.positions.values()
    assert all(m.gap is None for m in markets)
    assert not any(m.scarcity == "Very Scarce" for m in markets)
    assert not any("Very Scarce" in n for t in built.ld.waiver_targets for n in t.notes)
    assert "Very Scarce" not in built.md


@pytest.mark.xfail(strict=True, reason=(
    "team_status with every ranking source absent still classifies me a CONTENDER at the '100th percentile roster "
    "strength in-league' — every roster values to zero and mine ties for first; a percentile of nothing is not a fact"
))
def test_no_valuation_at_all_does_not_claim_a_100th_percentile_contender(lab):
    """Repro: `_engine(players, ktc_on=False, fp_on=False, rb_on=False)`; `ld.team_status.reason`
    reads '100th percentile roster strength in-league ...' and the status chip says CONTENDER."""
    built = _absent(lab, "ktc", "fantasypros", "rotoballer")
    assert "percentile" not in built.ld.team_status.reason
    assert "100th percentile" not in built.md


@pytest.mark.xfail(strict=True, reason=(
    "signal_health grades the schedule on the raw row count of the cache payload, not on what parsed: 250 rows "
    "with non-integer weeks (or missing columns) grade Fresh while schedule_from_rows keeps zero games"
))
def test_a_schedule_whose_rows_do_not_parse_is_not_graded_fresh(lab):
    """Repro: cached nflverse_schedule payload of 250 rows like
    {'season': 2026, 'week': 'three', 'game_type': 'REG', 'home': 'KC', 'away': 'BUF'} one hour old;
    the health line reads 'NFL schedule · Fresh · 1.0h · 250 rows'."""
    payload = {"season": SEASON, "rows": [{"season": SEASON, "week": "three", "game_type": "REG", "home": "KC", "away": "BUF"}] * 250}
    built = _schedule_scenario(lab, "schedule:non-int-weeks", age=dt.timedelta(hours=1), payload=payload)
    assert built.health.by_family("nflverse_schedule")[0].label != FRESH


@pytest.mark.xfail(strict=True, reason=(
    "nfl_schedule.load_schedule returns a Schedule with zero games for a payload whose rows all fail to parse "
    "(missing columns), so build_windows says 'regular season over' in week 3 and plan_streams — which filters "
    "its weeks through schedule.regular_weeks() — silently drops every streamer plan; no schedule at all would "
    "have kept them"
))
def test_an_unparseable_schedule_degrades_like_no_schedule_at_all(lab):
    """Repro: cached payload {'season': 2026, 'rows': [{'week': 1, 'home': 'KC'}] * 250};
    `ld.windows.describe()` contains 'regular season over' and `ld.streamers == []`, while
    the same league with no schedule file has three streamer plans."""
    payload = {"season": SEASON, "rows": [{"week": 1, "home": "KC"}] * 250}
    built = _schedule_scenario(lab, "schedule:missing-columns", age=dt.timedelta(hours=1), payload=payload)
    none_at_all = _schedule_scenario(lab, "schedule:61d", age=dt.timedelta(days=61))
    assert none_at_all.ld.streamers
    assert "regular season over" not in built.md
    assert [p.position for p in built.ld.streamers] == [p.position for p in none_at_all.ld.streamers]


@pytest.mark.xfail(strict=True, reason=(
    "nfl_usage.cached_health counts the raw cached rows, duplicates included, so a file of 100 distinct "
    "player-weeks shipped twice reports 200 rows and clears the 200 floor that the same 100 games alone would not"
))
def test_duplicated_rows_do_not_lift_a_partial_usage_file_over_the_floor(lab):
    """Repro: 20 players x 5 weeks with every row twice -> `cached_health(2026).rows == 200`
    and the label is Fresh; the deduplicated `UsageData.player_weeks` has 100."""
    clean = _usage_scenario(lab, "usage:100-clean", limit=20)
    doubled = _usage_scenario(lab, "usage:100-doubled", limit=20, duplicate=True)
    assert len(doubled.report.usage_loaded.player_weeks) == len(clean.report.usage_loaded.player_weeks) == 100
    assert clean.health.by_family("nflverse_usage")[0].label == PARTIAL
    assert doubled.health.by_family("nflverse_usage")[0].label == PARTIAL
