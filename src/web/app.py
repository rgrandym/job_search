"""FastAPI app: REST for the UI forms, a WebSocket for the agent chat, and the built SPA.

Run:  uvicorn src.web.app:app --reload --port 8000     (UI dev server: cd web && npm run dev)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, SecretStr, ValidationError

from src.agents import chat
from src.agents.definitions import JOB_MATCHER_INFO
from src.agents.registry import AGENTS
from src.core.config import PROJECT_ROOT, LLMProviderName
from src.core.llm import LLMConfig, LLMError, available_models
from src.core.llm.catalog import ModelInfo
from src.core.llm.codex_backend import codex_status, start_login
from src.cv.models import MasterCV
from src.jobs.fetcher import ALL_SOURCES, build_sources
from src.jobs.models import SearchQuery
from src.services import cv_service
from src.services.search_service import SearchOutcome, SearchRequest, get_summary, run_search
from src.services.workspace import get_workspace

app = FastAPI(title="AI Job Search", version="0.1.0")
DIST = PROJECT_ROOT / "web" / "dist"


# ------------------------------------------------------------------ state & config


def _llm_view() -> dict[str, Any]:
    ws = get_workspace()
    cfg = ws.llm
    return cfg.model_dump(exclude={"api_key"}) | {
        "key_set": cfg.api_key is not None,
        "ready": ws.llm_ready(),
    }


@app.get("/api/state")
def state() -> dict[str, Any]:
    ws = get_workspace()
    sources, skipped = build_sources([*ALL_SOURCES, "demo"], settings=ws.settings)
    agents = [
        {"name": a.name, "description": a.description, "role": a.role} for a in AGENTS.values()
    ] + [JOB_MATCHER_INFO]
    cv = ws.master_cv
    return {
        "llm": _llm_view(),
        "cv": None
        if cv is None
        else {
            "name": cv.basics.name,
            "headline": cv.basics.headline,
            "roles": len(cv.experience),
            "skills": len(cv.all_skills()),
        },
        "sources": {"available": [s.name for s in sources], "skipped": skipped},
        "agents": agents,
        "defaults": {"threshold": ws.settings.score_threshold},
    }


class LLMUpdate(BaseModel):
    provider: LLMProviderName
    orchestrator_model: str
    worker_model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    api_key: str | None = None


@app.put("/api/llm")
def update_llm(body: LLMUpdate) -> dict[str, Any]:
    get_workspace().set_llm_config(body.model_dump(exclude={"api_key"}), body.api_key)
    return _llm_view()


@app.get("/api/llm/models")
def llm_models(provider: LLMProviderName) -> list[ModelInfo]:
    ws = get_workspace()
    cfg = LLMConfig.from_settings(ws.settings, provider)
    if key := ws.saved_key(provider):
        cfg = cfg.model_copy(update={"api_key": SecretStr(key)})
    try:
        return available_models(cfg)
    except Exception as exc:  # noqa: BLE001 - surface any provider failure to the UI
        raise HTTPException(502, f"Could not list {provider} models: {exc}") from exc


@app.get("/api/codex/status")
def get_codex_status() -> dict[str, Any]:
    return codex_status()


@app.post("/api/codex/login")
def codex_login() -> dict[str, Any]:
    """Open the ChatGPT sign-in page via `codex login` (the CLI stores the credentials)."""
    try:
        start_login()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"started": True}


# ------------------------------------------------------------------ CV


@app.get("/api/cv")
def get_cv() -> MasterCV | None:
    return get_workspace().master_cv


@app.put("/api/cv")
def put_cv(cv: MasterCV) -> MasterCV:
    get_workspace().save_master_cv(cv)
    return cv


@app.post("/api/cv/upload")
async def upload_cv(file: Annotated[UploadFile, File()]) -> MasterCV:
    ws = get_workspace()
    if not ws.llm_ready():
        raise HTTPException(400, "Configure an LLM provider first (Settings)")
    try:
        return await cv_service.import_cv(ws, file.filename or "cv.txt", await file.read())
    except (ValueError, ValidationError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc


class SummaryRequest(BaseModel):
    query: SearchQuery = SearchQuery()
    use_cv: bool = True
    refresh: bool = False


@app.post("/api/profile-summary")
async def profile_summary(body: SummaryRequest) -> dict[str, Any]:
    ws = get_workspace()
    if not ws.llm_ready():
        raise HTTPException(400, "Configure an LLM provider first (Settings)")
    cv = ws.master_cv if body.use_cv else None
    try:
        summary, cached = await get_summary(ws, cv, body.query, body.refresh)
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"summary": summary, "from_memory": cached}


# ------------------------------------------------------------------ search & jobs


@app.post("/api/search")
async def search(body: SearchRequest) -> SearchOutcome:
    return await run_search(get_workspace(), body)


class TailorRequest(BaseModel):
    template: Literal["classic", "modern", "compact"] = "classic"


@app.post("/api/jobs/{job_id}/tailor")
async def tailor_job(job_id: str, body: TailorRequest) -> dict[str, Any]:
    ws = get_workspace()
    job = ws.job(job_id)
    if job is None:
        raise HTTPException(404, "Job not in the last search")
    try:
        tailored, path = await cv_service.tailor_to_job(ws, job, body.template)
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "download_url": f"/api/files/{path.name}",
        "keyword_coverage": tailored.keyword_coverage,
        "missing_keywords": tailored.missing_keywords,
        "rejected": sum(not c.accepted for c in tailored.changes),
    }


@app.get("/api/files/{name}")
def download(name: str) -> FileResponse:
    out = get_workspace().output_dir.resolve()
    path = (out / name).resolve()
    if path.parent != out or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=path.name)


# ------------------------------------------------------------------ agent chat


@app.websocket("/api/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    ws = get_workspace()
    session = chat.get_session(websocket.query_params.get("session"))
    await websocket.send_json({"type": "session", "session_id": session.id})
    run: asyncio.Task[None] | None = None

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        await websocket.send_json({"type": kind, **payload})

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "cancel":
                chat.cancel(session)
            elif kind == "reset":
                session.messages.clear()
            elif kind == "user_message":
                if run and not run.done():
                    await emit("error", {"message": "A run is already in progress"})
                    continue
                if not ws.llm_ready():
                    await emit("error", {"message": "Configure an LLM provider first (Settings)"})
                    continue
                query = SearchQuery.model_validate(msg.get("filters") or {})
                run = asyncio.create_task(
                    chat.handle_user_message(
                        ws,
                        session,
                        str(msg.get("text", "")),
                        query,
                        bool(msg.get("use_cv", True)),
                        emit,
                    )
                )
    except WebSocketDisconnect:
        chat.cancel(session)


# ------------------------------------------------------------------ SPA

if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        target = (DIST / path).resolve()
        if path and target.is_file() and DIST.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(Path(DIST / "index.html"))
