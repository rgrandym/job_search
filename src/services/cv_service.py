"""CV actions: import an uploaded CV, tailor it to a job, write a cover letter, export .docx."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.core import progress
from src.core.llm import Role, UsageSink
from src.core.llm_provider import LLMProvider
from src.cv import master_cv_manager as mgr
from src.cv.ats import check_docx
from src.cv.cover_letter import letter_context, letter_gaps, write_letter
from src.cv.docx_exporter import export_cover_letter, export_docx
from src.cv.docx_original import align_to_document, write_like_original
from src.cv.fit_check import compare_fit
from src.cv.models import (
    CoverLetter,
    JDAnalysis,
    MasterCV,
    OriginalReading,
    SearchFitBaseline,
    TailoredCV,
    TailoredDocument,
    TailoringPlan,
    TailoringSettings,
)
from src.cv.tailor import (
    Audience,
    LengthChoice,
    LevelEmphasis,
    TailoringEmphasis,
    analyze_jd,
    apply_plan,
    cv_to_text,
    literal_terms,
    tailor,
)
from src.jobs.models import JobPosting, JobVerdict, MatchResult
from src.jobs.profile_memory import profile_brief
from src.services import declined, document_files, tailored_documents, tracker
from src.services.intent import delete_intent, get_intent
from src.services.workspace import Workspace

ACCEPTED = {".pdf", ".docx", ".md", ".txt"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ASSET_DIRNAME = "cvs"
PARSED_DIRNAME = ".parsed"
PREVIEW_DIRNAME = ".preview"
SAME_CV_TEXT = 0.6  # word overlap at which a Word file is the same CV as a PDF
IDS_FILENAME = "cv_ids.json"  # in data/: uploaded file name -> its permanent asset id
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
    duplicate = next(
        (
            item
            for item in _uploaded_assets(ws)
            if asset_id in (item.id, _content_id((directory / item.filename).read_bytes()))
        ),
        None,
    )
    if duplicate is None:
        source = _storage_target(directory, safe_name, data)
        tmp = directory / f".{safe_name}.uploading"
        tmp.write_bytes(data)
        tmp.replace(source)
        document_files.save_upload_copy(ws, safe_name, data)
    return select_cv(ws, duplicate.id if duplicate else asset_id)


def store_parsed_cv(ws: Workspace, filename: str, data: bytes, cv: MasterCV) -> CVAsset:
    """Store and select a Word CV whose facts are already known (`cv`), so it needs no parse."""
    mgr.save(cv, _parsed_path(ws, _content_id(data)))
    return store_cv(ws, filename, data)


def list_cvs(ws: Workspace) -> list[CVAsset]:
    """List locally stored CV sources, plus the legacy Master CV when present."""
    document_files.organize_legacy_output(ws)
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
    _sync_copies(ws, uploaded)
    if ws.output_dir.exists():
        # An upload's copy in output/cvs/ (same name, or same content) is the same CV.
        names = {item.filename for item in uploaded}
        contents = {_content_id((_asset_dir(ws) / item.filename).read_bytes()) for item in uploaded}
        generated = [
            path
            for path in ws.output_dir.glob("*.docx")
            if _content_id(path.read_bytes()) not in contents
        ] + [
            path
            for path in (ws.output_dir / "cvs").glob("*.docx")
            if path.name not in names and _content_id(path.read_bytes()) not in contents
        ]
        assets.extend(
            CVAsset(
                id=f"generated:{path.name}",
                filename=path.name,
                size=path.stat().st_size,
                kind="generated",
                parsed=_parsed_path(ws, f"generated:{path.name}").exists(),
            )
            for path in sorted(generated)
            if path.is_file() and "cover_letter" not in path.stem.lower()
        )
    available_ids = {item.id for item in assets}
    if ws.active_cv_id not in available_ids:
        replaced = None
        if ws.active_cv_id and ws.active_cv_id.startswith("generated:"):
            old_name = Path(ws.active_cv_id.removeprefix("generated:")).name
            if old_name.lower().endswith(".docx"):
                repaired_name = f"{Path(old_name).stem}_format_repaired.docx"
                replaced = next((item for item in assets if item.filename == repaired_name), None)
        default = replaced or (
            uploaded[0] if len(uploaded) == 1 else assets[0] if len(assets) == 1 else None
        )
        ws.active_cv_id = default.id if default else None
        ws.remember_cv()
        if ws.active_cv_id == "master":
            ws.master_cv = mgr.load(ws.settings.master_cv_path)
        else:
            ws.master_cv = _parsed_cv(ws, ws.active_cv_id) if ws.active_cv_id else None
    elif ws.master_cv is None and ws.active_cv_id not in (None, "master"):
        ws.master_cv = _parsed_cv(ws, ws.active_cv_id)  # selection restored after a restart
    return [item.model_copy(update={"selected": item.id == ws.active_cv_id}) for item in assets]


def cv_name(ws: Workspace, asset_id: str) -> str | None:
    """File name of a stored CV, without touching the selection or the library."""
    if asset_id == "master":
        return "Master CV"
    if asset_id.startswith("generated:"):
        return Path(asset_id.removeprefix("generated:")).name
    if _asset_dir(ws).is_dir():
        for path in _source_files(ws):
            if _content_id(path.read_bytes()) == asset_id:
                return path.name
    return None


def select_cv(ws: Workspace, asset_id: str) -> CVAsset:
    """Select a stored CV source; parsing remains lazy."""
    asset = next((item for item in list_cvs(ws) if item.id == asset_id), None)
    if asset is None:
        raise ValueError("CV file was not found")
    cv = mgr.load(ws.settings.master_cv_path) if asset.id == "master" else _parsed_cv(ws, asset.id)
    _select(ws, asset.id, cv)
    return asset.model_copy(update={"selected": True})


def delete_cv(ws: Workspace, asset_id: str) -> None:
    """Remove a library CV, its parse cache, and profiles owned by that CV."""
    asset = next((item for item in list_cvs(ws) if item.id == asset_id), None)
    if asset is None:
        raise ValueError("CV file was not found")
    fallback = _replacement(ws, asset) if ws.active_cv_id == asset_id else None
    if asset.kind == "master":
        path = ws.settings.master_cv_path
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".bak").unlink(missing_ok=True)
    else:
        path = _source_path(ws, asset_id)
        if asset.kind == "uploaded":
            document_files.remove_upload_copies(ws, path.read_bytes())
        path.unlink()
        _parsed_path(ws, asset_id).unlink(missing_ok=True)
        if asset.kind == "generated":
            tailored_documents.delete_for_file(ws, path.name)
    for record in ws.memory.records(asset_id):
        ws.memory.delete(record.key)
    delete_intent(ws, asset_id)
    if ws.active_cv_id == asset_id:
        ws.active_cv_id = None
        ws.remember_cv()
        ws.master_cv = None
        ws.last_report = None
        if fallback in {item.id for item in list_cvs(ws)}:
            select_cv(ws, fallback)


def _replacement(ws: Workspace, asset: CVAsset) -> str | None:
    """The CV to select once the selected `asset` is deleted, so tailoring stays available:
    a tailored draft's source CV, else the first other CV the user added."""
    if asset.kind == "generated":
        drafts = tailored_documents.list_all(ws)
        source = next((d.cv_id for d in drafts if d.filename == asset.filename), None)
        if source is not None:
            return source
    others = [item for item in list_cvs(ws) if item.id != asset.id]
    return next((item.id for item in others if item.kind != "generated"), None)


