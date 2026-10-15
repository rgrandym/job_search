"""How easily a person reads a CV: a deterministic score beside the screening score.

A recruiter skims a CV in seconds before reading on (user's decision 2026-10-15: the CV must
pass the screen and read well). Code measures what makes it quick to read, never a model,
so the same CV always scores the same:

- lines a reader takes in at a glance: bullets of at most `LONG_BULLET_WORDS` words: 25 points
- strong openings: no bullet starts "Responsible for", "Worked on", "Helped" ...: 20 points
- active voice: no "was developed", "were analysed" ...: 10 points
- varied openings: bullets of one role do not start with the same word: 10 points
- a short summary: at most `SUMMARY_WORDS` words in at most four sentences: 15 points
- the match at a glance: the job keywords the CV shows appear in its first
  `TOP_WORDS` words (headline, summary, key strengths, relevant skills): 20 points

A part with nothing to measure (no keywords given, no summary) is left out and the rest
scaled to 100. Grades as the screening score: A 80+, B 65+, C 50+, D. Notes name what to fix,
most points first. Length is reported, never scored: the user decides it.
"""

from __future__ import annotations

import re

from src.cv.ats_score import Text, grade
from src.cv.models import MasterCV, ReadabilityScore

LONG_BULLET_WORDS = 35  # about two lines: a recruiter's eye skips longer bullets
SUMMARY_WORDS = 80
SUMMARY_SENTENCES = 4
TOP_WORDS = 150  # about the top third of the first page
WEIGHTS = {"length": 25, "openings": 20, "active": 10, "varied": 10, "summary": 15, "top": 20}
WEAK_OPENING = re.compile(
    r"^(?:responsible for|duties (?:included|include)|worked on|working on|involved in|"
    r"helped|assisted|participated in|tasked with|in charge of|various|i |my )",
    re.IGNORECASE,
)
PASSIVE = re.compile(r"\b(?:was|were|been|being|is|are)\s+(?:\w+ly\s+)?\w+(?:ed|en)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def score_readability(cv: MasterCV, printed: str, keywords: list[str]) -> ReadabilityScore:
    """The readability of one CV. `printed`: its text as a reader sees it, top first;
    `keywords`: the job's ATS keywords."""
    bullets = [b.text for role in cv.experience for b in role.bullets]
    long = [b for b in bullets if len(b.split()) > LONG_BULLET_WORDS]
    weak = [b for b in bullets if WEAK_OPENING.match(b.strip())]
    passive = [b for b in bullets if PASSIVE.search(b)]
    repeats = 0
    for role in cv.experience:
        firsts = [b.text.split()[0].casefold() for b in role.bullets if b.text.split()]
        repeats += len(firsts) - len(set(firsts))
    summary = cv.basics.summary or ""
    words = summary.split()
    sentences = _sentences(summary)
    shares: dict[str, float | None] = {
        "length": 1 - len(long) / len(bullets) if bullets else None,
        "openings": 1 - len(weak) / len(bullets) if bullets else None,
        "active": 1 - len(passive) / len(bullets) if bullets else None,
        "varied": 1 - repeats / len(bullets) if bullets else None,
        "summary": _summary_share(len(words), len(sentences)) if summary else None,
        "top": _top_share(printed, keywords),
    }
    asked = {k: v for k, v in shares.items() if v is not None}
    total = sum(WEIGHTS[k] for k in asked) or 1
    lost = {k: WEIGHTS[k] * (1 - v) * 100 / total for k, v in asked.items()}
    score = round(100 - sum(lost.values())) if asked else 0
    notes = {
        "length": f"{len(long)} bullet(s) over {LONG_BULLET_WORDS} words: a reader skims past "
        "them; tighten or split",
        "openings": f"{len(weak)} bullet(s) open weakly (e.g. 'Responsible for', 'Helped'): "
        "lead with what you did",
        "active": f"{len(passive)} bullet(s) in the passive voice: say who did it",
        "varied": f"{repeats} bullet(s) start with the same word as another in their role",
        "summary": f"The summary has {len(words)} words in {len(sentences)} sentence(s): "
        f"keep it to {SUMMARY_WORDS} words or fewer so it is read",
        "top": "Few of the job's keywords you show appear near the top: a recruiter may not "
        "see the match at a glance",
    }
    ordered = sorted((k for k in lost if lost[k] >= 0.5), key=lambda k: -lost[k])
    return ReadabilityScore(
        score=max(0, score),
        grade=grade(max(0, score)),
        notes=[notes[k] for k in ordered],
        words=len(printed.split()),
        bullets=len(bullets),
    )


def _summary_share(words: int, sentences: int) -> float:
    over = max(0, words - SUMMARY_WORDS) / SUMMARY_WORDS + max(0, sentences - SUMMARY_SENTENCES)
    return max(0.0, 1 - over)


def _top_share(printed: str, keywords: list[str]) -> float | None:
    """Of the job keywords the CV shows, the share a reader meets in its first words."""
    shown = [k for k in keywords if Text(printed).has(k)]
    if not shown:
        return None
    top = Text(" ".join(printed.split()[:TOP_WORDS]))
    return sum(top.has(k) for k in shown) / len(shown)
