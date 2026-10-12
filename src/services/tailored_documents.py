"""Persist guarded tailored CV drafts for review and later cover letters."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from src.cv.ats import check_docx
from src.cv.docx_exporter import TEMPLATES, export_docx
from src.cv.docx_original import restore_normalized_indents, write_like_original
from src.cv.models import (
    JDAnalysis,
    MasterCV,
    SearchFitBaseline,
    SkillGroup,
    TailoredCV,
    TailoredCVEdits,
    TailoredDocument,
    TailoringSettings,
)
from src.cv.tailor import keyword_coverage, revise_tailored
from src.jobs.models import JobPosting
from src.services.workspace import Workspace

DOCUMENT_ID = re.compile(r"[a-f0-9]{24}")


ORIGINAL = "original"  # template name: a copy of the CV's own Word file


def _directory(ws: Workspace) -> Path:
    return ws.settings.data_dir / "tailored_cvs"


def _save(ws: Workspace, document: TailoredDocument) -> None:
    directory = _directory(ws)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document.id}.json"
    temporary = directory / f".{document.id}.tmp"
    temporary.write_text(document.model_dump_json(indent=2))
    temporary.replace(path)


def create(
    ws: Workspace,
    job_id: str,
    source_cv: MasterCV,
    jd: JDAnalysis,
    tailored: TailoredCV,
    template: str,
    path: Path,
    *,
    posting: JobPosting | None = None,
    reviewed: bool = True,
    imported: bool = False,
    original_file: Path | None = None,
    settings: TailoringSettings | None = None,
    search_fit: SearchFitBaseline | None = None,
) -> TailoredDocument:
    """Keep the exact CV and source evidence used for an exported Word draft."""
    now = datetime.now(UTC).isoformat()
    document_id = secrets.token_hex(12)
    tailored = tailored.model_copy(update={"document_id": document_id})
    document = TailoredDocument(
        id=document_id,
        cv_id=ws.active_cv_id or "master",
        job_id=job_id,
        job_title=posting.title if posting else jd.job_title,
        job_company=posting.company if posting else jd.company or "",
        job_description=posting.description if posting else "",
        job_location=posting.location if posting else None,
        created_at=now,
        updated_at=now,
        template=template,
        filename=path.name,
        original_file=str(original_file) if original_file else None,
        source_cv=source_cv,
        jd=jd,
        tailored=tailored,
        reviewed=reviewed,
        imported=imported,
        settings=settings,
        search_fit=search_fit,
    )
    _save(ws, document)
    return document


def list_for_job(ws: Workspace, job_id: str) -> list[TailoredDocument]:
    """List saved drafts for one posting, newest first."""
    return [d for d in list_all(ws) if d.job_id == job_id]


def list_all(ws: Workspace) -> list[TailoredDocument]:
    """List every saved tailored draft, newest first."""
    directory = _directory(ws)
    if not directory.exists():
        return []
    documents = [
        TailoredDocument.model_validate_json(path.read_text()) for path in directory.glob("*.json")
    ]
    return sorted(documents, key=lambda d: d.updated_at, reverse=True)


def ensure_original_layout(ws: Workspace, path: Path) -> bool:
    """Repair a saved viewer rewrite when the original CV's indents can be matched safely."""
    if path.suffix.lower() != ".docx" or not path.is_file():
        return False
    document = next((d for d in list_all(ws) if d.filename == path.name), None)
    if document is None or document.template != ORIGINAL or not document.original_file:
        return False
    original = Path(document.original_file)
    if not original.is_file() or original.resolve() == path.resolve():
        return False
    return restore_normalized_indents(original, path)


def delete_for_file(ws: Workspace, filename: str) -> None:
    """Discard saved drafts whose current Word export was removed from the CV library."""
    for document in list_all(ws):
        if document.filename == filename:
            (_directory(ws) / f"{document.id}.json").unlink()
            current = ws.tailored.get(document.job_id)
            if current is not None and current.document_id == document.id:
                ws.tailored.pop(document.job_id, None)


def load(ws: Workspace, document_id: str, job_id: str | None = None) -> TailoredDocument:
    """Load a draft and ensure it belongs to the requested posting, if given."""
    if not DOCUMENT_ID.fullmatch(document_id):
        raise ValueError("Invalid tailored CV identifier")
    path = _directory(ws) / f"{document_id}.json"
    if not path.is_file():
        raise ValueError("Tailored CV was not found")
    document = TailoredDocument.model_validate_json(path.read_text())
    if job_id is not None and document.job_id != job_id:
        raise ValueError("Tailored CV does not belong to this job")
    return document


def edit(ws: Workspace, document_id: str, edits: TailoredCVEdits) -> TailoredDocument:
    """Guard edits against the source CV, then save a new Word version."""
    document = load(ws, document_id)
    pending = any(a.status == "pending" for a in document.tailored.review_additions)
    if edits.headline is None and edits.summary is None and not edits.bullets:
        if document.reviewed or pending:
            return document
        updated = document.model_copy(
            update={"reviewed": True, "updated_at": datetime.now(UTC).isoformat()}
        )
        _save(ws, updated)
        return updated
    revised = revise_tailored(document.source_cv, document.tailored, edits, document.jd)
    return _write_revision(ws, document, revised, [] if edits.headline is not None else None)


