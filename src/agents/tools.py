"""The assistant's tools: thin wrappers over `src/services`, for updates the user asks for."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.agents.registry import tool
from src.agents.runtime import AgentContext
from src.cv import master_cv_manager as mgr
from src.cv.edits import CVEdit, apply_edits
from src.cv.models import MasterCV
from src.jobs.models import ProfileSummary
from src.jobs.profile_memory import ProfileRecord
from src.services import (
    company_discovery,
    cv_document,
    cv_service,
    enrichment,
    saved,
    search_service,
    tracker,
)
from src.services.intent import get_intent, patch_intent


class NoArgs(BaseModel):
    pass


def _profile_context(item: ProfileRecord) -> dict[str, Any]:
    # The whole summary: list patches replace lists, so the assistant must see every field.
    return {
        "key": item.key,
        "role_family": item.role_family,
        **item.summary.model_dump(mode="json", exclude_defaults=True),
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
        default_factory=dict,
        description="Merge patch for the saved ProfileSummary. Lists are replaced whole. "
        "Only change fields the user asked to edit.",
    )
    append: dict[str, list[Any]] = Field(
        default_factory=dict,
        description="Items to add to list fields, keeping what is there, e.g. "
        '{"target_roles": ["ML Platform Lead"], "achievements": ["..."]}',
    )
    reason: str = Field(description="What the user asked to change")


def _append(data: dict[str, Any], append: dict[str, list[Any]]) -> dict[str, Any]:
    for field, items in append.items():
        current = data.get(field) or []
        if not isinstance(current, list):
            raise ValueError(f"{field} is not a list; use patch to change it")
        data[field] = current + [item for item in items if item not in current]
    return data


@tool(
    "update_profile",
    "Edit a saved profile for the selected CV as the user asks. Use get_context to choose its "
    "exact key.",
    ProfileArgs,
)
async def update_profile(args: ProfileArgs, ctx: AgentContext) -> dict[str, Any]:
    cv = cv_service.cached_selected_cv(ctx.ws)
    if cv is None:
        raise ValueError("Read the selected CV and build a profile before editing it")
    if not args.patch and not args.append:
        raise ValueError("Give a patch or items to append")
    records = search_service.list_profiles(ctx.ws, cv)
    record = next((item for item in records if item.key == args.key), None)
    if record is None:
        raise ValueError("That profile is not available for the selected CV")
    data = mgr.merge_patch(record.summary.model_dump(mode="json"), args.patch)
    updated = ProfileSummary.model_validate(_append(data, args.append))
    saved = search_service.edit_profile(ctx.ws, cv, args.key, updated)
    await ctx.emit("profile_updated", {"key": saved.key, "reason": args.reason})
    return {"key": saved.key, "changed_fields": sorted({*args.patch, *args.append})}


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
    text: str = Field(description="The document text, copied verbatim")


@tool(
    "propose_cv_facts",
    "Read a long document the user pasted (report, portfolio, reference letter) and queue the "
    "CV additions it evidences for review in Profiles › Add evidence. For a direct instruction "
    "to change the CV, use edit_cv instead.",
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


@tool("read_cv", "The selected CV in full, with the ids of every role and bullet.", NoArgs)
async def read_cv(_: NoArgs, ctx: AgentContext) -> dict[str, Any]:
    cv = await cv_service.ensure_selected_cv(ctx.ws, ctx.usage_sink("assistant"))
    return cv.model_dump(mode="json", exclude={"preferences"})


class CVEditArgs(BaseModel):
    edits: list[CVEdit] = Field(
        min_length=1, description="The changes, in order, naming roles and bullets by id"
    )
    reason: str = Field(description="What the user asked for")


def _cv_lines(cv: MasterCV) -> set[str]:
    """The CV as one line per fact, to report what an edit changed."""
    lines = {f"{k}: {v}" for k, v in cv.basics.model_dump(exclude={"links"}).items() if v}
    lines |= {f"link: {link.label} {link.url}" for link in cv.basics.links}
    for role in cv.experience:
        lines.add(f"role: {role.title}, {role.company}, {role.start} to {role.end or 'now'}")
        lines |= {f"bullet ({role.company}): {b.text}" for b in role.bullets}
    lines |= {f"education: {e.degree}, {e.institution}" for e in cv.education}
    lines |= {f"skill ({g.category}): {item}" for g in cv.skills for item in g.items}
    lines |= {f"certification: {c.name}" for c in cv.certifications}
    lines |= {f"project: {p.name}: {p.description}" for p in cv.projects}
    return lines | {f"language: {lang}" for lang in cv.languages}


@tool(
    "edit_cv",
    "Change the selected CV: add a bullet to a role (at the end or after a given bullet), "
    "reword or remove a bullet, edit, add or remove a role, or set basics, education, skills, "
    "certifications, projects or languages. Only what an edit names changes. Bullet changes "
    "are also written to the CV's Word document (its copy in output/cvs).",
    CVEditArgs,
)
async def edit_cv(args: CVEditArgs, ctx: AgentContext) -> dict[str, Any]:
    cv = await cv_service.ensure_selected_cv(ctx.ws, ctx.usage_sink("assistant"))
    updated = apply_edits(cv, args.edits)
    before, after = _cv_lines(cv), _cv_lines(updated)
    cv_service.save_selected_cv(ctx.ws, updated)
    document = cv_document.sync_bullets(ctx.ws, cv, updated)
    await ctx.emit("cv_updated", {"reason": args.reason})
    return {
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "word_document": document.model_dump(exclude_defaults=True)
        or "no Word document changed (only bullet edits are written to a Word CV)",
    }
