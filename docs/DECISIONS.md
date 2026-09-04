# Decisions

Consequential choices and why. Newest first. The module docstrings carry the
mechanics; this file carries the reasoning that isn't obvious from the code.

## 2026-09-04 — Night build: replay, thesis tracking, lineup decisions, and a red-team pass

Ten reviewer agents read the real report end to end (three "would a skilled
manager laugh at this" passes, two false-negative hunts, five personas). What
follows is what their findings changed, and the reasoning that is not obvious
from the diffs.

### New capabilities

- **Historical replay is descriptive, and says so.** `historical_replay.py`
  replays 2025 role signals week by week from truncated usage (a future row
  cannot reach the labeller), then reads the following three weeks purely to
  describe what happened. Forward outcomes are named by what the share DID
  ("share up", "share down", "no games"), never by whether the label was
  vindicated — "reverted" means opposite things for Surging and Collapsing.
  The 2025 season is the same one the thresholds were smoke-tested on, so the
  report validates nothing; it describes. Its headline finding is recorded
  under "Known problems" in HANDOFF, not acted on: on that season the rising
  labels preceded a share LOSS three to four times more often than Stable did,
  and the well-behaved part is the conservative one-week structural rule.
- **The watchlist tracks a thesis, not a trigger.** Every item now carries the
  reason it was written in evidence terms and is re-evaluated each run into
  Triggered / Invalidated / Strengthened / Weakened / Unchanged, with named
  invalidation conditions per kind. Invalidation outranks promotion on the
  same run: a Must Add whose role collapsed is Invalidated, not Triggered.
- **This week's decisions is a separate section from lineup leverage.**
  `lineup_decisions.py` answers "what needs a decision before kickoff" (set-
  lineup mismatches, toss-ups with what-if deltas, questionable starters,
  holes, FLEX/Superflex explanations) on the week lineup with byes and outs
  applied. Lineup leverage keeps only the structural fact (bench surplus,
  which is trade material). The same close call was previously told twice.
- **Cross-league asymmetry** names the league where a widely-held player is
  cheapest to move against the one where he is dearest. It is a portfolio
  fact for the trade engine to use, never a sell signal.
- **Recommendation search** (`recommendation_search.py`, `scripts/search_recommendations.py`)
  queries a built report; every hit is a sentence the report already holds.

### Defects the red team found, and the rules that replaced them

- **Every draft pick in every league read "Late".** `estimate_tier` wants a
  league-relative rank; it was being handed the raw starter-percentile
  average, which is 65-95 for every real roster. Bottom-third teams' own
  firsts are Early picks and are priced roughly double a Late 1st, so every
  pick-inclusive offer was mispriced.
- **A SUPER_FLEX slot is QB demand.** Splitting it evenly across four
  positions made a third QB in a Superflex league read as "buried" surplus
  and a fourth QB in a 1QB league read as the roster's top need.
- **A position already carrying its slots plus two spare bodies is not a
  need.** Ranking four positions against each other has to put one last;
  without a depth term that made a four-deep QB room "need #1", which is how
  a fifth QB became a Strong Add at 19% of budget.
- **K and DEF are slots, not assets.** Offering the roster's only kicker
  manufactured an empty required slot the optimizer then priced as a
  "Major Lineup Cost" — a conflict, a chip and a Best Move, all artefacts of
  the give side. IR/PUP players are off the give side for the same reason.
- **An optimizer starter is never a drop candidate, and never a paired
  waiver drop across positions either.** The same-position veto was already
  there; once earlier rows consumed the same-position bench bodies, the
  fallback reached for whoever was cheapest anywhere, pairing a 6th-
  percentile QB add with dropping a 76th-percentile TE.
- **A youth premium is not a market overreaction.** The dynasty-minus-redraft
  gap is the resting state for a first- or second-year player, so buy-low
  fired on youth, not on dips.
- **In redraft, value IS weekly production.** A buy-low target the league's
  own wire already out-projects is not a trade; dynasty keeps its
  developmental buys, where the price is the future rather than this week.
- **An Abundant market caps a waiver tier.** An add that beats no starter
  into a market where comparable production sits on the wire every week is
  depth by definition, however good his rank. Three backup QBs in a 1QB
  league at 8% of budget each was this rule missing.
- **Preserve does not bid the tier's own floor.** "Preserve budget" at a
  Strong Add's low bound is 8% of budget — eight times the largest winning
  bid in some of these leagues. It bids the speculative floor.
- **A sell-high never ships an optimizer starter unless something coming back
  can start.** Otherwise it is a lineup hole bought with a bench piece.
- **A trade that drops my own team status is an Against.** The impact block
  printed "contender → middling" as a neutral line while the card carried no
  argument against the move.
- **Waivers see the wire, not just Sleeper's trending list.** The trending
  endpoint is platform-wide and name-driven; the best projected free agent in
  THIS league is often not on it. Each position's projection-best free agent
  is now a candidate when he out-projects my weakest starter there, and
  arrives with no trend count — a fact about the wire, not the player.
- **The buyer board is built before the proposals.** Building proposals first
  meant a sell-high card could name one counterparty while the board named a
  different Strong Fit two sections down.
- **Offers the engine itself rates Very Low are not printed** unless they are
  all a league has. The engine expecting a refusal is the answer.

### Presentation rules that came out of the persona reviews

- **Best moves is two lists.** Time-boxed or materially lineup-changing moves
  are "Do this week"; everything else is "Optional value plays". A Conflicted
  move is never a to-do. When every row shares one priority line, it is
  printed once above the list rather than on every row, because a line every
  row shares discriminates nothing.
- **One human name per slot.** `slot_label` in `lineup_optimizer` is the only
  place a Sleeper token becomes prose ("SUPER_FLEX" → "Superflex").
- **A percentile gap is "percentile pts", never "points".** The same card
  carries fantasy points; two scales under one word was the tell. A gap that
  rounds to zero is "comparable", not an upgrade, and never a reason for.
- **Facts printed once.** The schedule-window sentence goes at the top of the
  report when every league shares it; draft-capital picks that share a
  classification and a reason are one row; an empty Time-sensitive section is
  not rendered.
- **Waiver rows carry "why this drop" and "what could invalidate this"** from
  `Provenance.extras`, below the capped For/Against lists rather than
  competing with them.

## 2026-09-03 — Intelligence & hardening tranche (usage, feedback, calibration, arbitration)

- **The trade engine's primitives were extracted before anything else touched
  them.** Six modules that sit strictly below `trade_engine.py` (`asset_value`,
  `trade_types`, `roster_assets`, `trade_fit`, `trade_rating`, `trade_messages`)
  plus `draft_picks.pick_key`, with 23 characterization tests written first
  and the report diffed byte-for-byte after. Nothing outside the engine reaches
  for a private helper any more; the engine re-exports what it imports.
- **Player usage comes from nflverse's `stats_player_week`, `stats_team_week`
  and `snap_counts` releases, not the deprecated `player_stats` family and not
  play-by-play.** Three small gzipped CSVs per season (~2 MB) through the same
  daily file cache the rankings use; red-zone shares would need the 19 MB PBP
  file and are a documented future extension. A season whose files 404 is
  cached as an explicit "absent" marker for a day, so the pre-season report
  costs one request per day, not one per league.
- **Sleeper ids are mapped to nflverse ids by a ladder, never by name alone.**
  Sleeper's own `gsis_id` (present for ~21% of rostered players, sometimes with
  a leading space) → DynastyProcess `db_playerids` by `sleeper_id` (~95%) →
  nflverse `players.csv` by name + position + team, active players only →
  unmatched. Name-only matching is last because seven rostered offensive
  players share a name with an IDP (Lamar Jackson the CB, Justin Jefferson the
  LB). DEF units map to their team code. Only rostered and trending players
  are crosswalked (~400 of 12k).
