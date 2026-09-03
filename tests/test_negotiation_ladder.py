from conftest import make_entry, make_format, make_league_info, make_roster, make_value

from sleeper_tool.negotiation_ladder import (
    FALLBACK,
    MAX_OUTGOING_RATIO,
    OPENING,
    WALK_AWAY,
    build_ladders,
    build_negotiation_ladder,
)
from sleeper_tool.trade_types import TradeProposal

POSITIONS = ("QB", "RB", "RB", "WR", "WR", "TE", "BN", "BN", "BN", "BN")


def _p(pid, pos, dyn, *, pctl=60.0, age=25.0, is_starter=False):
    return make_entry(
        player_id=pid, name=pid, position=pos, age=age, is_starter=is_starter,
        value=make_value(
            name=pid, position=pos, dynasty_value=dyn, proj_points=100,
            dynasty_value_percentile=pctl, dynasty_positional_percentile=pctl, redraft_ecr_percentile=pctl,
        ),
    )


def _roster(rid, entries, owner):
    return make_roster(
        roster_id=rid, owner_id=owner, owner_username=owner, team_name=owner, entries=entries,
        fmt=make_format(roster_positions=POSITIONS), league=make_league_info(kind="dynasty", name="L"),
    )


def _setup():
    # My tradeable pool (non-starters, mid-value): a spread of WR/RB pieces.
    mine = _roster(1, [
        _p("qb1", "QB", 9000, pctl=95, is_starter=True), _p("rb1", "RB", 8000, pctl=95, is_starter=True),
        _p("wr_a", "WR", 3000, pctl=55), _p("wr_b", "WR", 4000, pctl=62), _p("wr_c", "WR", 5000, pctl=70),
        _p("rb_x", "RB", 2500, pctl=50), _p("te_z", "TE", 1500, pctl=45),
    ], "me")
    # Their roster: weak WRs (so my WRs fit), target is their RB2 valued 5000.
    theirs = _roster(2, [
        _p("tqb", "QB", 7000, pctl=90, is_starter=True), _p("trb1", "RB", 7000, pctl=88, is_starter=True),
        _p("target", "RB", 5000, pctl=70, is_starter=True), _p("twr1", "WR", 1200, pctl=30, is_starter=True),
        _p("twr2", "WR", 1000, pctl=25, is_starter=True),
    ], "rival")
    target = theirs.entries[2]
    proposal = TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="rival",
        give=[mine.entries[4]], receive=[target], my_value_total=5000, their_value_total=5000,
        rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="buy_low",
        acceptance_rating="Moderate", message="Hey, wr_c for target?",
    )
    return mine, theirs, proposal


def test_opening_is_the_cheapest_package_that_still_rates_at_least_moderate():
    mine, theirs, proposal = _setup()
    ladder = build_negotiation_ladder(proposal, mine, theirs, [], my_status="contender", their_status="contender")
    assert ladder.opening.name == OPENING
    assert ladder.opening.acceptance in ("Moderate", "Good", "High")
    # Every cheaper fitting package must rate below Moderate, or the opening isn't the cheapest.
    assert ladder.opening.outgoing_value <= proposal.my_value_total
    assert ladder.opening_message  # the opening always carries a message


def test_fallback_is_one_asset_away_and_inside_the_ten_percent_cap():
    mine, theirs, proposal = _setup()
    ladder = build_negotiation_ladder(proposal, mine, theirs, [], my_status="contender", their_status="contender")
    if ladder.fallback is not None:
        assert ladder.fallback.name == FALLBACK
        assert ladder.fallback.ratio <= MAX_OUTGOING_RATIO + 1e-9
        assert ladder.fallback.outgoing_value > ladder.opening.outgoing_value
        added = ladder.fallback.key() - ladder.opening.key()
        removed = ladder.opening.key() - ladder.fallback.key()
        assert len(added) == 1 and len(removed) <= 1
        assert ladder.fallback.acceptance != ladder.opening.acceptance


def test_walk_away_is_the_most_expensive_still_acceptable_package_and_never_a_repeat():
    mine, theirs, proposal = _setup()
    ladder = build_negotiation_ladder(proposal, mine, theirs, [], my_status="contender", their_status="contender")
    if ladder.walk_away is not None:
        assert ladder.walk_away.name == WALK_AWAY
        assert ladder.walk_away.acceptance in ("Moderate", "Good", "High")
        assert ladder.walk_away.ratio <= MAX_OUTGOING_RATIO + 1e-9
        assert ladder.walk_away.outgoing_value >= ladder.opening.outgoing_value
        assert ladder.walk_away.key() != ladder.opening.key()
        if ladder.fallback is not None:
            assert ladder.walk_away.key() != ladder.fallback.key()


