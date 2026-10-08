"""Saved jobs: the ones the user ticked and kept from a search, across searches.

Stored in `data/saved_jobs.json` (git-ignored, personal data), newest first, each with the
result as it was judged (posting, verdict, role family, flags). Their status is read live from
`services.tracker`; applied jobs are hidden here and shown in the application register. Saved
jobs stay until the user removes them, here or by deleting the job from the results
(`search_service.remove_result`, which also drops it from every retained search; the register
of applications is never changed); tailoring and cover
letters work on them like on current results (`Workspace.job` finds both).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from src.jobs.models import SavedJob
from src.services import tracker
from src.services.workspace import Workspace


def _write(ws: Workspace, items: list[SavedJob]) -> None:
    path = ws.saved_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([i.model_dump(mode="json") for i in items], indent=1), "utf-8")


def list_saved(ws: Workspace) -> list[SavedJob]:
    """Saved jobs, newest first, each with its current tracker status."""
    out = []
    for item in ws.saved_jobs():
        tracking = tracker.tracking_for(ws, item.result.job)
        if tracking is not None and tracking.status == "applied":
            continue
        result = item.result.model_copy(update={"tracking": tracking})
        out.append(item.model_copy(update={"result": result}))
    return out


def save(ws: Workspace, job_ids: list[str]) -> list[SavedJob]:
    """Save jobs of the current search (already saved ones are kept as they are). Returns the
    newly saved ones; unknown ids raise, so the UI never shows a save that did not happen."""
    items = ws.saved_jobs()
    known = {i.result.job.id for i in items}
    report = ws.last_report
    results = {r.job.id: r for r in report.all_results()} if report else {}
    missing = [j for j in job_ids if j not in results and j not in known]
    if missing:
        raise ValueError(f"Not in the current search: {', '.join(missing)}")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    new = [
        SavedJob(result=results[j].model_copy(update={"tracking": None}), saved_at=now)
        for j in dict.fromkeys(job_ids)
        if j not in known
    ]
    if new:
        _write(ws, new + items)
    return new


def remove(ws: Workspace, job_ids: list[str]) -> int:
    """Remove saved jobs (the user's choice); returns how many were removed. Their tracker
    entries (applications, outcomes) are kept."""
    items = ws.saved_jobs()
    kept = [i for i in items if i.result.job.id not in set(job_ids)]
    if len(kept) != len(items):
        _write(ws, kept)
    return len(items) - len(kept)