- **A role trend needs two played games; a strong label needs three.** Bye
  weeks and DNPs are absent rows, never zero-usage rows, so a bye cannot
  read as a collapse. Every threshold is a named constant with an epsilon
  because `fmean([0.6, 0.6]) - 0.5` is not exactly 0.1. The prior season is
  context only ("2025 baseline: ...") and never feeds a label — last year's
  role is not evidence about this year's.
- **Role vs market is three labels or nothing.** `market_cross` compares the
  role direction with the market's (velocity, source direction); when the
  market's own labels disagree it says nothing. Role Ahead of Market also
  covers a role moving against the price, on purpose: that is the case worth
  a look.
- **Role annotations are sparse.** Only Rising/Surging/Falling/Collapsing is
  ever written, on the side of the recommendation it argues for; a Stable or
  Insufficient role writes nothing. Until the season has usage rows the report
  says "Role data begins after games are played" exactly once, in the health
  block, and no per-player line.
- **The decision ledger records; it never grades.** Fingerprints exclude the
  run id, so a same-day rerun refreshes `last_seen` instead of duplicating.
  Outcomes are Sleeper facts (Completed / Partially Matched / Acquired by
  Another Manager / Still Available / No Observed Action / Unable to
  Determine) — never "Rejected": the public API has no rejected state and a
  trade nobody sent is not a rejection. Only transactions created at or
  after `first_seen` count, and the open entries are observed BEFORE this
  run's entries are merged, so a recommendation made this minute is
  "(open)", not "Still Available". Persisted only by `daily_run.py` after a
  complete run, like the snapshot.
