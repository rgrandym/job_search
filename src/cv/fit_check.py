"""How much better the tailored CV shows the fit for one job, against the CV it came from.

    compare_fit: code measures keyword coverage (whole CV and first page), then one LLM call
    reads both CVs as a recruiter would (in an order that does not reveal which is which)
    -> FitComparison

    score_versions: several CVs for one job (originals and tailored versions) read side by
    side by the same blind reader, each tailored version set against its original
    -> VersionReading per CV; best_version picks the one that shows the fit most clearly

The candidate's actual fit is the same for both (the same facts); the job_matcher's verdict
judges that. This measures how clearly each CV shows it to a reader.
"""

from __future__ import annotations

import hashlib

from src.core.llm_provider import LLMProvider
from src.cv.models import (
    AreaChange,
    DimensionChange,
    FitComparison,
    FitDimension,
    FitReading,
    FitReadings,
    FitReadout,
    JDAnalysis,
    MasterCV,
    TailoredCV,
    VersionReading,
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
MAX_VERSIONS = 6  # CVs read in one call; more and one reader's standard drifts

_RUBRIC = """Read each CV on its own, as if it were the only one in front of you, and judge it \
only by what it shows on the page: what a reader concludes about this candidate for this job, not \
what the candidate might have done. Rate each 0-4:
- function: 0 another discipline .. 4 clearly does this work now
- domain: 0 unrelated field .. 4 the same field
- seniority: 0 far off .. 4 the same level and scope
- leadership: 0 not shown .. 4 the kind of leadership the job needs
- sector: 0 unrelated sector .. 4 the same sector
`requirements_shown`: the numbers of the listed requirements the CV clearly shows the \
candidate meets. `impression`: one sentence on what the top of the CV conveys for this job."""

FIT_SYSTEM = f"""You are a recruiter screening applications for one job. You see the job \
description, a numbered list of its requirements, and two CVs (CV 1 and CV 2). {UNTRUSTED}

{_RUBRIC}
Return the readings in order: `first` for CV 1, `second` for CV 2."""

VERSIONS_SYSTEM = f"""You are a recruiter screening applications for one job. You see the job \
description, a numbered list of its requirements, and several CVs of the same candidate \
(CV 1, CV 2, ...), written in different ways. {UNTRUSTED}

{_RUBRIC}
Use the same standard for every CV, so the ratings can be compared. Return one reading per \
CV in `readings`, in the order given: CV 1 first."""


def requirements_for(tailored: TailoredCV, jd: JDAnalysis) -> list[str]:
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
    requirements = requirements_for(tailored, jd)
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


def read_versions(
    texts: list[str], requirements: list[str], jd_text: str, llm: LLMProvider
) -> list[FitReading]:
    """All CVs read in one call by the same reader, in an order fixed by the job and the texts
    (so an original is not always first); returns the readings in the order given."""
    if not 1 <= len(texts) <= MAX_VERSIONS:
        raise ValueError(f"Compare between 1 and {MAX_VERSIONS} CVs at a time")
    order = sorted(
        range(len(texts)), key=lambda i: hashlib.sha256((jd_text + texts[i]).encode()).digest()
    )
    listed = "\n".join(f"{n}. {r}" for n, r in enumerate(requirements, 1))
    cvs = "\n\n".join(f"<cv_{n}>\n{texts[i]}\n</cv_{n}>" for n, i in enumerate(order, 1))
    prompt = (
        f"<job_description>\n{jd_text}\n</job_description>\n\n"
        f"<requirements>\n{listed or 'none listed'}\n</requirements>\n\n{cvs}"
    )
    readout = llm.generate(system=VERSIONS_SYSTEM, prompt=prompt, output_model=FitReadings)
    if len(readout.readings) != len(texts):
        raise ValueError(
            f"The reader returned {len(readout.readings)} readings for {len(texts)} CVs; try again"
        )
    by_index = dict(zip(order, readout.readings, strict=True))
    return [by_index[i] for i in range(len(texts))]


def _found(text: str, keywords: list[str]) -> list[str]:
    return [k for k in keywords if mentions(text, k)]


def _measure(
    item: VersionReading, text: str, reading: FitReading, total: int, keywords: list[str]
) -> VersionReading:
    """One CV's reading and keyword counts (its dimensions set against itself for now)."""
    found = _found(text, keywords)
    return item.model_copy(
        update={
            "score": _score(reading),
            "dimensions": [
                DimensionChange(name=n, before=getattr(reading, n), after=getattr(reading, n))
                for n in WEIGHTS
            ],
            "requirements_shown": sorted(
                {n for n in reading.requirements_shown if 1 <= n <= total}
            ),
            "keywords_found": found,
            "keyword_coverage": len(found) / len(keywords) if keywords else 1.0,
            "first_page_coverage": _coverage(_first_page(text), keywords),
            "impression": reading.impression,
            "words": len(text.split()),
        }
    )


def _area(area: str, before: float, after: float, shown: tuple[str, str]) -> AreaChange:
    change = "better" if after > before + 1e-9 else "worse" if after < before - 1e-9 else "same"
    return AreaChange(area=area, before=shown[0], after=shown[1], change=change)


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def version_areas(
    version: VersionReading, baseline: VersionReading, requirements: list[str]
) -> list[AreaChange]:
    """What a tailored version shows better, worse or the same as its original."""
    total = len(requirements)
    areas = [
        _area("Fit score", baseline.score, version.score, (f"{baseline.score}", f"{version.score}"))
    ]
    for mine, theirs in zip(version.dimensions, baseline.dimensions, strict=True):
        label = mine.name.capitalize()
        areas.append(
            _area(label, theirs.after, mine.after, (f"{theirs.after}/4", f"{mine.after}/4"))
        )
    before_req, after_req = set(baseline.requirements_shown), set(version.requirements_shown)
    areas.append(
        _area(
            "Requirements shown",
            len(before_req),
            len(after_req),
            (f"{len(before_req)} of {total}", f"{len(after_req)} of {total}"),
        )
    )
    for n in sorted(before_req ^ after_req):
        now = n in after_req
        status = ("not shown", "shown") if now else ("shown", "not shown")
        areas.append(_area(f"Requirement: {requirements[n - 1]}", not now, now, status))
    for label, before, after in (
        ("Keyword coverage", baseline.keyword_coverage, version.keyword_coverage),
        ("Keywords on the first page", baseline.first_page_coverage, version.first_page_coverage),
    ):
        areas.append(_area(label, before, after, (_pct(before), _pct(after))))
    gained = [k for k in version.keywords_found if k not in baseline.keywords_found]
    lost = [k for k in baseline.keywords_found if k not in version.keywords_found]
    if gained:
        areas.append(
            AreaChange(area="Keywords added", before="", after=", ".join(gained), change="better")
        )
    if lost:
        areas.append(
            AreaChange(area="Keywords lost", before=", ".join(lost), after="", change="worse")
        )
    return areas


def score_versions(
    items: list[VersionReading],
    texts: list[str],
    requirements: list[str],
    keywords: list[str],
    jd_text: str,
    llm: LLMProvider,
) -> list[VersionReading]:
    """Read every CV for the job side by side; each tailored version is then set against its
    original (`baseline_id`, which must be among `items`)."""
    readings = read_versions(texts, requirements, jd_text, llm)
    measured = [
        _measure(item, text, reading, len(requirements), keywords)
        for item, text, reading in zip(items, texts, readings, strict=True)
    ]
    by_id = {m.id: m for m in measured}
    out: list[VersionReading] = []
    for item in measured:
        baseline = by_id.get(item.baseline_id or "")
        if item.kind == "tailored" and baseline is not None:
            item = item.model_copy(
                update={
                    "dimensions": [
                        DimensionChange(name=mine.name, before=theirs.after, after=mine.after)
                        for mine, theirs in zip(item.dimensions, baseline.dimensions, strict=True)
                    ],
                    "areas": version_areas(item, baseline, requirements),
                }
            )
        out.append(item)
    return out


def best_version(readings: list[VersionReading], total: int) -> tuple[str | None, str]:
    """The CV that shows the fit most clearly (score, then requirements, then keywords; a
    tailored version wins a tie), and one sentence saying why."""
    if not readings:
        return None, ""

    def rank(r: VersionReading) -> tuple[int, int, float, bool]:
        return r.score, len(r.requirements_shown), r.keyword_coverage, r.kind == "tailored"

    best = max(readings, key=rank)
    shown = f"; {len(best.requirements_shown)} of {total} requirements shown" if total else ""
    if best.kind == "original":
        if not any(r.kind == "tailored" for r in readings):
            return best.id, f"{best.label} reads best for this job: {best.score}/100{shown}."
        return best.id, (
            f"No tailored version reads better than {best.label} for this job "
            f"({best.score}/100); try another level or emphasis, or check the wording."
        )
    baseline = next((r for r in readings if r.id == best.baseline_id), None)
    gain = f" ({best.score - baseline.score:+d} on its original)" if baseline else ""
    ties = [r.label for r in readings if r is not best and rank(r)[:3] == rank(best)[:3]]
    tie = f" It ties with {', '.join(ties)}." if ties else ""
    return best.id, f"{best.label} shows the fit most clearly: {best.score}/100{gain}{shown}.{tie}"
