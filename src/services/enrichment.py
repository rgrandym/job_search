"""Evidence enrichment: propose Master CV additions from documents the user supplies.

The user uploads (or pastes) a document: a portfolio page, project report, publication list,
reference letter, certificate. The LLM proposes skills, certifications, projects and bullets
the Master CV lacks, each with a verbatim quote; code drops any proposal whose quote is not
in the document, or that the CV already has. Proposals wait in a review queue
(`data/evidence_queue.json`); only the ones the user accepts are written to the Master CV
(the no-fabrication contract: new facts enter the Master CV after the user confirms them).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from src.core.llm import UsageSink
from src.cv.models import (
    Bullet,
    Certification,
    EvidenceKind,
    EvidenceProposal,
    EvidenceProposals,
    MasterCV,
    Project,
    SkillGroup,
)
from src.cv.tailor import cv_to_text
from src.services import cv_service
from src.services.workspace import Workspace
from src.tools.search_tools import extract_numbers, mentions

ADDED_SKILLS = "Additional"  # skill group for accepted skills
MAX_DOC_CHARS = 30_000

EVIDENCE_SYSTEM = """You compare a document the candidate supplied with their Master CV and \
propose facts about the candidate that the CV does not already contain.

Rules:
- Only facts about the candidate themselves, stated or clearly demonstrated in the document. \
Never infer a skill from a job title alone; never generalise ("leadership") from one line.
- Every item quotes the passage that states it, copied exactly from the document (under 200 \
characters). An item without an exact quote is discarded.
- kind: "skill" (a named skill, tool or method), "certification", "project" (give a short \
name and a one-sentence description), or "bullet" (an accomplishment in a role the CV \
already lists: set attach_to to that role's id; keep the document's numbers exactly).
- confidence: high when the document states it outright, medium when the described work \
clearly shows it, low when it only hints at it.
- Skip anything the Master CV already says, even in other words."""


class QueuedEvidence(BaseModel):
    """A proposal waiting for (or decided by) the user."""

    id: str
    owner: str  # the CV it is for
    source: str  # document name
    kind: EvidenceKind
    text: str
    name: str | None = None
    attach_to: str | None = None
    quote: str
    confidence: Literal["high", "medium", "low"]
    status: Literal["pending", "accepted", "rejected"] = "pending"
    created_at: str


def _path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "evidence_queue.json"


def _load(ws: Workspace) -> list[QueuedEvidence]:
    try:
        return TypeAdapter(list[QueuedEvidence]).validate_json(_path(ws).read_bytes())
    except (OSError, ValueError):
        return []


def _save(ws: Workspace, items: list[QueuedEvidence]) -> None:
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([i.model_dump(mode="json") for i in items], indent=1), "utf-8")


def _owner(ws: Workspace) -> str:
    if not ws.active_cv_id:
        raise ValueError("Select a CV first: evidence is added to the selected CV")
    return ws.active_cv_id


def _plain(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").split())


def _already_in(cv: MasterCV, item: EvidenceProposal) -> bool:
    text = cv_to_text(cv)
    if item.kind in ("skill", "certification"):
        return mentions(text, item.text)
    return _plain(item.text) in _plain(text)


def check_proposals(
    cv: MasterCV, document: str, proposals: EvidenceProposals
) -> list[EvidenceProposal]:
    """Keep proposals whose quote is in the document, whose numbers all come from that quote,
    that the CV lacks, and (bullets) that name a role the CV has."""
    doc = _plain(document)
    roles = {e.id for e in cv.experience}
    return [
        p
        for p in proposals.items
        if p.quote.strip()
        and _plain(p.quote) in doc
        and extract_numbers(p.text) <= extract_numbers(p.quote)
        and not _already_in(cv, p)
        and (p.kind != "bullet" or p.attach_to in roles)
    ]


async def propose(
    ws: Workspace, source: str, document: str, usage_sink: UsageSink | None = None
) -> list[QueuedEvidence]:
    """Queue checked proposals from one document's text; returns the new queue items."""
    owner = _owner(ws)
    cv = await cv_service.ensure_selected_cv(ws, usage_sink)
    text = document[:MAX_DOC_CHARS]
    if len(text.strip()) < 40:
        raise ValueError("The document has too little text to read")
    prompt = (
        f"<master_cv>\n{cv.model_dump_json(indent=1, exclude={'preferences'})}\n</master_cv>"
        f"\n\n<document name={source!r}>\n{text}\n</document>"
    )
    llm = ws.structured("quality", usage_sink, "Evidence review")
    raw = await asyncio.to_thread(
        llm.generate, system=EVIDENCE_SYSTEM, prompt=prompt, output_model=EvidenceProposals
    )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    new = [
        QueuedEvidence(
            id=uuid.uuid4().hex[:12], owner=owner, source=source, created_at=now, **p.model_dump()
        )  # fmt: skip
        for p in check_proposals(cv, text, raw)
    ]
    _save(ws, _load(ws) + new)
    return new


