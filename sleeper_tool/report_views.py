"""Render-only view helpers shared by the Markdown report and the HTML
dashboard.

Both renderers must show the same facts. These functions decide WHICH of
the sentences the decision layer already wrote gets the visible slot and
which goes behind progressive disclosure — nothing here scores, ranks,
re-derives or reclassifies anything. Every string that goes in is one a
decision module produced; a string that loses a dedupe contest is
already on screen somewhere above it, or is handed back in the
"collapsed" half of a split.

Why a separate module rather than either renderer: a presentation rule
applied in only one of them IS a renderer divergence, which is the bug
class this file exists to prevent. And why not `report_data.py`: this is
presentation, not analysis — `report_data` stays the place where
decisions are made, this is the place where they are laid out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sleeper_tool.role_trends import INSUFFICIENT as INSUFFICIENT_ROLE
from sleeper_tool.waiver_engine import EARLY_SEASON_CLAUSE
from sleeper_tool.lineup_optimizer import slot_label
from sleeper_tool.action_priority import IMMEDIATE, MAJOR, MEANINGFUL, THIS_WEEK, PriorityKey, priority_line  # noqa: F401  (PriorityKey re-exported for renderers)
from sleeper_tool.recommendation_conflicts import CONFLICTED
from sleeper_tool.signal_health import DEGRADED_LABELS, FRESH, OPTIONAL_FAMILIES, UNAVAILABLE, USABLE
from sleeper_tool.trade_rating import player_confidence

# -- one vocabulary for one fact ---------------------------------------------
#
# The same replacement-scarcity fact is written in several phrasings by
# several modules (trade_opportunity_cost's economics note,
# recommendation_conflicts' against-reason, replacement_value's per-player
# clause, waiver_engine's note via report_data). Whichever one reaches the
# reader first claims the fact; the rest are suppressed on that card/row.

POSITION_TOKENS = ("QB", "RB", "WR", "TE", "K", "DEF")
# "Normal" is deliberately absent. It is the neutral level, so restating
# it costs the reader nothing, and matching on it would collide with the
# source-panel phrase "Normal Consensus".
SCARCITY_LEVELS = ("Very Scarce", "Scarce", "Abundant")
_SCARCITY_CONTEXT = ("market", "replacement", "waiver", "wire")
_SOURCE_LEADS = (
    ("sources on ", ":"),
    ("sources disagree on ", None),
    ("sources split on ", None),
)


# A leading "Label: " or "Label (Sub): " — the provenance layer prefixes
# harvested sentences with their own category label, so the same clause
# reaches the reader twice in two dressings unless the label is ignored
# when comparing.
_LABEL_PREFIX = re.compile(r"^[A-Z][^:.]{0,40}: ")


def _normalized(text: str) -> str:
    text = " ".join(text.split()).strip().strip("*_")
    text = _LABEL_PREFIX.sub("", text)
    return text.rstrip(".").casefold()


def _earliest(text: str, options) -> str | None:
    """The option that appears first in `text`; longest wins a tie, so
    "Very Scarce" beats the "Scarce" nested inside it."""
    best = None
    for option in options:
        i = text.find(option)
        if i < 0:
            continue
        if best is None or i < best[0] or (i == best[0] and len(option) > len(best[1])):
            best = (i, option)
    return best[1] if best else None


def scarcity_fact(text: str) -> tuple | None:
    """The (positions, level) a replacement-market sentence states, or None
    when the sentence isn't one. Position-set and level must both match for
    two sentences to count as the same fact, so a "RB Very Scarce" note
    never suppresses a "WR Scarce" one."""
    if not text:
        return None
    lowered = text.casefold()
    if not any(word in lowered for word in _SCARCITY_CONTEXT):
        return None
    level = _earliest(text, SCARCITY_LEVELS)
    if level is None:
        return None
    positions = tuple(p for p in POSITION_TOKENS if re.search(rf"\b{p}\b", text))
    if not positions:
        return None
    # A per-player measured edge ("+4.2/wk over the best free-agent RB") is
    # not the same fact as the market label it mentions; only two market
    # sentences, or two per-player ones, are the same fact.
    has_number = bool(re.search(r"\d+\.\d/wk", text))
    return ("scarcity", positions, level, has_number)


def source_fact(text: str) -> tuple | None:
    """The player a source-disagreement sentence is about — one such
    sentence per player per card/row."""
    if not text:
        return None
    stripped = text.strip().lstrip("⚠️ ").strip()
    lowered = stripped.casefold()
    for lead, stop in _SOURCE_LEADS:
        if lowered.startswith(lead):
            subject = stripped[len(lead):]
            if stop:
                subject = subject.split(stop)[0]
            return ("sources", _normalized(subject))
    if lowered.startswith("sources:"):
        return ("sources", "")  # a waiver note: the row already names the player
    panel = re.match(r"^(.+?): fantasypros' own expert panel", lowered)
    if panel:
        return ("sources", _normalized(panel.group(1)))
    return None


def fact_of(text: str) -> tuple:
    """The dedupe key for one already-written sentence. Falls back to the
    sentence itself, which is what catches the commonest duplication of
    all: the provenance card quotes a rationale/caveat verbatim, so the
    same sentence would otherwise appear twice on one card."""
    return source_fact(text) or scarcity_fact(text) or ("text", _normalized(text))


def claim(seen: set, texts) -> set:
    """Register sentences as already on screen. Returns `seen` for chaining."""
    for text in texts:
        if text:
            seen.add(fact_of(text))
    return seen


def without_repeats(texts, seen: set) -> list[str]:
    """`texts` minus every sentence whose fact is already claimed. Claims
    what survives, so a list deduped against itself keeps one of each."""
    kept = []
    for text in texts:
        if not text:
            continue
        key = fact_of(text)
        if key in seen:
            continue
        seen.add(key)
        kept.append(text)
    return kept


# -- progressive disclosure ---------------------------------------------------


def split_visible(items, limit: int) -> tuple[list, list]:
    """(shown, hidden). Nothing is discarded — the caller renders `hidden`
    behind a disclosure control."""
    items = list(items)
    if limit < 0 or len(items) <= limit:
        return items, []
    return items[:limit], items[limit:]


MAX_IMPACT_DELTAS = 4
MAX_WHY_NOW = 3
MAX_AGAINST = 2
WAIVER_LEAD_CLAUSES = 2
ORDERING_NOTE = "Ordered by how soon each must be decided, then by how much it changes; ties by kind, then league."
URGENT_URGENCIES = (IMMEDIATE, THIS_WEEK)
MATERIAL_MATERIALITIES = (MAJOR, MEANINGFUL)
NOTHING_URGENT_NOTE = "Nothing here is time-boxed to this week — these are value plays, kept for when you want one."
ALL_SAME_PRIORITY_NOTE = "Every move below carries the same priority ({priority}), so the order is by kind and league."


def split_actions(actions):
    """(do_now, optional). A move is `do_now` when the priority layer says it
    is time-boxed (Immediate / This Week) or materially changes the lineup
    (Major / Meaningful), and it isn't a Conflicted move — a "review this
    manually" item is never a to-do. Everything else is a value play the
    reader can take or leave, which is what the eight identical
    `Monitor · Marginal · Durable` rows were really saying."""
    do_now, optional = [], []
    for a in actions:
        key = getattr(a, "priority", None)
        timely = key is not None and (key.urgency in URGENT_URGENCIES or key.materiality in MATERIAL_MATERIALITIES)
        (do_now if timely and not action_view(a).conflicted else optional).append(a)
    return do_now, optional


def shared_priority(actions) -> str:
    """The one priority line every action shares, or "" when they differ —
    a line repeated on every row discriminates nothing and belongs above
    the list, once."""
    lines = {priority_line(a.priority) for a in actions if getattr(a, "priority", None) is not None}
    return lines.pop() if len(lines) == 1 and len(actions) > 1 else ""




def clauses(text: str, sep: str = "; ") -> list[str]:
    return [c.strip() for c in (text or "").split(sep) if c.strip()]


# -- Best Moves rows ----------------------------------------------------------


@dataclass(frozen=True)
class ActionView:
    """One "Best moves right now" row, split into the four things the
    reader needs: what, how urgent, why now, what could go wrong — with
    the old free-text `detail` demoted to a secondary line that no longer
    leads with the conflict banner."""

    conflicted: bool
    conflict_note: str  # the banner's against-reasons, minus any already in `against`
    detail: str
    why_now: list[str]
    against: list[str]
    priority: str  # the three leading dimensions, or ""
    league: str  # pulled out of `detail`, which used to be the only place it appeared


_BANNER = CONFLICTED + ":"


def action_view(action) -> ActionView:
    """Pure over a PriorityAction. `report_data` writes the conflict banner
    into `detail`; here it becomes a flag plus a trailing note, so the row
    can lead with what to actually do."""
    detail = (action.detail or "").strip()
    stated = (getattr(action, "conflict_note", "") or "").strip()
    conflicted = bool(stated) or detail.startswith(_BANNER)
    banner = stated
    if conflicted and not stated:
        head, sep, rest = detail.partition(". ")
        if sep:
            banner, detail = head, rest.strip()
        else:  # a one-sentence detail that is only the banner
            banner, detail = head, ""
        banner = banner.partition("against — ")[2] or banner

    league = action.league_name or ""
    for lead in (f"{league} — ", f"{league} - ", f"{league}: "):
        if league and detail.startswith(lead):
            detail = detail[len(lead):]
            break

    why_now = list(action.why_now)[:MAX_WHY_NOW]
    against = list(action.against)[:MAX_AGAINST]

    # Anything already shown as Why now / Against is not repeated in the
    # secondary line or the conflict note. The comparison runs one level
    # deeper than sentences, because the engine's own reason string is a
    # semicolon-joined list whose clauses are what the provenance card
    # quoted individually.
    shown = [*why_now, *against]
    seen = claim(set(), shown)
    known: list[str] = []
    for text in shown:
        for part in clauses(text):
            claim(seen, [part])
            known.append(_flat(part))
    note = " · ".join(without_repeats(clauses(banner), set(seen)))
    kept = [s for s in (_prune(sentence, seen, known) for sentence in _sentences(detail)) if s]
    text = " ".join(s if s.endswith((".", "!", "?")) else s + "." for s in kept)
    return ActionView(
        conflicted=conflicted, conflict_note=note,
        detail=text[:1].upper() + text[1:],
        why_now=why_now, against=against, league=league,
        priority=priority_line(action.priority) if action.priority is not None else "",
    )


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _prune(sentence: str, seen: set, known: list[str]) -> str:
    """One sentence with its already-shown semicolon clauses removed. A
    clause that merely BEGINS with an already-shown one keeps its tail:
    `report_data` concatenates the engine's reason and the impact note
    without a separator, so the join lands mid-clause."""
    parts = [_trim_known_prefix(c, known) for c in clauses(sentence)]
    return "; ".join(without_repeats(parts, seen))


def _trim_known_prefix(text: str, known: list[str]) -> str:
    flat = " ".join(text.split())
    lowered = flat.casefold()
    for prefix in known:
        if prefix and lowered.startswith(prefix) and len(flat) > len(prefix):
            rest = flat[len(prefix):].strip(" ,;:.—-")
            return rest if any(c.isalnum() for c in rest) else ""
    return text


def _sentences(text: str) -> list[str]:
    """Split on sentence boundaries only — a decimal point or an
    abbreviation like "C.J." must not start a new sentence."""
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[a-z0-9\)\]])\.\s+", text) if s.strip()]


# -- waiver rows --------------------------------------------------------------


@dataclass
class WaiverRow:
    """A waiver table row split into a short "Why" cell and a per-row
    details list. Every sentence the engine wrote is in one or the other."""

    lead: str
    chips: list[tuple[str, str]] = field(default_factory=list)  # (text, chip kind)
    details: list[str] = field(default_factory=list)
    table_notes: list[str] = field(default_factory=list)  # facts about the week, printed once under the table


_TABLE_LEVEL_CLAUSES = frozenset({_flat(EARLY_SEASON_CLAUSE)})


def waiver_row_view(target, *, impact=None, conflict=None, faab_detail: str | None = None, seen_leads: set | None = None) -> WaiverRow:
    """`target.reason` is a semicolon-joined list of clauses; the first two
    are the answer to "why him", the rest is supporting detail. With
    `seen_leads` (shared across one table), a lead the previous rows already
    showed word for word steps aside for the row's next clause, so a column
    of eight identical sentences becomes a column of what differs."""
    parts = clauses(target.reason or "")
    # A clause that is true of the WEEK rather than of this player belongs
    # under the table once, not in fifty rows (the engine writes it per row
    # because that is where it knows the week).
    table_notes = [c for c in parts if _flat(c) in _TABLE_LEVEL_CLAUSES]
    parts = [c for c in parts if _flat(c) not in _TABLE_LEVEL_CLAUSES]
    lead, rest = split_visible(parts, WAIVER_LEAD_CLAUSES)
    if seen_leads is not None and parts:
        # Clause by clause: "depth behind your starting QB, X" repeats on
        # every QB row; the clause that differs is the one worth the slot.
        fresh = [c for c in parts if _flat(c) not in seen_leads]
        if fresh:
            lead = fresh[:WAIVER_LEAD_CLAUSES]
            rest = [c for c in parts if c not in lead]
        seen_leads.update(_flat(c) for c in lead)
    row = WaiverRow(lead="; ".join(lead))

    seen = claim(set(), lead)
    if conflict is not None:
        row.chips.append((CONFLICTED, "negative"))
    if impact is not None:
        deltas = impact.material_deltas()
        row.chips.append((("Impact: " + deltas[0]) if deltas else "Depth only", "neutral"))

    details: list[str] = []
    if conflict is not None:  # the objection leads, ahead of the supporting detail
        details.append(f"{CONFLICTED} — against: {'; '.join(conflict.reasons_against)}")
    details.extend(rest)
    if impact is not None:
        deltas = impact.material_deltas()
        details.append("Impact: " + ("; ".join(deltas) if deltas else "no lineup change — depth only"))
        if impact.matchup_note:
            details.append(impact.matchup_note)
    details.extend(target.notes)
    if faab_detail:
        details.append("FAAB: " + faab_detail)
    row.details = without_repeats(details, seen)
    row.table_notes = table_notes
    return row


# -- roster / valuation -------------------------------------------------------

CONFIDENCE_MARK = "⚠"
CONFIDENCE_LEGEND = (
    "⚠ = the valuation behind this row is less reliable "
    "(one source only, a thin market, or the sources disagreeing)"
)


def confidence_caveat(value) -> str | None:
    """Why a player's valuation is shaky, or None when it isn't. Reuses
    trade_rating's own rubric rather than re-deriving a partial copy."""
    if value is None or player_confidence(value) == "High":
        return None
    return (
        getattr(value, "thin_market_caveat", None)
        or getattr(value, "panel_disagreement_caveat", None)
        or (
            "Only one ranking source has this player — treat the value as less reliable"
            if not getattr(value, "is_corroborated", True)
            else "KTC and FantasyPros disagree on this player's value"
        )
    )


