"""Small shared text-formatting helpers used across the trade engine,
waiver engine, and weekly report.
"""
from __future__ import annotations

import datetime as dt


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
    """Formats a 0-100 percentile as e.g. '43rd percentile', or 'unknown' if None."""
    if value is None:
        return "unknown"
    return f"{ordinal(round(value))} percentile"
