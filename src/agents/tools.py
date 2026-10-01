"""Agent tools. Thin wrappers over `src/services`: agents and UI buttons run the same code."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.registry import tool
from src.agents.runtime import AgentContext
from src.cv import master_cv_manager as mgr
from src.cv.models import WorkArrangement
from src.jobs.fetcher import ALL_SOURCES, build_sources
from src.jobs.models import MatchResult
from src.services import cv_service
from src.services.search_service import SearchRequest, get_summary, run_search


class NoArgs(BaseModel):
    pass


def digest(r: MatchResult) -> dict[str, Any]:
    """Compact view of one result for the model's context."""
    j, v, s = r.job, r.verdict, r.score
    out: dict[str, Any] = {
        "job_id": j.id,
        "title": j.title,
        "company": j.company,
        "location": j.location,
        "arrangement": j.work_arrangement,
        "salary": j.salary_range,
        "url": j.url,
    }
    if v:
        out |= {
            "fit_score": v.fit_score,
            "verdict": v.verdict,
            "match": v.match,
            "reasons": v.reasons[:3],
            "gaps": v.gaps[:3],
            "dealbreakers": v.dealbreakers,
        }
    if s:
        out["prefilter_score"] = s.total
    if r.exclusion_reasons:
        out["excluded_because"] = r.exclusion_reasons
    return out


# ------------------------------------------------------------------ orchestrator


