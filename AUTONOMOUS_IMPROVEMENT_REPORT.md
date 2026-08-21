# Autonomous Improvement Report — Trade/Waiver Recommendation Overhaul

**Date:** 2026-08-19 to 2026-08-20 | **Session type:** Autonomous multi-agent engineering pass | **Commits:** `475bf2f`, `16b8ac7`, `48173c5`, `d4952de`, `8ef39bd`, `93be182`, `91fcb02`, `c56a2c9`

## Executive Summary

You asked for the tool to get materially better at identifying realistic trades, judging whether the other manager would actually accept them, explaining adds/drops, and generating something you could actually send. The trade and waiver engines have been rebuilt around one idea: **a numerically balanced offer is not the same thing as a realistic one.** The previous engine matched value and stopped there. It now checks whether the *receiving* team's roster has any actual use for what it would get, ranks every viable trade across every opponent before picking the best ones (instead of taking whichever matched first), buckets a real acceptance likelihood (Very Low → High) with plain-language reasons, rates its own confidence in the underlying valuations separately from that, and writes a short, non-templated message you can paste into league chat. A previously-dead code path ("sell before regression") is now live. Waiver adds come with a specific drop, a priority tier, a horizon, and — where the league tracks it — a FAAB suggestion. The dashboard leads with a cross-league "best moves right now" list instead of requiring you to click through 10 leagues to find anything urgent.

This was done in three rounds. The first pass (8 independent review agents → synthesis → implementation) built the above. A second pass (5 *fresh* reviewers, briefed to find what was still wrong) caught the first pass's headline feature — "does the recipient's roster actually need this" — being computed correctly but never wired in (hardcoded `True` at the call site), plus 20+ other findings. After both passes shipped, you gave direct feedback: injury alerts were noise (you only care about a player on IR who isn't actually slotted into an IR spot), trade messages listed everyone's characteristics instead of explaining the one thing that mattered to the recipient, and you wanted proactive drop recommendations based on upside/depth/dynasty rank. Round 3 rebuilt all three, then a *third* independent review pass (4 fresh reviewers) on that new code caught a recurring bug class (a give-piece could still prop up the comparison used to justify trading it away — the same bug fixed on the sell-high side in round 2 turned out to also exist on the buy-low side) plus a live data bug (a source's positional rank can exceed the locally-counted pool size, producing a negative percentile that rendered as "-28nd percentile" in the actual generated report).

**148 tests pass** (was 59 at the start of this session). The full production pipeline (`scripts/daily_run.py`) ran clean against all 10 of your real leagues after every round, and specific fixes were spot-checked against the real symptoms that surfaced them — including the negative-percentile text and the drop-candidates section rendering correctly in both the Markdown report and the HTML dashboard.

## Major Changes

### Trade engine (`sleeper_tool/trade_engine.py`)
- **Opponent-fit checking.** Every offer is checked against the *recipient's* roster: does what they'd receive beat their weakest currently-rosterable player at that position, or fill a position they have none at? An offer that's roster clutter for them is never proposed. When the smallest value-matching combination happens to include a useless piece, the engine now retries excluding just that piece to look for an all-fit alternative before falling back.
- **Best-fit, not first-fit.** All viable (opponent, target, offer) combinations across every opponent are collected and scored before picking the top N — previously it stopped at the first opponent that produced any match, in whatever order Sleeper happened to return rosters.
- **Acceptance rating + reasons.** A bucketed Very Low → High rating (not a fabricated precise probability) built from value closeness, whether the ask targets an active starter, whether it matches the opponent's contender/rebuild timeline, offer fragmentation, and their documented trading tendency — with the specific reasons shown, not just the bucket.
- **Confidence, separate from acceptance.** Rolls up existing but previously-buried signals (source corroboration, cross-source disagreement, thin-market/panel-disagreement caveats) into one Low/Medium/High field per proposal.
- **Sell-high proposals are live.** `identify_sell_high` existed and was fully documented as core to the tool but was never called by anything. It's now wired into a real "shop my rising asset to whoever needs it" pass, with the same cornerstone-asset protection as the buy-low side (see Bugs Fixed — this needed a follow-up fix after the first smoke test).
- **A message you'd actually send.** Short, casual, varies by trade shape and by the actual players/opponent involved — deliberately avoids "according to my projections" phrasing.
- **Untouchable-asset protection reworked.** Previously ranked a roster's "don't touch these" set by raw dollar value across the whole roster, which could (a) treat a non-starting backup as protected just because of raw value, while missing (b) a scarce position's actual best starter, and (c) worked correctly only in dynasty leagues. Now ranks by percentile among starters, protects each position's clear-best asset in both currencies, and protects any starter whose loss would drop the roster below what the league's own lineup requires there.
- **Depth-need signal.** A position where the single best player is strong but there's no real depth behind them (e.g., an elite RB1 with nothing rosterable at RB2) now surfaces as a need on its own, using exact roster-slot counts read from the league's actual `roster_positions` (with FLEX/SUPER_FLEX demand distributed across eligible positions).
- **Redraft-league QB-value bias removed.** Several ranking/matching steps compared players by raw projected points, which run structurally higher for QBs than skill positions — fixed to use each source's own percentile instead, which is already normalized.