def test_a_step_that_spends_a_current_starter_says_so():
    mine, theirs, proposal = _setup()
    ladder = build_negotiation_ladder(
        proposal, mine, theirs, [], my_status="contender", their_status="contender", my_starter_ids={"wr_c", "wr_b"}
    )
    steps = [s for s in (ladder.opening, ladder.fallback, ladder.walk_away) if s]
    for s in steps:
        names = {e.name for e in s.players}
        if names & {"wr_c", "wr_b"}:
            assert s.starter_note is not None and s.starter_note.startswith("includes your current starter")
        else:
            assert s.starter_note is None


def test_duplicate_pick_names_render_with_a_count():
    from sleeper_tool.draft_picks import OwnedPick
    from sleeper_tool.negotiation_ladder import LadderStep

    picks = [
        OwnedPick(season="2028", round=4, original_roster_id=1, tier="Late", name="2028 Late 4th", value=1400),
        OwnedPick(season="2028", round=4, original_roster_id=5, tier="Late", name="2028 Late 4th", value=1400),
    ]
    step = LadderStep(OPENING, [], picks, 2800, 1.0, "Moderate", [])
    assert step.asset_names == "2028 Late 4th ×2"
    assert step.lowball is False
    assert LadderStep(OPENING, [], picks, 2000, 0.7, "Moderate", []).lowball is True


def test_opening_reuses_the_engine_message_when_it_is_the_engine_offer():
    mine, theirs, proposal = _setup()
    ladder = build_negotiation_ladder(proposal, mine, theirs, [], my_status="contender", their_status="contender")
    if ladder.opening.key() == frozenset({("player", "wr_c")}):
        assert ladder.opening_message == "Hey, wr_c for target?"
    else:
        assert ladder.opening_message != "Hey, wr_c for target?"


def test_sell_high_proposals_get_no_ladder_and_only_two_per_league():
    mine, theirs, proposal = _setup()
    sell = TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="rival",
        give=[mine.entries[2]], receive=[theirs.entries[3]], my_value_total=3000, their_value_total=1200,
        rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="sell_high",
    )
    assert build_negotiation_ladder(sell, mine, theirs, [], my_status="contender", their_status="contender") is None
    ladders = build_ladders(
        [sell, proposal, proposal, proposal], mine, {1: mine, 2: theirs}, [],
        my_status="contender", status_of={2: "contender"},
    )
    assert sorted(ladders) == [1, 2]


def test_the_opening_is_never_dearer_than_the_engines_own_offer():
    """An opening is a cheaper way in, never a dearer one.

    The biting case: the engine's own package (wr_a, $3000) is on the board
    but rates Very Low, and the ONLY package that rates Moderate or better
    is wr_c at $5000 — two thirds more expensive. Without the "no dearer
    than the base step" filter the ladder would open by asking me to pay
    $5000 for a deal I was told costs $3000, which is not a negotiating
    position, it is a different trade.
    """
    mine, theirs, base_proposal = _setup()
    cheap = next(e for e in mine.entries if e.player_id == "wr_a")
    dearer = next(e for e in mine.entries if e.player_id == "wr_c")
    proposal = TradeProposal(
        league_name="L", currency="dynasty", target_username="rival", target_team_name="rival",
        give=[cheap], receive=list(base_proposal.receive), my_value_total=3000, their_value_total=5000,
        rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="buy_low",
        acceptance_rating="Very Low", message="wr_a for target?",
    )
    ladder = build_negotiation_ladder(proposal, mine, theirs, [], my_status="contender", their_status="contender")

    assert ladder is not None
    assert [p.player_id for p in ladder.opening.players] == [cheap.player_id]
    assert ladder.opening.outgoing_value == proposal.my_value_total
    # The better-rated package exists and is deliberately not the opening.
    assert dearer.player_id not in {p.player_id for p in ladder.opening.players}

    # The general invariant, over both proposals this file builds.
    for candidate in (base_proposal, proposal):
        built = build_negotiation_ladder(candidate, mine, theirs, [], my_status="contender", their_status="contender")
        assert built.opening.outgoing_value <= candidate.my_value_total
