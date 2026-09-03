# Handoff — as of 2026-09-03 (overnight intelligence & hardening tranche)

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

**Local commits only — NOT pushed.** The overnight tranche of 2026-09-03 sits on `main`
on top of `2d1f739` (the last pushed commit). Pushing is Jonathan's decision; the 9am ET
automated run publishes whatever `origin/main` holds, so treat the push as a deploy.
Before pushing, do the manual QC listed at the end of the tranche's completion report
(the message that closed the session), or at minimum: open `data/dashboard.html`, read
the Best Moves list and one league panel, and run `scripts/daily_run.py` once.

What landed (each capability its own module under `sleeper_tool/`, tests alongside):

- **P0** trade-engine primitives extracted into `asset_value`, `trade_types`,
  `roster_assets`, `trade_fit`, `trade_rating`, `trade_messages` (byte-identical output,
  characterization tests first).
- **P1/P2** `nfl_usage`, `player_ids`, `role_analysis`, `role_trends` — nflverse weekly
  usage through a documented id crosswalk; role labels and the role-vs-market cross.
  The 2026 feed is not published yet (a cached "absent" marker, one request per day);
  the 2025 season is fully cached and was used to smoke-test the layer.
- **P3/P4** `decision_ledger`, `decision_outcomes` — fingerprinted recommendations,
  Sleeper-observed outcomes, 1/3/6-week descriptive facts. Persisted by `daily_run.py`
  only, after a complete run, in `data/decision_ledger/ledger.json`.
- **P5** `calibration` + `scripts/calibration_report.py` → `data/calibration_report.md`
  (also written by `daily_run.py`).
- **P6/P7** `recommendation_provenance`, `action_priority` — For/Against/Context cards
  and the six-dimension lexicographic Best Moves ordering.
- **P8/P9** `faab_strategy`, `watchlist` (persisted in `data/watchlist/watchlist.json`
  by `daily_run.py` after a complete run).
- **P10** `signal_health` + `rankings/freshness` — per-source labels, feature
  suppression, stale-cache ceiling, trending-table replace fix.
- **P11** both renderers restructured (hierarchy, Why now cards, progressive
  disclosure, one vocabulary per fact via `report_views.py`, parity tests).
- A seven-reviewer red team (strategy, double-counting, data reliability,
  architecture, tests, UX, performance) and three fix batches; `docs/DECISIONS.md`
  "After the seven-reviewer red team" records every behaviour that changed and why.

`daily_run.py` was run twice end to end on 2026-09-03 (9/9 leagues): the second run
recorded 0 new ledger entries and 0 new watchlist triggers, i.e. same-day reruns are
idempotent. The suite is ~850+ synthetic tests in ~2.5s.

## Run and test

```
.venv/Scripts/python.exe -m pytest tests/ -q          # no network
.venv/Scripts/python.exe scripts/daily_run.py         # sync + both reports + snapshot + ledger + watchlist + calibration
.venv/Scripts/python.exe scripts/generate_report.py   # Markdown from cache*
.venv/Scripts/python.exe scripts/generate_dashboard.py
.venv/Scripts/python.exe scripts/calibration_report.py
```
\*The report scripts fetch the nflverse schedule once a day and the nflverse usage
assets once a day (a 404 for an unpublished season is cached for a day too). They never
write the ledger, watchlist or snapshot — only `daily_run.py` does, and only after a
complete run (every league synced and built, no ranking source missing or served from a
failed re-fetch).

## Where things live

- `sleeper_tool/report_data.py` — the orchestration seam. `build_league_report_data`
  order: rosters/status/proposals → shared structural lineup map → free-agent pool →
  replacement market → statuses → consolidations → clogs → waivers (optimizer starter
  ids and protected ids threaded in) → insurance (Scarce/Very Scarce positions only) →
  alerts/bye → lineup leverage → drops (bench surplus excluded) → replacement/source
  annotations (each note's side recorded in `note_directions`) → stash → matchup /
  defensive add → economy → windows → playoff → ladders/buyer boards → previews
  (never for Insurance rows) → economics → streamers → role trends → FAAB advice.
  `build_weekly_report_data`: usage layer + crosswalk → signal health (before the
  leagues, so Unavailable families suppress) → leagues → exposure → velocity → role
  annotations → conflicts → provenance + priority keys → Best Moves
  (`action_priority.rank_actions`) → snapshot/delta → watchlist → ledger.
- `sleeper_tool/report_views.py` — render-only choices shared by both renderers
  (which phrasing of one fact keeps the visible slot, visible/collapsed splits, the
  health banner and state, the Best Moves ordering note).
- `docs/DECISIONS.md` — why each threshold and design choice is what it is, including
  everything the red team changed.

## In flight / to verify next session

- **Performance pass on `lineup_optimizer.py`** (mask tables, back-pointer DP, per-run
  memo, `Storage.get_league` memo, cache-read memo) was delegated at the end of the
  tranche; check `git log` for its commit and that the report is byte-identical.
- **Test hardening** (documented constants pinned at absolute boundaries, NA parsing,
  the `daily_run` persistence gate, partial-failure end-to-end, parity self-check,
  duck-typing guard) was delegated at the same time; check for its commit and any
  `xfail` it added (a `strict=True` xfail is a real bug to fix).

## Known problems

- Week 1: no usage rows for 2026 yet, so every role line is suppressed and the health
  block says so once; velocity needs three daily snapshots; no NFL byes yet, so no
  defensive adds and all streamers Hold; Must Add is rare by design now (a need that
  beats the weakest starter), so most waiver rows are Strong Add / Streamer at Preserve
  bids in Abundant markets.
- The sell-high signal is RotoBaller's rank arrow (fires on ~1/3 of the pool); the
  value-match tolerance never sees replacement scarcity; simultaneous offers are each
  previewed against the untouched roster; the waiver table has no capacity check.
- `build_league_report_data` is ~320 lines of straight-line stages whose order matters.
- Two `_roster_impact_note` phrasings (trade vs waiver) remain by design; the trade one
  still reads Sleeper's set-lineup flag (it has no lineup in hand).

## Decisions that constrain future work

- League settings are always read from the Sleeper API, never hardcoded.
- Every label is bucketed; no probabilities. Materiality is the gain a move makes.
- Structural lineups by default; the optimizer's starters are "who starts" everywhere.
- The feedback ledger records and never grades; calibration reports and never tunes.
- `daily_run.py` alone persists state, only after a complete run (`is_complete_run` now
  also refuses a run with a missing or fallback-served ranking source).
- Tests are synthetic and offline; no scipy/pandas/Polars; stdlib first.

## Gotchas

- Three Pythons on PATH. Always `.venv/Scripts/python.exe`, never bare `python`.
- `data/yahoo_token.json` is a real credential. Don't read, print, or commit it.
- Don't scrape KTC/FantasyPros/RotoBaller during development — use the cache.
- The repo path has a space in it. Quote it in every shell command.
- Bash heredocs with long Python bodies fail to parse in this environment; write patch
  scripts to the scratchpad and run them instead.
- `.claude/launch.json` (untracked, gitignored) serves `data/` on port 8765 for the
  browser preview of `dashboard.html`.
- The dashboard Artifact ("Fantasy Command Center") must be re-read before republishing
  from a new session, or the publish is refused.
