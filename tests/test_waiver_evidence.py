from __future__ import annotations

import pytest
from waiver_fixtures import WEEK, player

from sleeper_tool.rankings.fantasyfootballers import BallersRow
from sleeper_tool.rankings.rotoballer_waivers import WaiverBoardRow
from sleeper_tool.replacement_value import ABUNDANT, NORMAL, SCARCE, VERY_SCARCE
from sleeper_tool.waiver_evidence import (
    BALLERS_CONVICTION,
    BALLERS_SPLIT,
    BROAD_CONVICTION,
    DEEPER_LEAGUES_ONLY,
    DEPTH_FIT,
    EXPERTS_AGREE,
    FANTASYPROS_HIGHER,
    REPLACEABLE_POSITION,
    ROLE_LAGS,
    ROLE_SUPPORTS,
    SCARCE_POSITION,
    SPECIALISTS_HIGHER,
    SUPERFLEX_ONLY,
    SUPPORT_AGREE,
    SUPPORT_BROAD,
    SUPPORT_NONE,
    SUPPORT_ROS,
    SUPPORT_SINGLE,
    WAIVER_LISTED,
    WAIVER_TOP,
    build_evidence,
    rosterable_depth,
)
from sleeper_tool.waiver_sources import BALLERS, BOONE, FP_ROS, ROTOBALLER, FPRosView, WaiverSources

PID = "t"
STARTABLE = {"WR": 30}  # rosterable depth 53
ROSTERABLE = rosterable_depth(30)
ALL_SOURCES = (BALLERS, ROTOBALLER, BOONE, FP_ROS)


def ballers_row(rank, hosts=(None, None, None)):
    return BallersRow(name="t", team="KC", rank=rank, andy=hosts[0], jason=hosts[1], mike=hosts[2])


def roto_row(rank, *, note=None, min_size=None, superflex_only=False):
    return WaiverBoardRow(
        rank=rank, name="t", team="KC", position="WR", league_size_note=note, min_league_size=min_size,
        superflex_only=superflex_only,
    )


def sources_with(*, fp=None, ballers=None, roto=None, boone=None):
    src = WaiverSources(claim_week=2)
    if fp is not None:
        src.fp_ros_by_id["ros_full_ppr"] = {PID: FPRosView(fp, 120, 3.0, None, None, "ros_full_ppr")}
    if ballers is not None:
        src.ballers_by_id[PID] = ballers
    if roto is not None:
        src.rotoballer_by_id[PID] = roto
    if boone is not None:
        src.boone_by_id["ppr"] = {PID: boone}
    return src


def ev(*, proj=170.0, num_teams=12, effective_size=None, is_superflex=False, present=ALL_SOURCES, entry=None, **kw):
    return build_evidence(
        entry if entry is not None else player(PID, "WR", proj),
        sources=sources_with(**kw), ppr=1.0, num_teams=num_teams, effective_size=effective_size,
        is_superflex=is_superflex,
        startable=STARTABLE, current_week=WEEK, sources_present=present,
        scarcity=kw.pop("scarcity", None) if False else None,
    )


def ev_full(**kw):
    """build_evidence with the annotation arguments exposed."""
    scarcity = kw.pop("scarcity", None)
    role_label = kw.pop("role_label", None)
    role_market = kw.pop("role_market", None)
    velocity = kw.pop("velocity_label", None)
    present = kw.pop("present", ALL_SOURCES)
    num_teams = kw.pop("num_teams", 12)
    entry = kw.pop("entry", None) or player(PID, "WR", 170.0)
    return build_evidence(
        entry, sources=sources_with(**kw), ppr=1.0, num_teams=num_teams, startable=STARTABLE,
        current_week=WEEK, scarcity=scarcity, role_label=role_label, role_market=role_market,
        velocity_label=velocity, sources_present=present,
    )


# -- depth --------------------------------------------------------------------------------


def test_rosterable_depth_is_1_75x_startable_and_always_at_least_one_deeper():
    assert rosterable_depth(30) == 53
    assert rosterable_depth(2) == 4
    assert rosterable_depth(1) == 2  # ceil(1.75) == 2, and startable + 1 == 2


def test_evidence_carries_both_depths_and_the_weekly_projection():
    e = ev(proj=170.0)
    assert (e.startable_depth, e.rosterable_depth) == (30, 53)
    assert e.weekly_projection == 170.0  # games_remaining(17) == 1
    assert build_evidence(
        player(PID, "K", 10.0), sources=sources_with(), ppr=1.0, num_teams=12, startable=STARTABLE,
        current_week=WEEK,
    ).rosterable_depth is None


