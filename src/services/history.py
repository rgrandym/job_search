"""Search history: the most recent searches with their full results.

Stored in `data/search_history.json` (git-ignored, personal data). Every search run through
`search_service.run_search` is recorded, so the UI, REST API and agents share one history.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, TypeAdapter

from src.jobs.models import MatchReport, SearchQuery
from src.services import tracker
from src.services.workspace import Workspace

if TYPE_CHECKING:
    from src.services.search_service import SearchOutcome, SearchRequest

HISTORY_SIZE = 10
YIELD_SIZE = 200  # searches kept in the source-yield log (it holds counts only)
# Company feeds report one source per board ("greenhouse:acme"); yield groups them.
COMPANY_FEEDS = {
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "personio",
    "teamtailor", "workday", "icims", "careers",
}  # fmt: skip


class HistoryItem(BaseModel):
    """One past search as listed to the user (without its results)."""

    id: str
    created_at: str
    label: str
    query: SearchQuery
    fetched: int
    matches: int
    screened: bool
    models: str | None = None  # who judged the matches, so runs can be compared


def _models(ws: Workspace, outcome: SearchOutcome) -> str | None:
    """Provider, models and efforts, when the job_matcher ran."""
    if not outcome.report.screened:
        return None
    llm = ws.llm
    return (
        f"{llm.provider} · screening {llm.screening_model} ({llm.screening_effort}) "
        f"· quality {llm.quality_model} ({llm.quality_effort})"
    )


def _path(ws: Workspace) -> Any:
    return ws.settings.data_dir / "search_history.json"


def _entries(ws: Workspace) -> list[dict[str, Any]]:
    path = _path(ws)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # a damaged history is not worth failing a search over
    return data if isinstance(data, list) else []


def _save(ws: Workspace, entries: list[dict[str, Any]]) -> None:
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, default=str), encoding="utf-8")


def _label(query: SearchQuery, outcome: SearchOutcome) -> str:
    """What was searched, in words: titles (or the CV roles used) and where."""
    if outcome.alert_only:
        what = "New Gmail alerts"
    else:
        titles = query.titles or outcome.cv_titles
        what = " / ".join(titles[:3]) if titles else "Any title"
    where = "; ".join(query.place_names()) or "anywhere"
    when = f" · last {query.posted_within_days} d" if query.posted_within_days else ""
    return f"{what} · {where}{when}"


def record(ws: Workspace, req: SearchRequest, outcome: SearchOutcome) -> HistoryItem:
    """Add a finished search, keeping only the newest `HISTORY_SIZE`."""
    item = HistoryItem(
        id=uuid.uuid4().hex[:12],
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        label=_label(req.query, outcome),
        query=req.query,
        fetched=outcome.fetched,
        matches=len(outcome.report.matches),
        screened=outcome.report.screened,
        models=_models(ws, outcome),
    )
    entry = {
        **item.model_dump(mode="json"),
        "request": req.model_dump(mode="json"),
        "outcome": outcome.model_dump(mode="json"),
    }
    _save(ws, [entry, *_entries(ws)][:HISTORY_SIZE])
    ws.last_models = item.models
    _log_yield(ws, item.id, item.created_at, outcome)
    return item


def list_history(ws: Workspace) -> list[HistoryItem]:
    """Past searches, newest first."""
    fields = set(HistoryItem.model_fields)
    return TypeAdapter(list[HistoryItem]).validate_python(
        [{k: v for k, v in e.items() if k in fields} for e in _entries(ws)]
    )


def open_entry(ws: Workspace, entry_id: str) -> tuple[SearchRequest, SearchOutcome]:
    """A past search's request and results; it becomes the workspace's current report, so its
    jobs can be tailored and discussed with the agent again."""
    from src.services.search_service import SearchOutcome, SearchRequest, count_unscreened

    entry = next((e for e in _entries(ws) if e.get("id") == entry_id), None)
    if entry is None:
        raise ValueError("That search is no longer in the history")
    req = SearchRequest.model_validate(entry["request"])
    outcome = SearchOutcome.model_validate(entry["outcome"])
    outcome.history_id = entry_id
    # Entries saved before this was recorded (or with failed batches) can still be continued.
    outcome.unscreened = count_unscreened(ws, req, outcome.report)
    tracker.refresh(ws, outcome.report)  # statuses and notes may have changed since
    ws.last_query, ws.last_report = req.query, outcome.report
    ws.last_models = entry.get("models")
    return req, outcome


def screened_reports(
    ws: Workspace, skip: set[str] | None = None
) -> list[tuple[str, str, MatchReport]]:
    """(entry id, models line, report) of each screened search, newest first, except the
    ids in `skip`. An entry that no longer validates is left out."""
    out = []
    for e in _entries(ws):
        if not e.get("models") or e.get("id") in (skip or set()):
            continue
        try:
            report = MatchReport.model_validate(e["outcome"]["report"])
        except (KeyError, ValueError):
            continue
        out.append((str(e["id"]), str(e["models"]), report))
    return out


def update(ws: Workspace, entry_id: str, outcome: SearchOutcome) -> None:
    """Replace a past search's results (after Continue judged the rest), keeping its place."""
    entries = _entries(ws)
    for entry in entries:
        if entry.get("id") == entry_id:
            entry["outcome"] = outcome.model_dump(mode="json")
            entry["matches"] = len(outcome.report.matches)
            entry["screened"] = outcome.report.screened
            entry["models"] = _models(ws, outcome) or entry.get("models")
            _log_yield(ws, entry_id, str(entry.get("created_at", "")), outcome)
    _save(ws, entries)