def lineup_units(games_left: int | None) -> str:
    """`LineupResult` projections are rest-of-season totals;
    `LineupLeverage.games_left` is documented as the divisor renderers use
    to show them per week. Without a leverage record there is no divisor,
    and the number must be labelled for what it is rather than silently
    presented as a weekly figure."""
    return "/wk" if games_left else " rest-of-season"


def lineup_total(lineup, games_left: int | None = None) -> float:
    if lineup is None:
        return 0.0
    return lineup.total_projected_points / games_left if games_left else lineup.total_projected_points


def lineup_lines(lineup, games_left: int | None = None) -> list[tuple[str, str, str]]:
    """The optimized starting lineup as (slot, player, projection) in the
    league's own slot order. `lineup_optimizer` is the single owner of who
    starts; this only formats what it decided."""
    if lineup is None:
        return []
    divisor = games_left or 1
    rows = [
        (slot_label(a.slot), f"{a.name} ({a.position or '?'})", f"{a.projection / divisor:.1f}")
        for a in sorted(lineup.assignments, key=lambda a: a.slot_index)
    ]
    rows.extend((slot_label(slot), "— empty —", "0.0") for slot in lineup.unfilled_slots)
    return rows


# -- signal health ------------------------------------------------------------


@dataclass(frozen=True)
class HealthBanner:
    degraded: bool
    label: str
    text: str


