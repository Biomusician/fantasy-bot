"""One honest answer to "how much should I trust today's report?".

Every input this tool reads — three ranking scrapers, the nflverse schedule
and usage feeds, Sleeper's own tables, an optional manual CSV — can be
missing, old, partially parsed, or served out of a cache after a failed
re-fetch, and until now each of those failure modes surfaced (if at all) as
a raw age in hours next to a source name. A reader has no way to know that
"KTC · 91h" means the dynasty numbers are a week behind the market while
"NFL schedule · 91h" means nothing at all is wrong.

This module turns each input into a `SignalHealth` with a label — Fresh,
Usable, Partial, Stale, Unavailable — computed against that source's own
windows (`rankings/freshness.py`), and rolls them up into a
`SignalHealthReport` that says which whole families are gone. From there,
`suppressed_features` names the features that should not be rendered at all
because the data they rest on isn't there.

Nothing here fetches. It reads what other layers already loaded, so it is
safe to call at any point in a run and costs one SQL query per Sleeper
family. The orchestrator wires the result into the renderers and into
feature suppression; this module makes no decisions of its own about what
to print.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sleeper_tool.rankings import cache as ranking_cache
from sleeper_tool.rankings.ff_dynasty_pass import ff_dynasty_status
from sleeper_tool.rankings.freshness import MIN_COVERAGE, SOURCE_WINDOWS

__all__ = [
    "FRESH",
    "USABLE",
    "PARTIAL",
    "STALE",
    "UNAVAILABLE",
    "SOURCE_WINDOWS",
    "MIN_COVERAGE",
    "FEATURE_REQUIREMENTS",
    "SignalHealth",
    "SignalHealthReport",
    "label_for",
    "build_health",
    "suppressed_features",
    "freshness_lines",
]

FRESH = "Fresh"
USABLE = "Usable"
PARTIAL = "Partial"
STALE = "Stale"
UNAVAILABLE = "Unavailable"

# Labels that mean the report should say something about its own inputs.
DEGRADED_LABELS = (PARTIAL, STALE, UNAVAILABLE)

# Families that are optional by design: nothing in FEATURE_REQUIREMENTS
# depends on them, so their absence is the normal state, not a fault. They
# still appear in the source list with their own label — they just never
# make the run "degraded", which would otherwise be true on every run and
# so mean nothing.
OPTIONAL_FAMILIES = frozenset({"ff_dynasty_pass"})

# Sleeper's SQLite tables, grouped by how fast they turn over. League-shaped
# rows (settings, rosters, users, traded picks) are re-pulled every sync and
# drift slowly; weekly rows are the ones that make this week's advice.
SLEEPER_LEAGUE_TABLES = ("leagues", "rosters", "league_users", "traded_picks")
SLEEPER_WEEKLY_TABLES = ("matchups", "transactions", "trending")

# feature -> families it cannot be computed without. A feature is suppressed
# when ANY required family is Unavailable; a merely Stale family still
# produces output, flagged with its age.
FEATURE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "dynasty_values": ("ktc",),
    "source_disagreement": ("ktc", "fantasypros"),
    "redraft_currency": ("rotoballer", "fantasypros"),
    "replacement_value": ("rotoballer", "fantasypros"),
    "lineup_optimizer": ("rotoballer", "fantasypros"),
    "matchup_leverage": ("rotoballer", "fantasypros"),
    "streamer_planner": ("rotoballer", "fantasypros"),
    "schedule_windows": ("nflverse_schedule",),
    "waiver_trending": ("sleeper_weekly",),
    "roster_clog": ("sleeper_weekly",),
    "role_trends": ("nflverse_usage",),
    "roster_analysis": ("sleeper_league", "sleeper_players"),
    "team_status": ("sleeper_league", "sleeper_players"),
    "trade_engine": ("sleeper_league", "sleeper_players"),
    "waiver_engine": ("sleeper_league", "sleeper_players"),
}

_DISPLAY_PREFIXES = {
    "ktc_": "KTC ",
    "fantasypros_": "FantasyPros ",
    "rotoballer_": "RotoBaller ",
}
_DISPLAY_EXACT = {
    # Bare family names, used when a whole family is missing and there are no
    # per-list sources to name.
    "ktc": "KTC",
    "fantasypros": "FantasyPros",
    "rotoballer": "RotoBaller",
    "nflverse_schedule": "NFL schedule",
    "nflverse_usage": "NFL usage",
    "sleeper_players": "Sleeper players",
    "sleeper_league": "Sleeper leagues",
    "sleeper_weekly": "Sleeper weekly",
    "ff_dynasty_pass": "FF Dynasty Pass",
}


def label_for(
    age: dt.timedelta | None,
    windows: tuple[dt.timedelta, dt.timedelta, dt.timedelta] | None,
    coverage: int | None = None,
    floor: int | None = None,
    parse_ok: bool = True,
    fallback: bool = False,
) -> str:
    """Grade one source. `windows` is (fresh, usable, ceiling).

    Boundaries are inclusive downward: an age exactly equal to a window is
    still inside it, so `age == ceiling` is Stale rather than Unavailable
    and matches what the cache layer will still serve.

    A snapshot served from a fallback (a failed re-fetch fell back to cache)
    is never Fresh even if it is young — the source itself is currently
    down, and calling that Fresh would hide exactly the thing worth knowing.
    """
    if windows is None or age is None or not parse_ok:
        return UNAVAILABLE
    fresh, usable, ceiling = windows
    if age > ceiling:
        return UNAVAILABLE
    if age > usable:
        return STALE
    label = USABLE if age > fresh else FRESH
    if fallback and label == FRESH:
        label = USABLE
    # A short list is a different problem from an old one: the fetch worked
    # and the data is current, we just didn't get all of it. Only downgrade
    # something that was otherwise fine — "Stale AND short" is still Stale.
    if floor is not None and coverage is not None and coverage < floor:
        return PARTIAL
    return label


@dataclass
class SignalHealth:
    source: str
    family: str
    fetched_at: dt.datetime | None = None
    published_at: dt.datetime | None = None  # when the source says its data is from, if it says
    latest_week: int | None = None  # newest NFL week present, for week-indexed feeds
    cache_age: dt.timedelta | None = None
    parse_ok: bool = True
    coverage: int | None = None  # rows/players actually loaded
    fallback: bool = False
    label: str = UNAVAILABLE
    detail: str = ""
    # True when the source's absence is its normal state right now (the
    # season's usage file before any game is played): still Unavailable,
    # still suppresses what needs it, but never grades the run degraded.
    expected_absent: bool = False

    @property
    def display_name(self) -> str:
        return _display_name(self.source)


@dataclass
class SignalHealthReport:
    signals: list[SignalHealth] = field(default_factory=list)
    degraded: bool = False
    unavailable_families: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def by_family(self, family: str) -> list[SignalHealth]:
        return [s for s in self.signals if s.family == family]

    def describe(self) -> str:
        header = "Signal health: degraded" if self.degraded else "Signal health: all sources fresh"
        if self.unavailable_families:
            header += f" — unavailable: {', '.join(sorted(self.unavailable_families))}"
        lines = [header]
        lines.extend(f"  {line}" for line in freshness_lines(self))
        lines.extend(f"  ! {note}" for note in self.notes)
        return "\n".join(lines)


def _display_name(source: str) -> str:
    if source in _DISPLAY_EXACT:
        return _DISPLAY_EXACT[source]
    for prefix, label in _DISPLAY_PREFIXES.items():
        if source.startswith(prefix):
            return label + source[len(prefix):].replace("_", " ")
    return source.replace("_", " ")


def _format_age(age: dt.timedelta | None) -> str:
    if age is None:
        return "no data"
    hours = age.total_seconds() / 3600
    if hours < 0:
        return "0.0h"  # a clock skew shouldn't render as "-2.0h"
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


def _age(now: dt.datetime, fetched_at: dt.datetime | None) -> dt.timedelta | None:
    """Age, tolerant of a naive timestamp on either side.

    Everything this reads stamps UTC-aware times, but this module exists to
    report on broken inputs — it must not itself raise on one. A naive
    datetime is read as UTC rather than blowing up the whole run.
    """
    if fetched_at is None:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now - fetched_at


def _unavailable(source: str, family: str, detail: str) -> SignalHealth:
    return SignalHealth(source=source, family=family, label=UNAVAILABLE, detail=detail)


def _snapshot_signal(
    source: str,
    family: str,
    snapshot,
    now: dt.datetime,
) -> SignalHealth:
    """Grade one RankingSnapshot-shaped object (source, fetched_at, payload)."""
    fetched_at = getattr(snapshot, "fetched_at", None)
    age = _age(now, fetched_at)
    payload = getattr(snapshot, "payload", None)
    coverage = _payload_size(payload)
    # Read through the module, not a `from ... import` binding: the registry
    # is replaced wholesale in tests and rebound per process.
    fallback = bool(getattr(snapshot, "served_from_fallback", False)) or (
        ranking_cache.last_fetch_outcome.get(source) == "fallback"
    )
    # An empty or non-sized payload means the scraper returned something the
    # readers can't index — as bad as no fetch at all, so it fails parse.
    parse_ok = bool(coverage)
    label = label_for(
        age,
        SOURCE_WINDOWS.get(family),
        coverage=coverage,
        floor=MIN_COVERAGE.get(family),
        parse_ok=parse_ok,
        fallback=fallback,
    )
    details = []
    if not parse_ok:
        details.append("payload empty or unreadable")
    if fallback:
        details.append("served from cache after a failed re-fetch")
    if label == PARTIAL:
        details.append(f"{coverage} rows, below the {MIN_COVERAGE.get(family)} floor")
    return SignalHealth(
        source=source,
        family=family,
        fetched_at=fetched_at,
        cache_age=age,
        parse_ok=parse_ok,
        coverage=coverage,
        fallback=fallback,
        label=label,
        detail="; ".join(details),
    )


def _payload_size(payload) -> int | None:
    """Row count of a cache payload. The ranking sources cache a list; the
    schedule caches {"season": ..., "rows": [...]}."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        rows = payload.get("rows")
        return len(rows) if isinstance(rows, list) else None
    try:
        return len(payload)
    except TypeError:
        return None