# -- Broad Analyst Conviction --------------------------------------------------------------


def test_broad_conviction_needs_both_waiver_boards_plus_one_ros_or_weekly_list():
    with_fp = ev(ballers=ballers_row(3), roto=roto_row(5), fp=ROSTERABLE)
    assert BROAD_CONVICTION in with_fp.labels and with_fp.support == SUPPORT_BROAD

    with_boone = ev(ballers=ballers_row(3), roto=roto_row(5), boone=ROSTERABLE)
    assert BROAD_CONVICTION in with_boone.labels and with_boone.support == SUPPORT_BROAD


def test_two_ros_or_weekly_lists_agreeing_is_never_broad_conviction():
    e = ev(ballers=ballers_row(20), fp=10, boone=10)
    assert BROAD_CONVICTION not in e.labels
    assert EXPERTS_AGREE not in e.labels
    assert e.support == SUPPORT_ROS  # FantasyPros inside startable depth, and nothing more


def test_ros_support_must_be_inside_rosterable_depth():
    inside = ev(ballers=ballers_row(3), roto=roto_row(5), fp=ROSTERABLE)
    outside = ev(ballers=ballers_row(3), roto=roto_row(5), fp=ROSTERABLE + 1)
    assert BROAD_CONVICTION in inside.labels
    assert BROAD_CONVICTION not in outside.labels and EXPERTS_AGREE in outside.labels


def test_both_boards_top_without_ros_support_is_waiver_experts_agree():
    e = ev(ballers=ballers_row(WAIVER_TOP), roto=roto_row(WAIVER_TOP), present=(BALLERS, ROTOBALLER))
    assert e.labels[0] == EXPERTS_AGREE and e.support == SUPPORT_AGREE


def test_waiver_top_boundary():
    inside = ev(ballers=ballers_row(WAIVER_TOP), roto=roto_row(WAIVER_TOP), present=(BALLERS, ROTOBALLER))
    outside = ev(ballers=ballers_row(WAIVER_TOP), roto=roto_row(WAIVER_TOP + 1), present=(BALLERS, ROTOBALLER))
    assert EXPERTS_AGREE in inside.labels
    assert EXPERTS_AGREE not in outside.labels and outside.support == SUPPORT_SINGLE


# -- the Ballers' own hosts --------------------------------------------------------------


def test_ballers_conviction_needs_a_top_rank_and_strong_host_agreement():
    strong = ev(ballers=ballers_row(4, (3, 4, 6)), present=(BALLERS,))
    assert BALLERS_CONVICTION in strong.labels

    moderate = ev(ballers=ballers_row(4, (3, 4, 14)), present=(BALLERS,))
    assert BALLERS_CONVICTION not in moderate.labels

    deep = ev(ballers=ballers_row(WAIVER_TOP + 1, (13, 14, 15)), present=(BALLERS,))
    assert BALLERS_CONVICTION not in deep.labels


def test_ballers_split_is_a_disagreement_and_stops_at_waiver_listed():
    listed = ev(ballers=ballers_row(WAIVER_LISTED, (1, 20, 40)), present=(BALLERS,))
    assert BALLERS_SPLIT in listed.labels and listed.disagreement is True
    assert listed.risk_lines[-1] == "Ballers hosts split (1/20/40)"

    beyond = ev(ballers=ballers_row(WAIVER_LISTED + 1, (1, 20, 40)), present=(BALLERS,))
    assert BALLERS_SPLIT not in beyond.labels


def test_one_host_rank_says_nothing_about_agreement():
    e = ev(ballers=ballers_row(4, (2, None, None)), present=(BALLERS,))
    assert e.ballers_agreement is None
    assert BALLERS_CONVICTION not in e.labels and BALLERS_SPLIT not in e.labels
    assert e.hosts_text() == "2/—/—"


# -- the specialists / FantasyPros split ---------------------------------------------------


def test_waiver_specialists_higher_when_fantasypros_has_him_outside_rosterable_depth():
    e = ev(ballers=ballers_row(5), fp=ROSTERABLE + 1)
    assert SPECIALISTS_HIGHER in e.labels and e.disagreement is True


def test_specialists_higher_also_fires_when_fantasypros_does_not_rank_him_at_all():
    e = ev(ballers=ballers_row(5))
    assert SPECIALISTS_HIGHER in e.labels
    assert "unranked in FantasyPros ROS" in e.risk_lines


