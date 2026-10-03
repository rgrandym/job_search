"""Search pipeline used by the Search button, the REST API, and the agents.

1. Capture     sources (per title, location, distance, salary)  -> postings
2. Pre-filter  deterministic: hard exclusions + cheap ranking    -> shortlist
3. Enrich      full descriptions for shortlisted snippet-only postings (e.g. Reed)
4. Summarise   ProfileSummary, quality model (from memory when the search type repeats)
5. Screen      job_matcher (screening model) judges the shortlist -> true matches
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, Field

from src.core.config import Settings
from src.core.llm import UsageSink
from src.core.llm.calls import call_group, cancel_group, clear_group
from src.core.llm_provider import LLMError
from src.jobs.fetcher import ALL_SOURCES, JobSource, build_sources, dedupe, fetch_all
from src.jobs.matcher import JobMatcher, build_profile
from src.jobs.models import (
    JobPosting,
    JobVerdict,
    MatchReport,
    MatchResult,
    ProfileSummary,
    SearchIntent,
    SearchQuery,
)
from src.jobs.profile_memory import (
    ProfileMemory,
    ProfileRecord,
    cv_fingerprint,
    family_of,
    family_terms,
    intent_fingerprint,
    intent_text,
    pivot_titles,
    role_family,
    searched_families,
    summarize_profile,
)
from src.jobs.screener import VerdictCache, apply_verdicts, screen_jobs
from src.jobs.sources.base import SourceError
from src.jobs.sources.gmail_alerts import GmailAlertSource, GmailAuth
from src.services import company_discovery, cv_service, history, labels, learning, tracker
from src.services.intent import get_intent
from src.services.workspace import Workspace
from src.tools.search_tools import board_terms, company_key, shares_role_words

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
BOARD_TERMS = 16  # job-board searches per source (profile roles split into short terms)
# Server-side search by title.
KEYWORD_SOURCES = {
    "reed", "cv_library", "adzuna", "linkedin_search", "totaljobs", "jobs_ac_uk", "nhs_jobs",
}  # fmt: skip
# Large employer feeds (Workday, iCIMS, ...) get the same terms, or a pharma's HR and finance
# roles would fill the shortlist; they match titles loosely (`SearchQuery.is_loosely_relevant`).
TARGETED_SOURCES = KEYWORD_SOURCES | {"company"}


async def _noop(_: str, __: dict[str, Any]) -> None:
    return None


class SearchRequest(BaseModel):
    query: SearchQuery
    use_cv: bool = True
    smart: bool = Field(True, description="Screen the shortlist with the job_matcher")
    threshold: float | None = None
    refresh_summary: bool = False
    profile_key: str | None = Field(
        None, description="Use this stored profile summary instead of the remembered default"
    )
    widen: bool = Field(
        False, description="Also search the adjacent role families the profile proposes"
    )
    run_id: str | None = Field(None, description="Client id, so the search can be stopped")


class SourceReport(BaseModel):
    """What one source contributed to a search."""

    name: str
    status: Literal["used", "failed", "skipped"]
    fetched: int = 0  # postings returned (before de-duplication across sources)
    detail: str | None = None


class SearchOutcome(BaseModel):
    report: MatchReport
    fetched: int
    alert_only: bool = False
    sources: list[SourceReport] = Field(default_factory=list)
    cv_titles: list[str] = Field(
        default_factory=list, description="Titles taken from the CV profile (titles left empty)"
    )
    errors: dict[str, str] = Field(default_factory=dict)
    skipped_sources: dict[str, str] = Field(default_factory=dict)
    summary_from_memory: bool | None = None
    smart_unavailable: str | None = None
    seconds: float = 0.0
    cancelled: bool = Field(False, description="The user stopped the search")
    unscreened: int = Field(0, description="Shortlisted jobs not yet judged (continue them)")
    history_id: str | None = None
    families: dict[str, str] = Field(
        default_factory=dict, description="Role families searched: name -> tier"
    )


# Running searches by run id, so a stop request (another HTTP call) can reach them.
_RUNS: dict[str, asyncio.Event] = {}
_stop: ContextVar[asyncio.Event | None] = ContextVar("search_stop", default=None)


def _stopped() -> bool:
    event = _stop.get()
    return event is not None and event.is_set()


def stop_search(run_id: str) -> bool:
    """Stop a running search: skip what has not started and kill its in-flight model calls.
    Work already done (postings found, verdicts returned) is kept."""
    event = _RUNS.get(run_id)
    if event is None:
        return False
    event.set()
    cancel_group(run_id)
    return True


@asynccontextmanager
async def _running(run_id: str | None) -> AsyncIterator[str]:
    """Register a run so it can be stopped; model calls inside join its cancellable group."""
    run_id = run_id or uuid.uuid4().hex
    event = _RUNS[run_id] = asyncio.Event()
    tokens = (_stop.set(event), call_group.set(run_id))
    try:
        yield run_id
    finally:
        _RUNS.pop(run_id, None)
        clear_group(run_id)
        _stop.reset(tokens[0])
        call_group.reset(tokens[1])


async def run_search(
    ws: Workspace,
    req: SearchRequest,
    emit: Emit = _noop,
    usage_sink: UsageSink | None = None,
) -> SearchOutcome:
    """Execute the full pipeline and store the report in the workspace.

    Every stage reports what it is doing through `emit("search_progress", ...)`, so the UI
    and the agent chat can show live, detailed progress. `stop_search(req.run_id)` stops it
    early; the partial outcome is still returned and recorded.
    """
    async with _running(req.run_id):
        return await _run_search(ws, req, emit, usage_sink)


async def _run_search(
    ws: Workspace, req: SearchRequest, emit: Emit, usage_sink: UsageSink | None
) -> SearchOutcome:
    t0 = time.monotonic()
    names, gmail_skipped = _source_names(ws, req)
    cv = await _load_cv(ws, req, emit, usage_sink)
    intent = get_intent(ws)
    query, profile = await _plan_query(ws, req, cv, intent, emit, usage_sink)
    cv_key = f"{ws.active_cv_id}:{cv_fingerprint(cv)}" if cv and ws.active_cv_id else None
    if "company" in names:
        await _ensure_company_boards(ws, emit)
    sources, skipped = build_sources(
        names,
        settings=ws.settings,
        cv_key=cv_key,
        new_alerts_only=names == ["gmail_alerts"],  # "Search new alerts for this CV"
    )
    skipped |= gmail_skipped
    counts: dict[str, int] = {}
    jobs, errors = await _capture(sources, req.query, query, counts, emit)
    jobs, applied, dismissed = await _set_aside(ws, jobs, emit)

    ctx = _Context(req, cv, query, intent, profile)
    matched = await _match(ws, ctx, jobs, sources, errors, emit, usage_sink)
    matched.report.applied, matched.report.dismissed = applied, dismissed
    families = _attribute(matched.report, req.widen)
    tracker.annotate(ws, matched.report)
    outcome = SearchOutcome(
        report=matched.report,
        fetched=len(jobs),
        alert_only=query.sources == ["gmail_alerts"],
        errors=errors,
        skipped_sources=skipped,
        sources=_source_report(sources, counts, errors, skipped),
        cv_titles=list(query.titles) if profile else [],
        summary_from_memory=matched.summary_from_memory,
        smart_unavailable=matched.smart_unavailable,
        cancelled=_stopped(),
        unscreened=count_unscreened(ws, req, matched.report),
        families=families,
    )
    if matched.complete:
        _mark_gmail_analysed(sources, outcome.report)

    ws.last_query, ws.last_report = query, outcome.report
    outcome.seconds = round(time.monotonic() - t0, 2)
    outcome.history_id = history.record(ws, req, outcome).id
    labels.attach_verdicts(ws, outcome.report, ws.last_models)  # labelled postings seen again
    history.log_family_yield(ws, outcome.history_id, _owner(ws, cv), families, outcome.report)
    return await _finish(outcome, emit)


async def continue_search(
    ws: Workspace,
    history_id: str,
    emit: Emit = _noop,
    usage_sink: UsageSink | None = None,
    run_id: str | None = None,
) -> SearchOutcome:
    """Judge the shortlisted jobs a stopped (or partly failed) search left unjudged, keeping
    every verdict it already has. Nothing is fetched again; the history entry is updated."""
    async with _running(run_id):
        t0 = time.monotonic()
        req, outcome = history.open_entry(ws, history_id)
        report = outcome.report
        previous = {r.job.id: r.verdict for r in report.all_results() if r.verdict}
        shortlist = _shortlist(report, ws.settings)
        pending = [job for job in shortlist if job.id not in previous]
        if not pending:
            raise ValueError("Every shortlisted job in this search has already been judged")
        if not ws.llm_ready():
            raise ValueError("Connect an LLM in Settings to continue screening")
        cv = await _load_cv(ws, req, emit, usage_sink)
        intent = get_intent(ws)
        summary = (
            report.summary
            or (
                pinned_profile(ws, cv, req.profile_key)
                or await get_summary(ws, cv, req.query, False, emit, usage_sink, intent)
            )[0]
        )
        threshold = ws.settings.score_threshold if req.threshold is None else req.threshold
        await _say(emit, "plan", f"Continuing: {len(pending)} shortlisted jobs left to judge")
        errors = await _screen(
            ws, summary, pending, req.query, report, threshold, emit, usage_sink,
            screening_base(cv, req.query), previous, intent_text(intent),
        )  # fmt: skip
        outcome.errors = {k: v for k, v in outcome.errors.items() if not k.startswith("screening")}
        outcome.errors |= {f"screening {i}": e for i, e in enumerate(errors)}
        outcome.smart_unavailable = None
        outcome.cancelled = _stopped()
        outcome.unscreened = count_unscreened(ws, req, report)
        outcome.seconds = round(outcome.seconds + time.monotonic() - t0, 2)
        outcome.history_id = history_id
        history.update(ws, history_id, outcome)
        labels.attach_verdicts(ws, report, ws.last_models)
        history.log_family_yield(ws, history_id, _owner(ws, cv), outcome.families, report)
        ws.last_report = report
        return await _finish(outcome, emit)


async def _set_aside(
    ws: Workspace, jobs: list[JobPosting], emit: Emit
) -> tuple[list[JobPosting], list[MatchResult], list[MatchResult]]:
    """Jobs already applied for or ruled out leave before screening (no model calls)."""
    keep, applied, dismissed = tracker.set_aside(ws, jobs)
    if applied or dismissed:
        await _say(
            emit,
            "prefilter",
            f"Set aside {len(applied)} already applied for and {len(dismissed)} marked N/A",
        )
    return keep, applied, dismissed


async def _finish(outcome: SearchOutcome, emit: Emit) -> SearchOutcome:
    matches = len(outcome.report.matches)
    found = "true matches" if outcome.report.screened else "above threshold"
    stopped = "Stopped" if outcome.cancelled else "Done"
    left = f"; {outcome.unscreened} shortlisted jobs not judged yet" if outcome.unscreened else ""
    await _say(
        emit,
        "done",
        f"{stopped}: {matches} {found} from {outcome.fetched} postings in {outcome.seconds}s{left}",
    )
    await emit("search_results", {"outcome": outcome.model_dump(mode="json")})
    return outcome


def count_unscreened(ws: Workspace, req: SearchRequest, report: MatchReport) -> int:
    """Shortlisted jobs without a verdict (stopped or failed batches): what Continue judges."""
    if not req.smart or not ws.llm_ready():
        return 0
    judged = {r.job.id for r in report.all_results() if r.verdict}
    shortlist = _shortlist(report, ws.settings)
    return sum(1 for job in shortlist if job.id not in judged)


async def _say(emit: Emit, stage: str, message: str, **extra: Any) -> None:
    await emit("search_progress", {"stage": stage, "message": message, **extra})


async def _ensure_company_boards(ws: Workspace, emit: Emit) -> None:
    """Company sites selected and directory companies not checked yet (first use, or a pass
    that was stopped): find their job boards now, so every board is searched. Takes about 10
    minutes the first time; later searches skip this (results are kept for 30 days)."""
    if _stopped() or not company_discovery.needs_discovery(ws.settings):
        return
    loop, stop = asyncio.get_running_loop(), _stop.get()

    def progress(message: str) -> None:
        line = f"Company sites: finding job boards: {message}"
        asyncio.run_coroutine_threadsafe(
            _say(emit, "capture", line, source="company", status="running"), loop
        )

    await _say(
        emit,
        "capture",
        "Company sites: first use, finding the job boards of the UK life-science companies "
        "(about 10 minutes, only once; Stop keeps what was found)",
        source="company",
        status="running",
    )
    try:
        report = await asyncio.to_thread(
            company_discovery.discover_companies,
            ws.settings,
            mode="new",
            progress=progress,
            should_stop=lambda: stop is not None and stop.is_set(),
        )
    except SourceError as exc:
        await _say(emit, "capture", f"Company sites: discovery failed: {exc}", source="company")
        return
    boards = sum(report.boards.values())
    await _say(emit, "capture", f"Company sites: {boards} job boards ready", source="company")


async def _load_cv(
    ws: Workspace, req: SearchRequest, emit: Emit, usage_sink: UsageSink | None
) -> Any:
    if not req.use_cv:
        await _say(emit, "cv", "Searching without a CV (filters only)")
        return None
    if cv_service.cached_selected_cv(ws) is None and ws.active_cv_id is not None:
        model = ws.llm.model_for("quality")
        await _say(emit, "cv", f"Reading the selected CV with {model} (first time for this file)")
    else:
        await _say(emit, "cv", "Loading the selected CV")
    cv = await cv_service.ensure_selected_cv(ws, usage_sink)
    await _say(emit, "cv", f"CV ready: {len(cv.experience)} roles, {len(cv.all_skills())} skills")
    return cv


async def _plan_query(
    ws: Workspace,
    req: SearchRequest,
    cv: Any,
    intent: SearchIntent,
    emit: Emit,
    usage_sink: UsageSink | None,
) -> tuple[SearchQuery, tuple[ProfileSummary, bool] | None]:
    """Pinned profile, and the job-board search terms: the user's titles, or (when empty) the
    CV profile's role families (budgeted per tier), split into short advertised titles."""
    query = req.query
    if query.titles:
        query = query.model_copy(update={"titles": board_terms(query.titles, limit=BOARD_TERMS)})
    profile = pinned_profile(ws, cv, req.profile_key) if cv is not None else None
    if profile is not None:
        family = ws.memory.record(req.profile_key or "")
        label = family.role_family if family else "selected"
        await _say(emit, "summary", f"Using your selected profile ({label})")
    if cv is not None and _titles_from_cv(ws, req):
        # The summary is stored per CV, so the same CV always searches the same roles.
        profile = profile or await get_summary(
            ws, cv, query, req.refresh_summary, emit, usage_sink, intent
        )
        summary = profile[0]
        planned = family_terms(summary, req.widen, BOARD_TERMS)
        if planned and not any(f.tier == "core" for f in searched_families(summary, req.widen)):
            # Searching only the next level up or other functions would silently drop the
            # roles the candidate does now: fall back to the profile's target roles.
            planned = []
            await _say(emit, "plan", "No core role family can be searched in this profile "
                       "(see Profiles); using its target roles instead")  # fmt: skip
        terms = [t for t, _ in planned] or board_terms(
            summary.target_roles, summary.search_keywords, BOARD_TERMS
        )
        query = query.model_copy(update={"titles": terms})
        if query.titles:
            await _say(emit, "plan", _plan_message(summary, planned, terms, req.widen))
    return query, profile