def _ranking_signals(engine, now: dt.datetime) -> list[SignalHealth]:
    if engine is None:
        return [
            _unavailable("ktc_dynasty", "ktc", "no valuation engine supplied"),
            _unavailable("fantasypros", "fantasypros", "no valuation engine supplied"),
            _unavailable("rotoballer", "rotoballer", "no valuation engine supplied"),
        ]
    signals: list[SignalHealth] = []
    ktc_snapshot = getattr(engine, "ktc_snapshot", None)
    if ktc_snapshot is None:
        signals.append(_unavailable("ktc_dynasty", "ktc", "engine has no KTC snapshot"))
    else:
        signals.append(_snapshot_signal("ktc_dynasty", "ktc", ktc_snapshot, now))

    for family, attr, prefix in (
        ("fantasypros", "fp_snapshots", "fantasypros_"),
        ("rotoballer", "rb_snapshots", "rotoballer_"),
    ):
        snapshots = getattr(engine, attr, None) or {}
        if not snapshots:
            signals.append(_unavailable(family, family, f"engine has no {family} snapshots"))
            continue
        for key, snapshot in snapshots.items():
            signals.append(_snapshot_signal(f"{prefix}{key}", family, snapshot, now))
    return signals


def _sleeper_signals(storage, now: dt.datetime) -> list[SignalHealth]:
    if storage is None:
        return [
            _unavailable("sleeper_players", "sleeper_players", "no storage supplied"),
            _unavailable("sleeper_league", "sleeper_league", "no storage supplied"),
            _unavailable("sleeper_weekly", "sleeper_weekly", "no storage supplied"),
        ]

    signals: list[SignalHealth] = []

    players_at = storage.players_last_updated()
    player_count = storage.player_count()
    players_age = _age(now, players_at)
    players_label = label_for(
        players_age,
        SOURCE_WINDOWS.get("sleeper_players"),
        coverage=player_count,
        floor=MIN_COVERAGE.get("sleeper_players"),
        parse_ok=bool(player_count),
    )
    signals.append(
        SignalHealth(
            source="sleeper_players",
            family="sleeper_players",
            fetched_at=players_at,
            cache_age=players_age,
            parse_ok=bool(player_count),
            coverage=player_count,
            label=players_label,
            detail="" if players_at else "players have never been cached",
        )
    )

    for family, tables in (
        ("sleeper_league", SLEEPER_LEAGUE_TABLES),
        ("sleeper_weekly", SLEEPER_WEEKLY_TABLES),
    ):
        fetched_at = storage.latest_fetched_at(*tables)
        age = _age(now, fetched_at)
        coverage = sum(storage.row_count(t) for t in tables)
        signals.append(
            SignalHealth(
                source=family,
                family=family,
                fetched_at=fetched_at,
                cache_age=age,
                parse_ok=bool(coverage),
                coverage=coverage,
                label=label_for(age, SOURCE_WINDOWS.get(family), parse_ok=bool(coverage)),
                detail="" if fetched_at else f"no rows in {', '.join(tables)}",
            )
        )
    return signals