def health_state(report) -> str:
    """The one phrase both section headings use, so neither can disagree
    with the banner: degraded / usable, with gaps / all sources fresh or usable."""
    health = getattr(report, "health", None)
    if health is None:
        return ""
    if health.degraded:
        return "degraded"
    if getattr(report, "suppressed", None):
        return "usable, with gaps"
    return "all sources fresh or usable"


# suppressed feature -> what a reader loses, in their terms. A feature name
# is an implementation detail; "dynasty market values" is the thing that is
# missing from the page.
_CAPABILITY_NAMES = {
    "dynasty_values": "dynasty market values",
    "dynasty_rank_context": "dynasty rank context",
    "source_disagreement_dynasty": "dynasty source disagreement",
    "source_disagreement_redraft": "redraft source disagreement",
    "redraft_currency": "redraft player values",
    "replacement_value": "replacement-level analysis",
    "lineup_optimizer": "lineup optimisation",
    "matchup_leverage": "matchup leverage",
    "streamer_planner": "streaming plans",
    "schedule_windows": "schedule windows",
    "waiver_trending": "trending waiver adds",
    "roster_clog": "roster-clog analysis",
    "role_trends": "usage and role trends",
    "roster_analysis": "roster analysis",
    "team_status": "contender/rebuild status",
    "trade_engine": "trade proposals",
    "waiver_engine": "waiver targets",
}