def _plan_message(
    summary: ProfileSummary, planned: list[tuple[str, str]], terms: list[str], widen: bool
) -> str:
    """What the job boards will search, grouped by role family."""
    if not planned:
        return f"Job boards will run {len(terms)} searches from your CV profile: " + " · ".join(
            terms
        )
    tiers = {f.name: f.tier for f in summary.role_families}
    groups: dict[str, list[str]] = {}
    for term, fam in planned:
        groups.setdefault(fam, []).append(term)
    parts = [f"{fam} ({tiers.get(fam, 'core')}): {', '.join(ts)}" for fam, ts in groups.items()]
    held = [f.name for f in summary.role_families if f.rejected is None and f not in
            searched_families(summary, widen)]  # fmt: skip
    extra = f"; not searched (widen to include): {', '.join(held)}" if held else ""
    return f"Job boards will run {len(terms)} searches by role family: " + " · ".join(parts) + extra


class _Context(NamedTuple):
    """What one search matches against: the request, CV, board query, intent and profile."""

    req: SearchRequest
    cv: Any
    query: SearchQuery
    intent: SearchIntent
    profile: tuple[ProfileSummary, bool] | None


def _attribute(report: MatchReport, widen: bool) -> dict[str, str]:
    """Tag every result with the role family it belongs to; returns the families searched."""
    summary = report.summary
    if summary is None:
        return {}
    families = searched_families(summary, widen)
    for result in [*report.matches, *report.below_threshold, *report.excluded]:
        result.family = family_of(result.job.title, families)
    return {f.name: f.tier for f in families}