### Waiver engine (`sleeper_tool/waiver_engine.py`)
- Every add is paired with a specific drop candidate (same-position bench player preferred, deduplicated across the whole table so two "Add" rows don't both tell you to cut the same guy), a priority tier (Must Add → Monitor), a horizon (Breakout / Season Starter / Stash / Streamer), and a FAAB bid suggestion where the league actually tracks a budget.
- Injury/bye alerts carry a structured severity (derived from the actual injury status, not by pattern-matching the sentence).

### Dashboard & report (`sleeper_tool/html_report.py`, `report.py`, `report_data.py`)
- New cross-league "Best moves right now" section, ranked by actual quality (not by which league happened to be processed first).
- Trade cards show acceptance/confidence chips and a ready-to-send message; the value-balance badge and trade-type label are now computed once and shared by both renderers instead of being independently (and differently) re-derived.
- A league's Time-sensitive section moves to the top of its panel when there's a high-severity alert.

### Reliability
- Two proven crash bugs fixed: a league with an explicitly-null `scoring_settings`/`roster_positions` field crashed the whole valuation step; one bad league's data used to be able to abort the *entire* unattended daily report for all 10 leagues (now isolated per-league). A ranking-source outage now falls back to a stale cache instead of aborting the run.

## Recommendation Methodology (current)

**Trade proposals** — three generation passes, each opponent-fit checked and globally ranked before selection:
1. *Buy-low*: a corroborated, trending-down, still-rosterable opponent asset at one of my needs, matched against my tradeable pool.
2. *Sell-high*: my corroborated, trending-up, non-cornerstone asset, shopped to whichever opponent has a real need there.
3. *Pick-target*: rebuild-only, asking for a draft pick instead of a player.

Every proposal carries: `trade_type`, `acceptance_rating` + `acceptance_reasons`, `confidence`, `balance_label` (value closeness), and `message`. None of these are precise probabilities — they're bucketed, reasoned judgments built from data the tool already has (both rosters, team-status classification, owner-trading-tendency notes), explicitly not a claim of statistical calibration.

**Waivers** — `priority_tier` from need-fit + percentile + trending-rank; `horizon` from age/trend/currency; `drop_candidate` from the weakest same-position (or non-need) bench player, deduplicated across the table.

## Agent Findings (most consequential)

*First-pass review (8 agents, 72 findings) — the pattern that mattered most:* four of eight independent reviewers, from different angles (analyst, negotiation, skeptical opponent, red-team), converged on the same root cause: **nothing checked whether the receiving team's roster had any actual use for what it was being sent.** That became the centerpiece fix.

*Second-pass review (5 fresh reviewers, 34 findings) — same pattern, one level deeper:* the fix above was implemented correctly as a standalone function, but the field meant to carry its result into the acceptance score (`would_upgrade_their_roster`) was hardcoded `True` at every call site — so the single largest scoring penalty could never actually fire, and the feature was dead in production despite passing its own unit tests (which constructed the `False` case by hand rather than exercising it end-to-end). This is the clearest lesson from the session: **unit-testing a function in isolation doesn't catch a wiring bug at the call site** — the integration tests added in response (`test_generate_trade_proposals_never_gives_away_the_same_player_twice`, the collision-retry test, etc.) exist specifically to close that gap.

## Round 3 — Your Feedback + Third-Pass Review

You flagged three specific problems after living with the first two passes:

1. **"Injuries as alerts is wrong. I only care if I have a player on IR who is not in an IR spot."** `get_time_sensitive_notes` previously flagged any non-Healthy `injury_status`, including day-to-day designations that don't require action. Rewrote it to fire only when a player is functionally long-term out (IR/PUP/NFI/Suspended) *and* not already sitting in the roster's actual reserve slot — the one situation where you're carrying dead weight in an active spot and would want to know.
2. **"The messages need a lot of work... don't need to mention everyone's characteristics... should explain exactly how the trade is helping."** `_benefit_reason` was rewritten to name the *one* specific thing about the recipient's roster the trade addresses ("since he'd start over what you've got at TE now" / "for real QB depth behind your starter") instead of restating value/trend/age for every piece. `generate_trade_message` uses that single reason as the message's closer.
3. **"Consider recommending drops from my team based on upside, relative position depth, or dynasty ranking."** New `identify_drop_candidates`: flags bench players who are simultaneously low-percentile-for-position, buried behind more rosterable options than the lineup needs, or (dynasty only) aging with no upside on a non-contending roster. Cross-referenced against live trade proposals so the same player is never told to both "trade away" and "drop for nothing" in one report.

A fourth, independent review pass (4 fresh reviewers on the round-3 diff) then caught:

- **The buy-low path had the same self-reference bug already fixed on sell-high in round 2**, just not in the spot that round's fix touched: a give-piece could still be counted as its own comparison bar when justifying why it should be traded away. I'd explicitly reasoned this path was safe before spawning the review — I was wrong, and said so at the time. Fixed at the root by threading `exclude_player_id` through the shared fit-checking primitives (`_weakest_rosterable_percentile`, `_piece_fits`, `_recipient_need_fit`, `_find_fitting_offer`) instead of patching each call site separately.
- **A live "-28nd percentile" bug in the actual generated report.** Root cause: a ranking source's own positional rank can exceed the pool size counted locally from the cached snapshot, producing a negative percentile with no clamp anywhere in the chain. Fixed at the source (`ValuationEngine._percentile`) and defensively at the formatter (`ordinal_pct`).
- `identify_drop_candidates` counted taxi-squad/reserve players as "better options at the position" (they can't take a bench spot's job), truncated fractional `starter_slots` instead of respecting them, and its caveat text could run on past ~2 clauses in an edge case.
- The dashboard color-coded "Strong Drop" as its `negative` (red) tier when the file's own documented convention reserves that for something worse than a plain drop suggestion — should be `caution` (amber), matching "Consider Dropping".

## Round 4 — Waiver Message Parity, Live-Data Priority-List Gap, Acceptance Calibration

After round 3 shipped, I did a lighter self-directed pass rather than another full 8-agent review: a browser check of the actual rendered dashboard (not just grepping the Markdown) surfaced one issue directly, and a smaller 3-agent Workflow targeted specifically at the code least reviewed so far (waiver_engine's own reasoning, edge cases in the newest alert/drop-candidate logic, and acceptance-rating calibration read against real generated proposals) found the rest.

**Found by direct inspection:** with 8+ trades rated Good/High-confidence this week, the cross-league "Best moves right now" list — which sorts alerts, then trades, then waivers, then drop-cleanup — filled all 8 slots with trades alone, leaving zero visibility for a Must-Add waiver or a Strong-Drop candidate even though every league had at least one. Fixed by reserving a floor of 2 slots each for the waiver and roster-cleanup kinds before filling the remaining budget by overall quality.

**Found by the targeted review (8 findings, all confirmed against live data):**
- `_LONG_TERM_INJURY_STATUSES` checked for `"Suspended"`/`"NFI"` — strings that never appear in Sleeper's real `injury_status` field (confirmed by querying `data/sleeper.sqlite3` directly: real values are `Sus`/`IR`/`PUP`/etc.; `"NFI"` only ever shows up in the separate `status` field as `"Non Football Injury"`, which the function never read). Two of the alert's four designations could never fire. Now checks both fields against their real values.
- The same alert fired for taxi-squad stashes, wrongly claiming they occupy "an active roster spot" — a taxi slot doesn't compete for bench room any more than a reserve slot does. Excluded.
- `rate_acceptance` never had a branch for `trades_often == "infrequent"` (6 of 10 documented owners) — it silently scored as neutral despite the same proposal's own framing text warning "doesn't complete trades often." Confirmed live: two Good/High-rated offers to documented infrequent traders (`dlooney1`, `Bazinga9`) were inflated by this gap; one dropped from High to Good once fixed.
- `rate_acceptance` also never consulted `youth_vs_veteran`, even though the framing hints shown in the same report block do — an offer could contradict a documented preference with no scoring penalty. Added. Also made the value-closeness penalty direction-aware: a generous overpay was scored identically to a lowball, when only lowballing should ever reduce acceptance likelihood.
- Waiver reasons only got a concrete roster comparison ("would beat your current starting RB, X, 34th percentile") when the add filled a declared need — every other row was pure trend-count boilerplate, the exact generic-vs-concrete gap round 3 fixed for trade messages but hadn't carried over to waivers. Now computed unconditionally.
- Waiver drop-candidate dedup was resolved while iterating Sleeper's trending-feed (add-count) order, before the final priority-tier sort — a lower-priority target could claim the one available same-position bench cut ahead of a higher-priority target at the same position. Reassigned after sorting into final display order instead.
- The percentile in a waiver reason silently switched between within-position and pool-wide with no visible label. Now qualified with "within-position" to match `identify_drop_candidates`' existing convention.

## Bugs Fixed

- `derive_league_format` crashed on an explicit `null` `scoring_settings`/`roster_positions` (proven, not hypothetical).
- One league's malformed data could abort the entire daily report for all 10 leagues; a ranking-source fetch failure had no stale-cache fallback.
- `identify_sell_high`, wired live in this session, initially had no protection for cornerstone assets — the first smoke test against real data caught it proposing to trade away the user's actual RB1 the moment he trended up. Fixed same-day, before this report was written.
- `_untouchable_ids`'s protection only worked for dynasty currency and was itself QB-value-biased in redraft leagues (caught in the second-pass review, confirmed empirically).
- The same roster player could be proposed away in two different proposals in one report (Pass 3 didn't exclude assets Passes 1–2 already committed).
- Two opponents' best-fit offers landing on the identical give-piece silently dropped the second opponent instead of retrying against the remainder of the pool.
- Waiver `_find_drop_candidate` recommended the same bench player as the drop for multiple simultaneous "Add" rows; separately, it excluded a target's own position from consideration whenever that position was a declared need — exactly the common case.
- Waiver `fills_need` matched 3 of 4 possible positions (nearly meaningless) instead of the top-2 `trade_engine` itself uses.
- A Must-Add redraft starter was tagged "Streamer" (a horizon meant for this-week-only churn adds).
- The Markdown waiver table's "Add" column header actually rendered the player's NFL team.
- `OpponentFit.would_upgrade_their_roster` hardcoded `True` (see Agent Findings above) — the highest-value fix of the second pass.

## Tests Added/Run

- **157 tests passing** (baseline was 59; 121 after round 2, 148 after round 3). New files: `tests/test_waiver_engine.py`, `tests/test_report_data.py`, `tests/test_rankings_cache.py`. Substantial additions to `tests/test_trade_engine.py`, `tests/test_valuation.py`, and `tests/test_waiver_engine.py` in every round.
- Every fix above has a dedicated regression test, several built directly from the reviewers' empirical repro scripts (e.g. the FLEX-distribution test, the give-piece-collision test, the cross-league quality-ranking test, the buy-low self-reference integration test, the drop-candidate taxi/reserve/fractional-slot tests, the infrequent-trader/youth-veteran/direction-aware acceptance tests, the taxi-squad-alert and Sus/NFI-status regression tests).
- `scripts/daily_run.py` (the actual production entry point) was run end-to-end against all 10 real leagues after every round, and specific fixes were spot-checked against the exact real-data symptom that surfaced them (Dak Prescott no longer appears as a sell-high candidate in the Superflex league where he's the only real starting QB; no waiver row repeats another row's drop suggestion; no negative-percentile text anywhere in the regenerated report; the drop-candidates section renders correctly in both outputs; the cross-league priority list now shows a waiver add and two drop candidates instead of eight trades and nothing else; `dlooney1`'s previously-High-rated offer now correctly rates Good).
- Full suite: `.venv/Scripts/python.exe -m pytest tests/ -v`

## Remaining Weaknesses

Deliberately not addressed this session — flagged, not fixed, either because the fix was large/risky relative to its benefit or because it needs a product decision:

- **`trade_engine.py` is now ~1,450+ lines** mixing candidate selection, opponent-fit scoring, rationale/message generation, and orchestration — grew further in round 3 (drop candidates, message personalization). The second-pass architecture reviewer recommended splitting the presentation layer (rationale + message templating, which has zero scoring-logic risk) into its own module. Deferred per your explicit priority ("prioritize recommendation intelligence over refactors") — the strongest candidate for a refactor before the file grows again.
- **`waiver_engine.py` keeps its own separate copy of `_roster_impact_note`**, not shared with `trade_engine.py`'s, because the two need different phrasing (inline clause vs. standalone sentence). Flagged LOW severity by round-3 reviewers; judged not worth a forced unification that would compromise either call site's phrasing.
- **"Rosterable" still means pool-wide percentile at a few call sites** (`identify_buy_low`'s eligibility filter, `identify_depth_needs`), not the within-position percentile `identify_needs` itself uses — a smaller residual version of the bias already fixed in `_untouchable_ids`'s ranking. Medium effort, touches 3-4 call sites.
- **`get_or_fetch`'s stale-cache fallback has no ceiling.** If a ranking source breaks permanently, every run will keep serving the same, increasingly stale snapshot forever with only a log line — no user-facing escalation.
- **Acceptance ratings have no feedback loop.** They're a reasoned heuristic, not a calibrated model — there's no mechanism yet that checks a predicted rating against which trades actually got accepted.
- **No underlying usage/role signal** (snap share, target share) — every valuation is still rank/consensus-based, so the tool is reactive to what the market has already priced in, never predictive of a role change before it shows up in next week's rankings. This was flagged first-pass as the single biggest gap versus what a sharp human manager uses to time buy-low windows; closing it needs a new data source (Sleeper doesn't expose it), not just refactoring.

## Recommended Next Steps (ranked)

1. **Add an acceptance-rating feedback signal** — even a manual "did they accept?" note per proposal would start building the calibration data the ratings currently lack.
2. **Split `trade_engine.py`'s presentation layer** into its own module before adding more fields to `TradeProposal`.
3. **Finish threading within-position percentile** through the remaining pool-wide `MIN_ROSTERABLE_PERCENTILE` call sites.
4. **Investigate a lightweight usage/role signal** (e.g., `nfl_data_py`/`nflverse`, free and unauthenticated) as a supplementary buy-low trigger alongside RotoBaller's trend label.
5. **Trade-block / "managers you should call" ranking** and **likely-counteroffer suggestions** — both explicitly scoped as optional in the original brief and not attempted this session, now that the core acceptance-modeling infrastructure exists to support them cheaply.

## Assumptions Made

- Pushed the finished work to GitHub (`git push` to `main`) without asking mid-session, since the daily cloud routine only picks up what's on GitHub — leaving the fix un-pushed would mean the automated dashboard keeps running the old, unreviewed engine. Documented here rather than asked, per your explicit "don't stop to ask me routine questions" instruction.
- Raised `MIN_ROSTERABLE_PERCENTILE` from 20 → 45 and `waiver_engine`'s `fills_need` threshold from "top-3-of-4 positions" → "top-2" — both are judgment calls about where to draw a heuristic line, reasoned from the codebase's own stated design intent (a rank-401-of-500 player isn't realistically rostered in a 10-12 team league; `trade_engine` itself already treats "need" as top-2) rather than from a number you specified.
- FAAB suggestions are scoped to leagues where Sleeper actually exposes a `waiver_budget` setting; every other league gets `None` rather than a guessed number.
- Did not attempt the `trade_engine.py` file split (see Remaining Weaknesses) — judged as a pure refactor with real regression risk and no user-facing benefit, which your brief explicitly said to deprioritize relative to recommendation intelligence.

## Files Changed

`sleeper_tool/trade_engine.py` (core rewrite), `sleeper_tool/waiver_engine.py` (core rewrite), `sleeper_tool/report_data.py`, `sleeper_tool/html_report.py`, `sleeper_tool/report.py`, `sleeper_tool/valuation.py`, `sleeper_tool/roster_analysis.py`, `sleeper_tool/team_status.py`, `sleeper_tool/rankings/cache.py`, `sleeper_tool/formatting.py`, `README.md`, plus `tests/test_trade_engine.py`, `tests/test_valuation.py`, and three new test files.

## How to Test It

```bash
# Full test suite
.venv/Scripts/python.exe -m pytest tests/ -v

# Regenerate the actual report/dashboard against your real leagues
.venv/Scripts/python.exe scripts/daily_run.py
# -> data/weekly_report.md and data/dashboard.html

# Open the dashboard locally to look at the new "Best moves right now"
# section, trade acceptance/confidence chips, and waiver priority/drop/
# horizon columns
```

The unattended daily routine (13:00 UTC / 9am ET) will pick up all of this automatically on its next run, since the changes are pushed to `main`.
