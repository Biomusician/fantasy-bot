# Handoff — as of 2026-09-04 (night build: replay, thesis tracking, lineup decisions, red team)

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

**Local commits only — NOT pushed.** Two tranches (2026-09-03 intelligence & hardening,
2026-09-04 night build) sit on `main` on top of `2d1f739`, the last pushed commit.
Pushing is Jonathan's decision; the 9am ET automated run publishes whatever
`origin/main` holds, so treat the push as a deploy.

### Must QC before pushing

Both renderers changed shape, and several engine rules now suppress recommendations
that used to appear. Read, in `data/weekly_report.md` or the dashboard:

1. **Best moves** — the "Do this week" group should contain only moves you would
   actually make today. Anything in it that reads as a value play means
   `report_views.split_actions` is mis-classifying.
2. **This week's decisions**, in two or three leagues — every set-lineup mismatch is a
   claim about what Sleeper currently has set. Check one against the Sleeper app.
3. **The Surfeit and Disco waiver tables** — tiers and bids moved a lot (Abundant-market
   cap, Preserve floor). Confirm the surviving Must Add / Strong Add rows are ones you
   would actually claim.
4. **Any trade card carrying "drops this team from contender to middling"** — new
   Against; confirm the status call is right before trusting it.
5. **Draft capital in a dynasty league** — pick tiers now vary (Early / Mid / Late).
   Spot-check one pick against the original owner's actual standing.
6. **`data/calibration_report.md`** — "Drop protection" flags International AWACKOS as a
   roster with nobody droppable (1 of 23). That is a real over-protection signal.
7. Run `scripts/daily_run.py` once and confirm 9/9 leagues, then rerun and confirm the
   second run adds no ledger entries (same-day idempotence).

What landed on 2026-09-04 (night build):

- `historical_replay.py` + `scripts/backtest_report.py` → `data/backtest_report.md`:
  2025 role-signal replay with no leakage, snapshot replay, ledger-outcome replay.
- `lineup_decisions.py` — "This week's decisions", ahead of trades in both renderers.
- `cross_league_asymmetry.py` — cheapest and dearest league to move a held player.
- `recommendation_search.py` + `scripts/search_recommendations.py` — query one report.
- `watchlist.py` rewritten around a thesis per item and five thesis states.
- `calibration.py` v2 — Rare / Healthy / Overactive / Format-Biased / Position-Biased /
  Potential Double Count labels, contradictions, a drop-protection monitor, and a
  dependency map of how many rules read one fact.
- `tests/test_data_health_chaos.py` — absent, stale, corrupt, duplicated and
  schema-mismatched inputs end to end.
- Engine and renderer fixes from a ten-agent red team; every one is recorded with its
  reasoning in `docs/DECISIONS.md` under 2026-09-04.

What landed on 2026-09-03 (each capability its own module under `sleeper_tool/`, tests alongside):

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
idempotent. The suite is 1142 synthetic tests in ~6s.

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

Nothing is half-finished. The next development opportunities, in the order they would
pay off:

- **The trade engine still cannot buy a need.** `identify_buy_low` requires RotoBaller's
  "down" arrow, so a roster whose starters project below the wire at a position (real
  case: International AWACKOS starting RBs at 3.8 and 2.5/wk while holding the league's
  largest pick hoard) never gets a "spend a pick on an RB" proposal. A need-buy pass
  that bypasses the trend gate when my weakest starter is below replacement is the
  single largest remaining false negative.
- **A rebuild never shops its veterans.** `identify_sell_high` also keys on the trend
  arrow, so a 17th-percentile rebuild holding four 30-plus veterans generates nothing.
- **`max_proposals` is shared across passes in a fixed order**, so a rebuild gets three
  buy-lows and the pick-target pass never runs. Reserve a slot per status-appropriate
  pass.
- **Bye planning stops at 4 weeks** (`bye_collision.LOOKAHEAD_WEEKS`) and streaming at
  3, so a week-7 bye stack worth -24/wk is invisible until week 3 and a week-13 one
  until after the trade deadline. Scanning to the deadline for trade-fixable weeks is
  the fix.
- **No keeper logic at all.** Primo Veterans is a pre-draft keeper league where the only
  decision that matters this month is which four to keep; the report offers trades on a
  roster that will mostly be released.
- **Message assembly still leaks internals** (the buy-low thesis is told to the seller;
  "depth behind X" appears in the pitch; percentile gaps read as points to a league-mate).
- **`build_league_report_data` is ~380 lines of straight-line stages whose order now
  matters more, not less** (the buyer board and the replacement market both run before
  proposals). Splitting it into named stages is overdue.

## Known problems

- Week 1 with no 2026 usage rows: every role line is suppressed, velocity needs three
  daily snapshots, no NFL byes yet, so no defensive adds and all streamers Hold.
- **The 2025 replay says the role labels behave backwards on that season.**
  `data/backtest_report.md`: Role Surging preceded a 6-point share LOSS 13% of the time
  against Stable's 3%, and Role Collapsing preceded a share RISE 11% of the time. The
  conservative one-week structural rule (`SURGE_ONE_WEEK_SNAP_JUMP`) is the well-behaved
  part; the multi-component path (`STRONG_COMPONENTS_FOR_EXTREME`) is where the mean
  reversion lives. 2025 is the season the thresholds were smoke-tested on, so this
  describes behaviour and validates nothing — but it is a reason not to trust a rising
  label as a buy signal until it is re-checked on 2026 data.
- Sell-high is still RotoBaller's rank arrow (fires on ~1/3 of the pool); the
  value-match tolerance never sees replacement scarcity; simultaneous offers are each
  previewed against the untouched roster; the waiver table has no roster-capacity check.
- `_roster_impact_note` on the trade side still reads Sleeper's set-lineup flag (it has
  no lineup in hand), so "your current starting X" can name a different player there
  than the optimizer starts. The waiver side takes `starter_ids` and does not.
- Calibration flags 115 of 126 rules; most flags are "Rare" or "Nearly Universal" on a
  week-1 sample of 8 leagues and are not actionable yet.
- `data/backtest_report.md` and `data/calibration_report.md` are gitignored outputs.

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