_RULES = "location, arrangement, salary, seniority, certifications, languages"


class _Matched(NamedTuple):
    report: MatchReport
    summary_from_memory: bool | None
    smart_unavailable: str | None
    complete: bool  # every shortlisted posting was judged (or nothing needed judging)


async def _match(
    ws: Workspace,
    ctx: _Context,
    jobs: list[JobPosting],
    sources: list[JobSource],
    errors: dict[str, str],
    emit: Emit,
    usage_sink: UsageSink | None,
) -> _Matched:
    """Pre-filter, enrich the shortlist, then screen it with the job_matcher."""
    req, cv, query = ctx.req, ctx.cv, ctx.query
    settings = ws.settings
    threshold = settings.score_threshold if req.threshold is None else req.threshold
    matcher = JobMatcher(settings=settings)
    first = 0 if req.smart else threshold
    known = ctx.profile[0] if ctx.profile else ws.memory.get(_owner(ws, cv), role_family(query))
    pivots = pivot_titles(known)

    def prefilter(postings: list[JobPosting], cut: float) -> MatchReport:
        return matcher.match(
            cv, postings, threshold=cut, query=query, intent=ctx.intent, pivot_titles=pivots
        )

    report = prefilter(jobs, first)
    shortlist = _shortlist(report, settings)
    excluded = len(report.excluded)
    await _say(emit, "prefilter", f"Pre-filter: {excluded} excluded by hard rules ({_RULES}); "
               f"shortlist of {len(shortlist)} for screening")  # fmt: skip
    enriched = await _enrich_shortlist(shortlist, sources, errors, emit)
    if enriched:  # re-score with full descriptions
        report = prefilter(_replace(jobs, enriched), first)
        shortlist = _shortlist(report, settings)
    from_memory = ctx.profile[1] if ctx.profile else None
    unavailable = None
    if req.smart and not ws.llm_ready():
        unavailable = "No LLM configured; showing keyword pre-filter scores only"
    elif req.smart and _stopped():
        unavailable = "Stopped before AI screening; showing keyword pre-filter scores"
    screening_errors: list[str] = []
    if req.smart and ws.llm_ready() and shortlist and not unavailable:
        try:
            from_memory, screening_errors = await _screen_shortlist(
                ws, ctx, shortlist, report, threshold, emit, usage_sink
            )
            errors |= {f"screening {i}": e for i, e in enumerate(screening_errors)}
        except LLMError as exc:
            unavailable = (
                "Stopped before AI screening; showing keyword pre-filter scores"
                if _stopped()
                else f"Smart matching failed: {exc}"
            )
            await _say(emit, "screen", unavailable, status="failed")
    if unavailable:  # fall back to deterministic buckets
        report = prefilter(_replace(jobs, enriched), threshold)
    if report.summary is None and known is not None:
        report.summary = known
    complete = not unavailable and not screening_errors and (report.screened or not shortlist)
    return _Matched(report, from_memory, unavailable, complete)


