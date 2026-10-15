"""A term and its abbreviation, as an ATS searches them (user's decision 2026-10-15).

An ATS search is literal: a recruiter who searches "machine learning" does not find "ML".
Current guidance is to write both forms once ("machine learning (ML)"). Code pairs a term with
its abbreviation only when that is certain:
- the posting or the CV writes them together, "long form (ABBR)" or "ABBR (long form)", and the
  abbreviation's letters are the long form's initials ("liquid chromatography-mass
  spectrometry (LC-MS)", "key performance indicators (KPIs)");
- or a known pair (`search_tools.SKILL_SYNONYMS`: machine learning / ML, NLP, AWS ...).
Pairing the two forms of one term adds no skill and no fact: the guards already read them as
the same term (`search_tools.mentions`).
"""

from __future__ import annotations

import re

from src.tools.search_tools import SKILL_SYNONYMS

Pair = tuple[str, str]  # (long form, abbreviation)

STOP_WORDS = {"and", "of", "the", "for", "in", "to", "with", "on", "&"}
MAX_LONG_WORDS = 8
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9']*")
_BEFORE = re.compile(r"([A-Za-z0-9][A-Za-z0-9'&/\- –]{0,120}?)\s*\(([^()]{1,80})\)")
_ABBR = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-/&][A-Za-z0-9]+)*")


def _letters(abbr: str) -> str:
    """The initials an abbreviation stands for: "LC-MS" -> "lcms", "KPIs" -> "kpi"."""
    if re.fullmatch(r".*[A-Z]s", abbr) and len(abbr) > 2:  # plural: KPIs, iPSCs
        abbr = abbr[:-1]
    return re.sub(r"[^a-z]", "", abbr.casefold())


def is_abbreviation(word: str) -> bool:
    """One token with a capital after its first letter, or all capitals (ML, qPCR, LC-MS)."""
    return (
        bool(_ABBR.fullmatch(word))
        and len(_letters(word)) >= 2
        and any(c.isupper() for c in word[1:])
    )


def _initials(words: list[str]) -> str:
    return "".join(w[0].casefold() for w in words if w.casefold() not in STOP_WORDS)


def _long_form(before: str, letters: str) -> str | None:
    """The shortest run of words ending `before` whose initials are `letters`, as written."""
    spans = list(re.finditer(r"[A-Za-z0-9][A-Za-z0-9']*", before))
    for k in range(2, min(len(spans), MAX_LONG_WORDS) + 1):
        run = spans[-k:]
        if run[0].group().casefold() in STOP_WORDS:
            continue
        if _initials([m.group() for m in run]) == letters:
            return before[run[0].start() :].strip(" -–")
    return None


def pairs_in(text: str) -> list[Pair]:
    """The (long form, abbreviation) pairs `text` defines in brackets."""
    out: list[Pair] = []
    for match in _BEFORE.finditer(text):
        before, inside = match.group(1), " ".join(match.group(2).split())
        if is_abbreviation(inside):
            long = _long_form(before, _letters(inside))
            if long:
                out.append((long, inside))
            continue
        words = _WORD.findall(inside)
        abbr = before.rstrip().rsplit(maxsplit=1)[-1] if before.strip() else ""
        if (
            is_abbreviation(abbr)
            and 2 <= len(words) <= MAX_LONG_WORDS
            and _initials(words) == _letters(abbr)
        ):
            out.append((inside, abbr))
    return list(dict.fromkeys(out))


def known_pairs() -> list[Pair]:
    """Pairs from the skill synonyms whose alias is the canonical name's initials."""
    return [
        (canon, alias.upper())
        for canon, aliases in SKILL_SYNONYMS.items()
        for alias in aliases
        if " " in canon and alias.isalpha() and alias == _initials(canon.split())
    ]