def cached_selected_cv(ws: Workspace) -> MasterCV | None:
    """The selected CV if it is already parsed; never calls the LLM."""
    if ws.active_cv_id is None:
        return None
    if ws.active_cv_id == "master":
        return ws.master_cv
    return _parsed_cv(ws, ws.active_cv_id)


async def ensure_selected_cv(
    ws: Workspace,
    usage_sink: UsageSink | None = None,
    role: Role = "quality",
) -> MasterCV:
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
    if not ws.role_ready(role):
        raise ValueError(f"Connect the {role} model in Settings to read this CV")
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
        role=role,
    )
    mgr.save(cv, _parsed_path(ws, asset_id))
    ws.master_cv = cv
    return cv


def source_file(ws: Workspace, asset_id: str) -> Path:
    """The CV's own file: the upload (kept identical to its output/cvs/ copy) or a generated
    document."""
    if asset_id == "master":
        raise ValueError("The Master CV is structured data, not a document")
    return _source_path(ws, asset_id)


def working_copy(ws: Workspace, asset_id: str) -> Path:
    """The file desktop apps open: the CV's copy in output/cvs/. Saves made there are copied
    back to the upload (`sync_copies`), so the two stay one CV."""
    source = source_file(ws, asset_id)
    directory = ws.output_dir / "cvs"
    if source.parent.resolve() == directory.resolve():
        return source
    existing = directory / source.name
    if existing.is_file():
        return existing
    return document_files.save_upload_copy(ws, source.name, source.read_bytes())