async def _screen_shortlist(
    ws: Workspace,
    ctx: _Context,
    shortlist: list[JobPosting],
    report: MatchReport,
    threshold: float,
    emit: Emit,
    usage_sink: UsageSink | None,
) -> tuple[bool, list[str]]:
    """Profile summary (pinned, remembered or built), then the job_matcher on the shortlist.
    Returns (summary from memory, screening errors)."""
    summary, from_memory = ctx.profile or await get_summary(
        ws, ctx.cv, ctx.query, ctx.req.refresh_summary, emit, usage_sink, ctx.intent
    )
    errors = await _screen(
        ws, summary, shortlist, ctx.query, report, threshold, emit, usage_sink,
        screening_base(ctx.cv, ctx.query), None, intent_text(ctx.intent),
    )  # fmt: skip
    return from_memory, errors


def screening_base(cv: Any, query: SearchQuery) -> str:
    """Where the candidate lives and how they will work, for the matcher's practicality score."""
    profile = build_profile(cv, query=query) if cv is not None else None
    if profile is None or not profile.locations:
        return ""
    ways = "/".join(profile.work_arrangements) or "any arrangement"
    move = "will relocate" if profile.willing_to_relocate else "will not relocate"
    return f"{'; '.join(profile.locations)} ({ways}; {move})"


