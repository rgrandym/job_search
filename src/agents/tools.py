"""The assistant's tools: thin wrappers over `src/services`, for updates the user asks for."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.agents.registry import tool
from src.agents.runtime import AgentContext
from src.cv import master_cv_manager as mgr
from src.services import cv_service, enrichment
from src.services.intent import get_intent, patch_intent


class NoArgs(BaseModel):
    pass


@tool(
    "get_context",
    "The selected CV's roles (with ids), search preferences and career intent.",
    NoArgs,
)
async def get_context(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    ws = ctx.ws
    cv = cv_service.cached_selected_cv(ws)
    return {
        "cv_selected": ws.active_cv_id is not None,
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
        "career_intent": get_intent(ws).model_dump(exclude_defaults=True),
    }


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
