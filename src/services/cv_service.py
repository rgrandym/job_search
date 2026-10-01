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

from src.core.llm import UsageSink
from src.cv import master_cv_manager as mgr
from src.cv.docx_exporter import export_docx
from src.cv.models import MasterCV, TailoredCV
from src.cv.tailor import tailor
from src.jobs.models import JobPosting
from src.services.workspace import Workspace

ACCEPTED = {".pdf", ".docx", ".md", ".txt"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ASSET_DIRNAME = "cvs"
PARSED_DIRNAME = ".parsed"
LEGACY_ASSET_ID = re.compile(r"[a-f0-9]{24}")
LEGACY_SOURCE_NAME = re.compile(r"[a-f0-9]{24}\.source\.(pdf|docx|md|txt)", re.IGNORECASE)


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
    _validate_upload(filename, data)
    safe_name = _safe_filename(filename)
    directory = _asset_dir(ws)
    directory.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_uploads(ws)
    asset_id = _content_id(data)
    duplicate = next((item for item in _uploaded_assets(ws) if item.id == asset_id), None)
    if duplicate is not None:
        return select_cv(ws, duplicate.id)
    source = _storage_target(directory, safe_name, data)
    tmp = directory / f".{safe_name}.uploading"
    tmp.write_bytes(data)
    tmp.replace(source)
    return select_cv(ws, asset_id)


def list_cvs(ws: Workspace) -> list[CVAsset]:
    """List locally stored CV sources, plus the legacy Master CV when present."""
    assets: list[CVAsset] = []
    directory = _asset_dir(ws)
    uploaded: list[CVAsset] = []
    if directory.exists():
        _migrate_legacy_uploads(ws)
        uploaded = _uploaded_assets(ws)
    has_master = ws.settings.master_cv_path.exists() or (
        ws.active_cv_id == "master" and ws.master_cv is not None
    )
    if has_master and not uploaded:
        assets.append(
            CVAsset(
                id="master",
                filename="Master CV",
                size=(
                    ws.settings.master_cv_path.stat().st_size
                    if ws.settings.master_cv_path.exists()
                    else 0
                ),
                kind="master",
                parsed=True,
            )
        )
    assets.extend(uploaded)
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
    available_ids = {item.id for item in assets}
    if ws.active_cv_id not in available_ids:
        default = uploaded[0] if len(uploaded) == 1 else assets[0] if len(assets) == 1 else None
        ws.active_cv_id = default.id if default else None
        if ws.active_cv_id == "master":
            ws.master_cv = mgr.load(ws.settings.master_cv_path)
        else:
            ws.master_cv = _parsed_cv(ws, ws.active_cv_id) if ws.active_cv_id else None
    return [item.model_copy(update={"selected": item.id == ws.active_cv_id}) for item in assets]


def select_cv(ws: Workspace, asset_id: str) -> CVAsset:
    """Select a stored CV source; parsing remains lazy."""
    asset = next((item for item in list_cvs(ws) if item.id == asset_id), None)
    if asset is None:
        raise ValueError("CV file was not found")
    cv = mgr.load(ws.settings.master_cv_path) if asset.id == "master" else _parsed_cv(ws, asset.id)
    _select(ws, asset.id, cv)
    return asset.model_copy(update={"selected": True})


async def ensure_selected_cv(ws: Workspace, usage_sink: UsageSink | None = None) -> MasterCV:
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
    cv = await import_cv(
        ws,
        asset.filename,
        await asyncio.to_thread(source.read_bytes),
        save=False,
        usage_sink=usage_sink,
    )
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


async def import_cv(
    ws: Workspace,
    filename: str,
    data: bytes,
    save: bool = True,
    usage_sink: UsageSink | None = None,
) -> MasterCV:
    """Parse an uploaded CV into a Master CV with the LLM; optionally make it the active CV."""
    if not data:
        raise ValueError("The selected CV file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("CV file is larger than 10 MB")
    text = await asyncio.to_thread(extract_text, filename, data)
    cv = await asyncio.to_thread(
        mgr.from_text,
        text,
        ws.structured("worker", usage_sink, "CV parsing"),
    )
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


def _content_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:24]


def _source_files(ws: Workspace) -> list[Path]:
    directory = _asset_dir(ws)
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in ACCEPTED
    )


def _uploaded_assets(ws: Workspace) -> list[CVAsset]:
    unique: dict[str, Path] = {}
    for path in _source_files(ws):
        asset_id = _content_id(path.read_bytes())
        current = unique.get(asset_id)
        if current is None:
            unique[asset_id] = path
            continue
        keep, discard = sorted((current, path), key=lambda item: _filename_preference(item.name))
        unique[asset_id] = keep
        discard.unlink()
    return [
        CVAsset(
            id=asset_id,
            filename=path.name,
            size=path.stat().st_size,
            kind="uploaded",
            parsed=_parsed_path(ws, asset_id).exists(),
        )
        for asset_id, path in sorted(unique.items(), key=lambda item: item[1].name.lower())
    ]


def _filename_preference(filename: str) -> tuple[bool, str]:
    return (bool(LEGACY_SOURCE_NAME.fullmatch(filename)), filename.lower())


