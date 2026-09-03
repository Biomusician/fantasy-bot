"""Is a player's role growing or shrinking, and can you see why?

Every label here decomposes into named component signals — snap share up
11 points, targets up 2.4 a game — so the report can show its work instead
of asserting a verdict. Nothing is fitted, smoothed or forecast: this says
what the last two or three games did against what came before them.

  Insufficient Role History  fewer than MIN_GAMES_FOR_TREND played games,
                             or no season has been played yet
  Stable Role                no component crossed its threshold
  Role Rising / Falling      more components moved one way than the other
  Role Surging / Collapsing  STRONG_COMPONENTS_FOR_EXTREME components at
                             STRONG_MULTIPLE x their threshold, or a
                             one-week structural snap change

**Windows.** The recent window is the last 3 played games once there are
GAMES_FOR_MEDIUM_WINDOW of them, otherwise the last 2; the baseline is
every played game before that window, never overlapping it. Byes don't
appear (see role_analysis) so a window is games, not calendar weeks.

**Two games is nearly nothing.** With exactly two played games the only
readable signal is a structural one: a snap share that moved at least
SURGE_ONE_WEEK_SNAP_JUMP in a single week is a coaching decision, not
noise, and can say Surging or Collapsing. Everything else with two games
is Stable Role. Rising/Falling need MIN_GAMES_FOR_STRONG games, because a
one-game spike is a game script, not a role.

**The market cross** compares this role label against the value/velocity/
disagreement labels the rest of the tool already produces, using only
those labels — never the report objects. It answers "has the price caught
up to the usage yet", and says nothing when either side is silent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import pstdev

from sleeper_tool.nfl_usage import UsageData
from sleeper_tool.role_analysis import (
    RoleWindow,
    played_rows,
    role_window_for_weeks,
    team_opportunity_leaders,
    window_from_rows,
)

INSUFFICIENT = "Insufficient Role History"
STABLE = "Stable Role"
RISING = "Role Rising"
SURGING = "Role Surging"
FALLING = "Role Falling"
COLLAPSING = "Role Collapsing"

NO_HISTORY_NOTE = "Role data begins after games are played"

MIN_GAMES_FOR_TREND = 2
MIN_GAMES_FOR_STRONG = 3
GAMES_FOR_MEDIUM_WINDOW = 5  # 3 recent + 2 baseline before last3-vs-rest is used

# Component thresholds. Shares are 0-1 fractions, so 0.10 is ten share
# points; the counting stats are per game. Falls are the same numbers
# negated, and every comparison is inclusive of the threshold itself.
SNAP_SHARE_RISE = 0.10
TARGET_SHARE_RISE = 0.05
CARRY_SHARE_RISE = 0.08
OPPORTUNITY_SHARE_RISE = 0.06
ABS_TARGETS_RISE = 2.0
ABS_CARRIES_RISE = 3.0

# A single week's snap share moving this far is a role change by decision,
# not variance — the one thing worth labelling off two games.
SURGE_ONE_WEEK_SNAP_JUMP = 0.30
STRONG_MULTIPLE = 2.0
STRONG_COMPONENTS_FOR_EXTREME = 2

# Population stdev of snap share across the last MEDIUM window games.
VOLATILITY_MIN_GAMES = 3
VOLATILITY_SNAP_STDEV = 0.15

# A teammate "overtook" only if this player actually gave something up and
# the teammate's gain covers the whole of that loss.
TEAMMATE_MIN_LOSS = 0.02
TEAMMATE_OVERTAKE_MIN_GAIN = 0.05

DESCRIBE_MAX_SIGNALS = 2

# Averages of decimal inputs land a hair under an exactly-equal threshold
# (0.6 - 0.5 == 0.09999999999999998), and float noise must not be what
# decides a label. Every threshold comparison is inclusive to within this.
THRESHOLD_EPSILON = 1e-9

ROLE_AHEAD = "Role Ahead of Market"
MARKET_AHEAD = "Market Ahead of Role"
CONFIRM = "Role and Market Confirm"

UP = "up"
DOWN = "down"
FLAT = "flat"
VOLATILE = "volatile"
OVERTAKEN = "overtaken"

_UP_TOKENS = {"rising", "rise", "risen", "surging", "surge", "up", "higher", "gaining", "buy", "positive"}
_DOWN_TOKENS = {"falling", "fall", "fallen", "collapsing", "collapse", "down", "lower", "declining", "decline", "dropping", "sell", "negative"}
_FLAT_TOKENS = {"stable", "flat", "unchanged", "steady", "neutral", "hold"}
_RISING_LABELS = {RISING, SURGING}
_FALLING_LABELS = {FALLING, COLLAPSING}


@dataclass(frozen=True)
class RoleSignal:
    name: str
    direction: str  # up / down / volatile / overtaken
    magnitude_text: str
    window: str

    def describe(self) -> str:
        return f"{self.name} {self.magnitude_text}"


@dataclass
class RoleTrend:
    gsis_id: str
    label: str
    components: list[RoleSignal] = field(default_factory=list)
    games: int = 0
    note: str | None = None
    history_available: bool = True

    @property
    def rising(self) -> bool:
        return self.label in _RISING_LABELS

    @property
    def falling(self) -> bool:
        return self.label in _FALLING_LABELS

    @property
    def notable(self) -> bool:
        return self.label in _RISING_LABELS | _FALLING_LABELS

    def describe(self) -> str:
        """One sparse line. Two components at most — this is an annotation
        on a recommendation, not a stat page."""
        if self.label == INSUFFICIENT:
            return f"{self.label}{f' ({self.note})' if self.note else ''}"
        head = f"{self.label} ({self.games} games)"
        shown = [c.describe() for c in self.components[:DESCRIBE_MAX_SIGNALS]]
        return f"{head}: {', '.join(shown)}" if shown else head


def _at_least(value: float, threshold: float) -> bool:
    """value >= threshold, inclusive of the boundary despite float noise."""
    return value >= threshold - THRESHOLD_EPSILON


def _share_text(delta: float) -> str:
    return f"{delta * 100:+.0f} pts"


def _per_game_text(delta: float) -> str:
    return f"{delta:+.1f}/g"


def _component(name: str, recent: float | None, baseline: float | None, threshold: float, window: str, formatter) -> tuple[RoleSignal | None, float]:
    """A signal when the move reaches the threshold, plus how many
    thresholds it is worth (used to decide Surging vs Rising)."""
    if recent is None or baseline is None:
        return None, 0.0
    delta = recent - baseline
    if _at_least(delta, threshold):
        return RoleSignal(name, UP, formatter(delta), window), delta / threshold
    if _at_least(-delta, threshold):
        return RoleSignal(name, DOWN, formatter(delta), window), delta / threshold
    return None, 0.0


def _split_windows(weeks: list[int]) -> tuple[list[int], list[int]]:
    """(recent weeks, baseline weeks) — never overlapping."""
    n = len(weeks)
    if n >= GAMES_FOR_MEDIUM_WINDOW:
        return weeks[-3:], weeks[:-3]
    if n >= MIN_GAMES_FOR_STRONG:
        return weeks[-2:], weeks[:-2]
    return weeks[-1:], weeks[:-1]


def _snap_volatility(usage: UsageData | None, gsis_id: str, weeks: list[int], window_text: str) -> RoleSignal | None:
    rows = [r for r in played_rows(usage, gsis_id) if r.week in set(weeks)]
    snaps = [r.snap_pct for r in rows if r.snap_pct is not None]
    if len(snaps) < VOLATILITY_MIN_GAMES:
        return None
    spread = pstdev([float(s) for s in snaps])
    if not _at_least(spread, VOLATILITY_SNAP_STDEV):
        return None
    return RoleSignal("snap volatility", VOLATILE, f"{spread * 100:.0f} pts stdev", window_text)


def teammate_overtaking(
    usage: UsageData | None,
    gsis_id: str,
    *,
    recent_weeks: list[int],
    baseline_weeks: list[int],
    team: str | None,
    position: str | None,
    loss: float,
    window: str,
) -> RoleSignal | None:
    """Did someone at his own position take what he lost?

    Only asked when he actually lost opportunity share; a teammate rising
    while he holds steady is the offense growing, not a demotion.
    """
    if usage is None or not _at_least(loss, TEAMMATE_MIN_LOSS) or not team or not position or not baseline_weeks:
        return None
    recent = {l.gsis_id: l for l in team_opportunity_leaders(usage, team, position=position, weeks=recent_weeks)}
    baseline = {l.gsis_id: l for l in team_opportunity_leaders(usage, team, position=position, weeks=baseline_weeks)}
    best: tuple[float, str] | None = None
    for other_id, leader in sorted(recent.items()):
        if other_id == gsis_id or leader.opportunity_share is None:
            continue
        before = baseline.get(other_id)
        before_share = before.opportunity_share if before is not None else 0.0
        if before_share is None:
            continue
        gain = leader.opportunity_share - before_share
        if _at_least(gain, loss) and _at_least(gain, TEAMMATE_OVERTAKE_MIN_GAIN) and (best is None or gain > best[0]):
            best = (gain, leader.name or other_id)
    if best is None:
        return None
    gain, name = best
    return RoleSignal("teammate overtaking", OVERTAKEN, f"{name} {_share_text(gain)}", window)


def role_trend(usage: UsageData | None, gsis_id: str | None) -> RoleTrend:
    """Label one player's role movement, with the components behind it."""
    key = gsis_id or ""
    if usage is None or not usage.latest_week:
        return RoleTrend(gsis_id=key, label=INSUFFICIENT, games=0, note=NO_HISTORY_NOTE, history_available=False)

    rows = played_rows(usage, key)
    games = len(rows)
    if games < MIN_GAMES_FOR_TREND:
        note = f"{games} played game{'' if games == 1 else 's'} of {MIN_GAMES_FOR_TREND} needed"
        return RoleTrend(gsis_id=key, label=INSUFFICIENT, games=games, note=note)

    weeks = [r.week for r in rows]
    recent_weeks, baseline_weeks = _split_windows(weeks)
    window = f"last {len(recent_weeks)} vs prior {len(baseline_weeks)}"
    recent = window_from_rows(usage, [r for r in rows if r.week in set(recent_weeks)])
    baseline = role_window_for_weeks(usage, key, baseline_weeks)

    snap_delta = None
    if recent.snap_pct is not None and baseline.snap_pct is not None:
        snap_delta = recent.snap_pct - baseline.snap_pct

    if games < MIN_GAMES_FOR_STRONG:
        return _two_game_trend(key, games, snap_delta, window)

    components: list[RoleSignal] = []
    strengths: list[float] = []
    for name, recent_v, base_v, threshold, formatter in (
        ("snap share", recent.snap_pct, baseline.snap_pct, SNAP_SHARE_RISE, _share_text),
        ("target share", recent.target_share, baseline.target_share, TARGET_SHARE_RISE, _share_text),
        ("carry share", recent.carry_share, baseline.carry_share, CARRY_SHARE_RISE, _share_text),
        ("opportunity share", recent.opportunity_share, baseline.opportunity_share, OPPORTUNITY_SHARE_RISE, _share_text),
        ("targets", recent.targets, baseline.targets, ABS_TARGETS_RISE, _per_game_text),
        ("carries", recent.carries, baseline.carries, ABS_CARRIES_RISE, _per_game_text),
    ):
        signal, strength = _component(name, recent_v, base_v, threshold, window, formatter)
        if signal is not None:
            components.append(signal)
            strengths.append(strength)

    volatility = _snap_volatility(usage, key, recent_weeks, window)
    overtaking = _overtaking_signal(usage, key, rows, recent, baseline, recent_weeks, baseline_weeks, window)

    label, note = _label_from(components, strengths, snap_delta, games)
    if volatility is not None:
        components.append(volatility)
    if overtaking is not None:
        components.append(overtaking)
    return RoleTrend(gsis_id=key, label=label, components=components, games=games, note=note)


