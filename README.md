# Fantasy Bot

A weekly decision-support tool for dynasty, keeper, and redraft Sleeper
fantasy football leagues: pulls your rosters, cross-references three
independent ranking sources, and generates trade offers, waiver targets,
and injury/bye alerts — as both a Markdown report and an HTML dashboard.

Built for one specific set of leagues (see `sleeper_tool/config.py`), not
a generic multi-user product — the account, league list, and league-mate
notes are hardcoded, not configurable via a UI.

## What it does

- **Data layer**: pulls rosters, users, matchups, transactions, and traded
  draft picks from Sleeper's public API, cached locally in SQLite so
  re-runs don't refetch everything.
- **Valuation**: scrapes KeepTradeCut (dynasty trade values), FantasyPros
  (redraft/dynasty consensus ranks), and RotoBaller (point projections),
  and picks the right slice of each based on your *league's actual*
  scoring settings (1QB vs Superflex, PPR type, TE premium, 6pt passing,
  100yd rush bonus) — never assumed, always read from Sleeper directly.
  Also flags two known weaknesses of the underlying sources directly in
  trade rationale: wide disagreement within FantasyPros' own 100+-expert
  panel (not just vs. KTC), and KTC's crowd-vote liquidity thinning out
  hard outside the startup-relevant player pool (rank 150+).
- **Team status**: classifies each of your teams as a contender, middling,
  or a rebuild candidate, using starter *and* bench/taxi roster strength,
  owned future draft capital, the league's playoff format, and (once
  enough games are played) actual record.
