"""job_matcher: semantic screening of the shortlist against a ProfileSummary.

The deterministic scorer only pre-filters (hard constraints + cheap ranking). This step
decides what truly matches: the LLM reads each posting the way a recruiter would and rates
six dimensions on a fixed 0-4 scale whose levels are described in the prompt; code turns the
levels into points, caps and the verdict.

Consistency: postings are screened in small batches (separate calls, fixed order, each with
only the profile and its own postings), every assessment is remembered (`VerdictCache`) so a
posting already judged for the same profile, constraints and model keeps its verdict, and a
posting near the threshold or with ambiguous midrange seniority and leadership gets one more
independent assessment, averaged with the first (rounded down).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.core.llm.calls import call_group, cancel_group
from src.core.llm_provider import LLMError, LLMProvider
from src.jobs.models import (
    FIT_WEIGHTS,
    FitBand,
    FitRatings,
    JobAssessment,
    JobPosting,
    JobVerdict,
    MatchReport,
    Priority,
    ProfileSummary,
    SearchQuery,
    rating_points,
)
from src.tools.search_tools import company_key, posting_description, posting_excerpt

MATCHER_SYSTEM = """You are job_matcher, an executive research specialist screening live \
vacancies for one candidate, in whatever field they work. You receive the candidate's \
profile summary (the only authority on what they can do), optionally their career intent \
(what they want), their base and constraints, and a batch of postings. Return one \
assessment per posting, in the same order, with the same job_id.

