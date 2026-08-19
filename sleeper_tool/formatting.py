"""Small shared text-formatting helpers used across the trade engine,
waiver engine, and weekly report.
"""
from __future__ import annotations


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
