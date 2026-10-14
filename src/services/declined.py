"""Red proposals the user declined, remembered across jobs (user's decision 2026-10-14).

Deleting an optimised CV's red proposal means "I did not do this". It is kept in
`data/declined_proposals.json` (git-ignored, personal data) for the CV it was made from, so
later tailorings of that CV never propose it again (`proposals.declined_match`) and the
writer is told what the user has ruled out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.cv.models import ReviewAddition
from src.services.workspace import Workspace

MAX_REMEMBERED = 200  # per CV, newest kept
MAX_PROMPTED = 40  # newest shown to the writer


class DeclinedProposal(BaseModel):
    """One proposal the user deleted."""

    model_config = ConfigDict(extra="forbid")

    cv_id: str | None = None
    kind: str
    text: str
    requirement: str = ""
    reason: str = ""
    at: str


def _path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "declined_proposals.json"


def load(ws: Workspace) -> list[DeclinedProposal]:
    """Every remembered declined proposal, newest first."""
    path = _path(ws)
    if not path.is_file():
        return []
    try:
        return [DeclinedProposal.model_validate(d) for d in json.loads(path.read_text("utf-8"))]
    except (ValueError, TypeError):
        return []  # an unreadable file never blocks tailoring


def record(ws: Workspace, cv_id: str | None, addition: ReviewAddition, reason: str = "") -> None:
    """Remember that the user declined `addition` for the CV `cv_id`."""
    if addition.kind == "metric":
        return  # a number question declined is not a fact ruled out
    item = DeclinedProposal(
        cv_id=cv_id,
        kind=addition.kind,
        text=addition.text,
        requirement=addition.requirement,
        reason=reason.strip(),
        at=datetime.now(UTC).isoformat(),
    )
    same = [d for d in load(ws) if d.cv_id == cv_id and d.text != addition.text]
    others = [d for d in load(ws) if d.cv_id != cv_id]
    items = [item, *same[: MAX_REMEMBERED - 1], *others]
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps([d.model_dump() for d in items], indent=1), "utf-8")
    temporary.replace(path)


def texts(ws: Workspace, cv_id: str | None) -> list[str]:
    """What the user declined for this CV, newest first (compared word for word with new
    proposals, so the reason is kept on the record only)."""
    return [d.text for d in load(ws) if d.cv_id == cv_id][:MAX_PROMPTED]
