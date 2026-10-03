"""Outcomes (stages, reasons, read-only calibration), the evidence review queue, the
assistant's career-intent tool and the REST routes for intent, cover letters, outcomes and
evidence."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.agents.definitions import ASSISTANT
from src.agents.runtime import AgentContext, run_agent
from src.core.config import Settings
from src.core.llm import ChatMessage
from src.cv.models import (
    CoverLetterDraft,
    EvidenceProposal,
    EvidenceProposals,
    JDAnalysis,
    LetterParagraph,
    MasterCV,
)
from src.jobs.matcher import build_profile
from src.jobs.models import JobPosting, MatchReport, MatchResult
from src.services import calibration, enrichment, intent, tracker
from src.services.workspace import Workspace
from tests.conftest import FakeLLM
from tests.test_webapp import ScriptedChat, _call

DOC = (
    "Portfolio. I hold the Google Professional Data Engineer certificate. "
    "At Nimbus I also built an A/B testing platform used by 6 product teams."
)


def _proposals(_: str) -> EvidenceProposals:
    def item(kind: str, text: str, quote: str, **kw: Any) -> EvidenceProposal:
        return EvidenceProposal(kind=kind, text=text, quote=quote, confidence="high", **kw)  # type: ignore[arg-type]

    return EvidenceProposals(
        items=[
            item("certification", "Google Professional Data Engineer",
                 "I hold the Google Professional Data Engineer certificate"),
            item("bullet", "Built an A/B testing platform used by 6 product teams.",
                 "built an A/B testing platform used by 6 product teams", attach_to="nimbus"),
            item("skill", "Rust", "I write Rust daily"),  # quote not in the document
            item("skill", "Python", "Portfolio"),  # already in the CV
            item("bullet", "Led 12 teams.", "At Nimbus I also built", attach_to="nimbus"),
        ]
    )  # fmt: skip


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    w = Workspace(settings)
    w.master_cv, w.active_cv_id = master_cv, "master"
    llm = FakeLLM(
        {
            EvidenceProposals: _proposals,
            JDAnalysis: JDAnalysis(job_title="ML Engineer", company="Orbit"),
            CoverLetterDraft: CoverLetterDraft(
                paragraphs=[
                    LetterParagraph(
                        text="I am interested in the ML Engineer role.", source_ids=["nimbus"]
                    ),
                    LetterParagraph(text="I built recommenders.", source_ids=["nimbus"]),
                    LetterParagraph(
                        text="I would welcome a conversation about the role.", source_ids=["nimbus"]
                    ),
                ]
            ),
        }
    )
    monkeypatch.setattr(w, "structured", lambda *a, **k: llm)
    monkeypatch.setattr(w, "llm_ready", lambda: True)
    monkeypatch.setattr(
        "src.services.cv_service.save_selected_cv", lambda ws, cv: setattr(ws, "master_cv", cv)
    )
    return w


def _job(n: int, title: str = "Machine Learning Engineer") -> JobPosting:
    return JobPosting(id=f"j{n}", title=title, company=f"Co{n}")


# ---------------------------------------------------------------- outcomes


def test_stages_are_dated_and_reasons_kept(ws: Workspace) -> None:
    job = _job(1)
    tracker.set_status(ws, job, "applied", today=date(2026, 9, 1))
    tracking = tracker.set_status(ws, job, "applied", stage="interview", today=date(2026, 9, 10))
    assert tracking.stage == "interview"
    entry = tracker.load(ws)[tracker.role_id(job.title, job.company)]
    assert [(e.stage, e.at) for e in entry.stages] == [("interview", "2026-09-10")]
    tracker.set_status(ws, _job(2), "na", reason="Too much travel")
    assert tracker.load(ws)[tracker.role_id("Machine Learning Engineer", "Co2")].reason == (
        "Too much travel"
    )


def _decide(ws: Workspace, n: int, status: Any, fit: int, family: str, **kw: Any) -> None:
    tracker.set_status(ws, _job(n), status, **kw)
    entries = tracker.load(ws)
    entry = entries[tracker.role_id("Machine Learning Engineer", f"Co{n}")]
    entry.fit_score, entry.family = fit, family
    tracker._save(ws, entries, date.today())


def test_review_finds_patterns_only_with_enough_cases(ws: Workspace) -> None:
    reasons = ["too much travel", "constant travel to sites", "travel every week", "low pay"]
    for n, reason in enumerate(reasons):
        _decide(ws, n, "na", 82, "Field applications", reason=reason)
    _decide(ws, 9, "na", 50, "Field applications", reason="travel")  # low fit: not a signal
    for n in range(10, 13):
        _decide(ws, n, "applied", 80, "Business development", stage="rejected")
    tracker.set_status(ws, _job(20), "applied", today=date(2026, 9, 1))
    review = calibration.review_outcomes(ws, today=date(2026, 10, 2))
    labels = {(p.kind, p.label): p for p in review.patterns}
    assert labels[("dismissal_reason", "travel")].count == 3
    assert ("dismissal_reason", "pay") not in labels  # one case is not a pattern
    assert labels[("dismissed_family", "Field applications")].count == 5
    assert (
        "do not make it unrealistic"
        in labels[("family_outcome", "Business development")].suggestion
    )
    assert review.high_fit_dismissals == 4 and review.by_stage["rejected"] == 3
    assert review.quiet == ["Machine Learning Engineer at Co20 (applied 2026-09-01)"]


# ---------------------------------------------------------------- evidence review


def test_evidence_keeps_only_quoted_new_facts_and_writes_on_accept(ws: Workspace) -> None:
    queued = asyncio.run(enrichment.propose(ws, "portfolio.md", DOC))
    assert [q.kind for q in queued] == ["certification", "bullet"]  # 3 proposals dropped
    cert, bullet = queued
    enrichment.decide(ws, cert.id, accept=False)
    added = enrichment.decide(ws, bullet.id, accept=True)
    assert added.status == "accepted" and enrichment.queue(ws) == []
    assert ws.master_cv is not None
    nimbus = next(e for e in ws.master_cv.experience if e.id == "nimbus")
    assert nimbus.bullets[-1].text.startswith("Built an A/B testing platform")
    assert nimbus.bullets[-1].metrics == ["6"] and nimbus.bullets[-1].id == "nimbus-added"
    with pytest.raises(ValueError, match="not pending"):
        enrichment.decide(ws, bullet.id, accept=True)


def test_accepted_skill_and_project_land_in_their_sections(master_cv: MasterCV) -> None:
    def queued(kind: str, text: str, **kw: Any) -> enrichment.QueuedEvidence:
        return enrichment.QueuedEvidence(
            id="x", owner="m", source="d", kind=kind, text=text, quote="q",  # type: ignore[arg-type]
            confidence="high", created_at="", **kw,
        )  # fmt: skip

    cv = enrichment.add_to_cv(master_cv, queued("skill", "Rust"))
    assert cv.skills[-1].category == "Additional" and cv.skills[-1].items == ["Rust"]
    cv = enrichment.add_to_cv(cv, queued("project", "Open-source ranking library", name="Ranky"))
    assert cv.projects[-1].id == "ranky" and cv.projects[-1].name == "Ranky"


# ---------------------------------------------------------------- agent & API


def test_assistant_records_career_intent_from_the_users_words(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = {
        "assistant": [
            _call(
                "update_search_intent",
                patch={"target_areas": ["business development"]},
                reason="I'd like to move into BD",
            ),  # fmt: skip
            ChatMessage(role="assistant", content="Noted: business development."),
        ]
    }
    monkeypatch.setattr(ws, "chat", lambda role: ScriptedChat(script))
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    messages = [ChatMessage(role="user", content="I'd like to move into BD")]
    asyncio.run(run_agent(ASSISTANT, messages, AgentContext(ws=ws, emit=emit)))
    assert intent.get_intent(ws).target_areas == ["business development"]
    assert "intent_updated" in events
    assert "target_areas: + business development" in messages[2].content


def test_api_intent_outcomes_evidence_and_cover_letter(ws: Workspace, monkeypatch: Any) -> None:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    client = TestClient(webapp.app)
    assert client.get("/api/intent").json()["direction"] == ""
    saved = client.put("/api/intent", json={"direction": "Lead ML", "languages": [
        {"language": "German", "level": "basic"}]}).json()  # fmt: skip
    assert saved["updated_at"] and intent.get_intent(ws).direction == "Lead ML"
    assert client.put("/api/intent", json={"bogus": 1}).status_code == 422

    assert client.get("/api/outcomes/review").json()["applications"] == 0
    queued = client.post("/api/evidence/text", json={"source": "site", "text": DOC}).json()
    assert len(queued) == 2 and len(client.get("/api/evidence").json()) == 2
    decided = client.post(f"/api/evidence/{queued[0]['id']}", json={"accept": False}).json()
    assert decided["status"] == "rejected"
    assert client.post("/api/evidence/nope", json={"accept": True}).status_code == 404

    job = JobPosting(id="j1", title="ML Engineer", company="Orbit", description="Build ML.")
    ws.last_report = MatchReport(
        profile=build_profile(ws.master_cv),
        threshold=60,
        matches=[MatchResult(job=job)],  # type: ignore[arg-type]
    )
    letter = client.post("/api/jobs/j1/cover-letter", json={}).json()
    assert letter["download_url"].endswith("_Orbit_ML_Engineer_cover_letter.docx")
    assert letter["paragraphs"] == 3
    assert client.post("/api/jobs/nope/cover-letter", json={}).status_code == 404
    tracked = client.put("/api/jobs/j1/tracking", json={"status": "applied", "stage": "screening"})
    assert tracked.json()["stage"] == "screening"
