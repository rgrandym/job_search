"""Keep generated CVs and cover letters in their separate output folders."""

from __future__ import annotations

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


def _available_path(directory: Path, name: str) -> Path:
    """Avoid overwriting a file already exported under the same name."""
    target = directory / name
    number = 2
    while target.exists():
        target = directory / f"{Path(name).stem}_{number}{Path(name).suffix}"
        number += 1
    return target
