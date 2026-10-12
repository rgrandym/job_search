"""How much better the tailored CV shows the fit for one job, against the CV it came from.

    compare_fit: code measures keyword coverage (whole CV and first page), then one LLM call
    reads both CVs as a recruiter would (in an order that does not reveal which is which)
    -> FitComparison

    read_versions: several CVs for one job read side by side by the same blind reader,
    `READS` times in different orders; every rating and requirement must quote a line that
    code finds in that CV, and the median of the reads counts -> FitReading per CV
    score_versions: each tailored version set against its original, with a verdict by fixed
    margins -> VersionReading per CV; best_version picks the clearest beyond `NOISE_MARGIN`

The candidate's actual fit is the same for both (the same facts); the job_matcher's verdict
judges that. This measures how clearly each CV shows it to a reader.
"""

from __future__ import annotations

import contextvars
import hashlib
import re
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from src.core.llm_provider import LLMProvider
from src.cv.models import (
    AreaChange,
    DimensionChange,
    EvidencedReading,
    EvidencedReadings,
    FitComparison,
    FitDimension,
    FitReading,
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
READS = 3  # independent reads per comparison; the median rating and majority requirements count
NOISE_MARGIN = 5  # score points: less than one rating step on a 20-weight dimension
UNQUOTED_CAP = 1  # a rating above this needs a line found in the CV

_RUBRIC = """Read each CV on its own, as if it were the only one in front of you, and judge it \
only by what it shows on the page: what a reader concludes about this candidate for this job, not \
what the candidate might have done. Rate each 0-4:
Identify the few capabilities and experiences central to delivering the role from its work \
and outcomes. An item can be pivotal even when listed as desirable. Treat long lists of \
tools and isolated keywords as supporting context, and judge transferable experience by \
what it actually demonstrates.
- function: 0 another discipline .. 4 clearly does this work now
- domain: 0 unrelated field .. 4 the same field
- seniority: 0 far off .. 4 the same level and scope
- leadership: 0 not shown .. 4 the kind of leadership the job needs
- sector: 0 unrelated sector .. 4 the same sector
When torn between two ratings, give the lower one. `requirements_shown`: the numbers of the \
listed requirements that a specific line of the CV shows the candidate meets; leave out any \
you would have to infer. `impression`: one sentence on what the top of the CV conveys for \
this job."""

FIT_SYSTEM = f"""You are a recruiter screening applications for one job. You see the job \
description, a numbered list of its requirements, and two CVs (CV 1 and CV 2). {UNTRUSTED}

{_RUBRIC}
Return the readings in order: `first` for CV 1, `second` for CV 2."""

VERSIONS_SYSTEM = f"""You are a recruiter screening applications for one job. You see the job \
description, a numbered list of its requirements, and several CVs of the same candidate \
(CV 1, CV 2, ...), written in different ways. {UNTRUSTED}

Read each CV on its own, as if it were the only one in front of you, and judge it only by what \
it shows on the page: what a reader concludes about this candidate for this job, not what the \
candidate might have done. Rate each 0-4 and back it with `quote`: the one line of THAT CV that \
best supports the rating, copied word for word (you may shorten it with "..."); '' for 0.
Identify the few capabilities and experiences central to delivering the role from its work \
and outcomes. An item can be pivotal even when listed as desirable. Treat isolated keywords \
as supporting context and assess genuinely transferable experience on its substance.
- function: 0 another discipline .. 4 clearly does this work now
- domain: 0 unrelated field .. 4 the same field
- seniority: 0 far off .. 4 the same level and scope
- leadership: 0 not shown .. 4 the kind of leadership the job needs
- sector: 0 unrelated sector .. 4 the same sector
When torn between two ratings, give the lower one. `requirements_met`: each listed requirement \
a specific line of the CV shows the candidate meets, with that line copied word for word as \
`quote`; leave out any you would have to infer. Quotes are checked against the CV: a rating \
above {UNQUOTED_CAP} or a requirement whose quote is not in that CV does not count. \
`impression`: one sentence on what the top of the CV conveys for this job.
Use the same standard for every CV, so the ratings can be compared. Return one reading per \
CV in `readings`, in the order given: CV 1 first."""


def requirements_for(tailored: TailoredCV, jd: JDAnalysis) -> list[str]:
    """Read pivotal requirements first, then other essentials and desirables."""
    rows = tailored.requirements
    ordered = sorted(
        rows,
        key=lambda r: (r.importance != "key", r.priority != "essential"),
    )
    listed = [r.requirement for r in ordered] or [*jd.must_have, *jd.nice_to_have]
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


def reading_key(text: str, requirements: list[str], jd_text: str, reader: str) -> str:
    """Identity of one CV's reading: the same CV, job, requirements, rubric, number of reads
    and reader model give the same reading, so comparing again does not re-roll the scores."""
    parts = [VERSIONS_SYSTEM, str(READS), jd_text, "\n".join(requirements), text, reader]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


_NOT_WORD = re.compile(r"[^\w%+#]+")
_ELLIPSIS = re.compile(r"\.\.\.|…")


def _plain(text: str) -> str:
    return " ".join(_NOT_WORD.sub(" ", text.casefold()).split())


def quoted(quote: str, cv_plain: str) -> bool:
    """Whether `quote` (pieces split at "...") is in the CV, ignoring case, spacing and
    punctuation; at least two words, so a lone skill name is not evidence."""
    pieces = [_plain(p) for p in _ELLIPSIS.split(quote)]
    pieces = [p for p in pieces if p]
    words = sum(len(p.split()) for p in pieces)
    return words >= 2 and all(f" {p} " in f" {cv_plain} " for p in pieces)


def verify(raw: EvidencedReading, text: str, total: int) -> FitReading:
    """Keep what the CV's own lines support: a rating above `UNQUOTED_CAP` needs its quote
    found in this CV, a requirement counts only with its quote found."""
    cv_plain = _plain(text)
    ratings: dict[str, int] = {}
    evidence: dict[str, str] = {}
    for name in WEIGHTS:
        line = getattr(raw, name)
        found = quoted(line.quote, cv_plain)
        ratings[name] = line.rating if found else min(line.rating, UNQUOTED_CAP)
        if found:
            evidence[name] = line.quote.strip()
    met: dict[int, str] = {}
    for item in raw.requirements_met:
        if 1 <= item.number <= total and item.number not in met and quoted(item.quote, cv_plain):
            met[item.number] = item.quote.strip()
    return FitReading(
        **ratings,
        requirements_shown=sorted(met),
        impression=raw.impression,
        evidence=evidence,
        requirement_quotes={str(n): q for n, q in sorted(met.items())},
    )


def consensus(runs: list[FitReading]) -> FitReading:
    """One reading from several: the median rating per dimension (its quote from a read that
    gave it), the requirements most reads found, the impression of the median read."""
    ratings: dict[str, int] = {}
    evidence: dict[str, str] = {}
    for name in WEIGHTS:
        ratings[name] = int(statistics.median_low(getattr(r, name) for r in runs))
        quote = next(
            (
                r.evidence[name]
                for r in runs
                if getattr(r, name) == ratings[name] and name in r.evidence
            ),
            None,
        )
        if quote:
            evidence[name] = quote
    counts = Counter(n for r in runs for n in r.requirements_shown)
    shown = sorted(n for n, c in counts.items() if c > len(runs) / 2)
    quotes = {
        str(n): next(r.requirement_quotes[str(n)] for r in runs if str(n) in r.requirement_quotes)
        for n in shown
    }
    middle = sorted(runs, key=_score)[(len(runs) - 1) // 2]
    return FitReading(
        **ratings,
        requirements_shown=shown,
        impression=middle.impression,
        evidence=evidence,
        requirement_quotes=quotes,
    )


def _read_once(
    texts: list[str], requirements: list[str], jd_text: str, llm: LLMProvider, run: int
) -> list[FitReading]:
    """All CVs in one call, in an order fixed by the run, the job and the texts (so neither
    an original nor a position is favoured); checked readings in the order given."""
    order = sorted(
        range(len(texts)),
        key=lambda i: hashlib.sha256(f"{run}\x00{jd_text}{texts[i]}".encode()).digest(),
    )
    listed = "\n".join(f"{n}. {r}" for n, r in enumerate(requirements, 1))
    cvs = "\n\n".join(f"<cv_{n}>\n{texts[i]}\n</cv_{n}>" for n, i in enumerate(order, 1))
    prompt = (
        f"<job_description>\n{jd_text}\n</job_description>\n\n"
        f"<requirements>\n{listed or 'none listed'}\n</requirements>\n\n{cvs}"
    )
    readout = llm.generate(system=VERSIONS_SYSTEM, prompt=prompt, output_model=EvidencedReadings)
    if len(readout.readings) != len(texts):
        raise ValueError(
            f"The reader returned {len(readout.readings)} readings for {len(texts)} CVs; try again"
        )
    by_index = dict(zip(order, readout.readings, strict=True))
    return [verify(by_index[i], texts[i], len(requirements)) for i in range(len(texts))]


def read_versions(
    texts: list[str],
    requirements: list[str],
    jd_text: str,
    llm: LLMProvider,
    known: list[FitReading | None] | None = None,
) -> list[FitReading]:
    """Every CV read `READS` times side by side (in parallel, each in another order), checked
    against its own lines and reduced to the median. A `known` reading (this CV read before)
    is kept; the reader is called only when a CV has none, and still sees them all."""
    if not 1 <= len(texts) <= MAX_VERSIONS:
        raise ValueError(f"Compare between 1 and {MAX_VERSIONS} CVs at a time")
    known = known or [None] * len(texts)
    if all(k is not None for k in known):
        return [k for k in known if k is not None]
    runs = [(contextvars.copy_context(), run) for run in range(READS)]
    with ThreadPoolExecutor(max_workers=READS) as pool:
        reads = list(
            pool.map(lambda r: r[0].run(_read_once, texts, requirements, jd_text, llm, r[1]), runs)
        )
    return [k or consensus([read[i] for read in reads]) for i, k in enumerate(known)]


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
            "evidence": reading.evidence,
            "requirement_quotes": reading.requirement_quotes,
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


def verdict(version: VersionReading, baseline: VersionReading) -> tuple[str, str]:
    """Did tailoring change how the fit reads? Fixed margins: the score by `NOISE_MARGIN`,
    any requirement gained or lost (each backed by a checked quote). Exact keywords are
    diagnostic, not proof of overall fit. Returns (better | worse | mixed | same, why)."""
    gain = version.score - baseline.score
    gained = set(version.requirements_shown) - set(baseline.requirements_shown)
    lost = set(baseline.requirements_shown) - set(version.requirements_shown)
    added = [k for k in version.keywords_found if k not in baseline.keywords_found]
    dropped = [k for k in baseline.keywords_found if k not in version.keywords_found]
    up: list[str] = []
    down: list[str] = []
    if gain >= NOISE_MARGIN:
        up.append(f"fit score {gain:+d}")
    elif gain <= -NOISE_MARGIN:
        down.append(f"fit score {gain:+d}")
    if gained:
        up.append(f"{len(gained)} more requirement{'s' * (len(gained) > 1)} shown")
    if lost:
        down.append(f"{len(lost)} requirement{'s' * (len(lost) > 1)} no longer shown")
    keywords = []
    if added:
        keywords.append(f"job keyword{'s' * (len(added) > 1)} added: {', '.join(added)}")
    if dropped:
        keywords.append(f"job keyword{'s' * (len(dropped) > 1)} lost: {', '.join(dropped)}")
    detail = f" Keyword detail: {'; '.join(keywords)}." if keywords else ""
    if up and down:
        return "mixed", f"Mixed: {', '.join(up)}, but {', '.join(down)}.{detail}"
    if up:
        return "better", f"Meaningful improvement: {', '.join(up)}.{detail}"
    if down:
        return "worse", f"Reads worse: {', '.join(down)}.{detail}"
    return "same", (
        f"No meaningful change: fit score {gain:+d} (within ±{NOISE_MARGIN - 1}), the same "
        f"requirements shown.{detail}"
    )


def score_versions(
    items: list[VersionReading],
    texts: list[str],
    readings: list[FitReading],
    requirements: list[str],
    keywords: list[str],
) -> list[VersionReading]:
    """Measure every CV and set each tailored version against its original (`baseline_id`,
    which must be among `items`), with its verdict."""
    measured = [
        _measure(item, text, reading, len(requirements), keywords)
        for item, text, reading in zip(items, texts, readings, strict=True)
    ]
    by_id = {m.id: m for m in measured}
    out: list[VersionReading] = []
    for item in measured:
        baseline = by_id.get(item.baseline_id or "")
        if item.kind == "tailored" and baseline is not None:
            judged, reason = verdict(item, baseline)
            item = item.model_copy(
                update={
                    "dimensions": [
                        DimensionChange(name=mine.name, before=theirs.after, after=mine.after)
                        for mine, theirs in zip(item.dimensions, baseline.dimensions, strict=True)
                    ],
                    "areas": version_areas(item, baseline, requirements),
                    "verdict": judged,
                    "verdict_reason": reason,
                }
            )
        out.append(item)
    return out


def _tiebreak(r: VersionReading) -> tuple[int, int, bool, float, int]:
    """For close scores, use checked requirements and score before literal keywords."""
    return (
        len(r.requirements_shown),
        r.score,
        r.kind == "tailored",
        r.first_page_coverage,
        len(r.keywords_found),
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def _why_first(best: VersionReading, other: VersionReading, total: int) -> tuple[str, str]:
    """(why `best` ranks above `other` within the margin, what `other` does better or '')."""
    req = (len(best.requirements_shown), len(other.requirements_shown))
    kw = (len(best.keywords_found), len(other.keywords_found))
    if req[0] != req[1]:
        why = f"it shows more of the job's requirements ({req[0]} vs {req[1]} of {total})"
    elif best.score != other.score:
        why = f"its score is a little higher ({best.score} vs {other.score}), within the margin"
    elif best.kind != other.kind:
        why = "they show the same substantive fit; the tailored version is preferred"
    elif best.first_page_coverage != other.first_page_coverage:
        why = "more job keywords are on its first page"
    elif kw[0] != kw[1]:
        why = f"it has more of the job's keywords word for word ({kw[0]} vs {kw[1]})"
    else:
        why = "they read the same; a tailored version is preferred"
    better: list[str] = []
    if kw[1] > kw[0]:
        found = [k for k in other.keywords_found if k not in best.keywords_found]
        better.append(f"has more job keywords ({kw[1]} vs {kw[0]}: {', '.join(found)})")
    if other.score > best.score:
        better.append(f"scores {other.score - best.score} points higher, within the margin")
    return why, (f"{other.label} {' and '.join(better)}." if better else "")


def best_version(readings: list[VersionReading], total: int) -> tuple[str | None, str]:
    """The CV that shows the fit most clearly, and why. Scores within `NOISE_MARGIN` of the
    top count as the same, so checked requirements and score decide before exact
    job keywords (`_tiebreak`); the sentence names what decided and what the runner-up does
    better."""
    if not readings:
        return None, ""
    top = max(r.score for r in readings)
    close = [r for r in readings if top - r.score < NOISE_MARGIN]
    best = max(close, key=_tiebreak)
    others = sorted((r for r in close if r is not best), key=_tiebreak, reverse=True)
    shown = f"; {len(best.requirements_shown)} of {total} requirements shown" if total else ""
    if others:
        runner = others[0]
        why, other_better = _why_first(best, runner, total)
        same = f"{best.label} and {runner.label} score about the same ({best.score} vs " + (
            f"{runner.score}/100, within ±{NOISE_MARGIN - 1}). "
        )
        rest = f" {other_better}" if other_better else ""
        return best.id, f"{same}{best.label} ranks first because {why}.{rest}"
    if best.kind == "original":
        if not any(r.kind == "tailored" for r in readings):
            return best.id, f"{best.label} reads best for this job: {best.score}/100{shown}."
        return best.id, (
            f"No tailored version reads better than {best.label} for this job "
            f"({best.score}/100); try another level or emphasis, or check the wording."
        )
    baseline = next((r for r in readings if r.id == best.baseline_id), None)
    gain = f" ({best.score - baseline.score:+d} on its original)" if baseline else ""
    return best.id, (
        f"{best.label} shows the fit most clearly: {best.score}/100{gain}{shown}, more than "
        f"{NOISE_MARGIN - 1} points ahead of the next CV."
    )
