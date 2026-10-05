"""FastAPI app: REST for the UI forms, a WebSocket for the agent chat, and the built SPA.

Run:  uvicorn src.web.app:app --reload --port 8000     (UI dev server: cd web && npm run dev)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from src.agents import chat
from src.core.config import PROJECT_ROOT, LLMProviderName
from src.core.llm import LLMConfig, LLMError, ModelUsage, available_models, claude_code_backend
from src.core.llm.catalog import ModelInfo
from src.core.llm.codex_backend import codex_status, codex_usage, start_login
from src.cv.models import MasterCV, TailoredCVEdits, TailoredDocument
from src.jobs.fetcher import SELECTABLE_SOURCES, SOURCE_CATALOG, build_sources
from src.jobs.models import (
    JobPosting,
    JobTracking,
    MatchResult,
    OutcomeStage,
    ProfileSummary,
    SavedJob,
    SearchIntent,
    SearchQuery,
)
from src.jobs.profile_memory import ProfileRecord
from src.jobs.sources.base import SourceError
from src.jobs.sources.companies import CompanyBoard
from src.jobs.sources.gmail_alerts import GmailAuth
from src.services import (
    calibration,
    company_discovery,
    cover_letters,
    cv_service,
    enrichment,
    history,
    intent,
    labels,
    learning,
    saved,
    search_service,
    tailored_documents,
    tracker,
)
from src.services.search_service import SearchOutcome, SearchRequest, get_summary, run_search
from src.services.workspace import Workspace, get_workspace

app = FastAPI(title="AI Job Search", version="0.1.0")
log = logging.getLogger(__name__)
DIST = PROJECT_ROOT / "web" / "dist"
_GMAIL_PENDING: dict[str, tuple[str, float]] = {}


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
    sources, skipped = build_sources(
        [name for name in SELECTABLE_SOURCES if name != "gmail_alerts"], settings=ws.settings
    )
    available = [s.name for s in sources]
    gmail = GmailAuth(ws.settings)
    if gmail.connected:  # the CV it is tracked against is chosen at search time
        available.append("gmail_alerts")
    else:
        skipped["gmail_alerts"] = (
            "Click Connect Gmail alerts"
            if gmail.configured
            else "Set the Gmail OAuth credentials and account in .env"
        )
    cv = ws.master_cv
    cv_files = cv_service.list_cvs(ws)
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
        "cv_files": {
            "available": [item.model_dump() for item in cv_files],
            "selected": ws.active_cv_id,
        },
        "sources": {
            "available": available,
            "skipped": skipped,
            "catalog": [info.model_dump() for info in SOURCE_CATALOG],
        },
        "defaults": {"threshold": ws.settings.score_threshold},
    }


@app.get("/api/gmail/status")
def gmail_status() -> dict[str, Any]:
    """Report whether the dedicated Gmail account is ready, without exposing tokens."""
    auth = GmailAuth(get_workspace().settings)
    return {
        "configured": auth.configured,
        "connected": auth.connected,
        "account": auth.settings.gmail_account,
    }


_DISCOVERY_TASKS: set[asyncio.Task[None]] = set()


@app.get("/api/companies/status")
def companies_status() -> dict[str, Any]:
    """Company job boards found so far, and the progress of a running discovery pass."""
    return company_discovery.status(get_workspace().settings).model_dump(mode="json")


@app.get("/api/companies/boards")
def company_boards() -> list[company_discovery.BoardView]:
    """Every company job board, so single boards can be switched off for searches."""
    return company_discovery.list_boards(get_workspace().settings)


class CompanyRequest(BaseModel):
    name: str
    url: str  # website, careers page or ATS link


@app.get("/api/companies/mine")
def my_companies() -> list[CompanyBoard]:
    """Companies the user added by hand, kept across discovery updates."""
    return company_discovery.your_companies(get_workspace().settings)


@app.post("/api/companies/mine")
async def add_my_company(body: CompanyRequest) -> CompanyBoard:
    """Find a company's job feed now and add it to the watch-list (400 says why not)."""
    settings = get_workspace().settings
    try:
        return await asyncio.to_thread(company_discovery.add_company, settings, body.name, body.url)
    except (ValueError, SourceError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/companies/mine/{name}")
def remove_my_company(name: str) -> dict[str, bool]:
    if not company_discovery.remove_company(get_workspace().settings, name):
        raise HTTPException(404, "That company is not in your list")
    return {"removed": True}


@app.post("/api/companies/discover")
async def companies_discover(
    mode: Literal["new", "stale", "all"] = "stale",
) -> dict[str, Any]:
    """Start a discovery pass in the background (Update button); poll the status route."""
    settings = get_workspace().settings
    if not company_discovery.status(settings).running:

        async def run() -> None:
            try:
                await asyncio.to_thread(company_discovery.discover_companies, settings, mode=mode)
            except SourceError as exc:  # recorded in the status for the UI
                log.warning("company discovery failed: %s", exc)

        task = asyncio.create_task(run())
        _DISCOVERY_TASKS.add(task)
        task.add_done_callback(_DISCOVERY_TASKS.discard)
        await asyncio.sleep(0.05)  # let the pass take its lock, so the status says running
    return company_discovery.status(settings).model_dump(mode="json")


@app.get("/api/gmail/connect")
def gmail_connect() -> RedirectResponse:
    """Start a read-only Google OAuth flow for the configured account."""
    auth = GmailAuth(get_workspace().settings)
    if not auth.configured:
        raise HTTPException(422, "Configure Gmail OAuth client ID, secret, and account in .env")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    _GMAIL_PENDING[state] = (verifier, time.time() + 600)
    response = RedirectResponse(auth.authorization_url(state, challenge.decode()))
    response.set_cookie(
        "gmail_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=auth.settings.gmail_redirect_uri.startswith("https://"),
    )
    return response


@app.get("/api/gmail/callback")
def gmail_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> HTMLResponse:
    """Verify the browser state and connected account before storing OAuth tokens.

    Runs in the tab the app opened for sign-in, so it answers with a small page (which closes
    itself on success) while the app tab keeps its state and picks up the connection.
    """
    auth = GmailAuth(get_workspace().settings)
    pending = _GMAIL_PENDING.pop(state, None)
    cookie = request.cookies.get("gmail_oauth_state", "")
    if not pending or not secrets.compare_digest(state, cookie) or time.time() > pending[1]:
        return _gmail_page(auth, "Gmail authorization state is invalid or expired.", ok=False)
    if error or not code:
        return _gmail_page(auth, "Gmail access was not granted.", ok=False)
    try:
        auth.exchange(code, pending[0])
    except SourceError as exc:
        return _gmail_page(auth, str(exc), ok=False)
    message = f"Gmail alerts connected for {auth.settings.gmail_account}."
    response = _gmail_page(auth, message, ok=True)
    response.delete_cookie("gmail_oauth_state")
    return response


def _gmail_page(auth: GmailAuth, message: str, *, ok: bool) -> HTMLResponse:
    """Result page for the sign-in tab; the app tab polls `/api/gmail/status` meanwhile."""
    next_step = (
        "This tab will close. Return to the app tab to search new alerts."
        if ok
        else "Close this tab and click Connect Gmail alerts in the app to try again."
    )
    app_url = html.escape(auth.settings.gmail_frontend_url, quote=True)
    close = "<script>setTimeout(() => window.close(), 1500)</script>" if ok else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Gmail alerts</title>
<style>
:root {{ color-scheme: light dark; --bg: #f7f7f8; --fg: #1b1b1f; --muted: #5f6068; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #141417; --fg: #ececf1; --muted: #a0a0ab; }}
}}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
  background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; }}
