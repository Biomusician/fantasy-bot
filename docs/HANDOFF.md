# Handoff — as of 2026-09-03

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

**Local commits ahead of `origin/main`, not pushed.** The second decision-layer tranche
(12 capabilities, one commit each plus integration, fix-up and docs commits) landed on
2026-09-02/03 on top of `76e64ff`. Pushing was not authorized by the tranche prompt; the
9am ET automated run still publishes the previous version until someone pushes.

What landed, in order (each its own module in `sleeper_tool/`, tests alongside):
`replacement_value`, `source_disagreement`, `trade_opportunity_cost`, `nfl_schedule` +
`streamer_planner`, `market_velocity`, `matchup_leverage` + `opponent_blocker`,
`roster_consolidation`, `stash_board`, `schedule_window`, `buyer_board`,
`recommendation_conflicts`. `report_data.py` orchestrates all of them; both renderers show
everything (trade cards carry economics chips, conflict blocks and source/replacement
notes; the waiver table carries `notes`; the collapsed "Roster context" holds the
replacement market, stash board, buyer board, schedule windows). `docs/DECISIONS.md`
records the reasoning behind every threshold and design choice of the tranche.

A three-agent red-team review (value semantics; architecture/data flow; tests and real
output) ran at the end of the tranche; the review-fix commit after `91c72cd` lists every
confirmed finding and its fix (consolidations folded into the proposal list, rank-scaled
source gaps, replacement level ignoring abandoned rosters, conflict rules tightened,
buyer-board scoring, streamer sequences, ~2x faster report build). `docs/DECISIONS.md`
has the reasoning.

Verified 2026-09-03: 366 tests pass in ~1.5s, `generate_report.py` rebuilds all 9
leagues from cache in ~7s (the memoized consolidation search is the largest new cost),
dashboard renders with the hierarchy intact (Best Moves → alerts/matchup →
trades/waivers/streamers/defensive add → collapsed context).

## Run and test

```
.venv/Scripts/python.exe -m pytest tests/ -q          # no network
.venv/Scripts/python.exe scripts/daily_run.py         # full sync + both reports + snapshot
.venv/Scripts/python.exe scripts/generate_report.py   # Markdown from cache, no network*
.venv/Scripts/python.exe scripts/generate_dashboard.py
```
\*`generate_report.py` will fetch the nflverse schedule once a day if the cache in
`data/rankings_cache/nflverse_schedule.json` is older than 24h (2 MB CSV, no auth).
Output lands in `data/weekly_report.md` and `data/dashboard.html`. Only `daily_run.py`
writes `data/run_snapshots/` (one file per UTC day, last 28 kept, only after a fully
complete run) — market velocity needs three of them before it says anything.

## Where things live

- `sleeper_tool/config.py` — league identities. Everything else about a league is read
  from the Sleeper API at runtime, on purpose.
- `sleeper_tool/valuation.py` — format-aware per-player value; `weekly_projection`,
  `games_remaining`, `composite_overall_rank`, `ValuationEngine.snapshots_for(fmt)`.
- `sleeper_tool/trade_engine.py` — ~1950 lines. Candidate selection, opponent-fit scoring,
  acceptance rating, message generation. `roster_consolidation` and `buyer_board` import
  its private helpers (`_recipient_need_fit`, `_status_fit`, `_piece_fits`,
  `_tradeable_pool`, `_untouchable_ids`) — the same debt `negotiation_ladder` carries.
