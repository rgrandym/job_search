"""Saved, editable cover letters and their versioned Word and text exports."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from docx import Document
from pydantic import BaseModel, ConfigDict, Field

from src.cv.cover_letter import apply_letter
from src.cv.docx_exporter import export_cover_letter
from src.cv.models import (
    Basics,
    CoverLetter,
    CoverLetterDraft,
    JDAnalysis,
    LetterParagraph,
    MasterCV,
)
from src.jobs.models import JobPosting
from src.services import document_files
from src.services.workspace import Workspace


class LetterEdits(BaseModel):
    """Editable cover-letter text; the four body paragraphs remain separate."""

    model_config = ConfigDict(extra="forbid")

    greeting: str = Field(min_length=1)
    paragraphs: list[str] = Field(min_length=1)
    closing: str = Field(min_length=1)


class SavedLetter(BaseModel):
    """Persistent source and export information for one letter."""

    model_config = ConfigDict(extra="forbid")

    id: str
    job_id: str = ""
    title: str = ""
    company: str = ""
    candidate_name: str
    created_at: str
    updated_at: str
    filename: str
    versions: list[str] = Field(default_factory=list)
    template: str = "classic"
    letter: CoverLetter
    source_cv: MasterCV | None = None
    jd_text: str = ""


def _directory(ws: Workspace) -> Path:
    return ws.settings.data_dir / "cover_letters"


def _save(ws: Workspace, document: SavedLetter) -> None:
    directory = _directory(ws)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document.id}.json"
    temporary = directory / f".{document.id}.tmp"
    temporary.write_text(document.model_dump_json(indent=2))
    temporary.replace(path)


def create(
    ws: Workspace,
    job: JobPosting,
    letter: CoverLetter,
    source_cv: MasterCV,
    jd_text: str,
    template: str,
    path: Path,
) -> SavedLetter:
    """Persist the exact letter and CV evidence alongside its Word export."""
    now = datetime.now(UTC).isoformat()
    document = SavedLetter(
        id=secrets.token_hex(12),
        job_id=job.id,
        title=job.title,
        company=job.company,
        candidate_name=source_cv.basics.name,
        created_at=now,
        updated_at=now,
        filename=path.name,
        template=template,
        letter=letter,
        source_cv=source_cv,
        jd_text=jd_text,
    )
    _save(ws, document)
    return document


def _legacy(ws: Workspace) -> list[SavedLetter]:
    """Expose older Word letters without moving or overwriting their files."""
    paths = list(ws.output_dir.glob("*cover_letter*.docx"))
    paths += list((ws.output_dir / "cover_letters").glob("*.docx"))
    known = {name for document in _stored(ws) for name in [document.filename, *document.versions]}
    letters: list[SavedLetter] = []
    for path in paths:
        if path.name in known:
            continue
        try:
            letters.append(_read_legacy(path))
        except ValueError:
            continue
    return letters


def _read_legacy(path: Path) -> SavedLetter:
    paragraphs = [paragraph.text.strip() for paragraph in Document(str(path)).paragraphs]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if len(paragraphs) < 3:
        raise ValueError(f"Cover letter {path.name} has no readable body")
    name = paragraphs[-1]
    body = paragraphs[1:-2]
    return SavedLetter(
        id="legacy-" + hashlib.sha256(str(path).encode()).hexdigest()[:24],
        candidate_name=name,
        created_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        updated_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        filename=path.name,
        letter=CoverLetter(
            target_title="",
            greeting=paragraphs[0],
            paragraphs=body,
            closing=paragraphs[-2],
        ),
    )


def _stored(ws: Workspace) -> list[SavedLetter]:
    return [
        SavedLetter.model_validate_json(path.read_text()) for path in _directory(ws).glob("*.json")
    ]


def list_all(ws: Workspace) -> list[SavedLetter]:
    """List current and earlier cover letters, newest first."""
    document_files.organize_legacy_output(ws)
    return sorted([*_stored(ws), *_legacy(ws)], key=lambda item: item.updated_at, reverse=True)


def load(ws: Workspace, document_id: str) -> SavedLetter:
    """Find a saved letter by its opaque ID."""
    document = next((item for item in list_all(ws) if item.id == document_id), None)
    if document is None:
        raise ValueError("Cover letter was not found")
    return document


def delete(ws: Workspace, document_id: str) -> None:
    """Remove one saved or legacy letter and its local exports."""
    document = load(ws, document_id)
    if not re.fullmatch(r"(?:[0-9a-f]{24}|legacy-[0-9a-f]{24})", document.id):
        raise ValueError("Invalid cover-letter identifier")
    names = {document.filename, *document.versions}
    for name in names:
        if Path(name).name != name or Path(name).suffix.lower() != ".docx":
            raise ValueError("Invalid cover-letter filename")
    other_stems = {
        Path(name).stem
        for other in list_all(ws)
        if other.id != document.id
        for name in [other.filename, *other.versions]
    }
    own_stems = {Path(name).stem for name in names}
    for path in (ws.output_dir / "cover_letters").glob("*.txt"):
        if _matches_text_export(path.name, own_stems) and not _matches_text_export(
            path.name, other_stems
        ):
            path.unlink()
    directories = (ws.output_dir, ws.output_dir / "cover_letters")
    for name in names:
        for directory in directories:
            (directory / name).unlink(missing_ok=True)
    if not document.id.startswith("legacy-"):
        (_directory(ws) / f"{document.id}.json").unlink(missing_ok=True)


def _matches_text_export(filename: str, stems: set[str]) -> bool:
    """Match plain-text exports made from a Word version's filename."""
    return any(re.fullmatch(rf"{re.escape(stem)}(?:_[0-9]+)?\.txt", filename) for stem in stems)


