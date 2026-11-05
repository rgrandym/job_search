"""The UI's remembered settings, kept on disk for each launcher (user's request 2026-11-05).

The page remembers its filters, preferences, theme, layout, zoom and window sizes in the
browser's local storage, which belongs to one address (host and port). A copy is kept in
`data/ui_state_<launcher>.json` (git-ignored) so that they survive a port change or a cleared
browser: the page restores what its storage lacks at startup and saves its storage back. The
Dock app ("desktop") and the browser web app ("web") keep separate copies, as they keep
separate local storage.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.config import Settings

PREFIXES = ("jobsearch.", "react-resizable-panels:jobsearch")  # the page's own keys only
MAX_BYTES = 512_000


def _path(settings: Settings) -> Path:
    return settings.data_dir / f"ui_state_{settings.launcher}.json"


def _kept(items: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in items.items() if k.startswith(PREFIXES)}


def load(settings: Settings) -> dict[str, str]:
    """This launcher's saved settings; empty when none were saved or the file is unreadable."""
    path = _path(settings)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return _kept({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)})


def save(settings: Settings, items: dict[str, str]) -> dict[str, str]:
    """Replace the saved settings with the page's own keys from `items`; returns what was kept."""
    kept = _kept(items)
    text = json.dumps(kept, indent=1, sort_keys=True)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("The page's settings are too large to keep")
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, "utf-8")
    temporary.replace(path)
    return kept
