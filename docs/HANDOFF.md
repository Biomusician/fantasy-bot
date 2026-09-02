# Handoff — as of 2026-09-02

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

**Local `main` is 17 commits ahead of `origin/main` and has NOT been pushed** — the
overnight feature run (2026-09-01/02) was deliberately kept local so Jonathan can QC
eleven features' worth of dashboard output before the 9am ET automated run publishes
them. Pushing is his call. Everything is committed; the tree is clean.

A post-run QC pass (Jonathan's eight-point list: move-impact deltas, SF/multi-flex
lineups, clogs, insurance, ladders, weak-aging, delta sparsity, dashboard hierarchy)
produced `9ec78cd` and `b680578`: developmental dynasty players exempt from clog
detection, weak-aging gated on the position's veteran age, ladder steps that spend a
current starter say so, pre-draft leagues get no waiver/insurance targets, and the
dashboard collapses the context sections under one disclosure.

What landed (one commit per feature, then one red-team fix commit):
`lineup_optimizer.py` (shared), then Roster Clog Detector, Portfolio Exposure, Lineup
Leverage, Contender Insurance, Bye Collision Planner, Decision Delta, League Economy Map,
Move Impact Preview, Playoff Leverage, Pick Opportunity Cost, Negotiation Ladder. The
feature specs came from a two-round ChatGPT brainstorm Jonathan ran; the file-level design
(each feature its own module, `trade_engine.py`/`waiver_engine.py` get thin hooks only,
`report_data.py` stays the single derived-data seam) was the standing plan he approved.

A `/code-review high` red-team pass over the whole diff produced ten confirmed/plausible
findings; all ten are fixed in `dee2e07` (see that commit message for the list — the
important ones were redraft value swings firing on every week rollover, post-trade team
status computed off stale `is_starter` flags, IR/PUP free agents recommendable as
insurance, and an unknown slot type blanking a whole league).

Verified 2026-09-02: 280 tests pass in ~0.4s; `scripts/daily_run.py` runs end to end
against all 9 leagues (~3s offline rebuild after the `get_all_players` memoization).

## Run and test

```
.venv/Scripts/python.exe -m pytest tests/ -q          # 280 tests, no network
.venv/Scripts/python.exe scripts/daily_run.py         # full sync + both reports + snapshot
.venv/Scripts/python.exe scripts/generate_report.py   # Markdown from cache, no network
.venv/Scripts/python.exe scripts/generate_dashboard.py
```
Output lands in `data/weekly_report.md` and `data/dashboard.html`. Only `daily_run.py`
writes `data/run_snapshots/` (the "since last run" baseline; one file per UTC day, last
two kept, only after a fully complete run).

## Where things live

- `sleeper_tool/config.py` — league identities. Everything else about a league is read
  from the Sleeper API at runtime, on purpose.
- `sleeper_tool/valuation.py` — format-aware per-player value; the source reconciliation.
  Also `weekly_projection`, `games_remaining`, `composite_overall_rank`.
- `sleeper_tool/trade_engine.py` — ~1950 lines. Candidate selection, opponent-fit scoring,
  acceptance rating, and message generation. Still the hard logic in the project.
- `sleeper_tool/waiver_engine.py` — trending-add targeting, drop candidates, FAAB. Now
  also the home of the `INSURANCE` tier and the shared drop-candidate search that
  prefers roster clogs.
- `sleeper_tool/lineup_optimizer.py` — the ONE place that decides who starts. Exact DP
  over a slot bitmask; raises `UnsupportedSlotError` for slot types it doesn't know
  (report_data degrades to "lineup features skipped" for that league).
- The decision layer, one module each: `lineup_leverage`, `move_impact`,
  `contender_insurance`, `bye_collision`, `roster_clog`, `portfolio_exposure`,
  `league_economy`, `playoff_leverage`, `pick_opportunity`, `negotiation_ladder`,
  `decision_delta`. Each has a module docstring stating its rules and thresholds.
- `sleeper_tool/report_data.py` — the orchestration seam. `build_league_report_data`
  now calls every module above in a fixed order (proposals → clogs → waivers →
  insurance → alerts/bye → drops → leverage → economy → playoff → ladders/picks →
  previews) and several passes annotate `TradeProposal`/`WaiverTarget` objects
  in place. Cross-league passes (exposure, delta) run in `build_weekly_report_data`.
