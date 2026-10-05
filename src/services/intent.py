"""Career intent: what the user wants from the next role, kept apart from the CV's facts.

One `SearchIntent` per CV (the same person may keep CVs for different directions), stored in
`data/search_intent.json` (git-ignored, personal). The user edits it in the Profiles dialog,
or asks the assistant to add something they said ("I'd like to move into business
development"). It orients the profile summary's role families, sets each verdict's
alignment, and supplies confirmed languages and eligibility to the pre-filter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.cv import master_cv_manager as mgr
from src.jobs.models import SearchIntent
from src.services.workspace import Workspace

NO_CV = "no-cv"  # intent for filter-only searches


def _path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "search_intent.json"


def _owner(ws: Workspace) -> str:
    return ws.active_cv_id or NO_CV


def _load_all(ws: Workspace) -> dict[str, Any]:
    try:
        raw = json.loads(_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def get_intent(ws: Workspace, cv_id: str | None = None) -> SearchIntent:
    """A CV's intent (the selected CV by default; empty when none was stated)."""
    stored = _load_all(ws).get(cv_id or _owner(ws))
    try:
        return SearchIntent.model_validate(stored) if stored else SearchIntent()
    except ValueError:
        return SearchIntent()


def save_intent(ws: Workspace, intent: SearchIntent) -> SearchIntent:
    """Replace the selected CV's intent (the user's edit)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    intent = intent.model_copy(update={"updated_at": now})
    data = _load_all(ws) | {_owner(ws): intent.model_dump(mode="json")}
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return intent


def delete_intent(ws: Workspace, cv_id: str) -> None:
    """Remove intent belonging to a CV removed from the library."""
    data = _load_all(ws)
    if cv_id not in data:
        return
    del data[cv_id]
    _path(ws).write_text(json.dumps(data, indent=2), encoding="utf-8")


def patch_intent(ws: Workspace, patch: dict[str, Any]) -> tuple[SearchIntent, list[str]]:
    """Apply a JSON merge patch (lists are replaced whole) and save. Returns the new intent
    and one line per changed field, so the change can be reported back to the user."""
    before = get_intent(ws)
    merged = mgr.merge_patch(before.model_dump(mode="json"), patch)
    after = SearchIntent.model_validate({**merged, "updated_at": before.updated_at})
    changes = describe_changes(before, after)
    return (save_intent(ws, after) if changes else before), changes


def describe_changes(before: SearchIntent, after: SearchIntent) -> list[str]:
    """Human-readable differences, one per field ("target_areas: + business development")."""
    out: list[str] = []
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    for key in new:
        if key == "updated_at" or old[key] == new[key]:
            continue
        if isinstance(new[key], list):
            added = [_item(v) for v in new[key] if v not in old[key]]
            removed = [_item(v) for v in old[key] if v not in new[key]]
            parts = [f"+ {a}" for a in added] + [f"- {r}" for r in removed]
            out.append(f"{key}: {'; '.join(parts)}")
        else:
            out.append(f"{key}: {new[key]!r}")
    return out


def _item(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values())
    return str(value)