def capability_name(feature: str) -> str:
    return _CAPABILITY_NAMES.get(feature, feature.replace("_", " "))


def health_banner(report) -> HealthBanner | None:
    """One line at the top of both outputs naming what is actually missing.

    Severity follows consequence, not noise. A source serving a validated
    snapshot after a failed refresh has not changed a single recommendation,
    and shouting about it teaches the reader to scroll past the banner that
    matters. The loud form is reserved for a run where something is
    genuinely suppressed; everything else is a note that says which numbers
    are a few hours old.

    The text names the capability lost and what still works, because "signals
    degraded" tells a reader to distrust the whole page when the truth is
    usually that one source of several is out.
    """
    health = getattr(report, "health", None)
    suppressed = sorted(getattr(report, "suppressed", {}) or {})
    if health is None or (not health.degraded and not suppressed):
        return None

    # A source that is optional by design (a paid CSV nobody exported) is not
    # what went wrong, and naming it put "FF Dynasty Pass unavailable" in the
    # banner of every single run — including the runs where KTC was the thing
    # that broke and went unmentioned.
    def matters(signal) -> bool:
        return not signal.expected_absent and signal.family not in OPTIONAL_FAMILIES

    # A fallback signal's label is Usable, which is not a degraded label — so
    # keying on the label alone could never name the case the "serving a
    # validated snapshot" wording was written for.
    stale = sorted(
        s.display_name for s in health.signals
        if matters(s) and s.label != UNAVAILABLE and (s.label in DEGRADED_LABELS or s.fallback)
    )
    gone = sorted(s.display_name for s in health.signals if matters(s) and s.label == UNAVAILABLE)

    # Severity is the health layer's own grade, never the banner's opinion:
    # the lead phrase below has to match the Signal health section this sits
    # above, and an expected-absent source (the season's usage file before
    # any game) can suppress a feature without the run being degraded.
    bits: list[str] = []
    if suppressed:
        lost = ", ".join(capability_name(f) for f in suppressed)
        bits.append(f"{lost} {'are' if len(suppressed) > 1 else 'is'} limited this run")
    if gone:
        bits.append(f"{', '.join(gone)} unavailable")
    if stale:
        bits.append(f"{', '.join(stale)} served from a validated cached snapshot")
    if not bits:
        bits.append("one or more sources unusable")
    if suppressed:
        # What survived is the half a reader cannot see from a warning, and
        # it is usually most of the page.
        working = sorted({s.display_name for s in health.signals if s.label in (FRESH, USABLE)})
        if working:
            shown = ", ".join(working[:4])
            more = f" and {len(working) - 4} more" if len(working) > 4 else ""
            bits.append(f"still available: {shown}{more}")
    elif health.degraded:
        bits.append("no recommendation was suppressed")

    lead = "Signal health: degraded" if health.degraded else "Signal health: usable, with gaps"
    # The label is where a reader learns which capability actually went;
    # "Signals degraded" told them to distrust a page that mostly works.
    label = (
        f"{capability_name(suppressed[0]).capitalize()} degraded" if suppressed
        else ("Signals degraded" if health.degraded else "Signals: gaps")
    )
    return HealthBanner(degraded=health.degraded, label=label, text=f"{lead} — " + "; ".join(bits) + ".")


