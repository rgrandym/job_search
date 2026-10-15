"""A deterministic screening (ATS) score: the same CV and job always give the same number.

Applicant-tracking systems file a CV as text and let recruiters search it by the posting's
terms; tools such as Jobscan turn that into a match rate. Their exact weights are their own
and not public, so this score is a transparent stand-in built the same way (user's decision
2026-10-14), computed by code from the printed CV text, never by a model:

- job keywords (hard skills and short must-haves, in the posting's own words), each weighted by
  how often the posting uses it (1 to 3): 65 points
- the job title (the title before any comma or dash) in the CV: 15 points; part of it in the
  headline or a role title: part of the points
- degrees the posting names (PhD, MSc, ...) found in the CV: 10 points
- soft skills the posting names found in the CV: 10 points

A part the posting does not ask for is left out and the rest scaled to 100. Terms match as an
ATS search would: case-insensitive, plural or singular, with or without hyphens
("post-doctoral" = "postdoctoral"). When the posting writes a term with its abbreviation
("machine learning (ML)"), either form in the CV counts: a recruiter may search for either
(`cv/acronyms.py`). Grade A is 80 or more.
"""

from __future__ import annotations

import re

from src.cv.acronyms import Pair, pairs_in
from src.cv.models import JDAnalysis, ScreeningScore, TermCheck
from src.tools.search_tools import role_words

WEIGHTS = {"keywords": 65, "title": 15, "education": 10, "soft_skills": 10}
MAX_TERM_WEIGHT = 3
MIN_COMPACT = 8  # "postdoctoral" vs "post-doctoral": compared without spaces only when this long
DEGREES: dict[str, tuple[str, ...]] = {
    "PhD": ("phd", "ph d", "dphil", "doctorate", "doctoral degree"),
    "MSc": ("msc", "m sc", "master", "masters", "master's", "ma", "mres", "mphil"),
    "BSc": ("bsc", "b sc", "bachelor", "bachelors", "bachelor's", "ba", "undergraduate degree"),
    "MBA": ("mba",),
    "MD": ("md", "medical degree"),
    "PharmD": ("pharmd",),
}
GRADES = ((80, "A"), (65, "B"), (50, "C"))


def grade(score: int) -> str:
    """A (80+), B (65+), C (50+) or D."""
    return next((g for limit, g in GRADES if score >= limit), "D")


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    keep = word.endswith(("ss", "us")) or (word.endswith("is") and len(word) > 4)  # analysis
    if len(word) > 3 and word.endswith("s") and not keep:
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    """Lowercase word tokens, singular, with hyphens, slashes and '&' read as an ATS reads them."""
    text = text.casefold().replace("&", " and ").replace("’", "'")
    text = re.sub(r"[-/_]", " ", text)
    return [_stem(t) for t in re.findall(r"[a-z0-9+#]+(?:'s)?", text)]


class Text:
    """A CV's text prepared once for many term checks."""

    def __init__(self, text: str) -> None:
        self.padded = f" {' '.join(tokens(text))} "
        self.compact = self.padded.replace(" ", "")

    def has(self, term: str) -> bool:
        """`term` appears as whole words (or, for a long term, with spacing ignored)."""
        words = tokens(term)
        if not words:
            return False
        if f" {' '.join(words)} " in self.padded:
            return True
        compact = "".join(words)
        return len(compact) >= MIN_COMPACT and compact in self.compact


def term_found(text: str, term: str) -> bool:
    return Text(text).has(term)


def _weight(jd_text: Text, term: str) -> int:
    """How often the posting uses `term` (1 to `MAX_TERM_WEIGHT`), counted word by word."""
    words, text = tokens(term), jd_text.padded.split()
    n = len(words)
    count = sum(text[i : i + n] == words for i in range(len(text) - n + 1)) if n else 0
    return max(1, min(MAX_TERM_WEIGHT, count))


def other_forms(term: str, pairs: list[Pair]) -> list[str]:
    """The term's other forms in `pairs`: its abbreviation, or the long form it stands for."""
    key = tokens(term)
    return [b if tokens(a) == key else a for a, b in pairs if key in (tokens(a), tokens(b))]


def _checks(cv: Text, jd: Text, terms: list[str], pairs: list[Pair]) -> list[TermCheck]:
    seen: set[str] = set()
    out: list[TermCheck] = []
    for term in terms:
        key = " ".join(tokens(term))
        if not key or key in seen:
            continue
        seen.add(key)
        found = cv.has(term) or any(cv.has(f) for f in other_forms(term, pairs))
        out.append(TermCheck(term=term, weight=_weight(jd, term), found=found))
    return out


def degrees_asked(jd_text: str) -> list[str]:
    """The degrees the posting names."""
    text = Text(jd_text)
    return [
        name for name, forms in DEGREES.items() if any(text.has(f) for f in forms if len(f) > 2)
    ]


def _degree_found(cv: Text, name: str) -> bool:
    return any(cv.has(f) for f in DEGREES[name] if len(f) > 2 or f == name.casefold())


def title_parts(title: str) -> str:
    """The title a recruiter searches for: before any comma, dash or bracket."""
    return re.split(r"\s[–—-]\s|,|\(|\|", title)[0].strip()


def _title_points(cv_text: str, titles: str, job_title: str) -> float:
    """1 when the job title is in the CV; else the share of its role words in the headline
    and role titles, at half value."""
    wanted = title_parts(job_title)
    if not wanted:
        return 0.0
    if term_found(cv_text, wanted):
        return 1.0
    words = [w for w in role_words(wanted)]
    if not words:
        return 0.0
    held = Text(titles)
    return 0.5 * sum(held.has(w) for w in words) / len(words)


def score_cv(
    cv_text: str, titles: str, jd: JDAnalysis, jd_text: str, keywords: list[str]
) -> ScreeningScore:
    """The screening score of one CV's printed text. `titles`: its headline and role titles;
    `keywords`: the job's ATS keywords (`tailor.ats_keywords`)."""
    cv, posting, pairs = Text(cv_text), Text(jd_text), pairs_in(jd_text)
    words = _checks(cv, posting, keywords, pairs)
    soft = _checks(cv, posting, [s for s in jd.soft_skills if len(s.split()) <= 4], pairs)
    asked = degrees_asked(jd_text)
    education = [TermCheck(term=d, weight=1, found=_degree_found(cv, d)) for d in asked]
    title = _title_points(cv_text, titles, jd.job_title)
    shares: dict[str, float | None] = {
        "keywords": _share(words),
        "title": title if title_parts(jd.job_title) else None,
        "education": _share(education),
        "soft_skills": _share(soft),
    }
    asked_parts = {k: v for k, v in shares.items() if v is not None}
    total = sum(WEIGHTS[k] for k in asked_parts) or 1
    points = {k: round(WEIGHTS[k] * v * 100 / total, 1) for k, v in asked_parts.items()}
    score = round(sum(points.values()))
    return ScreeningScore(
        score=score,
        grade=grade(score),
        keywords=words,
        title=title_parts(jd.job_title),
        title_match=round(title, 2),
        education=education,
        soft_skills=soft,
        points=points,
    )


def _share(checks: list[TermCheck]) -> float | None:
    total = sum(c.weight for c in checks)
    return sum(c.weight for c in checks if c.found) / total if total else None
