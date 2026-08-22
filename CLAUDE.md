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

121 tests, ~0.2s, fully synthetic — no network. Keep it that way.

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
(contender/rebuild) → `trade_engine.py` (1273 lines; the acceptance-rating logic is the
hard part) and `waiver_engine.py`. `report_data.py` is the shared layer both renderers
read from — put new derived data there, not in a renderer.

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

## Don't

- Don't add a network call to the test suite.
- Don't hammer KeepTradeCut, FantasyPros, or RotoBaller during development — use the cache
  or fixtures. These are free sources that can rate-limit or block.
- Don't regenerate `data/*.sqlite3` or delete the rankings cache to "start clean" without
  asking; a refill costs real scraping.
