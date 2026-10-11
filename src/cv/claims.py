"""Shared language checks for newly drafted CV and cover-letter claims."""

from __future__ import annotations

import re

_PUFFERY = re.compile(
    r"\b(?:world[- ]class|best[- ]in[- ]class|industry[- ]leading|unparalleled|"
    r"visionary|rockstar|guru|proven leader|exceptional|outstanding|perfect fit|"
    r"ideal candidate|hit the ground running|dream job|go-getter|thought leader)\b",
    re.IGNORECASE,
)
# Verbs that claim ownership or authority; taking part must not become leading.
_OWNERSHIP = re.compile(
    r"\b(?:led|headed|directed|directing|managed|managing|owned|owning|oversaw|overseeing|"
    r"supervised|supervising|spearheaded|spearheading|drove|championed|founded|chaired|"
    r"line[- ]managed|accountable for)\b",
    re.IGNORECASE,
)  # not "lead" or "leading" ("lead author", "leading journals") or "ran" (ran assays)
# In a source line, these also show authority ("responsible for", "project management").
_AUTHORITY = re.compile(
    r"\b(?:responsible for|in charge of|management|leadership|head of|lead|leading|supervision)\b",
    re.IGNORECASE,
)
# Scope words that enlarge the reach of the work.
_SCOPE = re.compile(
    r"\b(?:global(?:ly)?|worldwide|company[- ]wide|organi[sz]ation[- ]wide|enterprise[- ]wide|"
    r"group[- ]wide|cross[- ]site|international(?:ly)?|multinational|strategic)\b",
    re.IGNORECASE,
)


def overclaim(text: str) -> str | None:
    """Identify stock superlatives that do not make a factual application stronger."""
    match = _PUFFERY.search(text)
    return f"uses unsupported superlative: {match.group()}" if match else None


def escalation(source: str, new: str) -> str | None:
    """Why `new` claims more ownership or reach than `source`, or None. Contributing does not
    become leading, and local work does not become global, unless the source says so."""
    claimed = _OWNERSHIP.search(new)
    if claimed and not (_OWNERSHIP.search(source) or _AUTHORITY.search(source)):
        return f"claims leadership or ownership the source does not show: {claimed.group()!r}"
    for scope in _SCOPE.finditer(new):
        stem = scope.group().casefold().replace("-", " ")[:7]  # "strategic" ~ "strategy"
        if stem not in source.casefold().replace("-", " "):
            return f"adds scope the source does not show: {scope.group()!r}"
    return None
