"""Compare the CV versions tailored for one job, side by side, against the originals.

compare(ws, job_id, document_ids) -> VersionComparison: the chosen tailored versions and
the original CV each came from, read by one blind reader in one call
(`fit_check.score_versions`), then saved per job so the report opens again without a
model call. labels(documents): "V1 · Automatic", "V2 · Level: senior" ...
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from src.core import progress
from src.core.llm import UsageSink
from src.cv.fit_check import MAX_VERSIONS, best_version, requirements_for, score_versions
from src.cv.models import TailoredDocument, VersionComparison, VersionReading
from src.cv.tailor import ats_keywords, cv_to_text, printed_text
from src.services import cv_service, tailored_documents
from src.services.workspace import Workspace

SETTING_LABELS = {
    "level": {"senior": "Level: more senior", "junior": "Level: more junior"},
    "length": {"full": "Full length", "junior": "Trimmed (junior)"},
    "emphasis": {"leadership": "Leadership", "hands_on": "Hands-on"},
    "audience": {"industry": "Industry", "academic": "Academic"},
    "template": {"classic": "Classic design", "modern": "Modern design", "compact": "Compact"},
}


def _describe(document: TailoredDocument) -> str:
    if document.imported:
        return "Imported Word CV"
    if document.settings is None:
        return "Tailored"
    chosen = [
        names[value]
        for field, names in SETTING_LABELS.items()
        if (value := getattr(document.settings, field)) in names
    ]
    return " · ".join(chosen) or "Automatic"


def labels(documents: list[TailoredDocument]) -> dict[str, str]:
    """A short name per tailored CV, numbered per job in the order they were made."""
    out: dict[str, str] = {}
    counts: dict[str, int] = {}
    for document in sorted(documents, key=lambda d: d.created_at):
        counts[document.job_id] = counts.get(document.job_id, 0) + 1
        out[document.id] = f"V{counts[document.job_id]} · {_describe(document)}"
    return out


def _path(ws: Workspace, job_id: str) -> Path:
    key = hashlib.sha256(job_id.encode()).hexdigest()[:24]
    return ws.settings.data_dir / "cv_comparisons" / f"{key}.json"


def saved(ws: Workspace, job_id: str) -> tuple[VersionComparison | None, bool]:
    """The last comparison for this job, and whether a version it read has changed since."""
    path = _path(ws, job_id)
    if not path.is_file():
        return None, False
    comparison = VersionComparison.model_validate_json(path.read_text())
    current = {d.id: d for d in tailored_documents.list_for_job(ws, job_id)}
    stale = any(
        r.kind == "tailored"
        and (r.id not in current or current[r.id].updated_at > comparison.created_at)
        for r in comparison.readings
    )
    return comparison, stale


def _chosen(ws: Workspace, job_id: str, document_ids: list[str]) -> list[TailoredDocument]:
    documents = tailored_documents.list_for_job(ws, job_id)
    if not documents:
        raise ValueError("Tailor a CV for this job first")
    if not document_ids:
        return documents
    known = {d.id: d for d in documents}
    missing = [i for i in document_ids if i not in known]
    if missing:
        raise ValueError("A chosen CV does not belong to this job")
    return [known[i] for i in dict.fromkeys(document_ids)]


def _original_name(ws: Workspace, cv_id: str) -> str:
    if cv_id == "master":
        return "Master CV"
    return cv_service.cv_name(ws, cv_id) or "your CV"


def _entries(
    ws: Workspace, documents: list[TailoredDocument], names: dict[str, str]
) -> tuple[list[VersionReading], list[str]]:
    """The originals (one per source CV, its newest snapshot) then the tailored versions, with
    the text a reader sees for each."""
    items: list[VersionReading] = []
    texts: list[str] = []
    originals: dict[str, TailoredDocument] = {}
    for document in documents:  # newest first, so the first seen is the newest snapshot
        if not document.imported:
            originals.setdefault(document.cv_id, document)
    for cv_id, document in originals.items():
        items.append(
            VersionReading(
                id=f"original:{cv_id}",
                kind="original",
                label=f"Original · {_original_name(ws, cv_id)}",
                score=0,
            )
        )
        texts.append(cv_to_text(document.source_cv, tags=False))
    for document in documents:
        tailored = document.tailored
        items.append(
            VersionReading(
                id=document.id,
                kind="tailored",
                label=names.get(document.id, "Tailored"),
                baseline_id=None if document.imported else f"original:{document.cv_id}",
                created_at=document.created_at,
                filename=document.filename,
                settings=document.settings,
                score=0,
                rejected_changes=sum(not c.accepted for c in tailored.changes),
                left_out=sum(c.accepted and not c.tailored for c in tailored.changes),
            )
        )
        texts.append(printed_text(tailored))
    return items, texts


def _job_text(document: TailoredDocument) -> str:
    """The job as tailoring read it (`cv_service._job_text`)."""
    title = document.job_title or document.jd.job_title
    company = document.job_company or document.jd.company or ""
    return f"{title} at {company}\n{document.job_location or ''}\n\n{document.job_description}"


async def compare(
    ws: Workspace, job_id: str, document_ids: list[str], usage_sink: UsageSink | None = None
) -> VersionComparison:
    """Read the chosen tailored CVs for this job and their originals side by side, and save
    the comparison."""
    progress.step("Gathering the CV versions", total=3)
    documents = _chosen(ws, job_id, document_ids)
    names = labels(tailored_documents.list_for_job(ws, job_id))
    items, texts = _entries(ws, documents, names)
    if len(items) > MAX_VERSIONS:
        raise ValueError(
            f"That is {len(items)} CVs with the originals; choose at most {MAX_VERSIONS} in all"
        )
    lead = next((d for d in documents if not d.imported), documents[0])
    requirements = requirements_for(lead.tailored, lead.jd)
    keywords = ats_keywords(lead.jd)
    progress.step(f"Reading {len(items)} CVs side by side for this job")
    llm = ws.structured("cv", usage_sink, "CV version comparison")
    readings = await asyncio.to_thread(
        score_versions, items, texts, requirements, keywords, _job_text(lead), llm
    )
    progress.step("Ranking the versions")
    best_id, summary = best_version(readings, len(requirements))
    comparison = VersionComparison(
        job_id=job_id,
        job_title=lead.job_title or lead.jd.job_title,
        job_company=lead.job_company or lead.jd.company or "",
        created_at=datetime.now(UTC).isoformat(),
        requirements=requirements,
        keywords=keywords,
        readings=readings,
        best_id=best_id,
        summary=summary,
    )
    path = _path(ws, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(comparison.model_dump_json(indent=2))
    return comparison
