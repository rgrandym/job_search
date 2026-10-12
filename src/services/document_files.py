"""Keep generated CVs and cover letters in their separate output folders."""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
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


# macOS Quick Look renders Word files to HTML in a fraction of a second, without starting Word
# or asking for any permission: the viewer never opens Word (only "Open in Word" does).
QUICK_LOOK = "/usr/bin/qlmanage"
_LOCAL_IMAGE = re.compile(r'src="([^":/]+\.(png|jpe?g|gif|bmp|tiff?|svg))"', re.IGNORECASE)
_IMAGE_TYPES = {"jpg": "jpeg", "tif": "tiff", "svg": "svg+xml"}
# A white page on a grey desk, readable in the app's dark themes too.
_PAPER = (
    "<style>html{background:#e8e8e8;color-scheme:light}"
    "body>div{margin:16px auto;background:#fff;color:#000;box-shadow:0 1px 4px #0003}</style>"
)


def html_preview(source: Path, cache_dir: Path) -> Path | None:
    """An HTML page view of a Word file made by macOS Quick Look; None when unavailable."""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:24]
    cached = cache_dir / f"{digest}.html"
    if cached.is_file():
        return cached
    if sys.platform != "darwin" or not Path(QUICK_LOOK).is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="cv-preview-") as tmp:
        # A plain name: Quick Look names its output folder after the file.
        work = Path(tmp) / "document.docx"
        shutil.copyfile(source, work)
        try:
            subprocess.run(
                [QUICK_LOOK, "-p", "-o", tmp, str(work)],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        bundle = Path(tmp) / "document.docx.qlpreview"
        page = bundle / "Preview.html"
        if not page.is_file():
            return None
        html = _inline_images(page.read_text(encoding="utf-8", errors="replace"), bundle)
    html = html.replace("<head>", f"<head>{_PAPER}", 1)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(html, encoding="utf-8")
    return cached


def _inline_images(html: str, folder: Path) -> str:
    """Images Quick Look writes beside the page, embedded so the page stands alone."""

    def embed(match: re.Match[str]) -> str:
        image = folder / match.group(1)
        if not image.is_file() or image.parent != folder:
            return match.group(0)
        kind = match.group(2).lower()
        data = base64.b64encode(image.read_bytes()).decode()
        return f'src="data:image/{_IMAGE_TYPES.get(kind, kind)};base64,{data}"'

    return _LOCAL_IMAGE.sub(embed, html)


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