@tool("get_workspace_state", "Current CV status, UI search filters, LLM and last search.", NoArgs)
async def get_workspace_state(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    ws, cv = ctx.ws, ctx.ws.master_cv
    rep = ws.last_report
    return {
        "cv_selected": ws.active_cv_id is not None,
        "selected_cv": ws.active_cv_id,
        "cv_loaded": cv is not None,
        "cv": None
        if cv is None
        else {
            "name": cv.basics.name,
            "headline": cv.basics.headline,
            "roles": [f"{e.title} @ {e.company}" for e in cv.experience[:4]],
        },
        "match_against_cv": ctx.use_cv,
        "ui_filters": ctx.query.model_dump(exclude_defaults=True),
        "last_search": None
        if rep is None
        else {
            "matches": len(rep.matches),
            "below": len(rep.below_threshold),
            "excluded": len(rep.excluded),
            "screened": rep.screened,
        },
    }


class SummarizeArgs(BaseModel):
    titles: list[str] | None = Field(
        None, description="Role family to orient the summary to; defaults to the UI filter titles"
    )
    refresh: bool = Field(False, description="Rebuild even if a summary is in memory")


@tool(
    "summarize_profile",
    "Build (or recall from memory) the accurate profile summary used to screen jobs. "
    "Same CV + same role family returns the remembered summary.",
    SummarizeArgs,
)
async def summarize_profile_tool(args: SummarizeArgs, ctx: AgentContext) -> dict[str, Any]:
    query = ctx.query.model_copy(update={"titles": args.titles}) if args.titles else ctx.query
    cv = await cv_service.ensure_selected_cv(ctx.ws) if ctx.use_cv else None
    summary, cached = await get_summary(ctx.ws, cv, query, args.refresh, ctx.emit)
    await ctx.emit("profile_summary", {"summary": summary.model_dump(), "from_memory": cached})
    return {"from_memory": cached, "summary": summary.model_dump()}


class DelegateArgs(BaseModel):
    agent: Literal["job_search_expert", "cv_expert"]
    task: str = Field(description="Self-contained instructions: goal, constraints, what to return")


@tool("delegate", "Hand a task to a specialist subagent and get its answer back.", DelegateArgs)
async def delegate(_: DelegateArgs, __: AgentContext) -> str:  # intercepted by the runtime
    raise NotImplementedError


# ------------------------------------------------------------------ job_search_expert


class SearchArgs(BaseModel):
    """Overrides on top of the user's UI filters. Omit a field to keep the UI value."""

    titles: list[str] | None = None
    keywords: list[str] | None = None
    locations: list[str] | None = None
    distance_miles: int | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    work_arrangements: list[WorkArrangement] | None = None
    sources: list[str] | None = Field(None, description=f"Any of {[*ALL_SOURCES, 'demo']}")
    smart: bool = Field(True, description="Screen the shortlist with job_matcher")
    threshold: float | None = None
    top: int = Field(10, ge=1, le=50, description="How many top matches to return")


@tool(
    "search_jobs",
    "Capture postings from the sources, pre-filter on hard constraints, then have the "
    "job_matcher subagent judge the shortlist against the profile summary. Updates the UI.",
    SearchArgs,
)
async def search_jobs(args: SearchArgs, ctx: AgentContext) -> dict[str, Any]:
    overrides = args.model_dump(exclude_none=True, exclude={"smart", "threshold", "top"})
    query = ctx.query.model_copy(update=overrides)
    req = SearchRequest(query=query, use_cv=ctx.use_cv, smart=args.smart, threshold=args.threshold)
    out = await run_search(ctx.ws, req, ctx.emit)
    rep = out.report
    return {
        "fetched": out.fetched,
        "matches": len(rep.matches),
        "below_threshold": len(rep.below_threshold),
        "excluded": len(rep.excluded),
        "screened_by_job_matcher": rep.screened,
        "summary_from_memory": out.summary_from_memory,
        "smart_unavailable": out.smart_unavailable,
        "top_matches": [digest(r) for r in rep.matches[: args.top]],
        "best_rejected": [digest(r) for r in rep.below_threshold[:3]],
        "source_errors": out.errors,
        "skipped_sources": out.skipped_sources,
    }


class ResultsArgs(BaseModel):
    bucket: Literal["matches", "below_threshold", "excluded"] = "matches"
    offset: int = Field(0, ge=0)
    limit: int = Field(10, ge=1, le=50)


@tool("get_results", "Page through the last search's results.", ResultsArgs)
async def get_results(args: ResultsArgs, ctx: AgentContext) -> dict[str, Any]:
    rep = ctx.ws.last_report
    if rep is None:
        return {"error": "no search has been run yet"}
    items: list[MatchResult] = getattr(rep, args.bucket)
    page = items[args.offset : args.offset + args.limit]
    return {"total": len(items), "items": [digest(r) for r in page]}


class JobArgs(BaseModel):
    job_id: str


@tool("get_job", "Full posting, pre-filter score and job_matcher verdict for one job.", JobArgs)
async def get_job(args: JobArgs, ctx: AgentContext) -> dict[str, Any]:
    rep = ctx.ws.last_report
    r = next((r for r in rep.all_results() if r.job.id == args.job_id), None) if rep else None
    if r is None:
        raise ValueError(f"job {args.job_id!r} not in the last search")
    return r.model_dump(mode="json", exclude_none=True)


@tool("list_sources", "Which job sources are configured, and why others are skipped.", NoArgs)
async def list_sources(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    sources, skipped = build_sources([*ALL_SOURCES, "demo"], settings=ctx.ws.settings)
    return {"available": [s.name for s in sources], "skipped": skipped}


# ------------------------------------------------------------------ cv_expert


@tool("get_master_cv", "The user's Master CV (JSON), or null if none is loaded.", NoArgs)
async def get_master_cv(_: NoArgs, ctx: AgentContext) -> Any:
    cv = await cv_service.ensure_selected_cv(ctx.ws)
    return cv.model_dump(mode="json", exclude_none=True)


class TailorArgs(BaseModel):
    job_id: str
    template: Literal["classic", "modern", "compact"] = "classic"


@tool(
    "tailor_cv",
    "Tailor the Master CV to a job from the last search (guarded: no fabricated facts) and "
    "export a .docx. Returns keyword coverage, missing keywords and rejected rewrites.",
    TailorArgs,
)
async def tailor_cv(args: TailorArgs, ctx: AgentContext) -> dict[str, Any]:
    job = ctx.ws.job(args.job_id)
    if job is None:
        raise ValueError(f"job {args.job_id!r} not in the last search")
    tailored, path = await cv_service.tailor_to_job(ctx.ws, job, args.template)
    url = f"/api/files/{path.name}"
    await ctx.emit("file_ready", {"name": path.name, "url": url, "job_id": job.id})
    return {
        "download_url": url,
        "keyword_coverage": round(tailored.keyword_coverage, 2),
        "missing_keywords": tailored.missing_keywords,
        "rejected_rewrites": [
            {"bullet": c.source_id, "reason": c.reason} for c in tailored.changes if not c.accepted
        ],
    }


class UpdateCVArgs(BaseModel):
    patch: dict[str, Any] = Field(description="RFC 7386 merge patch for the Master CV JSON")
    reason: str = Field(description="What the user asked to change")


@tool(
    "update_master_cv",
    "Apply a merge patch to the Master CV. Only for facts the user explicitly stated.",
    UpdateCVArgs,
)
async def update_master_cv(args: UpdateCVArgs, ctx: AgentContext) -> dict[str, Any]:
    if ctx.ws.master_cv is None:
        raise ValueError("No Master CV loaded")
    cv_service.save_selected_cv(ctx.ws, mgr.update(ctx.ws.master_cv, args.patch))
    await ctx.emit("cv_updated", {"reason": args.reason})
    return {"ok": True}
