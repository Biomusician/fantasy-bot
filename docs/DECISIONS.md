# Decisions

Consequential choices and why. Newest first. The module docstrings carry the
mechanics; this file carries the reasoning that isn't obvious from the code.

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
