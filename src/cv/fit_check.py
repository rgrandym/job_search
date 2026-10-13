"""How much better the tailored CV shows the fit for one job, against the CV it came from.

    compare_fit: code measures keyword coverage (whole CV and first page), then one LLM call
    reads both CVs as a recruiter would (in an order that does not reveal which is which;
    every rating and requirement must quote a line code finds in that CV) -> FitComparison

    read_versions: several CVs for one job read side by side by the same blind reader,
    `READS` times in different orders; every rating and requirement must quote a line that
    code finds in that CV, and the median of the reads counts -> FitReading per CV
    score_versions: each tailored version set against its original, with a verdict by fixed
    margins -> VersionReading per CV; best_version picks the clearest beyond `NOISE_MARGIN`
    with_proposals + project: an optimised draft read again with its pending proposals in
    place -> ProjectedFit (hypothetical: what confirming them would show, and which proposal
    shows each newly met requirement)

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

from src.core.fit_rubric import FIT_WEIGHTS, capped_score, points_for
from src.core.llm_provider import LLMProvider
from src.cv.models import (
    AreaChange,
    Bullet,
    DimensionChange,
    EvidencedReading,
    EvidencedReadings,
    FitComparison,
    FitReading,
    JDAnalysis,
    MasterCV,
    ProjectedFit,
    ProposalGain,
    ReviewAddition,
    SearchFitBaseline,
    SkillGroup,
    TailoredCV,
    VersionReading,
)
from src.cv.tailor import MAX_ROLE_SKILLS, UNTRUSTED, ats_keywords, cv_to_text, printed_text
from src.tools.search_tools import mentions

WEIGHTS: dict[str, int] = {
    name: weight for name, weight in FIT_WEIGHTS.items() if name != "practicality"
}  # practicality comes from the search verdict; wording cannot change the commute
FIRST_PAGE_WORDS = 450  # what fits above the fold of page two on a typical CV
MAX_REQUIREMENTS = 15
MAX_VERSIONS = 6  # CVs read in one call; more and one reader's standard drifts
READS = 3  # independent reads per comparison; the median rating and majority requirements count
NOISE_MARGIN = 5  # score points: less than one rating step on a 20-weight dimension
UNQUOTED_CAP = 1  # a rating above this needs a line found in the CV
CORE_UNMET = "core requirement unmet:"  # the search verdict's cap_reason prefix (screener)

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
- function: 4 does this work now or in the last role; 3 did it earlier or most of it now; \
2 adjacent work with clear transfer; 1 shares only tools or subject; 0 different discipline
- domain: 4 same field or technology; 3 close neighbour; 2 related with transferable core \
knowledge; 1 distant link; 0 unrelated
- seniority: 4 same level and scope; 3 one step; 2 two steps or unclear; 1 three steps; 0 far off
- leadership: 4 same kind and size (or none required); 3 one step different; 2 different \
mode; 1 much more than shown; 0 a kind never shown that the role centres on
- sector: 4 same industry and organisation type; 3 same industry, different organisation \
type; 2 different type with real overlap; 1 distant; 0 unrelated
When torn between two ratings, give the lower one. `requirements_met`: each listed requirement \
a specific line of the CV shows the candidate meets, with that line copied word for word as \
`quote`; leave out any you would have to infer. Quotes are checked against the CV: a rating \
above {UNQUOTED_CAP} or a requirement whose quote is not in that CV does not count. \
`impression`: one sentence on what the top of the CV conveys for this job.
Use the same standard for every CV, so the ratings can be compared. Return one reading per \
CV in `readings`, in the order given: CV 1 first."""


def requirements_for(
    tailored: TailoredCV, jd: JDAnalysis, search_fit: SearchFitBaseline | None = None
) -> list[str]:
    """Read the core requirements the search found unmet first (whether a CV now shows them
    decides the search's cap), then pivotal requirements, other essentials and desirables."""
    rows = tailored.requirements
    ordered = sorted(
        rows,
        key=lambda r: (r.importance != "key", r.priority != "essential"),
    )
    listed = [r.requirement for r in ordered] or [*jd.must_have, *jd.nice_to_have]
    core = core_unmet(search_fit)
    seen = {_plain(c) for c in core}
    listed = [*core, *(r for r in listed if _plain(r) not in seen)]
    return list(dict.fromkeys(r for r in listed if r.strip()))[:MAX_REQUIREMENTS]


