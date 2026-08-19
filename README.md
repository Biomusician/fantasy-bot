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
  report_data.py                  Shared data layer for both report formats
  report.py                        Markdown renderer
  html_report.py                    HTML dashboard renderer

scripts/            Weekly-run entry points (see above)
tests/               pytest suite for the valuation/trade-matching logic
data/                 SQLite DB, cached rankings, generated reports (gitignored-equivalent — not meant to be committed)
```

## Testing

```bash
.venv/Scripts/python.exe -m pytest tests/ -v
```

Tests cover the valuation and trade-matching logic specifically (name
normalization, format derivation, positional need-ranking, buy-low
filtering including the age-curve and decline-vs-overreaction checks,
value-tolerance matching, and team-status classification) using synthetic
data — they don't hit any live network endpoints, so they're fast and
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