- **Trade engine**: finds buy-low/sell-high candidates and matches them
  against your positional needs — but a numerically balanced offer isn't
  necessarily one the other manager would ever accept, so every proposal
  is also checked against *their* side: does the receiving roster have an
  actual hole at the position being sent, would it beat their existing
  depth there, does it fit their contender/rebuild timeline, and how does
  their historical trade activity factor in. That produces a bucketed
  **acceptance rating** (Very Low → High, not a fake precise probability),
  a separate **confidence** rating in the underlying valuations, and a
  short, casual **message you can actually send** the other manager.
  Proposals span three shapes: buy-low (their dip, your need), sell-high
  (your rising asset, shopped to whoever actually needs it), and
  pick-for-player asks for rebuilding teams. Owned future draft picks are
  real trade chips throughout, and a roster's true cornerstone starters
  (plus a scarce position's only real starter, e.g. a lone startable TE)
  are never offered or targeted regardless of trend.
- **Waiver engine**: cross-references trending adds against your roster
  gaps and pairs every add with a specific drop candidate, a priority tier
  (Must Add → Monitor), a horizon (breakout/stash/streamer), and a FAAB
  bid suggestion where the league tracks a budget — not just a bare name.
  Also flags injuries and starter byes, both on your own roster and on
  waiver targets themselves.
- **Reports**: one consolidated Markdown file, or a dark-mode HTML
  dashboard you can open locally or publish as a Claude Artifact. Both
  lead with a cross-league "best moves right now" summary — the tool's
  answer to "what should I actually do today" across all your leagues at
  once, not just a per-league data dump you have to click through to
  assemble yourself.
- **Decision layer** (each its own module, all built on one shared
  lineup optimizer that computes the best *legal* lineup for the league's
  real slot list — exact, not greedy, so Superflex and partial-flex slots
  are handled correctly):
  - *Lineup leverage*: every slot's closest bench alternative labelled
    Clear Start / Lean Start / Toss-Up, plus "bench surplus" — value
    trapped behind a slightly better starter, i.e. trade material that
    costs the lineup nothing.
  - *Move impact preview*: what a recommended trade or Must-Add waiver
    actually changes — lineup, weekly points, depth needs, team status,
    roster value, starter age — reported only when material, and
    explicitly "a value play, not a lineup play" when nothing is.
  - *Contender insurance*: which single starter injury would crater a
    contender's lineup, and the free agent who'd soften it, added to the
    waiver list as an Insurance row.
  - *Bye collision planner*: a four-week look-ahead for weeks the bench
    can't legally or adequately cover a bye, surfaced while a cheap
    waiver fix is still available.
  - *Roster clogs*: players with no path to the lineup, no market value,
    and no strategic use — the dead roster spots — preferred as the drop
    paired with a waiver add.
  - *Portfolio exposure*: how many of your rosters ride on the same
    player (and the same starting QB), as a tie-breaker and risk flag on
    trade/waiver recommendations, never a sell signal.
  - *League economy*: who actually trades, who's accumulating or selling
    picks, and who stockpiles a position — from this season's real
    transaction record, annotating the "why they say yes" side.
  - *Playoff leverage*: Comfortable / Bubble / Long Shot / Out from the
    standings and the league's actual playoff format, with a Deadline
    Window that promotes a bubble team's trades when the deadline is near.
  - *Pick opportunity cost* (dynasty): whether a 1st/2nd-round pick is
    Strategic, Useful, or Spendable for *this* roster — a replacement
    path for a weak, aging unit vs. pure market value.
  - *Negotiation ladder*: for the top buy-low trades, an opening (the
    cheapest package that still rates acceptable), a fallback after a
    counter, and a walk-away line — all rated with the engine's own
    acceptance rubric.
  - *Decision delta*: "since last run" — only what changed vs. the last
    complete daily run (statuses, recommendation lists, roster moves,
    15%+ value swings).
  - *Replacement market*: what each starting position is worth against
    THIS league's waiver wire — the best startable free agent vs the
    worst current starter league-wide — with Abundant / Normal / Scarce /
    Very Scarce derived from that gap (no "QB is scarce in Superflex"
    rule; the second QB slot produces it). Annotates trades, waivers,
    clogs, bench surplus and pick units; highlights players whose generic
    rank under- or overstates their edge here.
  - *Source disagreement*: whether KTC, FantasyPros and RotoBaller agree
    on a player, compared in within-position rank space (Strong /
    Normal Consensus, Source / High Disagreement, Market Above Projection
    / Projection Above Market), with the FantasyPros expert-panel spread
    when it's wide.
  - *Trade economics*: every trade gets two separate verdicts that are
    never blended — asset economics (Favorable / Roughly Even /
    Unfavorable from the engine's own balance) and roster economics
    (Improves Lineup / Mostly Neutral / Costs Lineup / Major Lineup Cost
    from the move preview). Opposite directions are a Strategic
    Tradeoff.
  - *Streaming planner*: for QB/TE/K/DEF, the best single player vs the
    best one-switch two-player sequence over the next three weeks, byes
    from the real NFL schedule (nflverse, cached daily), no opponent
    adjustment invented; the single plan wins within 8%.
  - *Market velocity*: Rising / Rapidly Rising / Falling / Rapidly
    Falling / Stable from up to 28 days of decision snapshots, on
    actionable players only; Insufficient History under three
    observations.
  - *Matchup leverage*: this week's projected gap vs your actual Sleeper
    opponent (Strong Edge → Large Deficit); recommendations say how their
    weekly gain relates to the gap.
  - *Opponent blocking*: at most one Defensive Add per league per week,
    only when the opponent has a real hole this week, the free agent is
    worth 4+ to their lineup, and the drop costs you nothing you value.
  - *Roster consolidation*: 2-for-1 offers for contenders and strong
    middling teams where the incoming player enters your optimized
    lineup by 3+ points/week, value-matched with the engine's numbers,
    fragility flagged. Never 3-for-1.
  - *Stash board* (dynasty/keeper): developmental free agents worth a
    roster spot — Priority Stash or Watch — never described as lineup
    help.
  - *Schedule windows*: next-3 / remaining / fantasy-playoff windows from
    the schedule and the league's own playoff settings; used only as a
    tiebreak between near-equal players or to note a bye in a window.
  - *Buyer board*: for each sell-high piece, the three counterparties
    most likely to pay (need, timeline, league economy, scarcity,
    fundability), feeding the sell-high proposals as annotations.
  - *Recommendation conflicts*: when the tool's own signals oppose each
    other on one move, it is labelled "Conflicted Move — Review
    Manually" with reasons for and against — never suppressed or
    re-scored.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

No API keys needed for the Sleeper/KTC/FantasyPros/RotoBaller data — all
four are scraped from public, unauthenticated endpoints.

### Optional: Fantasy Footballers Dynasty Pass

Their rankings are paywalled and can't be scraped. If you have a
subscription, export/paste rankings into `data/ff_dynasty_pass.csv`
(see `data/ff_dynasty_pass.example.csv` for the expected columns:
`player_name,position,rank,team,notes`). Files older than 7 days are
ignored automatically rather than silently used stale.

### Optional: Yahoo league

Requires a Yahoo Developer app + a one-time OAuth exchange —
see `scripts/yahoo_oauth_setup.py` for instructions. Currently blocked on
Yahoo's Fantasy Sports API access approval (a manual review process on
their end); not yet wired into the report pipeline.

## Running it daily

The tool is designed to be re-run once a day (an automated cloud routine
does exactly this — see below). One command does everything:

```bash
.venv/Scripts/python.exe scripts/daily_run.py
# -> syncs fresh Sleeper data, then writes data/weekly_report.md and
#    data/dashboard.html
```

Or run the three steps individually if you want more control:

```bash
# 1. Pull fresh Sleeper data (rosters, matchups, transactions, trending, traded picks)
.venv/Scripts/python.exe scripts/pull_data.py

# 2. Generate the Markdown report
.venv/Scripts/python.exe scripts/generate_report.py
# -> data/weekly_report.md

# 3. Generate the HTML dashboard
.venv/Scripts/python.exe scripts/generate_dashboard.py
# -> data/dashboard.html (open directly in a browser, or publish as a
#    Claude Artifact for a shareable link viewable on mobile)
```

Ranking sources (KTC/FantasyPros/RotoBaller) cache themselves for ~20
hours, so a daily cadence naturally gets a fresh scrape on (almost) every
run without hammering those sites. The Sleeper player dictionary (~5MB)
caches for ~20 hours too. `daily_run.py` always syncs Sleeper data first;
the standalone `generate_report.py`/`generate_dashboard.py` scripts read
from local SQLite and expect `pull_data.py` to have run first.

### Automated daily run

A scheduled cloud routine runs `daily_run.py` once a day and republishes
the dashboard to the same Claude Artifact link automatically — see it (and
its run history) at [claude.ai/code/routines](https://claude.ai/code/routines).
It clones this repo fresh each run, so nothing here needs to stay running
locally. The Markdown report isn't currently pushed anywhere automatically;
ask in a Claude Code session for the latest `data/weekly_report.md` if you
want it outside the dashboard.

## Project layout

```
sleeper_tool/
  client.py            Sleeper API wrapper
  storage.py            SQLite persistence
  sync.py                Orchestrates pulling + storing weekly data
  players_cache.py        Daily-refresh player dictionary cache
  config.py                League list + my Sleeper identity
  name_matching.py          Cross-source player name normalization
  rankings/
    ktc.py                   KeepTradeCut dynasty value scraper
    fantasypros.py            FantasyPros ECR scraper
    rotoballer.py               RotoBaller projections scraper
    ff_dynasty_pass.py           Manual CSV import (paywalled source)
    cache.py                      Generic fetch-date-aware ranking cache
  valuation.py           Format-aware per-player valuation engine
  roster_analysis.py      Joins Sleeper rosters + valuations
  owner_profiles.py        League-mate trading-tendency notes
  team_status.py            Contender/middling/rebuild classification
  trade_engine.py            Buy-low/sell-high + trade proposal generation
  waiver_engine.py             Trending-add waiver targeting + alerts
  draft_picks.py                 Traded-pick ownership + KTC pick valuation
  lineup_optimizer.py             Best legal lineup for the league's real slot list (shared)
  lineup_leverage.py               Start/sit closeness + bench surplus
  move_impact.py                    Post-move roster preview (material deltas only)
  contender_insurance.py             Fragile-starter detection + free-agent cover
  bye_collision.py                    Four-week bye look-ahead
  roster_clog.py                       Dead roster spots
  portfolio_exposure.py                 Cross-league player concentration
  league_economy.py                      Per-manager trade/pick/position tendencies
  playoff_leverage.py                     Standings vs the playoff cut + deadline window
  pick_opportunity.py                      Strategic/Useful/Spendable pick classification
  negotiation_ladder.py                     Opening / fallback / walk-away per top trade
  decision_delta.py                          "Since last run" snapshot diffing (28 daily files kept)
  replacement_value.py                        League-relative replacement levels + scarcity labels
  source_disagreement.py                       KTC/FantasyPros/RotoBaller consensus in rank space
  trade_opportunity_cost.py                     Asset economics vs roster economics per trade
  nfl_schedule.py                                nflverse schedule, cached daily (the one non-ranking fetch)
  streamer_planner.py                             QB/TE/K/DEF one-player vs two-player streaming plans
  market_velocity.py                               Direction-of-travel labels from snapshot history
  matchup_leverage.py                               This-week gap vs the real opponent
  opponent_blocker.py                                At most one Defensive Add per league per week
  roster_consolidation.py                             2-for-1 proposals for contenders
  stash_board.py                                       Dynasty/keeper developmental free agents
  schedule_window.py                                    Next-3 / remaining / playoff windows, tiebreaks only
  buyer_board.py                                         Likely buyers for each sell-high piece
  recommendation_conflicts.py                             "Conflicted Move" detection across signals
  report_data.py                  Shared data layer for both report formats
  report.py                        Markdown renderer
  html_report.py                    HTML dashboard renderer

scripts/            Weekly-run entry points (see above)
tests/               pytest suite (synthetic, offline) for every module above
data/                 SQLite DB, cached rankings, generated reports, run snapshots (gitignored — not meant to be committed)
```

## Testing

```bash
.venv/Scripts/python.exe -m pytest tests/ -v
```

Tests cover the valuation and trade-matching logic specifically (name
normalization, format derivation, positional need-ranking, buy-low
filtering including the age-curve and decline-vs-overreaction checks,
value-tolerance matching, and team-status classification) and every
decision-layer module (thresholds at their exact boundaries, missing-data
paths, pre-draft suppression, deterministic ordering) using synthetic
data — they don't hit any live network endpoints (the NFL schedule tests
use an inline CSV fixture and a temp cache), so they're fast and
deterministic.

## Known limitations

- **KTC's TE-premium modeling** is three fixed tiers (+0.5/+1.0/+1.5 per
  TE reception) regardless of your league's exact PPR type — a
  half-PPR-TE-premium league and a full-PPR-TE-premium league with the
  same TE bonus resolve to the same KTC value slice. Flagged as a caveat
  on any trade involving a TE in an affected league.