- `sleeper_tool/storage.py` — SQLite cache. `get_all_players()` is memoized per
  instance (treat the dict as read-only); `get_all_transactions()` returns every cached
  week.
- `AUTONOMOUS_IMPROVEMENT_REPORT.md` — rationale for the trade-engine design (earlier
  session). Not updated for the decision layer; the module docstrings and README's
  "Known limitations" carry that.

## In flight

Nothing half-implemented. The only open decision is whether to push (above).

## Known problems

- `report_data.build_league_report_data` has become a long orchestration function
  with post-hoc string appends onto `WaiverTarget.reason` from three places (insurance
  merge, bye cover, exposure) plus renderer-side `Impact:` suffixes. The cleaner shape
  is a `notes: list[str]` on `WaiverTarget` (the shape `TradeProposal` already uses) and
  letting renderers join. Flagged in the red-team pass; deliberately not done in the
  same run as the features.
- The two renderers each re-derive some presentation strings (ladder step text,
  impact fallbacks, economy row ordering). A wording tweak must be made twice.
- `negotiation_ladder` rebuilds the engine's package-rating pipeline from five private
  `trade_engine` helpers and duplicates the 0.85 lowball literal. A public
  `rate_package(...)` in trade_engine that both callers use would remove the drift risk.
- `classify_team_status` is recomputed per counterparty in report_data (the engine
  already computed it inside `generate_trade_proposals` but doesn't return it). Small
  cost (~0.2s/run), pure redundancy.
- `trade_engine.py` still mixes scoring, orchestration, and presentation; unchanged
  this run, still worth splitting before more fields go into `TradeProposal`.
- `get_or_fetch`'s stale-cache fallback has no ceiling (unchanged; still the quietest
  failure mode).
- Acceptance ratings have no feedback loop; no usage/role data source. Unchanged.

## Decisions that constrain future work

- League settings are **always** read from the Sleeper API, never hardcoded.
- Acceptance ratings stay bucketed; every new label in the decision layer is bucketed
  too (Toss-Up, Fragile, Bye Hole, Bubble, Strategic…). No invented probabilities.
- The optimizer's default lineup is STRUCTURAL: it keeps a player tagged `Out` this week.
  A this-week lineup is `optimize_lineup(..., exclude_game_day_out=True)`; nothing
  renders one yet. Don't "fix" the structural one to drop `Out` players.
- Post-move team status is classified on optimizer-flagged starters for both sides and
  only reported with a 10-point strength move — the headline status uses Sleeper's set
  lineup, so the two can legitimately differ.
- Redraft snapshot values are per-game (rest-of-season totals shrink every week by
  construction). Snapshot `schema` is 2; bump it if the meaning of a field changes.
- The test suite is fully synthetic and offline. Keep it that way.
- No scipy: the optimizer's problem is ≤20 players × ≤18 slots, a bitmask DP is exact.

## Next actions

1. **Jonathan QCs the dashboard, then pushes** (`git push origin main`). Until then the
   9am automated run keeps publishing the pre-run version.
2. **`WaiverTarget.notes`** — replace the reason-string appends with a list (see Known
   problems). Small, and it stops the "Why" column becoming an order-dependent run-on.
3. **Render a this-week lineup** somewhere (the optimizer already supports it) so
   lineup leverage can say "sit him, he's Out" rather than only structural calls.
4. **Ceiling on the stale-cache fallback** in `sleeper_tool/rankings/cache.py` — still
   the quietest way the tool goes wrong.
5. **Split the presentation layer** out of `trade_engine.py`.
6. **Usage/role data source** (`nfl_data_py`/nflverse) as a buy-low trigger.

## Gotchas

- Three Pythons on PATH. Always `.venv/Scripts/python.exe`, never bare `python`.
- `data/yahoo_token.json` is a real credential. Don't read, print, or commit it.
- Don't scrape KTC/FantasyPros/RotoBaller during development — use the cache.
  `generate_report.py`/`generate_dashboard.py` are the no-network regeneration path.
- The repo path has a space in it. Quote it in every shell command.
- Pushing to `main` changes what the 9am ET automated run publishes. Treat push as a
  deploy, not a save.
- `data/run_snapshots/` is gitignored generated state. Deleting it just means the next
  report has no "since last run" section; a same-day re-run overwrites today's file.
- The dashboard Artifact ("Fantasy Command Center") must be re-read before republishing
  from a new session, or the publish is refused.
