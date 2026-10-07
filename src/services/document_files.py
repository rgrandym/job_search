"""Keep generated CVs and cover letters in their separate output folders."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from src.services.workspace import Workspace


def organize_legacy_output(ws: Workspace) -> None:
    """Move older generated Word files out of the shared output root once."""
    if not ws.output_dir.is_dir():
        return
    for source in ws.output_dir.glob("*.docx"):
        folder = "cover_letters" if "cover_letter" in source.stem.lower() else "cvs"
        directory = ws.output_dir / folder
        directory.mkdir(parents=True, exist_ok=True)
        target = _available_path(directory, source.name)
        source.replace(target)


def save_upload_copy(ws: Workspace, filename: str, data: bytes) -> Path:
    """Keep a byte-identical copy of an uploaded CV in output/cvs/ next to the user's other CVs."""
    directory = ws.output_dir / "cvs"
    directory.mkdir(parents=True, exist_ok=True)
    existing = directory / filename
    if existing.is_file() and existing.read_bytes() == data:
        return existing
    target = _available_path(directory, filename)
    tmp = directory / f".{target.name}.uploading"
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


def remove_upload_copies(ws: Workspace, data: bytes) -> None:
    """Delete the output/cvs/ copies of an upload the user removed from the library."""
    directory = ws.output_dir / "cvs"
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data:
            path.unlink()


# Word for Mac is sandboxed: it can always read and write its own container, so previews are
# converted from a copy there. A copy also never disturbs a document the user has open.
WORD_CONTAINER_TMP = Path.home() / "Library/Containers/com.microsoft.Word/Data/tmp"
_WORD_TO_PDF = """
on run argv
    tell application "Microsoft Word"
        open (POSIX file (item 1 of argv))
        repeat 60 times
            if exists document (item 3 of argv) then exit repeat
            delay 0.5
        end repeat
        set doc to document (item 3 of argv)
        save as doc file name (item 2 of argv) file format format PDF
        close doc saving no
    end tell
end run
"""


def word_pdf_preview(source: Path, cache_dir: Path) -> Path | None:
    """PDF rendering of a Word file made by Microsoft Word itself; None when Word is unavailable."""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:24]
    cached = cache_dir / f"{digest}.pdf"
    if cached.is_file():
        return cached
    if sys.platform != "darwin" or not WORD_CONTAINER_TMP.is_dir():
        return None
    work_docx = WORD_CONTAINER_TMP / f"cv-preview-{digest}.docx"
    work_pdf = work_docx.with_suffix(".pdf")
    shutil.copyfile(source, work_docx)
    try:
        subprocess.run(
            ["osascript", "-", str(work_docx), str(work_pdf), work_docx.name],
            input=_WORD_TO_PDF,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if not work_pdf.is_file():
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(work_pdf, cached)
        return cached
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        work_docx.unlink(missing_ok=True)
        work_pdf.unlink(missing_ok=True)


def _available_path(directory: Path, name: str) -> Path:
    """Avoid overwriting a file already exported under the same name."""
    target = directory / name
    number = 2
    while target.exists():
        target = directory / f"{Path(name).stem}_{number}{Path(name).suffix}"
        number += 1
    return target


def open_in_default_app(path: Path, app: str | None = None) -> bool:
    """Open a document in the desktop's default app, or a named macOS app; False if none."""
    if sys.platform == "darwin":
        command = ["open", *(["-a", app] if app else []), str(path)]
    elif sys.platform.startswith("linux"):
        command = ["xdg-open", str(path)]
    else:
        command = ["cmd", "/c", "start", "", str(path)]
    try:
        return subprocess.run(command, capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
