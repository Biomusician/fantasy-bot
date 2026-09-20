# Handoff — as of 2026-09-19 (dynasty source health repair)

To resume cold, start a session with:
`Read CLAUDE.md and docs/HANDOFF.md, verify the current state, and continue from the
highest-priority unfinished task.`

Regenerate this file with `/handoff`.

## Status

**Pushed 2026-09-19.** `origin/main` is at `7049f44` — the 2026-09-15 waiver tranche and
the 2026-09-19 source-health repair, 30 commits, fast-forwarded from `fdf6ad8`. The
working tree is clean and level with the remote.

### The dashboard problem was deployment drift, not KTC

KTC moved its player data and now writes
`var playersArray = JSON.parse(document.getElementById('ktc-players').textContent)`, so a
parser matching only the old `var playersArray = [...]` literal finds nothing on a
perfectly healthy page. That was fixed on 2026-09-15 in `1f5f67f` and then sat unpushed
for four days while the 9am ET run kept publishing `origin/main`. Local debugging looked
healthy the whole time, because locally the fix was already in place — which is exactly
what made it hard to see.

Verified against one live fetch: the old parser matched nothing; the shipped parser
returns 500 rows and validates. **Confirm at the next 9am run that KTC reads Fresh with
~500 rows and the degradation banner is gone.** If it does not, start with
`scripts/source_health.py --source ktc --live`.

The lesson worth keeping: a fix that is not deployed is not a fix, and nothing in the
repo made the gap visible.

Tests: **1809 passed, 1 skipped, 7 xfailed in ~8s**, fully synthetic and network-free.
`scripts/daily_run.py` is ~34s end to end (9/9 leagues); the report ~8s, the dashboard ~7s.

## Diagnosing a source break

    .venv/Scripts/python.exe scripts/source_health.py --source ktc          # cache only
    .venv/Scripts/python.exe scripts/source_health.py --source ktc --live   # one request

It reports each stage separately — cache path, what is on disk, whether it loads, age and
row count, the health layer's label AND state, and under `--live` which parser strategy
matched, how many rows it yielded and whether that would be cached. It never writes the
cache, including under `--live`.

## What the 2026-09-15 waiver tranche did

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

## What the 2026-09-19 source-health repair did

- `rankings/ktc_parser.py` (new) holds the page strategies, tried in order and each named
  in the result: the current embedded JSON, the older `playersArray` literal, and the
  rendered rows. The rendered rows are marked INCOMPLETE and can never be cached — the
  page shows 50 of 500 players with one format's value and no TE-premium variants, and a
  partial board is worse than no board because only one of them is legible as a failure.
- Validation now sits before the cache write: row count against the shared coverage
  floor, all four positions, no nameless rows, no mostly-duplicate board, values inside
  KTC's 0-9999 scale, not all zeroes. A challenge page is recognised as one.
- `get_or_fetch` takes a `validate` callback and refuses to replace a good snapshot with
  a payload that fails it — the previous board is kept and served, the outcome is
  "rejected", and the validator's own reason is recorded. Writes are atomic.
- Each `SignalHealth` carries a `state` next to its label, and the reason travels with
  it. FF Dynasty Pass is NOT_CONFIGURED rather than Unavailable.
- `FEATURE_REQUIREMENTS` carries a mode (all / any / any-two). `source_disagreement` was
  split by currency, because a redraft league compares FantasyPros against RotoBaller and
  never reads KTC — one global rule was silencing it in four leagues it has no part in.
- The banner names the capability lost and what still works.

## Next tasks, in order (Jonathan, 2026-09-19)

1. **KTC-independent dynasty degradation pass.** Make the drop, stash and buyer boards
   keep working from rank/context when KTC is absent instead of silently disappearing.
   `roster_assets.py` is the right place — see finding 1 below.
2. **Audit first-fetch validation across every ranking source.** The cache gate added on
   2026-09-19 protects data you already have and deliberately does not second-guess a
   first fetch, which means first-run garbage can still become the baseline unless the
   source's own parser rejects it. KTC is covered (its parser refuses a short board
   before the cache is reached); the others are not audited.

## Open findings from the 2026-09-19 red team (not fixed)

Three reviewers ran against the real cache. Everything dangerous or
dishonest was fixed; these are the capability gaps left, in priority order.

1. **The drop board, stash board and buyer board go silently empty in all
   five dynasty leagues when KTC is out** (measured: 3-5 rows each becomes
   0). All trade-side functions gate on `asset_value.corroborated`, which
   needs a number in the league's currency — the KTC market value. Trade
   proposals and the buyer board genuinely need cardinal value and are
   right to suppress. The **drop and stash boards only use it for
   ordering**, and now that `percentile_for_currency` falls back to
   FantasyPros dynasty ECR they could run on rank alone. The fix is a
   rank-based gate in `roster_assets.py`, not a change to `corroborated`
   (which would wrongly resurrect trade proposals on a rank-only basis).
   Those sections also vanish with no note; the banner and health section
   say dynasty values are limited, but the sections themselves are silent.

2. **`Reason.freshness` is computed on every provenance card and never
   rendered.** Both renderers drop it. Before it can be rendered,
   `signal_health.FAMILY_SOURCES` needs to be currency-aware: `trade_engine`
   is fed by all three ranking families, so a degraded KTC currently tags 20
   reason rows in the four redraft/keeper leagues, which never read KTC.
   The last-write-wins bug in that dict is fixed; the currency gap is not.

3. **13 of 17 `FEATURE_REQUIREMENTS` entries have no consumer.** Only
   `role_trends`, `waiver_engine` and the two `source_disagreement_*` flags
   are honoured. `dynasty_values` is the awkward one: the reader is told it
   was suppressed while the page still makes dynasty claims from FantasyPros
   — which is now correct behaviour, but the flag's name no longer describes
   it. Either give each flag a consumer or shrink the table to the ones that
   are honoured.

4. **`roster_clog` still says "reconciled dynasty rank" when only one source
   contributed**, and a single-source composite crossing the top-150 cutoff
   invents a new clog (Keenan Allen in That Other Dynasty League). It should
   name the source it actually used.

5. **`corroborated` (>=2 of KTC/FantasyPros/RotoBaller) is the real
   `ANY_TWO_OF` case and has no entry in `FEATURE_REQUIREMENTS`.**

Smaller, from the cache review: `nflverse_players` and
`dynastyprocess_playerids` have no `MIN_COVERAGE` entry so `signal_health`
never grades them; the memoized cache payload object is shared across
callers (no current mutation site, but nothing enforces read-only); and
`save_snapshot` strands a `.tmp` file on a hard kill, which nothing sweeps.

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