def test_specialists_higher_needs_the_fantasypros_page_to_have_loaded():
    e = ev(ballers=ballers_row(5), present=(BALLERS, ROTOBALLER))
    assert SPECIALISTS_HIGHER not in e.labels
    assert "unranked in FantasyPros ROS" not in e.risk_lines


def test_fantasypros_higher_only_when_no_waiver_board_lists_him():
    labelled = ev(fp=30, ballers=ballers_row(WAIVER_LISTED + 1))
    assert FANTASYPROS_HIGHER in labelled.labels

    listed = ev(fp=30, ballers=ballers_row(WAIVER_LISTED))
    assert FANTASYPROS_HIGHER not in listed.labels


def test_fantasypros_higher_needs_a_waiver_board_to_have_loaded_at_all():
    assert FANTASYPROS_HIGHER not in ev(fp=30, present=(FP_ROS,)).labels
    assert FANTASYPROS_HIGHER in ev(fp=30, present=(FP_ROS, ROTOBALLER)).labels


def test_fantasypros_higher_needs_startable_not_merely_rosterable_depth():
    assert FANTASYPROS_HIGHER in ev(fp=30, present=(FP_ROS, BALLERS)).labels
    assert FANTASYPROS_HIGHER not in ev(fp=31, present=(FP_ROS, BALLERS)).labels


# -- league size -----------------------------------------------------------------------------


def test_deeper_leagues_only_compares_the_tag_with_this_leagues_size():
    e = ev(roto=roto_row(5, note="14+ Team PPR Leagues", min_size=14), num_teams=12)
    assert DEEPER_LEAGUES_ONLY in e.labels
    assert e.risk_lines[0] == "RotoBaller #5, 14+ Team PPR Leagues — deeper than this 12-team league"
    assert not any("RotoBaller" in line for line in e.for_lines)


def test_a_tag_at_this_leagues_own_size_is_not_a_deeper_league_warning():
    e = ev(roto=roto_row(5, note="12+ Team PPR Leagues", min_size=12), num_teams=12)
    assert DEEPER_LEAGUES_ONLY not in e.labels
    assert e.for_lines[-1] == "RotoBaller #5, 12+ Team PPR Leagues"


# -- league size is roster depth, not team count -----------------------------------------------

# The four real leagues, as (team count, effective size): a board's size tag
# is a proxy for how many players are off the board, and these leagues do
# not roster like their team counts.
PRIMO = (8, 145 / 14)  # 10.4 — an 8-team keeper with deep rosters
SUCKS = (12, 127 / 14)  # 9.1 — a 12-team redraft with shallow ones
DISCO = (12, 228 / 14)  # 16.3
SURFEIT = (10, 129 / 14)  # 9.2


@pytest.mark.parametrize(
    "league,teams,effective,demotes",
    [
        ("Primo Veterans", *PRIMO, False),
        ("This League Sucks", *SUCKS, True),
        ("Disco", *DISCO, False),
        ("The Surfeit", *SURFEIT, True),
    ],
)
def test_a_ten_plus_tag_is_read_against_roster_depth_not_the_team_count(league, teams, effective, demotes):
    e = ev(roto=roto_row(6, note="10+ Team Leagues", min_size=10), num_teams=teams, effective_size=effective)
    assert (DEEPER_LEAGUES_ONLY in e.labels) is demotes, league
    assert (DEPTH_FIT in e.labels) is not demotes, league


def test_the_tag_boundary_is_strict_so_a_league_at_exactly_the_tagged_size_is_not_demoted():
    at = ev(roto=roto_row(6, note="10+ Team Leagues", min_size=10), num_teams=10, effective_size=10.0)
    just_under = ev(roto=roto_row(6, note="10+ Team Leagues", min_size=10), num_teams=10, effective_size=9.99)
    assert DEEPER_LEAGUES_ONLY not in at.labels and DEPTH_FIT in at.labels
    assert DEEPER_LEAGUES_ONLY in just_under.labels and DEPTH_FIT not in just_under.labels


def test_a_tag_this_league_reaches_is_a_for_line_naming_the_roster_depth_that_makes_it_fit():
    e = ev(roto=roto_row(38, note="14+ Team PPR Leagues", min_size=14), num_teams=12, effective_size=DISCO[1])
    assert DEPTH_FIT in e.labels
    assert e.for_lines[-1] == (
        "RotoBaller #38, 14+ Team PPR Leagues — matches this league's roster depth "
        "(12 teams rostering like a 16-team league)"
    )
    assert not any("RotoBaller" in line for line in e.risk_lines)