def grouped_picks(assessments):
    """Picks that share a classification and a reason, as one row each:
    (classification, reason, [assessment, ...]) in the order they arrived.
    Twelve bullets drawn from two sentences is not twelve facts."""
    groups: dict[tuple[str, str], list] = {}
    for a in assessments:
        groups.setdefault((a.classification, a.reason), []).append(a)
    return [(cls, reason, items) for (cls, reason), items in groups.items()]


def pick_group_label(items) -> str:
    """"2026 Late 1st x3 (KTC 4,107 each)" or the single pick's own name."""
    names = [a.display_name for a in items]
    values = {a.pick.value for a in items if a.pick.value}
    if len(items) == 1:
        value = f" (KTC {items[0].pick.value:,})" if items[0].pick.value else ""
        return f"{names[0]}{value}"
    same = sorted(set(names))
    head = f"{same[0]} x{len(items)}" if len(same) == 1 else ", ".join(same)
    if len(values) == 1:
        return f"{head} (KTC {next(iter(values)):,} each)"
    return head


def common_schedule_line(leagues) -> str:
    """The schedule-window sentence when every drafted league's is the same
    (the usual case — the NFL calendar is the NFL calendar), else "". A
    fact printed once at the top is a fact; printed nine times it is
    furniture."""
    lines = {ld.windows.describe() for ld in leagues if getattr(ld, "windows", None) is not None}
    return lines.pop() if len(lines) == 1 else ""


