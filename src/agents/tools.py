"""The assistant's tools: thin wrappers over `src/services`, for updates the user asks for."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.agents.registry import tool
from src.agents.runtime import AgentContext
from src.cv import master_cv_manager as mgr
from src.jobs.models import ProfileSummary
from src.jobs.profile_memory import ProfileRecord
from src.services import company_discovery, cv_service, enrichment, saved, search_service, tracker
from src.services.intent import get_intent, patch_intent


class NoArgs(BaseModel):
    pass


def _profile_context(item: ProfileRecord) -> dict[str, Any]:
    return {
        "key": item.key,
        "role_family": item.role_family,
        "headline": item.summary.headline,
        "summary": item.summary.summary,
        "target_roles": item.summary.target_roles,
        "stretch_roles": item.summary.stretch_roles,
        "not_a_fit": item.summary.not_a_fit,
        "search_keywords": item.summary.search_keywords,
        "role_families": [
            {"name": family.name, "titles": family.titles, "tier": family.tier}
            for family in item.summary.role_families
        ],
    }


@tool(
    "get_context",
    "Selected CV, profiles, current and saved jobs, tracker, preferences and career intent.",
    NoArgs,
)
async def get_context(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    ws = ctx.ws
    assets = cv_service.list_cvs(ws)
    cv = cv_service.cached_selected_cv(ws)
    profiles = search_service.list_profiles(ws, cv) if cv is not None else []
    report = ws.last_report
    return {
        "cv_selected": ws.active_cv_id is not None,
        "cv_library": [
            {"id": item.id, "filename": item.filename, "selected": item.selected} for item in assets
        ],
        "cv": None
        if cv is None
        else {
            "name": cv.basics.name,
            "roles": [
                {"id": e.id, "title": e.title, "company": e.company, "start": e.start}
                for e in cv.experience
            ],
            "preferences": cv.preferences.model_dump(),
        },
        "profiles": [_profile_context(item) for item in profiles],
        "career_intent": get_intent(ws).model_dump(exclude_defaults=True),
        "current_search": {
            "filters": ctx.query.model_dump(exclude_defaults=True),
            "jobs": [
                {"id": item.job.id, "title": item.job.title, "company": item.job.company}
                for item in report.all_results()[:15]
            ]
            if report
            else [],
            "total": len(report.all_results()) if report else 0,
        },
        "saved_jobs": [
            {
                "id": item.result.job.id,
                "title": item.result.job.title,
                "company": item.result.job.company,
            }
            for item in saved.list_saved(ws)[:15]
        ],
        "tracked_jobs": [
            {
                "id": item.id,
                "job_id": item.job_id,
                "title": item.title,
                "company": item.company,
                "status": item.status,
                "stage": item.stage,
                "note": item.note,
            }
            for item in tracker.register(ws)[:15]
        ],
        "watched_companies": [
            {"name": item.name, "url": item.url}
            for item in company_discovery.your_companies(ws.settings)[:15]
        ],
    }


class ProfileArgs(BaseModel):
    key: str = Field(description="Exact key of a profile returned by get_context")
    patch: dict[str, Any] = Field(
        description="Merge patch for the saved ProfileSummary. Lists are replaced whole. "
        "Only change fields the user explicitly asked to edit."
    )
    reason: str = Field(description="What the user asked to change")


@tool(
    "update_profile",
    "Edit a saved profile for the selected CV. Use get_context to choose its exact key.",
    ProfileArgs,
)
async def update_profile(args: ProfileArgs, ctx: AgentContext) -> dict[str, Any]:
    cv = cv_service.cached_selected_cv(ctx.ws)
    if cv is None:
        raise ValueError("Read the selected CV and build a profile before editing it")
    records = search_service.list_profiles(ctx.ws, cv)
    record = next((item for item in records if item.key == args.key), None)
    if record is None:
        raise ValueError("That profile is not available for the selected CV")
    updated = ProfileSummary.model_validate(
        mgr.merge_patch(record.summary.model_dump(mode="json"), args.patch)
    )
    saved = search_service.edit_profile(ctx.ws, cv, args.key, updated)
    await ctx.emit("profile_updated", {"key": saved.key, "reason": args.reason})
    return {"key": saved.key, "changed_fields": list(args.patch)}


class IntentArgs(BaseModel):
    patch: dict[str, Any] = Field(
        description="RFC 7386 merge patch for the career intent. Lists are replaced whole: to "
        "add an item, send the current list plus the new one. Fields: direction, "
        "energising_work, avoid_work, target_areas, preferred_sectors, avoided_sectors, "
        "organisation_types, soft_dealbreakers, languages [{language, level: basic|"
        "conversational|professional|native}], eligibility"
    )
    reason: str = Field(description="What the user said, in their words")


@tool(
    "update_search_intent",
    "Record what the user wants from their next role, only from what they said. Returns the "
    "changes.",
    IntentArgs,
)
async def update_search_intent(args: IntentArgs, ctx: AgentContext) -> dict[str, Any]:
    intent, changes = patch_intent(ctx.ws, args.patch)
    if changes:
        await ctx.emit("intent_updated", {"intent": intent.model_dump(), "changes": changes})
    return {"changes": changes or ["nothing changed"]}


class PreferencesArgs(BaseModel):
    patch: dict[str, Any] = Field(
        description="Merge patch for the CV's search preferences: target_titles, locations, "
        "work_arrangements (remote|hybrid|onsite), willing_to_relocate, min_salary. Lists are "
        "replaced whole."
    )
    reason: str = Field(description="What the user said, in their words")


@tool(
    "update_preferences",
    "Change the selected CV's search preferences (never printed on the CV).",
    PreferencesArgs,
)
async def update_preferences(args: PreferencesArgs, ctx: AgentContext) -> dict[str, Any]:
    cv = await cv_service.ensure_selected_cv(ctx.ws, ctx.usage_sink("assistant"))
    updated = mgr.update(cv, {"preferences": args.patch})
    cv_service.save_selected_cv(ctx.ws, updated)
    await ctx.emit("cv_updated", {"reason": args.reason})
    return {"preferences": updated.preferences.model_dump()}


class FactsArgs(BaseModel):
    text: str = Field(description="The user's own words stating the facts, copied verbatim")


@tool(
    "propose_cv_facts",
    "Turn facts the user stated into proposed CV additions (skills, certifications, projects, "
    "achievements). They wait for the user to accept them in Profiles › Add evidence.",
    FactsArgs,
)
async def propose_cv_facts(args: FactsArgs, ctx: AgentContext) -> dict[str, Any]:
    queued = await enrichment.propose(
        ctx.ws, "assistant chat", args.text, ctx.usage_sink("assistant")
    )
    if queued:
        await ctx.emit("evidence_proposed", {"count": len(queued)})
    return {
        "proposed": [{"kind": q.kind, "text": q.text, "role": q.attach_to} for q in queued],
        "note": "Waiting for the user to accept them in Profiles › Add evidence"
        if queued
        else "Nothing new: the CV already says this, or no fact could be quoted exactly",
    }
