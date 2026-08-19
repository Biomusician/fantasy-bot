"""Static configuration: my Sleeper identity and known league metadata.

League `kind` and `sleeper_type` were confirmed directly against the Sleeper API
(`settings.type`: 0=redraft, 1=keeper, 2=dynasty) on 2026-08-18, not just taken
from the handoff doc's "(unconfirmed)" labels.
"""
from __future__ import annotations

from dataclasses import dataclass, field


MY_USERNAME = "Biomusician"
MY_USER_ID = "383699662819082240"
SEASON = "2026"

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"


@dataclass(frozen=True)
class LeagueInfo:
    name: str
    league_id: str
    kind: str  # "dynasty", "keeper", "redraft"
    sleeper_type: int  # 0=redraft, 1=keeper, 2=dynasty (confirmed via API)
    my_team_name: str
    qb_format: str = ""  # informational only; real values come from /league scoring_settings
    notes: str = ""


LEAGUES: list[LeagueInfo] = [
    LeagueInfo(
        name="Big Daddy AF",
        league_id="1312111629638328320",
        kind="dynasty",
        sleeper_type=2,
        my_team_name="Statistical Anomalies",
        qb_format="1QB",
    ),
    LeagueInfo(
        name="That Other Dynasty League",
        league_id="1312114906702565376",
        kind="dynasty",
        sleeper_type=2,
        my_team_name="Statistical Anomalies",
        qb_format="SF",
        notes="Taxi squad (4 slots)",
    ),
    LeagueInfo(
        name="No Taco Zone",
        league_id="1337895170238066688",
        kind="dynasty",
        sleeper_type=2,
        my_team_name="The Chicago Bears",
        qb_format="SF",
        notes="Taxi squad (3 slots), TE premium, 6pt pass TD",
    ),
    LeagueInfo(
        name="Handsome Ross Durham +11",
        league_id="1312111562655293440",
        kind="dynasty",
        sleeper_type=2,
        my_team_name="Buc'd Up",
        qb_format="SF",
        notes="Reserve-only (no taxi), TE premium, 100yd rush bonus",
    ),
    LeagueInfo(
        name="International AWACKOS",
        league_id="1312114051639181312",
        kind="dynasty",
        sleeper_type=2,
        my_team_name="\U0001F3F3️Young and Hopeless\U0001F3F3️",
        qb_format="SF",
        notes="Taxi squad (2 slots)",
    ),
    LeagueInfo(
        name="Primo Veterans ($20)",
        league_id="1389362188744937472",
        kind="keeper",
        sleeper_type=1,
        my_team_name="CD Drives",
        qb_format="SF + 3 FLEX",
        notes=(
            "League of Record keeper format: 1 franchise player (auto-kept) + "
            "3 lottery players (no shared position w/ franchise; 2 of 3 randomly kept). "
            "0.5 PPR, 6pt pass TD, 40+yd TD bonus (+2), no K, 2 IR, FAAB, "
            "trade deadline wk 11, playoffs wk 15-17 (6 teams)."
        ),
    ),
    LeagueInfo(
        name="This League Sucks (and Bites)",
        league_id="1395575615750406144",
        kind="redraft",
        sleeper_type=0,
        my_team_name="",
        qb_format="1QB",
    ),
    LeagueInfo(
        name="Disco",
        league_id="1355356729629495296",
        kind="redraft",
        sleeper_type=0,
        my_team_name="",
        qb_format="SF + 4 FLEX",
        notes="Half PPR, league-average scoring",
    ),
    LeagueInfo(
        name="The 7th League",
        league_id="1368719252474822656",
        kind="redraft",
        sleeper_type=0,
        my_team_name="",
        qb_format="SF",
    ),
    LeagueInfo(
        name="The Surfeit",
        league_id="1367544788303253504",
        kind="redraft",
        sleeper_type=0,
        my_team_name="",
        qb_format="1QB",
    ),
]

DYNASTY_LEAGUES = [l for l in LEAGUES if l.kind == "dynasty"]
KEEPER_LEAGUES = [l for l in LEAGUES if l.kind == "keeper"]
REDRAFT_LEAGUES = [l for l in LEAGUES if l.kind == "redraft"]

LEAGUES_BY_ID = {l.league_id: l for l in LEAGUES}


def get_league(league_id: str) -> LeagueInfo:
    try:
        return LEAGUES_BY_ID[league_id]
    except KeyError as exc:
        raise KeyError(f"Unknown league_id {league_id!r}; add it to sleeper_tool/config.py") from exc
