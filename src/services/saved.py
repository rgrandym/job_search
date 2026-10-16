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

from pydantic import BaseModel, Field

from src.jobs.models import JobPosting, MatchResult, SavedJob
from src.jobs.sources.base import stable_id
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


class OutsideJob(BaseModel):
    """A posting the user found elsewhere and supplies for document creation."""

    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    description: str = Field(min_length=1)
    url: str | None = None
    location: str | None = None


def import_job(ws: Workspace, posting: OutsideJob) -> SavedJob:
    """Keep an outside posting in Saved so existing document actions can use it."""
    from urllib.parse import urlsplit

    title, company, description = (
        posting.title.strip(),
        posting.company.strip(),
        posting.description.strip(),
    )
    if not title or not company or not description:
        raise ValueError("Title, company and job description are required")
    url = (posting.url or "").strip() or None
    if url and (urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc):
        raise ValueError("The posting link must be an http or https URL")
    job = JobPosting(
        id=f"manual:{stable_id(url or title, company)}",
        title=title,
        company=company,
        description=description,
        url=url,
        location=(posting.location or "").strip() or None,
        source="manual",
    )
    items = ws.saved_jobs()
    existing = next((item for item in items if item.result.job.id == job.id), None)
    if existing is not None:
        return existing
    item = SavedJob(
        result=MatchResult(job=job), saved_at=datetime.now(UTC).isoformat(timespec="seconds")
    )
    _write(ws, [item, *items])
    return item


def save_found(ws: Workspace, job: JobPosting) -> SavedJob:
    """Keep a focused lookup result with its original board ID and full posting text."""
    if not job.title.strip() or not job.company.strip() or not job.description.strip():
        raise ValueError(
            "This posting has no readable description; paste its text under Add a job you found"
        )
    items = ws.saved_jobs()
    existing = next((item for item in items if item.result.job.id == job.id), None)
    if existing is not None:  # saved before without its full text: keep the fuller copy
        if len(job.description) > len(existing.result.job.description):
            return update_posting(ws, job)
        return existing
    item = SavedJob(
        result=MatchResult(job=job), saved_at=datetime.now(UTC).isoformat(timespec="seconds")
    )
    _write(ws, [item, *items])
    return item


def update_posting(ws: Workspace, job: JobPosting) -> SavedJob:
    """Replace a saved job's posting with a fuller copy of it (same id); its verdict stays."""
    items = ws.saved_jobs()
    for index, item in enumerate(items):
        if item.result.job.id == job.id:
            result = item.result.model_copy(update={"job": job})
            items[index] = item.model_copy(update={"result": result})
            _write(ws, items)
            return items[index]
    raise ValueError("This job is not in Saved")


def remove(ws: Workspace, job_ids: list[str]) -> int:
    """Remove saved jobs (the user's choice); returns how many were removed. Their tracker
    entries (applications, outcomes) are kept."""
    items = ws.saved_jobs()
    kept = [i for i in items if i.result.job.id not in set(job_ids)]
    if len(kept) != len(items):
        _write(ws, kept)
    return len(items) - len(kept)
