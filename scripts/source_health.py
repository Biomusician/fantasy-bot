"""What one external source is actually doing, end to end.

    .venv/Scripts/python.exe scripts/source_health.py                # every source, from cache
    .venv/Scripts/python.exe scripts/source_health.py --source ktc   # one source
    .venv/Scripts/python.exe scripts/source_health.py --source ktc --live

This exists because "KTC dynasty: Unavailable" is not a diagnosis. It says
nothing about whether the site is down, the page moved its data, a bot wall
was served, the parse returned three players, or the cache was never
written — and those have entirely different fixes. Every stage of the path
is reported separately so the next break is readable in a minute.

Without `--live` nothing is fetched: the cache and the health layer are
read as the last run left them. `--live` performs ONE request for the named
source and reports which parser strategy matched and how many rows it
yielded, without writing to the cache — a diagnosis must not change the
thing being diagnosed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleeper_tool.rankings import cache as ranking_cache
from sleeper_tool.rankings import ktc
from sleeper_tool.rankings.ff_dynasty_pass import DEFAULT_CSV_PATH, SOURCE_URL, ff_dynasty_status
from sleeper_tool.rankings.freshness import SOURCE_WINDOWS, coverage_floor
from sleeper_tool.signal_health import build_health
from sleeper_tool.storage import Storage
from sleeper_tool.valuation import ValuationEngine

# The cache file each family writes, for the sources that write one.
FAMILY_CACHE = {
    "ktc": ["ktc_dynasty"],
    "fantasypros": [
        "fantasypros_dynasty_1qb", "fantasypros_dynasty_superflex",
        "fantasypros_redraft_full_ppr", "fantasypros_redraft_half_ppr", "fantasypros_redraft_superflex",
        "fantasypros_ros_full_ppr", "fantasypros_ros_half_ppr",
    ],
    "rotoballer": ["rotoballer_full_ppr", "rotoballer_half_ppr", "rotoballer_standard"],
    "rotoballer_waivers": ["rotoballer_waivers"],
    "boone_weekly": ["boone_ppr", "boone_half_ppr"],
    "nflverse_schedule": ["nflverse_schedule"],
}


def _fmt_age(age: dt.timedelta | None) -> str:
    if age is None:
        return "—"
    hours = age.total_seconds() / 3600
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


def _row(label: str, value) -> str:
    return f"  {label:<18} {value}"


def cache_report(source: str) -> list[str]:
    """What is on disk for one cache key, and whether it loads."""
    path = ranking_cache._cache_path(source)
    lines = [_row("cache path", path)]
    if not path.exists():
        return lines + [_row("cache", "MISSING — nothing to fall back on")]
    snapshot = ranking_cache.load_snapshot(source)
    if snapshot is None:
        return lines + [_row("cache", f"UNREADABLE ({path.stat().st_size} bytes) — treated as no cache")]
    rows = len(snapshot.payload) if hasattr(snapshot.payload, "__len__") else "—"
    lines += [
        _row("cache written", snapshot.fetched_at.isoformat()),
        _row("cache age", _fmt_age(snapshot.age())),
        _row("cache rows", rows),
        _row("cache loads", "yes"),
    ]
    return lines


def live_ktc() -> list[str]:
    """One request, parsed but never cached."""
    lines: list[str] = []
    try:
        import requests

        resp = requests.get(ktc.KTC_URL, headers=ktc._BROWSER_HEADERS, timeout=30)
    except Exception as exc:
        return [_row("fetch", f"FAILED — {type(exc).__name__}: {exc}")]
    lines += [
        _row("fetch", "success"),
        _row("HTTP", resp.status_code),
        _row("final URL", resp.url),
        _row("content type", resp.headers.get("content-type", "—")),
        _row("bytes", len(resp.content)),
    ]
    if resp.status_code != 200:
        return lines
    parse, failure = ktc.parse_for_diagnostics(resp.text)
    if parse is None:
        return lines + [_row("parser strategy", "NONE MATCHED"), _row("parse failure", failure)]
    lines += [
        _row("parser strategy", parse.strategy),
        _row("parsed rows", parse.rows),
        _row("complete board", "yes" if parse.complete else "no (diagnostic strategy only)"),
    ]
    for note in parse.notes:
        lines.append(_row("parser note", note))
    accepted = ktc.valid_ktc_payload(parse.players)
    lines.append(_row("would cache", "yes" if accepted else "NO — fails validation, cache would be kept"))
    if not accepted:
        from sleeper_tool.rankings.ktc_parser import KTCParseError, validate_parse

        try:
            validate_parse(parse)
        except KTCParseError as exc:
            lines.append(_row("validation", str(exc)))
    return lines


def ff_report() -> list[str]:
    """The Dynasty Pass CSV is a paid manual export — no fetch is ever made."""
    path = Path(DEFAULT_CSV_PATH)
    lines = [
        _row("kind", "manual export of a paid product — never fetched or scraped"),
        _row("expected at", path),
        _row("present", "yes" if path.exists() else "no"),
        _row("status", ff_dynasty_status()),
    ]
    if not path.exists():
        lines.append(_row("to configure", f"export the rankings to that path from {SOURCE_URL}"))
    return lines


def _cached_engine() -> ValuationEngine:
    """A ValuationEngine built strictly from what is on disk.

    ValuationEngine() with no arguments FETCHES every source. A command whose
    job is to report on the cache must not refresh the thing it is reporting
    on — that both hides the failure being diagnosed and makes the diagnosis
    a network call.
    """
    return ValuationEngine(
        ktc_snapshot=ranking_cache.load_snapshot("ktc_dynasty"),
        fp_snapshots={
            key.removeprefix("fantasypros_"): ranking_cache.load_snapshot(key)
            for key in FAMILY_CACHE["fantasypros"]
        },
        rb_snapshots={
            key.removeprefix("rotoballer_"): ranking_cache.load_snapshot(key)
            for key in FAMILY_CACHE["rotoballer"]
        },
    )


def health_report(families: list[str]) -> list[str]:
    """The health layer's own verdict, from the cache, with no fetching."""
    health = build_health(engine=_cached_engine(), storage=Storage())
    lines: list[str] = []
    for signal in health.signals:
        if families and signal.family not in families:
            continue
        lines.append(
            f"  {signal.display_name:<32} {signal.label:<12} {signal.state:<26} "
            f"{_fmt_age(signal.cache_age):>7}  {signal.coverage if signal.coverage is not None else '—'} rows"
        )
        if signal.detail:
            lines.append(f"      {signal.detail}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", help="one family, e.g. ktc / fantasypros / ff_dynasty_pass")
    parser.add_argument("--live", action="store_true", help="make ONE real request (KTC only); never writes the cache")
    args = parser.parse_args()

    families = [args.source] if args.source else []
    if args.source and args.source not in SOURCE_WINDOWS:
        print(f"Unknown source {args.source!r}. Known: {', '.join(sorted(SOURCE_WINDOWS))}")
        return 2

    for family in families or sorted(FAMILY_CACHE):
        print(f"\nsource: {family}")
        windows = SOURCE_WINDOWS.get(family)
        if windows:
            fresh, usable, ceiling = windows
            print(_row("windows", f"fresh <{_fmt_age(fresh)}, usable <{_fmt_age(usable)}, ceiling {_fmt_age(ceiling)}"))
        floor = coverage_floor(family)
        if floor:
            print(_row("coverage floor", f"{floor} rows"))
        if family == "ff_dynasty_pass":
            print("\n".join(ff_report()))
            continue
        for source in FAMILY_CACHE.get(family, []):
            print(_row("—", source))
            print("\n".join(cache_report(source)))
            outcome = ranking_cache.last_fetch_outcome.get(source)
            if outcome:
                print(_row("last outcome", outcome))
        if args.live and family == "ktc":
            print(_row("—", "live request"))
            print("\n".join(live_ktc()))
        elif args.live:
            print(_row("live", "only implemented for --source ktc; other sources read from cache"))

    print("\nhealth layer (from cache, nothing fetched):")
    lines = health_report(families)
    print("\n".join(lines) if lines else "  (no signals for that family)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