async def _enrich_shortlist(
    shortlist: list[JobPosting], sources: list[JobSource], errors: dict[str, str], emit: Emit
) -> dict[str, JobPosting]:
    if not shortlist:
        return {}
    await _say(
        emit, "enrich", f"Checking full descriptions for {len(shortlist)} shortlisted postings"
    )
    enriched = await asyncio.to_thread(_enrich, shortlist, sources, errors)
    if enriched:
        await _say(emit, "enrich", f"Fetched {len(enriched)} full job descriptions")
    return enriched


async def _screen(
    ws: Workspace,
    summary: ProfileSummary,
    shortlist: list[JobPosting],
    query: SearchQuery,
    report: MatchReport,
    threshold: float,
    emit: Emit,
    usage_sink: UsageSink | None,
    base: str = "",
    previous: dict[str, JobVerdict] | None = None,
    intent: str = "",
) -> list[str]:
    settings = ws.settings
    model = ws.llm.model_for("screening")
    ceiling = len(shortlist) >= settings.screen_max_jobs
    await _say(
        emit,
        "screen",
        f"job_matcher ({model}) judging {len(shortlist)} postings, {settings.screen_batch_size} "
        "per worker; postings judged before for this profile keep their verdict"
        + (f" (safety ceiling of {settings.screen_max_jobs} reached)" if ceiling else ""),
        done=0,
        total=len(shortlist),
    )

    async def progress(done: int, total: int) -> None:
        await _say(emit, "screen", f"Screened {done}/{total} new postings", done=done, total=total)

    async def note(message: str) -> None:
        await _say(emit, "screen", message)

    verdicts, screening_errors = await screen_jobs(
        summary,
        shortlist,
        ws.structured("screening", usage_sink, "Job matching"),
        query,
        settings.screen_batch_size,
        settings.screen_concurrency,
        progress,
        threshold,
        base,
        batch_timeout=settings.screen_batch_timeout_s,
        stop=_stop.get(),
        cache=VerdictCache(settings.data_dir / "verdict_cache.json"),
        model=_matcher_key(ws),
        note=note,
        review_llm=ws.structured("quality", usage_sink, "Second opinion"),
        intent=intent,
    )
    remembered = sum(v.from_memory for v in verdicts.values())
    if remembered:
        await _say(emit, "screen", f"{remembered} verdict(s) reused from earlier searches")
    report.summary = summary
    apply_verdicts(report, {**(previous or {}), **verdicts}, threshold)
    return screening_errors


