"""Saved jobs persist across searches and move out of the list when applied."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.cv.models import MasterCV
from src.jobs.matcher import build_profile
from src.jobs.models import JobPosting, MatchReport, MatchResult
from src.services import saved, tracker
from src.services.workspace import Workspace


def _report(ws: Workspace, *ids: str) -> MatchReport:
    assert ws.master_cv is not None
    jobs = [JobPosting(id=i, title=f"Scientist {i}", company=f"Co {i}") for i in ids]
    return MatchReport(
        profile=build_profile(ws.master_cv),
        threshold=60,
        matches=[MatchResult(job=j) for j in jobs],
    )


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    w = Workspace(settings)
    w.master_cv, w.active_cv_id = master_cv, "master"
    monkeypatch.setattr(w, "llm_ready", lambda: True)
    return w


def test_saved_jobs_outlive_the_search_and_track_applications_live(ws: Workspace) -> None:
    ws.last_report = _report(ws, "a", "b")
    assert [s.result.job.id for s in saved.save(ws, ["a", "b", "a"])] == ["a", "b"]
    assert saved.save(ws, ["a"]) == []  # already saved: kept as it is
    with pytest.raises(ValueError, match="Not in the current search: zz"):
        saved.save(ws, ["zz"])

    ws.last_report = _report(ws, "c")  # a new search: saved jobs are still found
    job = ws.job("a")
    assert job is not None and job.title == "Scientist a"
    tracker.set_status(ws, job, "applied", stage="interview")
    status = {s.result.job.id: s.result.tracking for s in saved.list_saved(ws)}
    assert "a" not in status
    assert status["b"] is None
    applied = next(e for e in tracker.register(ws) if e.title == "Scientist a")
    assert applied.stage == "interview"
    assert applied.application_result and applied.application_result.job.id == "a"

    assert saved.remove(ws, ["a", "nope"]) == 1
    assert [s.result.job.id for s in saved.list_saved(ws)] == ["b"]
    assert any(e.title == "Scientist a" for e in tracker.register(ws))  # application kept


def test_api_save_list_and_remove(ws: Workspace, monkeypatch: Any) -> None:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    client = TestClient(webapp.app)
    ws.last_report = _report(ws, "a")
    assert client.get("/api/saved").json() == []
    assert len(client.post("/api/saved", json={"job_ids": ["a"]}).json()) == 1
    assert client.post("/api/saved", json={"job_ids": ["zz"]}).status_code == 404
    listed = client.get("/api/saved").json()
    assert listed[0]["result"]["job"]["id"] == "a" and listed[0]["saved_at"]
    ws.last_report = _report(ws, "b")
    marked = client.put("/api/jobs/a/tracking", json={"status": "applied"})  # a saved job
    assert marked.status_code == 200 and marked.json()["status"] == "applied"
    assert client.post("/api/saved/remove", json={"job_ids": ["a"]}).json() == {"removed": 1}


def test_deleting_a_result_clears_it_from_every_list_but_the_register(ws: Workspace) -> None:
    from src.jobs.models import SearchQuery
    from src.services import history, search_service
    from src.services.search_service import SearchOutcome, SearchRequest

    req = SearchRequest(query=SearchQuery())
    older = history.record(ws, req, SearchOutcome(report=_report(ws, "a", "b"), fetched=2))
    newer = history.record(ws, req, SearchOutcome(report=_report(ws, "a", "c"), fetched=2))
    ws.last_report = _report(ws, "a", "b", "c")
    saved.save(ws, ["a", "b"])
    tracker.set_status(ws, JobPosting(id="b", title="Scientist b", company="Co b"), "applied")

    assert search_service.remove_result(ws, "a", newer.id)
    assert "a" not in {r.job.id for r in ws.last_report.all_results()}
    for entry in (older, newer):  # every saved search, not only the one on screen
        _, outcome = history.open_entry(ws, entry.id)
        assert "a" not in {r.job.id for r in outcome.report.all_results()}
    assert [s.result.job.id for s in ws.saved_jobs()] == ["b"]

    ws.last_report = _report(ws, "b")
    assert search_service.remove_result(ws, "b", None)
    assert ws.saved_jobs() == []
    assert any(e.title == "Scientist b" for e in tracker.register(ws))  # the application stays