def delete_all(ws: Workspace) -> int:
    """Remove every letter currently in the library."""
    ids = [document.id for document in list_all(ws)]
    for document_id in ids:
        delete(ws, document_id)
    return len(ids)


def _path(ws: Workspace, document: SavedLetter) -> Path:
    if Path(document.filename).name != document.filename:
        raise ValueError("Invalid cover-letter filename")
    directory = (
        ws.output_dir if document.id.startswith("legacy-") else ws.output_dir / "cover_letters"
    )
    path = directory / document.filename
    if document.id.startswith("legacy-") and not path.is_file():
        path = ws.output_dir / "cover_letters" / document.filename
    return path


def edit(ws: Workspace, document_id: str, edits: LetterEdits) -> SavedLetter:
    """Save edited text and a new Word version, checking claims when CV evidence is available."""
    document = load(ws, document_id)
    if len(edits.paragraphs) != len(document.letter.paragraphs):
        raise ValueError("Keep the same number of body paragraphs")
    if document.source_cv is not None:
        source_ids = [
            change.original.split(", ") if change.original else []
            for change in document.letter.changes
            if change.accepted
        ]
        draft = CoverLetterDraft(
            greeting=edits.greeting,
            paragraphs=[
                LetterParagraph(text=text, source_ids=source_ids[index])
                for index, text in enumerate(edits.paragraphs)
            ],
            closing=edits.closing,
        )
        checked = apply_letter(
            document.source_cv,
            draft,
            JDAnalysis(job_title=document.title, company=document.company),
            document.jd_text,
        )
        if len(checked.paragraphs) != len(edits.paragraphs):
            reasons = "; ".join(c.reason or "" for c in checked.changes if not c.accepted)
            raise ValueError(reasons)
        if checked.greeting != edits.greeting or checked.closing != edits.closing:
            raise ValueError("Greeting and closing cannot contain unsupported numbers")
        letter = checked
    else:
        letter = document.letter.model_copy(
            update={
                "greeting": edits.greeting,
                "paragraphs": edits.paragraphs,
                "closing": edits.closing,
            }
        )
    stem = Path(document.filename).stem + "_edited"
    path = _new_path(ws, stem, ".docx")
    cv = document.source_cv or MasterCV(basics=Basics(name=document.candidate_name))
    export_cover_letter(letter, cv, path, document.template)
    now = datetime.now(UTC).isoformat()
    updated = document.model_copy(
        update={
            "id": secrets.token_hex(12) if document.id.startswith("legacy-") else document.id,
            "updated_at": now,
            "filename": path.name,
            "versions": [*document.versions, document.filename],
            "letter": letter,
        }
    )
    _save(ws, updated)
    return updated


def _new_path(ws: Workspace, stem: str, suffix: str) -> Path:
    directory = ws.output_dir / "cover_letters"
    path = directory / f"{stem}{suffix}"
    number = 2
    while path.exists():
        path = directory / f"{stem}_{number}{suffix}"
        number += 1
    return path


def export(ws: Workspace, document_id: str, format: Literal["docx", "txt"]) -> Path:
    """Return the saved Word file or make a plain-text export."""
    document = load(ws, document_id)
    if format == "docx":
        path = _path(ws, document)
        if not path.is_file():
            raise ValueError("Cover-letter file is unavailable")
        return path
    path = _new_path(ws, Path(document.filename).stem, ".txt")
    parts = [document.letter.greeting, *document.letter.paragraphs, document.letter.closing]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join([*parts, document.candidate_name]) + "\n")
    return path