def _matcher_key(ws: Workspace) -> str:
    """Who judges, for the verdict memory: the screening model and the second-opinion model."""
    llm = ws.llm
    return (
        f"{llm.provider}:{llm.screening_model}:{llm.screening_effort}"
        f"|review:{llm.quality_model}:{llm.quality_effort}"
    )


def _mark_gmail_analysed(sources: list[JobSource], report: MatchReport) -> None:
    """Gmail alerts become "seen" for this CV only once they were excluded or judged."""
    analyzed_ids = {
        result.job.id for result in report.all_results() if result.excluded or result.verdict
    }
    for source in sources:
        if isinstance(source, GmailAlertSource):
            source.mark_analyzed(analyzed_ids)


async def get_summary(
    ws: Workspace,
    cv: Any,
    query: SearchQuery | None,
    refresh: bool,
    emit: Emit = _noop,
    usage_sink: UsageSink | None = None,
    intent: SearchIntent | None = None,
) -> tuple[ProfileSummary, bool]:
    """Profile summary (quality model), reused from memory for the same CV and search type.
    A new one is oriented by the career `intent` (the selected CV's when not given). The
    preferences you accepted from your labels are applied on top (`learning`)."""
    intent = intent if intent is not None else get_intent(ws)
    family = role_family(query)
    owner = _owner(ws, cv)
    if cv is not None and ws.active_cv_id:
        ws.memory.adopt(ws.active_cv_id, cv_fingerprint(cv))
    known = not refresh and ws.memory.get(owner, family) is not None
    model = ws.llm.model_for("quality")
    await _say(
        emit,
        "summary",
        f"Profile summary for role family '{family}': "
        + ("loading from memory" if known else f"building a new one with {model}"),
    )
    result = await asyncio.to_thread(
        summarize_profile,
        cv,
        query,
        ws.structured("quality", usage_sink, "Profile summary"),
        ws.memory,
        refresh,
        ws.active_cv_id,
        intent,
    )
    _label_profiles(ws, cv)
    await _say(emit, "summary", "Profile summary ready" + (" (from memory)" if result[1] else ""))
    return learning.learned_for(ws, result[0], cv), result[1]


# ------------------------------------------------------------------ stored profiles


class StoredProfile(ProfileRecord):
    """A stored profile as listed to the user."""

    cv_changed: bool = False  # the CV was edited after this profile was (re)built
    intent_changed: bool = False  # the career intent changed after this profile was built


def _owner(ws: Workspace, cv: Any) -> str:
    """Profiles belong to the selected CV's id, so CV edits keep them; a new CV starts afresh."""
    return ws.active_cv_id if cv is not None and ws.active_cv_id else cv_fingerprint(cv)


def _label_profiles(ws: Workspace, cv: Any) -> None:
    """Stamp the selected CV's file name on its stored profiles."""
    if cv is None or not ws.active_cv_id:
        return
    name = cv_service.cv_name(ws, ws.active_cv_id)
    if name is not None:
        ws.memory.label(_owner(ws, cv), name)


def summary_key(ws: Workspace, cv: Any, query: SearchQuery | None) -> str:
    """Memory key of the summary `get_summary` uses for this CV and search."""
    return ProfileMemory.key(_owner(ws, cv), role_family(query))


def list_profiles(ws: Workspace, cv: Any) -> list[StoredProfile]:
    """Stored profile summaries for the selected CV, flagged when the CV changed since."""
    fp = cv_fingerprint(cv)
    if ws.active_cv_id:
        ws.memory.adopt(ws.active_cv_id, fp)
    _label_profiles(ws, cv)
    intent = get_intent(ws)
    intent_fp = intent_fingerprint(intent)
    for rec in ws.memory.records(_owner(ws, cv)):
        ws.memory.recheck(rec.key, cv, intent)  # the dialog shows what searches will use
    return [
        StoredProfile(
            **r.model_dump(),
            cv_changed=r.cv_fingerprint != fp,
            intent_changed=r.intent_fingerprint != intent_fp,
        )
        for r in ws.memory.records(_owner(ws, cv))
    ]


