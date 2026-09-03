"""Turning a proposal into the short chat message you'd actually send.

Only the assembly lives here: the individual clauses are computed by the
module that has the roster context to compute them honestly (mostly
`trade_engine._benefit_reason` and its neighbours) and passed in. That split
is deliberate — this file must never be able to invent a claim, only arrange
the ones it's handed.
"""
from __future__ import annotations

from sleeper_tool.draft_picks import OwnedPick
from sleeper_tool.roster_analysis import RosterEntry
from sleeper_tool.trade_types import OpponentFit, TradeProposal

_MESSAGE_OPENERS = {
    "buy_low": [
        "Hey, {give_line} for {receive_line}? ",
        "Would you do {give_line} for {receive_line}? ",
        "Hey, I'd offer {give_line} for {receive_line} if you're open to it. ",
    ],
    "sell_high": [
        "Hey, I'd move {give_line} for {receive_line}, interested? ",
        "I'd let go of {give_line} for {receive_line} if it helps you out. ",
    ],
    "pick_target": [
        "Hey, I'd send {give_line} for {receive_line}. ",
        "Thinking {give_line} for {receive_line}? ",
    ],
}

# Generic fallback closers — used only when no concrete benefit_reason
# could be computed (e.g. a caller that hasn't threaded roster data
# through). Prefer benefit_reason everywhere it's available: "fills a
# real need" says nothing an opposing manager doesn't already assume
# you'd claim; naming the actual position/starter gap is what makes an
# offer read as considered rather than mass-produced.
_GENERIC_CLOSERS = [
    "No pressure either way, just floating it.",
    "Let me know if it's not your speed.",
    "Totally open to a different mix if this isn't it.",
]


def _names_line(entries: list[RosterEntry], picks: list[OwnedPick]) -> str:
    names = [e.name for e in entries] + [p.name for p in picks]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return " + ".join(names)


def _content_seed(*parts: str) -> int:
    """A stable (not process-hash-randomized, unlike Python's built-in
    hash()) integer derived from actual message content, used to vary
    opener choice by what the trade actually IS rather than by piece-
    counts/username-length alone — the latter made two different real
    trades produce byte-identical messages whenever give/receive counts
    and username length happened to match, which is common for ordinary
    single-for-single offers.
    """
    return sum(ord(c) for c in "".join(parts))


def generate_trade_message(
    proposal: TradeProposal,
    fit: OpponentFit | None = None,
    *,
    benefit_reason: str | None = None,
    timeline_clause: str | None = None,
    buzz_clause: str | None = None,
    my_interest_clause: str | None = None,
) -> str:
    """A short, casual chat message to actually send — not a summary of the
    proposal's stats. Deliberately avoids AI-sounding phrasing ("according
    to my projections", "this benefits both parties", etc.) in favor of
    the way a real manager pitches a trade in a league chat: plain, a
    little informal, no hard sell.

    Four independent, optional clauses, each dropped silently when there's
    nothing honest to say (never padded with filler) — this is deliberately
    a short PITCH, not the full rationale restated:
    - `benefit_reason` (from trade_engine._benefit_reason): the concrete
      roster-fit case for THEM — e.g. "since he'd start over what you've
      got at TE now".
    - `timeline_clause` (from trade_engine._timeline_clause): why it fits
      THEIR contender/rebuild timeline, only when it genuinely does.
    - `buzz_clause` (from trade_engine._buzz_clause_buy_low/_sell_high):
      recent market movement on the player driving this trade.
    - `my_interest_clause` (from trade_engine._my_interest_clause): why *I*
      want what I'd be getting — the message previously only ever argued
      their side, never said anything about why the sender is interested.

    `fit` is accepted but not read: callers pass the OpponentFit they
    already built, and the clause functions above consume it on their way
    in. Kept in the signature so the call sites stay readable as "this
    message is about this fit".
    """
    trade_type = proposal.trade_type if proposal.trade_type in _MESSAGE_OPENERS else "buy_low"
    give_line = _names_line(proposal.give, proposal.give_picks)
    receive_line = _names_line(proposal.receive, proposal.receive_picks)
    seed = _content_seed(give_line, receive_line, proposal.target_username or "")
    opener_idx = seed % len(_MESSAGE_OPENERS[trade_type])
    opener = _MESSAGE_OPENERS[trade_type][opener_idx].format(give_line=give_line, receive_line=receive_line)

    def _sentence(clause: str | None) -> str | None:
        if not clause:
            return None
        text = clause[0].upper() + clause[1:]
        return text if text.endswith((".", "!", "?")) else text + "."

    parts = [opener.strip()]
    for clause in (benefit_reason, timeline_clause, buzz_clause, my_interest_clause):
        sentence = _sentence(clause)
        if sentence:
            parts.append(sentence)
    if len(parts) == 1:
        # Nothing concrete could be computed for ANY clause -- fall back
        # to a generic closer rather than shipping a bare opener with no
        # substance at all.
        closer_idx = (seed * 7 + 3) % len(_GENERIC_CLOSERS)
        parts.append(_GENERIC_CLOSERS[closer_idx])
    return " ".join(parts)
