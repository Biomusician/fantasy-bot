"""Small shared text-formatting helpers used across the trade engine,
waiver engine, and weekly report.
"""
from __future__ import annotations

import datetime as dt


def article(n: int) -> str:
    """'an 8-point', 'an 11-point', 'an 18-point', 'an 80-point'; 'a' otherwise."""
    n = abs(int(n))
    if n in (8, 11, 18) or 80 <= n <= 89 or 800 <= n <= 899:
        return "an"
    return "a"


def age_str(age: dt.timedelta) -> str:
    """Formats a timedelta as a short freshness label, e.g. '43m', '6h', '2d'.
    Previously defined separately (and inconsistently — one appended 'old',
    the other didn't) in report.py and html_report.py; a single shared
    version so the two renderers can't drift again.
    """
    hours = age.total_seconds() / 3600
    if hours < 1:
        return f"{int(age.total_seconds() // 60)}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{age.days}d"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def ordinal_pct(value: float | None) -> str:
    """Formats a 0-100 percentile as e.g. '43rd percentile', or 'unknown' if None.
    Clamped to [0, 100] — a rank that exceeds its source's own counted pool
    size (a real, observed data anomaly, not just a theoretical one) can
    otherwise produce a negative percentile, which upstream of this clamp
    once rendered as literal garbled text like "-28nd percentile" straight
    into a generated report. Clamping degrades gracefully to a boundary
    value instead of exposing the anomaly as broken prose.
    """
    if value is None:
        return "unknown"
    clamped = max(0.0, min(100.0, value))
    return f"{ordinal(round(clamped))} percentile"