main {{ max-width: 420px; }} p {{ color: var(--muted); }} a {{ color: inherit; }}
</style></head><body><main>
<h1>{"Connected" if ok else "Not connected"}</h1>
<p>{html.escape(message)}</p><p>{next_step}</p>
<p><a href="{app_url}">Back to the app</a></p>
</main>{close}</body></html>"""
    return HTMLResponse(page, status_code=200 if ok else 400)


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LLMUpdate(BaseModel):
    provider: LLMProviderName
    quality_model: str
    screening_model: str
    quality_effort: Effort = "medium"
    screening_effort: Effort = "medium"
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


@app.get("/api/codex/usage")
async def get_codex_usage() -> dict[str, Any]:
    """Current ChatGPT-plan Codex limits from the signed-in Codex app-server."""
    if get_workspace().llm.provider != "codex":
        raise HTTPException(400, "Codex is not the active provider")
    try:
        return await asyncio.to_thread(codex_usage)
    except (LLMError, OSError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/codex/login")
def codex_login() -> dict[str, Any]:
    """Open the ChatGPT sign-in page via `codex login` (the CLI stores the credentials)."""
    try:
        start_login()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"started": True}


@app.get("/api/claude-code/status")
def get_claude_code_status() -> dict[str, Any]:
    return claude_code_backend.claude_code_status()


@app.get("/api/claude-code/usage")
def get_claude_code_usage() -> dict[str, Any]:
    """Claude plan rate-limit windows as reported by the CLI on recent calls."""
    return claude_code_backend.claude_code_usage()


@app.post("/api/claude-code/login")
def claude_code_login() -> dict[str, Any]:
    """Open the claude.ai sign-in page via `claude auth login` (the CLI stores credentials)."""
    try:
        claude_code_backend.start_login()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"started": True}


# ------------------------------------------------------------------ CV


@app.get("/api/cv")
def get_cv() -> MasterCV | None:
    return get_workspace().master_cv


@app.put("/api/cv")
def put_cv(cv: MasterCV) -> MasterCV:
    cv_service.save_selected_cv(get_workspace(), cv)
    return cv


@app.post("/api/cv/upload")
async def upload_cv(file: Annotated[UploadFile, File()]) -> cv_service.CVAsset:
    ws = get_workspace()
    try:
        return cv_service.store_cv(ws, file.filename or "cv.txt", await file.read())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/cv/selection/{asset_id:path}")
def select_cv(asset_id: str) -> cv_service.CVAsset:
    try:
        return cv_service.select_cv(get_workspace(), asset_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/cv/{asset_id:path}")
def delete_cv(asset_id: str) -> dict[str, bool]:
    """Delete a CV from the library and its associated profile summaries."""
    try:
        cv_service.delete_cv(get_workspace(), asset_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": True}


@app.post("/api/cv/general")
async def export_general_cv() -> dict[str, Any]:
    """Export the selected CV with its full work history to an editable Word file."""
    try:
        path, roles = await cv_service.export_general_cv(get_workspace())
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"download_url": _file_url(path), "roles": roles}


class SummaryRequest(BaseModel):
    query: SearchQuery = SearchQuery()
    use_cv: bool = True
    refresh: bool = False


@app.post("/api/profile-summary")
async def profile_summary(body: SummaryRequest) -> dict[str, Any]:
    ws = get_workspace()
    if not ws.llm_ready():
        raise HTTPException(400, "Configure an LLM provider first (Settings)")
    try:
        cv = await cv_service.ensure_selected_cv(ws) if body.use_cv else None
        summary, cached = await get_summary(ws, cv, body.query, body.refresh)
    except (ValueError, LLMError) as exc:
        raise HTTPException(502, str(exc)) from exc
    key = search_service.summary_key(ws, cv, body.query)
    return {"summary": summary, "from_memory": cached, "key": key}


@app.get("/api/profiles")
def list_profiles() -> dict[str, Any]:
    """Stored profile summaries for the selected CV (empty until the CV has been parsed)."""
    ws = get_workspace()
    cv = cv_service.cached_selected_cv(ws)
    if cv is None:
        return {"cv_parsed": False, "profiles": [], "family_yield": []}
    return {
        "cv_parsed": True,
        "profiles": search_service.list_profiles(ws, cv),
        "family_yield": history.family_yield(ws, ws.active_cv_id or ""),
    }


@app.get("/api/intent")
def get_intent() -> SearchIntent:
    """The selected CV's career intent (empty when none was stated)."""
    return intent.get_intent(get_workspace())