def _own_record(ws: Workspace, cv: Any, key: str) -> ProfileRecord:
    rec = ws.memory.record(key)
    if rec is None or not key.startswith(f"{_owner(ws, cv)}:"):
        raise ValueError("That profile does not belong to the selected CV; choose another one")
    return rec


async def refresh_profile(
    ws: Workspace, cv: Any, key: str, usage_sink: UsageSink | None = None
) -> ProfileRecord:
    """Rebuild one stored profile from the CV's current content (the user asked to update it)."""
    rec = _own_record(ws, cv, key)
    titles = [] if rec.role_family == "any" else [rec.role_family]
    await get_summary(ws, cv, SearchQuery(titles=titles), True, usage_sink=usage_sink)
    return _own_record(ws, cv, key)


def pinned_profile(ws: Workspace, cv: Any, key: str | None) -> tuple[ProfileSummary, bool] | None:
    """(summary, from_memory) for a profile the user chose, or None to use the default.
    Its role families are re-checked against the current CV and intent (`ProfileMemory.recheck`)."""
    if key is None:
        return None
    record = _own_record(ws, cv, key)
    summary = ws.memory.recheck(key, cv, get_intent(ws)) or record.summary
    return learning.learned_for(ws, summary, cv), True


def edit_profile(ws: Workspace, cv: Any, key: str, summary: ProfileSummary) -> ProfileRecord:
    """Save the user's edits to one of the selected CV's profiles."""
    _own_record(ws, cv, key)
    return ws.memory.update(key, summary)


def delete_profile(ws: Workspace, cv: Any, key: str) -> None:
    _own_record(ws, cv, key)
    ws.memory.delete(key)


SOURCE_LABELS = {
    "reed": "Reed",
    "cv_library": "CV-Library",
    "adzuna": "Adzuna",
    "linkedin_search": "LinkedIn",
    "totaljobs": "Totaljobs",
    "jobs_ac_uk": "jobs.ac.uk",
    "nhs_jobs": "NHS Jobs",
    "biotechnologyjobs": "Biotechnology Jobs",
    "company": "Company career sites",
    "inbox": "LinkedIn/Indeed inbox",
    "linkedin": "LinkedIn inbox",
    "indeed": "Indeed inbox",
    "gmail_alerts": "Gmail alerts",
    "demo": "Demo jobs",
}


def _source_task(name: str, query: SearchQuery) -> str:
    """What a source is about to do, in words."""
    if name in KEYWORD_SOURCES:
        titles = ", ".join(query.titles) or "any title"
        where = "; ".join(query.place_names()) or "any location"
        radius = f" ±{query.distance_miles} mi" if query.locations and query.distance_miles else ""
        return f"searching {titles} in {where}{radius}"
    if name == "gmail_alerts":
        if query.sources == ["gmail_alerts"]:
            return "reading the inbox for alerts not yet judged for this CV"
        return "reading the inbox for alert jobs in the date window"
    if name == "company":
        titles = ", ".join(query.titles[:4]) + ("…" if len(query.titles) > 4 else "")
        return f"reading company career feeds for {titles or 'any title'}"
    if name == "biotechnologyjobs":
        return "reading the latest 50 jobs from its public feed (refreshed hourly)"
    if name in ("inbox", "linkedin", "indeed"):
        return "reading saved alert emails and postings"
    return "loading postings"


