"""Assistant controls for review queues, search history, learning and sources."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.registry import tool
from src.agents.runtime import AgentContext
from src.services import (
    calibration,
    company_discovery,
    enrichment,
    history,
    labels,
    learning,
    search_service,
)


class NoArgs(BaseModel):
    pass


@tool(
    "review_workspace",
    "Read search history, labels, outcome patterns and pending evidence.",
    NoArgs,
)
async def review_workspace(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    return {
        "history": [item.model_dump() for item in history.list_history(ctx.ws)],
        "labels": labels.review(ctx.ws).model_dump(exclude={"labels"}),
        "outcomes": calibration.review_outcomes(ctx.ws).model_dump(),
        "evidence": [
            {"id": item.id, "kind": item.kind, "text": item.text, "source": item.source}
            for item in enrichment.queue(ctx.ws)
        ],
        "learning": learning.state(ctx.ws).model_dump(),
    }


class EvidenceDecision(BaseModel):
    item_id: str
    accept: bool
    text: str | None = None


@tool(
    "decide_cv_fact",
    "Accept or reject a queued CV fact after the user reviews it.",
    EvidenceDecision,
)
async def decide_cv_fact(args: EvidenceDecision, ctx: AgentContext) -> dict[str, Any]:
    item = enrichment.decide(ctx.ws, args.item_id, args.accept, args.text)
    await ctx.emit("cv_updated", {"reason": "CV fact reviewed"})
    return {"id": item.id, "accepted": args.accept, "text": item.text}


class HistoryId(BaseModel):
    history_id: str = Field(description="Exact search history id from review_workspace")


@tool("open_search_history", "Show a previous search in the results panel.", HistoryId)
async def open_search_history(args: HistoryId, ctx: AgentContext) -> dict[str, Any]:
    _, outcome = history.open_entry(ctx.ws, args.history_id)
    await ctx.emit("search_results", {"outcome": outcome.model_dump(mode="json")})
    return {"history_id": args.history_id, "matches": len(outcome.report.matches)}


@tool("continue_search", "Finish screening the remaining jobs in a partial search.", HistoryId)
async def continue_search(args: HistoryId, ctx: AgentContext) -> dict[str, Any]:
    outcome = await search_service.continue_search(
        ctx.ws, args.history_id, ctx.emit, ctx.usage_sink("search")
    )
    await ctx.emit("search_results", {"outcome": outcome.model_dump(mode="json")})
    return {"matches": len(outcome.report.matches), "unscreened": outcome.unscreened}


class RecheckArgs(HistoryId):
    job_ids: list[str] | None = None
    description: str | None = None


@tool("recheck_postings", "Read and judge full postings in a prior search.", RecheckArgs)
async def recheck_postings(args: RecheckArgs, ctx: AgentContext) -> dict[str, Any]:
    outcome = await search_service.recheck_postings(
        ctx.ws,
        args.history_id,
        args.job_ids,
        args.description,
        ctx.emit,
        ctx.usage_sink("search"),
    )
    await ctx.emit("search_results", {"outcome": outcome.model_dump(mode="json")})
    return {"matches": len(outcome.report.matches), "to_check": len(outcome.report.to_check)}


class LearningDecision(BaseModel):
    preference_id: str
    accept: bool
    text: str | None = None


@tool("suggest_learning", "Suggest general profile preferences from Your call labels.", NoArgs)
async def suggest_learning(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    llm = ctx.ws.structured("quality", ctx.usage_sink("assistant"), "Learn from your labels")
    state = await asyncio.to_thread(learning.suggest, ctx.ws, llm)
    await ctx.emit("learning_updated", {})
    return state.model_dump()


@tool("decide_learning", "Accept or reject a proposed learned preference.", LearningDecision)
async def decide_learning(args: LearningDecision, ctx: AgentContext) -> dict[str, Any]:
    state = learning.decide(ctx.ws, args.preference_id, args.accept, args.text)
    await ctx.emit("learning_updated", {})
    return {"pending": len(state.pending), "accepted": len(state.accepted)}


class CompanyArgs(BaseModel):
    name: str
    url: str = Field(description="Website or careers URL when adding a company")


@tool("add_company", "Add a company career site to the watch list.", CompanyArgs)
async def add_company(args: CompanyArgs, ctx: AgentContext) -> dict[str, Any]:
    board = await asyncio.to_thread(
        company_discovery.add_company, ctx.ws.settings, args.name, args.url
    )
    await ctx.emit("companies_updated", {})
    return board.model_dump()


class CompanyName(BaseModel):
    name: str


@tool("remove_company", "Remove a company from the user's watch list.", CompanyName)
async def remove_company(args: CompanyName, ctx: AgentContext) -> dict[str, bool]:
    removed = company_discovery.remove_company(ctx.ws.settings, args.name)
    if not removed:
        raise ValueError("Company not in the watch list")
    await ctx.emit("companies_updated", {})
    return {"removed": True}


class DiscoveryArgs(BaseModel):
    mode: Literal["new", "stale", "all"] = "stale"


@tool("discover_companies", "Update known company job boards on explicit request.", DiscoveryArgs)
async def discover_companies(args: DiscoveryArgs, ctx: AgentContext) -> dict[str, Any]:
    status = await asyncio.to_thread(
        company_discovery.discover_companies, ctx.ws.settings, mode=args.mode
    )
    await ctx.emit("companies_updated", {})
    return status.model_dump()


class CVBasicsEdit(BaseModel):
    patch: dict[str, Any] = Field(
        description="Only existing basics fields: name, headline, email, phone, location, summary"
    )


@tool(
    "edit_cv_basics", "Correct the selected CV's existing contact details or summary.", CVBasicsEdit
)
async def edit_cv_basics(args: CVBasicsEdit, ctx: AgentContext) -> dict[str, Any]:
    from src.cv import master_cv_manager as mgr
    from src.services import cv_service

    allowed = {"name", "headline", "email", "phone", "location", "summary"}
    if not args.patch or set(args.patch) - allowed:
        raise ValueError("Only existing contact details and summary can be edited here")
    cv = await cv_service.ensure_selected_cv(ctx.ws, ctx.usage_sink("assistant"), role="cv")
    updated = mgr.update(cv, {"basics": args.patch})
    cv_service.save_selected_cv(ctx.ws, updated)
    await ctx.emit("cv_updated", {"reason": "CV details corrected"})
    return {"changed_fields": list(args.patch)}


class CVRoleEdit(BaseModel):
    role_id: str = Field(description="Existing role id from get_context")
    patch: dict[str, Any] = Field(
        description="Existing role's title, company, location, start or end"
    )


@tool(
    "edit_cv_role", "Correct an existing CV role's title, employer, location or dates.", CVRoleEdit
)
async def edit_cv_role(args: CVRoleEdit, ctx: AgentContext) -> dict[str, Any]:
    from src.cv.models import MasterCV
    from src.services import cv_service

    allowed = {"title", "company", "location", "start", "end"}
    if not args.patch or set(args.patch) - allowed:
        raise ValueError("Only existing role details can be corrected here")
    cv = await cv_service.ensure_selected_cv(ctx.ws, ctx.usage_sink("assistant"), role="cv")
    if not any(role.id == args.role_id for role in cv.experience):
        raise ValueError("Role id not found in the selected CV")
    data = cv.model_dump(mode="json")
    for role in data["experience"]:
        if role["id"] == args.role_id:
            role.update(args.patch)
    updated = MasterCV.model_validate(data)
    cv_service.save_selected_cv(ctx.ws, updated)
    await ctx.emit("cv_updated", {"reason": "CV role corrected"})
    return {"role_id": args.role_id, "changed_fields": list(args.patch)}


class TrackerManual(BaseModel):
    title: str
    company: str
    url: str | None = None
    note: str = ""


@tool("record_application", "Record an application made outside this app.", TrackerManual)
async def record_application(args: TrackerManual, ctx: AgentContext) -> dict[str, Any]:
    from src.services import tracker

    entry = tracker.add_application(ctx.ws, tracker.ManualApplication(**args.model_dump()))
    await ctx.emit("tracker_updated", {})
    return {"id": entry.id, "title": entry.title, "company": entry.company}


class TrackerEntryEdit(BaseModel):
    entry_id: str
    status: Literal["open", "applied", "na"]
    note: str | None = None
    reason: str | None = None
    stage: str | None = None


@tool(
    "edit_tracker_entry", "Edit a tracked role's status, note, reason or outcome.", TrackerEntryEdit
)
async def edit_tracker_entry(args: TrackerEntryEdit, ctx: AgentContext) -> dict[str, Any]:
    from pydantic import TypeAdapter

    from src.jobs.models import OutcomeStage
    from src.services import tracker

    stage = TypeAdapter(OutcomeStage).validate_python(args.stage) if args.stage else None
    entry = tracker.edit_entry(ctx.ws, args.entry_id, args.status, args.note, args.reason, stage)
    await ctx.emit("tracker_updated", {})
    return {"id": entry.id, "status": entry.status, "stage": entry.stage}


@tool("list_documents", "List saved tailored CVs and cover letters with their ids.", NoArgs)
async def list_documents(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    from src.services import cover_letters, tailored_documents

    return {
        "tailored_cvs": [
            {"id": item.id, "job_id": item.job_id, "filename": item.filename}
            for item in tailored_documents.list_all(ctx.ws)
        ],
        "cover_letters": [
            {"id": item.id, "job_id": item.job_id, "filename": item.filename}
            for item in cover_letters.list_all(ctx.ws)
        ],
    }


class TailoredEdit(BaseModel):
    document_id: str
    headline: str | None = None
    summary: str | None = None
    bullets: dict[str, str] = Field(default_factory=dict)


@tool("edit_tailored_cv", "Revise a tailored CV draft with source-fact guards.", TailoredEdit)
async def edit_tailored_cv(args: TailoredEdit, ctx: AgentContext) -> dict[str, Any]:
    from src.cv.models import TailoredCVEdits
    from src.services import tailored_documents

    document = tailored_documents.edit(
        ctx.ws,
        args.document_id,
        TailoredCVEdits(headline=args.headline, summary=args.summary, bullets=args.bullets),
    )
    await ctx.emit("documents_updated", {"job_id": document.job_id})
    return {
        "id": document.id,
        "filename": document.filename,
        "download_url": f"/api/files/cvs/{document.filename}",
    }


class LetterEdit(BaseModel):
    document_id: str
    greeting: str
    paragraphs: list[str]
    closing: str


@tool("edit_cover_letter", "Revise a saved cover letter with source-fact guards.", LetterEdit)
async def edit_cover_letter(args: LetterEdit, ctx: AgentContext) -> dict[str, Any]:
    from src.services import cover_letters

    document = cover_letters.edit(
        ctx.ws,
        args.document_id,
        cover_letters.LetterEdits(
            greeting=args.greeting, paragraphs=args.paragraphs, closing=args.closing
        ),
    )
    await ctx.emit("documents_updated", {"job_id": document.job_id})
    return {
        "id": document.id,
        "filename": document.filename,
        "download_url": f"/api/cover-letters/{document.id}/export/docx",
    }


class ProfileQuery(BaseModel):
    query_patch: dict[str, Any] = Field(default_factory=dict)


@tool(
    "build_profile", "Build or load a CV profile for the current search role family.", ProfileQuery
)
async def build_profile(args: ProfileQuery, ctx: AgentContext) -> dict[str, Any]:
    from src.cv import master_cv_manager as mgr
    from src.jobs.models import SearchQuery
    from src.services import cv_service

    cv = await cv_service.ensure_selected_cv(ctx.ws, ctx.usage_sink("assistant"), role="profile")
    query = SearchQuery.model_validate(mgr.merge_patch(ctx.query.model_dump(), args.query_patch))
    summary, cached = await search_service.get_summary(
        ctx.ws, cv, query, False, ctx.emit, ctx.usage_sink("assistant")
    )
    key = search_service.summary_key(ctx.ws, cv, query)
    await ctx.emit("profile_updated", {"key": key, "reason": "profile built"})
    return {"key": key, "headline": summary.headline, "from_memory": cached}


class DeleteCV(BaseModel):
    asset_id: str = Field(description="Exact CV id from get_context")


@tool("delete_cv", "Delete a CV and its profiles only after explicit user confirmation.", DeleteCV)
async def delete_cv(args: DeleteCV, ctx: AgentContext) -> dict[str, bool]:
    from src.services import cv_service

    cv_service.delete_cv(ctx.ws, args.asset_id)
    await ctx.emit("cv_selected", {"id": ctx.ws.active_cv_id or ""})
    return {"deleted": True}


class DeleteProfile(BaseModel):
    key: str


@tool(
    "delete_profile", "Delete a saved profile only after explicit user confirmation.", DeleteProfile
)
async def delete_profile(args: DeleteProfile, ctx: AgentContext) -> dict[str, bool]:
    from src.services import cv_service

    cv = cv_service.cached_selected_cv(ctx.ws)
    if cv is None:
        raise ValueError("No parsed CV selected")
    search_service.delete_profile(ctx.ws, cv, args.key)
    await ctx.emit("profile_updated", {"key": args.key, "reason": "profile deleted"})
    return {"deleted": True}


class DeleteHistory(BaseModel):
    history_id: str


@tool(
    "delete_search_history",
    "Delete one saved search only after explicit user confirmation.",
    DeleteHistory,
)
async def delete_search_history(args: DeleteHistory, ctx: AgentContext) -> dict[str, bool]:
    if not history.delete_entry(ctx.ws, args.history_id):
        raise ValueError("Search not in history")
    await ctx.emit("history_updated", {})
    return {"deleted": True}


class RemoveResult(BaseModel):
    job_id: str
    history_id: str | None = None


@tool("remove_search_result", "Remove a posting from current or past search results.", RemoveResult)
async def remove_search_result(args: RemoveResult, ctx: AgentContext) -> dict[str, bool]:
    if not search_service.remove_result(ctx.ws, args.job_id, args.history_id):
        raise ValueError("Job not in those results")
    await ctx.emit("result_removed", {"job_id": args.job_id})
    return {"removed": True}


class DeleteTracker(BaseModel):
    entry_id: str


@tool(
    "delete_tracker_entry",
    "Delete a tracker entry only after explicit user confirmation.",
    DeleteTracker,
)
async def delete_tracker_entry(args: DeleteTracker, ctx: AgentContext) -> dict[str, bool]:
    from src.services import tracker

    if not tracker.delete_entry(ctx.ws, args.entry_id):
        raise ValueError("Entry not in tracker")
    await ctx.emit("tracker_updated", {})
    return {"deleted": True}


class DeleteLetter(BaseModel):
    document_id: str


@tool(
    "delete_cover_letter",
    "Delete a saved cover letter only after explicit user confirmation.",
    DeleteLetter,
)
async def delete_cover_letter(args: DeleteLetter, ctx: AgentContext) -> dict[str, bool]:
    from src.services import cover_letters

    cover_letters.delete(ctx.ws, args.document_id)
    await ctx.emit("documents_updated", {})
    return {"deleted": True}


class LearnedPreferenceId(BaseModel):
    preference_id: str


@tool(
    "remove_learned_preference",
    "Remove a learned preference already in force.",
    LearnedPreferenceId,
)
async def remove_learned_preference(args: LearnedPreferenceId, ctx: AgentContext) -> dict[str, Any]:
    state = learning.remove(ctx.ws, args.preference_id)
    await ctx.emit("learning_updated", {})
    return {"accepted": len(state.accepted), "pending": len(state.pending)}


class ModelSettingsEdit(BaseModel):
    quality_model: str | None = None
    screening_model: str | None = None
    quality_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    screening_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


@tool(
    "change_app_models",
    "Change configured quality or screening models on the current provider.",
    ModelSettingsEdit,
)
async def change_app_models(args: ModelSettingsEdit, ctx: AgentContext) -> dict[str, Any]:
    from src.core.llm import available_models

    update = args.model_dump(exclude_none=True)
    if not update:
        raise ValueError("Name a model or effort to change")
    selected = {update[key] for key in ("quality_model", "screening_model") if key in update}
    if selected:
        available = {item.id for item in await asyncio.to_thread(available_models, ctx.ws.llm)}
        if unknown := selected - available:
            raise ValueError(
                f"Model not in this provider's catalogue: {', '.join(sorted(unknown))}"
            )
    config = ctx.ws.set_llm_config(update)
    await ctx.emit("models_updated", {})
    return {
        "provider": config.provider,
        "quality_model": config.quality_model,
        "screening_model": config.screening_model,
        "quality_effort": config.quality_effort,
        "screening_effort": config.screening_effort,
    }