# -- Waiver Command Center -----------------------------------------------------
#
# The claim plan is written by waiver_plan; these helpers only choose what
# each renderer shows on the visible line and what it hides behind a
# disclosure, so the Markdown report and the dashboard say the same thing.

MAX_CLAIM_WHY = 4
MAX_CLAIM_RISK = 3
NO_CLAIMS_NOTE = "Nothing on this wire is worth a claim for this roster right now."
NEEDS_SETTLED_NOTE = "no position group is short"


def needs_line(needs) -> str:
    """The position groups that are not Adequate, worst first; the settled
    note when every group is fine."""
    from sleeper_tool.roster_needs import ADEQUATE, NEED_ORDER

    short = [n for n in needs.groups.values() if n.label != ADEQUATE]
    short.sort(key=lambda n: NEED_ORDER[n.label])
    return " · ".join(f"{n.group} {n.label}" for n in short) or NEEDS_SETTLED_NOTE


def dependency_line(claim) -> str:
    """How a claim depends on the one before it, in words."""
    from sleeper_tool.waiver_plan import IF_PREVIOUS_FAILS, INDEPENDENT, ONLY_IF_DROP_AVAILABLE

    if claim.dependency == INDEPENDENT:
        return "Independently" if claim.order > 1 else ""
    if claim.dependency == IF_PREVIOUS_FAILS:
        return f"If claim {claim.depends_on} fails"
    if claim.dependency == ONLY_IF_DROP_AVAILABLE:
        return f"Only if claim {claim.depends_on} fails — same drop"
    return claim.dependency


def budget_facts(claim, center) -> list[str]:
    """What the league's own budgets say about a recommended bid: how many
    managers still hold more than it, and what claims have actually cost
    here this season. Facts about money, never a forecast of a bid."""
    if claim.window is None or center.faab_context is None:
        return []
    from sleeper_tool.faab_strategy import _anchor_text
    from sleeper_tool.faab_window import outbid_text

    return [t for t in (outbid_text(center.faab_context, claim.window.recommended), _anchor_text(center.faab_context.league_bids)) if t]


def bid_line(claim, center) -> str:
    """The money (or priority) line for one claim, in the league's own mode."""
    from sleeper_tool.waiver_plan import PRIORITY_MODE

    if center.mode == PRIORITY_MODE and claim.priority_call:
        where = f" (you are #{center.priority_position[0]} of {center.priority_position[1]})" if center.priority_position else ""
        return f"{claim.priority_call}{where}"
    if claim.window is not None:
        return f"{claim.window.text()} · {claim.window.posture}"
    return center.mode_note or "no bid advice for this league"


@dataclass(frozen=True)
class ClaimView:
    order: int
    dependency: str
    problem: str
    name: str
    position: str
    team: str
    strength: str
    cls: str
    add_line: str
    drop_line: str
    bid: str
    why: list[str]
    risks: list[str]
    notes: list[str]
    budget: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)  # "Devaughn Vele — $1–3, recommend $1"


