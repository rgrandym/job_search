"""Job tracker: what the user has applied for, ruled out or already seen, across searches.

Stored in `data/job_tracker.json` (git-ignored, personal data), one entry per role:

- **Applied** (set by the user) and **N/A** (ruled
  out) jobs are set aside *before* screening, so they cost no model calls and never take a
  place in the ranked matches. They are listed once, in the report's `applied` / `dismissed`.
- Every other job that reaches the matches or "not selected" is **New** the first search it
  appears in and **Open** afterwards.
- A role matches when it is the same posting (id or URL), or the same employer with the same
  title (or the same role words at the same level). Another role at the same employer is never
  skipped; it carries a one-line `related` note instead.
- An application older than `APPLIED_LOOKBACK_DAYS` is history, not a reason to skip: a
  re-advertised role is ranked again, with the earlier date shown.
- The note is the user's: the app never rewrites it.
- Outcomes: an application moves through stages (screening, interview, ... offer, rejected,
  no response), each dated; a ruled-out or applied job may carry the user's reason. The fit
  score and role family at decision time are kept, so `services.calibration` can point out
  patterns (never acting on them by itself).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter

from src.jobs.models import (
    JobPosting,
    JobStatus,
    JobTracking,
    MatchReport,
    MatchResult,
    OutcomeStage,
)
from src.jobs.sources.base import stable_id
from src.services.workspace import Workspace
from src.tools.search_tools import company_key, seniority_level, title_core

APPLIED_LOOKBACK_DAYS = 365
FORGET_SEEN_DAYS = 180  # jobs only ever seen (never acted on) are forgotten after this
StoredStatus = Literal["seen", "applied", "na"]


class StageEvent(BaseModel):
    stage: OutcomeStage
    at: str  # ISO date


class TrackedJob(BaseModel):
    """One role in the tracker."""

    id: str
    title: str
    company: str
    location: str | None = None
    url: str | None = None
    job_id: str | None = None
    source: str = "manual"
    status: StoredStatus = "seen"
    note: str = ""
    first_seen: str
    last_seen: str
    first_run: str | None = None
    applied_at: str | None = None
    cv_file: str | None = None
    reason: str = ""  # the user's: why it was (or was not) suitable
    stage: OutcomeStage | None = None  # latest outcome of an application
    stages: list[StageEvent] = Field(default_factory=list)
    fit_score: int | None = None  # job_matcher fit when the user decided
    family: str | None = None  # role family the search attributed it to
    document_postings: dict[str, JobPosting] = Field(default_factory=dict)
    application_result: MatchResult | None = None


class ManualApplication(BaseModel):
    """An application made outside the app, added to the register by hand."""

    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    url: str | None = None
    note: str = ""
    applied_at: date | None = None


def _path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "job_tracker.json"


def load(ws: Workspace) -> dict[str, TrackedJob]:
    """Tracked roles by id (empty when the file is missing or damaged)."""
    try:
        raw = json.loads(_path(ws).read_text(encoding="utf-8"))
        jobs = TypeAdapter(list[TrackedJob]).validate_python(raw.get("jobs", []))
    except (OSError, ValueError, AttributeError):
        return {}
    return {j.id: j for j in jobs}


def _save(ws: Workspace, entries: dict[str, TrackedJob], today: date) -> None:
    cutoff = (today - timedelta(days=FORGET_SEEN_DAYS)).isoformat()
    kept = [
        e for e in entries.values()
        if e.status != "seen" or e.last_seen >= cutoff or e.document_postings
    ]
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "jobs": [e.model_dump(mode="json") for e in kept]}
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _title(text: str) -> str:
    return " ".join(text.casefold().split())


def role_id(title: str, company: str) -> str:
    """Tracker id of a role: the employer (aliases merged) and the title."""
    return stable_id(company_key(company), _title(title))


def _same_role(entry: TrackedJob, job: JobPosting) -> bool:
    if company_key(entry.company) != company_key(job.company):
        return False
    if _title(entry.title) == _title(job.title):
        return True
    core = title_core(job.title)
    return (
        bool(core)
        and core == title_core(entry.title)
        and (seniority_level(entry.title) == seniority_level(job.title))
    )


def find(entries: dict[str, TrackedJob], job: JobPosting) -> TrackedJob | None:
    """The tracked role this posting is: same posting id, same URL, or same role."""
    hit = entries.get(role_id(job.title, job.company))
    if hit is not None:
        return hit
    url = (job.url or "").split("?")[0].rstrip("/")
    for entry in entries.values():
        if entry.job_id == job.id or (url and (entry.url or "").split("?")[0].rstrip("/") == url):
            return entry
    return next((e for e in entries.values() if _same_role(e, job)), None)


def _recent(applied_at: str | None, today: date) -> bool:
    if applied_at is None:
        return True
    return date.fromisoformat(applied_at) >= today - timedelta(days=APPLIED_LOOKBACK_DAYS)


def _related(entries: dict[str, TrackedJob], job: JobPosting, own: TrackedJob | None) -> str | None:
    """A one-line note about an application to another role at the same employer."""
    key = company_key(job.company)
    for e in entries.values():
        if e.status == "applied" and e is not own and company_key(e.company) == key:
            when = f" on {e.applied_at}" if e.applied_at else ""
            return f"You applied for {e.title} here{when}"
    return None


def _tracking(
    entry: TrackedJob, status: JobStatus, entries: dict[str, TrackedJob], job: JobPosting
) -> JobTracking:
    return JobTracking(
        status=status,
        note=entry.note,
        first_seen=entry.first_seen,
        applied_at=entry.applied_at,
        cv_file=entry.cv_file,
        related=_related(entries, job, entry),
        reason=entry.reason,
        stage=entry.stage,
    )


def set_aside(
    ws: Workspace, jobs: list[JobPosting], today: date | None = None
) -> tuple[list[JobPosting], list[MatchResult], list[MatchResult]]:
    """(jobs to search on, already applied for, ruled out). Run before screening."""
    today = today or date.today()
    entries = load(ws)
    keep: list[JobPosting] = []
    applied: list[MatchResult] = []
    dismissed: list[MatchResult] = []
    for job in jobs:
        entry = find(entries, job)
        if entry is not None and entry.status == "applied" and _recent(entry.applied_at, today):
            reason = f"already applied on {entry.applied_at or 'an earlier date'}"
            tracking = _tracking(entry, "applied", entries, job)
            applied.append(
                MatchResult(job=job, excluded=True, exclusion_reasons=[reason], tracking=tracking)
            )
        elif entry is not None and entry.status == "na":
            tracking = _tracking(entry, "na", entries, job)
            reason = "marked N/A by you"
            dismissed.append(
                MatchResult(job=job, excluded=True, exclusion_reasons=[reason], tracking=tracking)
            )
        else:
            keep.append(job)
    return keep, applied, dismissed


def annotate(
    ws: Workspace, report: MatchReport, run: str | None = None, today: date | None = None
) -> None:
    """Record this search's matches and "not selected" jobs and give each its status:
    New (first search it appears in) or Open (seen before). Applied/N/A are set aside earlier."""
    today_s = (today or date.today()).isoformat()
    run = run or uuid.uuid4().hex
    entries = load(ws)
    for result in [*report.matches, *report.below_threshold]:
        job = result.job
        entry = find(entries, job)
        if entry is None:
            entry = TrackedJob(
                id=role_id(job.title, job.company),
                title=job.title,
                company=job.company,
                location=job.location,
                url=job.url,
                job_id=job.id,
                source=job.source,
                first_seen=today_s,
                last_seen=today_s,
                first_run=run,
            )
            entries[entry.id] = entry
        entry.last_seen = today_s
        if entry.status == "seen":
            status: JobStatus = "new" if entry.first_run == run else "open"
        else:  # an application past the look-back: ranked again, with its date shown
            status = "open"
        result.tracking = _tracking(entry, status, entries, job)
    _save(ws, entries, today or date.today())


def refresh(ws: Workspace, report: MatchReport) -> None:
    """Bring a reopened search's statuses and notes up to date with the tracker."""
    entries = load(ws)
    for result in report.all_results():
        entry = find(entries, result.job) if result.tracking else None
        if entry is None or result.tracking is None:
            continue
        status = result.tracking.status
        if entry.status == "applied":
            status = "applied"
        elif entry.status == "na":
            status = "na"
        elif status in ("applied", "na"):
            status = "open"
        result.tracking = _tracking(entry, status, entries, result.job)


