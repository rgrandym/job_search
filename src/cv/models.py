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


class TailoringPlan(_Strict):
    """LLM output: what to change. Applied deterministically by `tailor.apply_plan`."""

    headline_options: list[str] = Field(
        default_factory=list,
        description="Up to 4 alternative headlines for this job; the CV keeps its own headline "
        "and the user may choose one of these",
    )
    summary: str | None = None
    rewritten_bullets: list[RewrittenBullet] = Field(default_factory=list)
    bullet_order: list[RoleOrder] = Field(
        default_factory=list,
        description="For every role: its experience id and bullet ids, most relevant first. "
        "Bullets not listed move to the end of their role; they are left out only when "
        "trimming is allowed",
    )
    skills_priority: list[str] = Field(default_factory=list)

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

    def has_issues(self) -> bool:
        return bool(self.missed_requirements or self.weak_bullets or self.order_notes)

    def notes(self) -> list[str]:
        """One line per point, for the user."""
        return [
            *(f"Surface {m.requirement} ({m.source_id})" for m in self.missed_requirements),
            *(f"Strengthen {b.source_id}: {b.issue}" for b in self.weak_bullets),
            *self.order_notes,
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
    conclusion: LetterParagraph
    closing: str = "Yours sincerely,"


class CoverLetterDraft(_Strict):
    """LLM output: untrusted until `cover_letter.apply_letter`."""

    greeting: str = "Dear Hiring Manager,"
    opening: LetterOpening | None = None
    evidence: list[LetterParagraph] = Field(default_factory=list)
    conclusion: LetterParagraph | None = None
    # Older callers can still pass paragraphs directly to apply_letter.
    paragraphs: list[LetterParagraph] = Field(default_factory=list)
    closing: str = "Yours sincerely,"


class CoverLetter(_Strict):
    """A guarded cover letter: only paragraphs whose claims trace to the Master CV."""

    target_title: str
    target_company: str | None = None
    greeting: str
    paragraphs: list[str]
    closing: str
    changes: list[ChangeRecord] = Field(
        default_factory=list, description="Every proposed paragraph, accepted or rejected"
    )


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