def source_text(ws: Workspace) -> str | None:
    """Full text of the selected CV's original document (None for the structured Master CV or
    an unreadable file), so profiles read everything, not only what the parser extracted."""
    if ws.active_cv_id is None or ws.active_cv_id == "master":
        return None
    try:
        path = source_file(ws, ws.active_cv_id)
        return extract_text(path.name, path.read_bytes())
    except (OSError, ValueError):
        return None


def preview_file(ws: Workspace, asset_id: str) -> Path:
    """A browser-viewable rendering of a CV's working copy (the stored file plus the user's and
    the assistant's edits): the file itself, or an HTML page view of a .docx."""
    sync_copies(ws)
    source = working_copy(ws, asset_id)
    if source.suffix.lower() != ".docx":
        return source
    preview = document_files.html_preview(source, _asset_dir(ws) / PREVIEW_DIRNAME)
    if preview is None:
        raise ValueError("This Word file cannot be shown here; use Download or Open in Word")
    return preview


def preview_revision(ws: Workspace, asset_id: str) -> str:
    """Change key for the actual output file shown by the CV viewer."""
    sync_copies(ws)
    path = working_copy(ws, asset_id)
    stat = path.stat()
    return f"{stat.st_mtime_ns}-{stat.st_size}"


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

        text = _docx_text(Document(io.BytesIO(data)))
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
    role: Role = "quality",
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
        ws.structured(role, usage_sink, "CV parsing"),
    )
    if save:  # summaries are keyed by CV content, so a new CV naturally gets new ones
        save_selected_cv(ws, cv)
    return cv