async def _capture(
    sources: list[JobSource],
    user_query: SearchQuery,
    query: SearchQuery,
    counts: dict[str, int],
    emit: Emit = _noop,
) -> tuple[list[JobPosting], dict[str, str]]:
    """Fetch each source in turn, reporting progress. Job boards and company feeds search
    `query` (which may carry the CV's target roles); small local feeds keep the user's own
    filters, since every posting they return is judged by the job_matcher."""
    jobs: list[JobPosting] = []
    errors: dict[str, str] = {}
    for i, src in enumerate(sources, 1):
        label = SOURCE_LABELS.get(src.name, src.name)
        if _stopped():
            errors[src.name] = "stopped before searching"
            continue
        source_query = query if src.name in TARGETED_SOURCES else user_query
        await _say(
            emit,
            "capture",
            f"[{i}/{len(sources)}] {label}: {_source_task(src.name, source_query)}",
            source=src.name,
            status="running",
        )
        found, failed = await asyncio.to_thread(fetch_all, [src], source_query, counts)
        jobs += found
        errors |= failed
        if src.name in failed:
            await _say(
                emit,
                "capture",
                f"{label} failed: {failed[src.name]}",
                source=src.name,
                status="failed",
            )
        else:
            await _say(
                emit,
                "capture",
                f"{label}: {counts.get(src.name, 0)} postings",
                source=src.name,
                status="done",
            )
    unique = dedupe(jobs)
    window = ""
    if query.posted_within_days:
        undated = sum(1 for j in unique if j.posted_at is None)
        window = f", posted in the last {query.posted_within_days} day(s)" + (
            f"; {undated} without a date kept" if undated else ""
        )
    await _say(
        emit,
        "capture",
        f"Collected {len(unique)} unique postings{window} ({len(jobs)} before removing duplicates)",
    )
    return unique, errors


def _titles_from_cv(ws: Workspace, req: SearchRequest) -> bool:
    """No titles given: job boards search the CV profile's target roles (not needed for
    alert-only searches, whose alerts are already targeted by the user's subscriptions)."""
    query = req.query
    return not query.titles and req.smart and query.sources != ["gmail_alerts"] and ws.llm_ready()


def _has_local_inbox(ws: Workspace) -> bool:
    """Whether the user saved alert emails or postings under `data/inbox/`."""
    inbox = ws.settings.inbox_dir
    return inbox.is_dir() and any(p.is_file() for p in inbox.rglob("*"))


def _source_names(ws: Workspace, req: SearchRequest) -> tuple[list[str], dict[str, str]]:
    """Sources to query. "All sources" includes new Gmail alerts once Gmail is connected.

    Gmail alerts are tracked per CV and only marked analysed after screening, so they need a
    selected CV and smart matching; otherwise they are skipped (or rejected when alone).
    """
    names = list(req.query.sources or ALL_SOURCES)
    if not req.query.sources and GmailAuth(ws.settings).connected:
        names.append("gmail_alerts")
    if "gmail_alerts" not in names or (req.use_cv and req.smart):
        if not req.query.sources and "gmail_alerts" in names and not _has_local_inbox(ws):
            names.remove("inbox")  # Gmail already carries the LinkedIn/Indeed alerts.
        return names, {}
    if names == ["gmail_alerts"]:
        raise ValueError("New Gmail alerts require a selected CV and smart matching")
    names.remove("gmail_alerts")
    return names, {"gmail_alerts": "Needs a selected CV and smart matching"}


def _source_report(
    sources: list[JobSource],
    counts: dict[str, int],
    errors: dict[str, str],
    skipped: dict[str, str],
) -> list[SourceReport]:
    """Which sources ran, how many postings each returned, and which failed or were skipped."""
    report: list[SourceReport] = []
    for src in sources:
        if src.name in errors:
            report.append(SourceReport(name=src.name, status="failed", detail=errors[src.name]))
            continue
        partial = sum(1 for key in errors if key.startswith(f"{src.name}:"))
        report.append(
            SourceReport(
                name=src.name,
                status="used",
                fetched=counts.get(src.name, 0),
                detail=f"{partial} partial error(s)" if partial else None,
            )
        )
    report += [SourceReport(name=n, status="skipped", detail=why) for n, why in skipped.items()]
    return report


def _shortlist(report: MatchReport, settings: Settings) -> list[JobPosting]:
    """Postings for the job_matcher, one per (employer, title): boards list the same role once
    per location (and two sources may spell the employer differently). No fixed size: every
    posting whose title shares a role-specific word with the target roles, then the
    `screen_extra` best others (a relevant role can have an odd title), up to `screen_max_jobs`.
    Ranked without AI verdicts, so it is the same list before and after screening."""
    targets = report.profile.target_titles
    ranked = sorted(
        [*report.matches, *report.below_threshold],
        key=lambda r: (
            not targets or shares_role_words(r.job.title, targets),
            r.score.total if r.score else 0.0,
        ),
        reverse=True,
    )
    seen: set[tuple[str, str]] = set()
    relevant: list[JobPosting] = []
    others: list[JobPosting] = []
    for r in ranked:
        key = (company_key(r.job.company), " ".join(r.job.title.lower().split()))
        if key not in seen:
            seen.add(key)
            on_target = not targets or shares_role_words(r.job.title, targets)
            (relevant if on_target else others).append(r.job)
    if not targets:
        return relevant[: min(settings.screen_untargeted, settings.screen_max_jobs)]
    return [*relevant, *others[: settings.screen_extra]][: settings.screen_max_jobs]


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