- **Outcome facts are descriptive windows (1/3/6 weeks), not verdicts.** A
  value move is the sources' own move; "moved in the direction the read
  implied" is the strongest phrasing allowed. Only OBSERVED facts render;
  "window not reached" is state, not news.
- **Calibration reports, it does not tune.** 117 rules over 24 modules,
  eligible counted before any cap, Never Fires only with ≥25 eligible,
  Nearly Always Fires above 60%, time-gated rules listed as such. Findings
  become manual changes with their own decision entries (below) or
  documented expectations — never a threshold rewritten by the report.
- **Source disagreement was recalibrated from measured gaps, and Strong
  Consensus is allowed to be the common case.** On the 2026-09-03 caches
  (591 rostered views) the sources agree almost perfectly inside the top 48
  at every position and every real split sits at rank 49+; the previous
  0.02-per-place scaling silenced all of them (1 Disagreement in 591). 0.01
  per place and a tighter Strong band (≤3 scaled places) give 7 Disagreement,
  21 Direction calls, 63% Strong Consensus. Strong Consensus is a description
  of the sources, not a signal, so its frequency is not a pathology.
- **A Must Add has to beat the weakest starter he would replace.** "Need" is
  relative — two of four positions are always the two weakest, even behind a
  94th-percentile starter — so the tier now reads the same comparison the
  reason sentence phrases; a depth-only need is a Strong Add. The tell was six
  $35 Must Adds for depth QBs/TEs in one league.