def _docx_text(doc: Any) -> str:
    """Paragraphs and table rows in document order (CV layouts often sit in tables)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            lines.append(Paragraph(child, doc).text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _validate_upload(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED:
        raise ValueError(f"Unsupported file type {suffix}; use one of {sorted(ACCEPTED)}")
    if not data:
        raise ValueError("The selected CV file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("CV file is larger than 10 MB")
    return suffix


def _copy_file(source: Path, target: Path) -> None:
    """Copy `source` over `target` atomically, keeping its modification time."""
    tmp = target.with_name(f".{target.name}.syncing")
    shutil.copy2(source, tmp)
    tmp.replace(target)


def _sync_copies(ws: Workspace, uploaded: list[CVAsset]) -> None:
    """Keep each upload and its copy in output/cvs/ identical: one CV. The file changed last
    (in Word, or by the assistant) is copied over the other."""
    for item in uploaded:
        source = _asset_dir(ws) / item.filename
        copy = ws.output_dir / "cvs" / item.filename
        if not copy.is_file() or not source.is_file():
            continue
        if (
            copy.stat().st_size == source.stat().st_size
            and copy.read_bytes() == source.read_bytes()
        ):
            continue
        if copy.stat().st_mtime > source.stat().st_mtime:
            _copy_file(copy, source)
        else:
            _copy_file(source, copy)


def sync_copies(ws: Workspace) -> None:
    """Bring every upload and its output/cvs/ copy level (before reading or editing either)."""
    if _asset_dir(ws).is_dir():
        _sync_copies(ws, _uploaded_assets(ws))


def mirror_copy(ws: Workspace, source: Path) -> None:
    """After the app edits an uploaded CV, give its copy in output/cvs/ the same content."""
    copy = ws.output_dir / "cvs" / source.name
    if source.resolve() != copy.resolve() and source.parent.resolve() == _asset_dir(ws).resolve():
        copy.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(source, copy)


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


def _id_registry(ws: Workspace) -> dict[str, str]:
    try:
        data = json.loads((ws.settings.data_dir / IDS_FILENAME).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_id_registry(ws: Workspace, ids: dict[str, str]) -> None:
    path = ws.settings.data_dir / IDS_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ids, indent=1, sort_keys=True))
    tmp.replace(path)


def _uploaded_assets(ws: Workspace) -> list[CVAsset]:
    """Uploaded CVs. Each keeps the id it was first seen with (its content hash then), so
    edits to the file never orphan its parse, profiles, intent or documents."""
    ids = _id_registry(ws)
    files = _source_files(ws)
    known = dict(ids)
    unique: dict[str, Path] = {}
    for path in files:
        asset_id = ids.setdefault(path.name, _content_id(path.read_bytes()))
        current = unique.get(asset_id)
        if current is None:
            unique[asset_id] = path
            continue
        keep, discard = sorted((current, path), key=lambda item: _filename_preference(item.name))
        unique[asset_id] = keep
        discard.unlink()
    ids = {path.name: asset_id for asset_id, path in unique.items()}
    if ids != known:
        _save_id_registry(ws, ids)
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
            ws.remember_cv()
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
        relative = Path(name)
        if relative.is_absolute() or len(relative.parts) not in (1, 2):
            raise ValueError("Invalid CV identifier")
        if len(relative.parts) == 2 and relative.parts[0] != "cvs":
            raise ValueError("Invalid CV identifier")
        path = (ws.output_dir / relative).resolve()
        if len(relative.parts) == 1 and not path.is_file():
            path = (ws.output_dir / "cvs" / relative).resolve()
        if (
            path.parent not in (ws.output_dir.resolve(), (ws.output_dir / "cvs").resolve())
            or not path.is_file()
            or "cover_letter" in path.stem.lower()
        ):
            raise ValueError("CV source file is unavailable")
        if path.parent == (ws.output_dir / "cvs").resolve():
            tailored_documents.ensure_original_layout(ws, path)
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


@contextmanager
def working_on(ws: Workspace, cv_id: str) -> Iterator[MasterCV]:
    """Act on one library CV (its structured copy and Word file) as if selected, without
    changing the user's selection: a draft's facts go back to the CV it came from."""
    if cv_id == "master":
        path = ws.settings.master_cv_path
        cv = mgr.load(path) if path.exists() else None
    else:
        cv = _parsed_cv(ws, cv_id) if any(a.id == cv_id for a in list_cvs(ws)) else None
    if cv is None:
        raise ValueError("The CV this draft came from is no longer in the CV library")
    selected, selected_cv = ws.active_cv_id, ws.master_cv
    ws.active_cv_id, ws.master_cv = cv_id, cv
    try:
        yield cv
    finally:
        if selected != cv_id:  # another CV is selected: put it back as it was
            ws.active_cv_id, ws.master_cv = selected, selected_cv
        else:  # this CV is the selected one: keep its updated copy
            ws.active_cv_id = selected


def _select(ws: Workspace, asset_id: str, cv: MasterCV | None) -> None:
    ws.active_cv_id = asset_id
    ws.remember_cv()
    ws.master_cv = cv
    ws.last_report = None


async def tailor_to_job(
    ws: Workspace,
    job: JobPosting,
    template: str = "original",
    usage_sink: UsageSink | None = None,
    result: MatchResult | None = None,
    emphasis: TailoringEmphasis = "auto",
    level: LevelEmphasis = "auto",
    length: LengthChoice = "auto",
    audience: Audience = "auto",
    mode: Literal["standard", "optimised"] = "standard",
) -> tuple[TailoredCV, Path]:
    """Tailor the Master CV to `job` (steered by the job_matcher's verdict on it and the
    stored profile, then reviewed and revised), export it to .docx, check what an ATS reads
    from the file and how much better it shows the fit than the original CV.
    `template` "original" (the default) writes it in the CV's own Word design."""
    progress.step("Reading your CV", total=10)
    master_cv = await ensure_selected_cv(ws, usage_sink, role="cv")
    original = original_docx(ws)
    if original is not None:  # tailor from every line as written in the Word file
        master_cv = await asyncio.to_thread(align_to_document, original, master_cv)
    llm = ws.structured("cv", usage_sink, "CV tailoring")
    text = _job_text(job)
    progress.step("Analysing the job description")
    jd = await _analysis(ws, text, llm)
    result = result if result is not None else ws.result(job.id)
    guidance = matcher_guidance(result.verdict if result is not None else None)
    profile = stored_profile(ws, result.family if result is not None else None)
    search_fit = search_fit_baseline(result, ws.active_cv_id)

    grader = ws.structured(GRADER, usage_sink, "CV reading (independent grader)")
    reader = grader_id(ws)
    kept = load_baseline(ws, job.id)  # one reading of the original per job and grader
    held = {job.id: kept if kept is not None and kept.reader == reader else None}

    def check(draft: TailoredCV) -> TailoredCV:  # the independent grader's reading
        out = _fit_check(master_cv, draft, jd, text, grader, search_fit, held[job.id])
        baseline = out.fit_check.baseline if out.fit_check else None
        if baseline is not None and baseline != held[job.id]:
            baseline = baseline.model_copy(update={"reader": reader})
            held[job.id] = baseline
            save_baseline(ws, job.id, baseline)
        return out

    grounds: dict[str, Any] = {}  # optimised only: the user's other CVs, declined proposals
    if mode == "optimised":
        grounds = {
            "background": library_background(ws, ws.active_cv_id),
            "declined": declined.texts(ws, ws.active_cv_id),
        }
    tailored = await asyncio.to_thread(
        tailor,
        master_cv,
        text,
        llm,
        guidance,
        True,
        jd,
        emphasis,
        level,
        length,
        profile,
        audience,
        mode == "standard",
        mode,
        check,
        **grounds,
    )
    progress.step("Writing the Word file in your CV's design")
    path = _new_output_path(ws, _file_stem(master_cv, job))
    tailored, template = _write_tailored(tailored, master_cv, original, path, template)
    progress.step("Checking what an ATS reads from the file")
    tailored = _with_ats(ws, tailored, master_cv, original, path)
    chosen = TailoringSettings(
        template=template,
        emphasis=emphasis,
        level=level,
        length=length,
        audience=audience,
        mode=mode,
    )
    document = tailored_documents.create(
        ws,
        job.id,
        master_cv,
        jd,
        tailored,
        template,
        path,
        posting=job,
        original_file=original,
        settings=chosen,
        search_fit=search_fit,
        reviewed=not any(a.status == "pending" for a in tailored.review_additions),
    )
    tailored = document.tailored
    ws.tailored[job.id] = tailored
    tracker.remember_document_job(ws, job, cv_file=path.name)
    return tailored, path


