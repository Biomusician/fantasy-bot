# Handoff — as of 2026-08-19

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

Working and in sync with `origin/main`. The trade and waiver recommendation engines were
overhauled and then hardened in a second review pass (commits `475bf2f`, `16b8ac7`), and
the full findings are written up in `AUTONOMOUS_IMPROVEMENT_REPORT.md` — read that before
touching `trade_engine.py`, since it records which approximations are deliberate.

An unattended cloud routine runs `scripts/daily_run.py` at 13:00 UTC (9am ET) and
publishes the dashboard, so anything pushed to `main` goes live on the next run. Verified
2026-08-19: 184 tests pass in ~0.6s.

Committed in `349ac91`: this `docs/` directory, a project `CLAUDE.md`, and a narrowed
`.gitignore` (was blanket-ignoring `.claude/`, now only ignores local session state so
shared project config can be committed). Not yet pushed to `origin/main`.

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
- `sleeper_tool/trade_engine.py` — 1273 lines. Candidate selection, opponent-fit scoring,
  acceptance rating, and message generation all live here. The hard logic in the project.
- `sleeper_tool/waiver_engine.py` — trending-add targeting, drop candidates, FAAB.
- `sleeper_tool/report_data.py` — shared derived-data layer. New computed fields belong
  here, not in `report.py` or `html_report.py`.
- `sleeper_tool/storage.py` — SQLite cache of everything fetched.
- `AUTONOMOUS_IMPROVEMENT_REPORT.md` — rationale for the current engine design.

## In flight

Nothing half-implemented. The working tree changes listed above are configuration only and
don't touch application code.

## Known problems

- `trade_engine.py` mixes scoring, orchestration, and presentation in one file. The
  presentation layer (rationale + message templating) is the safe thing to split out; it
  carries no scoring risk. Deferred deliberately, not forgotten.
- "Rosterable" is still a pool-wide percentile at a few call sites (`identify_buy_low`'s
  eligibility filter, `identify_depth_needs`) rather than the within-position percentile
  `identify_needs` uses. Same class of bias that was already fixed elsewhere. 3–4 sites.
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
- The test suite is fully synthetic and offline. Keep it that way — it's what makes it
  usable at 0.15s.
- `trade_engine.py` was not split, because a pure refactor carries regression risk with no
  user-facing benefit. That tradeoff still holds until the file grows further.

## Next actions

1. **Put a ceiling on the stale-cache fallback** in `sleeper_tool/rankings/cache.py`. Beyond
   N days, surface it in the report itself rather than only a log line. Small, and it
   closes the quietest failure mode in the system.
2. **Thread within-position percentile** through the remaining `MIN_ROSTERABLE_PERCENTILE`
   call sites in `trade_engine.py`.
3. **Split the presentation layer** out of `trade_engine.py` before adding more fields to
   `TradeProposal`.
4. **Investigate a usage/role data source** (`nfl_data_py` / nflverse are free and
   unauthenticated) as a supplementary buy-low trigger. Biggest capability gap.
5. **Acceptance-rating feedback signal** — even a manual "did they accept?" note per
   proposal starts building calibration data.

## Gotchas

- Three Pythons on PATH. Always `.venv/Scripts/python.exe`, never bare `python`.
- `data/yahoo_token.json` is a real credential. Don't read, print, or commit it.
- Don't scrape KTC/FantasyPros/RotoBaller during development — use the cache. They're free
  sources that can rate-limit.
- The repo path has a space in it. Quote it in every shell command.
- Pushing to `main` changes what the 9am ET automated run publishes. Treat push as a
  deploy, not a save.
