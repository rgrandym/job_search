"""CV actions: import an uploaded CV, tailor it to a job, export .docx."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.cv import master_cv_manager as mgr
from src.cv.docx_exporter import export_docx
from src.cv.models import MasterCV, TailoredCV
from src.cv.tailor import tailor
from src.jobs.models import JobPosting
from src.services.workspace import Workspace

ACCEPTED = {".pdf", ".docx", ".md", ".txt"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ASSET_DIRNAME = "cvs"


class CVAsset(BaseModel):
    """A locally stored CV source available for selection."""

    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    size: int
    kind: Literal["master", "uploaded", "generated"] = "uploaded"
    parsed: bool = False
    selected: bool = False


def store_cv(ws: Workspace, filename: str, data: bytes) -> CVAsset:
    """Validate and store an uploaded source immediately, without waiting for the LLM."""
    suffix = _validate_upload(filename, data)
    safe_name = Path(filename).name
    asset_id = hashlib.sha256(safe_name.encode() + b"\0" + data).hexdigest()[:24]
    directory = _asset_dir(ws)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / f"{asset_id}.source{suffix}"
    tmp = source.with_suffix(source.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(source)
    metadata = {
        "id": asset_id,
        "filename": safe_name,
        "size": len(data),
        "kind": "uploaded",
    }
    metadata_path = directory / f"{asset_id}.json"
    metadata_tmp = metadata_path.with_suffix(".json.tmp")
    metadata_tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    metadata_tmp.replace(metadata_path)
    return select_cv(ws, asset_id)


def list_cvs(ws: Workspace) -> list[CVAsset]:
    """List locally stored CV sources, plus the legacy Master CV when present."""
    assets: list[CVAsset] = []
    if ws.settings.master_cv_path.exists():
        assets.append(
            CVAsset(
                id="master",
                filename="Master CV",
                size=ws.settings.master_cv_path.stat().st_size,
                kind="master",
                parsed=True,
            )
        )
    directory = _asset_dir(ws)
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            try:
                asset_id = path.stem
                assets.append(_asset(ws, asset_id))
            except (OSError, ValueError):
                continue
    if ws.output_dir.exists():
        assets.extend(
            CVAsset(
                id=f"generated:{path.name}",
                filename=path.name,
                size=path.stat().st_size,
                kind="generated",
                parsed=_parsed_path(ws, f"generated:{path.name}").exists(),
            )
            for path in sorted(ws.output_dir.glob("*.docx"))
            if path.is_file()
        )
    return [item.model_copy(update={"selected": item.id == ws.active_cv_id}) for item in assets]


def select_cv(ws: Workspace, asset_id: str) -> CVAsset:
    """Select a stored CV source; parsing remains lazy."""
    asset = next((item for item in list_cvs(ws) if item.id == asset_id), None)
    if asset is None:
        raise ValueError("CV file was not found")
    cv = mgr.load(ws.settings.master_cv_path) if asset.id == "master" else _parsed_cv(ws, asset.id)
    _select(ws, asset.id, cv)
    return asset.model_copy(update={"selected": True})


async def ensure_selected_cv(ws: Workspace) -> MasterCV:
    """Return the selected parsed CV, parsing its source once when necessary."""
    asset_id = ws.active_cv_id
    if asset_id is None:
        raise ValueError("Upload or select a CV first")
    if asset_id == "master":
        if ws.master_cv is None:
            raise ValueError("The selected Master CV is unavailable")
        return ws.master_cv
    parsed = _parsed_cv(ws, asset_id)
    if parsed is not None:
        ws.master_cv = parsed
        return parsed
    if not ws.llm_ready():
        raise ValueError("Connect an LLM in Settings to build a profile from this CV")
    asset = next((item for item in list_cvs(ws) if item.id == asset_id), None)
    if asset is None:
        raise ValueError("The selected CV is unavailable")
    source = _source_path(ws, asset_id)
    cv = await import_cv(ws, asset.filename, await asyncio.to_thread(source.read_bytes), save=False)
    mgr.save(cv, _parsed_path(ws, asset_id))
    ws.master_cv = cv
    return cv


def save_selected_cv(ws: Workspace, cv: MasterCV) -> None:
    """Persist structured edits against the currently selected CV."""
    if ws.active_cv_id is None or ws.active_cv_id == "master":
        ws.save_master_cv(cv)
        return
    mgr.save(cv, _parsed_path(ws, ws.active_cv_id))
    ws.master_cv = cv


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
        save_selected_cv(ws, cv)
    return cv


def _validate_upload(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED:
        raise ValueError(f"Unsupported file type {suffix}; use one of {sorted(ACCEPTED)}")
    if not data:
        raise ValueError("The selected CV file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("CV file is larger than 10 MB")
    return suffix


def _asset_dir(ws: Workspace) -> Path:
    return ws.settings.data_dir / ASSET_DIRNAME


def _asset(ws: Workspace, asset_id: str) -> CVAsset:
    if not re.fullmatch(r"[a-f0-9]{24}", asset_id):
        raise ValueError("Invalid CV identifier")
    path = _asset_dir(ws) / f"{asset_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return CVAsset.model_validate(data | {"parsed": _parsed_path(ws, asset_id).exists()})


def _source_path(ws: Workspace, asset_id: str) -> Path:
    if asset_id.startswith("generated:"):
        name = asset_id.removeprefix("generated:")
        if Path(name).name != name:
            raise ValueError("Invalid CV identifier")
        path = (ws.output_dir / name).resolve()
        if path.parent != ws.output_dir.resolve() or not path.is_file():
            raise ValueError("CV source file is unavailable")
        return path
    matches = list(_asset_dir(ws).glob(f"{asset_id}.source.*"))
    if len(matches) != 1:
        raise ValueError("CV source file is unavailable")
    return matches[0]


def _parsed_path(ws: Workspace, asset_id: str) -> Path:
    cache_id = hashlib.sha256(asset_id.encode()).hexdigest()[:24]
    return _asset_dir(ws) / f"{cache_id}.cv.json"


def _parsed_cv(ws: Workspace, asset_id: str) -> MasterCV | None:
    path = _parsed_path(ws, asset_id)
    return mgr.load(path) if path.exists() else None


def _select(ws: Workspace, asset_id: str, cv: MasterCV | None) -> None:
    ws.active_cv_id = asset_id
    ws.master_cv = cv
    ws.last_report = None


async def tailor_to_job(
    ws: Workspace, job: JobPosting, template: str = "classic"
) -> tuple[TailoredCV, Path]:
    """Tailor the Master CV to `job` and export it to .docx."""
    master_cv = await ensure_selected_cv(ws)
    jd = f"{job.title} at {job.company}\n{job.location or ''}\n\n{job.description}"
    tailored = await asyncio.to_thread(tailor, master_cv, jd, ws.structured("worker"))
    ws.tailored[job.id] = tailored
    name = master_cv.basics.name.replace(" ", "_")
    company = re.sub(r"[^A-Za-z0-9]+", "_", job.company).strip("_") or "Company"
    path = export_docx(tailored, ws.output_dir / f"{name}_{company}.docx", template)
    return tailored, path