MAX_BACKGROUND = 15_000  # characters of the user's other CVs given to the optimiser


# Who grades a CV is not who wrote it (user's decision 2026-10-15): the screening model, the
# same judge whose verdict at search time anchors the scores, reads every CV for a job.
GRADER: Role = "screening"


def grader_id(ws: Workspace) -> str:
    """The grader model (provider:model:effort): another model reads afresh."""
    llm = ws.llm
    return f"{llm.provider_for(GRADER)}:{llm.model_for(GRADER)}:{llm.effort_for(GRADER)}"


def library_background(ws: Workspace, cv_id: str | None) -> str:
    """The user's other CVs already parsed in the library (never parses one): grounds the
    optimiser may quote when inferring what a role involved. Not facts for the tailored CV."""
    parts: list[str] = []
    for asset in list_cvs(ws):
        if asset.id == cv_id or not asset.parsed:
            continue
        cv = _parsed_cv(ws, asset.id)
        if cv is not None:
            parts.append(f"--- {asset.filename} ---\n{cv_to_text(cv, tags=False)}")
    return "\n\n".join(parts)[:MAX_BACKGROUND]


def search_fit_baseline(result: MatchResult | None, cv_id: str | None) -> SearchFitBaseline | None:
    """The search verdict a tailored CV's scores are anchored to: one with ratings, judged
    with the CV `cv_id` (a verdict from before verdicts named their CV is taken as it)."""
    verdict = result.verdict if result is not None else None
    if verdict is None or verdict.ratings is None:
        return None
    if verdict.cv_id is not None and verdict.cv_id != cv_id:
        return None  # judged another CV (or none): its score is not this CV's
    return SearchFitBaseline(
        score=verdict.fit_score,
        ratings=verdict.ratings.model_dump(),
        cap_reason=verdict.cap_reason,
        requirements_checked=verdict.requirements_checked,
    )


