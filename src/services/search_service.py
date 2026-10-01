"""Search pipeline used by the Search button, the REST API, and the agents.

1. Capture     sources (per title, location, distance, salary)  -> postings
2. Pre-filter  deterministic: hard exclusions + cheap ranking    -> shortlist
3. Enrich      full descriptions for shortlisted snippet-only postings (e.g. Reed)
4. Summarise   orchestrator ProfileSummary (from memory when the search type repeats)
5. Screen      job_matcher subagent judges the shortlist         -> true matches
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from src.core.llm_provider import LLMError
from src.jobs.fetcher import JobSource, build_sources, fetch_all
from src.jobs.matcher import JobMatcher
from src.jobs.models import JobPosting, MatchReport, ProfileSummary, SearchQuery
from src.jobs.profile_memory import summarize_profile
from src.jobs.screener import apply_verdicts, screen_jobs
from src.jobs.sources.base import SourceError
from src.services import cv_service
from src.services.workspace import Workspace

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop(_: str, __: dict[str, Any]) -> None:
    return None


class SearchRequest(BaseModel):
    query: SearchQuery
    use_cv: bool = True
    smart: bool = Field(True, description="Screen the shortlist with the job_matcher subagent")
    threshold: float | None = None
    refresh_summary: bool = False


class SearchOutcome(BaseModel):
    report: MatchReport
    fetched: int
    errors: dict[str, str] = Field(default_factory=dict)
    skipped_sources: dict[str, str] = Field(default_factory=dict)
    summary_from_memory: bool | None = None
    smart_unavailable: str | None = None
    seconds: float = 0.0


async def run_search(ws: Workspace, req: SearchRequest, emit: Emit = _noop) -> SearchOutcome:
    """Execute the full pipeline and store the report in the workspace."""
    t0 = time.monotonic()
    settings = ws.settings
    threshold = settings.score_threshold if req.threshold is None else req.threshold
    query = req.query
    cv = await cv_service.ensure_selected_cv(ws) if req.use_cv else None

    await emit("search_progress", {"stage": "capture", "message": "Collecting postings"})
    sources, skipped = build_sources(query.sources or None, settings=settings)
    jobs, errors = await asyncio.to_thread(fetch_all, sources, query)

    await emit("search_progress", {"stage": "prefilter", "message": f"Pre-filtering {len(jobs)}"})
    matcher = JobMatcher(settings=settings)
    report = matcher.match(cv, jobs, threshold=0 if req.smart else threshold, query=query)

    shortlist = _shortlist(report, settings.screen_shortlist_size)
    enriched = await asyncio.to_thread(_enrich, shortlist, sources, errors)
    if enriched:  # re-score with full descriptions
        report = matcher.match(
            cv, _replace(jobs, enriched), threshold=0 if req.smart else threshold, query=query
        )
        shortlist = _shortlist(report, settings.screen_shortlist_size)

    outcome = SearchOutcome(
        report=report, fetched=len(jobs), errors=errors, skipped_sources=skipped
    )
    if req.smart and not ws.llm_ready():
        outcome.smart_unavailable = "No LLM configured; showing keyword pre-filter scores only"
    if req.smart and ws.llm_ready() and shortlist:
        try:
            summary, cached = await get_summary(ws, cv, query, req.refresh_summary, emit)
            outcome.summary_from_memory = cached
            await emit(
                "search_progress",
                {"stage": "screen", "message": f"job_matcher screening {len(shortlist)} postings"},
            )
            verdicts, screen_errors = await screen_jobs(
                summary,
                shortlist,
                ws.structured("worker"),
                query,
                settings.screen_batch_size,
                settings.screen_concurrency,
            )
            errors |= {f"screening {i}": e for i, e in enumerate(screen_errors)}
            report.summary = summary
            apply_verdicts(report, verdicts, threshold)
        except LLMError as exc:
            outcome.smart_unavailable = f"Smart matching failed: {exc}"
    if outcome.smart_unavailable:  # fall back to deterministic buckets
        report = matcher.match(cv, _replace(jobs, enriched), threshold=threshold, query=query)
        outcome.report = report

    ws.last_query, ws.last_report = query, outcome.report
    outcome.seconds = round(time.monotonic() - t0, 2)
    await emit("search_results", {"outcome": outcome.model_dump(mode="json")})
    return outcome


async def get_summary(
    ws: Workspace, cv: Any, query: SearchQuery | None, refresh: bool, emit: Emit = _noop
) -> tuple[ProfileSummary, bool]:
    """Orchestrator-built profile summary, reused from memory for the same search type."""
    await emit("search_progress", {"stage": "summary", "message": "Profile summary"})
    return await asyncio.to_thread(
        summarize_profile, cv, query, ws.structured("orchestrator"), ws.memory, refresh
    )


def _shortlist(report: MatchReport, size: int) -> list[JobPosting]:
    ranked = sorted(
        [*report.matches, *report.below_threshold], key=lambda r: r.rank_key(), reverse=True
    )
    return [r.job for r in ranked[:size]]


def _enrich(
    shortlist: list[JobPosting], sources: list[JobSource], errors: dict[str, str]
) -> dict[str, JobPosting]:
    by_name = {s.name: s for s in sources if hasattr(s, "enrich")}
    out: dict[str, JobPosting] = {}
    for job in shortlist:
        src = by_name.get(job.source)
        if src is None or len(job.description) > 600:
            continue
        try:
            out[job.id] = src.enrich(job)
        except SourceError as exc:
            errors[f"enrich {job.id}"] = str(exc)
    return out


def _replace(jobs: list[JobPosting], enriched: dict[str, JobPosting]) -> list[JobPosting]:
    return [enriched.get(j.id, j) for j in jobs]