def _legacy_entries(directory: Path) -> list[tuple[Path, Path, dict[str, object]]]:
    entries: list[tuple[Path, Path, dict[str, object]]] = []
    for metadata_path in sorted(directory.glob("*.json")):
        if not LEGACY_ASSET_ID.fullmatch(metadata_path.stem):
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            sources = list(directory.glob(f"{metadata_path.stem}.source.*"))
            filename = metadata.get("filename")
            if len(sources) == 1 and isinstance(filename, str):
                entries.append((metadata_path, sources[0], metadata))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return entries


def _safe_filename(filename: str) -> str:
    safe_name = Path(filename).name.lstrip(".")
    return safe_name or f"CV{Path(filename).suffix.lower()}"


def _storage_target(directory: Path, filename: str, data: bytes) -> Path:
    safe_name = _safe_filename(filename)
    if LEGACY_SOURCE_NAME.fullmatch(safe_name):
        safe_name = f"CV{Path(safe_name).suffix.lower()}"
    target = directory / safe_name
    counter = 2
    while target.exists() and target.read_bytes() != data:
        target = directory / f"{Path(safe_name).stem} ({counter}){Path(safe_name).suffix}"
        counter += 1
    return target


def _move_legacy_parse(ws: Workspace, old_ids: list[str], asset_id: str) -> None:
    destination = _parsed_path(ws, asset_id)
    for old_id in old_ids:
        cache_id = hashlib.sha256(old_id.encode()).hexdigest()[:24]
        old_path = _asset_dir(ws) / f"{cache_id}.cv.json"
        if old_path.exists() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            old_path.replace(destination)
        elif old_path.exists():
            old_path.unlink()


def _migrate_legacy_uploads(ws: Workspace) -> None:
    directory = _asset_dir(ws)
    entries = _legacy_entries(directory)
    grouped: dict[str, list[tuple[Path, Path, dict[str, object]]]] = {}
    for entry in entries:
        grouped.setdefault(_content_id(entry[1].read_bytes()), []).append(entry)
    for asset_id, duplicates in grouped.items():
        chosen = min(
            duplicates,
            key=lambda entry: _filename_preference(str(entry[2]["filename"])),
        )
        data = chosen[1].read_bytes()
        target = _storage_target(directory, str(chosen[2]["filename"]), data)
        if not target.exists():
            chosen[1].replace(target)
        old_ids = [entry[0].stem for entry in duplicates]
        _move_legacy_parse(ws, old_ids, asset_id)
        if ws.active_cv_id in old_ids:
            ws.active_cv_id = asset_id
        for metadata_path, source, _metadata in duplicates:
            metadata_path.unlink(missing_ok=True)
            if source != target:
                source.unlink(missing_ok=True)


def _asset(ws: Workspace, asset_id: str) -> CVAsset:
    if not LEGACY_ASSET_ID.fullmatch(asset_id):
        raise ValueError("Invalid CV identifier")
    asset = next((item for item in _uploaded_assets(ws) if item.id == asset_id), None)
    if asset is None:
        raise ValueError("CV file was not found")
    return asset


def _source_path(ws: Workspace, asset_id: str) -> Path:
    if asset_id.startswith("generated:"):
        name = asset_id.removeprefix("generated:")
        if Path(name).name != name:
            raise ValueError("Invalid CV identifier")
        path = (ws.output_dir / name).resolve()
        if path.parent != ws.output_dir.resolve() or not path.is_file():
            raise ValueError("CV source file is unavailable")
        return path
    asset = _asset(ws, asset_id)
    path = _asset_dir(ws) / asset.filename
    if not path.is_file():
        raise ValueError("CV source file is unavailable")
    return path


def _parsed_path(ws: Workspace, asset_id: str) -> Path:
    cache_id = (
        asset_id
        if LEGACY_ASSET_ID.fullmatch(asset_id)
        else hashlib.sha256(asset_id.encode()).hexdigest()[:24]
    )
    return _asset_dir(ws) / PARSED_DIRNAME / f"{cache_id}.json"


def _parsed_cv(ws: Workspace, asset_id: str) -> MasterCV | None:
    path = _parsed_path(ws, asset_id)
    return mgr.load(path) if path.exists() else None


def _select(ws: Workspace, asset_id: str, cv: MasterCV | None) -> None:
    ws.active_cv_id = asset_id
    ws.master_cv = cv
    ws.last_report = None


async def tailor_to_job(
    ws: Workspace,
    job: JobPosting,
    template: str = "classic",
    usage_sink: UsageSink | None = None,
) -> tuple[TailoredCV, Path]:
    """Tailor the Master CV to `job` and export it to .docx."""
    master_cv = await ensure_selected_cv(ws, usage_sink)
    jd = f"{job.title} at {job.company}\n{job.location or ''}\n\n{job.description}"
    tailored = await asyncio.to_thread(
        tailor,
        master_cv,
        jd,
        ws.structured("worker", usage_sink, "CV tailoring"),
    )
    ws.tailored[job.id] = tailored
    name = master_cv.basics.name.replace(" ", "_")
    company = re.sub(r"[^A-Za-z0-9]+", "_", job.company).strip("_") or "Company"
    path = export_docx(tailored, ws.output_dir / f"{name}_{company}.docx", template)
    return tailored, path