- **FAAB posture is about the money, not the player.** Four postures
  (Preserve / Normal / Aggressive / Priority Spend) from facts the report
  already holds; the bid is stated as a share of REMAINING budget, the budget
  is Sleeper's `waiver_budget`, and a negative `waiver_budget_used` (FAAB
  acquired by trade) is real. Guardrails: a streamer with ≥4 comparable free
  agents is a Preserve; so is an Abundant market with ≥4 substitutes and no
  urgent need, whatever the tier. The anchor note ("more than 2× the largest
  winning bid") waits for three observed bids. Leverage is a count of who can
  outbid, never a probability.
- **Signal health grades every input before the leagues build; Unavailable
  suppresses, Stale flags.** Per-source windows (fresh / usable / ceiling); a
  fallback-served snapshot is never Fresh and always degrades the run; the
  stale-cache fallback now has a ceiling past which a fetch failure raises.
  A season's usage feed not existing yet is `expected_absent`: still
  Unavailable (role trends are suppressed), but not a degraded run.
  `save_trending` replaces the table (it used to append, silently
  accumulating 12-day-old counts) and sync skips an empty fetch.
- **Provenance harvests, it does not re-derive.** Every For/Against/Context
  reason is a sentence the decision layer already wrote or a `describe()` of
  an object it built, labelled with a category and a source module; at most
  3/2/2 per card by a categorical priority order, never a weight. Sentences
  that are notes on the method ("treat these offers as more approximate",
  "KTC rank 192 is well outside the startup-relevant pool") or admissions
  ("not an immediate upgrade") are Context, never evidence.
- **Best Moves is ordered by six categorical dimensions, lexicographically.**
  Urgency, Materiality, Perishability, Strategic fit, Evidence agreement,
  Cost — then kind, then the kind's own quality rank, then league and
  headline. No per-kind floors and no numeric boosts remain: a Must Add is
  Immediate and outranks any trade; a Strong Drop is cheaper to reverse than
  a trade and outranks an otherwise-equal one; a deadline-window team's
  trades are This Week. `explain_order` names the deciding dimension.
- **The watchlist is deterministic and quiet.** Near-misses are stored with
  the metrics they were judged on; New Trigger fires once per promotion key,
  a same-day rerun is a no-op (`last_run_on`), two consecutive misses resolve
  an item, resolved items prune after the run that resolved them, 28-day cap.
- **Performance came from sharing, not caching heuristics.** One structural
  lineup map per league threaded into every consumer (the optimizer was 72%
  of the build), valued picks priced once per league, `normalize_name`
  memoized, KTC's name index memoized per snapshot: 5.1s → 3.4s with the
  report byte-identical.

### After the seven-reviewer red team (2026-09-03)

- **Projections interpolate by the league's PPR value.** RotoBaller publishes
  full-PPR and standard totals (its TE-premium column equals PPR in every
  cached file, and its format parameter changes nothing — the three fetches
  are identical). Half PPR was being read as standard, marking every
  pass-catcher down ~35% in two leagues and making QBs look enormous next to
  them, which is what made a 94th-percentile QB "fragile" and five depth
  QBs "Strong Adds". Now `standard + ppr × (ppr_total − standard)`; the
  TE-premium column only for a TE and only when it differs from PPR.
- **Facts the tool computes can now veto, not only annotate.** The previous
  tranche's rule ("conflicts are labelled, never resolved") stays for genuine
  tradeoffs, but three facts are not tradeoffs and now act as filters: an
  optimized starter is never the paired drop; a better same-position player
  is never cut for a worse add; an insurance candidate who out-projects the
  starter he insures, or covers an Abundant/Normal position, is not
  insurance. A negotiation ladder never opens above the engine's own offer.
- **"Your current starting X" is the optimizer's starter everywhere.** The
  waiver engine read Sleeper's set-lineup flag, so one panel named two
  different starting QBs. It now receives the optimizer's starter ids;
  trade_engine's sibling sentence still reads the flag (it has no lineup in
  hand) and says so in its docstring.
- **Materiality is what a move adds.** `abs(delta)` had promoted a −10.7/wk
  sell-high above every unconflicted buy-low. The lineup cost rides on the
  Risk reason and the Cost dimension; a marginal claim or switch is a Monitor
  unless it is a Must Add, so a +0.0 add can never lead the list. Evidence
  agreement counts distinct source modules. Perishability needs a paid tier
  that is also trending. Drop ordering no longer rewards how many modules
  appended a sentence.
- **One positional-need fact on the buyer board.** "Upgrades their WR" and
  "WR is a top need" are the same ranking read twice; the second is now a
  reason string without points, so Strong requires need + timeline +
  economy-or-scarcity, which is what DECISIONS already claimed.
- **The note's writer states its side.** Provenance had inferred FOR/AGAINST
  for waiver notes by substring ("abundant", "exposure"); `report_data`
  annotators now record the direction in `LeagueReportData.note_directions`
  and the heuristics are only a fallback. Conflict reasons are keyed on the
  fact they state, so one scarcity is one Against however many modules
  phrase it. Method caveats ("treat as approximate") are Context.
