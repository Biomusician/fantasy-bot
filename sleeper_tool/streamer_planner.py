"""Streaming planner — for the streamable positions this league starts
(QB, TE, K, DEF), the best plan over the next PLAN_WEEKS weeks from the
league's own free-agent pool:

  one player        the single player (rostered or free agent) with the
                    highest projected total over the window
  two-player        the best "player A, then switch to player B in week
  sequence          k" — one roster slot, one add/drop mid-window

Weekly projection is the player's per-game projection in this league's
scoring (0 in a bye week from the NFL schedule, or on a long-term
unavailability). No opponent-strength or points-allowed adjustment is
applied — none is available here, and none is invented.

The one-player plan is preferred whenever it's within
SINGLE_PREFERENCE_TOLERANCE of the sequence: a second transaction is real
friction (a waiver claim, FAAB, a drop) and the projections aren't precise
enough to chase 3%. Neither plan is suggested at all unless it clears
MIN_GAIN_OVER_HOLD over simply keeping the current starter. Pre-draft
leagues never get here (report_data hands over an empty pool).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sleeper_tool.lineup_optimizer import LineupResult, slot_eligibility, starter_slots_for, unavailability_reason
from sleeper_tool.nfl_schedule import Schedule
from sleeper_tool.roster_analysis import RosterEntry, ValuedRoster
from sleeper_tool.valuation import weekly_projection

STREAM_POSITIONS = ("QB", "TE", "K", "DEF")
PLAN_WEEKS = 3
SINGLE_PREFERENCE_TOLERANCE = 0.08  # one-player plan wins if within 8% of the best sequence
MIN_GAIN_OVER_HOLD = 3.0  # projected points over the whole window (about a point a week)
MAX_FREE_AGENTS_PER_POSITION = 8  # the sequence search is quadratic in candidates

HOLD = "Hold"
ADD = "Add"
SEQUENCE = "Sequence"


@dataclass
class WeekLine:
    week: int
    projection: float
    note: str | None = None  # "bye", or an unavailability reason


@dataclass
class StreamOption:
    entry: RosterEntry
    rostered: bool
    weeks: list[WeekLine]
    total: float

    def week_text(self) -> str:
        return ", ".join(f"wk{w.week} {w.note}" if w.note else f"wk{w.week} {w.projection:.1f}" for w in self.weeks)


@dataclass
class StreamSequence:
    first: StreamOption
    second: StreamOption
    switch_week: int
    total: float


@dataclass
class StreamPlan:
    position: str
    weeks: list[int]
    current: StreamOption | None  # my current starter at the position, if any
    single: StreamOption  # best one-player plan
    sequence: StreamSequence | None  # best two-player plan (always computed; recommended only when it clearly wins)
    recommendation: str  # HOLD / ADD / SEQUENCE
    note: str
    candidates: list[StreamOption] = field(default_factory=list)

    def describe(self) -> str:
        window = f"weeks {self.weeks[0]}-{self.weeks[-1]}" if len(self.weeks) > 1 else f"week {self.weeks[0]}"
        if self.recommendation == HOLD:
            return f"{self.position} ({window}): Hold — {self.note}"
        if self.recommendation == ADD:
            return f"{self.position} ({window}): Add {self.single.entry.name} — {self.note}"
        s = self.sequence
        return (
            f"{self.position} ({window}): {s.first.entry.name} then {s.second.entry.name} from week {s.switch_week} — {self.note}"
        )


def _week_line(entry: RosterEntry, week: int, per_game: float, schedule: Schedule | None, current_week: int | None) -> WeekLine:
    # The NFL schedule is the authority on byes when we have it; without
    # it, unavailability_reason falls back to the ranking sources' bye week.
    on_bye = schedule.is_bye(entry.team, week) if schedule is not None else entry.value.bye_week == week
    if on_bye:
        return WeekLine(week, 0.0, "bye")
    reason = unavailability_reason(entry, None, exclude_game_day_out=(week == current_week))
    if reason:
        return WeekLine(week, 0.0, reason)
    return WeekLine(week, per_game)


def _option(entry: RosterEntry, rostered: bool, weeks: list[int], schedule: Schedule | None, current_week: int | None) -> StreamOption | None:
    per_game = weekly_projection(entry.value, current_week)
    if per_game is None:
        return None
    lines = [_week_line(entry, w, per_game, schedule, current_week) for w in weeks]
    return StreamOption(entry, rostered, lines, round(sum(line.projection for line in lines), 1))


def _startable_stream_positions(roster: ValuedRoster) -> list[str]:
    eligible: set[str] = set()
    for slot in starter_slots_for(roster):
        eligible |= slot_eligibility(slot)
    return [p for p in STREAM_POSITIONS if p in eligible]


def _current_starter(roster: ValuedRoster, lineup: LineupResult | None, position: str) -> RosterEntry | None:
    """The lowest-projected starter at the position — in Superflex that's
    the QB in the flex slot, the one a streamer would replace."""
    if lineup is None:
        return None
    by_id = {e.player_id: e for e in roster.entries}
    starters = [(a.projection, by_id[a.player_id]) for a in lineup.assignments if a.player_id in by_id and by_id[a.player_id].position == position]
    if not starters:
        return None
    return min(starters, key=lambda t: t[0])[1]


def _best_sequence(options: list[StreamOption], weeks: list[int]) -> StreamSequence | None:
    best: StreamSequence | None = None
    for a in options:
        for b in options:
            if a is b:
                continue
            for i in range(1, len(weeks)):
                total = round(sum(w.projection for w in a.weeks[:i]) + sum(w.projection for w in b.weeks[i:]), 1)
                if best is None or total > best.total:
                    best = StreamSequence(a, b, weeks[i], total)
    return best


def plan_streams(
    my_roster: ValuedRoster,
    free_agents: list[RosterEntry],
    *,
    schedule: Schedule | None,
    current_week: int | None,
    lineup: LineupResult | None,
) -> list[StreamPlan]:
    if not free_agents or not current_week:
        return []
    weeks = list(range(current_week, current_week + PLAN_WEEKS))
    if schedule is not None:
        weeks = [w for w in weeks if w in schedule.regular_weeks()]
    if not weeks:
        return []

    plans: list[StreamPlan] = []
    for pos in _startable_stream_positions(my_roster):
        mine = [e for e in my_roster.entries if e.position == pos and not e.is_taxi]
        fas = sorted(
            (fa for fa in free_agents if fa.position == pos and fa.value.proj_points is not None),
            key=lambda e: (-e.value.proj_points, e.name),
        )[:MAX_FREE_AGENTS_PER_POSITION]
        options = [o for e in mine if (o := _option(e, True, weeks, schedule, current_week))]
        options += [o for e in fas if (o := _option(e, False, weeks, schedule, current_week))]
        if not options or not any(not o.rostered for o in options):
            continue
        current_entry = _current_starter(my_roster, lineup, pos)
        current = next((o for o in options if current_entry is not None and o.entry.player_id == current_entry.player_id), None)
        # Deterministic: highest total, then a rostered player, then name.
        single = sorted(options, key=lambda o: (-o.total, not o.rostered, o.entry.name))[0]
        sequence = _best_sequence(options, weeks)
        hold_total = current.total if current is not None else 0.0
        best_total = max(single.total, sequence.total if sequence else 0.0)

        starter_ids = set(lineup.starter_ids) if lineup is not None else set()
        sequence_wins = (
            sequence is not None and sequence.total - hold_total >= MIN_GAIN_OVER_HOLD
            and single.total < sequence.total * (1 - SINGLE_PREFERENCE_TOLERANCE)
            and not (sequence.first.rostered and sequence.second.rostered)  # two rostered legs is a start/sit call, not a stream
        )
        if (single.rostered and not sequence_wins) or best_total - hold_total < MIN_GAIN_OVER_HOLD:
            if current is None:
                note = "no starter at the position and nothing on waivers clears the bar"
            elif single.rostered and single.entry.player_id not in starter_ids:
                note = f"your own {single.entry.name} projects best over the window ({single.total:.1f}); start him over {current.entry.name}"
            elif single.rostered and single.entry.player_id != current.entry.player_id:
                note = f"your starters already project best over the window ({single.entry.name} {single.total:.1f}); no free agent beats {current.entry.name} by {MIN_GAIN_OVER_HOLD:g}+"
            elif single.rostered:
                note = f"{current.entry.name} projects best over the window ({single.total:.1f}); no free agent adds {MIN_GAIN_OVER_HOLD:g}+"
            else:
                note = (
                    f"{current.entry.name} projects {current.total:.1f} over the window; the best free agent, {single.entry.name}, "
                    f"reaches {single.total:.1f}, under the {MIN_GAIN_OVER_HOLD:g}-point bar"
                )
            plans.append(StreamPlan(pos, weeks, current, single, sequence, HOLD, note, options))
            continue
        if sequence_wins:
            gain = sequence.total - hold_total
            note = (
                f"{sequence.total:.1f} over the window vs {hold_total:.1f} holding ({gain:+.1f}); "
                f"the best single player gets {single.total:.1f} — the switch is worth the second move"
            )
            plans.append(StreamPlan(pos, weeks, current, single, sequence, SEQUENCE, note, options))
        else:
            gain = single.total - hold_total
            seq_note = f"; a two-player sequence would only reach {sequence.total:.1f}" if sequence and sequence.total > single.total else ""
            note = f"{single.total:.1f} over the window vs {hold_total:.1f} holding ({gain:+.1f}){seq_note}"
            plans.append(StreamPlan(pos, weeks, current, single, sequence, ADD, note, options))
    return plans
