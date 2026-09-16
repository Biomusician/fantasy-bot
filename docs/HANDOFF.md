# Handoff — as of 2026-09-15 (redraft waiver command center)

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

**Nothing from this tranche has been pushed.** `origin/main` is at `fdf6ad8` (the
2026-09-04 night build, already published); the commits of this tranche sit on local
`main` ahead of it, starting at `1f5f67f`. Pushing is Jonathan's decision; the 9am ET
automated run publishes whatever `origin/main` holds, so treat the push as a deploy.

Tests: **1763 passed, 1 skipped, 7 xfailed in ~12s**, fully synthetic and network-free
(1140 at the start of this tranche). Report generation is ~8s, the dashboard ~7s — both
unchanged from before the tranche.

## What this tranche did

Tuesday and Wednesday now open with a **Waiver Command Center** per redraft/keeper
league: the exact claims to submit, in order, each with a drop, a FAAB window, a
recommended bid, and the fallback chain if it fails. Four questions, four modules
(`waiver_acquisition`, `waiver_drops`, `faab_window`, `waiver_plan`), assembled by
`waiver_command_center`. Dynasty leagues are untouched and still use `waiver_engine`.

Three new outside sources, each keeping its own identity and units — the manual Fantasy
Footballers CSV (`data/manual/`, see README), RotoBaller's weekly waiver board, and
Justin Boone's weekly ranks as an optional cross-check. Nothing is averaged into a
score. `docs/DECISIONS.md` (2026-09-15) carries the reasoning.

### The late findings, which changed real output

A six-persona red team ran against real data. The consequential ones, all fixed:

- **Depth was assumed, not measured.** FLEX demand was split evenly RB/WR/TE; these
  leagues' own optimized lineups fill flex slots RB 35% / WR 57% / TE 6%. League size
  was taken as the team count; it is really how many players are off the wire (8-team
  Primo plays like 10.4, 12-team Disco like 16.3). `league_depth.py` and
  `valuation.FLEX_DEMAND_SHARE` are the fixes.
- **Sleeper's `position` is not always a fantasy position.** Travis Hunter is "DB";
  124 players were dropped out of the ranking-source name index and the free-agent
  universe. `sleeper_positions.py` now answers that question once.
- **The class ladder had no FantasyPros/Boone rung**, so a player those lists rank as a
  starter in this format, who is not on this week's waiver board, was invisible.
- **Two league settings were hardcoded**: IR eligibility (`reserve_allow_*` — Disco has
  IR slots and forbids Out) and the claim-week lineup, which was not excluding players
  ruled Out.
- **The FAAB window raised the bid when sources disagreed.** It now widens the band
  without moving the recommendation. See DECISIONS for the other window rules.
- **Fallback claims were priced at $0** and speculative adds all shared one problem key,
  so only one could ever clear.

## Must QC before pushing

1. **The four command centers** in `data/weekly_report.md` — Primo Veterans, This
   League Sucks, Disco, The Surfeit. For each: is the top claim one you would actually
   submit, is the drop one you would actually cut, and is the bid in the right
   neighbourhood? These are the tranche's whole point.
2. **Every drop.** The board never offers an optimized starter, a claim-week starter, a
   trade piece, or (where the league's settings allow IR) an IR-eligible player. Confirm
   the four it does offer in each league read as your four most expendable.
3. **Claim counts run 4-7 a league** (Primo the highest: an 8-team keeper league with a
   deep wire). If that reads as too many, the dials are `MAX_GROUPS`, `MAX_BACKUPS`,
   `MAX_SPECULATIVE_GROUPS` and `MAX_SPECULATIVE_BACKUPS` in `waiver_plan.py`.
4. **The Ballers CSV path.** `data/manual/` is gitignored except its README. With no CSV
   for the claim week that source is simply absent and the plan still builds; confirm
   that is what happens when you have not yet exported one.
5. Run `scripts/daily_run.py` once, confirm 9/9 leagues complete, then rerun and confirm
   the second run adds no ledger entries (same-day idempotence).
6. **`data/calibration_report.md`** now carries a waiver-bid section. It is a diagnostic
   with a `MIN_SAMPLE` guard and never auto-tunes anything.

## Known gaps left deliberately

- **Scarcity anchors on the worst starter league-wide**, which inverts for WR in a
  many-FLEX league: Disco's four FLEX slots push 53 WRs into lineups, so the anchor is
  the 53rd-best WR and the wire reads "Abundant" at a gap of 0.0%. That label then
  suppresses a WR FAAB shift, blocks the cover path to `FLEX_DEPTH`, and disqualifies WR
  from contender insurance entirely — in the league where insurance matters most.
  Technically true, decision-useless. Fixing it means reworking `replacement_value`'s
  anchor, which the trade engine also reads, so it was out of scope for a waiver
  tranche.
- **K and DEF are not command-center candidates.** `streamer_planner.py` owns that
  decision; the exclusion is now stated in `waiver_acquisition`'s docstring rather than
  implied. A reviewer found LAC DEF would be a ~+0.65/wk free upgrade in The Surfeit, so
  there is real value on the table here.
- **Priority-waiver leagues are built but not live.** The path exists and is used the
  moment a league reports a waiver order; the Yahoo league is still blocked on Yahoo API
  access, and no priority position is ever guessed.
- **`PER_POSITION_BY_PROJECTION = 6`** does not scale with the league's starting demand
  at that position, so in a deep league the 7th-best free agent at a position can go
  unevaluated.
- The 14-per-team depth baseline and the flex shares are rules of thumb from one
  measurement of nine leagues in one season, not per-league figures re-derived at
  runtime.

## Where things are

`CLAUDE.md` has the module map and the conventions. `docs/DECISIONS.md` has the
reasoning, newest first. `README.md` has the Tuesday CSV workflow (§ Setup), what each
source is for, and the full known-limitations list.