def _write_revision(
    ws: Workspace,
    document: TailoredDocument,
    revised: TailoredCV,
    options: list[str] | None = None,
    source_cv: MasterCV | None = None,
) -> TailoredDocument:
    """Write a new Word version after a guarded edit or a proposal decision."""
    original_path = Path(document.filename)
    if original_path.name != document.filename:
        raise ValueError("Invalid tailored CV filename")
    directory = ws.output_dir / "cvs"
    path = directory / f"{original_path.stem}_edited.docx"
    number = 2
    while path.exists():
        path = directory / f"{original_path.stem}_edited_{number}.docx"
        number += 1
    original = Path(document.original_file) if document.original_file else None
    # Suggested headlines stay in the file until the user picks one.
    headlines = revised.headline_options if options is None else options
    source = source_cv or document.source_cv
    if document.template == ORIGINAL and original is not None and original.is_file():
        write_like_original(
            original,
            source,
            revised.cv,
            path,
            headlines,
            revised.role_skills,
            revised.academic,
            revised.skills_to_confirm,
            revised.review_additions,
        )
    else:
        export_docx(
            revised.model_copy(update={"headline_options": headlines}),
            path,
            document.template if document.template in TEMPLATES else "classic",
        )
    keywords = revised.matched_keywords + revised.missing_keywords
    revised = revised.model_copy(update={"ats": check_docx(path, revised.cv, keywords)})
    updated = document.model_copy(
        update={
            "tailored": revised,
            "source_cv": source,
            "filename": path.name,
            "updated_at": datetime.now(UTC).isoformat(),
            "reviewed": not any(a.status == "pending" for a in revised.review_additions),
        }
    )
    _save(ws, updated)
    return updated


def review_addition(
    ws: Workspace,
    document_id: str,
    addition_id: str,
    decision: Literal["confirm", "delete"],
    text: str | None = None,
) -> TailoredDocument:
    """Accept a user-confirmed fact into the selected Master CV, or remove the question."""
    document = load(ws, document_id)
    addition = next((a for a in document.tailored.review_additions if a.id == addition_id), None)
    if addition is None or addition.status != "pending":
        raise ValueError("Pending review addition was not found")
    wording = (text if text is not None else addition.text).strip()
    if decision == "confirm" and (not wording or len(wording) > 400):
        raise ValueError("Enter a concise, accurate skill or experience before confirming")
    source = document.source_cv.model_copy(deep=True)
    revised = document.tailored.model_copy(deep=True)
    if decision == "confirm":
        from src.cv.edits import CVEdit, apply_edits
        from src.services import cv_document, cv_service

        if ws.active_cv_id != document.cv_id:
            raise ValueError("Select the CV this draft came from before confirming a new fact")
        current = cv_service.cached_selected_cv(ws)
        if current is None:
            raise ValueError("The source CV is unavailable; select it in the library first")
        if addition.kind == "experience":
            role_id = addition.role_id or ""
            if not any(r.id == role_id for r in current.experience):
                raise ValueError("The source role is no longer in the selected CV")
            updated = apply_edits(
                current, [CVEdit(action="add_bullet", role_id=role_id, text=wording)]
            )
            created = next(
                b for b in updated.bullet_index().values() if b.id not in current.bullet_index()
            )
            for cv in (source, revised.cv):
                role = next((r for r in cv.experience if r.id == role_id), None)
                if role is None:
                    raise ValueError("The proposed role is no longer in this draft")
                role.bullets.append(created.model_copy(deep=True))
            synced = cv_document.sync_bullets(ws, current, updated)
            if synced.not_written:
                raise ValueError("Could not place the confirmed experience in the source Word CV")
        else:
            if any(wording.casefold() == s.casefold() for s in current.all_skills()):
                raise ValueError("This skill is already in the source CV")
            updated = current.model_copy(deep=True)
            for cv in (updated, source, revised.cv):
                group = next((g for g in cv.skills if g.category == "Additional"), None)
                if group is None:
                    cv.skills.append(SkillGroup(category="Additional", items=[wording]))
                elif wording not in group.items:
                    group.items.append(wording)
            cv_document.sync_confirmed_skill(ws, wording)
            revised.role_skills = list(dict.fromkeys([*revised.role_skills, wording]))[:8]
        cv_service.save_selected_cv(ws, updated)
        revised.fit_check = None  # the reviewed addition changes the evidence being scored
        matched, missing = keyword_coverage(revised.cv, document.jd)
        revised.matched_keywords = matched
        revised.missing_keywords = missing
        total = len(matched) + len(missing)
        revised.keyword_coverage = len(matched) / total if total else 1.0
    revised.review_additions = [
        a.model_copy(
            update={"status": "confirmed" if decision == "confirm" else "deleted", "text": wording}
        )
        if a.id == addition_id
        else a
        for a in revised.review_additions
    ]
    return _write_revision(ws, document, revised, source_cv=source)


def as_tailored(document: TailoredDocument) -> MasterCV:
    """The source CV as tailoring read it: without the red suggestions confirmed since, which
    were added to it. This is the CV the search scored, so comparisons keep it as the original."""
    cv = document.source_cv.model_copy(deep=True)
    for item in document.tailored.review_additions:
        if item.status != "confirmed":
            continue
        if item.kind == "experience":
            for role in cv.experience:
                if role.id == item.role_id:
                    role.bullets = [b for b in role.bullets if b.text != item.text]
        else:
            for group in cv.skills:
                if group.category == "Additional":
                    group.items = [s for s in group.items if s != item.text]
            cv.skills = [g for g in cv.skills if g.items or g.category != "Additional"]
    return cv