- `sleeper_tool/waiver_engine.py` — waiver targeting; `WaiverTarget.notes` is where every
  decision-layer annotation goes now (reason stays the engine's own sentence).
- `sleeper_tool/lineup_optimizer.py` — the ONE place that decides who starts. Structural
  by default; `nfl_week=current_week, exclude_game_day_out=True` gives the this-week
  lineup used by `matchup_leverage`, `opponent_blocker` and Move Impact. Totals are
  rest-of-season projections: divide by `games_remaining(current_week)` for per-week.
- `sleeper_tool/nfl_schedule.py` — the one non-ranking fetch; `Schedule.is_bye`,
  `opponent`, `regular_weeks`. `schedule_window.py` derives playoff weeks from Sleeper's
  `playoff_week_start` / `playoff_teams` / `playoff_round_type`.
- `sleeper_tool/report_data.py` — the orchestration seam. `build_league_report_data` order:
  proposals → clogs → pre-draft gate → free-agent pool (once; `require_projection=False`,
  projected subset for lineup consumers) → replacement market → insurance → alerts/bye →
  drops → leverage → replacement/source annotations → stash → matchup/defensive add →
  economy → windows/schedule notes → playoff → statuses/ladders/consolidations/buyer
  boards → previews/economics/streamers. Cross-league passes in
  `build_weekly_report_data`: exposure → velocity → conflicts → priority actions →
  snapshot/delta.
- `docs/DECISIONS.md` — why each threshold and design choice is what it is.

## In flight

Nothing half-implemented. Unpushed local commits (see Status).

## Known problems

- `build_league_report_data` is now a very long orchestration function; every capability
  adds a block. A split into named stages (pool → lineup features → trade features →
  cross-annotations) would help the next tranche.
- `roster_consolidation` and `buyer_board` import private `trade_engine` helpers. A
  public `rate_package(...)` / `piece_fit(...)` surface in `trade_engine` would remove
  the drift risk for three callers.
- Real-data early-season quirks: no Defensive Adds before NFL byes start (correct, but
  the block renders nothing); every Superflex sell-high of a QB is a Conflicted Move by
  construction (Very Scarce QB market); the stash board is all "Watch" on full rosters
  with no clogs.
- Market velocity shows Insufficient History until three daily snapshots exist
  (retention only just went to 28 days).
- `get_or_fetch`'s stale-cache fallback still has no ceiling.
- Acceptance ratings still have no feedback loop; usage/role data not yet integrated.

## Decisions that constrain future work

- League settings are **always** read from the Sleeper API, never hardcoded — including
  fantasy playoff weeks.
- Every label is bucketed; no probabilities. Asset and roster economics stay separate.
- The default lineup is STRUCTURAL; this-week lineups only where the question is this
  week's game.
- Snapshot `schema` is 2; additive fields don't bump it, a change of meaning does.
- The test suite is fully synthetic and offline (schedule tests use inline CSV + a temp
  cache dir). No scipy/pandas/Polars.
- The nflverse schedule is fetched at most once a day unless forced.

## Next actions

1. **Decide on the push.** The tranche is local only. Regenerate, skim the dashboard, and
   push when satisfied — pushing changes what the 9am run publishes.
2. **Watch the first three daily snapshots**: market velocity turns on at the third, and
   "Since last run" should stay sparse.
3. **Public `rate_package` / fit helpers in `trade_engine`** to stop three modules importing
   private functions.
4. **Split `build_league_report_data`** into named stages.
5. **Ceiling on the stale-cache fallback** in `sleeper_tool/rankings/cache.py`.
6. Usage/role data source (tracked separately; not part of this tranche by instruction).

## Gotchas

- Three Pythons on PATH. Always `.venv/Scripts/python.exe`, never bare `python`.
- `data/yahoo_token.json` is a real credential. Don't read, print, or commit it.
- Don't scrape KTC/FantasyPros/RotoBaller during development — use the cache.
- The repo path has a space in it. Quote it in every shell command.
- Pushing to `main` changes what the 9am ET automated run publishes. Treat push as a
  deploy, not a save.
- Bash heredocs with long Python bodies have failed to parse in this environment; write
  patch scripts to the scratchpad and run them instead.
- The dashboard Artifact ("Fantasy Command Center") must be re-read before republishing
  from a new session, or the publish is refused.