def _with_ats(
    ws: Workspace, tailored: TailoredCV, master: MasterCV, original: Path | None, path: Path
) -> TailoredCV:
    """What an ATS reads from the written file, and from the CV as it was before tailoring."""
    keywords = tailored.matched_keywords + tailored.missing_keywords
    return tailored.model_copy(
        update={
            "ats": check_docx(path, tailored.cv, keywords),
            "source_ats_keyword_coverage": _source_coverage(ws, master, original, keywords),
        }
    )


def _fit_check(
    source: MasterCV,
    tailored: TailoredCV,
    jd: JDAnalysis,
    text: str,
    llm: LLMProvider,
    search_fit: SearchFitBaseline | None = None,
    original: OriginalReading | None = None,
) -> TailoredCV:
    """The before/after reading; a failed check never costs the user the tailored CV."""
    try:
        fit = compare_fit(source, tailored, jd, text, llm, search_fit, original)
        return tailored.model_copy(update={"fit_check": fit})
    except Exception as exc:  # noqa: BLE001 - the CV is written whatever the check's fate
        note = f"The before/after fit check could not run: {exc}"
        return tailored.model_copy(update={"document_notes": [*tailored.document_notes, note]})


def _baselines_path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "fit_baselines.json"


def load_baseline(ws: Workspace, job_id: str) -> OriginalReading | None:
    """The original CV's reading kept for this job, if any (`fit_check.compare_fit` checks
    that it is still the same CV text)."""
    path = _baselines_path(ws)
    try:
        raw = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        return OriginalReading.model_validate(raw[job_id]) if job_id in raw else None
    except (ValueError, TypeError):
        return None  # an unreadable file only means the original is read again


def save_baseline(ws: Workspace, job_id: str, baseline: OriginalReading) -> None:
    path = _baselines_path(ws)
    try:
        raw = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    except ValueError:
        raw = {}
    raw[job_id] = baseline.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(raw), "utf-8")
    temporary.replace(path)


def stored_profile(ws: Workspace, family: str | None = None) -> str:
    """The selected CV's stored profile as tailoring reads it ("" when none is stored; never
    builds one). The general profile is preferred, else the most recently updated."""
    owner = ws.active_cv_id
    records = ws.memory.records(owner) if owner else []
    if not records:
        return ""
    general = [r for r in records if r.role_family == "any"]
    record = (general or sorted(records, key=lambda r: r.updated_at or r.created_at))[-1]
    return profile_brief(record.summary, family)


def original_docx(ws: Workspace) -> Path | None:
    """The selected CV's Word design: its own file when it is a Word CV, else the Word version
    of the same CV in the library (a PDF is normally exported from it). For the structured
    Master CV, the library Word CV it was read from (`_master_docx`)."""
    asset_id = ws.active_cv_id
    if not asset_id:
        return None
    sync_copies(ws)
    if asset_id == "master":
        return _master_docx(ws)
    try:
        path = source_file(ws, asset_id)
    except ValueError:
        return None
    return path if path.suffix.lower() == ".docx" else _matching_docx(ws, path)


def _word_set(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if len(w) > 2}


def _file_words(path: Path) -> set[str]:
    try:
        return _word_set(extract_text(path.name, path.read_bytes()))
    except Exception:  # noqa: BLE001 - an unreadable file is simply not a match
        return set()


def _master_docx(ws: Workspace) -> Path | None:
    """The library Word CV holding most of the Master CV's words. The Master CV is a parsed,
    shorter copy, so containment (not overlap both ways) decides."""
    master = ws.master_cv
    words = _word_set(cv_to_text(master, tags=False)) if master is not None else set()
    if not words or not _asset_dir(ws).is_dir():
        return None
    best: tuple[float, Path | None] = (0.0, None)
    for item in _uploaded_assets(ws):
        path = _asset_dir(ws) / item.filename
        if path.suffix.lower() == ".docx":
            score = len(words & _file_words(path)) / len(words)
            if score > best[0]:
                best = (score, path)
    return best[1] if best[0] >= SAME_CV_TEXT else None