def _schedule_signal(schedule_snapshot, now: dt.datetime) -> SignalHealth:
    if schedule_snapshot is None:
        return _unavailable(
            "nflverse_schedule", "nflverse_schedule", "no cached nflverse schedule"
        )
    return _snapshot_signal("nflverse_schedule", "nflverse_schedule", schedule_snapshot, now)


def _usage_signal(usage_health, now: dt.datetime) -> SignalHealth:
    """The nflverse weekly-usage feed, read duck-typed so this module doesn't
    depend on the module that owns it (fetched_at, latest_week, rows,
    absent, stale)."""
    if usage_health is None:
        return _unavailable("nflverse_usage", "nflverse_usage", "no usage health supplied")
    if getattr(usage_health, "absent", False):
        signal = _unavailable("nflverse_usage", "nflverse_usage", "not published for this season yet")
        signal.expected_absent = True
        return signal

    fetched_at = getattr(usage_health, "fetched_at", None)
    age = _age(now, fetched_at)
    rows = getattr(usage_health, "rows", None)
    latest_week = getattr(usage_health, "latest_week", None)
    label = label_for(
        age, SOURCE_WINDOWS.get("nflverse_usage"), parse_ok=bool(rows)
    )
    details = []
    # The owning module can know the feed is behind the current NFL week even
    # when the file itself was downloaded minutes ago — trust it over the age.
    if getattr(usage_health, "stale", False):
        details.append("source reports its own data as behind the current week")
        if label in (FRESH, USABLE):
            label = STALE
    if latest_week is not None:
        details.append(f"through week {latest_week}")
    return SignalHealth(
        source="nflverse_usage",
        family="nflverse_usage",
        fetched_at=fetched_at,
        latest_week=latest_week,
        cache_age=age,
        parse_ok=bool(rows),
        coverage=rows,
        label=label,
        detail="; ".join(details),
    )


