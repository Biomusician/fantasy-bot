"""Characterization tests for the trade primitives extracted out of
`trade_engine.py` into `asset_value`/`roster_assets`/`trade_fit`/
`trade_rating`/`trade_messages`/`trade_types`.

These were written against the pre-extraction code and the message strings
below are pinned BYTE-FOR-BYTE as the original produced them — the whole
point is that moving a function between modules cannot change what it
returns. Treat a failure here as "the move changed behaviour", not as a
test that needs its expectation refreshed.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import make_entry, make_format, make_roster, make_value

from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.roster_assets import untouchable_ids
from sleeper_tool.team_status import CONTENDER, MIDDLING, REBUILD
from sleeper_tool.trade_fit import piece_fits, status_fit, weakest_rosterable_percentile
from sleeper_tool.trade_messages import _content_seed, generate_trade_message
from sleeper_tool.trade_types import OpponentFit, TradeProposal


def _pick() -> OwnedPick:
    return OwnedPick(season="2027", round=1, original_roster_id=4, tier="Mid", name="2027 Mid 1st", value=4200)


# -- generate_trade_message / _content_seed: pinned output -------------------


def test_content_seed_is_a_stable_sum_of_code_points():
    # Deliberately not Python's hash(): that is randomized per process, so
    # the opener a trade gets would change run to run for the same trade.
    assert _content_seed("") == 0
    assert _content_seed("a", "b", "c") == 294
    assert _content_seed("Give Guy", "Receive Guy", "rival") == 2326


def test_message_buy_low_single_for_single_with_no_clauses_is_byte_identical():
    proposal = TradeProposal(
        league_name="Test League", currency="dynasty", target_username="rival", target_team_name="Rival Team",
        give=[make_entry(player_id="g1", name="Give Guy")],
        receive=[make_entry(player_id="r1", name="Receive Guy")],
        my_value_total=1000.0, their_value_total=1000.0,
        rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="buy_low",
    )
    assert generate_trade_message(proposal) == (
        "Would you do Give Guy for Receive Guy? Let me know if it's not your speed."
    )


def test_message_sell_high_multi_piece_with_all_four_clauses_is_byte_identical():
    proposal = TradeProposal(
        league_name="Test League", currency="dynasty", target_username="seller", target_team_name="Seller Team",
        give=[make_entry(player_id="g2", name="Hot Streak")],
        receive=[make_entry(player_id="r2", name="Steady Vet"), make_entry(player_id="r3", name="Bench Arm")],
        receive_picks=[_pick()],
        my_value_total=5000.0, their_value_total=5200.0,
        rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="sell_high",
    )
    fit = OpponentFit(
        target_is_starter=True, would_upgrade_their_roster=True, fit_notes=[],
        opponent_status=CONTENDER, status_fit="good_fit", piece_count=1,
    )
    msg = generate_trade_message(
        proposal, fit,
        benefit_reason="since he'd clear Weak Starter at WR by 18 points, not just marginally better",
        timeline_clause="figured it makes sense since you're pushing to win now",
        buzz_clause="he's been heating up lately, more than the rankings have caught up to yet",
        my_interest_clause="he'd start over Bench Guy for me at RB",
    )
    assert msg == (
        "Hey, I'd move Hot Streak for Steady Vet + Bench Arm + 2027 Mid 1st, interested? "
        "Since he'd clear Weak Starter at WR by 18 points, not just marginally better. "
        "Figured it makes sense since you're pushing to win now. "
        "He's been heating up lately, more than the rankings have caught up to yet. "
        "He'd start over Bench Guy for me at RB."
    )


def test_message_pick_target_two_for_a_pick_is_byte_identical():
    proposal = TradeProposal(
        league_name="Test League", currency="dynasty", target_username="rebuilder", target_team_name="Rebuild Team",
        give=[make_entry(player_id="g3", name="Vet One"), make_entry(player_id="g4", name="Vet Two")],
        receive=[], receive_picks=[_pick()],
        my_value_total=4100.0, their_value_total=4200.0,
        rationale_for_me=[], rationale_for_them=[], caveats=[], trade_type="pick_target",
    )
    fit = OpponentFit(
        target_is_starter=False, would_upgrade_their_roster=True, fit_notes=[],
        opponent_status=REBUILD, status_fit="good_fit", piece_count=2,
    )
    msg = generate_trade_message(
        proposal, fit,
        benefit_reason="since you don't have a real WR right now",
        timeline_clause="figured it makes sense since you're rebuilding",
        my_interest_clause="I'd rather stockpile picks than lock into a specific player right now",
    )
    assert msg == (
        "Hey, I'd send Vet One + Vet Two for 2027 Mid 1st. "
        "Since you don't have a real WR right now. "
        "Figured it makes sense since you're rebuilding. "
        "I'd rather stockpile picks than lock into a specific player right now."
    )


# -- weakest_rosterable_percentile ------------------------------------------


def _wr(pid: str, pctl: float) -> object:
    return make_entry(player_id=pid, position="WR", value=make_value(position="WR", dynasty_value_percentile=pctl))


def test_weakest_rosterable_percentile_returns_none_when_the_only_body_is_excluded():
    # The buy-low target is still ON their roster (the trade hasn't
    # executed), so without the exclusion he props up "their WR depth"
    # against himself.
    roster = make_roster(entries=[_wr("target-wr", 80.0), _wr("scrub-wr", 10.0)])
    assert weakest_rosterable_percentile(roster, "WR", "dynasty", exclude_player_id="target-wr") is None
    assert weakest_rosterable_percentile(roster, "WR", "dynasty") == 80.0


def test_weakest_rosterable_percentile_takes_the_minimum_of_the_rosterable_ones():
    roster = make_roster(entries=[_wr("wr1", 90.0), _wr("wr2", 55.0), _wr("wr3", 20.0)])
    assert weakest_rosterable_percentile(roster, "WR", "dynasty") == 55.0  # 20.0 is below the rosterable bar


# -- piece_fits --------------------------------------------------------------


def test_piece_fits_true_when_they_have_no_rosterable_depth_there():
    roster = make_roster(entries=[_wr("wr1", 90.0)])  # nothing at TE at all
    piece = make_entry(player_id="give-te", position="TE", value=make_value(position="TE", dynasty_value_percentile=20.0))
    assert piece_fits(roster, piece, "dynasty") is True


def test_piece_fits_true_when_the_piece_beats_their_weakest_rosterable():
    roster = make_roster(entries=[_wr("wr1", 90.0), _wr("wr2", 55.0)])
    assert piece_fits(roster, _wr("give-wr", 70.0), "dynasty") is True


def test_piece_fits_false_when_the_piece_does_not_clear_their_weakest_rosterable():
    roster = make_roster(entries=[_wr("wr1", 90.0), _wr("wr2", 55.0)])
    assert piece_fits(roster, _wr("give-wr", 55.0), "dynasty") is False  # ties do not clear the bar
    assert piece_fits(roster, _wr("give-wr2", 50.0), "dynasty") is False


# -- untouchable_ids ---------------------------------------------------------


def test_untouchable_ids_protects_a_second_qb_the_leagues_own_starter_slots_require():
    # Superflex-shaped: starter_slots says this league starts 2 QBs, so
    # the QB2 is lineup-critical even though he's neither a top-2-overall
    # asset nor a within-position outlier.
    fmt = replace(make_format(qb_format="SF"), starter_slots={"QB": 2.0})
    qb1 = make_entry(player_id="qb1", position="QB", is_starter=True,
                     value=make_value(position="QB", dynasty_value=9000, dynasty_value_percentile=95.0,
                                      dynasty_positional_percentile=95.0))
    qb2 = make_entry(player_id="qb2", position="QB", is_starter=True,
                     value=make_value(position="QB", dynasty_value=4000, dynasty_value_percentile=60.0,
                                      dynasty_positional_percentile=60.0))
    wr1 = make_entry(player_id="wr1", position="WR", is_starter=True,
                     value=make_value(position="WR", dynasty_value=8000, dynasty_value_percentile=88.0,
                                      dynasty_positional_percentile=88.0))
    wr2 = make_entry(player_id="wr2", position="WR", is_starter=True,
                     value=make_value(position="WR", dynasty_value=7000, dynasty_value_percentile=85.0,
                                      dynasty_positional_percentile=85.0))
    roster = make_roster(fmt=fmt, entries=[qb1, qb2, wr1, wr2])
    ids = untouchable_ids(roster, "dynasty", exclude_top=2)
    assert "qb2" in ids  # protected purely by the starter_slots rule
    assert ids == {"qb1", "qb2", "wr1"}


def test_untouchable_ids_falls_back_to_all_corroborated_when_starters_are_too_few():
    # Only ONE starter on the roster but exclude_top=2, so the top-2 cut
    # ranks over every corroborated entry instead of over starters alone.
    starter = make_entry(player_id="s1", position="WR", is_starter=True,
                         value=make_value(position="WR", dynasty_value_percentile=70.0,
                                          dynasty_positional_percentile=70.0))
    bench_better = make_entry(player_id="b1", position="RB", is_starter=False,
                              value=make_value(position="RB", dynasty_value_percentile=80.0,
                                               dynasty_positional_percentile=80.0))
    bench_worse = make_entry(player_id="b2", position="TE", is_starter=False,
                             value=make_value(position="TE", dynasty_value_percentile=30.0,
                                              dynasty_positional_percentile=30.0))
    roster = make_roster(entries=[starter, bench_better, bench_worse])
    ids = untouchable_ids(roster, "dynasty", exclude_top=2)
    assert ids == {"s1", "b1"}  # the bench RB made the cut via the fallback pool


# -- status_fit --------------------------------------------------------------


def _veteran() -> object:
    return make_entry(player_id="vet", position="RB", age=30.0, value=make_value(position="RB"))


def _youngster() -> object:
    return make_entry(player_id="kid", position="RB", age=22.0, value=make_value(position="RB"))


@pytest.mark.parametrize(
    "opponent_status, shape, expected",
    [
        (CONTENDER, "veteran", "good_fit"),
        (CONTENDER, "young", "neutral"),
        (CONTENDER, "picks_only", "mismatch"),
        (CONTENDER, "mixed", "good_fit"),  # a veteran in the package still reads win-now
        (MIDDLING, "veteran", "neutral"),
        (MIDDLING, "young", "neutral"),
        (MIDDLING, "picks_only", "neutral"),
        (MIDDLING, "mixed", "neutral"),
        (REBUILD, "veteran", "mismatch"),
        (REBUILD, "young", "good_fit"),
        (REBUILD, "picks_only", "good_fit"),
        (REBUILD, "mixed", "good_fit"),  # picks in the package outweigh the veteran
    ],
)
def test_status_fit_table(opponent_status, shape, expected):
    give, picks = {
        "veteran": ([_veteran()], []),
        "young": ([_youngster()], []),
        "picks_only": ([], [_pick()]),
        "mixed": ([_veteran()], [_pick()]),
    }[shape]
    assert status_fit(give, picks, opponent_status) == expected
