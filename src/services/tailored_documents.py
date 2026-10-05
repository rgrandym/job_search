"""Persist guarded tailored CV drafts for review and later cover letters."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from src.cv.ats import check_docx
from src.cv.docx_exporter import export_docx
from src.cv.models import (
    JDAnalysis,
    MasterCV,
    TailoredCV,
    TailoredCVEdits,
    TailoredDocument,
)
from src.cv.tailor import revise_tailored
from src.jobs.models import JobPosting
from src.services.workspace import Workspace

DOCUMENT_ID = re.compile(r"[a-f0-9]{24}")


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
        source_cv=source_cv,
        jd=jd,
        tailored=tailored,
        reviewed=reviewed,
        imported=imported,
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
        TailoredDocument.model_validate_json(path.read_text())
        for path in directory.glob("*.json")
    ]
    return sorted(documents, key=lambda d: d.updated_at, reverse=True)


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
    if edits.headline is None and edits.summary is None and not edits.bullets:
        if document.reviewed:
            return document
        updated = document.model_copy(
            update={"reviewed": True, "updated_at": datetime.now(UTC).isoformat()}
        )
        _save(ws, updated)
        return updated
    revised = revise_tailored(document.source_cv, document.tailored, edits, document.jd)
    original_path = Path(document.filename)
    if original_path.name != document.filename:
        raise ValueError("Invalid tailored CV filename")
    directory = ws.output_dir / "cvs"
    path = directory / f"{original_path.stem}_edited.docx"
    number = 2
    while path.exists():
        path = directory / f"{original_path.stem}_edited_{number}.docx"
        number += 1
    export_docx(revised, path, document.template)
    keywords = revised.matched_keywords + revised.missing_keywords
    revised = revised.model_copy(update={"ats": check_docx(path, revised.cv, keywords)})
    updated = document.model_copy(
        update={
            "tailored": revised,
            "filename": path.name,
            "updated_at": datetime.now(UTC).isoformat(),
            "reviewed": True,
        }
    )
    _save(ws, updated)
    return updated