def _overtaking_signal(usage, key, rows, recent: RoleWindow, baseline: RoleWindow, recent_weeks, baseline_weeks, window) -> RoleSignal | None:
    if recent.opportunity_share is None or baseline.opportunity_share is None:
        return None
    loss = baseline.opportunity_share - recent.opportunity_share
    latest_row = rows[-1]
    return teammate_overtaking(
        usage,
        key,
        recent_weeks=recent_weeks,
        baseline_weeks=baseline_weeks,
        team=latest_row.team,
        position=latest_row.position,
        loss=loss,
        window=window,
    )


def _two_game_trend(key: str, games: int, snap_delta: float | None, window: str) -> RoleTrend:
    """Exactly MIN_GAMES_FOR_TREND games: a structural snap move or nothing."""
    if snap_delta is not None and _at_least(snap_delta, SURGE_ONE_WEEK_SNAP_JUMP):
        signal = RoleSignal("snap share", UP, _share_text(snap_delta), window)
        return RoleTrend(gsis_id=key, label=SURGING, components=[signal], games=games, note="one-week structural snap change")
    if snap_delta is not None and _at_least(-snap_delta, SURGE_ONE_WEEK_SNAP_JUMP):
        signal = RoleSignal("snap share", DOWN, _share_text(snap_delta), window)
        return RoleTrend(gsis_id=key, label=COLLAPSING, components=[signal], games=games, note="one-week structural snap change")
    return RoleTrend(gsis_id=key, label=STABLE, games=games, note=f"{games} games: only a structural snap change is readable")


