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


class TailoringPlan(_Strict):
    """LLM output: what to change. Applied deterministically by `tailor.apply_plan`."""

    headline: str | None = None
    summary: str | None = None
    rewritten_bullets: list[RewrittenBullet] = Field(default_factory=list)
    bullet_order: dict[str, list[str]] = Field(
        default_factory=dict, description="experience id -> ordered bullet ids to keep"
    )
    skills_priority: list[str] = Field(default_factory=list)


class ChangeRecord(_Strict):
    source_id: str
    original: str
    tailored: str
    accepted: bool
    reason: str | None = None


class TailoredCV(_Strict):
    """A tailored CV plus its full audit trail back to the Master CV."""

    cv: MasterCV
    target_title: str
    target_company: str | None = None
    keyword_coverage: float = Field(ge=0, le=1)
    matched_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    changes: list[ChangeRecord] = Field(default_factory=list)