def _matching_docx(ws: Workspace, source: Path) -> Path | None:
    """The uploaded Word CV with the same name, or nearly the same text, as `source`."""
    candidates = [
        _asset_dir(ws) / item.filename
        for item in _uploaded_assets(ws)
        if item.filename.lower().endswith(".docx")
    ]
    same_name = next((p for p in candidates if p.stem.casefold() == source.stem.casefold()), None)
    text = _file_words(source) if same_name is None else set()
    if same_name is not None or not text:
        return same_name
    best: tuple[float, Path | None] = (0.0, None)
    for path in candidates:
        other = _file_words(path)
        score = len(text & other) / len(text | other) if text | other else 0.0
        if score > best[0]:
            best = (score, path)
    return best[1] if best[0] >= SAME_CV_TEXT else None


def _write_tailored(
    tailored: TailoredCV, master: MasterCV, original: Path | None, path: Path, template: str
) -> tuple[TailoredCV, str]:
    """Write the Word file: by default a copy of the CV's Word design with the tailored
    content; a named template only when the user chose one (or no Word version exists).
    Returns the CV with notes, and the template name used."""
    if template == tailored_documents.ORIGINAL and original is not None:
        notes = write_like_original(
            original,
            master,
            tailored.cv,
            path,
            tailored.headline_options,
            tailored.role_skills,
            tailored.academic,
            tailored.skills_to_confirm,
            tailored.review_additions,
            tailored.compressions,
            key_strengths=tailored.key_strengths,
        )
        notes.insert(0, f"Written in the design of {original.name}")
    elif template == tailored_documents.ORIGINAL:
        export_docx(tailored, path, "classic")
        template = "classic"
        notes = ["No Word version of your CV was found, so the classic design was used"]
    else:
        export_docx(tailored, path, template)
        notes = [f"Written in the {template} design, as you chose"]
    notes = [*notes, *tailored.document_notes]
    return tailored.model_copy(update={"document_notes": notes}), template


def _source_coverage(
    ws: Workspace, master: MasterCV, original: Path | None, keywords: list[str]
) -> float:
    """Keyword coverage of the CV as it was before tailoring, read from its file."""
    if original is not None:
        return check_docx(original, master, keywords).keyword_coverage
    ws.output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=ws.output_dir) as source_dir:
        source_path = export_docx(master, Path(source_dir) / "source.docx", "classic")
        return check_docx(source_path, master, keywords).keyword_coverage


async def attach_existing_cv(
    ws: Workspace,
    job: JobPosting,
    asset_id: str,
    usage_sink: UsageSink | None = None,
) -> TailoredDocument:
    """Import an existing Word CV for review before it can support this job's letter."""
    source = _source_path(ws, asset_id)
    if source.suffix.lower() != ".docx" or "cover_letter" in source.stem.lower():
        raise ValueError("Choose a Word CV, not a cover letter or another file type")
    parsed = await import_cv(
        ws,
        source.name,
        await asyncio.to_thread(source.read_bytes),
        save=False,
        usage_sink=usage_sink,
        role="letter",
    )
    jd = JDAnalysis(job_title=job.title, company=job.company)
    tailored = apply_plan(parsed, TailoringPlan(), jd)
    path = export_docx(
        tailored, _new_output_path(ws, f"{_file_stem(parsed, job)}_imported"), "classic"
    )
    tailored = tailored.model_copy(update={"ats": check_docx(path, tailored.cv, [])})
    return tailored_documents.create(
        ws,
        job.id,
        parsed,
        jd,
        tailored,
        "classic",
        path,
        posting=job,
        reviewed=False,
        imported=True,
    )


async def export_general_cv(
    ws: Workspace,
    template: str = "original",
    usage_sink: UsageSink | None = None,
) -> tuple[Path, int]:
    """Export every role from the selected CV as an editable, general Word CV (by default in
    the CV's own Word design)."""
    progress.step("Reading your CV", total=2)
    cv = await ensure_selected_cv(ws, usage_sink, role="cv")
    progress.step("Exporting to Word")
    name = re.sub(r"[^A-Za-z0-9]+", "_", cv.basics.name).strip("_") or "Candidate"
    path = _new_output_path(ws, f"{name}_General_CV")
    original = original_docx(ws) if template == tailored_documents.ORIGINAL else None
    if original is not None:
        shutil.copy2(original, path)  # every role is already there, in the CV's own design
    else:
        export_docx(cv, path, "classic" if template == tailored_documents.ORIGINAL else template)
    return path, len(cv.experience)


