"""Structured trading-tendency profiles for league mates, encoded from
sleeper-leaguemates.md (2026-08-18 notes). Sleeper usernames here were
cross-checked against the actual `/league/<id>/users` display_names pulled
into storage — all matched exactly.

These drive HOW trade offers get framed (e.g. don't cite KTC to someone who
hates it), not just whether an offer is fair by value. Coverage is
necessarily incomplete: the dynasty/keeper leagues are well documented, but
several redraft leagues (Disco, The Surfeit) have owners never captured in
the notes. Unknown owners get DEFAULT_PROFILE rather than a crash or a
fabricated read on them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Free-text categorical fields are intentionally strings, not enums — the
# source notes don't always map cleanly to a fixed taxonomy, and callers
# (trade-offer framing, the report) mostly just want to render them as-is.

SAVVY = "savvy"
DECENT = "decent"
BELOW_AVERAGE = "below_average"
UNKNOWN = "unknown"

KTC_FRIENDLY = "friendly"
KTC_AVERSE = "averse"
KTC_NEUTRAL = "neutral"


@dataclass(frozen=True)
class OwnerProfile:
    username: str
    savvy: str = UNKNOWN
    trades_often: str = UNKNOWN  # "active" | "infrequent" | "inactive" | "unknown"
    ktc_stance: str = KTC_NEUTRAL
    youth_vs_veteran: str = UNKNOWN  # "prefers_youth" | "prefers_veteran" | "neutral" | "unknown"
    rebuild_literate: bool | None = None
    fandom: str | None = None
    relationship: str | None = None  # e.g. spouse sharing rosters with another owner
    # Structured, not parsed from `notes` at read time -- a specific, actionable
    # behavioral tell an owner's free-text notes already documented, but that
    # nothing outside owner_profiles.py could ever act on since it was trapped
    # in prose. Only set True from a note that says this explicitly; default
    # False (including for every undocumented owner) rather than guessed.
    dislikes_multi_piece: bool = False  # pushes back specifically on lopsided multi-for-one offers
    hot_streak_susceptible: bool = False  # documented as prone to overvaluing a player on a hot stretch
    notes: str = ""

    def framing_notes(self) -> list[str]:
        """Short imperative hints for how to word a trade offer to this owner."""
        hints: list[str] = []
        if self.ktc_stance == KTC_AVERSE:
            hints.append("Do NOT cite KeepTradeCut values — frame value in on-field production/outlook terms instead.")
        elif self.ktc_stance == KTC_FRIENDLY:
            hints.append("OK to cite KeepTradeCut values as a supporting data point.")
        if self.youth_vs_veteran == "prefers_youth":
            hints.append("Prefers young players — expect to have to overpay in youth/draft capital to get a deal done.")
        elif self.youth_vs_veteran == "prefers_veteran":
            hints.append("Buys veterans — a proven-production pitch will land better than a youth/upside pitch.")
        if self.rebuild_literate is False:
            hints.append("Doesn't read tanking/rebuilding well — avoid explicit 'you're rebuilding' framing; sell on immediate team need instead.")
        if self.trades_often == "infrequent":
            hints.append("Doesn't complete trades often — keep the offer simple and be patient/willing to follow up.")
        if self.dislikes_multi_piece:
            hints.append("Pushes back on lopsided multi-for-one offers — a single clean piece near this value will land better than a bundle.")
        if self.fandom:
            hints.append(f"{self.fandom} fan — a {self.fandom} player in the offer may carry extra appeal.")
        return hints


DEFAULT_PROFILE = OwnerProfile(username="", notes="No notes on file — treat as an average, unknown trader.")

# -- Recurring across multiple leagues (behavior carries over regardless of league) --

GLOBAL_PROFILES: dict[str, OwnerProfile] = {
    "thenotoriousDIP": OwnerProfile(
        username="thenotoriousDIP",
        savvy=SAVVY,
        trades_often="active",
        ktc_stance=KTC_NEUTRAL,
        youth_vs_veteran="prefers_youth",
        rebuild_literate=True,
        fandom="Bears",
        notes="Savvy trader. Willing to take risk on young talent. Easier to trade with.",
    ),
    "JKman08": OwnerProfile(
        username="JKman08",
        savvy=SAVVY,
        trades_often="infrequent",
        ktc_stance=KTC_FRIENDLY,
        youth_vs_veteran="prefers_youth",
        dislikes_multi_piece=True,
        notes=(
            "Savvy trader. Knows I'm a Cowboys fan and exploits it. Hard to get deals done. "
            "Fan of youth over veteran players. Knows of KTC, use it as a data point. "
            "Hates 2-for-1 trades when he's getting the worse players — don't structure lopsided "
            "multi-for-one offers to him."
        ),
    ),
    "MRossDurham": OwnerProfile(
        username="MRossDurham",
        savvy=SAVVY,
        trades_often="infrequent",
        ktc_stance=KTC_AVERSE,
        youth_vs_veteran="prefers_youth",
        fandom="Vikings",
        relationship="Married to karendurham2 — they share rosters across leagues",
        notes=(
            "Savvy trader. Have to overpay for young rookies to get him to move them. "
            "Often willing to deal veterans. Harder to get deals done. Hates KeepTradeCut. "
            "Commissioner of Big Daddy AF and Handsome Ross Durham +11."
        ),
    ),
    "cheongwater": OwnerProfile(
        username="cheongwater",
        savvy=DECENT,
        trades_often="active",
        hot_streak_susceptible=True,
        notes="Not a great trader. Vulnerable to hot starts or streaks — a player coming off a hot stretch may get more than they're worth from him.",
    ),
    "karendurham2": OwnerProfile(
        username="karendurham2",
        savvy=SAVVY,
        ktc_stance=KTC_AVERSE,
        youth_vs_veteran="prefers_youth",
        relationship="Married to MRossDurham — they share rosters across leagues",
        notes="Wife of Ross. Savvy trader. Values young players and tight ends. Also hates KTC.",
    ),
    "Bazinga9": OwnerProfile(
        username="Bazinga9",
        savvy=DECENT,
        trades_often="infrequent",
        fandom="Browns",
        notes="Decent trader, but doesn't trade often.",
    ),
    "dlooney1": OwnerProfile(
        username="dlooney1",
        savvy=DECENT,
        trades_often="infrequent",
        rebuild_literate=True,
        notes="Decent trader. Understands tanking strategies. Not gotten a lot of deals done.",
    ),
    "Asexypikachu": OwnerProfile(
        username="Asexypikachu",
        savvy=SAVVY,
        trades_often="infrequent",
        notes="Not gotten a lot of deals done. Savvy fantasy football player.",
    ),
    "Revoque": OwnerProfile(
        username="Revoque",
        savvy=BELOW_AVERAGE,
        fandom="Bears",
        notes="Bad at fantasy and trades. Overpaid for Caleb Williams in AWACKOS — will overpay for a hyped name.",
    ),
    "HenniJ": OwnerProfile(
        username="HenniJ",
        savvy=UNKNOWN,
        notes="Not a lot known. Assume average fantasy player.",
    ),
}

# -- League-specific owners (not documented as recurring elsewhere) --

LEAGUE_ONLY_PROFILES: dict[str, dict[str, OwnerProfile]] = {
    "Big Daddy AF": {
        "zpsiko": OwnerProfile(username="zpsiko", notes="Not a lot known. Assume average fantasy player."),
        "Distro": OwnerProfile(
            username="Distro",
            savvy=BELOW_AVERAGE,
            rebuild_literate=False,
            youth_vs_veteran="prefers_veteran",
            notes="Not a great fantasy player or trader. Doesn't understand tanking/rebuilding well. Buys older players sometimes.",
        ),
        "Basic7791": OwnerProfile(
            username="Basic7791",
            savvy=BELOW_AVERAGE,
            rebuild_literate=False,
            youth_vs_veteran="prefers_veteran",
            notes="Not a great fantasy player or trader. Doesn't understand tanking/rebuilding well. Buys older players sometimes.",
        ),
    },
    "That Other Dynasty League": {
        "Double_TDs": OwnerProfile(
            username="Double_TDs",
            savvy=BELOW_AVERAGE,
            trades_often="inactive",
            rebuild_literate=False,
            youth_vs_veteran="prefers_veteran",
            notes="Not a great fantasy player or trader. Doesn't understand tanking/rebuilding well. Buys older players sometimes. Not very active.",
        ),
        "GWOT": OwnerProfile(
            username="GWOT",
            savvy=DECENT,
            rebuild_literate=False,
            youth_vs_veteran="prefers_veteran",
            notes="Decent fantasy player and trader. Doesn't understand tanking/rebuilding well. Buys older players sometimes.",
        ),
        "Shetonya": OwnerProfile(
            username="Shetonya",
            savvy=BELOW_AVERAGE,
            rebuild_literate=False,
            youth_vs_veteran="prefers_veteran",
            notes="Not a great fantasy player or trader. Doesn't understand tanking/rebuilding well. Buys older players sometimes.",
        ),
    },
    "No Taco Zone": {
        "AndrewV": OwnerProfile(username="AndrewV", savvy=DECENT, notes="Decent. Assume average."),
        "JamesonJasper": OwnerProfile(username="JamesonJasper", savvy=DECENT, notes="Decent. Assume average."),
        "chrissalsbury6969": OwnerProfile(username="chrissalsbury6969", savvy=DECENT, notes="Decent. Assume average."),
        "bchaffs": OwnerProfile(username="bchaffs", savvy=SAVVY, notes="Good. Assume above average."),
        "StefenAntes": OwnerProfile(username="StefenAntes", savvy=DECENT, notes="Decent. Assume average."),
        "glenbrinks32": OwnerProfile(username="glenbrinks32", savvy=SAVVY, notes="Good. Assume above-average."),
        "Jmw1027": OwnerProfile(username="Jmw1027", savvy=DECENT, notes="Decent. Assume average."),
        "SethT448": OwnerProfile(username="SethT448", savvy=UNKNOWN, notes="New, but took on a terrible team."),
    },
    "Handsome Ross Durham +11": {
        "ObiUno": OwnerProfile(username="ObiUno", savvy=SAVVY, notes="Savvy fantasy football player."),
        "atduvall": OwnerProfile(username="atduvall", savvy=DECENT, notes="Decent. Assume average."),
        "jcarolin": OwnerProfile(username="jcarolin", savvy=SAVVY, notes="Savvy fantasy football player."),
        "LAXPedersen": OwnerProfile(username="LAXPedersen", savvy=DECENT, notes="Decent. Assume average."),
        "jakepedersen": OwnerProfile(username="jakepedersen", savvy=SAVVY, notes="Savvy fantasy football player."),
        "aubreewhitehurst": OwnerProfile(username="aubreewhitehurst", savvy=DECENT, notes="Decent. Assume average."),
        "MMikhail10": OwnerProfile(username="MMikhail10", savvy=DECENT, notes="Decent. Assume average."),
    },
    "International AWACKOS": {
        "Zorp": OwnerProfile(username="Zorp", savvy=SAVVY, notes="Savvy fantasy football player."),
        "KanchoLicious": OwnerProfile(username="KanchoLicious", savvy=BELOW_AVERAGE, notes="Below average."),
        "Press701": OwnerProfile(username="Press701", savvy=DECENT, fandom="Packers", notes="Decent. Assume average."),
        "RunDMZ": OwnerProfile(username="RunDMZ", savvy=DECENT, fandom="Packers", notes="Decent. Assume average."),
        "GreatestWookie": OwnerProfile(username="GreatestWookie", savvy=BELOW_AVERAGE, notes="Below average."),
        "SirDingus": OwnerProfile(username="SirDingus", savvy=DECENT, notes="Decent. Assume average."),
    },
    "Primo Veterans ($20)": {
        "L8rGator": OwnerProfile(username="L8rGator", savvy=DECENT, notes="Decent. Assume average."),
        "pugalicious": OwnerProfile(username="pugalicious", savvy=DECENT, notes="Decent. Assume average."),
        "Kaleigh34": OwnerProfile(username="Kaleigh34", savvy=DECENT, notes="Decent. Assume average."),
        "caltolley": OwnerProfile(username="caltolley", savvy=BELOW_AVERAGE, notes="Below average."),
        "roskoj": OwnerProfile(username="roskoj", savvy=BELOW_AVERAGE, notes="Below average."),
        "EllisOrquiza21": OwnerProfile(username="EllisOrquiza21", savvy=BELOW_AVERAGE, notes="Below average."),
        "chrissd": OwnerProfile(username="chrissd", savvy=BELOW_AVERAGE, notes="Below average."),
    },
}


def get_owner_profile(username: str, league_name: str | None = None) -> OwnerProfile:
    """Look up an owner's profile: global notes first (they carry across
    leagues), then league-specific notes, then a neutral default for owners
    we simply have no information on (common in the less-documented redraft
    leagues).
    """
    if username in GLOBAL_PROFILES:
        return GLOBAL_PROFILES[username]
    if league_name and username in LEAGUE_ONLY_PROFILES.get(league_name, {}):
        return LEAGUE_ONLY_PROFILES[league_name][username]
    return OwnerProfile(username=username, notes=DEFAULT_PROFILE.notes)
