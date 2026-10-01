"""job_matcher subagent: semantic screening of a shortlist against a ProfileSummary.

The deterministic scorer only pre-filters (hard constraints + cheap ranking). This step
decides what truly matches: the LLM reads each posting the way a recruiter would and
returns a `JobVerdict`. Postings are screened in small batches, concurrently.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from src.core.llm_provider import LLMError, LLMProvider
from src.jobs.models import JobPosting, JobVerdict, MatchReport, ProfileSummary, SearchQuery

MATCHER_SYSTEM = """You are job_matcher, a senior technical recruiter screening job postings \
for one candidate. You receive the candidate's profile summary, their search constraints, \
and a batch of postings. Return one verdict per posting.

Judge real fit, not keyword overlap:
- Does the core of the role (what the person would actually do day to day) match the \
candidate's core expertise and target roles?
- Is the level right? Compare the role's real scope and seniority with the candidate's.
- Are the must-have requirements met by evidenced skills (expert/proficient count; \
"familiar" rarely satisfies a must-have)?
- Is the domain a fit or a reasonable transfer?
- Reject postings that match keywords but are a different discipline or listed in \
not_a_fit. Respect the constraints (location, arrangement, salary) when stated.

Scoring: 85-100 strong (apply now) · 70-84 good · 50-69 stretch · <50 poor.
Set match=true only for strong or good fits. Reasons and gaps must cite specifics from \
the posting and the profile. Be decisive and consistent; never invent posting details."""

DESCRIPTION_CHARS = 2500


class ScreeningBatch(BaseModel):
    verdicts: list[JobVerdict] = Field(description="Exactly one per posting, same job_id")


def job_digest(job: JobPosting) -> str:
    """Compact, LLM-readable posting."""
    lines = [
        f'<posting job_id="{job.id}">',
        f"title: {job.title}",
        f"company: {job.company}",
        f"location: {job.location or 'unknown'} ({job.work_arrangement})",
    ]
    if job.salary_range:
        lines.append(f"salary: {job.salary_range}")
    if job.required_skills:
        lines.append(f"required: {', '.join(job.required_skills)}")
    if job.preferred_skills:
        lines.append(f"preferred: {', '.join(job.preferred_skills)}")
    if job.required_certifications:
        lines.append(f"required certifications: {', '.join(job.required_certifications)}")
    if job.min_years_experience:
        lines.append(f"min years: {job.min_years_experience}")
    desc = job.description[:DESCRIPTION_CHARS]
    lines += [
        f"description: {desc or '(not available: judge from title/company only)'}",
        "</posting>",
    ]
    return "\n".join(lines)


def _constraints(query: SearchQuery | None) -> str:
    if query is None:
        return "none"
    parts = []
    if query.locations:
        parts.append(f"locations: {', '.join(query.locations)} (within {query.distance_miles} mi)")
    if query.work_arrangements:
        parts.append(f"arrangements: {', '.join(query.work_arrangements)}")
    if query.salary_min or query.salary_max:
        parts.append(f"salary: {query.salary_min or '-'} to {query.salary_max or '-'}")
    return "; ".join(parts) or "none"


async def screen_jobs(
    summary: ProfileSummary,
    jobs: list[JobPosting],
    llm: LLMProvider,
    query: SearchQuery | None = None,
    batch_size: int = 6,
    concurrency: int = 4,
) -> tuple[dict[str, JobVerdict], list[str]]:
    """Screen `jobs`. Returns (verdicts by job id, errors). Failed batches are reported."""
    header = (
        f"<candidate_profile>\n{summary.model_dump_json(indent=1)}\n</candidate_profile>\n"
        f"<constraints>{_constraints(query)}</constraints>\n\n"
    )
    sem = asyncio.Semaphore(concurrency)
    errors: list[str] = []

    async def run(batch: list[JobPosting]) -> list[JobVerdict]:
        prompt = header + "\n\n".join(job_digest(j) for j in batch)
        async with sem:
            try:
                out = await asyncio.to_thread(
                    llm.generate, system=MATCHER_SYSTEM, prompt=prompt, output_model=ScreeningBatch
                )
            except (LLMError, OSError) as exc:
                errors.append(f"batch {batch[0].id}..: {exc}")
                return []
        wanted = {j.id for j in batch}
        return [v for v in out.verdicts if v.job_id in wanted]

    batches = [jobs[i : i + batch_size] for i in range(0, len(jobs), batch_size)]
    results = await asyncio.gather(*(run(b) for b in batches))
    return {v.job_id: v for batch in results for v in batch}, errors


def apply_verdicts(report: MatchReport, verdicts: dict[str, JobVerdict], threshold: float) -> None:
    """Re-bucket a deterministic report using AI verdicts (in place).

    matches          = verdict.match and fit_score >= threshold, best fit first
    below_threshold  = everything else that was scored (screened-out or not screened)
    """
    pool = [*report.matches, *report.below_threshold]
    for r in pool:
        r.verdict = verdicts.get(r.job.id)
        r.passed = bool(r.verdict and r.verdict.match and r.verdict.fit_score >= threshold)
    report.matches = sorted((r for r in pool if r.passed), key=lambda r: r.rank_key(), reverse=True)
    rest = [r for r in pool if not r.passed]
    report.below_threshold = sorted(
        rest, key=lambda r: (r.verdict is not None, r.rank_key()), reverse=True
    )
    report.screened = True
