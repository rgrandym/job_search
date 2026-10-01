"""Pydantic v2 models for job postings, candidate profiles and match results.

`.agent/skills/job_search/job_schema.json` is generated from `JobPosting.model_json_schema()`.
Regenerate with: `python -m src.jobs.fetcher export-schema`.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.cv.models import WorkArrangement
from src.tools.search_tools import jaccard, mentions, parse_salary, title_core


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchQuery(BaseModel):
    """Search filters (the UI form). Sources use them for recall; the matcher for precision."""

    titles: list[str] = Field(default_factory=list, description="Target job titles")
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    distance_miles: int | None = Field(25, ge=0, le=500)
    salary_min: int | None = Field(None, ge=0, description="Annual, in posting currency")
    salary_max: int | None = Field(None, ge=0)
    work_arrangements: list[WorkArrangement] = Field(
        default_factory=list, description="Empty = any"
    )
    sources: list[str] = Field(default_factory=list, description="Empty = all configured")
    limit: int = Field(200, ge=1, description="Max postings per source")

    @property
    def remote_only(self) -> bool:
        return self.work_arrangements == ["remote"]

    def search_terms(self) -> list[str]:
        """One query string per title (plus keywords), for sources with keyword search."""
        extra = " ".join(self.keywords)
        return [f"{t} {extra}".strip() for t in self.titles] or ([extra] if extra else [""])

    def is_relevant(self, job: JobPosting) -> bool:
        """Loose recall filter for sources without server-side search."""
        if not self.titles and not self.keywords:
            return True
        job_core = title_core(job.title)
        for t in self.titles:
            core = title_core(t)
            if core and (core <= job_core or jaccard(core, job_core) >= 0.5):
                return True
        text = job.to_text()
        return any(mentions(text, k) for k in self.keywords)


class JobPosting(_Strict):
    """A normalised job posting from any source."""

    id: str
    title: str
    company: str
    location: str | None = Field(None, description="City, Country, or region for remote roles")
    work_arrangement: WorkArrangement = "onsite"
    description: str = ""
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    required_certifications: list[str] = Field(default_factory=list)
    min_years_experience: float | None = Field(None, ge=0)
    seniority: str | None = Field(
        None, description="intern … executive; inferred from title if null"
    )
    salary_range: str | None = None
    salary_min: float | None = Field(None, ge=0, description="Annual; parsed from salary_range")
    salary_max: float | None = Field(None, ge=0)
    within_search_area: bool = Field(
        False, description="Source applied the location/distance filter server-side"
    )
    url: str | None = None
    posted_at: date | None = None
    source: str = "manual"

    @model_validator(mode="after")
    def _parse_salary(self) -> JobPosting:
        if self.salary_range and self.salary_min is None and self.salary_max is None:
            self.salary_min, self.salary_max = parse_salary(self.salary_range)
        return self

    def to_text(self) -> str:
        """Flatten for embeddings."""
        return "\n".join(
            [
                self.title,
                self.company,
                self.description,
                " ".join(self.required_skills),
                " ".join(self.preferred_skills),
            ]
        )


class CandidateProfile(_Strict):
    """Matching-oriented view of a CV, derived by `matcher.build_profile`."""

    name: str
    titles: list[str] = Field(description="Held titles, most recent first")
    target_titles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(description="Canonical (normalised) skills")
    years_experience: float = Field(ge=0)
    seniority_level: int = Field(ge=0)
    locations: list[str] = Field(default_factory=list)
    work_arrangements: list[WorkArrangement] = Field(default_factory=list)
    willing_to_relocate: bool = False
    certifications: list[str] = Field(default_factory=list)
    salary_min: float | None = Field(None, description="Excludes jobs paying less (if known)")
    cv_based: bool = Field(True, description="False = built from search filters only, no CV")
    text: str = Field("", description="Flattened CV text used for embeddings")


class ScoreBreakdown(_Strict):
    """Per-metric sub-scores (0-1) and the weighted total (0-100)."""

    title: float = Field(ge=0, le=1)
    skills: float = Field(ge=0, le=1)
    experience: float = Field(ge=0, le=1)
    location: float = Field(ge=0, le=1)
    semantic: float = Field(ge=0, le=1)
    total: float = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list)
    missing_required_skills: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metrics_used: list[str] = Field(
        default_factory=lambda: ["title", "skills", "experience", "location", "semantic"],
        description="Metrics that contributed to `total` (no-CV searches drop skills/experience)",
    )


class SkillEvidence(_Strict):
    skill: str
    level: Literal["expert", "proficient", "familiar"]
    evidence: str = Field(description="Where in the CV this is demonstrated")


class ProfileSummary(_Strict):
    """The orchestrator's evidence-based reading of a candidate, used to screen jobs.

    Built once per (CV, role family) and reused from `ProfileMemory`.
    """

    headline: str = Field(description="One line: who this candidate is professionally")
    seniority: str
    years_experience: float = Field(ge=0)
    core_expertise: list[str] = Field(description="What they are genuinely strong at")
    key_skills: list[SkillEvidence]
    domains: list[str] = Field(default_factory=list, description="Industries / problem domains")
    target_roles: list[str] = Field(description="Roles they fit now, incl. adjacent titles")
    stretch_roles: list[str] = Field(default_factory=list)
    not_a_fit: list[str] = Field(description="Role types to reject, with a short reason each")
    search_keywords: list[str] = Field(default_factory=list)
    summary: str = Field(description="3-5 sentence narrative")


class JobVerdict(_Strict):
    """The job_matcher subagent's judgement of one posting against a ProfileSummary."""

    job_id: str
    match: bool = Field(description="True only if this is a genuine fit worth applying to")
    fit_score: int = Field(ge=0, le=100)
    verdict: Literal["strong", "good", "stretch", "poor"]
    reasons: list[str] = Field(default_factory=list, description="Why it fits (evidence)")
    gaps: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)


class MatchResult(_Strict):
    job: JobPosting
    excluded: bool = False
    exclusion_reasons: list[str] = Field(default_factory=list)
    similarity: float | None = None
    score: ScoreBreakdown | None = None
    verdict: JobVerdict | None = None
    passed: bool = False

    def rank_key(self) -> float:
        """AI fit score when screened, else the deterministic total."""
        if self.verdict is not None:
            return float(self.verdict.fit_score)
        return self.score.total if self.score else 0.0


class MatchReport(_Strict):
    """Full outcome of one search run: ranked matches plus everything filtered out, and why."""

    profile: CandidateProfile
    threshold: float
    matches: list[MatchResult] = Field(description="Passed threshold, best first")
    below_threshold: list[MatchResult] = Field(default_factory=list)
    excluded: list[MatchResult] = Field(default_factory=list)
    not_retrieved: list[str] = Field(default_factory=list, description="Job ids cut at retrieval")
    screened: bool = Field(False, description="True if the job_matcher subagent judged matches")
    summary: ProfileSummary | None = None

    def all_results(self) -> list[MatchResult]:
        return [*self.matches, *self.below_threshold, *self.excluded]
