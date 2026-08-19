"""Normalizes player names so Sleeper, KTC, FantasyPros, and RotoBaller
data can be joined on name — they don't always agree on suffixes, periods,
or apostrophes (e.g. "Patrick Mahomes II" vs "Patrick Mahomes",
"D.J. Moore" vs "DJ Moore", "AJ Brown" vs "A.J. Brown").
"""
from __future__ import annotations

import re
import unicodedata

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_MULTI_SPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    # Strip accents (e.g. "Amon-Ra St. Brown" stays, but "Gabriel Davis" etc unaffected;
    # matters more for names like "Michael Pittman Jr." vs accented international names).
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))

    lowered = ascii_name.lower()
    # Periods and apostrophes get dropped entirely (not turned into a space)
    # so "D.J." collapses to "dj" and "Ja'Marr" to "jamarr" — sources
    # disagree on whether these appear at all, but never disagree by
    # inserting a word-break where the punctuation was.
    no_periods = lowered.replace(".", "").replace("'", "").replace("’", "")
    no_punct = _NON_ALNUM_RE.sub(" ", no_periods)
    tokens = [t for t in _MULTI_SPACE_RE.split(no_punct.strip()) if t]
    tokens = [t for t in tokens if t not in _SUFFIXES]
    return " ".join(tokens)


def build_name_index(players: list[dict], name_key: str = "name") -> dict[str, dict]:
    """Build a normalized-name -> record index. Later entries win on collision,
    which is fine for our ranking sources (collisions are rare and near-duplicate).
    """
    index: dict[str, dict] = {}
    for p in players:
        key = normalize_name(p.get(name_key, ""))
        if key:
            index[key] = p
    return index
