"""The job's own words for evidence a CV already has (user's decision 2026-10-13).

An ATS matches the job's keywords word for word, and a CV often shows the same work in its own
words ("cultured HEK293 and CHO lines" for "Mammalian cell culture"). The tailoring plan may
pair a job keyword with the exact words of one CV bullet that show it (`TermBridge`); code
keeps a pairing only when:
- the term is one of the job's ATS keywords and a general method, domain or practice: a named
  tool, platform or acronym (AWS, CRISPR, LC-MS), a term with a number, or a regulated,
  credential or management term needs the CV's own word;
- the quoted words are in that bullet, which does not already name the term;
- the requirement map lists that bullet as evidence for a requirement naming the term, with a
  class the mode allows: `direct` (standard), `direct` or `transferable` (optimised).
A kept pairing lets the term into that bullet's rewrite and the relevant-skills line; every
other guard (numbers, ownership, scope, loss) is unchanged. Pairings are shown to the user.
"""

from __future__ import annotations

import re

from src.cv.models import MasterCV, MatchClass, RequirementEvidence, TermBridge
from src.tools.search_tools import (
    SKILL_SYNONYMS,
    TECH_SKILLS,
    mentions,
    normalize_skill,
)

BRIDGE_MATCHES: dict[str, tuple[MatchClass, ...]] = {
    "standard": ("direct",),
    "optimised": ("direct", "transferable"),
}
MAX_BRIDGES = 12
MAX_TERM_WORDS = 4
_REGULATED = re.compile(
    r"\b(?:certif\w*|licen[cs]\w*|clearance|right to work|regulat\w*|accredit\w*|chartered|"
    r"cgmp|gmp|gcp|glp|gdp|fda|mhra|ema|iso|management|manager|leadership|lead|head|"
    r"director|degree|phd|msc|bsc|years?)\b",
    re.IGNORECASE,
)
_TOOLS = {normalize_skill(t) for t in (*TECH_SKILLS, *SKILL_SYNONYMS)} | {
    normalize_skill(alias) for aliases in SKILL_SYNONYMS.values() for alias in aliases
}


def _plain(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def bridgeable(term: str) -> bool:
    """A general method, domain or practice that can be named in the job's words; a named tool,
    acronym, number or regulated term needs the CV's own word."""
    words = term.split()
    if not words or len(words) > MAX_TERM_WORDS or re.search(r"\d", term):
        return False
    if _REGULATED.search(term) or normalize_skill(term) in _TOOLS:
        return False
    # an acronym or product name (AWS, qPCR, iPSC, LC-MS) has capitals after its first letter
    return not any(re.search(r"[A-Z]", word[1:]) for word in words)


def _stems(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z]+", text.casefold()) if len(w) > 3}


def _names(requirement: str, term: str) -> bool:
    """The requirement is about the term: it names it, or most of its words ("Maintain
    mammalian ... cell cultures" names "Mammalian cell culture")."""
    if mentions(requirement, term):
        return True
    words = _stems(term)
    return bool(words) and len(words & _stems(requirement)) * 2 >= len(words)


def _role_of(master: MasterCV) -> dict[str, str]:
    return {b.id: role.id for role in master.experience for b in role.bullets}


def verified_bridges(
    proposed: list[TermBridge],
    rows: list[RequirementEvidence],
    master: MasterCV,
    keywords: list[str],
    matches: tuple[MatchClass, ...],
) -> list[TermBridge]:
    """The pairings code can check (module docstring), each term once per bullet."""
    by_key = {normalize_skill(k): k for k in keywords}
    bullets = master.bullet_index()
    role_of = _role_of(master)
    out: list[TermBridge] = []
    seen: set[tuple[str, str]] = set()
    for item in proposed:
        term = by_key.get(normalize_skill(item.term))
        bullet = bullets.get(item.source_id)
        if term is None or bullet is None or not bridgeable(term):
            continue
        evidence = f"{bullet.text} {' '.join(bullet.skills)}"
        quote = _plain(item.quote)
        if mentions(evidence, term) or len(quote.split()) < 2:
            continue
        if f" {quote} " not in f" {_plain(bullet.text)} ":
            continue
        ids = {bullet.id, role_of.get(bullet.id, "")}
        if not any(
            row.match in matches and ids & set(row.evidence) and _names(row.requirement, term)
            for row in rows
        ):
            continue
        if (term, bullet.id) in seen:
            continue
        seen.add((term, bullet.id))
        out.append(item.model_copy(update={"term": term, "quote": item.quote.strip()}))
        if len(out) == MAX_BRIDGES:
            break
    return out


def terms_by_bullet(bridges: list[TermBridge]) -> dict[str, list[str]]:
    """bullet id -> the job terms it may use."""
    out: dict[str, list[str]] = {}
    for bridge in bridges:
        out.setdefault(bridge.source_id, []).append(bridge.term)
    return out


def with_bridges(master: MasterCV, bridges: list[TermBridge]) -> MasterCV:
    """`master` with each verified term as a skill tag of its bullet (tags are evidence for the
    guards and never printed), so that bullet alone may name it."""
    if not bridges:
        return master
    cv = master.model_copy(deep=True)
    terms = terms_by_bullet(bridges)
    for bullet in cv.bullet_index().values():
        if bullet.id in terms:
            bullet.skills = list(dict.fromkeys([*bullet.skills, *terms[bullet.id]]))
    return cv