def tracking_for(ws: Workspace, job: JobPosting) -> JobTracking | None:
    """The tracker's current view of a job (applied, N/A or open, note, stage), or None if it
    was never tracked."""
    entries = load(ws)
    entry = find(entries, job)
    if entry is None:
        return None
    status: JobStatus = "open" if entry.status == "seen" else entry.status
    return _tracking(entry, status, entries, job)


def set_status(
    ws: Workspace,
    job: JobPosting,
    status: Literal["open", "applied", "na"],
    note: str | None = None,
    cv_file: str | None = None,
    today: date | None = None,
    reason: str | None = None,
    stage: OutcomeStage | None = None,
) -> JobTracking:
    """The user's decision on a job. `note=None` / `reason=None`
    keep what is there; `stage` records an application's outcome (dated)."""
    today = today or date.today()
    entries = load(ws)
    entry = find(entries, job) or TrackedJob(
        id=role_id(job.title, job.company),
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        job_id=job.id,
        source=job.source,
        first_seen=today.isoformat(),
        last_seen=today.isoformat(),
    )
    entries[entry.id] = entry
    stored: StoredStatus = "seen" if status == "open" else status
    newly_applied = stored == "applied" and entry.status != "applied"
    if stored == "applied" and (entry.status != "applied" or entry.applied_at is None):
        entry.applied_at = today.isoformat()
    elif stored != "applied":
        entry.applied_at = None
    entry.status = stored
    if stored == "applied" and (newly_applied or entry.application_result is None):
        result = ws.result(job.id)
        entry.application_result = (
            result.model_copy(update={"tracking": None}) if result else MatchResult(job=job)
        )
    if note is not None:
        entry.note = note.strip()
    if reason is not None:
        entry.reason = reason.strip()
    if cv_file is not None:
        entry.cv_file = cv_file
        entry.document_postings[job.id] = job
    if stage is not None and stage != entry.stage:
        entry.stage = stage
        entry.stages.append(StageEvent(stage=stage, at=today.isoformat()))
    _snapshot(ws, entry, job.id)
    _save(ws, entries, today)
    return _tracking(entry, status, entries, job)


