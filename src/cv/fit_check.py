"""How much better the tailored CV shows the fit for one job, against the CV it came from.

    compare_fit: code measures keyword coverage (whole CV and first page), then one LLM call
    reads both CVs as a recruiter would (in an order that does not reveal which is which)
    -> FitComparison

The candidate's actual fit is the same for both (the same facts); the job_matcher's verdict
judges that. This measures how clearly each CV shows it to a reader.
"""

from __future__ import annotations

import hashlib

from src.core.llm_provider import LLMProvider
from src.cv.models import (
    DimensionChange,
    FitComparison,
    FitDimension,
    FitReading,
    FitReadout,
    JDAnalysis,
    MasterCV,
    TailoredCV,
)
from src.cv.tailor import UNTRUSTED, ats_keywords, cv_to_text, printed_text
from src.tools.search_tools import mentions

WEIGHTS: dict[FitDimension, int] = {
    "function": 30,
    "domain": 20,
    "seniority": 20,
    "leadership": 10,
    "sector": 10,
}
FIRST_PAGE_WORDS = 450  # what fits above the fold of page two on a typical CV
MAX_REQUIREMENTS = 15

FIT_SYSTEM = f"""You are a recruiter screening applications for one job. You see the job \
description, a numbered list of its requirements, and two CVs (CV 1 and CV 2). {UNTRUSTED}

Read each CV on its own, as if it were the only one in front of you, and judge it only by \
what it shows on the page: what a reader concludes about this candidate for this job, not \
what the candidate might have done. Rate each 0-4:
- function: 0 another discipline .. 4 clearly does this work now
- domain: 0 unrelated field .. 4 the same field
- seniority: 0 far off .. 4 the same level and scope
- leadership: 0 not shown .. 4 the kind of leadership the job needs
- sector: 0 unrelated sector .. 4 the same sector
`requirements_shown`: the numbers of the listed requirements the CV clearly shows the \
candidate meets. `impression`: one sentence on what the top of the CV conveys for this job.
Return the readings in order: `first` for CV 1, `second` for CV 2."""


def _requirements(tailored: TailoredCV, jd: JDAnalysis) -> list[str]:
    """What the reader checks: the requirement map's essential rows (all rows when none are
    essential), else the job's must-haves and nice-to-haves."""
    rows = tailored.requirements
    essential = [r.requirement for r in rows if r.priority == "essential"]
    listed = essential or [r.requirement for r in rows] or [*jd.must_have, *jd.nice_to_have]
    return list(dict.fromkeys(r for r in listed if r.strip()))[:MAX_REQUIREMENTS]


def _coverage(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    return sum(mentions(text, k) for k in keywords) / len(keywords)


def _first_page(text: str) -> str:
    return " ".join(text.split()[:FIRST_PAGE_WORDS])


def _score(reading: FitReading) -> int:
    points = sum(getattr(reading, name) / 4 * weight for name, weight in WEIGHTS.items())
    return int(round(100 * points / sum(WEIGHTS.values())))


def _read(
    before: str, after: str, requirements: list[str], jd_text: str, llm: LLMProvider
) -> tuple[FitReading, FitReading]:
    """Both CVs read in one call, in an order fixed by the job text (so a reader cannot learn
    that the second is always the tailored one); returns (before, after)."""
    tailored_first = hashlib.sha256(jd_text.encode()).digest()[0] % 2 == 0
    one, two = (after, before) if tailored_first else (before, after)
    listed = "\n".join(f"{n}. {r}" for n, r in enumerate(requirements, 1))
    prompt = (
        f"<job_description>\n{jd_text}\n</job_description>\n\n"
        f"<requirements>\n{listed or 'none listed'}\n</requirements>\n\n"
        f"<cv_1>\n{one}\n</cv_1>\n\n<cv_2>\n{two}\n</cv_2>"
    )
    readout = llm.generate(system=FIT_SYSTEM, prompt=prompt, output_model=FitReadout)
    if tailored_first:
        return readout.second, readout.first
    return readout.first, readout.second


def _shown(reading: FitReading, total: int) -> int:
    return len({n for n in reading.requirements_shown if 1 <= n <= total})


def compare_fit(
    source: MasterCV, tailored: TailoredCV, jd: JDAnalysis, jd_text: str, llm: LLMProvider
) -> FitComparison:
    """Read the original and the tailored CV for this job and compare what each shows."""
    before_text = cv_to_text(source, tags=False)
    after_text = printed_text(tailored)
    keywords = ats_keywords(jd)
    requirements = _requirements(tailored, jd)
    before, after = _read(before_text, after_text, requirements, jd_text, llm)
    dimensions = [
        DimensionChange(name=name, before=getattr(before, name), after=getattr(after, name))
        for name in WEIGHTS
    ]
    worse = [f"{d.name}: {d.before} → {d.after}" for d in dimensions if d.after < d.before]
    shown_before, shown_after = _shown(before, len(requirements)), _shown(after, len(requirements))
    if shown_after < shown_before:
        worse.append(f"requirements shown: {shown_before} → {shown_after}")
    return FitComparison(
        before=_score(before),
        after=_score(after),
        dimensions=dimensions,
        requirements_total=len(requirements),
        requirements_before=shown_before,
        requirements_after=shown_after,
        keywords_before=_coverage(before_text, keywords),
        keywords_after=_coverage(after_text, keywords),
        first_page_before=_coverage(_first_page(before_text), keywords),
        first_page_after=_coverage(_first_page(after_text), keywords),
        impression_before=before.impression,
        impression_after=after.impression,
        worse=worse,
    )
