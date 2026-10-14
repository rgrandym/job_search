"""Shared language checks for newly drafted CV and cover-letter claims."""

from __future__ import annotations

import re

_PUFFERY = re.compile(
    r"\b(?:world[- ]class|best[- ]in[- ]class|industry[- ]leading|unparalleled|"
    r"visionary|rockstar|guru|proven leader|exceptional|outstanding|perfect fit|"
    r"ideal candidate|hit the ground running|dream job|go-getter|thought leader)\b",
    re.IGNORECASE,
)
# Corporate jargon and AI-sounding filler a recruiter's quick read takes as a red flag (user's
# decision 2026-10-14). AI screens do not detect AI writing; this protects the human reader.
_JARGON = re.compile(
    r"\b(?:passionate|team player|results[- ]driven|results[- ]oriented|dynamic|self[- ]starter|"
    r"detail[- ]oriented|proven track record|leverag(?:e|ed|es|ing)|synerg(?:y|ies)|"
    r"cutting[- ]edge|robust|seamless(?:ly)?|utili[sz](?:e|ed|es|ing)|foster(?:ed|ing|s)?|"
    r"pivotal|meticulous(?:ly)?|showcas(?:e|ed|es|ing)|drive impact|driving impact|"
    r"go[- ]to|wheelhouse|move the needle|game[- ]changer|holistic|best practices)\b",
    re.IGNORECASE,
)
# A CV speaks as its author; these name the candidate from outside.
_THIRD_PERSON = re.compile(
    r"\b(?:the (?:candidate|applicant)(?:'s)?|candidate's|he|she|him|his|her|hers|himself|"
    r"herself)\b",
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


def jargon(source: str, new: str) -> str | None:
    """Why `new` adds corporate jargon that `source` does not use, or None. A word the user
    wrote themselves stays theirs."""
    for match in _JARGON.finditer(new):
        if not re.search(rf"\b{re.escape(match.group())}\b", source, re.IGNORECASE):
            return f"adds jargon a recruiter reads as filler: {match.group()!r}"
    return None


def third_person(source: str, new: str, name: str = "") -> str | None:
    """Why `new` speaks of the candidate in the third person where `source` does not, or None.
    A CV is the user's own voice (user's decision 2026-10-14): first person or the implied
    first person of a bullet, never "the candidate", "she" or their own name."""
    names = [w for w in re.findall(r"[^\W\d_]+", name) if len(w) > 2]
    words = rf"\b(?:{'|'.join(map(re.escape, names))})\b" if names else None
    for pattern in (_THIRD_PERSON, re.compile(words, re.IGNORECASE) if words else None):
        match = pattern.search(new) if pattern else None
        if match and not re.search(rf"\b{re.escape(match.group())}\b", source, re.IGNORECASE):
            return f"refers to the candidate in the third person: {match.group()!r}"
    return None


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