- **Best Moves uses `action_priority.rank_actions` itself**, with the kind's
  quality rank as an Action field, so the shipped ordering is the tested
  ordering and `explain_order` can name the deciding dimension. The rule is
  printed above the list.
- **Ledger time is `status_updated`.** A waiver claim is queued at `created`
  and processes hours to days later; 17% of the real rows lag by more than
  an hour. The current roster is checked before a rival's add is treated as
  terminal, and an empty transaction list is an answer.
- **Usage staleness is measured against the league's week**, not only the
  fetch age; a season file with too few rows is Partial; the cache ceiling
  applies. Trending is its own health family (an empty fetch keeps
  yesterday's list, so a MAX over the weekly tables would have masked it).
  A degraded ranking source blocks the snapshot, so a source outage never
  becomes a "price move" for market velocity, and velocity breaks its run
  across gaps in the daily record.
- **Left as documented limitations:** the sell-high signal is RotoBaller's
  redraft rank arrow (fires on a third of the pool; a stronger KTC-vs-FP gate
  exists and is preferred when available); scarcity annotates but does not
  enter the value-match tolerance; simultaneous offers are each previewed
  against the untouched roster; the waiver table has no roster-capacity check;
  the owner-profile "trades often" note and the league-economy "Inactive
  Trader" label can disagree (one is a manual note, the other this season's
  record); two `_roster_impact_note` phrasings remain by design.

### Known calibration findings left as they are (2026-09-03)

- The trade engine only emits value-matched offers, so Asset economics is
  Roughly Even 92% of the time and Favorable never fires; a Strategic
  Tradeoff therefore needs a Slight-overpay/Overpay verdict against a lineup
  gain. Loosening the value-match bar is a product decision, not a fix.
- Velocity, schedule notes, defensive adds and outcome facts are time-gated
  (three snapshots, NFL byes, observation windows) and are expected to read
  Insufficient / Never Fires in week 1.
- 46% of scarcity-boosted buyer-board candidates also carry a replacement
  caveat: scarcity is why a buyer pays AND why selling costs; both are true
  and neither is double-counted into a score.

## 2026-09-02 — Second decision-layer tranche (12 capabilities)

- **Scarcity is a gap between two replacement levels, never a position rule.**
  `replacement_value` compares the best startable free agent to the worst
  current starter across the league's optimized lineups. A Superflex league
  gets a Very Scarce QB market because its second QB slot drags the worst
  starter down and empties the wire, not because the code knows what
  Superflex is. Keeps the "never hardcode a league setting" rule honest.
- **Source disagreement lives in within-position rank space.** KTC value,
  FantasyPros ECR and RotoBaller points are not commensurate; positional
  rank places are. The consensus pair is chosen by the league's currency
  (KTC vs FP dynasty for dynasty, FP redraft vs RotoBaller for redraft) and
  the projection side is always RotoBaller. FantasyPros dispersion is stored
  per FP list because the two lists rank the same player very differently.
- **Asset economics and roster economics are never combined.** Each is a
  relabelling of a verdict the engine already produces (balance label, move
  preview delta), so the two labels can disagree — that disagreement IS the
  Strategic Tradeoff signal. A Balanced trade with a Major Lineup Cost is not
  a tradeoff and is reported plainly; only Favorable/Unfavorable vs an
  opposite lineup direction qualifies.
- **The NFL schedule is a direct cached CSV fetch, not a library dependency.**
  nflverse's `games.csv` goes through the same daily file cache the ranking
  scrapers use (`nflverse_schedule`), with a different-season cache treated
  as stale and a `None` schedule degrading every consumer to the ranking
  sources' bye weeks. No nflreadpy, no pandas.
- **Streaming plans model one switch and prefer the single player within 8%.**
  A second transaction is real friction and the projections are not precise
  enough to chase small sequence gains; a 3-point window gain is the floor
  for saying anything. No opponent-strength adjustment because none is
  fetched.
