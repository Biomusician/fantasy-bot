"""The render-only view layer: which already-written sentence gets the
visible slot, and what goes behind disclosure.

Everything here is pure over strings and small records — the point of the
module is that both renderers make these choices identically, so a rule
that only one of them applied would be a renderer divergence.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import make_entry, make_value

from sleeper_tool.action_priority import PriorityKey
from sleeper_tool.move_impact import MoveImpact, RosterSnapshot
from sleeper_tool.recommendation_conflicts import CONFLICTED, Conflict, WAIVER
from sleeper_tool.report_data import PriorityAction
from sleeper_tool.report_views import (
    ActionView,
    action_view,
    claim,
    clauses,
    confidence_caveat,
    fact_of,
    health_banner,
    lineup_lines,
    scarcity_fact,
    source_fact,
    split_visible,
    waiver_row_view,
    without_repeats,
)
from sleeper_tool.waiver_engine import WaiverTarget


# -- one vocabulary for one fact ---------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # The four phrasings four different modules write for one fact.
        ("QB replacement market is Very Scarce — waivers won't repair this", ("scarcity", ("QB",), "Very Scarce")),
        ("QB replacement market is Very Scarce: no waiver replacement for what you'd send", ("scarcity", ("QB",), "Very Scarce")),
        ("RB market is Scarce here: an add at this position matters more than his rank alone suggests",
         ("scarcity", ("RB",), "Scarce")),
        ("+5.0/wk over the best free-agent RB (Scarce market)", ("scarcity", ("RB",), "Scarce")),
        # "Very Scarce" wins over the "Scarce" nested inside it.
        ("WR replacement market is Very Scarce", ("scarcity", ("WR",), "Very Scarce")),
        # A multi-position note keeps both, so it never collides with a
        # single-position one.
        ("RB/WR replacement market is Scarce/Very Scarce — waivers won't repair this",
         ("scarcity", ("RB", "WR"), "Scarce")),
    ],
)
def test_scarcity_fact_recognises_every_phrasing(text, expected):
    assert scarcity_fact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Brock Purdy would restore that slot to ~19.0/wk",
        "QB is a second lineup slot every roster competes for",  # no scarcity level
        "Very Scarce",  # no position, no market context
        # "Normal" is deliberately not a matched level: the source panel
        # writes "Normal Consensus" about a market, and treating that as a
        # scarcity statement would suppress a real one.
        "Sources: Normal Consensus; Projection Above Market (WR94 market vs WR60 projection)",
    ],
)
def test_scarcity_fact_ignores_everything_else(text):
    assert scarcity_fact(text) is None


def test_source_fact_is_one_sentence_per_player():
    assert source_fact("Sources on Tory Horton: Normal Consensus; Market Above Projection") == ("sources", "tory horton")
    assert source_fact("Sources disagree on Tory Horton.") == ("sources", "tory horton")
    # A waiver note has no subject in the text — the row already names him.
    assert source_fact("Sources: Source Disagreement: KTC vs FantasyPros differ by 37 WR places") == ("sources", "")
    assert source_fact("KTC and FantasyPros are within 6 points of each other on him") is None


def test_the_same_fact_in_two_phrasings_is_shown_once():
    economics = "QB replacement market is Very Scarce — waivers won't repair this"
    conflict = "QB replacement market is Very Scarce: no waiver replacement for what you'd send"
    seen = claim(set(), [economics])
    assert without_repeats([conflict, "an unrelated caveat"], seen) == ["an unrelated caveat"]


def test_a_different_position_or_level_is_a_different_fact():
    seen = claim(set(), ["QB replacement market is Very Scarce — waivers won't repair this"])
    kept = without_repeats(
        ["RB replacement market is Very Scarce — waivers won't repair this",
         "QB replacement market is Scarce — waivers won't repair this"],
        seen,
    )
    assert len(kept) == 2


def test_an_identical_sentence_is_deduped_even_without_a_recognised_fact():
    """The commonest duplication of all: the provenance card quotes a
    caveat verbatim, so the card would say it twice."""
    seen = claim(set(), ["Cuts against their apparent contender timeline."])
    assert without_repeats(["Cuts against their apparent contender timeline"], seen) == []


def test_a_category_label_prefix_does_not_defeat_the_match():
    seen = claim(set(), ["Insurance (Stash): Insurance for Joe Burrow (QB)"])
    assert without_repeats(["Insurance for Joe Burrow (QB)"], seen) == []


def test_without_repeats_dedupes_a_list_against_itself():
    assert without_repeats(["a", "b", "a"], set()) == ["a", "b"]


def test_empty_strings_are_dropped_not_deduped():
    assert without_repeats(["", None, "x"], set()) == ["x"]


# -- progressive disclosure ---------------------------------------------------


def test_split_visible_never_discards():
    shown, hidden = split_visible([1, 2, 3, 4, 5], 3)
    assert shown == [1, 2, 3] and hidden == [4, 5]
    assert split_visible([1, 2], 4) == ([1, 2], [])


def test_clauses_splits_the_engines_own_joined_reason():
    assert clauses("a; b; c") == ["a", "b", "c"]
    assert clauses("") == []


# -- Best Moves rows ----------------------------------------------------------


def _action(**kw) -> PriorityAction:
    base = dict(
        league_name="L", kind="trade", headline="Send X for Y", detail="L — good acceptance likelihood, sell high.",
        priority=PriorityKey("Monitor", "Major", "Durable", "Neutral", "Mixed", "Moderate"),
    )
    base.update(kw)
    return PriorityAction(**base)


def test_the_detail_no_longer_starts_with_the_conflict_banner():
    a = _action(
        detail=f"{CONFLICTED}: against — Major Lineup Cost (-10.7/wk). L — good acceptance likelihood, sell high.",
        against=["Major Lineup Cost (-10.7/wk)"],
    )
    v = action_view(a)
    assert v.conflicted is True
    assert not v.detail.startswith(CONFLICTED)
    assert v.detail == "Good acceptance likelihood, sell high."
    # The banner's only reason is already on the Against line, so it is not
    # restated — but nothing was lost.
    assert v.conflict_note == ""
    assert v.against == ["Major Lineup Cost (-10.7/wk)"]


def test_a_conflict_reason_the_against_line_does_not_carry_survives_as_a_note():
    a = _action(detail=f"{CONFLICTED}: against — spends a Strategic pick. L — buy low.", against=["something else"])
    v = action_view(a)
    assert v.conflict_note == "spends a Strategic pick"


def test_the_league_moves_out_of_the_detail_into_its_own_field():
    v = action_view(_action(league_name="Big Daddy AF", detail="Big Daddy AF — good acceptance likelihood, buy low."))
    assert v.league == "Big Daddy AF"
    assert v.detail == "Good acceptance likelihood, buy low."


def test_a_clause_already_shown_as_why_now_is_not_repeated_in_the_detail():
    a = _action(
        kind="waiver",
        detail="L — Insurance for Joe Burrow; Purdy restores that slot Impact: Purdy enters the lineup. FAAB: $10.",
        why_now=["Insurance (Stash): Insurance for Joe Burrow", "Purdy restores that slot"],
    )
    v = action_view(a)
    assert "Insurance for Joe Burrow" not in v.detail
    # The impact note is concatenated onto the end of the reason clause
    # without a separator, so only the known prefix is trimmed away.
    assert v.detail == "Impact: Purdy enters the lineup. FAAB: $10."


def test_why_now_and_against_are_capped_at_the_provenance_limits():
    v = action_view(_action(why_now=["a", "b", "c", "d"], against=["x", "y", "z"]))
    assert v.why_now == ["a", "b", "c"] and v.against == ["x", "y"]


def test_priority_is_the_three_leading_dimensions():
    assert action_view(_action()).priority == "Monitor · Major · Durable"
    assert action_view(_action(priority=None)).priority == ""


def test_an_action_view_is_frozen():
    with pytest.raises(Exception):
        action_view(_action()).detail = "x"
    assert isinstance(action_view(_action()), ActionView)


# -- waiver rows --------------------------------------------------------------


def _target(**kw) -> WaiverTarget:
    base = dict(
        player_id="w1", name="Wire Guy", position="RB", team="KC", trend_count=100, value=make_value(position="RB"),
        fills_need=True, need_rank=0, priority_tier="Must Add", horizon="Season Starter",
        reason="lead clause; second clause; third clause; fourth clause",
        notes=["RB market is Scarce here: an add at this position matters more than his rank alone suggests"],
    )
    base.update(kw)
    return WaiverTarget(**base)


def _impact(deltas_note: str | None = None) -> MoveImpact:
    snap = lambda pts: RosterSnapshot(  # noqa: E731
        lineup=None, weekly_points=pts, depth_needs=[], status=None, strength_percentile=None,
        roster_value=0, avg_starter_age=None,
    )
    return MoveImpact("Add", snap(100.0), snap(100.0), lineup_in=["Wire Guy"], matchup_note=deltas_note)


def test_the_why_cell_keeps_only_the_leading_clauses():
    row = waiver_row_view(_target())
    assert row.lead == "lead clause; second clause"
    assert "third clause" in row.details and "fourth clause" in row.details


def test_everything_else_lands_in_the_row_details_and_nothing_is_lost():
    conflict = Conflict(kind=WAIVER, key="w1", subject="Wire Guy", reasons_against=["the drop is a starter"])
    row = waiver_row_view(_target(), impact=_impact("matchup edge is 17.8"), conflict=conflict, faab_detail="uses 19% of budget")
    joined = " | ".join(row.details)
    for sentence in ("third clause", "fourth clause", "the drop is a starter", "matchup edge is 17.8",
                     "an add at this position matters more", "uses 19% of budget"):
        assert sentence in joined, sentence
    # The objection leads the details, and is chipped in the cell itself.
    assert row.details[0].startswith(CONFLICTED)
    assert (CONFLICTED, "negative") in row.chips


def test_a_scarcity_note_repeating_the_lead_clause_is_not_shown_twice():
    note = "RB market is Scarce here: an add at this position matters more than his rank alone suggests"
    row = waiver_row_view(_target(reason=note + "; second clause", notes=[note]))
    assert row.details.count(note) == 0  # already in the lead
    assert note in row.lead


def test_a_row_with_no_impact_gets_no_impact_chip():
    assert waiver_row_view(_target()).chips == []
    chips = waiver_row_view(_target(), impact=_impact()).chips
    assert chips and chips[0][0].startswith("Impact: ")


# -- roster / lineup ----------------------------------------------------------


def test_confidence_caveat_flags_a_shaky_valuation_and_stays_quiet_otherwise():
    assert confidence_caveat(make_value(position="RB", sources=["ktc"])) is not None  # single source
    assert confidence_caveat(make_value(position="RB", sources=["ktc", "fantasypros"])) is None
    assert confidence_caveat(None) is None


def test_lineup_lines_uses_the_leagues_own_slot_order():
    from sleeper_tool.lineup_optimizer import optimize_lineup

    from conftest import make_format, make_roster

    roster = make_roster(
        entries=[make_entry(player_id="q", name="Q", position="QB"), make_entry(player_id="r", name="R", position="RB")],
        fmt=make_format(roster_positions=("QB", "RB", "BN")),
    )
    rows = lineup_lines(optimize_lineup(roster))
    assert [slot for slot, _, _ in rows] == ["QB", "RB"]
    assert all("None" not in name for _, name, _ in rows)
    assert lineup_lines(None) == []


# -- signal health ------------------------------------------------------------


class _Signal:
    def __init__(self, name, label, expected_absent=False):
        self.display_name = name
        self.label = label
        self.expected_absent = expected_absent


class _Health:
    def __init__(self, degraded, signals):
        self.degraded = degraded
        self.signals = signals


class _Report:
    def __init__(self, health, suppressed=None):
        self.health = health
        self.suppressed = suppressed or {}
        self.generated_at = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)


def test_no_banner_when_everything_is_fine():
    assert health_banner(_Report(_Health(False, [_Signal("KTC", "Fresh")]))) is None
    assert health_banner(_Report(None)) is None


def test_the_banner_wording_tracks_the_health_grade():
    """It sits above the Signal health section; if it said "degraded" while
    that section said "all sources fresh", the reader is being lied to."""
    gaps = health_banner(_Report(_Health(False, [_Signal("KTC", "Fresh")]), {"role_trends": "no usage"}))
    assert gaps.degraded is False and gaps.text.startswith("Signal health: usable, with gaps")
    assert "role trends" in gaps.text

    bad = health_banner(_Report(_Health(True, [_Signal("KTC", "Stale"), _Signal("FF", "Unavailable", True)])))
    assert bad.degraded is True and bad.text.startswith("Signal health: degraded")
    assert "KTC" in bad.text and "FF" not in bad.text  # an expected-absent source is not the reason


def test_fact_of_always_returns_something_hashable():
    assert isinstance(fact_of("anything at all"), tuple)
    assert fact_of("A") == fact_of("a.")