def _label_from(components: list[RoleSignal], strengths: list[float], snap_delta: float | None, games: int) -> tuple[str, str | None]:
    if snap_delta is not None and _at_least(snap_delta, SURGE_ONE_WEEK_SNAP_JUMP):
        return SURGING, "structural snap change"
    if snap_delta is not None and _at_least(-snap_delta, SURGE_ONE_WEEK_SNAP_JUMP):
        return COLLAPSING, "structural snap change"

    ups = [s for s in strengths if s > 0]
    downs = [s for s in strengths if s < 0]
    if len(ups) == len(downs):
        note = "components disagree" if components else None
        return STABLE, note
    strong_ups = sum(1 for s in ups if _at_least(s, STRONG_MULTIPLE))
    strong_downs = sum(1 for s in downs if _at_least(-s, STRONG_MULTIPLE))
    if len(ups) > len(downs):
        if games >= MIN_GAMES_FOR_STRONG and strong_ups >= STRONG_COMPONENTS_FOR_EXTREME:
            return SURGING, None
        return RISING, "mixed, net up" if downs else None
    if games >= MIN_GAMES_FOR_STRONG and strong_downs >= STRONG_COMPONENTS_FOR_EXTREME:
        return COLLAPSING, None
    return FALLING, "mixed, net down" if ups else None