def core_unmet(search_fit: SearchFitBaseline | None) -> list[str]:
    """The core requirements the search verdict found unmet (they cap its score)."""
    reason = (search_fit.cap_reason if search_fit else None) or ""
    if not reason.startswith(CORE_UNMET):
        return []
    return [part.strip() for part in reason.removeprefix(CORE_UNMET).split(";") if part.strip()]


def _still_unmet(
    baseline: SearchFitBaseline,
    original: FitReading,
    version: FitReading,
    requirements: list[str] | None,
) -> bool:
    """Whether the search's unmet-core cap still holds for `version`. It lifts only when the
    version shows every core requirement the search found unmet, each with a line code found
    in that CV, and the original CV's reading did not: the cap came from what the CV showed,
    so a CV that now shows it is read as the matcher would read it."""
    if not (baseline.cap_reason or "").startswith(CORE_UNMET):
        return False
    core = [_plain(c) for c in core_unmet(baseline)]
    plain = [_plain(r) for r in requirements or []]
    if not core or any(c not in plain for c in core):
        return True
    numbers = {plain.index(c) + 1 for c in core}
    quoted_now = {int(n) for n in version.requirement_quotes}
    newly = (set(version.requirements_shown) & quoted_now) - set(original.requirements_shown)
    return not numbers <= newly


def ats_match(keywords: float, shown: int, total: int) -> int:
    """What screening software scores: the share of the job's keywords the CV contains and of
    its requirements a checked reading found shown, equally weighted (keywords alone when no
    requirements were read)."""
    requirements = shown / total if total else keywords
    return round(100 * (keywords + requirements) / 2)


