"""Targeted edits of a Master CV: add, reword or remove one bullet or role, or set one section.

Each edit names what it changes by id, so a caller (the assistant) never has to resend the
rest of the CV and nothing it did not name can be lost. The result is re-validated.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.cv import master_cv_manager as mgr
from src.cv.models import Bullet, Experience, MasterCV

Section = Literal["basics", "education", "skills", "certifications", "projects", "languages"]


class CVEdit(BaseModel):
    """One change. Fields used per action:
    add_bullet: role_id, text, optional after (a bullet id; default: end of the role) ·
    edit_bullet: bullet_id, text · remove_bullet: bullet_id ·
    edit_role: role_id, fields (title, company, location, start, end) · remove_role: role_id ·
    add_role: fields (company, title, location, start, end) and optional bullets (texts) ·
    set_section: section, value (basics merges field by field; other sections are replaced)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "add_bullet",
        "edit_bullet",
        "remove_bullet",
        "edit_role",
        "add_role",
        "remove_role",
        "set_section",
    ]
    role_id: str | None = None
    bullet_id: str | None = None
    after: str | None = Field(None, description="add_bullet: insert after this bullet id")
    text: str | None = None
    bullets: list[str] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    section: Section | None = None
    value: Any = None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _role(cv: MasterCV, role_id: str | None) -> Experience:
    role = next((r for r in cv.experience if r.id == role_id), None)
    if role is None:
        raise ValueError(f"No role with id {role_id!r}; ids: {[r.id for r in cv.experience]}")
    return role


def _bullet_role(cv: MasterCV, bullet_id: str | None) -> tuple[Experience, int]:
    for role in cv.experience:
        for index, bullet in enumerate(role.bullets):
            if bullet.id == bullet_id:
                return role, index
    raise ValueError(f"No bullet with id {bullet_id!r}")


def _required(value: str | None, what: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{what} is required")
    return value.strip()


def _add_bullet(cv: MasterCV, edit: CVEdit) -> None:
    role = _role(cv, edit.role_id)
    bullet = Bullet(id=_new_id(role.id), text=_required(edit.text, "text"))
    if edit.after is None:
        role.bullets.append(bullet)
        return
    owner, index = _bullet_role(cv, edit.after)
    if owner.id != role.id:
        raise ValueError(f"Bullet {edit.after!r} is not in role {role.id!r}")
    role.bullets.insert(index + 1, bullet)


def _add_role(cv: MasterCV, edit: CVEdit) -> None:
    data = {"id": _new_id("role"), **edit.fields}
    data["bullets"] = [{"id": _new_id(data["id"]), "text": text} for text in edit.bullets]
    cv.experience.insert(0, Experience.model_validate(data))


def _apply(cv: MasterCV, edit: CVEdit) -> MasterCV:
    if edit.action == "add_bullet":
        _add_bullet(cv, edit)
    elif edit.action == "edit_bullet":
        role, index = _bullet_role(cv, edit.bullet_id)
        role.bullets[index].text = _required(edit.text, "text")
    elif edit.action == "remove_bullet":
        role, index = _bullet_role(cv, edit.bullet_id)
        role.bullets.pop(index)
    elif edit.action == "edit_role":
        role = _role(cv, edit.role_id)
        allowed = {"title", "company", "location", "start", "end"}
        if not edit.fields or set(edit.fields) - allowed:
            raise ValueError(f"edit_role fields must be among {sorted(allowed)}")
        index = cv.experience.index(role)
        cv.experience[index] = Experience.model_validate({**role.model_dump(), **edit.fields})
    elif edit.action == "add_role":
        _add_role(cv, edit)
    elif edit.action == "remove_role":
        cv.experience.remove(_role(cv, edit.role_id))
    else:
        if edit.section is None or edit.value is None:
            raise ValueError("set_section needs section and value")
        return mgr.update(cv, {edit.section: edit.value})
    return cv


def apply_edits(cv: MasterCV, edits: list[CVEdit]) -> MasterCV:
    """A copy of `cv` with every edit applied in order; raises on the first invalid one."""
    out = cv.model_copy(deep=True)
    for number, edit in enumerate(edits, 1):
        try:
            out = _apply(out, edit)
        except ValueError as exc:
            raise ValueError(f"Edit {number} ({edit.action}): {exc}") from exc
    return MasterCV.model_validate(out.model_dump())
