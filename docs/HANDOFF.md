# Handoff — as of 2026-08-29

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

Working and in sync with `origin/main`. The trade and waiver recommendation engines were
overhauled and then hardened in a second review pass (commits `475bf2f`, `16b8ac7`), and
the full findings are written up in `AUTONOMOUS_IMPROVEMENT_REPORT.md` — read that before
touching `trade_engine.py`, since it records which approximations are deliberate.

An unattended cloud routine runs `scripts/daily_run.py` at 13:00 UTC (9am ET during
daylight saving, 8am ET the rest of the year — the cron itself is fixed UTC) and
publishes the dashboard, so anything pushed to `main` goes live on the next run. Verified
2026-08-29: 184 tests pass in ~0.2s.

Since this doc was first written (`349ac91`): "The Surfeit" had a blank `my_team_name`
backfilled (`4613d17`), and "The 7th League" was removed from `sleeper_tool/config.py`
(`481e89d`) after confirming via `/user/{id}/leagues/nfl/2026` that it no longer exists on
Sleeper's side (not a transient 404 — every other configured league, including other
pre-draft ones, still showed up there). All pushed to `origin/main`.

## Run and test

```
.venv/Scripts/python.exe -m pytest tests/ -q          # 184 tests, no network
.venv/Scripts/python.exe scripts/daily_run.py         # full sync + both reports
```
Output lands in `data/weekly_report.md` and `data/dashboard.html`.

## Where things live

- `sleeper_tool/config.py` — league identities. Everything else about a league is read
  from the Sleeper API at runtime, on purpose.
- `sleeper_tool/valuation.py` — format-aware per-player value; the source reconciliation.
- `sleeper_tool/trade_engine.py` — ~1950 lines. Candidate selection, opponent-fit scoring,
  acceptance rating, and message generation all live here. The hard logic in the project.
- `sleeper_tool/waiver_engine.py` — trending-add targeting, drop candidates, FAAB.
- `sleeper_tool/report_data.py` — shared derived-data layer. New computed fields belong
  here, not in `report.py` or `html_report.py`.
- `sleeper_tool/storage.py` — SQLite cache of everything fetched.
- `AUTONOMOUS_IMPROVEMENT_REPORT.md` — rationale for the current engine design.

## In flight

Nothing half-implemented and nothing uncommitted. Everything above is merged to
`origin/main`.

## Known problems

- `trade_engine.py` mixes scoring, orchestration, and presentation in one file. The
  presentation layer (rationale + message templating) is the safe thing to split out; it
  carries no scoring risk. Deferred deliberately, not forgotten — but the file has grown
  from ~1150 to ~1950 lines since that call was made, so the tradeoff is worth revisiting.
- `get_or_fetch`'s stale-cache fallback has no ceiling — if a ranking source breaks
  permanently, every run serves the same increasingly stale snapshot with only a log line.
  No user-facing escalation. This is the most likely way the tool silently goes wrong.
- Acceptance ratings have no feedback loop; they're a reasoned heuristic with no
  calibration against trades that actually got accepted.
- No usage/role signal (snap share, target share) anywhere — every valuation is
  rank/consensus-based, so the tool is reactive to what the market already priced in.
- Multi-FLEX formats aren't fully modeled in positional-need detection. See the
  "Known limitations" section of `README.md` for the full list of deliberate gaps.

## Decisions that constrain future work

- League settings are **always** read from the Sleeper API, never hardcoded. `config.py`'s
  `qb_format` is informational only.
- Acceptance ratings stay bucketed (Very Low → High). Converting them to a percentage
  would imply calibration that doesn't exist.
- The test suite is fully synthetic and offline. Keep it that way — sub-second runs are
  what make it worth running on every change.
- `trade_engine.py` was not split, because a pure refactor carries regression risk with no
  user-facing benefit. That was conditioned on the file not growing further — it since has
  (~1150 → ~1950 lines), so this decision is now due for a deliberate re-ruling rather than
  automatic renewal.

## Next actions

1. **Put a ceiling on the stale-cache fallback** in `sleeper_tool/rankings/cache.py`. Beyond
   N days, surface it in the report itself rather than only a log line. Small, and it
   closes the quietest failure mode in the system.
2. **Split the presentation layer** out of `trade_engine.py` before adding more fields to
   `TradeProposal`.
3. **Investigate a usage/role data source** (`nfl_data_py` / nflverse are free and
   unauthenticated) as a supplementary buy-low trigger. Biggest capability gap.
4. **Acceptance-rating feedback signal** — even a manual "did they accept?" note per
   proposal starts building calibration data.

## Gotchas

- Three Pythons on PATH. Always `.venv/Scripts/python.exe`, never bare `python`.
- `data/yahoo_token.json` is a real credential. Don't read, print, or commit it.
- Don't scrape KTC/FantasyPros/RotoBaller during development — use the cache. They're free
  sources that can rate-limit.
- The repo path has a space in it. Quote it in every shell command.
- Pushing to `main` changes what the 9am ET automated run publishes. Treat push as a
  deploy, not a save.