def remember_document_job(ws: Workspace, job: JobPosting, cv_file: str | None = None) -> None:
    """Retain a posting used for a document without changing its application status."""
    today = date.today()
    entries = load(ws)
    entry = find(entries, job) or TrackedJob(
        id=role_id(job.title, job.company),
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        job_id=job.id,
        source=job.source,
        first_seen=today.isoformat(),
        last_seen=today.isoformat(),
    )
    entry.document_postings[job.id] = job
    if cv_file is not None:
        entry.cv_file = cv_file
    entries[entry.id] = entry
    _save(ws, entries, today)


def document_job(ws: Workspace, job_id: str) -> JobPosting | None:
    """Find the original posting of a previously generated document."""
    return next(
        (entry.document_postings[job_id] for entry in load(ws).values()
         if job_id in entry.document_postings),
        None,
    )


def _snapshot(ws: Workspace, entry: TrackedJob, job_id: str) -> None:
    """Keep the matcher's fit and the role family the job had when the user decided."""
    result = ws.result(job_id)  # the current search or a saved job
    if result is not None and result.verdict is not None:
        entry.fit_score = result.verdict.fit_score
    if result is not None and result.family:
        entry.family = result.family


def update_result(ws: Workspace, job_id: str, tracking: JobTracking) -> None:
    """Show a changed status on the current report's copy of the job."""
    report = ws.last_report
    if report is None:
        return
    for result in report.all_results():
        if result.job.id == job_id:
            result.tracking = tracking


def register(ws: Workspace) -> list[TrackedJob]:
    """Applications and ruled-out jobs, most recent first."""
    acted = [e for e in load(ws).values() if e.status != "seen"]
    for entry in acted:
        if entry.status == "applied" and entry.application_result is None and entry.job_id:
            result = ws.result(entry.job_id)
            if result is not None:
                entry.application_result = result.model_copy(update={"tracking": None})
    return sorted(acted, key=lambda e: e.applied_at or e.last_seen, reverse=True)


def add_application(ws: Workspace, app: ManualApplication, today: date | None = None) -> TrackedJob:
    """Record an application made outside the app (searches will skip that role)."""
    today = today or date.today()
    entries = load(ws)
    job = JobPosting(
        id=f"manual:{role_id(app.title, app.company)}",
        title=app.title,
        company=app.company,
        url=app.url,
    )
    entry = find(entries, job) or TrackedJob(
        id=role_id(app.title, app.company),
        title=app.title,
        company=app.company,
        url=app.url,
        first_seen=today.isoformat(),
        last_seen=today.isoformat(),
    )
    entry.status = "applied"
    entry.applied_at = (app.applied_at or today).isoformat()
    if entry.application_result is None:
        result = ws.result(job.id)
        entry.application_result = (
            result.model_copy(update={"tracking": None}) if result else MatchResult(job=job)
        )
    entry.note = app.note.strip() or entry.note
    entries[entry.id] = entry
    _save(ws, entries, today)
    return entry


def edit_entry(
    ws: Workspace,
    entry_id: str,
    status: Literal["open", "applied", "na"],
    note: str | None,
    reason: str | None = None,
    stage: OutcomeStage | None = None,
) -> TrackedJob:
    """Change a register entry's status, note, reason or outcome stage."""
    entries = load(ws)
    entry = entries.get(entry_id)
    if entry is None:
        raise ValueError("That job is not in the tracker")
    job = JobPosting(
        id=entry.job_id or f"manual:{entry.id}",
        title=entry.title,
        company=entry.company,
        url=entry.url,
    )
    set_status(ws, job, status, note, reason=reason, stage=stage)
    return load(ws)[entry_id]


def delete_entry(ws: Workspace, entry_id: str) -> bool:
    entries = load(ws)
    if entries.pop(entry_id, None) is None:
        return False
    _save(ws, entries, date.today())
    return True
