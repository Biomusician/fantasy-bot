"""A league's projection is computed from its own scoring, not looked up.

RotoBaller publishes a standard and a full-PPR season total. The gap
between them is the player's projected receptions, because the PPR column
is exactly one point per catch more than the standard one. Everything else
follows by arithmetic: `standard + (points per catch) x receptions`.

Two bugs of the same shape have now been found on this path. The first
gave half-PPR leagues the standard column. The second gave every TE in a
TE-premium league the source's own te_premium column — which is full PPR
plus 0.5/catch — so a 0.5-PPR TE-premium league got a full-PPR projection
and a premium that was not its own. In the one live league scored that way
that was about +20% on every TE, enough to make a mid TE out-project a
good WR and take a FLEX slot he should not have had.

Neither was caught by a test, so these pin the arithmetic at the
boundaries rather than pinning any one league's label.
"""
from __future__ import annotations

import pytest

from sleeper_tool.valuation import LeagueFormat, _projection_for_format


def fmt(ppr: float, te_premium: float = 0.0) -> LeagueFormat:
    return LeagueFormat(qb_format="1QB", ppr=ppr, te_premium_bonus=te_premium,
                        rush_100_bonus=0.0, pass_td_pts=4.0,
                        starter_slots={}, roster_positions=())


# 100 standard points, 60 receptions: full PPR is 160.
CATCHER = {"proj_points_standard": 100.0, "proj_points_ppr": 160.0,
           "proj_points_te_premium": 190.0}
# a runner who never catches: every scoring setting is the same number
RUNNER = {"proj_points_standard": 200.0, "proj_points_ppr": 200.0,
          "proj_points_te_premium": 200.0}


@pytest.mark.parametrize("ppr,expected", [
    (0.0, 100.0),      # standard
    (0.5, 130.0),      # half PPR is halfway, not the standard column
    (1.0, 160.0),      # full PPR
])
def test_ppr_interpolates_continuously(ppr, expected):
    assert _projection_for_format(CATCHER, fmt(ppr), "WR") == pytest.approx(expected)


@pytest.mark.parametrize("ppr", [0.0, 0.25, 0.5, 0.75, 1.0])
@pytest.mark.parametrize("position", ["WR", "RB", "TE"])
def test_the_curve_is_the_league_setting_not_a_league_label(ppr, position):
    """Nothing here reads a league name or a cache-file key: the answer is
    a function of the scoring setting alone."""
    got = _projection_for_format(CATCHER, fmt(ppr), position)
    assert got == pytest.approx(100.0 + ppr * 60.0)


def test_an_unusual_fractional_ppr_is_arithmetic_not_extrapolation():
    """Receptions are known, so 1.5/catch is computable. Clamping it at 1.0
    would under-project every pass catcher in such a league."""
    assert _projection_for_format(CATCHER, fmt(1.5), "WR") == pytest.approx(190.0)
    assert _projection_for_format(CATCHER, fmt(0.1), "WR") == pytest.approx(106.0)


def test_a_te_premium_adds_to_the_leagues_own_ppr_it_does_not_replace_it():
    # 0.5 PPR + 0.5 premium = 1.0/catch for a TE = 160, NOT the source's 190
    assert _projection_for_format(CATCHER, fmt(0.5, 0.5), "TE") == pytest.approx(160.0)
    # full PPR + 0.5 premium = 1.5/catch = 190
    assert _projection_for_format(CATCHER, fmt(1.0, 0.5), "TE") == pytest.approx(190.0)
    # a premium bigger than the source publishes is still the league's own
    assert _projection_for_format(CATCHER, fmt(1.0, 1.0), "TE") == pytest.approx(220.0)
    # zero PPR with a premium is the premium alone
    assert _projection_for_format(CATCHER, fmt(0.0, 0.5), "TE") == pytest.approx(130.0)


def test_the_te_premium_column_is_not_read():
    """It is full PPR plus 0.5/catch whatever the league scores, so reading
    it silently promotes a half-PPR league. Poison it and nothing moves."""
    poisoned = dict(CATCHER, proj_points_te_premium=9999.0)
    for f in (fmt(0.5, 0.5), fmt(1.0, 0.5), fmt(0.0, 1.0)):
        assert _projection_for_format(poisoned, f, "TE") < 1000.0


def test_a_te_premium_never_touches_another_position():
    for position in ("WR", "RB", "QB", "K", "DEF", None):
        assert (_projection_for_format(CATCHER, fmt(0.5, 0.5), position)
                == pytest.approx(_projection_for_format(CATCHER, fmt(0.5), position)))


def test_a_player_who_does_not_catch_is_unaffected_by_reception_scoring():
    """A QB's projection must not move with the PPR dial."""
    values = {_projection_for_format(RUNNER, fmt(p), "QB")
              for p in (0.0, 0.5, 1.0, 1.5)}
    assert values == {200.0}
    assert _projection_for_format(RUNNER, fmt(0.5, 0.5), "TE") == pytest.approx(200.0)


@pytest.mark.parametrize("position", ["WR", "RB", "TE"])
def test_monotonic_in_ppr_for_anyone_with_receptions(position):
    got = [_projection_for_format(CATCHER, fmt(p), position)
           for p in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert got == sorted(got)
    assert got[0] < got[-1]


def test_a_negative_ppr_setting_never_subtracts_points():
    assert _projection_for_format(CATCHER, fmt(-1.0), "WR") == pytest.approx(100.0)


def test_a_missing_column_falls_back_rather_than_inventing_a_number():
    assert _projection_for_format(
        {"proj_points_standard": 100.0, "proj_points_ppr": None}, fmt(0.5), "WR") == 100.0
    assert _projection_for_format(
        {"proj_points_standard": None, "proj_points_ppr": 160.0}, fmt(0.5), "WR") == 160.0
    assert _projection_for_format(
        {"proj_points_standard": None, "proj_points_ppr": None}, fmt(0.5), "WR") is None


def test_the_live_half_ppr_te_premium_league_does_not_start_a_te_over_a_better_wr():
    """The regression that made this worth finding: real cached numbers for
    a 0.5-PPR + 0.5-TE-premium league. McBride must not out-project Nacua."""
    mcbride = {"proj_points_standard": 141.3, "proj_points_ppr": 250.3,
               "proj_points_te_premium": 304.8}
    nacua = {"proj_points_standard": 230.4, "proj_points_ppr": 349.4,
             "proj_points_te_premium": 349.4}
    league = fmt(0.5, 0.5)
    te = _projection_for_format(mcbride, league, "TE")
    wr = _projection_for_format(nacua, league, "WR")
    assert te == pytest.approx(250.3)
    assert wr == pytest.approx(289.9)
    assert te < wr
