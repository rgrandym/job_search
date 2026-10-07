"""Pydantic v2 models for job postings, candidate profiles and match results.

`.agent/skills/job_search/job_schema.json` is generated from `JobPosting.model_json_schema()`.
Regenerate with: `python -m src.jobs.fetcher export-schema`.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.cv.models import WorkArrangement
from src.tools.search_tools import jaccard, mentions, parse_salary, shares_role_words, title_core


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


MIN_PER_SEARCH = 20  # results per board search when the limit is shared across terms


class SearchQuery(BaseModel):
    """Search filters (the UI form). Sources use them for recall; the matcher for precision."""

    titles: list[str] = Field(default_factory=list, description="Target job titles")
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list, description="Cities or areas")
    country: str | None = Field(
        None, description="Country to search, e.g. 'United Kingdom'; cities are within it"
    )
    distance_miles: int | None = Field(25, ge=0, le=500)
    salary_min: int | None = Field(None, ge=0, description="Annual, in posting currency")
    salary_max: int | None = Field(None, ge=0)
    seniority_min: int | None = Field(None, ge=0, le=8, description="Optional minimum job level")
    seniority_max: int | None = Field(None, ge=0, le=8, description="Optional maximum job level")
    work_arrangements: list[WorkArrangement] = Field(
        default_factory=list, description="Empty = any"
    )
    posted_within_days: int | None = Field(
        None, ge=1, le=365, description="Only postings from the last N days (1 = 24 h); None = any"
    )
    sources: list[str] = Field(default_factory=list, description="Empty = all configured")
    exclude_boards: list[str] = Field(
        default_factory=list, description="Company job boards (`CompanyBoard.key`) not to read"
    )
    limit: int = Field(200, ge=1, description="Max postings per source")

    @model_validator(mode="after")
    def valid_seniority_range(self) -> SearchQuery:
        """An explicit level range must have its lower bound first."""
        if (
            self.seniority_min is not None
            and self.seniority_max is not None
            and self.seniority_min > self.seniority_max
        ):
            raise ValueError("Minimum seniority cannot exceed maximum seniority")
        return self

    @property
    def remote_only(self) -> bool:
        return self.work_arrangements == ["remote"]

    def posted_since(self, today: date | None = None) -> date | None:
        """Oldest accepted posting date, or None when any date is accepted."""
        if self.posted_within_days is None:
            return None
        return (today or date.today()) - timedelta(days=self.posted_within_days)

    def is_recent(self, job: JobPosting, today: date | None = None) -> bool:
        """Posted within the window. Undated postings (alert emails, saved pages) are kept:
        dropping them would hide jobs the user subscribed to."""
        since = self.posted_since(today)
        return since is None or job.posted_at is None or job.posted_at >= since

    def place_names(self) -> list[str]:
        """Locations for matching: each city qualified by the country, or the country alone."""
        if not self.country:
            return list(self.locations)
        return [f"{city}, {self.country}" for city in self.locations] or [self.country]

    def search_budget(self) -> tuple[int, int]:
        """(results per term-and-location search, total cap per source). Each search gets a
        fair share of `limit` (at least MIN_PER_SEARCH), so the first term cannot use it up."""
        searches = len(self.search_terms()) * max(1, len(self.locations))
        per_search = max(MIN_PER_SEARCH, math.ceil(self.limit / searches))
        return per_search, max(self.limit, per_search * searches)

    def search_terms(self) -> list[str]:
        """One query string per title (plus keywords), for sources with keyword search."""
        extra = " ".join(self.keywords)
        return [f"{t} {extra}".strip() for t in self.titles] or ([extra] if extra else [""])

    def is_loosely_relevant(self, job: JobPosting) -> bool:
        """Wider recall filter for large employer feeds: the title shares a role-specific word
        with a target title (scientist, cell, ipsc, ...), or the posting mentions a keyword."""
        if not self.titles and not self.keywords:
            return True
        if self.titles and shares_role_words(job.title, self.titles):
            return True
        return self.is_relevant(job)

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
    salary_maybe_estimated: bool = Field(
        False, description="The board may have estimated salary_range; never filter on it"
    )
    within_search_area: bool = Field(
        False, description="Source applied the location/distance filter server-side"
    )
    url: str | None = None
    posted_at: date | None = None
    closes_at: date | None = Field(
        None, description="Last day to apply, when the source states one (e.g. validThrough)"
    )
    source: str = "manual"

    @model_validator(mode="after")
    def _parse_salary(self) -> JobPosting:
        if self.salary_maybe_estimated:
            self.salary_min = self.salary_max = None
        elif self.salary_range and self.salary_min is None and self.salary_max is None:
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


LanguageLevel = Literal["basic", "conversational", "professional", "native"]
LANGUAGE_LEVELS: dict[str, int] = {"basic": 1, "conversational": 2, "professional": 3, "native": 4}


class LanguageSkill(_Strict):
    """A language the candidate works in, at a self-assessed level."""

    language: str = Field(min_length=1)
    level: LanguageLevel = "professional"


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
    seniority_min: int | None = Field(None, ge=0, le=8)
    seniority_max: int | None = Field(None, ge=0, le=8)
    cv_based: bool = Field(True, description="False = built from search filters only, no CV")
    text: str = Field("", description="Flattened CV text used for embeddings")
    languages: list[LanguageSkill] = Field(
        default_factory=list, description="Confirmed working languages (empty = not declared)"
    )
    eligibility: list[str] = Field(
        default_factory=list, description="Confirmed eligibility, e.g. 'right to work in the UK'"
    )
    pivot_titles: list[str] = Field(
        default_factory=list,
        description="Titles of areas the user chose to move into: no seniority exclusion there",
    )


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


class SearchIntent(_Strict):
    """What the candidate wants from the next role: their own words, kept apart from the CV's
    facts. Edited by the user (or by the assistant on the user's explicit word)."""

    direction: str = Field("", description="Where they want their career to go, in their words")
    energising_work: list[str] = Field(default_factory=list, description="Work they want more of")
    avoid_work: list[str] = Field(default_factory=list, description="Work they want less of")
    target_areas: list[str] = Field(
        default_factory=list,
        description="Areas to explore beyond the current function, e.g. 'business development'",
    )
    preferred_sectors: list[str] = Field(default_factory=list)
    avoided_sectors: list[str] = Field(default_factory=list)
    organisation_types: list[str] = Field(
        default_factory=list, description="e.g. 'start-up', 'scale-up', 'large pharma', 'VC fund'"
    )
    soft_dealbreakers: list[str] = Field(
        default_factory=list, description="Strong dislikes that lower priority, never exclude"
    )
    languages: list[LanguageSkill] = Field(
        default_factory=list, description="Confirmed working languages; overrides the CV's list"
    )
    eligibility: list[str] = Field(
        default_factory=list,
        description="Confirmed: right to work, security clearance, driving licence, registrations",
    )
    updated_at: str | None = None

    def is_empty(self) -> bool:
        """True when the user has stated nothing that steers matching."""
        return not any(v for k, v in self.model_dump().items() if k != "updated_at")


FamilyTier = Literal["core", "progression", "adjacent"]


class RoleFamily(_Strict):
    """A group of equivalent roles to search, with the CV evidence that makes it realistic."""

    name: str = Field(description="Short name, e.g. 'Business development (biotech)'")
    tier: FamilyTier = Field(
        "core",
        description="core: does this now · progression: the next level up · adjacent: a "
        "different function the experience credibly carries into",
    )
    titles: list[str] = Field(
        default_factory=list, description="2-4 titles as employers advertise them, best first"
    )
    domain_terms: list[str] = Field(
        default_factory=list, description="1-3 short terms that find these roles on job boards"
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Ids from the CV ([bullet-id], role or project ids) proving the work transfers",
    )
    gap: str = Field(
        "", description="The main thing missing for this family (required if not core)"
    )
    rationale: str = Field("", description="One line: why an employer would consider the candidate")
    requested: bool = Field(False, description="The user asked to explore this area")
    rejected: str | None = Field(None, description="Set by code: why it is not searched")


class SkillEvidence(_Strict):
    skill: str
    level: Literal["expert", "proficient", "familiar"]
    evidence: str = Field(description="Where in the CV this is demonstrated")


class PublicationRecord(_Strict):
    """What the candidate's publications (and patents, grants, talks, awards) show employers."""

    count: int = Field(0, ge=0, description="Peer-reviewed publications in the document")
    lead_author: int = Field(0, ge=0, description="Of those, first, last or corresponding author")
    years: str = Field("", description="Span of the record, e.g. '2008-2025'")
    themes: list[str] = Field(
        default_factory=list, description="3-6 research themes, in the words employers use"
    )
    notable: list[str] = Field(
        default_factory=list,
        description="2-4 outputs most relevant to the target roles: short title, journal, year",
    )
    other_outputs: list[str] = Field(
        default_factory=list,
        description="Patents, grants, invited talks, reviewing or editorial roles, awards, counted",
    )
    signals: list[str] = Field(
        default_factory=list,
        description="What the record evidences for employers (recognised expertise in X, "
        "scientific writing, KOL network, industry collaborations), each tied to the record",
    )
    summary: str = Field("", description="2-3 sentences: the record and the roles it strengthens")


class ProfileSummary(_Strict):
    """The quality model's evidence-based reading of a candidate, used to screen jobs.

    Built once per (CV, role family) and reused from `ProfileMemory` until the user updates it.
    """

    headline: str = Field(description="One line: who this candidate is professionally")
    seniority: str
    years_experience: float = Field(ge=0)
    core_expertise: list[str] = Field(description="What they are genuinely strong at")
    key_skills: list[SkillEvidence]
    domains: list[str] = Field(default_factory=list, description="Industries / problem domains")
    leadership: list[str] = Field(
        default_factory=list, description="Line, matrix and external leadership, with scope"
    )
    qualifications: list[str] = Field(
        default_factory=list, description="Degrees, PhD, registrations, certifications"
    )
    achievements: list[str] = Field(
        default_factory=list, description="The 3-5 strongest quantified achievements"
    )
    transferable_strengths: list[str] = Field(
        default_factory=list,
        description="Experience that carries into adjacent roles but is not direct experience",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Every distinct capability the document evidences (technical, delivery, "
        "leadership, external and commercial, communication), each with brief evidence",
    )
    publications: PublicationRecord | None = Field(
        None, description="The publication record, summarised; null when the CV has none"
    )
    target_roles: list[str] = Field(description="Roles they fit now, incl. adjacent titles")
    stretch_roles: list[str] = Field(default_factory=list)
    not_a_fit: list[str] = Field(description="Role types to reject, with a short reason each")
    search_keywords: list[str] = Field(default_factory=list)
    summary: str = Field(description="4-6 sentence narrative")
    role_families: list[RoleFamily] = Field(
        default_factory=list,
        description="The roles to search, grouped and tiered, each with CV evidence",
    )


class FitDimensions(_Strict):
    """Points awarded on each weighted dimension of fit (they sum to the 0-100 fit score)."""

    function: int = Field(
        ge=0,
        le=30,
        description="Core function (max 30): day-to-day work vs demonstrated experience",
    )
    domain: int = Field(
        ge=0, le=20, description="Technical/scientific domain (max 20): required vs actual science"
    )
    seniority: int = Field(
        ge=0, le=20, description="Seniority and scope (max 20): level, team, budget, decisions"
    )
    leadership: int = Field(
        ge=0, le=10, description="Leadership mode (max 10): line, matrix, external as required"
    )
    sector: int = Field(
        ge=0, le=10, description="Sector and organisation type (max 10), e.g. pharma, biotech, CRO"
    )
    practicality: int = Field(
        ge=0, le=10, description="Location and working pattern (max 10) from the candidate's base"
    )


FIT_WEIGHTS: dict[str, int] = {
    "function": 30,
    "domain": 20,
    "seniority": 20,
    "leadership": 10,
    "sector": 10,
    "practicality": 10,
}


# Rank points a match loses when it goes against the career intent (its fit score is kept).
ALIGNMENT_PENALTY = 10

RATING_LEVELS = 4  # each dimension is rated 0..4; code converts levels to points


class FitRatings(_Strict):
    """The job_matcher's rating of each dimension on a fixed 0-4 scale (anchors in the prompt).
    Choosing a described level is far more repeatable than choosing free points."""

    function: int = Field(ge=0, le=4, description="0 different discipline .. 4 does this now")
    domain: int = Field(ge=0, le=4, description="0 unrelated science .. 4 same domain")
    seniority: int = Field(ge=0, le=4, description="0 far off .. 4 same level and scope")
    leadership: int = Field(ge=0, le=4, description="0 role needs a kind never done .. 4 same")
    sector: int = Field(ge=0, le=4, description="0 unrelated sector .. 4 same sector")
    practicality: int = Field(ge=0, le=4, description="0 unworkable .. 4 easy from base")


def rating_points(ratings: FitRatings) -> FitDimensions:
    """Points per dimension: level / 4 of its weight, rounded half up (30: 0, 8, 15, 23, 30)."""
    levels = ratings.model_dump()
    return FitDimensions(
        **{
            n: (levels[n] * w * 2 + RATING_LEVELS) // (2 * RATING_LEVELS)
            for n, w in FIT_WEIGHTS.items()
        }
    )


Alignment = Literal["against", "neutral", "aligned"]


class _AssessmentText(_Strict):
    """The written part of a judgement, shared by the matcher's answer and the final verdict."""

    job_id: str
    fit_summary: str = Field(
        description="One or two sentences: how and how well this role matches the candidate"
    )
    reasons: list[str] = Field(
        default_factory=list, description="Why it fits: direct CV evidence against the posting"
    )
    transferable: list[str] = Field(
        default_factory=list,
        description="Where related (not direct) experience reasonably carries; say what transfers",
    )
    gaps: list[str] = Field(default_factory=list, description="Genuine missing requirements")
    essential_unmet: list[str] = Field(
        default_factory=list, description="Requirements the posting calls essential that are unmet"
    )
    unknowns: list[str] = Field(
        default_factory=list,
        description="What the posting does not say that matters (e.g. onsite days, salary, level)",
    )
    dealbreakers: list[str] = Field(
        default_factory=list, description="Hard blockers: a different discipline, a not_a_fit role"
    )
    alignment: Alignment = Field(
        "neutral",
        description="Fit with the career intent (not ability): against · neutral · aligned",
    )
    alignment_note: str = Field(
        "", description="Short clause naming the intent the role meets or goes against"
    )


class JobAssessment(_AssessmentText):
    """What the job_matcher proposes for one posting: evidence first, then the ratings it
    supports. Code turns it into a `JobVerdict`."""

    ratings: FitRatings


FitBand = Literal["exceptional", "very_strong", "strong", "stretch", "weak"]
Priority = Literal["apply_now", "worth_applying", "consider", "low"]


class JobVerdict(_AssessmentText):
    """The final judgement of one posting: the assessment plus the code-computed outcome."""

    ratings: FitRatings | None = Field(None, description="Levels 0-4 (None: older searches)")
    dimensions: FitDimensions = Field(description="Points from the ratings, per dimension")
    reviewed: bool = Field(
        False, description="A match or near the threshold: a second assessment was averaged in"
    )
    from_memory: bool = Field(
        False, description="Judged in an earlier search (same posting, profile and model)"
    )
    fit_score: int = Field(ge=0, le=100, description="Sum of dimensions, capped (see cap_reason)")
    band: FitBand
    priority: Priority
    match: bool = Field(description="fit_score >= threshold and no dealbreakers")
    borderline: bool = Field(
        False, description="Within BORDERLINE_MARGIN of the threshold: models may disagree"
    )
    cap_reason: str | None = Field(None, description="Why the score was capped, if it was")
    requirements_checked: bool = Field(
        True, description="False: too little posting text to check its requirements"
    )


JobStatus = Literal["new", "open", "applied", "na"]
OutcomeStage = Literal[
    "screening", "interview", "final_round", "offer", "accepted", "rejected", "no_response",
    "withdrawn",
]  # fmt: skip


class JobTracking(_Strict):
    """Where the user stands with a job across searches (kept by `services.tracker`)."""

    status: JobStatus = Field(
        description="new: first seen in this search · open: seen before, not acted on · "
        "applied · na: ruled out by the user"
    )
    note: str = Field("", description="The user's own note (never rewritten by the app)")
    first_seen: str = Field(description="ISO date the job first appeared in a search")
    applied_at: str | None = Field(None, description="ISO date of the application, if any")
    cv_file: str | None = Field(None, description="Tailored CV made for this job")
    related: str | None = Field(
        None, description="An application to another role at the same employer"
    )
    reason: str = Field("", description="The user's reason it was (or was not) suitable")
    stage: OutcomeStage | None = Field(None, description="Latest outcome of the application")


class MatchResult(_Strict):
    job: JobPosting
    excluded: bool = False
    exclusion_reasons: list[str] = Field(default_factory=list)
    similarity: float | None = None
    score: ScoreBreakdown | None = None
    verdict: JobVerdict | None = None
    passed: bool = False
    tracking: JobTracking | None = None
    family: str | None = Field(None, description="Role family whose search terms found it")
    flags: list[str] = Field(
        default_factory=list,
        description="Eligibility points to check (language level, right to work, clearance)",
    )

    def rank_key(self) -> float:
        """AI fit score when screened (lowered when it goes against the career intent), else
        the deterministic total."""
        if self.verdict is not None:
            against = self.verdict.alignment == "against"
            return float(self.verdict.fit_score) - (ALIGNMENT_PENALTY if against else 0)
        return self.score.total if self.score else 0.0


class SavedJob(_Strict):
    """A job the user saved from a search: the result as it was judged, kept across searches."""

    result: MatchResult
    saved_at: str = Field(description="ISO date-time it was saved")


class MatchReport(_Strict):
    """Full outcome of one search run: ranked matches plus everything filtered out, and why."""

    profile: CandidateProfile
    threshold: float
    matches: list[MatchResult] = Field(description="Passed threshold, best first")
    to_check: list[MatchResult] = Field(
        default_factory=list,
        description="Would match on what could be read, but the posting's requirements were not "
        "checked yet (no full text): fetch or paste it",
    )
    below_threshold: list[MatchResult] = Field(default_factory=list)
    excluded: list[MatchResult] = Field(default_factory=list)
    applied: list[MatchResult] = Field(
        default_factory=list, description="Already applied for: set aside before screening"
    )
    dismissed: list[MatchResult] = Field(
        default_factory=list,
        description="Marked N/A or labelled no by the user: set aside before screening",
    )
    not_retrieved: list[str] = Field(default_factory=list, description="Job ids cut at retrieval")
    screened: bool = Field(False, description="True if the job_matcher judged matches")
    summary: ProfileSummary | None = None

    def scored(self) -> list[MatchResult]:
        """Every posting that went to scoring: matches, to check, and not selected."""
        return [*self.matches, *self.to_check, *self.below_threshold]

    def all_results(self) -> list[MatchResult]:
        return [
            *self.matches,
            *self.to_check,
            *self.below_threshold,
            *self.excluded,
            *self.applied,
            *self.dismissed,
        ]