@app.put("/api/intent")
def put_intent(body: SearchIntent) -> SearchIntent:
    """Replace the career intent with the user's edit."""
    return intent.save_intent(get_workspace(), body)


def _profile_cv() -> Any:
    cv = cv_service.cached_selected_cv(get_workspace())
    if cv is None:
        raise HTTPException(404, "Select a CV that has been read at least once")
    return cv


@app.put("/api/profiles/{key:path}")
def edit_profile(key: str, summary: ProfileSummary) -> ProfileRecord:
    try:
        return search_service.edit_profile(get_workspace(), _profile_cv(), key, summary)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/profiles/refresh/{key:path}")
async def refresh_profile(key: str) -> ProfileRecord:
    """Rebuild a stored profile from the CV's current content (only when the user asks)."""
    ws = get_workspace()
    if not ws.llm_ready():
        raise HTTPException(400, "Configure an LLM provider first (Settings)")
    try:
        return await search_service.refresh_profile(ws, _profile_cv(), key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.delete("/api/profiles/{key:path}")
def delete_profile(key: str) -> dict[str, bool]:
    try:
        search_service.delete_profile(get_workspace(), _profile_cv(), key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": True}


# ------------------------------------------------------------------ search & jobs


@app.post("/api/search")
async def search(body: SearchRequest) -> SearchOutcome:
    try:
        return await run_search(get_workspace(), body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/history")
def list_history() -> list[history.HistoryItem]:
    """The last searches, newest first (without their results)."""
    return history.list_history(get_workspace())


@app.get("/api/history/{entry_id}")
def open_history(entry_id: str) -> dict[str, Any]:
    """A past search's filters and results; it becomes the current report again."""
    try:
        req, outcome = history.open_entry(get_workspace(), entry_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"request": req, "outcome": outcome}


@app.delete("/api/history/{entry_id}")
def delete_history(entry_id: str) -> dict[str, bool]:
    if not history.delete_entry(get_workspace(), entry_id):
        raise HTTPException(404, "That search is no longer in the history")
    return {"deleted": True}


@app.delete("/api/history")
def clear_history() -> dict[str, bool]:
    history.clear(get_workspace())
    return {"cleared": True}


def _stream(work: Callable[[Any, Any], Awaitable[Any]]) -> StreamingResponse:
    """Run `work(emit, usage)` streaming NDJSON lines: `search_progress` (what each stage is
    doing), `model_usage` (tokens per model call), then `search_results` or `error`."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        await queue.put({"type": kind, **payload})

    def usage(u: ModelUsage) -> None:  # called from worker threads
        item = {"type": "model_usage", "agent": "search", **u.model_dump()}
        loop.call_soon_threadsafe(queue.put_nowait, item)

    async def run() -> None:
        try:
            await work(emit, usage)
        except (ValueError, LLMError) as exc:
            await queue.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report it in the stream, not as a broken pipe
            log.exception("search failed")
            await queue.put({"type": "error", "message": f"Search failed: {exc}"})
        finally:
            await queue.put(None)

    async def lines() -> AsyncIterator[str]:
        task = asyncio.create_task(run())
        try:
            while (item := await queue.get()) is not None:
                yield json.dumps(item, default=str) + "\n"
        finally:
            task.cancel()

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.post("/api/search/stream")
async def search_stream(body: SearchRequest) -> StreamingResponse:
    """Run a search, streaming its progress and results (see `_stream`)."""
    return _stream(lambda emit, usage: run_search(get_workspace(), body, emit, usage))


class ContinueRequest(BaseModel):
    history_id: str
    run_id: str | None = None


@app.post("/api/search/continue/stream")
async def continue_stream(body: ContinueRequest) -> StreamingResponse:
    """Judge the shortlisted jobs a stopped or partly failed search left unjudged."""
    return _stream(
        lambda emit, usage: search_service.continue_search(
            get_workspace(), body.history_id, emit, usage, body.run_id
        )
    )


@app.post("/api/search/stop/{run_id}")
def stop_search(run_id: str) -> dict[str, bool]:
    """Stop a running search or continuation; its partial results still arrive in the stream."""
    return {"stopped": search_service.stop_search(run_id)}


class TailorRequest(BaseModel):
    template: Literal["classic", "modern", "compact"] = "classic"
    result: MatchResult | None = None
    tailored_cv_id: str | None = None
    emphasis: Literal["auto", "leadership", "hands_on"] = "auto"
    level: Literal["auto", "senior", "junior"] = "auto"


def _tailored_view(document: TailoredDocument) -> dict[str, Any]:
    """Return the editable CV and download link without duplicating its source snapshot."""
    return {
        "id": document.id,
        "job_id": document.job_id,
        "job_title": document.job_title or document.jd.job_title,
        "job_company": document.job_company or document.jd.company or "",
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "template": document.template,
        "download_url": _file_url(
            get_workspace().output_dir / "cvs" / document.filename
            if (get_workspace().output_dir / "cvs" / document.filename).exists()
            else get_workspace().output_dir / document.filename
        ),
        "cv": document.tailored.cv,
        "ats": document.tailored.ats,
        "source_ats_keyword_coverage": document.tailored.source_ats_keyword_coverage,
        "reviewed": document.reviewed,
        "imported": document.imported,
    }


@app.get("/api/jobs/{job_id}/tailored-cvs")
def tailored_cvs(job_id: str) -> list[dict[str, Any]]:
    return [_tailored_view(d) for d in tailored_documents.list_for_job(get_workspace(), job_id)]


@app.get("/api/tailored-cvs")
def all_tailored_cvs() -> list[dict[str, Any]]:
    """Saved tailored drafts remain reachable when their job card is no longer displayed."""
    return [_tailored_view(d) for d in tailored_documents.list_all(get_workspace())]


class ImportOlderCVRequest(BaseModel):
    asset_id: str
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    description: str = Field(min_length=1)


@app.post("/api/tailored-cvs/import")
async def import_older_cv(body: ImportOlderCVRequest) -> dict[str, Any]:
    """Attach a Word CV when the original posting is no longer in search history."""
    job = JobPosting(
        id=f"linked:{secrets.token_hex(8)}",
        title=body.title,
        company=body.company,
        description=body.description,
    )
    try:
        document = await cv_service.attach_existing_cv(get_workspace(), job, body.asset_id)
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return _tailored_view(document)


class ImportTailoredRequest(BaseModel):
    asset_id: str
    result: MatchResult | None = None


@app.post("/api/jobs/{job_id}/tailored-cvs/import")
async def import_tailored_cv(job_id: str, body: ImportTailoredRequest) -> dict[str, Any]:
    """Attach an older Word CV for this job, pending the user's review."""
    ws = get_workspace()
    result = _document_result(ws, job_id, TailorRequest(result=body.result))
    if result is None:
        raise HTTPException(404, "Job not found in results, saved jobs or search history")
    try:
        document = await cv_service.attach_existing_cv(ws, result.job, body.asset_id)
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return _tailored_view(document)


@app.put("/api/tailored-cvs/{document_id}")
def edit_tailored_cv(document_id: str, edits: TailoredCVEdits) -> dict[str, Any]:
    try:
        return _tailored_view(tailored_documents.edit(get_workspace(), document_id, edits))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _document_result(ws: Workspace, job_id: str, body: TailorRequest) -> MatchResult | None:
    """Use the displayed posting, or find it in saved jobs and retained searches."""
    if body.result is not None:
        if body.result.job.id != job_id:
            raise HTTPException(422, "The job ID does not match the posting")
        return body.result
    result = ws.result(job_id)
    if result is not None or body.tailored_cv_id is None:
        return result
    try:
        document = tailored_documents.load(ws, body.tailored_cv_id, job_id)
    except ValueError:
        return None
    return MatchResult(
        job=JobPosting(
            id=job_id,
            title=document.job_title or document.jd.job_title,
            company=document.job_company or document.jd.company or "",
            location=document.job_location,
            description=document.job_description,
        )
    )


@app.post("/api/jobs/{job_id}/tailor")
async def tailor_job(job_id: str, body: TailorRequest) -> dict[str, Any]:
    ws = get_workspace()
    result = _document_result(ws, job_id, body)
    if result is None:
        raise HTTPException(404, "Job not found in results, saved jobs or search history")
    try:
        tailored, path = await cv_service.tailor_to_job(
            ws, result.job, body.template, result=result, emphasis=body.emphasis, level=body.level
        )
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "document_id": tailored.document_id,
        "download_url": _file_url(path),
        "keyword_coverage": tailored.keyword_coverage,
        "missing_keywords": tailored.missing_keywords,
        "restored_keywords": tailored.restored_keywords,
        "critique": tailored.critique,
        "ats": tailored.ats,
        "source_ats_keyword_coverage": tailored.source_ats_keyword_coverage,
        "rejected": sum(not c.accepted for c in tailored.changes),
        "rejections": [
            {"source_id": c.source_id, "reason": c.reason}
            for c in tailored.changes
            if not c.accepted
        ],
        "tracking": tracker.tracking_for(ws, result.job),
    }


@app.post("/api/jobs/{job_id}/cover-letter")
async def cover_letter(job_id: str, body: TailorRequest) -> dict[str, Any]:
    """A guarded cover letter for a displayed or retained job, as .docx."""
    ws = get_workspace()
    result = _document_result(ws, job_id, body)
    if result is None:
        raise HTTPException(404, "Job not found in results, saved jobs or search history")
    try:
        letter, path = await cv_service.write_cover_letter(
            ws, result.job, body.template, tailored_cv_id=body.tailored_cv_id
        )
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "download_url": _file_url(path),
        "paragraphs": len(letter.paragraphs),
        "rejections": [
            {"source_id": c.source_id, "reason": c.reason} for c in letter.changes if not c.accepted
        ],
    }


def _letter_view(document: cover_letters.SavedLetter) -> dict[str, Any]:
    """Return saved letter text and its export links."""
    return {
        "id": document.id,
        "title": document.title,
        "company": document.company,
        "candidate_name": document.candidate_name,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "filename": document.filename,
        "greeting": document.letter.greeting,
        "paragraphs": document.letter.paragraphs,
        "closing": document.letter.closing,
        "docx_url": f"/api/cover-letters/{document.id}/export/docx",
        "txt_url": f"/api/cover-letters/{document.id}/export/txt",
    }


@app.get("/api/cover-letters")
def saved_cover_letters() -> list[dict[str, Any]]:
    return [_letter_view(item) for item in cover_letters.list_all(get_workspace())]


@app.delete("/api/cover-letters")
def delete_all_cover_letters() -> dict[str, int]:
    """Remove all saved and legacy cover letters."""
    return {"deleted": cover_letters.delete_all(get_workspace())}


@app.delete("/api/cover-letters/{document_id}")
def delete_cover_letter(document_id: str) -> dict[str, int]:
    """Remove one cover letter and its saved Word versions."""
    try:
        cover_letters.delete(get_workspace(), document_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": 1}


@app.put("/api/cover-letters/{document_id}")
def edit_cover_letter(document_id: str, edits: cover_letters.LetterEdits) -> dict[str, Any]:
    try:
        return _letter_view(cover_letters.edit(get_workspace(), document_id, edits))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/cover-letters/{document_id}/export/{format}")
def export_cover_letter_file(document_id: str, format: Literal["docx", "txt"]) -> FileResponse:
    try:
        path = cover_letters.export(get_workspace(), document_id, format)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, filename=path.name)


class TrackingRequest(BaseModel):
    status: Literal["open", "applied", "na"] | None = None
    note: str | None = None  # None keeps the current note
    reason: str | None = None  # None keeps the current reason
    stage: OutcomeStage | None = None


@app.put("/api/jobs/{job_id}/tracking")
def track_job(job_id: str, body: TrackingRequest) -> JobTracking:
    """Mark a job of the current search as applied, N/A or open, and/or set its note."""
    ws = get_workspace()
    job = ws.job(job_id)
    if job is None:
        raise HTTPException(404, "Job not in the current search")
    current = tracker.tracking_for(ws, job)
    status = body.status or (current.status if current and current.status != "new" else "open")
    tracking = tracker.set_status(ws, job, status, body.note, reason=body.reason, stage=body.stage)
    tracker.update_result(ws, job_id, tracking)
    return tracking


@app.get("/api/tracker")
def list_tracker() -> list[tracker.TrackedJob]:
    """Applications and ruled-out jobs, most recent first."""
    return tracker.register(get_workspace())


@app.post("/api/tracker")
def add_application(body: tracker.ManualApplication) -> tracker.TrackedJob:
    """Record an application made outside the app; searches will skip that role."""
    return tracker.add_application(get_workspace(), body)


@app.put("/api/tracker/{entry_id}")
def edit_tracker(entry_id: str, body: TrackingRequest) -> tracker.TrackedJob:
    try:
        entry = tracker.load(get_workspace()).get(entry_id)
        if entry is None:
            raise ValueError("That job is not in the tracker")
        status = body.status or ("open" if entry.status == "seen" else entry.status)
        return tracker.edit_entry(
            get_workspace(), entry_id, status, body.note, body.reason, body.stage
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/tracker/{entry_id}")
def delete_tracker(entry_id: str) -> dict[str, bool]:
    if not tracker.delete_entry(get_workspace(), entry_id):
        raise HTTPException(404, "That job is not in the tracker")
    return {"deleted": True}


class JobIds(BaseModel):
    job_ids: list[str]


@app.get("/api/saved")
def list_saved() -> list[SavedJob]:
    """Saved jobs, newest first, with their current status from the tracker."""
    return saved.list_saved(get_workspace())


@app.post("/api/saved")
def save_jobs(body: JobIds) -> list[SavedJob]:
    """Save jobs of the current search; returns the newly saved ones."""
    try:
        return saved.save(get_workspace(), body.job_ids)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/saved/remove")
def remove_saved(body: JobIds) -> dict[str, int]:
    """Remove saved jobs (their applications stay in the tracker)."""
    return {"removed": saved.remove(get_workspace(), body.job_ids)}


class LabelRequest(BaseModel):
    label: labels.UserLabel | None  # None removes the label; the note is the tracker's


@app.put("/api/jobs/{job_id}/label")
def label_job(job_id: str, body: LabelRequest) -> dict[str, labels.UserLabel | None]:
    """Your call on a job of the current search, kept to measure the models (no effect on
    searches)."""
    if not labels.set_label(get_workspace(), job_id, body.label):
        raise HTTPException(404, "Job not in the current search")
    return {"label": body.label}


@app.get("/api/labels")
def label_review() -> labels.LabelReview:
    """Label counts, progress to a usable set, and each model setup against your labels."""
    return labels.review(get_workspace())


class PreferenceDecision(BaseModel):
    accept: bool
    text: str | None = None  # the user's wording, when they edit the proposal


@app.get("/api/learning")
def learning_state() -> learning.LearningState:
    """Preferences learned from your labels: proposals to review and those in force."""
    return learning.state(get_workspace())


@app.post("/api/learning/suggest")
async def learning_suggest() -> learning.LearningState:
    """Ask the quality model for general preferences from your labels and notes."""
    ws = get_workspace()
    if not ws.llm_ready():
        raise HTTPException(400, "Configure an LLM provider first (Settings)")
    llm = ws.structured("quality", None, "Learn from your labels")
    try:
        return await asyncio.to_thread(learning.suggest, ws, llm)
    except (ValueError, LLMError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/learning/{pref_id}")
def learning_decide(pref_id: str, body: PreferenceDecision) -> learning.LearningState:
    try:
        return learning.decide(get_workspace(), pref_id, body.accept, body.text)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/learning/{pref_id}")
def learning_remove(pref_id: str) -> learning.LearningState:
    try:
        return learning.remove(get_workspace(), pref_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/outcomes/review")
def outcomes_review() -> calibration.OutcomeReview:
    """Funnel, quiet applications and patterns in ruled-out roles (read-only suggestions)."""
    return calibration.review_outcomes(get_workspace())


# ------------------------------------------------------------------ evidence review


class EvidenceText(BaseModel):
    source: str
    text: str


class EvidenceDecision(BaseModel):
    accept: bool
    text: str | None = None  # the user's wording, when they edit the proposal


@app.get("/api/evidence")
def evidence_queue() -> list[enrichment.QueuedEvidence]:
    """Pending proposals for the selected CV."""
    return enrichment.queue(get_workspace())


@app.post("/api/evidence/upload")
async def evidence_upload(
    file: Annotated[UploadFile, File()],
) -> list[enrichment.QueuedEvidence]:
    """Read a document (.pdf, .docx, .md, .txt) and queue what it adds to the CV."""
    ws = get_workspace()
    if not ws.llm_ready():
        raise HTTPException(400, "Configure an LLM provider first (Settings)")
    data = await file.read()
    try:
        return await enrichment.propose_from_file(ws, file.filename or "document", data)
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/evidence/text")
async def evidence_text(body: EvidenceText) -> list[enrichment.QueuedEvidence]:
    """Queue what pasted text (e.g. a portfolio page) adds to the CV."""
    ws = get_workspace()
    if not ws.llm_ready():
        raise HTTPException(400, "Configure an LLM provider first (Settings)")
    try:
        return await enrichment.propose(ws, body.source or "pasted text", body.text)
    except (ValueError, LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/evidence/{item_id}")
def evidence_decide(item_id: str, body: EvidenceDecision) -> enrichment.QueuedEvidence:
    """Accept (writes the fact to the selected Master CV) or reject one proposal."""
    try:
        return enrichment.decide(get_workspace(), item_id, body.accept, body.text)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/sources/yield")
def source_yield() -> list[history.SourceYield]:
    """Postings and true matches per source over the recent searches."""
    return history.source_yield(get_workspace())


def _file_url(path: Path) -> str:
    """Public download URL for a document under the output directory."""
    relative = path.resolve().relative_to(get_workspace().output_dir.resolve())
    return "/api/files/" + relative.as_posix()


@app.get("/api/files/{folder}/{name}")
def download_grouped(folder: str, name: str) -> FileResponse:
    if folder not in ("cvs", "cover_letters"):
        raise HTTPException(404, "File not found")
    out = (get_workspace().output_dir / folder).resolve()
    path = (out / name).resolve()
    if path.parent != out or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=path.name)


@app.get("/api/files/{name}")
def download(name: str) -> FileResponse:
    out = get_workspace().output_dir.resolve()
    path = (out / name).resolve()
    if path.parent != out:
        raise HTTPException(404, "File not found")
    if not path.is_file():
        folder = "cover_letters" if "cover_letter" in path.stem.lower() else "cvs"
        path = (out / folder / name).resolve()
        if path.parent != out / folder or not path.is_file():
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