- **6pt-passing and 100yd-rush-bonus formats** get an approximate
  multiplicative adjustment (not a precise recompute) to dynasty value and
  redraft point projections, since neither KTC nor RotoBaller natively
  model these axes.
- **RotoBaller's redraft data isn't superflex-aware** — confirmed
  identical regardless of league format param — so a fixed scarcity
  multiplier is applied to QB projections in superflex redraft/keeper
  leagues as an approximation.
- **Multi-FLEX formats** (3-4 FLEX spots) aren't fully accounted for in
  positional-need detection, which weighs each position's single best
  player rather than full flex-slot demand. The depth-need signal
  (`identify_depth_needs`) has the same gap — it counts exact QB/RB/WR/TE
  slots from `roster_positions` but doesn't attribute FLEX/SUPER_FLEX
  slots to any position, so it's a floor on real demand, not the total.
- **Acceptance ratings are a bucketed heuristic** (Very Low → High), not a
  calibrated probability — they're built from real, if incomplete, signals
  (does the offer fill an actual roster hole, does it match the other
  team's contender/rebuild timeline, how active a trader they are) but
  there's no feedback loop yet that checks predicted ratings against which
  trades actually got accepted.
- **FAAB suggestions require the league to expose a `waiver_budget`
  setting** — most of this tool's leagues don't use FAAB, so
  `suggested_faab_pct` is `None` for them by design, not a missing feature.
- **Yahoo integration** is not yet live (see above).
- **The lineup optimizer is structural, not this-week.** The lineup that
  leverage, insurance, bye planning, clogs and previews build on excludes
  season-long designations (IR/PUP/Sus/Inactive) but deliberately keeps a
  player tagged `Out` for this week — a one-week absence shouldn't rewrite
  the roster's shape for the next month. A this-week lineup is available
  (`exclude_game_day_out=True`) but nothing renders it yet. Players with no
  projection (K/DEF, deep bench) start at 0.0 rather than being benched, so
  a required slot is filled by somebody.
- **Playoff leverage is standings arithmetic only.** No Monte Carlo, no
  schedule strength, no invented playoff probability; elimination is
  called only when enough teams already hold more wins than this team can
  still reach, and a possible end-of-season tie never eliminates.
- **Move-impact previews don't see draft picks.** Pick ownership lives in
  Sleeper's traded_picks, not on the roster object, so a pick-heavy
  trade's preview is player-only (the trade card still shows the picks).
  Team status in a preview is classified on optimizer-flagged starters
  for both sides, so its "before" can differ from the headline status; a
  status change is only reported with a 10-point strength move.
- **Thresholds are named constants, not calibrated.** Contender insurance
  (65% / 15%), bench surplus (90%), Toss-Up (5%) / Lean Start (15%),
  bye-hole cover (70%), exposure (4 / 6 leagues, 3 starting-QB leagues),
  roster-clog rank cutoffs (150 dynasty / 120 redraft), league-economy
  labels (3 trades, ±1 pick, 1.5× median) and the negotiation ladder's
  110% ceiling are all first-guess values chosen to be easy to tune, not
  fitted to outcomes.
- **League economy is current-season only** (no `previous_league_id`
  traversal); trader-activity labels are suppressed under three completed
  league trades, so a quiet August says nothing about anyone.
- **Bye cover is by position, not by simulation.** A waiver target is
  tagged as covering a bye hole when he plays the displaced starter's
  position, not by re-running the optimizer with him added.
- **Replacement scarcity is a gap, not a supply count.** A position is
  Scarce when the best startable free agent sits far below the worst
  current starter league-wide (gap thresholds 10% / 30% / 50%); it says
  nothing about how many usable free agents exist. Pre-draft leagues have
  no replacement market at all (the "free agents" are the draft pool).
- **Source disagreement compares rank places, not values.** KTC dollars,
  FantasyPros ECR and RotoBaller points are never divided into each other;
  20 / 40 positional places are the Disagreement / High cutoffs. The
  FantasyPros min/max spread only exists for rows cached after 2026-09-02.
- **Trade economics reuse existing verdicts.** Asset economics IS the
  engine's balance label; roster economics IS the move preview's weekly
  delta bucketed at +3 / -2 / -7. A trade below the preview bar gets asset
  economics only. A Strategic Tradeoff needs a Favorable or Unfavorable
  asset verdict — a Balanced trade with a Major Lineup Cost is reported as
  exactly that, not as a tradeoff.
- **Streaming plans have no opponent adjustment** (no points-allowed data
  is fetched, none is invented) and model at most one switch inside the
  three-week window. Nothing is suggested under a 3-point window gain.
- **Market velocity is a direction, not a forecast.** Three daily
  observations minimum, 8% / 15% total-move thresholds, consecutive
  same-direction days required; no regression, no extrapolation.
- **Opponent blocking needs a visible hole.** Before NFL byes start (and in
  any week the opponent's lineup is intact) there is nothing to block, so
  early-season reports show no Defensive Add. That is the design.
- **Consolidation search is bounded**: my 12 most valuable non-starters,
  4 targets per counterparty, value ratio 0.90-1.35, and the same fit /
  acceptance helpers the trade engine uses.
- **Stash board value is the pool-wide dynasty percentile** (40 / 60
  cutoffs), not a within-position rank; a full roster with no clogs makes
  every stash a Watch.
- **Schedule windows never rate opponents.** Games played and byes per
  window are all the schedule contributes; it breaks ties only when two
  values are within 10%. Fantasy playoff weeks come from
  `playoff_week_start` / `playoff_teams` / `playoff_round_type`, clamped to
  the schedule.
- **Buyer-board scores are additive heuristics** (need +2/+1, timeline
  ±1, economy ±1, scarcity +1, unfunded -2; Strong at 4, Possible at 2).
- **Conflicts are mechanical.** Every Sell High of a QB out of a Very
  Scarce Superflex market is a Conflicted Move by construction; the label
  is information, not a veto.
- **The nflverse schedule is fetched at most once a day** and falls back
  to the stale cache or to the ranking sources' bye weeks when unavailable.