async def write_cover_letter(
    ws: Workspace,
    job: JobPosting,
    template: str = "classic",
    usage_sink: UsageSink | None = None,
    tailored_cv_id: str | None = None,
) -> tuple[CoverLetter, Path]:
    """Export a guarded letter citing the selected CV or an explicitly linked tailored draft."""
    document = tailored_documents.load(ws, tailored_cv_id, job.id) if tailored_cv_id else None
    if document is not None and not document.reviewed:
        if document.imported:
            raise ValueError("Review and save the imported CV before using it for a cover letter")
        raise ValueError("Resolve the CV's red suggestions before using it for a cover letter")
    progress.step("Reading your CV", total=8)
    master_cv = (
        document.tailored.cv
        if document
        else await ensure_selected_cv(ws, usage_sink, role="letter")
    )
    llm = ws.structured("letter", usage_sink, "Cover letter")
    text = _job_text(job)
    progress.step("Analysing the job description")
    jd = await _analysis(ws, text, llm)
    letter = await asyncio.to_thread(
        write_letter,
        master_cv,
        jd,
        text,
        llm,
        motivation(ws, document.cv_id if document else None),
        letter_context(document.tailored if document else None),
        False,  # fast: the checks and one redraft; the user reviews the letter
        letter_gaps(document.tailored if document else None),
    )
    if not letter.paragraphs:
        raise ValueError("The model wrote no paragraphs; try again")
    progress.step("Exporting to Word")
    path = _new_output_path(ws, f"{_file_stem(master_cv, job)}_cover_letter", "cover_letters")
    exported = export_cover_letter(letter, master_cv, path, template)
    from src.services import cover_letters

    cover_letters.create(ws, job, letter, master_cv, text, template, exported)
    tracker.remember_document_job(ws, job)
    return letter, exported


def motivation(ws: Workspace, cv_id: str | None = None) -> str:
    """The user's own reasons, from the career intent (the only source a letter may use)."""
    intent = get_intent(ws, cv_id)
    parts = [intent.direction, *intent.energising_work, *intent.target_areas]
    return "; ".join(p for p in parts if p)


def matcher_guidance(verdict: JobVerdict | None) -> str:
    """The job_matcher's reading of the job, for the tailoring plan to lead with."""
    if verdict is None:
        return ""
    lines = [f"fit {verdict.fit_score} ({verdict.band}): {verdict.fit_summary}"]
    for label, items in (
        ("reasons", verdict.reasons),
        ("transferable", verdict.transferable),
        ("gaps", verdict.gaps),
        ("core requirements unmet (they cap the fit score)", verdict.essential_unmet),
    ):
        if items:
            lines.append(f"{label}: " + "; ".join(items))
    return "\n".join(lines)


async def _analysis(ws: Workspace, text: str, llm: LLMProvider) -> JDAnalysis:
    """The job description's analysis, made once per job text (CV and letter share it)."""
    key = hashlib.sha256(text.encode()).hexdigest()
    if key not in ws.jd_analyses:
        ws.jd_analyses[key] = await asyncio.to_thread(analyze_jd, text, llm)
    return literal_terms(ws.jd_analyses[key], text)  # also for analyses made before


def _job_text(job: JobPosting) -> str:
    return f"{job.title} at {job.company}\n{job.location or ''}\n\n{job.description}"


def _file_stem(cv: MasterCV, job: JobPosting) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", cv.basics.name).strip("_") or "Candidate"
    company = re.sub(r"[^A-Za-z0-9]+", "_", job.company).strip("_") or "Company"
    role = re.sub(r"[^A-Za-z0-9]+", "_", job.title).strip("_") or "Role"
    return f"{name}_{company}_{role}"


def _new_output_path(ws: Workspace, stem: str, kind: str = "cvs") -> Path:
    """Give every generated document a new path, preserving earlier editable versions."""
    directory = ws.output_dir / kind
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.docx"
    number = 2
    while path.exists():
        path = directory / f"{stem}_{number}.docx"
        number += 1
    return path
