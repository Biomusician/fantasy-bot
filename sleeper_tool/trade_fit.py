"""Would the other side actually want this?

Everything in `trade_engine.py` above the proposal loop answers "is the math
balanced". None of it asks whether the RECEIVING team's roster has any use
for what they'd get — the single most consequential gap an 8-reviewer audit
of this codebase converged on independently. These four functions close that
gap, and they live here rather than inside the engine because the buyer
board, the negotiation ladder and the consolidation search all need the same
verdicts without needing the engine.
"""
from __future__ import annotations

from sleeper_tool.asset_value import MIN_ROSTERABLE_PERCENTILE, corroborated, percentile_for_currency
from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.team_status import CONTENDER, REBUILD, veteran_min_age, young_max_age


def weakest_rosterable_percentile(
    roster: ValuedRoster, position: str, currency: str, *, exclude_player_id: str | None = None
) -> float | None:
    """The bar a give-piece must clear to plausibly be an upgrade for this
    roster at this position — their worst currently-rosterable player
    there. None means they have no rosterable depth at all at this
    position, i.e. ANY reasonable piece would help. `exclude_player_id`
    matters when the piece being evaluated is going the OTHER direction in
    the same trade (e.g. the buy-low target leaving their roster, or the
    sell-high asset leaving mine) — the roster object still contains that
    departing player since the trade hasn't executed, so without excluding
    him he can prop up "their depth" against himself.
    """
    pctls = [
        percentile_for_currency(e.value, currency)
        for e in roster.by_position(position)
        if e.player_id != exclude_player_id
        and corroborated(e, currency)
        and (percentile_for_currency(e.value, currency) or 0) >= MIN_ROSTERABLE_PERCENTILE
    ]
    return min(pctls) if pctls else None


def piece_fits(their_roster: ValuedRoster, piece: RosterEntry, currency: str, *, exclude_player_id: str | None = None) -> bool:
    """Would this ONE piece plausibly help their_roster — beats their
    weakest currently-rosterable player at that position, or fills a
    position where they have zero rosterable depth at all."""
    weakest = weakest_rosterable_percentile(their_roster, piece.position or "", currency, exclude_player_id=exclude_player_id)
    piece_pctl = percentile_for_currency(piece.value, currency) or 0
    return weakest is None or piece_pctl > weakest


def recipient_need_fit(
    their_roster: ValuedRoster, give: list[RosterEntry], currency: str, *, exclude_player_id: str | None = None
) -> tuple[bool, bool, list[str]]:
    """Would sending `give` plausibly help their_roster, or is it roster
    clutter they have no use for? Returns (any_fits, all_fit, notes):
    any_fits is True if AT LEAST ONE piece clears the bar (used to decide
    whether an offer is worth proposing at all — a legitimate throw-in
    alongside a real upgrade is a normal trade shape); all_fit is True
    only if EVERY piece does (used to decide whether the offer should be
    scored as a clean, unqualified roster upgrade for them, vs. one that
    includes dead weight). A picks-only give (no players) always fits
    fully — draft capital helps any roster. `exclude_player_id` excludes a
    player still technically on their_roster but departing in this same
    trade (see weakest_rosterable_percentile).
    """
    if not give:
        return True, True, []
    notes: list[str] = []
    fits_flags: list[bool] = []
    for piece in give:
        ok = piece_fits(their_roster, piece, currency, exclude_player_id=exclude_player_id)
        fits_flags.append(ok)
        if not ok:
            notes.append(f"{piece.name} ({piece.position}) likely wouldn't beat their existing {piece.position} depth")
    return any(fits_flags), all(fits_flags), notes


def status_fit(give: list[RosterEntry], give_picks: list[OwnedPick], opponent_status: str) -> str:
    """Does what the OPPONENT receives match their apparent team timeline?
    Veteran, proven production fits a contender and cuts against a
    rebuild; picks/youth fit a rebuild and cut against a contender who
    wants to win now, not stockpile future assets.
    """
    has_picks = bool(give_picks)
    veteran_heavy = any(e.age is not None and e.age >= veteran_min_age(e.position) for e in give)
    young_heavy = (not veteran_heavy) and any(
        e.age is not None and e.age <= young_max_age(e.position) for e in give
    )
    if opponent_status == REBUILD:
        if has_picks or young_heavy:
            return "good_fit"
        if veteran_heavy:
            return "mismatch"
    elif opponent_status == CONTENDER:
        if veteran_heavy:
            return "good_fit"
        if has_picks and not give:
            return "mismatch"
    return "neutral"