def _problem_worth_printing(call) -> str:
    """The group's problem, unless it is only the acquisition class said a
    second time ("Upside Bench Add · Upside bench add"). It earns its place
    when it names a lineup hole ("RB2 upgrade (over Kenny Gainwell)")."""
    problem = call.problem or ""
    return "" if problem.lower() == (call.cls or "").lower() else problem


def claim_view(claim, center, *, backups=()) -> ClaimView:
    call = claim.call
    drop = claim.drop
    return ClaimView(
        order=claim.order,
        dependency=dependency_line(claim),
        problem=_problem_worth_printing(claim.call),
        name=call.entry.name,
        position=call.entry.position or "?",
        team=call.entry.team or "—",
        strength=call.strength,
        cls=call.cls or "",
        add_line=call.entry.name,
        drop_line=drop.name if drop is not None else ("no drop needed — open roster spot" if not call.needs_drop or claim.notes else "no drop needed"),
        bid=bid_line(claim, center),
        why=call.why[:MAX_CLAIM_WHY],
        risks=call.risks[:MAX_CLAIM_RISK],
        notes=list(claim.notes),
        budget=budget_facts(claim, center),
        backups=[f"{b.call.entry.name} — {bid_line(b, center)}" for b in backups],
    )


MATRIX_COLUMNS = ("Player", "Pos", "FP ROS", "Ballers", "A/J/M", "RotoBaller", "Boone", "Role", "Need", "Lineup Δ", "Call")


def matrix_row(call) -> tuple[str, ...]:
    """One consensus-matrix row,each source in its own units — nothing is
    converted into a shared scale."""
    ev = call.evidence
    delta = call.structural_gain if abs(call.structural_gain) >= abs(call.week_gain) else call.week_gain
    # A role label that says there is no role history yet is not a reading;
    # a lineup delta under a tenth of a point is not a change.
    role = ev.role_label if ev.role_label and ev.role_label != INSUFFICIENT_ROLE else "—"
    return (
        ev.name,
        ev.position or "?",
        ev.pos_label(ev.fp_ros_pos_rank),
        f"#{ev.ballers_rank}" if ev.ballers_rank is not None else "—",
        ev.hosts_text(),
        f"#{ev.rotoballer_rank}" if ev.rotoballer_rank is not None else "—",
        ev.pos_label(ev.boone_pos_rank),
        role,
        call.need.label if call.need is not None else "—",
        f"{delta:+.1f}" if abs(delta) >= 0.05 else "—",
        call.strength,
    )


DEEPER_LEAGUE_REASON = "deeper leagues"


def do_not_spend_lines(calls) -> list[str]:
    """One line per player, except the deeper-league passes: three rows all
    saying RotoBaller tags him for a bigger league is one fact, not three."""
    deeper = [c for c in calls if DEEPER_LEAGUE_REASON in (c.pass_reason or "")]
    rest = [c for c in calls if c not in deeper]
    lines = [do_not_spend_line(c) for c in rest]
    if len(deeper) == 1:
        lines.append(do_not_spend_line(deeper[0]))
    elif deeper:
        who = ", ".join(f"{c.entry.name} ({c.entry.position or '?'})" for c in deeper)
        lines.append(f"{who} — RotoBaller tags them for deeper leagues than this one, and none has a path onto this lineup")
    return lines


def do_not_spend_line(call) -> str:
    board = f"Ballers #{call.evidence.ballers_rank}" if call.evidence.ballers_rank is not None else (
        f"RotoBaller #{call.evidence.rotoballer_rank}" if call.evidence.rotoballer_rank is not None else "an expert board"
    )
    return f"{call.entry.name} ({call.entry.position or '?'}) — {board}: good player, poor claim for this roster: {call.pass_reason}"


def command_center_subtitle(center, mode) -> str:
    """"Tuesday — claims pending · week 2 claims · $95 FAAB left"."""
    bits = []
    if mode is not None and mode.active:
        bits.append(mode.subheader)
    if center.claim_week:
        bits.append(f"week {center.claim_week} claims")
    if center.remaining_budget is not None:
        bits.append(f"${center.remaining_budget} FAAB left")
    elif center.priority_position:
        bits.append(f"waiver priority #{center.priority_position[0]} of {center.priority_position[1]}")
    return " · ".join(bits)