def _coverage(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    return sum(mentions(text, k) for k in keywords) / len(keywords)


def _first_page(text: str) -> str:
    return " ".join(text.split()[:FIRST_PAGE_WORDS])


def _score(reading: FitReading) -> int:
    levels: dict[str, int] = {name: getattr(reading, name) for name in WEIGHTS}
    levels["practicality"] = 2  # neutral only when there is no saved search verdict
    return sum(points_for(levels).values())


def anchored_score(
    baseline: SearchFitBaseline,
    original: FitReading,
    version: FitReading,
    requirements: list[str] | None = None,
) -> int:
    """Apply CV visibility changes to the saved six-dimension search assessment.

    The candidate and job are unchanged. Only a dimension with a different CV reading
    moves; practicality stays fixed. The original score remains exactly the search score.
    Both sides go through the matcher's own capped total, so a capped search score moves
    only as far as the matcher itself would move it: an unmet-core cap lifts for a version
    that newly shows those requirements (`_still_unmet`; `requirements` as read).
    """
    unmet = (baseline.cap_reason or "").startswith(CORE_UNMET)
    checked = baseline.requirements_checked and not (baseline.cap_reason or "").startswith(
        "requirements not checked:"
    )
    old = capped_score(points_for(baseline.ratings), unmet=unmet, checked=checked)
    still = unmet and _still_unmet(baseline, original, version, requirements)
    new = capped_score(
        points_for(anchored_levels(baseline, original, version)), unmet=still, checked=checked
    )
    return max(0, min(100, baseline.score + new - old))


def anchored_levels(
    baseline: SearchFitBaseline, original: FitReading, version: FitReading
) -> dict[str, int]:
    """Search levels adjusted only by differences the two CV readings show."""
    old = baseline.ratings
    if set(old) != set(FIT_WEIGHTS) or any(not 0 <= value <= 4 for value in old.values()):
        raise ValueError("Saved search ratings are incomplete")
    new = dict(old)
    for name in WEIGHTS:
        new[name] = max(0, min(4, old[name] + getattr(version, name) - getattr(original, name)))
    return new


def _shown(reading: FitReading, total: int) -> int:
    return len({n for n in reading.requirements_shown if 1 <= n <= total})


def compare_fit(
    source: MasterCV,
    tailored: TailoredCV,
    jd: JDAnalysis,
    jd_text: str,
    llm: LLMProvider,
    search_fit: SearchFitBaseline | None = None,
) -> FitComparison:
    """Read the original and the tailored CV for this job and compare what each shows. An
    optimised draft with pending proposals is read a third time with them in place, in one
    checked call, so `if_confirmed` shows only what they add. The explicit version comparison
    still uses three independent reads."""
    before_text = cv_to_text(source, tags=False)
    after_text = printed_text(tailored)
    keywords = ats_keywords(jd)
    requirements = requirements_for(tailored, jd, search_fit)
    proposed = with_proposals(tailored)
    projection: ProjectedFit | None = None
    if proposed is None:
        before, after = _read_once([before_text, after_text], requirements, jd_text, llm, 0)
    else:
        texts = [before_text, after_text, printed_text(proposed)]
        before, after, projected = _read_once(texts, requirements, jd_text, llm, 0)
        projection = project(before, after, projected, _pending(tailored), requirements, search_fit)
        projection = with_keywords(projection, keywords, after_text, texts[2])
    dimensions = _dimensions(search_fit, before, after)
    worse = [f"{d.name}: {d.before} → {d.after}" for d in dimensions if d.after < d.before]
    shown_before, shown_after = _shown(before, len(requirements)), _shown(after, len(requirements))
    if shown_after < shown_before:
        worse.append(f"requirements shown: {shown_before} → {shown_after}")
    total = len(requirements)
    kw_before, kw_after = _coverage(before_text, keywords), _coverage(after_text, keywords)
    return FitComparison(
        before=search_fit.score if search_fit else _score(before),
        after=_scored(search_fit, before, after, requirements),
        search_anchored=search_fit is not None,
        dimensions=dimensions,
        requirements_total=len(requirements),
        requirements_before=shown_before,
        requirements_after=shown_after,
        keywords_before=kw_before,
        keywords_after=kw_after,
        ats_before=ats_match(kw_before, shown_before, total),
        ats_after=ats_match(kw_after, shown_after, total),
        requirements_missing=[
            r for n, r in enumerate(requirements, 1) if n not in set(after.requirements_shown)
        ],
        first_page_before=_coverage(_first_page(before_text), keywords),
        first_page_after=_coverage(_first_page(after_text), keywords),
        impression_before=before.impression,
        impression_after=after.impression,
        worse=worse,
        if_confirmed=projection,
    )


def _dimensions(
    search_fit: SearchFitBaseline | None, before: FitReading, after: FitReading
) -> list[DimensionChange]:
    """Each dimension before and after: the search's levels moved by the readings' change,
    else the readings themselves (practicality neutral)."""
    adjusted = anchored_levels(search_fit, before, after) if search_fit else None
    return [
        DimensionChange(
            name=name,
            before=search_fit.ratings[name]
            if search_fit
            else (2 if name == "practicality" else getattr(before, name)),
            after=adjusted[name]
            if adjusted
            else (2 if name == "practicality" else getattr(after, name)),
        )
        for name in FIT_WEIGHTS
    ]


def _scored(
    search_fit: SearchFitBaseline | None,
    original: FitReading,
    cv: FitReading,
    requirements: list[str] | None = None,
) -> int:
    return anchored_score(search_fit, original, cv, requirements) if search_fit else _score(cv)


def _levels(
    search_fit: SearchFitBaseline | None, original: FitReading, cv: FitReading
) -> dict[str, int]:
    if search_fit:
        return anchored_levels(search_fit, original, cv)
    return {n: 2 if n == "practicality" else getattr(cv, n) for n in FIT_WEIGHTS}


def _pending(tailored: TailoredCV) -> list[ReviewAddition]:
    return [a for a in tailored.review_additions if a.status == "pending"]


def with_proposals(tailored: TailoredCV) -> TailoredCV | None:
    """The draft as it would read with every pending proposal confirmed as worded, placed
    where confirming places it (`tailored_documents.review_addition`): an experience under
    its role, a skill in "Additional" and the role-skills line. None without proposals."""
    pending = _pending(tailored)
    if not pending:
        return None
    out = tailored.model_copy(deep=True)
    for n, item in enumerate(pending):
        if item.kind == "experience":
            role = next((r for r in out.cv.experience if r.id == item.role_id), None)
            if role is not None:
                role.bullets.append(Bullet(id=f"proposal-{n}", text=item.text))
            continue
        group = next((g for g in out.cv.skills if g.category == "Additional"), None)
        if group is None:
            out.cv.skills.append(SkillGroup(category="Additional", items=[item.text]))
        elif item.text not in group.items:
            group.items.append(item.text)
        out.role_skills = list(dict.fromkeys([*out.role_skills, item.text]))[:MAX_ROLE_SKILLS]
    return out


def project(
    original: FitReading,
    confirmed: FitReading,
    projected: FitReading,
    pending: list[ReviewAddition],
    requirements: list[str],
    search_fit: SearchFitBaseline | None = None,
) -> ProjectedFit:
    """The draft with its proposals against the draft as it is, all read in one pass. A newly
    shown requirement is credited to the proposal whose wording the reader quoted for it."""
    total = len(requirements)
    before = set(_numbers(confirmed, total))
    after = set(_numbers(projected, total))
    gained: list[ProposalGain] = []
    for n in sorted(after - before):
        quote = projected.requirement_quotes.get(str(n), "")
        source = next((a for a in pending if quote and quoted(quote, _plain(a.text))), None)
        gained.append(
            ProposalGain(
                requirement=requirements[n - 1],
                addition_id=source.id if source else None,
                quote=quote,
            )
        )
    credited = {g.addition_id for g in gained}
    old, new = _levels(search_fit, original, confirmed), _levels(search_fit, original, projected)
    return ProjectedFit(
        confirmed=_scored(search_fit, original, confirmed, requirements),
        projected=_scored(search_fit, original, projected, requirements),
        proposals=len(pending),
        dimensions=[DimensionChange(name=n, before=old[n], after=new[n]) for n in FIT_WEIGHTS],
        requirements_total=total,
        requirements_confirmed=len(before),
        requirements_projected=len(after),
        gained=gained,
        lost=[requirements[n - 1] for n in sorted(before - after)],
        no_effect=[a.id for a in pending if a.id not in credited],
        impression=projected.impression,
    )


def with_keywords(
    projection: ProjectedFit, keywords: list[str], confirmed: str, projected: str
) -> ProjectedFit:
    """The job terms an ATS or recruiter search would find only with the proposals."""
    found = set(_found(confirmed, keywords))
    before, after = _coverage(confirmed, keywords), _coverage(projected, keywords)
    total = projection.requirements_total
    return projection.model_copy(
        update={
            "keywords_confirmed": before,
            "keywords_projected": after,
            "keywords_added": [k for k in _found(projected, keywords) if k not in found],
            "ats_confirmed": ats_match(before, projection.requirements_confirmed, total),
            "ats_projected": ats_match(after, projection.requirements_projected, total),
        }
    )


def _numbers(reading: FitReading, total: int) -> list[int]:
    return sorted({n for n in reading.requirements_shown if 1 <= n <= total})


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
                DimensionChange(
                    name=n,
                    before=2 if n == "practicality" else getattr(reading, n),
                    after=2 if n == "practicality" else getattr(reading, n),
                )
                for n in FIT_WEIGHTS
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
    search_fit: SearchFitBaseline | None = None,
) -> list[VersionReading]:
    """Measure every CV and set each tailored version against its original (`baseline_id`,
    which must be among `items`), with its verdict."""
    measured = [
        _measure(item, text, reading, len(requirements), keywords)
        for item, text, reading in zip(items, texts, readings, strict=True)
    ]
    if search_fit is not None:
        original = readings[0]
        measured[0] = measured[0].model_copy(
            update={
                "score": search_fit.score,
                "dimensions": [
                    DimensionChange(
                        name=n, before=search_fit.ratings[n], after=search_fit.ratings[n]
                    )
                    for n in FIT_WEIGHTS
                ],
            }
        )
        for index in range(1, len(measured)):
            levels = anchored_levels(search_fit, original, readings[index])
            measured[index] = measured[index].model_copy(
                update={
                    "score": anchored_score(search_fit, original, readings[index], requirements),
                    "dimensions": [
                        DimensionChange(name=n, before=levels[n], after=levels[n])
                        for n in FIT_WEIGHTS
                    ],
                }
            )
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


def _tiebreak(r: VersionReading) -> tuple[int, int, float, int]:
    """For close scores, use checked requirements and score before literal keywords."""
    return (
        len(r.requirements_shown),
        r.score,
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
    elif best.first_page_coverage != other.first_page_coverage:
        why = "more job keywords are on its first page"
    elif kw[0] != kw[1]:
        why = f"it has more of the job's keywords word for word ({kw[0]} vs {kw[1]})"
    else:
        why = "the readings are otherwise the same"
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
    equal = [
        r
        for r in close
        if r.score == best.score and set(r.requirements_shown) == set(best.requirements_shown)
    ]
    if len(equal) > 1:
        labels = " and ".join(r.label for r in equal[:2])
        return None, (
            f"{labels} show the same substantive fit for this job "
            f"({best.score}/100, {len(best.requirements_shown)} of {total} requirements). "
            "No version shows a measurable improvement yet."
        )
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
