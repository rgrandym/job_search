"""Pydantic v2 models for the Master CV and tailored CVs.

These models are the source of truth. `.agent/skills/cv_writer/master_cv_schema.json`
is generated from `MasterCV.model_json_schema()`, and a test fails if the two drift.
Regenerate with: `python -m src.cv.master_cv_manager export-schema`.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_YEAR_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

WorkArrangement = Literal["remote", "hybrid", "onsite"]
ALL_ARRANGEMENTS: list[WorkArrangement] = ["remote", "hybrid", "onsite"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Link(_Strict):
    label: str
    url: str


class Basics(_Strict):
    name: str = Field(min_length=1)
    headline: str | None = Field(None, description="Current or target professional title")
    email: str | None = None
    phone: str | None = None
    location: str | None = Field(None, description="City, Country")
    links: list[Link] = Field(default_factory=list)
    summary: str | None = None


class Bullet(_Strict):
    """One accomplishment. `id` is stable and used for provenance when tailoring."""

    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    text: str = Field(min_length=1)
    skills: list[str] = Field(default_factory=list, description="Skills evidenced by this bullet")
    metrics: list[str] = Field(
        default_factory=list, description="Verifiable quantities, e.g. '40%', '$2M', '12 engineers'"
    )


class Experience(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    company: str
    title: str
    location: str | None = None
    start: str = Field(description="YYYY-MM")
    end: str | None = Field(None, description="YYYY-MM, or null if current")
    bullets: list[Bullet] = Field(default_factory=list)

    @field_validator("start", "end")
    @classmethod
    def _check_year_month(cls, v: str | None) -> str | None:
        if v is not None and not _YEAR_MONTH.match(v):
            raise ValueError(f"Expected YYYY-MM, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_order(self) -> Experience:
        if self.end is not None and self.end < self.start:
            raise ValueError(f"{self.id}: end {self.end} precedes start {self.start}")
        return self

    def months(self, today: date | None = None) -> int:
        """Inclusive duration in months."""
        today = today or date.today()
        sy, sm = map(int, self.start.split("-"))
        ey, em = map(int, self.end.split("-")) if self.end else (today.year, today.month)
        return max(0, (ey - sy) * 12 + (em - sm) + 1)


class Education(_Strict):
    institution: str
    degree: str
    field: str | None = None
    start: str | None = None
    end: str | None = None
    details: list[str] = Field(default_factory=list)


class Certification(_Strict):
    name: str
    issuer: str | None = None
    year: int | None = None


class Project(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    name: str
    description: str
    skills: list[str] = Field(default_factory=list)
    url: str | None = None


class SkillGroup(_Strict):
    category: str
    items: list[str] = Field(min_length=1)


class Preferences(_Strict):
    """Job-search preferences. Used by the matcher, never printed on the CV."""

    target_titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_arrangements: list[WorkArrangement] = Field(default_factory=lambda: list(ALL_ARRANGEMENTS))
    willing_to_relocate: bool = False
    min_salary: int | None = Field(None, ge=0, description="Minimum acceptable annual salary")


class MasterCV(_Strict):
    """The complete, factual career record. All tailored CVs derive from it."""

    schema_version: str = "1.0"
    basics: Basics
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)

    @model_validator(mode="after")
    def _unique_ids(self) -> MasterCV:
        ids = [e.id for e in self.experience] + [p.id for p in self.projects]
        ids += [b.id for e in self.experience for b in e.bullets]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Duplicate ids: {sorted(dupes)}")
        return self

    def all_skills(self) -> set[str]:
        """Every skill named anywhere in the CV (as written)."""
        out = {s for g in self.skills for s in g.items}
        out |= {s for e in self.experience for b in e.bullets for s in b.skills}
        out |= {s for p in self.projects for s in p.skills}
        return out

    def bullet_index(self) -> dict[str, Bullet]:
        """Map bullet id -> bullet."""
        return {b.id: b for e in self.experience for b in e.bullets}


# ---------------------------------------------------------------- tailoring


class JDAnalysis(_Strict):
    """Structured extraction of a job description (LLM output)."""

    job_title: str
    company: str | None = None
    seniority: str | None = None
    hard_skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    level_signals: list[str] = Field(
        default_factory=list,
        description="What the posting says about the level: autonomy, decision authority, people "
        "led, budget, stakeholders, years asked",
    )
    academic: bool = Field(
        False,
        description="An academic post (university, research institute, fellowship, lectureship, "
        "postdoctoral or research staff position)",
    )


class RewrittenBullet(_Strict):
    """A bullet rewritten for a JD. `source_id` must reference a Master CV bullet."""

    source_id: str
    text: str
    keywords_used: list[str] = Field(default_factory=list)


class RoleOrder(_Strict):
    """One role's bullet ids, most relevant first. A list, not a dict: strict structured
    output schemas (Codex, OpenAI) cannot carry free-form dict keys."""

    experience_id: str
    bullet_ids: list[str] = Field(default_factory=list)


MatchClass = Literal["direct", "transferable", "adjacent", "needs_confirmation", "not_evidenced"]
CLAIMABLE: tuple[MatchClass, ...] = ("direct", "transferable", "adjacent")


class RequirementEvidence(_Strict):
    """One job requirement and the CV evidence for it (checked by `tailor.check_requirements`).
    Only direct, transferable and adjacent rows may be presented as experience."""

    requirement: str = Field(description="The requirement in the job's own words")
    priority: Literal["essential", "desirable"] = "essential"
    importance: Literal["key", "supporting"] = Field(
        "supporting", description="Key = pivotal to doing this role; supporting = useful context"
    )
    match: MatchClass = Field(
        description="direct: the CV shows this exact skill or work · transferable: equivalent "
        "work in another context · adjacent: related but not equivalent work · "
        "needs_confirmation: plausible but not in the CV · not_evidenced: a gap"
    )
    evidence: list[str] = Field(
        default_factory=list, description="CV bullet, role or project ids that show it"
    )
    note: str = Field("", description="How the evidence transfers, or what is missing")


class Positioning(_Strict):
    """How the CV presents the candidate for this job, decided before any wording."""

    thesis: str = Field(description="What the candidate can deliver in this role, at what level")
    differentiators: list[str] = Field(
        default_factory=list, description="2-4 evidenced strengths that set them apart"
    )
    transfer: str = Field(
        "", description="For a role outside their current function: what carries over"
    )
    level: str = Field("", description="Their evidenced level against the job's level")


class SkillToConfirm(_Strict):
    """A job technique plausibly familiar from the background, not evidenced by the CV."""

    skill: str = Field(description="Routine technique or tool relevant to the job")
    background_quote: str = Field(description="Exact CV words making this worth asking about")
    job_quote: str = Field("", description="Exact posting words this technique would serve")
    reason: str = Field(description="Why the background makes it worth asking the user")


class SkillSuggestions(_Strict):
    """A focused follow-up when the main tailoring plan omitted review suggestions."""

    items: list[SkillToConfirm] = Field(default_factory=list)


AdditionKind = Literal[
    "skill", "experience", "enrichment", "rewording", "metric", "summary", "headline"
]


class AdditionCandidate(_Strict):
    """A possible omitted fact proposed by the writer, never a confirmed CV claim. Kinds:
    skill (into a skills category), experience (a new bullet after `anchor_id` in its role),
    enrichment (words added inside the existing bullet `anchor_id`), metric (a question for a
    number the bullet `anchor_id` lacks), rewording (the bullet `anchor_id` rewritten in the
    job's terms), summary (one sentence), headline (one option)."""

    kind: AdditionKind
    text: str = Field(
        min_length=1,
        description="CV wording to review, without invented metrics. enrichment: only the "
        "words to add to the bullet; metric: the question, e.g. 'how many samples?'",
    )
    role_id: str | None = Field(None, description="Existing role for an experience proposal")
    anchor_id: str | None = Field(
        None, description="experience, enrichment, metric: the existing bullet it belongs to"
    )
    skill_group: str | None = Field(
        None, description="skill: the CV's skills category it belongs in (as the CV names it)"
    )
    requirement: str = Field(description="The pivotal job need this would address")
    background_quote: str = Field(description="Exact words in the source CV")
    job_quote: str = Field(description="Exact words in the posting")
    reason: str = Field(description="Why this is plausible and what the user must verify")
    basis: str = Field(
        "",
        description="What makes it typical: what people with this title, at this kind of "
        "employer and in this sector routinely do, or the role's documented work",
    )
    likelihood: Literal["likely", "possible"] = Field(
        "likely",
        description="likely: such a role routinely involves it; possible: plausible from the "
        "background but less certain. Never propose what such a role would be unlikely to do",
    )


class TermBridge(_Strict):
    """The job's own word for something a CV bullet already shows in other words (LLM output;
    `tailor.verified_bridges` checks it), so ATS search finds the evidence the CV has."""

    term: str = Field(description="One of the job's keywords, exactly as the job names it")
    source_id: str = Field(description="The CV bullet that shows it")
    quote: str = Field(description="The exact words of that bullet that show the same thing")
    requirement: str = Field(description="The requirement row (as in `requirements`) it serves")


class AdditionCandidates(_Strict):
    items: list[AdditionCandidate] = Field(default_factory=list)


class ReviewAddition(AdditionCandidate):
    """A candidate sentence kept separate from factual CV text until the user confirms it."""

    id: str
    status: Literal["pending", "confirmed", "deleted"] = "pending"
    original_text: str = Field(
        "",
        description="enrichment, metric: the bullet as it was (code-set; restored for "
        "comparisons against the original CV)",
    )


class CompressionCandidate(_Strict):
    """A shorter version of lines that overlap or carry detail this job does not need (LLM
    output, optimised mode). Shown in red below the originals; only the user applies it."""

    # merge_roles: only in drafts made before 2026-10-15; different roles are never combined
    kind: Literal[
        "tighten", "merge_bullets", "merge_roles", "condense_role", "shorter_role", "trim_skills"
    ]
    role_ids: list[str] = Field(
        description="The one role whose lines it shortens (trim_skills: empty)"
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description="tighten: one bullet; merge_bullets: two or more bullets of the role; "
        "shorter_role and merge_roles: leave empty (all of the role's bullets); condense_role "
        "(built by code, never proposed by the model): the lines it leaves out",
    )
    bullets: list[str] = Field(
        description="The compressed wording: one bullet for tighten or merge_bullets; for "
        "shorter_role the role's whole entry synthesised in fewer bullets, most relevant first; "
        "for trim_skills the skills the line keeps (none: the line goes)"
    )
    removed: list[str] = Field(
        default_factory=list, description="Details left out because this job does not need them"
    )
    reason: str = Field(description="Why this reads better for this job")
    category: str = Field("", description="trim_skills only (code-set): the skills line's category")
    line_sources: list[list[str]] = Field(
        default_factory=list,
        description="shorter_role only (code-set): for each bullet, the role's line ids it "
        "draws on",
    )


class CompressionProposal(CompressionCandidate):
    """A checked compression: pending the user's decision, or (optimised mode's shorter
    roles and skills lines) applied by default with what it replaced kept for a restore."""

    id: str
    heading: str = Field("", description="merge_roles: the merged entry's heading (code-built)")
    status: Literal["pending", "applied", "rejected"] = "pending"
    line_ids: list[str] = Field(
        default_factory=list, description="Applied shorter_role: the draft ids of its lines"
    )
    before: list[str] = Field(
        default_factory=list,
        description="Applied: the replaced lines' text (shorter_role) or the line's skills "
        "(trim_skills), for 'Restore my lines'",
    )
    before_ids: list[str] = Field(
        default_factory=list, description="Applied shorter_role: the replaced lines' ids"
    )


class VersionLine(_Strict):
    """One bullet of a role's shorter version."""

    text: str
    sources: list[str] = Field(
        default_factory=list, description="The ids of the role's lines this bullet draws on"
    )


class RoleVersion(_Strict):
    """One role's entry rewritten shorter for this job (LLM output, optimised mode)."""

    role_id: str
    bullets: list[VersionLine] = Field(
        description="The role's whole entry consolidated, as many bullets as the role's "
        "`lines` attribute asks, most relevant first"
    )
    removed: list[str] = Field(
        default_factory=list, description="Each detail and number left out, briefly"
    )
    reason: str = Field(description="Why this version reads better for this job (one line)")


class SkillsLine(_Strict):
    """One skills line of the CV cut to what this job needs (LLM output, optimised mode)."""

    category: str
    keep: list[str] = Field(
        description="The line's own skills this job needs, exactly as written, most relevant "
        "first; empty when none is related (the line goes)"
    )
    reason: str = Field(description="Why the others do not help for this job (one line)")


class RoleVersions(_Strict):
    """LLM output: shorter versions of the draft's long roles and skills lines
    (`cv.role_versions`)."""

    roles: list[RoleVersion] = Field(default_factory=list)
    skills: list[SkillsLine] = Field(default_factory=list)


class KeyStrength(_Strict):
    """One "Key strengths" line under the summary, resting on the CV bullets it cites."""

    text: str = Field(
        description="One or two short sentences (at most about 30 words), first person implied; "
        "evidence the summary does not already state"
    )
    source_ids: list[str] = Field(description="The CV bullet ids whose facts it states")


class TailoringPlan(_Strict):
    """LLM output: what to change. Applied deterministically by `tailor.apply_plan`."""

    requirements: list[RequirementEvidence] = Field(
        default_factory=list, description="Every meaningful job requirement, mapped to evidence"
    )
    positioning: Positioning | None = None
    headline_options: list[str] = Field(
        default_factory=list,
        description="Up to 4 alternative headlines for this job; the CV keeps its own headline "
        "and the user may choose one of these",
    )
    summary: str | None = None
    key_strengths: list[KeyStrength] = Field(
        default_factory=list,
        description="3 succinct points under the summary: the experience, contributions or "
        "roles that best match this job, each from the CV bullets it cites, none repeating "
        "the summary or another point",
    )
    rewritten_bullets: list[RewrittenBullet] = Field(default_factory=list)
    bullet_order: list[RoleOrder] = Field(
        default_factory=list,
        description="For every role: its experience id and bullet ids, most relevant first. "
        "Bullets not listed move to the end of their role; they are left out only when "
        "trimming is allowed",
    )
    skills_priority: list[str] = Field(default_factory=list)
    role_skills: list[str] = Field(
        default_factory=list,
        description="Up to 8 of the CV's own skills most relevant to this job, for one "
        "'Relevant skills for this role' line under the summary",
    )
    skills_to_confirm: list[SkillToConfirm] = Field(
        default_factory=list,
        description="At most five job techniques plausibly used but absent from the CV; "
        "shown in red for review, never treated as confirmed experience",
    )
    review_candidates: list[AdditionCandidate] = Field(
        default_factory=list,
        description="Optimised mode only: plausible missing experience or skills to ask the user "
        "to confirm, never included in the factual CV",
    )
    term_bridges: list[TermBridge] = Field(
        default_factory=list,
        description="Job keywords a CV bullet shows in other words, each with that bullet's "
        "exact words; code checks them before the term may be used",
    )
    compressions: list[CompressionCandidate] = Field(
        default_factory=list,
        description="Optimised mode only: a shorter version of each long role, overlapping "
        "bullets merged or long bullets tightened, for the user to compare and apply",
    )

    @field_validator("bullet_order", mode="before")
    @classmethod
    def _order_from_dict(cls, value: object) -> object:
        """Also accept {experience id: [bullet ids]}."""
        if isinstance(value, dict):
            return [{"experience_id": k, "bullet_ids": v} for k, v in value.items()]
        return value

    def order_by_role(self) -> dict[str, list[str]]:
        """experience id -> bullet ids, most relevant first."""
        return {o.experience_id: o.bullet_ids for o in self.bullet_order}


class TrimAssessment(_Strict):
    """Whether this tailoring may leave bullets out. Only for a clearly more junior role (decided
    in code by `tailor.assess_trim`); otherwise every bullet is kept and only reordered."""

    allowed: bool
    reason: str


class ChangeRecord(_Strict):
    source_id: str
    original: str
    tailored: str
    accepted: bool
    reason: str | None = None


class MissedRequirement(_Strict):
    """A JD requirement the Master CV evidences but the tailored CV does not show."""

    requirement: str
    source_id: str = Field(description="Master CV bullet id that evidences it")


class BulletIssue(_Strict):
    source_id: str
    issue: str = Field(description="What is weak: passive, generic, result buried, off-target")


class CVCritique(_Strict):
    """A second reader's review of a tailored CV (LLM output). It may only point at facts
    already in the Master CV; the revision still goes through `apply_plan`."""

    missed_requirements: list[MissedRequirement] = Field(default_factory=list)
    weak_bullets: list[BulletIssue] = Field(default_factory=list)
    order_notes: list[str] = Field(default_factory=list, description="Bullet or section order")
    recruiter_notes: list[str] = Field(
        default_factory=list,
        description="At most 3: what the first page says, whether differentiators show, whether "
        "the case for a role outside the current function is credible",
    )

    def has_issues(self) -> bool:
        points = [self.missed_requirements, self.weak_bullets, self.order_notes]
        return any(points) or bool(self.recruiter_notes)

    def notes(self) -> list[str]:
        """One line per point, for the user."""
        return [
            *(f"Surface {m.requirement} ({m.source_id})" for m in self.missed_requirements),
            *(f"Strengthen {b.source_id}: {b.issue}" for b in self.weak_bullets),
            *self.order_notes,
            *self.recruiter_notes,
        ]


class ATSReport(_Strict):
    """What an applicant-tracking system reads from the exported .docx."""

    words: int
    est_pages: float = Field(description="Rough page count from the word count")
    contact_missing: list[str] = Field(
        default_factory=list, description="Contact details not found as plain text"
    )
    keyword_coverage: float = Field(ge=0, le=1, description="JD keywords found in the file text")
    missing_keywords: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


FitDimension = Literal["function", "domain", "seniority", "leadership", "sector", "practicality"]


class FitReading(_Strict):
    """A recruiter's reading of one CV for one job, from the CV alone (LLM output)."""

    function: int = Field(ge=0, le=4, description="0 another discipline .. 4 does this work now")
    domain: int = Field(ge=0, le=4, description="0 unrelated field .. 4 same field")
    seniority: int = Field(ge=0, le=4, description="0 far off .. 4 same level and scope")
    leadership: int = Field(ge=0, le=4, description="0 not shown .. 4 the kind the job needs")
    sector: int = Field(ge=0, le=4, description="0 unrelated sector .. 4 same sector")
    requirements_shown: list[int] = Field(
        default_factory=list,
        description="Numbers of the listed requirements this CV clearly shows the candidate meets",
    )
    impression: str = Field("", description="One sentence: what the top of the CV conveys")
    evidence: dict[str, str] = Field(
        default_factory=dict, description="Dimension -> the CV line behind its rating (checked)"
    )
    requirement_quotes: dict[str, str] = Field(
        default_factory=dict, description="Requirement number -> the CV line that shows it"
    )


class RatedLine(_Strict):
    """One rating and the CV line behind it (LLM output)."""

    rating: int = Field(ge=0, le=4)
    quote: str = Field(
        "", description="One line copied word for word from this CV that supports it; '' for 0"
    )


class MetRequirement(_Strict):
    """A listed requirement the CV shows the candidate meets (LLM output)."""

    number: int = Field(description="The requirement's number in the list")
    quote: str = Field(description="The CV line that shows it, copied word for word")


class EvidencedReading(_Strict):
    """One CV read for the job, each rating and requirement backed by a quoted line (LLM
    output; code checks every quote against the CV before it counts)."""

    function: RatedLine = Field(description="0 another discipline .. 4 does this work now")
    domain: RatedLine = Field(description="0 unrelated field .. 4 same field")
    seniority: RatedLine = Field(description="0 far off .. 4 same level and scope")
    leadership: RatedLine = Field(description="0 not shown .. 4 the kind the job needs")
    sector: RatedLine = Field(description="0 unrelated sector .. 4 same sector")
    requirements_met: list[MetRequirement] = Field(default_factory=list)
    impression: str = Field("", description="One sentence: what the top of the CV conveys")


class EvidencedReadings(_Strict):
    """Several CVs read independently for one job, in the order given (LLM output)."""

    readings: list[EvidencedReading] = Field(description="One reading per CV, in the order given")


class DimensionChange(_Strict):
    name: FitDimension
    before: int = Field(ge=0, le=4)
    after: int = Field(ge=0, le=4)


class ProposalGain(_Strict):
    """A requirement the draft shows only with the proposals in place."""

    requirement: str
    addition_id: str | None = Field(
        None, description="The proposal whose wording the reader quoted; None: another line"
    )
    quote: str = Field("", description="The line the reader quoted for it (checked)")


class ProjectedFit(_Strict):
    """The draft read again with every pending proposal in place, beside the draft as it is,
    in the same blind pass. Hypothetical: the proposals are not confirmed facts, so this
    shows what confirming them would change and never replaces the draft's own score."""

    confirmed: int = Field(ge=0, le=100, description="The draft as it is, read in this pass")
    projected: int = Field(ge=0, le=100, description="With every pending proposal in place")
    proposals: int = Field(0, description="Pending proposals placed in the reading")
    dimensions: list[DimensionChange] = Field(
        default_factory=list, description="before = the draft as it is, after = with proposals"
    )
    requirements_total: int = 0
    requirements_confirmed: int = 0
    requirements_projected: int = 0
    gained: list[ProposalGain] = Field(default_factory=list)
    lost: list[str] = Field(
        default_factory=list, description="Requirements shown only without them (reader noise)"
    )
    no_effect: list[str] = Field(
        default_factory=list, description="Proposal ids behind no newly shown requirement"
    )
    keywords_confirmed: float = Field(0, ge=0, le=1, description="Job terms the draft names")
    keywords_projected: float = Field(0, ge=0, le=1, description="With the proposals in place")
    keywords_added: list[str] = Field(
        default_factory=list, description="Job terms only the proposals name (ATS search)"
    )
    ats_confirmed: int = Field(0, ge=0, le=100, description="ATS match of the draft as it is")
    ats_projected: int = Field(0, ge=0, le=100, description="ATS match with the proposals")
    impression: str = ""


class TermCheck(_Strict):
    """One job term the screening score looks for."""

    term: str
    weight: int = Field(1, description="How often the posting uses it (1 to 3)")
    found: bool


class ReadabilityScore(_Strict):
    """How easily a person reads a CV (`cv/readability.py`): computed by code."""

    score: int = Field(ge=0, le=100)
    grade: str
    notes: list[str] = Field(default_factory=list, description="What to fix, most points first")
    words: int = 0
    bullets: int = 0


class Readability(_Strict):
    """The original CV and the tailored one, read for ease of reading."""

    before: ReadabilityScore
    after: ReadabilityScore


class ScreeningScore(_Strict):
    """A deterministic screening (ATS) score of one CV's printed text (`cv/ats_score.py`)."""

    score: int = Field(ge=0, le=100)
    grade: str = Field(description="A (80+), B (65+), C (50+), D")
    keywords: list[TermCheck] = Field(default_factory=list)
    title: str = ""
    title_match: float = Field(0, ge=0, le=1)
    education: list[TermCheck] = Field(default_factory=list)
    soft_skills: list[TermCheck] = Field(default_factory=list)
    points: dict[str, float] = Field(default_factory=dict, description="Points per part")


class Screening(_Strict):
    """The original, the tailored CV and (optimised) the CV with every proposal confirmed,
    scored the same deterministic way."""

    before: ScreeningScore
    after: ScreeningScore
    if_confirmed: ScreeningScore | None = None


class OriginalReading(_Strict):
    """The original CV read once for a job (median of several reads) with the requirements
    it was read against: every draft of that job is compared with this same baseline."""

    text_key: str = Field(description="Hash of the original CV's text as read")
    requirements: list[str] = Field(default_factory=list)
    reading: FitReading
    reader: str = Field("", description="The grader model that read it (provider:model:effort)")


class FitComparison(_Strict):
    """How clearly the tailored CV shows the fit, against the CV it was made from. The
    candidate's actual fit is the same (same facts); this measures what a reader sees."""

    before: int = Field(ge=0, le=100, description="The original CV, read for this job")
    after: int = Field(ge=0, le=100, description="The tailored CV, read for this job")
    search_anchored: bool = False
    dimensions: list[DimensionChange] = Field(default_factory=list)
    requirements_total: int = 0
    requirements_before: int = 0
    requirements_after: int = 0
    keywords_before: float = Field(0, ge=0, le=1)
    keywords_after: float = Field(0, ge=0, le=1)
    first_page_before: float = Field(0, ge=0, le=1, description="Keywords on the first page")
    first_page_after: float = Field(0, ge=0, le=1)
    impression_before: str = ""
    impression_after: str = ""
    worse: list[str] = Field(default_factory=list, description="Where the tailored CV reads worse")
    ats_before: int = Field(0, ge=0, le=100, description="ATS match of the original")
    ats_after: int = Field(0, ge=0, le=100, description="ATS match of the tailored CV")
    requirements_missing: list[str] = Field(
        default_factory=list, description="Requirements the tailored CV was not read as showing"
    )
    if_confirmed: ProjectedFit | None = Field(
        None, description="Optimised draft: the same reading with its pending proposals in place"
    )
    baseline: OriginalReading | None = Field(
        None, description="The original's reading this comparison used (kept per job)"
    )


class TailoredCV(_Strict):
    """A tailored CV plus its full audit trail back to the Master CV."""

    cv: MasterCV
    target_title: str
    target_company: str | None = None
    keyword_coverage: float = Field(ge=0, le=1)
    matched_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(
        default_factory=list, description="JD keywords the Master CV does not evidence (gaps)"
    )
    restored_keywords: list[str] = Field(
        default_factory=list,
        description="JD keywords the tailoring dropped with a bullet; the bullet was put back",
    )
    changes: list[ChangeRecord] = Field(default_factory=list)
    critique: list[str] = Field(
        default_factory=list, description="Reviewer points the revision was asked to address"
    )
    ats: ATSReport | None = None
    source_ats_keyword_coverage: float | None = Field(default=None, ge=0, le=1)
    document_id: str | None = None
    trim: TrimAssessment | None = None
    headline_options: list[str] = Field(
        default_factory=list,
        description="Checked headline suggestions for this job; the CV keeps its own headline",
    )
    document_notes: list[str] = Field(
        default_factory=list,
        description="How the Word file was written (same design as the original CV, or why not)",
    )
    positioning: Positioning | None = None
    requirements: list[RequirementEvidence] = Field(
        default_factory=list, description="The job's requirements and the evidence, as checked"
    )
    role_skills: list[str] = Field(
        default_factory=list, description="The 'Relevant skills for this role' line (CV skills)"
    )
    key_strengths: list[str] = Field(
        default_factory=list, description="The checked 'Key strengths' lines under the summary"
    )
    skills_to_confirm: list[SkillToConfirm] = Field(
        default_factory=list,
        description="Unverified job techniques shown in red, outside the confirmed CV facts",
    )
    review_additions: list[ReviewAddition] = Field(default_factory=list)
    compressions: list[CompressionProposal] = Field(
        default_factory=list,
        description="Optimised mode: shorter versions shown in red below the originals",
    )
    term_bridges: list[TermBridge] = Field(
        default_factory=list,
        description="Job keywords used for what a CV bullet shows in its own words (checked)",
    )
    revision: str = Field("", description="What the score-driven revision did, if it ran")
    screening: Screening | None = Field(
        None, description="Deterministic screening (ATS) score before, after and if confirmed"
    )
    readability: Readability | None = Field(
        None, description="Deterministic ease-of-reading score, original and tailored"
    )
    length_notes: list[str] = Field(
        default_factory=list,
        description="Standard mode: advice on lines that could be merged or tightened; never "
        "applied by the app (the user edits them if they agree)",
    )
    academic: bool = Field(False, description="Written for an academic post")
    fit_check: FitComparison | None = None


class TailoringSettings(_Strict):
    """The choices a tailored CV was written with, so versions for one job can be told apart."""

    template: str = "original"
    emphasis: str = "auto"
    level: str = "auto"
    length: str = "auto"
    audience: str = "auto"
    mode: Literal["standard", "optimised"] = "standard"


class AreaChange(_Strict):
    """One area of a CV version against the original it was made from."""

    area: str
    before: str
    after: str
    change: Literal["better", "worse", "same"]


class VersionReading(_Strict):
    """One CV (an original or a tailored version) read for the job, beside the others."""

    id: str = Field(description='A tailored CV id, or "original:<cv id>"')
    kind: Literal["original", "tailored"]
    label: str
    baseline_id: str | None = Field(None, description="The original this version was made from")
    created_at: str | None = None
    filename: str | None = None
    settings: TailoringSettings | None = None
    score: int = Field(ge=0, le=100)
    dimensions: list[DimensionChange] = Field(
        default_factory=list, description="before = the baseline's rating, after = this CV's"
    )
    requirements_shown: list[int] = Field(default_factory=list, description="1-based numbers")
    keywords_found: list[str] = Field(default_factory=list)
    keyword_coverage: float = Field(0, ge=0, le=1)
    first_page_coverage: float = Field(0, ge=0, le=1)
    impression: str = ""
    words: int = 0
    rejected_changes: int = 0
    left_out: int = 0
    rewritten: int = Field(0, description="Lines whose wording tailoring changed")
    areas: list[AreaChange] = Field(
        default_factory=list, description="Against its original (empty for an original)"
    )
    evidence: dict[str, str] = Field(
        default_factory=dict, description="Dimension -> the checked CV line behind its rating"
    )
    requirement_quotes: dict[str, str] = Field(
        default_factory=dict, description="Requirement number -> the checked CV line showing it"
    )
    verdict: Literal["better", "worse", "mixed", "same", ""] = Field(
        "", description="Against its original, by fixed margins ('' for an original)"
    )
    verdict_reason: str = ""
    if_confirmed: ProjectedFit | None = Field(
        None, description="Optimised draft: read again with its pending proposals in place"
    )


class SettingEffect(_Strict):
    """Two versions that differ in one tailoring setting only: what that setting changed."""

    setting: str
    from_id: str
    to_id: str
    from_value: str
    to_value: str
    score_change: int
    requirements_change: int
    keyword_change: float
    dimensions: list[DimensionChange] = Field(
        default_factory=list, description="before = the first version's, after = the second's"
    )
    effect: Literal["clear", "none"]
    text: str


class VersionComparison(_Strict):
    """Every chosen CV for one job read side by side by the same blind reader."""

    job_id: str
    job_title: str = ""
    job_company: str = ""
    search_fit_score: int | None = Field(
        None, ge=0, le=100, description="Search matcher's score, a different assessment"
    )
    search_fit_note: str = Field("", description="Why scores are not anchored to a search score")
    created_at: str
    requirements: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    readings: list[VersionReading] = Field(default_factory=list)
    best_id: str | None = None
    summary: str = ""
    setting_effects: list[SettingEffect] = Field(default_factory=list)
    reads: int = Field(1, description="Independent readings per CV; the median counts")
    margin: int = Field(0, description="Score points below which two CVs read the same")
    method_version: int = Field(1, description="Comparison rubric and ranking version")


class SearchFitBaseline(_Strict):
    """The search verdict for the CV used to make this draft, saved at tailoring time."""

    score: int = Field(ge=0, le=100)
    ratings: dict[str, int] = Field(description="The matcher's six 0-4 ratings")
    cap_reason: str | None = None
    requirements_checked: bool = Field(True, description="False: capped as an unread posting")


class TailoredDocument(_Strict):
    """A saved, editable tailored CV and the source used to guard later edits."""

    id: str
    cv_id: str
    job_id: str
    job_title: str = ""
    job_company: str = ""
    job_description: str = ""
    job_location: str | None = None
    created_at: str
    updated_at: str
    template: str = Field(description='A template name, or "original": a copy of the CV\'s file')
    filename: str
    original_file: str | None = Field(
        default=None, description="The original Word CV the document was written from"
    )
    source_cv: MasterCV
    jd: JDAnalysis
    tailored: TailoredCV
    reviewed: bool = True
    imported: bool = False
    settings: TailoringSettings | None = Field(
        default=None, description="The choices it was tailored with (None: imported or older)"
    )
    search_fit: SearchFitBaseline | None = None
    search_fit_invalidated: bool = False  # older drafts only; no longer set or used
    final_file: str | None = Field(
        default=None, description="The clean final version (no red), written on request"
    )
    final_notes: list[str] = Field(default_factory=list)


class TailoredCVEdits(_Strict):
    """Text fields a user can revise without changing the Master CV."""

    headline: str | None = None
    summary: str | None = None
    bullets: dict[str, str] = Field(default_factory=dict)


class LetterParagraph(_Strict):
    """One cover-letter paragraph and the Master CV ids its claims come from."""

    text: str
    source_ids: list[str] = Field(
        default_factory=list, description="Bullet, role or project ids backing every claim"
    )


class LetterOpening(_Strict):
    """A reason for interest followed by one CV-backed reason for fit."""

    interest: str = Field(min_length=1)
    fit: LetterParagraph


class LetterSections(_Strict):
    """Required sections the model fills before the letter is assembled."""

    greeting: str = "Dear Hiring Manager,"
    opening: LetterOpening
    evidence: list[LetterParagraph] = Field(min_length=1, max_length=2)
    bridge: LetterParagraph | None = Field(
        None,
        description="The gap paragraph: required when <gaps> lists requirements or the role is "
        "outside the current function or level; the strengths that carry over, the gap named "
        "plainly, and the range of skills mastered that shows the candidate learns quickly",
    )
    conclusion: LetterParagraph
    closing: str = "Yours sincerely,"


class CoverLetterDraft(_Strict):
    """LLM output: untrusted until `cover_letter.apply_letter`."""

    greeting: str = "Dear Hiring Manager,"
    opening: LetterOpening | None = None
    evidence: list[LetterParagraph] = Field(default_factory=list)
    bridge: LetterParagraph | None = None
    conclusion: LetterParagraph | None = None
    # Older callers can still pass paragraphs directly to apply_letter.
    paragraphs: list[LetterParagraph] = Field(default_factory=list)
    closing: str = "Yours sincerely,"


class LetterReview(_Strict):
    """A hiring manager's read of a drafted letter (LLM output); never a source of facts."""

    issues: list[str] = Field(
        default_factory=list,
        description="At most 4 concrete fixes: generic or recited lines, a weak opening, the "
        "top requirements not addressed, an unconvincing case for the move",
    )


class LetterFlag(_Strict):
    """A paragraph a check questions: written, shown in red, for the user to decide."""

    paragraph: int = Field(ge=0, description="0-based index into CoverLetter.paragraphs")
    reason: str


class LetterScore(_Strict):
    """How well a letter makes the case, computed by code (`cover_letter.score_letter`)."""

    score: int = Field(ge=0, le=100)
    grade: str
    notes: list[str] = Field(default_factory=list, description="What to improve, most first")
    words: int = 0
    terms: list[str] = Field(
        default_factory=list, description="The job's most used keywords the score looks for"
    )


class CoverLetter(_Strict):
    """A checked cover letter. Every paragraph is kept; one whose claims do not trace to the
    CV is flagged (red in Word and the app) until the user edits it or keeps it as written
    (user's decision 2026-10-11)."""

    target_title: str
    target_company: str | None = None
    greeting: str
    paragraphs: list[str]
    closing: str
    changes: list[ChangeRecord] = Field(
        default_factory=list, description="Every proposed paragraph, accepted or flagged"
    )
    flags: list[LetterFlag] = Field(
        default_factory=list, description="Paragraphs to review, with the check's reason"
    )
    notes: list[str] = Field(
        default_factory=list, description="Letter-level issues for the user (structure, length)"
    )
    score: LetterScore | None = None


# ---------------------------------------------------------------- evidence enrichment

EvidenceKind = Literal["skill", "certification", "project", "bullet"]


class EvidenceProposal(_Strict):
    """A fact found in a document the user supplied, proposed for the Master CV."""

    kind: EvidenceKind
    text: str = Field(description="Skill or certification name, project description or bullet")
    name: str | None = Field(None, description="Project name (projects only)")
    attach_to: str | None = Field(None, description="Experience id a bullet belongs to")
    quote: str = Field(description="Verbatim passage of the document that states it")
    confidence: Literal["high", "medium", "low"] = Field(
        description="high: stated outright · medium: clearly shown by described work · low: hinted"
    )


class EvidenceProposals(_Strict):
    """LLM output: untrusted until `services.enrichment` checks each quote."""

    items: list[EvidenceProposal] = Field(default_factory=list)
