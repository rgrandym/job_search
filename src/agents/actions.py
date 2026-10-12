"""Assistant actions backed by the same services as the app's buttons."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.registry import tool
from src.agents.runtime import AgentContext
from src.cv import master_cv_manager as mgr
from src.jobs.models import SearchQuery
from src.services import cv_service, labels, saved, search_service, tailored_documents, tracker
from src.services.search_service import AreaThresholds, SearchRequest, widen_note


class SearchArgs(BaseModel):
    query_patch: dict[str, Any] = Field(
        default_factory=dict,
        description="Changes to the current sidebar search filters; omit to use them as shown",
    )
    smart: bool | None = None
    threshold: float | None = None
    widen: bool | None = Field(
        None, description="Also search adjacent role families; applies only with titles empty"
    )
    area_thresholds: AreaThresholds | None = Field(
        None, description="Listing threshold per role area: progression, adjacent"
    )
    profile_key: str | None = None


class FilterArgs(BaseModel):
    patch: dict[str, Any] = Field(description="Merge patch for the current sidebar SearchQuery")


@tool("set_search_filters", "Change the search filters shown in the sidebar.", FilterArgs)
async def set_search_filters(args: FilterArgs, ctx: AgentContext) -> dict[str, Any]:
    query = SearchQuery.model_validate(mgr.merge_patch(ctx.query.model_dump(), args.patch))
    ctx.query = query
    await ctx.emit("search_filters_updated", {"query": query.model_dump(mode="json")})
    return {"filters": query.model_dump(exclude_defaults=True)}


@tool(
    "search_jobs", "Run a job search using the current filters, with optional changes.", SearchArgs
)
async def search_jobs(args: SearchArgs, ctx: AgentContext) -> dict[str, Any]:
    query = SearchQuery.model_validate(mgr.merge_patch(ctx.query.model_dump(), args.query_patch))
    if args.query_patch:
        ctx.query = query
        await ctx.emit("search_filters_updated", {"query": query.model_dump(mode="json")})

    async def progress(stage: str, payload: dict[str, Any]) -> None:
        await ctx.emit(stage, payload)

    request = SearchRequest(
        query=query,
        use_cv=ctx.use_cv,
        smart=args.smart if args.smart is not None else ctx.smart,
        threshold=args.threshold if args.threshold is not None else ctx.threshold,
        widen=args.widen if args.widen is not None else ctx.widen,
        area_thresholds=(
            args.area_thresholds if args.area_thresholds is not None else ctx.area_thresholds
        ),
        profile_key=args.profile_key if args.profile_key is not None else ctx.profile_key,
        run_id=uuid.uuid4().hex,
    )
    ctx.active_search_id = request.run_id
    try:
        outcome = await search_service.run_search(
            ctx.ws, request, progress, ctx.usage_sink("search")
        )
    finally:
        ctx.active_search_id = None
    await ctx.emit("search_results", {"outcome": outcome.model_dump(mode="json")})
    return {
        "matches": [
            {
                "id": item.job.id,
                "title": item.job.title,
                "company": item.job.company,
                "fit": item.verdict.fit_score if item.verdict else item.rank_key(),
            }
            for item in outcome.report.matches[:20]
        ],
        "match_count": len(outcome.report.matches),
        "to_check": len(outcome.report.to_check),
        "history_id": outcome.history_id,
        "errors": outcome.errors,
        **({"note": note} if (note := widen_note(ctx.ws, request)) else {}),
    }


class JobLookupArgs(BaseModel):
    text: str = Field("", description="Part of a job title, employer, or job id")


@tool("find_jobs", "Find job ids in the current results and saved jobs.", JobLookupArgs)
async def find_jobs(args: JobLookupArgs, ctx: AgentContext) -> list[dict[str, Any]]:
    report = ctx.ws.last_report
    jobs = [item.job for item in report.all_results()] if report else []
    jobs += [item.result.job for item in saved.list_saved(ctx.ws)]
    needle = args.text.casefold().strip()
    seen: set[str] = set()
    found = []
    for job in jobs:
        if job.id in seen or needle not in f"{job.id} {job.title} {job.company}".casefold():
            continue
        seen.add(job.id)
        found.append({"id": job.id, "title": job.title, "company": job.company})
        if len(found) >= 30:
            break
    return found


class JobIdsArgs(BaseModel):
    job_ids: list[str] = Field(description="Exact ids from find_jobs or search results")


@tool("save_jobs", "Save jobs from the current search for later.", JobIdsArgs)
async def save_jobs(args: JobIdsArgs, ctx: AgentContext) -> dict[str, Any]:
    items = saved.save(ctx.ws, args.job_ids)
    await ctx.emit("saved_updated", {"count": len(items)})
    return {"saved": [item.result.job.id for item in items]}


@tool("remove_saved_jobs", "Remove jobs from Saved; tracker history remains.", JobIdsArgs)
async def remove_saved_jobs(args: JobIdsArgs, ctx: AgentContext) -> dict[str, int]:
    count = saved.remove(ctx.ws, args.job_ids)
    await ctx.emit("saved_updated", {"count": -count})
    return {"removed": count}


class TrackingArgs(BaseModel):
    job_id: str
    status: Literal["open", "applied", "na"]
    note: str | None = None
    reason: str | None = None


@tool("track_job", "Set a job's status or note in the application tracker.", TrackingArgs)
async def track_job(args: TrackingArgs, ctx: AgentContext) -> dict[str, Any]:
    job = ctx.ws.job(args.job_id)
    if job is None:
        raise ValueError("Job not found in results or saved jobs")
    tracking = tracker.set_status(ctx.ws, job, args.status, args.note, reason=args.reason)
    tracker.update_result(ctx.ws, job.id, tracking)
    await ctx.emit("tracking_updated", {"job_id": job.id, "tracking": tracking.model_dump()})
    return {"job_id": job.id, "tracking": tracking.model_dump()}


class LabelArgs(BaseModel):
    job_id: str
    label: Literal["yes", "maybe", "no"] | None


@tool("label_job", "Set Your call on a job in the current search.", LabelArgs)
async def label_job(args: LabelArgs, ctx: AgentContext) -> dict[str, Any]:
    if not labels.set_label(ctx.ws, args.job_id, args.label):
        raise ValueError("Job not in the current search")
    await ctx.emit("label_updated", {"job_id": args.job_id, "label": args.label})
    return {"job_id": args.job_id, "label": args.label}


class DocumentArgs(BaseModel):
    job_id: str = Field(description="Exact job id from find_jobs or search results")
    template: Literal["original", "classic", "modern", "compact"] = Field(
        "original", description="original: the CV's own Word design (use unless asked)"
    )
    length: Literal["auto", "full", "junior"] = Field(
        "auto",
        description="full: keep every line; junior: trim for a junior role; auto: trim only "
        "when the job is clearly more junior",
    )
    audience: Literal["auto", "industry", "academic"] = Field(
        "auto",
        description="academic: arrange and word the CV for an academic post; auto: when the "
        "job is one",
    )
    level: Literal["auto", "senior", "junior"] = Field(
        "auto",
        description="senior / junior: lean the presentation more or less senior (titles and "
        "facts unchanged); each choice makes another version to compare",
    )


@tool("tailor_cv", "Create a guarded, reviewed Word CV for a result or saved job.", DocumentArgs)
async def tailor_cv(args: DocumentArgs, ctx: AgentContext) -> dict[str, Any]:
    job = ctx.ws.job(args.job_id)
    if job is None:
        raise ValueError("Job not found in results or saved jobs")
    tailored, path = await cv_service.tailor_to_job(
        ctx.ws,
        job,
        args.template,
        ctx.usage_sink("assistant"),
        ctx.ws.result(job.id),
        level=args.level,
        length=args.length,
        audience=args.audience,
    )
    await ctx.emit("documents_updated", {"job_id": job.id})
    return {
        "document_id": tailored.document_id,
        "filename": path.name,
        "download_url": f"/api/files/cvs/{path.name}",
        "missing_keywords": tailored.missing_keywords,
        "headline_kept": tailored.cv.basics.headline,
        "headline_options": tailored.headline_options,
        "trim": tailored.trim.model_dump() if tailored.trim else None,
        "document_notes": tailored.document_notes,
        "rejections": [change.reason for change in tailored.changes if not change.accepted],
        "positioning": tailored.positioning.model_dump() if tailored.positioning else None,
        "gaps": [r.requirement for r in tailored.requirements if r.match == "not_evidenced"],
        "fit_check": tailored.fit_check.model_dump() if tailored.fit_check else None,
    }


@tool(
    "write_cover_letter",
    "Create a guarded Word cover letter for a result or saved job.",
    DocumentArgs,
)
async def write_cover_letter(args: DocumentArgs, ctx: AgentContext) -> dict[str, Any]:
    job = ctx.ws.job(args.job_id)
    if job is None:
        raise ValueError("Job not found in results or saved jobs")
    # The newest reviewed tailored CV for this job, as the app uses by default.
    drafts = [d for d in tailored_documents.list_for_job(ctx.ws, job.id) if d.reviewed]
    letter, path = await cv_service.write_cover_letter(
        ctx.ws, job, args.template, ctx.usage_sink("assistant"), drafts[0].id if drafts else None
    )
    await ctx.emit("documents_updated", {"job_id": job.id})
    return {
        "filename": path.name,
        "download_url": f"/api/files/cover_letters/{path.name}",
        "paragraphs": len(letter.paragraphs),
        "to_review": [
            f"paragraph {flag.paragraph + 1}: {flag.reason} (shown in red)" for flag in letter.flags
        ],
        "notes": letter.notes,
    }


class CVChoiceArgs(BaseModel):
    asset_id: str = Field(description="Exact CV id from get_context")


@tool("select_cv", "Select a CV from the user's library.", CVChoiceArgs)
async def select_cv(args: CVChoiceArgs, ctx: AgentContext) -> dict[str, Any]:
    asset = cv_service.select_cv(ctx.ws, args.asset_id)
    await ctx.emit("cv_selected", {"id": asset.id})
    return {"selected": asset.filename, "id": asset.id}


class ProfileKeyArgs(BaseModel):
    key: str = Field(description="Exact saved profile key from get_context")


@tool("refresh_profile", "Rebuild a saved profile from the current CV and intent.", ProfileKeyArgs)
async def refresh_profile(args: ProfileKeyArgs, ctx: AgentContext) -> dict[str, Any]:
    cv = await cv_service.ensure_selected_cv(ctx.ws, ctx.usage_sink("assistant"), role="profile")
    record = await search_service.refresh_profile(ctx.ws, cv, args.key, ctx.usage_sink("assistant"))
    await ctx.emit("profile_updated", {"key": record.key, "reason": "rebuilt from the CV"})
    return {"key": record.key, "headline": record.summary.headline}


class TemplateArgs(BaseModel):
    template: Literal["original", "classic", "modern", "compact"] = Field(
        "original", description="original: the CV's own Word design (use unless asked)"
    )


@tool("export_general_cv", "Export the selected CV as a general Word document.", TemplateArgs)
async def export_general_cv(args: TemplateArgs, ctx: AgentContext) -> dict[str, Any]:
    path, roles = await cv_service.export_general_cv(
        ctx.ws, args.template, ctx.usage_sink("assistant")
    )
    await ctx.emit("documents_updated", {})
    return {"filename": path.name, "download_url": f"/api/files/cvs/{path.name}", "roles": roles}
