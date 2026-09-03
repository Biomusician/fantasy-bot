"""The decision layer reads its inputs duck-typed; this checks the ducks.

Several modules deliberately stay out of `report_data`'s import graph and
reach for their inputs with `getattr(ld, "...", default)` instead. That is
the right call architecturally — `decision_ledger` importing
`LeagueReportData` would make the dependency cycle real — but it means a
field renamed in `report_data` does not break anything loudly. Every reader
just starts seeing its default: no proposals, no conflicts, no role trends,
and a report that quietly stops saying things it used to say.

So: grep those modules for the literal attribute names they ask for, and
require each to be a real dataclass field on the type it is read off.
Static, not dynamic — a getattr on a code path no test exercises is exactly
the one that rots.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from sleeper_tool.report_data import LeagueReportData, WeeklyReportData

SOURCE_DIR = Path(__file__).resolve().parent.parent / "sleeper_tool"

# The modules that read report objects duck-typed rather than by import.
DUCK_TYPED_MODULES = (
    "action_priority",
    "recommendation_provenance",
    "watchlist",
    "decision_ledger",
    "decision_outcomes",
    "calibration",
    "recommendation_conflicts",
    "report_views",
)

# variable name in the source -> the dataclass it is expected to be
SUBJECTS = {"ld": LeagueReportData, "report": WeeklyReportData}


def _names_read(source: str, variable: str) -> set[str]:
    return set(re.findall(rf'getattr\(\s*{variable}\s*,\s*"([A-Za-z_][A-Za-z0-9_]*)"', source))


@pytest.mark.parametrize("module", DUCK_TYPED_MODULES)
def test_every_duck_typed_attribute_is_a_real_report_field(module):
    source = (SOURCE_DIR / f"{module}.py").read_text(encoding="utf-8")
    for variable, cls in SUBJECTS.items():
        fields = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(_names_read(source, variable) - fields)
        assert not unknown, (
            f"{module}.py reads {variable}.{{{', '.join(unknown)}}} duck-typed, "
            f"but {cls.__name__} has no such field — every reader silently gets its default"
        )


def test_the_guard_actually_found_something_to_check():
    """A regex that matches nothing would make every case above pass. Pin
    that the sweep is live and that it covers both subjects."""
    found = {
        variable: {
            name
            for module in DUCK_TYPED_MODULES
            for name in _names_read((SOURCE_DIR / f"{module}.py").read_text(encoding="utf-8"), variable)
        }
        for variable in SUBJECTS
    }
    assert len(found["ld"]) >= 20, found["ld"]
    assert len(found["report"]) >= 4, found["report"]
    # Spot-checks: fields whose loss would silently empty a whole feature.
    assert {"proposals", "waiver_targets", "role_trends", "conflicts"} <= found["ld"]
    assert {"current_week", "health"} <= found["report"]


def test_the_regex_would_notice_a_renamed_field():
    """Falsifiability check on the check itself — a fabricated source line
    with a bogus attribute has to be reported as unknown."""
    fabricated = 'value = getattr(ld, "proposals_renamed", None)\n'
    fields = {f.name for f in dataclasses.fields(LeagueReportData)}
    assert _names_read(fabricated, "ld") - fields == {"proposals_renamed"}