def test_a_tag_deeper_than_this_league_names_the_roster_depth_rather_than_the_team_count():
    e = ev(roto=roto_row(5, note="14+ Team PPR Leagues", min_size=14), num_teams=12, effective_size=SUCKS[1])
    assert e.risk_lines[0] == (
        "RotoBaller #5, 14+ Team PPR Leagues — deeper than this league's roster depth "
        "(12 teams rostering like a 9-team league)"
    )


def test_the_depth_sentence_stays_a_plain_team_count_when_the_two_agree():
    e = ev(roto=roto_row(5, note="14+ Team PPR Leagues", min_size=14), num_teams=12, effective_size=12.2)
    assert e.risk_lines[0] == "RotoBaller #5, 14+ Team PPR Leagues — deeper than this 12-team league"
    fits = ev(roto=roto_row(5, note="12+ Team Leagues", min_size=12), num_teams=12, effective_size=12.2)
    assert fits.for_lines[-1] == "RotoBaller #5, 12+ Team Leagues"  # nothing to explain


def test_a_fitting_tag_never_outranks_the_support_levels_it_only_annotates():
    e = ev(roto=roto_row(38, note="14+ Team PPR Leagues", min_size=14), num_teams=12, effective_size=DISCO[1])
    assert DEPTH_FIT in e.labels
    assert e.support == SUPPORT_NONE  # a #38 row is still not a waiver-board recommendation


# -- what counts as listed by a waiver board -----------------------------------------------------


def test_a_board_row_tagged_for_a_depth_this_league_reaches_is_listed_at_any_rank():
    deep = ev(roto=roto_row(38, note="14+ Team PPR Leagues", min_size=14), num_teams=12, effective_size=DISCO[1])
    shallow = ev(roto=roto_row(38, note="14+ Team PPR Leagues", min_size=14), num_teams=12, effective_size=SUCKS[1])
    assert deep.expert_listed is True
    assert shallow.expert_listed is False  # the same row, in a league the board is not talking to


def test_an_untagged_row_past_waiver_listed_is_still_not_listed_however_deep_the_league_is():
    e = ev(roto=roto_row(WAIVER_LISTED + 1), num_teams=12, effective_size=DISCO[1])
    assert e.depth_tag_fits is False and e.expert_listed is False


def test_fantasypros_higher_does_not_fire_when_a_fitting_tag_lists_him_on_a_waiver_board():
    listed = ev(fp=30, roto=roto_row(38, note="14+ Team Leagues", min_size=14), num_teams=12, effective_size=DISCO[1])
    not_listed = ev(fp=30, roto=roto_row(38, note="14+ Team Leagues", min_size=14), num_teams=12, effective_size=SUCKS[1])
    assert FANTASYPROS_HIGHER not in listed.labels
    assert FANTASYPROS_HIGHER in not_listed.labels


def test_a_league_whose_roster_depth_matches_its_team_count_behaves_exactly_as_before():
    for size in (None, 12.0):
        deeper = ev(roto=roto_row(5, note="14+ Team PPR Leagues", min_size=14), num_teams=12, effective_size=size)
        assert DEEPER_LEAGUES_ONLY in deeper.labels
        assert deeper.risk_lines[0] == "RotoBaller #5, 14+ Team PPR Leagues — deeper than this 12-team league"

        same = ev(roto=roto_row(5, note="12+ Team PPR Leagues", min_size=12), num_teams=12, effective_size=size)
        assert DEEPER_LEAGUES_ONLY not in same.labels
        assert same.for_lines[-1] == "RotoBaller #5, 12+ Team PPR Leagues"

        assert ev(roto=roto_row(WAIVER_LISTED + 1), num_teams=12, effective_size=size).expert_listed is False
        assert ev(roto=roto_row(WAIVER_LISTED), num_teams=12, effective_size=size).expert_listed is True


# -- support levels ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"ballers": ballers_row(3), "roto": roto_row(3), "fp": 10}, SUPPORT_BROAD),
        ({"ballers": ballers_row(3), "roto": roto_row(3)}, SUPPORT_AGREE),
        ({"ballers": ballers_row(3)}, SUPPORT_SINGLE),
        ({"roto": roto_row(3)}, SUPPORT_SINGLE),
        ({"fp": 30}, SUPPORT_ROS),
        ({"fp": 31}, SUPPORT_NONE),
        ({}, SUPPORT_NONE),
    ],
)
def test_support_levels(kwargs, expected):
    assert ev(**kwargs).support == expected