# -- market cross ------------------------------------------------------------


def _direction(text: str | None) -> str | None:
    """A label from anywhere in the tool -> up / down / flat / None.
    Tokenised rather than substring-matched so "Unmeasurable" doesn't
    accidentally read as a direction."""
    if not text:
        return None
    lowered = text.strip().lower()
    if lowered in {"no change", "none"}:
        return FLAT
    tokens = {t for t in re.split(r"[^a-z]+", lowered) if t}
    if tokens & _DOWN_TOKENS:
        return DOWN
    if tokens & _UP_TOKENS:
        return UP
    if tokens & _FLAT_TOKENS:
        return FLAT
    return None


def _role_direction(label: str) -> str | None:
    if label in _RISING_LABELS:
        return UP
    if label in _FALLING_LABELS:
        return DOWN
    if label == STABLE:
        return FLAT
    return None


def market_cross(trend: RoleTrend, *, value_direction: str | None, velocity_label: str | None, source_direction: str | None) -> str | None:
    """Where the usage sits relative to the price.

      Role and Market Confirm  both moved the same way
      Role Ahead of Market     the role moved and the price didn't — or is
                               still moving the other way
      Market Ahead of Role     the price moved on something other than
                               usage (news, hype, a name)
      None                     one side has nothing to say: no role
                               history, no market labels, the market's own
                               labels disagree, or neither side moved

    Takes labels, never report objects, so nothing here depends on the
    shape of a proposal or a waiver row.
    """
    role_dir = _role_direction(trend.label)
    if role_dir is None:
        return None
    dirs = [d for d in (_direction(value_direction), _direction(velocity_label), _direction(source_direction)) if d is not None]
    if not dirs:
        return None
    ups, downs = dirs.count(UP), dirs.count(DOWN)
    if ups and downs:
        return None  # the market itself is split; no cross to report
    market_dir = UP if ups else DOWN if downs else FLAT

    if role_dir == FLAT and market_dir == FLAT:
        return None
    if role_dir == FLAT:
        return MARKET_AHEAD
    if role_dir == market_dir:
        return CONFIRM
    return ROLE_AHEAD


def prior_season_baseline(usage_prior: UsageData | None, gsis_id: str | None) -> str | None:
    """An explicitly labelled prior-season line, e.g.
    "2025 baseline: 71% snaps, 18% target share over 14 games".

    Kept out of every trend label on purpose: last year is context for a
    reader, not evidence about this year's role.
    """
    if usage_prior is None or not gsis_id:
        return None
    rows = played_rows(usage_prior, gsis_id)
    if not rows:
        return None
    window = window_from_rows(usage_prior, rows)
    parts: list[str] = []
    if window.snap_pct is not None:
        parts.append(f"{window.snap_pct:.0%} snaps")
    if window.target_share:  # a QB's 0% target share is noise, not context
        parts.append(f"{window.target_share:.0%} target share")
    if not parts:
        return None
    return f"{usage_prior.season} baseline: {', '.join(parts)} over {window.games} games"