def _ff_signal() -> SignalHealth:
    """The manual Dynasty Pass CSV. ff_dynasty_status() already encodes the
    policy (fresh / stale-and-ignored / absent) as prose; this maps that one
    string onto the shared labels rather than re-deriving the file age."""
    try:
        status = ff_dynasty_status()
    except OSError as exc:
        return _unavailable("ff_dynasty_pass", "ff_dynasty_pass", f"unreadable: {exc}")
    if status.startswith("fresh"):
        label = FRESH
    elif status.startswith("stale"):
        label = STALE
    else:
        label = UNAVAILABLE
    return SignalHealth(
        source="ff_dynasty_pass",
        family="ff_dynasty_pass",
        label=label,
        parse_ok=label != UNAVAILABLE,
        detail=status,
    )


def build_health(
    *,
    engine=None,
    storage=None,
    schedule_snapshot=None,
    usage_health=None,
    now: dt.datetime | None = None,
) -> SignalHealthReport:
    """Grade every input this run had. Everything is optional and a missing
    input grades Unavailable — this must never be the thing that fails a run.

    `schedule_snapshot` is passed in rather than loaded here (the caller has
    it from `rankings.cache.load_snapshot("nflverse_schedule")`) so that
    grading stays free of disk I/O and of any chance of triggering a fetch.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    signals: list[SignalHealth] = []
    signals.extend(_ranking_signals(engine, now))
    signals.extend(_sleeper_signals(storage, now))
    signals.append(_schedule_signal(schedule_snapshot, now))
    signals.append(_usage_signal(usage_health, now))
    signals.append(_ff_signal())
    signals.sort(key=lambda s: (s.family, s.source))

    families: dict[str, list[SignalHealth]] = {}
    for signal in signals:
        families.setdefault(signal.family, []).append(signal)
    # A family is only gone when every source in it is gone. FantasyPros
    # publishes seven lists; losing the superflex cheatsheet doesn't mean the
    # dynasty consensus is missing too.
    unavailable_families = {
        family
        for family, members in families.items()
        if all(s.label == UNAVAILABLE for s in members)
    }

    notes: list[str] = []
    for family in sorted(unavailable_families - OPTIONAL_FAMILIES):
        members = families[family]
        if all(s.expected_absent for s in members):
            notes.append(f"{_display_name(family)} {members[0].detail or 'not available yet'}")
            continue
        details = sorted({s.detail for s in members if s.detail})
        reason = details[0] if details else "no usable data"
        notes.append(f"{_display_name(family)} unavailable ({reason})")
    for signal in signals:
        if signal.fallback:
            notes.append(f"{signal.display_name} served from cache after a failed re-fetch")
        elif signal.label == STALE:
            notes.append(f"{signal.display_name} is {_format_age(signal.cache_age)} old")
        elif signal.label == PARTIAL:
            notes.append(f"{signal.display_name} loaded only {signal.coverage} rows")

    return SignalHealthReport(
        signals=signals,
        # A fallback counts as degraded even though its label is only
        # Usable: the source is down RIGHT NOW, which is the thing a
        # reader most wants flagged, and the age alone won't show it.
        degraded=any(
            s.label in DEGRADED_LABELS or s.fallback
            for s in signals
            if s.family not in OPTIONAL_FAMILIES and not s.expected_absent
        ),
        unavailable_families=unavailable_families,
        notes=notes,
    )


def suppressed_features(report: SignalHealthReport) -> dict[str, str]:
    """Features whose required data isn't there, and why. The orchestrator
    decides what to do with this; nothing here hides anything by itself."""
    suppressed: dict[str, str] = {}
    for feature, required in sorted(FEATURE_REQUIREMENTS.items()):
        missing = [f for f in required if f in report.unavailable_families]
        if missing:
            names = ", ".join(_display_name(f) for f in missing)
            suppressed[feature] = f"requires {names}, which {'are' if len(missing) > 1 else 'is'} unavailable"
    return suppressed


def freshness_lines(report: SignalHealthReport) -> list[str]:
    """One line per source, for the report's and dashboard's source list —
    e.g. "KTC dynasty · Fresh · 2.9h · 500 rows"."""
    lines = []
    for signal in report.signals:
        parts = [signal.display_name, signal.label, _format_age(signal.cache_age)]
        if signal.coverage is not None:
            parts.append(f"{signal.coverage} rows")
        lines.append(" · ".join(parts))
    return lines
