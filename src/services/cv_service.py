"""CV actions: import an uploaded CV, tailor it to a job, export .docx."""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

from src.cv import master_cv_manager as mgr
from src.cv.docx_exporter import export_docx
from src.cv.models import MasterCV, TailoredCV
from src.cv.tailor import tailor
from src.jobs.models import JobPosting
from src.services.workspace import Workspace

ACCEPTED = {".pdf", ".docx", ".md", ".txt"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def extract_text(filename: str, data: bytes) -> str:
    """Plain text from an uploaded CV file."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED:
        raise ValueError(f"Unsupported file type {suffix}; use one of {sorted(ACCEPTED)}")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif suffix == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
    else:
        text = data.decode("utf-8", errors="replace")
    if len(text.strip()) < 100:
        raise ValueError("Could not read enough text from the file (scanned PDF?)")
    return text


async def import_cv(ws: Workspace, filename: str, data: bytes, save: bool = True) -> MasterCV:
    """Parse an uploaded CV into a Master CV with the LLM; optionally make it the active CV."""
    if not data:
        raise ValueError("The selected CV file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("CV file is larger than 10 MB")
    text = await asyncio.to_thread(extract_text, filename, data)
    cv = await asyncio.to_thread(mgr.from_text, text, ws.structured("worker"))
    if save:  # summaries are keyed by CV content, so a new CV naturally gets new ones
        ws.save_master_cv(cv)
    return cv


async def tailor_to_job(
    ws: Workspace, job: JobPosting, template: str = "classic"
) -> tuple[TailoredCV, Path]:
    """Tailor the Master CV to `job` and export it to .docx."""
    if ws.master_cv is None:
        raise ValueError("No Master CV loaded; upload a CV first")
    jd = f"{job.title} at {job.company}\n{job.location or ''}\n\n{job.description}"
    tailored = await asyncio.to_thread(tailor, ws.master_cv, jd, ws.structured("worker"))
    ws.tailored[job.id] = tailored
    name = ws.master_cv.basics.name.replace(" ", "_")
    company = re.sub(r"[^A-Za-z0-9]+", "_", job.company).strip("_") or "Company"
    path = export_docx(tailored, ws.output_dir / f"{name}_{company}.docx", template)
    return tailored, path
