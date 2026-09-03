# Fantasy Bot

Weekly decision-support for Jonathan's Sleeper dynasty/keeper/redraft leagues. Pulls
rosters, reconciles three independent ranking sources, and emits trade offers, waiver
targets, and alerts as a Markdown report plus an HTML dashboard. Built for one specific
set of leagues, not as a multi-user product.

## Run it

```
.venv/Scripts/python.exe scripts/daily_run.py
```

Individual stages: `scripts/pull_data.py` (sync only), `scripts/generate_report.py`
(Markdown), `scripts/generate_dashboard.py` (HTML).

## Test it

```
.venv/Scripts/python.exe -m pytest tests/ -q
```

850+ tests, about three seconds, fully synthetic — no network. Keep it that way.

## Conventions

- Use `.venv/Scripts/python.exe` explicitly. Three Pythons are on this machine's PATH.
- Everything fetched over the network is cached in SQLite (`sleeper_tool/storage.py`) or
  `data/rankings_cache/`. Re-runs must not refetch what hasn't changed.
- Scraping (`sleeper_tool/rankings/*`) is deliberately separate from valuation and
  analysis. When a source changes its HTML, only the scraper should need touching.
- Flat modules, plain functions, `from __future__ import annotations`, dataclasses for
  records. No class hierarchies.
- Every run is idempotent.

## Where the thinking is

`valuation.py` (format-aware player values) → `roster_analysis.py` → `team_status.py`
(contender/rebuild) → `trade_engine.py` (~1950 lines; the acceptance-rating logic is the
hard part) and `waiver_engine.py`. `report_data.py` is the shared layer both renderers
read from — put new derived data there, not in a renderer.

`lineup_optimizer.py` is the single owner of "best legal starting lineup" (exact DP over
the league's real slot list). Everything lineup-aware consumes it rather than deciding
who starts on its own: `lineup_leverage.py`, `contender_insurance.py`, `bye_collision.py`,
`move_impact.py`, `roster_clog.py`, `pick_opportunity.py`, `replacement_value.py`,
`streamer_planner.py`, `matchup_leverage.py`, `opponent_blocker.py`,
`roster_consolidation.py`. The other decision modules (`portfolio_exposure.py`,
`league_economy.py`, `playoff_leverage.py`, `negotiation_ladder.py`, `decision_delta.py`,
`source_disagreement.py`, `trade_opportunity_cost.py`, `market_velocity.py`,
`stash_board.py`, `schedule_window.py`, `buyer_board.py`, `recommendation_conflicts.py`)
are each one isolated file whose computation never lives in
`trade_engine.py`/`waiver_engine.py` — those two only get thin hooks. Annotations from the
decision layer land on `WaiverTarget.notes`, `TradeProposal.rationale_*`/`caveats`, and
`LadderStep.source_note`; renderers join them, never compute them.

`nfl_schedule.py` (nflverse `games.csv`) and `nfl_usage.py` (nflverse weekly stats, team
stats, snap counts, plus the DynastyProcess/nflverse id files) are the non-ranking external
fetches, cached in `data/rankings_cache/` (24h; 7 days for the id files; an unpublished
season is cached as an explicit absent marker). `player_ids.py` maps Sleeper ids to gsis
ids; `role_analysis.py` / `role_trends.py` turn usage into role windows and labels.

The intelligence layer added on 2026-09-03 follows the same one-file-per-capability rule:
`decision_ledger.py` / `decision_outcomes.py` (feedback: recorded, never graded),
`calibration.py` (rule diagnostics, never auto-tuning), `recommendation_provenance.py`
(For/Against/Context cards), `action_priority.py` (lexicographic Best Moves ordering),
`faab_strategy.py`, `watchlist.py`, `signal_health.py` + `rankings/freshness.py`.
`report_views.py` holds render-only choices shared by both renderers (which phrasing of
one fact keeps the visible slot, visible/collapsed splits) — never decision logic. Only
`scripts/daily_run.py` persists the snapshot, ledger and watchlist, after a complete run.

## Constraints

- **Never hardcode a league setting the Sleeper API can report.** Scoring type, superflex,
  PPR variant, TE premium, roster slots, and playoff format are all read from
  `/league` at runtime. `config.py`'s `qb_format` field is informational only.
- Model output must not sound more precise than its inputs justify. Acceptance ratings are
  bucketed (Very Low → High) on purpose — do not convert them to percentages.
- Known modeling gaps are documented at the end of `README.md`. Read that list before
  "fixing" something that's a deliberate approximation.
- `data/` holds generated output and one genuinely sensitive file
  (`data/yahoo_token.json`). Never read, print, or commit it.
- `data/run_snapshots/` is the "since last run" baseline and the market-velocity history
  (one JSON per UTC day, last 28 kept, schema 2 with an additive `tracked` bucket),
  written only by `scripts/daily_run.py` after a fully complete run. The report scripts
  read it but never write it.

## Don't

- Don't add a network call to the test suite.
- Don't hammer KeepTradeCut, FantasyPros, or RotoBaller during development — use the cache
  or fixtures. These are free sources that can rate-limit or block.
- Don't regenerate `data/*.sqlite3` or delete the rankings cache to "start clean" without
  asking; a refill costs real scraping.