async def propose_from_file(
    ws: Workspace, filename: str, data: bytes, usage_sink: UsageSink | None = None
) -> list[QueuedEvidence]:
    """Read an uploaded document (.pdf, .docx, .md, .txt) and queue its proposals."""
    return await propose(ws, filename, cv_service.extract_text(filename, data), usage_sink)


def queue(ws: Workspace) -> list[QueuedEvidence]:
    """The selected CV's pending proposals, newest first."""
    owner = ws.active_cv_id
    items = [i for i in _load(ws) if i.owner == owner and i.status == "pending"]
    return sorted(items, key=lambda i: i.created_at, reverse=True)


def decide(ws: Workspace, item_id: str, accept: bool, text: str | None = None) -> QueuedEvidence:
    """The user's decision. Accepting writes the fact (optionally reworded by the user) to
    the selected Master CV."""
    items = _load(ws)
    item = next((i for i in items if i.id == item_id and i.owner == ws.active_cv_id), None)
    if item is None or item.status != "pending":
        raise ValueError("That proposal is not pending for the selected CV")
    if accept:
        if text is not None and text.strip():
            item.text = text.strip()
        if ws.master_cv is None:
            raise ValueError("The selected CV has not been read yet")
        cv_service.save_selected_cv(ws, add_to_cv(ws.master_cv, item))
    item.status = "accepted" if accept else "rejected"
    _save(ws, items)
    return item


def _unique_id(base: str, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:30] or "item"
    candidate, n = slug, 2
    while candidate in taken:
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def add_to_cv(cv: MasterCV, item: QueuedEvidence) -> MasterCV:
    """A copy of `cv` with the accepted fact added in its place."""
    out = cv.model_copy(deep=True)
    taken = {e.id for e in out.experience} | {p.id for p in out.projects} | set(out.bullet_index())
    if item.kind == "skill":
        group = next((g for g in out.skills if g.category == ADDED_SKILLS), None)
        if group is None:
            out.skills.append(SkillGroup(category=ADDED_SKILLS, items=[item.text]))
        elif item.text not in group.items:
            group.items.append(item.text)
    elif item.kind == "certification":
        out.certifications.append(Certification(name=item.text))
    elif item.kind == "project":
        name = item.name or item.text[:40]
        out.projects.append(Project(id=_unique_id(name, taken), name=name, description=item.text))
    else:
        exp = next(e for e in out.experience if e.id == item.attach_to)
        numbers = re.findall(r"\$?\d[\d,.]*\s?(?:%|[kmb]\b)?", item.text)
        exp.bullets.append(
            Bullet(
                id=_unique_id(f"{exp.id}-added", taken),
                text=item.text,
                metrics=[n.strip() for n in numbers],
            )  # fmt: skip
        )
    return MasterCV.model_validate(out.model_dump())