def delete_entry(ws: Workspace, entry_id: str) -> bool:
    entries = _entries(ws)
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) != len(entries):
        _save(ws, kept)
    return len(kept) != len(entries)


def clear(ws: Workspace) -> None:
    _save(ws, [])


# ------------------------------------------------------------------ source yield


class SourceYield(BaseModel):
    """What one source has produced over the logged searches."""

    source: str  # "reed", "linkedin_search", "linkedin_alert", "company", ...
    searches: int  # searches it took part in
    found: int  # unique postings it supplied
    matches: int  # of which true matches
    last_match: str | None = None  # date of the latest search where it gave a match


def source_origin(source: str) -> str:
    """The source a posting came from, with every company feed grouped as "company"."""
    head = source.split(":")[0]
    return "company" if head in COMPANY_FEEDS else head


def _yield_counts(report: MatchReport, used: list[str]) -> dict[str, list[int]]:
    """{source: [postings found, true matches]}; sources searched without result count 0."""
    counts: dict[str, list[int]] = {name: [0, 0] for name in used}
    for result in report.all_results():
        counts.setdefault(source_origin(result.job.source), [0, 0])[0] += 1
    for result in report.matches:
        counts[source_origin(result.job.source)][1] += 1
    return counts


def _yield_path(ws: Workspace) -> Any:
    return ws.settings.data_dir / "source_yield.json"


def _yield_log(ws: Workspace) -> list[dict[str, Any]]:
    try:
        data = json.loads(_yield_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _log_yield(ws: Workspace, entry_id: str, created_at: str, outcome: SearchOutcome) -> None:
    """Record (or, after Continue, replace) one search's counts per source."""
    # A board searched without result still counts (as 0). Inbox and Gmail postings carry
    # their alert's origin ("linkedin_alert") instead of the source's name, so they are left out.
    alerts = {"inbox", "linkedin", "indeed", "gmail_alerts", "demo"}
    used = [s.name for s in outcome.sources if s.status == "used" and s.name not in alerts]
    row = {"id": entry_id, "at": created_at, "counts": _yield_counts(outcome.report, used)}
    log = [r for r in _yield_log(ws) if r.get("id") != entry_id]
    path = _yield_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([row, *log][:YIELD_SIZE]), encoding="utf-8")


def source_yield(ws: Workspace) -> list[SourceYield]:
    """Per source, over the logged searches: how often it ran, what it found and matched."""
    totals: dict[str, SourceYield] = {}
    for row in _yield_log(ws):
        for name, (found, matches) in dict(row.get("counts", {})).items():
            y = totals.setdefault(name, SourceYield(source=name, searches=0, found=0, matches=0))
            y.searches += 1
            y.found += int(found)
            y.matches += int(matches)
            day = str(row.get("at", ""))[:10]
            if matches and (y.last_match is None or day > y.last_match):
                y.last_match = day
    return sorted(totals.values(), key=lambda y: (y.matches, y.found), reverse=True)


# ------------------------------------------------------------------ role-family yield

# A family that has run in this many searches without one true match is flagged as not
# landing: the realism check made in advance, measured against the matcher's own verdicts.
LANDING_MIN_SEARCHES = 3


class FamilyYield(BaseModel):
    """What one role family's searches have produced for one CV."""

    family: str
    tier: str
    searches: int
    found: int  # postings attributed to the family (by title)
    matches: int  # of which true matches
    landing: bool | None = Field(
        None, description="False: no match after LANDING_MIN_SEARCHES searches; None: too early"
    )


def _family_path(ws: Workspace) -> Any:
    return ws.settings.data_dir / "family_yield.json"


def _family_log(ws: Workspace) -> list[dict[str, Any]]:
    try:
        data = json.loads(_family_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def log_family_yield(
    ws: Workspace, entry_id: str, owner: str, families: dict[str, str], report: MatchReport
) -> None:
    """Record (or, after Continue, replace) one search's counts per searched role family
    (`families`: name -> tier); results carry their family from `search_service`."""
    if not families:
        return
    counts: dict[str, dict[str, Any]] = {
        name: {"tier": tier, "found": 0, "matches": 0} for name, tier in families.items()
    }
    for result in report.all_results():
        if result.family in counts:
            counts[result.family]["found"] += 1
    for result in report.matches:
        if result.family in counts:
            counts[result.family]["matches"] += 1
    at = datetime.now(UTC).isoformat(timespec="seconds")
    row = {"id": entry_id, "at": at, "owner": owner, "counts": counts}
    log = [r for r in _family_log(ws) if r.get("id") != entry_id]
    path = _family_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([row, *log][:YIELD_SIZE]), encoding="utf-8")


def family_yield(ws: Workspace, owner: str) -> list[FamilyYield]:
    """Per role family of one CV, over the logged searches; `landing` flags the ones the
    matcher never accepts, so unrealistic adjacent families show up in the data."""
    totals: dict[str, FamilyYield] = {}
    for row in _family_log(ws):
        if row.get("owner") != owner:
            continue
        for name, c in dict(row.get("counts", {})).items():
            y = totals.setdefault(
                name, FamilyYield(family=name, tier=str(c["tier"]), searches=0, found=0, matches=0)
            )
            y.searches += 1
            y.found += int(c["found"])
            y.matches += int(c["matches"])
    for y in totals.values():
        y.landing = True if y.matches else (False if y.searches >= LANDING_MIN_SEARCHES else None)
    return sorted(totals.values(), key=lambda y: (y.matches, y.found), reverse=True)