- **Market velocity reads snapshot history rather than a new store.** Decision
  Delta already persists per-day stable values; retention went 2 → 28 days
  and an additive `tracked` bucket (trade targets, waiver adds) was added
  without bumping the schema, because additive fields don't change what
  existing fields mean. Labels need three observations and consecutive
  same-direction days; no regression line.
- **Matchup and blocking use this-week lineups; everything else stays
  structural.** `optimize_lineup(nfl_week=current_week, exclude_game_day_out=True)`
  is used only where the question is literally about this week's game.
  Optimizer totals are rest-of-season projections, so both modules divide by
  `games_remaining` before comparing.
- **A Defensive Add is judged on the opponent's gain and gated on my cost.**
  The drop reuses the waiver engine's own drop search with a protected set
  (optimized starters, bench surplus, developmental players, live trade
  pieces); if that yields nothing, there is no Defensive Add. One per league
  per week.
- **Consolidation reuses the trade engine's fit and acceptance helpers**
  (`_recipient_need_fit`, `_status_fit`, `rate_acceptance`, `proposal_confidence`,
  `generate_trade_message`) rather than inventing a second rating rubric, at
  the cost of importing private helpers — the same trade `negotiation_ladder`
  already made. A public `rate_package` in trade_engine would clean both up.
- **Stash board value is the pool-wide dynasty percentile** because rookies
  the projection sources haven't rated have no other comparable number; the
  free-agent pool can include unprojected but dynasty-valued players on
  request (`require_projection=False`) for this one consumer.
- **Fantasy playoff weeks are derived from Sleeper's playoff settings**
  (`playoff_week_start`, `playoff_teams`, `playoff_round_type`) and clamped to
  the schedule's last regular-season week; the tool never assumes 15-17.
- **The schedule breaks ties only.** Two players more than 10% apart in value
  are ordered by value; the schedule speaks only inside that band, and only
  about games played and byes.
- **Buyer-board fundability is a penalty, not a bonus.** Nearly every roster
  can fund something, so "can fund it" made every counterparty a Strong Fit
  on real data; Strong now requires a real need plus timeline, economy or
  scarcity.
- **Conflicts are labelled, never resolved.** A Conflicted Move keeps its
  acceptance rating and its place in Best Moves; the label and the reasons
  on each side are the whole feature.
- **One free-agent pool per league**, built once in `report_data` (skill
  positions plus K/DEF), empty pre-draft, shared by insurance, replacement
  value, streamers, blocking and the stash board.

### After the red-team review (2026-09-03)

- **Consolidations are proposals, not a side list.** The first cut kept
  2-for-1 offers in their own field, which silently opted them out of every
  annotation pass, the preview, economics, conflicts and Best Moves. They
  are now appended to the proposal list (trade type "consolidation") and
  the dedicated block is a summary only.
- **Rank gaps are scaled by depth and gated by list depth.** A fixed
  20-place bar fired on 49 of 193 deep-list dynasty players and 0 of 95
  top-24 players in the real cache: a "deep player" detector, not a
  disagreement detector. The scaling constant (2% per place) and the
  "beyond the other list's depth is not comparable" rule are the fix; the
  thresholds stay stated in top-of-list places.
- **The replacement level ignores starters below the wire.** One
  abandoned roster's 1.3/wk placeholder set an "Abundant" RB market for a
  whole league; a starter who projects below the best free agent is
  simply a roster that hasn't picked him up.
- **A scarce-market conflict needs the piece to play.** The first cut
  flagged every Superflex QB sell-high; bench-surplus sales out of a
  scarce market cost nothing and are no longer conflicts.
- **Developmental-drop conflicts need value and not already be a drop
  candidate.** 22 of 56 waiver rows carried the same label on the first
  real run; dynasty benches are young by construction.
- **A streamer sequence may beat a rostered best single.** The bye-cover
  case (hold my QB, add a streamer for his bye week) is the module's
  reason to exist and the first cut could never reach it.

## 2026-09-01/02 — First decision-layer tranche

Recorded in the module docstrings, `README.md` "Known limitations" and the
commit messages `dee2e07`, `9ec78cd`, `b680578` (this file did not exist yet).