# -- disagreement -------------------------------------------------------------------------------


def test_boone_not_ranking_a_recommended_player_is_a_disagreement():
    assert ev(ballers=ballers_row(3), fp=10).disagreement is True  # Boone loaded, no rank for him
    assert ev(ballers=ballers_row(3), fp=10, present=(BALLERS, FP_ROS)).disagreement is False


def test_boone_ranking_him_outside_rosterable_depth_is_a_disagreement():
    assert ev(ballers=ballers_row(3), fp=10, boone=ROSTERABLE + 1).disagreement is True
    assert ev(ballers=ballers_row(3), fp=10, boone=ROSTERABLE).disagreement is False


# -- annotations --------------------------------------------------------------------------------


def test_role_labels_are_recorded_as_context():
    assert ROLE_SUPPORTS in ev_full(role_label="Role Rising").labels
    assert ROLE_SUPPORTS in ev_full(role_market="Role Ahead of Market").labels
    assert ROLE_LAGS in ev_full(ballers=ballers_row(3), role_label="Role Falling").labels
    # Role Lags Hype is about a hyped player: with no board recommending him there is no hype to lag.
    assert ROLE_LAGS not in ev_full(role_label="Role Falling").labels


@pytest.mark.parametrize(
    "scarcity,label",
    [(SCARCE, SCARCE_POSITION), (VERY_SCARCE, SCARCE_POSITION), (ABUNDANT, REPLACEABLE_POSITION)],
)
def test_scarcity_labels(scarcity, label):
    assert label in ev_full(scarcity=scarcity).labels


def test_a_normal_market_gets_no_scarcity_label():
    e = ev_full(scarcity=NORMAL)
    assert SCARCE_POSITION not in e.labels and REPLACEABLE_POSITION not in e.labels


# -- the source facts ------------------------------------------------------------------------------


def test_each_source_line_lands_on_the_for_or_risk_side_it_belongs_on():
    e = ev(ballers=ballers_row(3, (2, 3, 5)), roto=roto_row(9), fp=ROSTERABLE, boone=ROSTERABLE + 1)
    assert e.for_lines == [
        "Ballers #3 (strong agreement: 2/3/5)",
        "RotoBaller #9",
        f"FantasyPros ROS WR{ROSTERABLE}",
    ]
    assert e.risk_lines == [f"Boone WR{ROSTERABLE + 1} this week"]


def test_a_ballers_rank_past_waiver_listed_is_a_risk_line_but_a_listed_one_is_not():
    assert ev(ballers=ballers_row(WAIVER_LISTED + 1)).risk_lines[0] == f"Ballers #{WAIVER_LISTED + 1}"
    assert ev(ballers=ballers_row(WAIVER_LISTED)).for_lines[0] == f"Ballers #{WAIVER_LISTED}"


def test_best_waiver_rank_and_expert_listed():
    e = ev(ballers=ballers_row(25), roto=roto_row(8))
    assert e.best_waiver_rank == 8 and e.expert_listed is True
    assert ev(ballers=ballers_row(WAIVER_LISTED + 1)).expert_listed is False
    assert ev().best_waiver_rank is None and ev().expert_listed is False


def test_pos_label_falls_back_to_a_dash():
    e = ev(ballers=ballers_row(3))
    assert e.pos_label(12) == "WR12"
    assert e.pos_label(None) == "—"


# -- a format tag is not a size tag ----------------------------------------------------------


def test_a_two_qb_tag_is_a_recommendation_in_superflex_and_a_warning_in_one_qb():
    row = roto_row(76, note="2QB Leagues", superflex_only=True)

    sf = ev(roto=row, is_superflex=True)
    assert DEPTH_FIT in sf.labels and SUPERFLEX_ONLY not in sf.labels
    assert any("RotoBaller #76" in line for line in sf.for_lines)

    one_qb = ev(roto=row, is_superflex=False)
    assert SUPERFLEX_ONLY in one_qb.labels and DEPTH_FIT not in one_qb.labels
    assert any("this league starts one quarterback" in line for line in one_qb.risk_lines)


def test_a_stated_size_wins_over_the_format_flag_when_the_board_gives_both():
    both = ev(
        roto=roto_row(40, note="14+ Team 2QB Leagues", min_size=14, superflex_only=True),
        effective_size=9.1, is_superflex=True,
    )
    assert DEEPER_LEAGUES_ONLY in both.labels and SUPERFLEX_ONLY not in both.labels
