"""Carry edits of the selected CV's experience bullets into its Word document.

The structured CV is what the app reads; the Word file is what the user reads and sends. When
the assistant (or any caller) changes bullets, the same changes are written to the CV's own
file, and its copy in output/cvs/ is kept identical: one CV, never a second version. New
bullets copy the formatting of the bullet they follow. Anything that cannot be placed safely is
reported, never guessed.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from pydantic import BaseModel, Field

from src.cv.models import Experience, MasterCV
from src.services import cv_service
from src.services.workspace import Workspace
from src.tools.docx_tools import (
    find_paragraph,
    insert_paragraph_after,
    remove_paragraph,
    set_paragraph_text,
)


class DocumentSync(BaseModel):
    """What reached the Word document, and what the user must place by hand."""

    file: str | None = None
    written: list[str] = Field(default_factory=list)
    not_written: list[str] = Field(default_factory=list)


def _document(ws: Workspace) -> Path | None:
    asset_id = ws.active_cv_id
    if asset_id is None or asset_id == "master":
        return None
    cv_service.sync_copies(ws)  # a newer copy saved in Word is the version to edit
    path = cv_service.source_file(ws, asset_id)
    return path if path.suffix.lower() == ".docx" else None


def _anchor(role: Experience, index: int, before: MasterCV) -> list[str]:
    """Texts (nearest first) a new bullet at `index` can follow: the role's earlier bullets,
    in their old wording too where one was just reworded."""
    old = before.bullet_index()
    texts = []
    for bullet in reversed(role.bullets[:index]):
        texts.append(bullet.text)
        if bullet.id in old:
            texts.append(old[bullet.id].text)
    return texts


def sync_bullets(ws: Workspace, before: MasterCV, after: MasterCV) -> DocumentSync:
    """Write added, reworded and removed experience bullets to the selected CV's Word file."""
    path = _document(ws)
    old, new = before.bullet_index(), after.bullet_index()
    if path is None or old == new:
        return DocumentSync()
    doc = Document(str(path))
    out = DocumentSync(file=str(path))
    for bullet_id, bullet in old.items():
        if bullet_id not in new or new[bullet_id].text == bullet.text:
            continue
        paragraph = find_paragraph(doc, bullet.text)
        if paragraph is None:
            out.not_written.append(f"reword: {new[bullet_id].text}")
        else:
            set_paragraph_text(paragraph, new[bullet_id].text)
            out.written.append(f"reworded: {new[bullet_id].text}")
    for role in after.experience:
        for index, bullet in enumerate(role.bullets):
            if bullet.id in old:
                continue
            anchor = next(
                (p for t in _anchor(role, index, before) if (p := find_paragraph(doc, t))), None
            )
            if anchor is None:
                out.not_written.append(f"add ({role.company}): {bullet.text}")
            else:
                insert_paragraph_after(anchor, bullet.text)
                out.written.append(f"added ({role.company}): {bullet.text}")
    for bullet_id, bullet in old.items():
        if bullet_id in new:
            continue
        paragraph = find_paragraph(doc, bullet.text)
        if paragraph is None:
            out.not_written.append(f"remove: {bullet.text}")
        else:
            remove_paragraph(paragraph)
            out.written.append(f"removed: {bullet.text}")
    if out.written:
        doc.save(str(path))
        cv_service.mirror_copy(ws, path)
    return out