Rate fit on six dimensions, each on this fixed scale (pick the level whose description \
fits; when torn between two, pick the lower):
- function (weight 30): 4 the candidate does this day-to-day work now or in the last role · \
3 did it in an earlier role, or most of it now · 2 adjacent work with a clear transfer · \
1 shares only tools, subject matter or setting · 0 a different discipline
- domain (20): 4 the same field, subject matter or technology · 3 a close neighbour · \
2 a related field, the core knowledge transfers · 1 distant link · 0 unrelated. In a \
cross-functional role (e.g. business development, investment, consulting or medical \
affairs within the candidate's field) rate the subject matter the role works on.
- seniority (20): 4 same level and scope · 3 one step up or down · 2 two steps, or scope \
unclear · 1 three steps · 0 far off
- leadership (10): 4 the same kind and size of leadership as done now (or none required \
and none expected) · 3 smaller or larger by one step · 2 a different mode (matrix vs line) \
· 1 much more than ever done · 0 a kind never done that the role centres on
- sector (10): 4 the industry and organisation type the candidate works in (see domains) · \
3 another organisation type in the same industry (a supplier, service provider, \
consultancy or investor serving it) · 2 a different organisation type with real overlap \
(academia, public sector, healthcare vs industry) · 1 distant · 0 unrelated
- practicality (10): 4 remote, or within easy reach of the base in an accepted pattern · \
3 reachable with some commute · 2 long commute or relocation the candidate accepts · \
1 hard (far, onsite) · 0 unworkable under the stated constraints
Code turns levels into points (level/4 of the weight) and a 0-100 total; bands: 90+ \
exceptional · 80s very strong · 70s strong · 60s reasonable stretch · below 60 weak. \
A sound adjacent role is a stretch, not a zero. Write the evidence (reasons, \
transferable, gaps, unknowns) first, then give the levels that evidence supports. \
Judge each posting on its own, never relative to the others in the batch.

Role families: the profile's role_families are the searches run for this candidate. A \
posting in an "adjacent" family (a different function with cited CV evidence and a stated \
gap) is not "a different discipline": rate function on how much of its day-to-day work \
the evidence covers (usually 1-2), put the family's gap under gaps, and never make it a \
dealbreaker on discipline alone.

Career intent (when given) is what the candidate wants, not what they can do: it never \
changes the levels. Set alignment: "aligned" when the role clearly serves their direction, \
energising work, a target area or a preferred sector or organisation type; "against" when \
it centres on work they want to avoid, an avoided sector or a soft dealbreaker; otherwise \
"neutral". alignment_note names which (under 15 words). Without a career intent, \
alignment is neutral.

Requirements check (do this first, for every posting with a description): find the \
posting's core requirements: what the role is built on and an employer would screen \
candidates out for, stated as essential or required, or plainly the centre of the job even \
when not labelled so (e.g. "PhD in a relevant field", "5+ years leading GMP manufacturing", \
"you will design novel algorithms"). Before calling one unmet, check the profile's \
key_skills, capabilities, core_expertise and transferable_strengths for direct or clearly \
equivalent evidence. A core requirement with none goes in essential_unmet, in the posting's \
words, at most 2 per posting, and function is then at most 2. Never core: day-to-day duties \
or outputs (producing reports, presentations, coordinating meetings), common tools learnable \
within weeks (Linux, git, Excel, a named software package), organisation-specific knowledge, \
and desirable or nice-to-have items; list those under gaps when the profile lacks them. In \
an adjacent-family posting, a requirement the family's evidence covers through transferable \
experience is not unmet; a core skill or experience nothing in the profile shows is unmet, \
as for any role.

Rules:
- Distinguish direct experience (reasons) from transferable experience (transferable). \
Never let transferable experience silently stand in for direct experience. Judge \
transferable experience on the underlying skill, not the label: a different technique, \
tool or product is not automatically disqualifying.
- Never stretch the profile to make a role fit. A gap is a gap. Never invent posting details.
- Many postings are alert snippets (title, company, location, a line or two). Judge from \
what is there: score a dimension the posting is silent on from what the title, company and \
level imply, and list what is missing under unknowns. Silence is not a gap.
- essential_unmet: core requirements (see the requirements check) the profile does not \
evidence. Code never lists such a role as a match, so be exact: neither miss one nor list \
what the profile does show.
- dealbreakers: only a clearly different discipline, a role in not_a_fit, or a stated \
constraint violated. A dealbreaker means the role should not be listed.
- Keyword overlap must never inflate a score where function, level or location are wrong.
- fit_summary says, in plain words, how and how well the role matches (e.g. "Direct match \
on the core method; one level junior; 12 miles from base"). Reasons, transferable and \
gaps cite specifics from the posting and the profile.
- Be concise: fit_summary is one sentence; every list has at most 3 items (the most \
important first), each a short clause of under 20 words. The scores carry the judgement."""

# A total is capped here when any one dimension is below this share of its weight: keyword
# density must not hide a real mismatch.
CAP_SCORE = 65
# A role missing a core requirement is never a match: its score is capped below the listing
# floor (60) and `match` is false whatever the threshold. There is no point applying for a
# role that needs what the CV does not show.
UNMET_CAP = 55
# A role whose requirements could not be read may still be listed, but never as a high scorer:
# capped as a low stretch, under the checked matches, until its full posting is read.
UNCHECKED_CAP = 62
# Less posting text than this (and no structured requirements) cannot show the requirements:
# alert listings and cards LinkedIn refused to open.
CHECKABLE_CHARS = 600
WEAK_DIMENSION_SHARE = 0.4
# Scores this close to the threshold (either side) are flagged: different models, or the same
# model on another run, typically differ by ~10 points, so these could fall either way.
BORDERLINE_MARGIN = 5
# Explanation points kept per list (the prompt asks for at most this many; the UI shows them).
MAX_POINTS = 3

# Bump whenever the prompt or the scale changes: remembered verdicts from another version are
# not reused.
MATCHER_VERSION = "levels-4"  # tighter core requirements; a block needs two readings to agree
CACHE_SIZE = 5000  # remembered assessments kept (newest)

DESCRIPTION_CHARS = 3000  # long postings keep their requirements section (`posting_excerpt`)
NO_DESCRIPTION = "(not available: alert listing; judge from title, company and location)"


class ScreeningBatch(BaseModel):
    verdicts: list[JobAssessment] = Field(description="Exactly one per posting, same job_id")


def _band(score: int) -> FitBand:
    for floor, band in ((90, "exceptional"), (80, "very_strong"), (70, "strong"), (60, "stretch")):
        if score >= floor:
            return band  # type: ignore[return-value]
    return "weak"


def _priority(score: int, blocked: bool, against_intent: bool = False) -> Priority:
    """From the fit score; a role against the career intent is at most "consider" (still
    listed: the user decides, the intent only steers)."""
    if blocked or score < 60:
        return "low"
    if against_intent:
        return "consider"
    return "apply_now" if score >= 80 else "worth_applying" if score >= 70 else "consider"


def checkable(job: JobPosting) -> bool:
    """Whether the posting shows enough to check its requirements against the profile."""
    return bool(job.required_skills) or len(posting_description(job.description)) >= CHECKABLE_CHARS


def finalize(
    assessment: JobAssessment,
    threshold: float,
    *,
    reviewed: bool = False,
    remembered: bool = False,
    checked: bool = True,
) -> JobVerdict:
    """Code decides: points from the levels, total = their sum. An unmet core requirement caps
    it below the listing floor and is never a match; a posting whose requirements could not
    be read (`checked=False`) is capped as a low stretch; a weak dimension caps it at 65.
    Band, priority and match follow from the final score."""
    points = rating_points(assessment.ratings)
    dims = points.model_dump()
    total = sum(min(dims[name], weight) for name, weight in FIT_WEIGHTS.items())
    weak = [n for n, w in FIT_WEIGHTS.items() if dims[n] < WEAK_DIMENSION_SHARE * w]
    unmet = bool(assessment.essential_unmet)
    cap, cap_reason = total, None
    if unmet:
        cap = UNMET_CAP
        cap_reason = "core requirement unmet: " + "; ".join(assessment.essential_unmet)
    elif not checked and total > UNCHECKED_CAP:
        cap = UNCHECKED_CAP
        cap_reason = "requirements not checked: the full posting could not be read yet"
    elif total > CAP_SCORE and weak:
        cap, cap_reason = CAP_SCORE, "weak on " + ", ".join(weak)
    score = min(total, cap)
    blocked = bool(assessment.dealbreakers)
    eligible = not blocked and not unmet
    lists = ("reasons", "transferable", "gaps", "essential_unmet", "unknowns", "dealbreakers")
    trimmed = {k: v[:MAX_POINTS] if k in lists else v for k, v in assessment.model_dump().items()}
    return JobVerdict(
        **trimmed,
        dimensions=points,
        reviewed=reviewed,
        from_memory=remembered,
        fit_score=score,
        band=_band(score),
        priority=_priority(score, blocked, assessment.alignment == "against"),
        match=score >= threshold and eligible,
        borderline=eligible and checked and abs(score - threshold) <= BORDERLINE_MARGIN,
        cap_reason=cap_reason,
        requirements_checked=checked,
    )


def combine(
    first: JobAssessment, second: JobAssessment, third: JobAssessment | None = None
) -> JobAssessment:
    """Independent assessments of one posting: each level is the mean of the first two,
    rounded down (a tie resolves to the cautious one); the text of the first is kept with the
    union of dealbreakers. An unmet core requirement blocks a match outright, so it stands
    only when most readings find one (`third` breaks a tie between the first two); then
    every reading's unmet requirements are kept."""
    a, b = first.ratings.model_dump(), second.ratings.model_dump()
    ratings = FitRatings(**{n: (a[n] + b[n]) // 2 for n in a})
    readings = [r for r in (first, second, third) if r is not None]
    blocking = [r for r in readings if r.essential_unmet]
    agreed = 2 * len(blocking) > len(readings)
    unmet = [u for r in blocking for u in r.essential_unmet] if agreed else []
    dealbreakers = [*first.dealbreakers, *second.dealbreakers]
    union: dict[str, Any] = {
        "dealbreakers": list(dict.fromkeys(dealbreakers))[:MAX_POINTS],
        "essential_unmet": list(dict.fromkeys(unmet))[:MAX_POINTS],
    }
    if second.alignment == "against" and first.alignment != "against":
        union |= {"alignment": "against", "alignment_note": second.alignment_note}
    return first.model_copy(update={"ratings": ratings, **union})


def _needs_review(assessment: JobAssessment, threshold: float, checked: bool = True) -> bool:
    """Recheck every match, threshold cases and ambiguous midrange seniority/leadership
    judgments. Matches are rechecked because a lenient first pass can miss an unmet essential
    and rank a weak job at the top; they are few, so the extra calls are cheap. A posting
    without readable requirements is not rechecked: a second reading cannot see more."""
    if not checked:
        return False
    verdict = finalize(assessment, threshold)
    ratings = assessment.ratings
    unmet_only = (
        bool(assessment.essential_unmet)
        and not assessment.dealbreakers
        and finalize(assessment.model_copy(update={"essential_unmet": []}), threshold).match
    )
    return (
        verdict.match
        or verdict.borderline
        or unmet_only  # one reading's unmet requirement alone would drop a good role
        or (
            65 <= verdict.fit_score <= 80
            and ratings.seniority in (2, 3)
            and ratings.leadership in (2, 3)
            and not assessment.dealbreakers
        )
    )


class _Remembered(BaseModel):
    assessment: JobAssessment
    reviewed: bool = False
    saved_on: date


class VerdictCache:
    """Assessments remembered across searches, keyed by the posting's content, the profile
    summary, the constraints, the model and MATCHER_VERSION. Same inputs -> same verdict, and
    repeat searches only screen postings not judged before. Stored in `data/` (personal)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, _Remembered] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._items = {k: _Remembered.model_validate(v) for k, v in raw.items()}
            except (OSError, ValueError):
                self._items = {}  # unreadable: start over rather than block screening

    @staticmethod
    def key(job: JobPosting, context: str) -> str:
        canonical = job.model_copy(
            update={
                "company": company_key(job.company),
                "description": posting_description(job.description),
            }
        )
        posting = job_digest(canonical).replace(f'job_id="{job.id}"', "")
        return hashlib.sha256(f"{MATCHER_VERSION}\n{context}\n{posting}".encode()).hexdigest()

    @staticmethod
    def legacy_key(job: JobPosting, context: str) -> str:
        """Find assessments saved before presentation text was normalized."""
        posting = job_digest(job).replace(f'job_id="{job.id}"', "")
        return hashlib.sha256(f"{MATCHER_VERSION}\n{context}\n{posting}".encode()).hexdigest()

    def get(self, key: str) -> _Remembered | None:
        return self._items.get(key)

    def put(self, key: str, assessment: JobAssessment, reviewed: bool) -> None:
        self._items[key] = _Remembered(
            assessment=assessment, reviewed=reviewed, saved_on=date.today()
        )

    def save(self) -> None:
        newest = sorted(self._items.items(), key=lambda kv: kv[1].saved_on, reverse=True)
        data = {k: v.model_dump(mode="json") for k, v in newest[:CACHE_SIZE]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")


def matcher_profile(summary: ProfileSummary) -> str:
    """The summary as the job_matcher sees it: families code rejected as unrealistic are left
    out, so they never vouch for a posting."""
    kept = [f for f in summary.role_families if f.rejected is None]
    return summary.model_copy(update={"role_families": kept}).model_dump_json(
        exclude={"role_families": {"__all__": {"rejected"}}}
    )


def job_digest(job: JobPosting) -> str:
    """Compact, LLM-readable posting."""
    lines = [
        f'<posting job_id="{job.id}">',
        f"title: {job.title}",
        f"company: {job.company}",
        f"location: {job.location or 'unknown'} ({job.work_arrangement})",
    ]
    if job.salary_range:
        estimate = " (may be the board's estimate)" if job.salary_maybe_estimated else ""
        lines.append(f"salary: {job.salary_range}{estimate}")
    if job.required_skills:
        lines.append(f"required: {', '.join(job.required_skills)}")
    if job.preferred_skills:
        lines.append(f"preferred: {', '.join(job.preferred_skills)}")
    if job.required_certifications:
        lines.append(f"required certifications: {', '.join(job.required_certifications)}")
    if job.min_years_experience:
        lines.append(f"min years: {job.min_years_experience}")
    desc = posting_excerpt(job.description, DESCRIPTION_CHARS)
    lines += [
        f"description: {desc or NO_DESCRIPTION}",
        "</posting>",
    ]
    return "\n".join(lines)


def _constraints(query: SearchQuery | None, base: str = "") -> str:
    parts = [f"candidate base: {base}"] if base else []
    if query is not None:
        if query.locations:
            parts.append(
                f"locations: {'; '.join(query.place_names())} (within {query.distance_miles} mi)"
            )
        elif query.country:
            parts.append(f"country: {query.country}")
        if query.work_arrangements:
            parts.append(f"arrangements: {', '.join(query.work_arrangements)}")
        if query.salary_min or query.salary_max:
            parts.append(f"salary: {query.salary_min or '-'} to {query.salary_max or '-'}")
    return "; ".join(parts) or "none"


async def _assess(
    llm: LLMProvider, prompt: str, group: str | None, timeout: float, stop: asyncio.Event | None
) -> ScreeningBatch:
    """One batch call under its own cancellable group. It ends at whichever comes first: the
    answer, `stop`, or `timeout`; on the last two its CLI process (if any) is killed and a
    still-running API call is abandoned (its late answer is ignored)."""
    token = call_group.set(group)
    try:
        # The task copies the context, so the call's CLI process joins `group`.
        call = asyncio.ensure_future(
            asyncio.to_thread(
                llm.generate, system=MATCHER_SYSTEM, prompt=prompt, output_model=ScreeningBatch
            )
        )
    finally:
        call_group.reset(token)
    stopper = asyncio.ensure_future(stop.wait()) if stop is not None else None
    waiting = {call, stopper} if stopper is not None else {call}
    done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    if stopper is not None:
        stopper.cancel()
    if call in done:
        return call.result()
    if group:
        cancel_group(group)
    call.cancel()
    if stop is not None and stop.is_set():
        raise LLMError("stopped")
    raise LLMError(f"no answer within {timeout:.0f}s")


async def _review(
    runner: _Runner,
    first: dict[str, JobAssessment],
    by_id: dict[str, JobPosting],
    threshold: float,
    note: Callable[[str], Awaitable[None]] | None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> dict[str, tuple[JobAssessment, bool]]:
    """Second, independent single-posting assessment for every match and borderline first
    verdict; returns (assessment to keep, whether it was reviewed) by job id."""
    near = sorted(
        j for j, a in first.items() if _needs_review(a, threshold, checkable(by_id[j]))
    )
    if near and note is not None:
        await note(f"Second opinion on {len(near)} matching or borderline posting(s)")
    if near and progress is not None:
        await progress(0, len(near))
    second = await runner.run([by_id[j] for j in near], 1, progress) if near else {}
    # The two readings disagree on whether a core requirement is unmet: a third decides.
    split = sorted(
        j for j in second if bool(first[j].essential_unmet) != bool(second[j].essential_unmet)
    )
    if split and note is not None:
        await note(f"Third reading on {len(split)} posting(s) the first two disagreed on")
    third = await runner.run([by_id[j] for j in split], 1, None) if split else {}
    return {
        j: (combine(a, second[j], third.get(j)), True) if j in second else (a, False)
        for j, a in first.items()
    }


class _Runner:
    """Runs batches of postings through the matcher, each batch a separate call with only the
    profile header and its own postings, at most `concurrency` at a time."""

    def __init__(
        self,
        llm: LLMProvider,
        header: str,
        concurrency: int,
        timeout: float,
        stop: asyncio.Event | None,
    ) -> None:
        self.llm, self.header, self.timeout, self.stop = llm, header, timeout, stop
        self.sem = asyncio.Semaphore(concurrency)
        self.parent = call_group.get()
        self.errors: list[str] = []
        self._calls = 0

    def _stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    async def run(
        self,
        jobs: list[JobPosting],
        size: int,
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, JobAssessment]:
        """Assessments by job id; `progress(done, total)` after each batch."""
        done = 0

        async def one(batch: list[JobPosting]) -> list[JobAssessment]:
            nonlocal done
            out = await self._batch(batch)
            done += len(batch)
            if progress is not None:
                await progress(done, len(jobs))
            return out

        groups = [jobs[i : i + size] for i in range(0, len(jobs), size)]
        results = await asyncio.gather(*(one(b) for b in groups))
        return {a.job_id: a for out in results for a in out}

    async def _batch(self, batch: list[JobPosting]) -> list[JobAssessment]:
        """One call, retried once; a batch that fails twice is reported, not raised."""
        self._calls += 1
        index = self._calls
        prompt = self.header + "\n\n".join(job_digest(j) for j in batch)
        out = ScreeningBatch(verdicts=[])
        async with self.sem:
            for attempt in (1, 2):
                if self._stopped():
                    self.errors.append(f"batch {batch[0].id}..: stopped before screening")
                    break
                group = f"{self.parent}/b{index}.{attempt}" if self.parent else None
                try:
                    out = await _assess(self.llm, prompt, group, self.timeout, self.stop)
                    break
                except (LLMError, OSError) as exc:
                    if attempt == 2 or self._stopped():
                        self.errors.append(f"batch {batch[0].id}..: {exc}")
        wanted = {j.id for j in batch}
        return [v for v in out.verdicts if v.job_id in wanted]


def _recall(
    jobs: list[JobPosting],
    keys: dict[str, str],
    context: str,
    cache: VerdictCache | None,
    threshold: float,
) -> tuple[dict[str, JobVerdict], dict[str, JobAssessment], list[JobPosting]]:
    """Split jobs into (remembered verdicts, remembered but unreviewed borderline assessments
    to recheck, postings never judged)."""
    verdicts: dict[str, JobVerdict] = {}
    pending: dict[str, JobAssessment] = {}
    todo: list[JobPosting] = []
    for job in jobs:
        hit = None
        if cache is not None:
            hit = cache.get(keys[job.id]) or cache.get(VerdictCache.legacy_key(job, context))
        if hit is None:
            todo.append(job)
            continue
        if cache is not None and cache.get(keys[job.id]) is None:
            cache.put(keys[job.id], hit.assessment, hit.reviewed)
        found = hit.assessment.model_copy(update={"job_id": job.id})
        if not hit.reviewed and _needs_review(found, threshold, checkable(job)):
            pending[job.id] = found
        else:
            verdicts[job.id] = finalize(
                found, threshold, reviewed=hit.reviewed, remembered=True, checked=checkable(job)
            )
    return verdicts, pending, todo


async def screen_jobs(
    summary: ProfileSummary,
    jobs: list[JobPosting],
    llm: LLMProvider,
    query: SearchQuery | None = None,
    batch_size: int = 3,
    concurrency: int = 6,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
    threshold: float = 60,
    base: str = "",
    *,
    batch_timeout: float = 240,
    stop: asyncio.Event | None = None,
    cache: VerdictCache | None = None,
    model: str = "",
    note: Callable[[str], Awaitable[None]] | None = None,
    intent: str = "",
    review_llm: LLMProvider | None = None,
    review_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> tuple[dict[str, JobVerdict], list[str]]:
    """Screen `jobs`. Returns (final verdicts by job id, errors). Failed batches are reported.

    Remembered assessments (`cache`, keyed with `model`) are reused; the rest are screened in
    batches of `batch_size` in a fixed order. Matches, postings within BORDERLINE_MARGIN of
    the threshold, or those with ambiguous midrange seniority and leadership, get a second,
    single-posting assessment, averaged with the first. `progress`
    follows the first pass; `note` reports the second. A batch with no answer within
    `batch_timeout` is killed and retried once; `stop` skips batches not yet started.
    `intent` is the career intent as prompt text (`profile_memory.intent_text`).
    `review_llm` (the quality model) gives the second opinions; without it, `llm` does.
    `review_progress(done, total)` follows the second opinions.
    """
    constraints = _constraints(query, base)
    profile = matcher_profile(summary)
    header = (
        f"<candidate_profile>\n{profile}\n</candidate_profile>\n{intent}"
        f"<constraints>{constraints}</constraints>\n\n"
    )
    context = f"{model}\n{constraints}\n{profile}\n{intent}"
    keys = {j.id: VerdictCache.key(j, context) for j in jobs}
    runner = _Runner(llm, header, concurrency, batch_timeout, stop)

    verdicts, pending, todo = _recall(jobs, keys, context, cache, threshold)
    todo.sort(key=lambda j: keys[j.id])  # the same postings always meet in the same batches
    first = {**pending, **await runner.run(todo, batch_size, progress)}
    reviewer = runner
    if review_llm is not None:
        reviewer = _Runner(review_llm, header, concurrency, batch_timeout, stop)
    final = await _review(
        reviewer, first, {j.id: j for j in jobs}, threshold, note, review_progress
    )
    if reviewer is not runner:
        runner.errors += reviewer.errors
    by_id = {j.id: j for j in jobs}
    for job_id, (assessment, reviewed) in final.items():
        checked = checkable(by_id[job_id])
        if cache is not None and (reviewed or not _needs_review(assessment, threshold, checked)):
            cache.put(keys[job_id], assessment, reviewed)  # an unreviewed match is retried
        verdicts[job_id] = finalize(assessment, threshold, reviewed=reviewed, checked=checked)
    if cache is not None:
        cache.save()
    return verdicts, runner.errors


def apply_verdicts(report: MatchReport, verdicts: dict[str, JobVerdict], threshold: float) -> None:
    """Re-bucket a deterministic report using AI verdicts (in place).

    matches          = verdict.match (fit_score >= threshold, no dealbreakers) on a posting
                       whose requirements were checked, best first
    to_check         = would match, but the requirements could not be read yet
    below_threshold  = everything else that was scored (screened-out or not screened)
    """
    report.threshold = threshold  # the pre-filter's cut (0 in smart mode) no longer applies
    pool = report.scored()
    for r in pool:
        r.verdict = verdicts.get(r.job.id)
        r.passed = bool(r.verdict and r.verdict.match and r.verdict.fit_score >= threshold)
    report.matches = sorted(
        (r for r in pool if r.passed and r.verdict and r.verdict.requirements_checked),
        key=lambda r: r.rank_key(),
        reverse=True,
    )
    report.to_check = sorted(
        (r for r in pool if r.passed and r.verdict and not r.verdict.requirements_checked),
        key=lambda r: r.rank_key(),
        reverse=True,
    )
    rest = [r for r in pool if not r.passed]
    report.below_threshold = sorted(
        rest, key=lambda r: (r.verdict is not None, r.rank_key()), reverse=True
    )
    report.screened = True
