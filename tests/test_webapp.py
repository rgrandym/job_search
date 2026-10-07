"""Smart matching pipeline, summary memory, agent loop, and the web API (all offline)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from src.agents import chat
from src.agents.definitions import ASSISTANT
from src.agents.runtime import AgentContext, run_agent
from src.core.config import ScoringWeights, Settings
from src.core.llm import ChatMessage, ChatResponse, ToolCall, ToolSpec
from src.cv.models import MasterCV
from src.jobs import scorer
from src.jobs.matcher import build_profile, profile_from_query
from src.jobs.models import (
    FIT_WEIGHTS,
    FitRatings,
    JobAssessment,
    JobPosting,
    ProfileSummary,
    SearchQuery,
    SkillEvidence,
)
from src.jobs.profile_memory import ProfileMemory, role_family, summarize_profile
from src.jobs.screener import ScreeningBatch
from src.jobs.sources.gmail_alerts import GmailAlertSource
from src.services import search_service
from src.services.search_service import SearchRequest, run_search
from src.services.workspace import Workspace
from src.tools.search_tools import board_terms, extract_skills, parse_salary, title_core
from tests.conftest import FakeLLM

SUMMARY = ProfileSummary(
    headline="Senior ML engineer, recommender systems and NLP in production",
    seniority="senior",
    years_experience=8.6,
    core_expertise=["production recommender systems", "NLP classification"],
    key_skills=[SkillEvidence(skill="PyTorch", level="expert", evidence="Nimbus recsys")],
    target_roles=["Senior Machine Learning Engineer", "Applied Scientist"],
    not_a_fit=["Data engineering (pipelines-only roles)"],
    summary="Strong applied ML engineer.",
)


def _screen(prompt: str) -> ScreeningBatch:
    """Fake job_matcher: accept ML engineer roles, reject everything else."""
    verdicts = []
    for job_id, title in re.findall(r'job_id="([^"]+)">\ntitle: ([^\n]+)', prompt):
        ok = "Machine Learning" in title
        levels = (4, 4, 3, 4, 3, 3) if ok else (1, 2, 2, 1, 1, 1)  # 91 vs 37
        verdicts.append(
            JobAssessment(
                job_id=job_id,
                ratings=FitRatings(**dict(zip(FIT_WEIGHTS, levels, strict=True))),
                fit_summary="Direct match" if ok else "Different discipline",
                reasons=["core ML role"] if ok else [],
                gaps=[] if ok else ["different discipline"],
            )
        )
    return ScreeningBatch(verdicts=verdicts)


def fake_llm() -> FakeLLM:
    return FakeLLM({ProfileSummary: SUMMARY, ScreeningBatch: _screen})


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    w = Workspace(settings)
    w.master_cv = master_cv
    w.active_cv_id = "master"
    llm = fake_llm()
    monkeypatch.setattr(w, "structured", lambda role="worker", *_: llm)
    monkeypatch.setattr(w, "llm_ready", lambda: True)
    return w


# ---------------------------------------------------------------- matching utilities


def test_title_abbreviations_and_skill_extraction() -> None:
    assert title_core("Sr. ML Eng") == title_core("Senior Machine Learning Engineer")
    text = "Python and k8s on AWS; R&D culture; go to market"
    assert extract_skills(text) == ["amazon web services", "kubernetes", "python"]
    assert parse_salary("£50,000 - £60k") == (50000, 60000)
    assert parse_salary("£450 per day") == (None, None)


def test_inferred_skills_when_posting_has_no_lists(master_cv: MasterCV) -> None:
    job = JobPosting(
        id="x",
        title="ML Engineer",
        company="A",
        description="You will build PyTorch models on Kubernetes with Spark.",
    )
    s = scorer.score(build_profile(master_cv), job, 0.3, ScoringWeights())
    assert "skills inferred from the description" in s.notes
    assert s.missing_required_skills == ["spark"]
    assert s.skills == 0.75  # has kubernetes, machine learning (title), pytorch; lacks spark


def test_salary_floor_and_filter_overrides(master_cv: MasterCV) -> None:
    q = SearchQuery(
        titles=["Staff ML Engineer"],
        locations=["London, UK"],
        salary_min=90000,
        work_arrangements=["onsite", "hybrid"],
    )
    p = build_profile(master_cv, query=q)
    assert p.target_titles == ["Staff ML Engineer"] and p.locations == ["London, UK"]
    cheap = JobPosting(
        id="c",
        title="ML Engineer",
        company="A",
        location="London, UK",
        salary_range="£60,000 - £70,000",
    )
    assert any("below minimum" in r for r in scorer.hard_exclusions(p, cheap))
    leeds = JobPosting(id="l", title="ML Engineer", company="A", location="Leeds, UK")
    assert scorer.score_location(p, leeds) == scorer.LOC_SAME_COUNTRY
    radius = leeds.model_copy(update={"within_search_area": True})
    assert scorer.score_location(p, radius) == 1.0


def test_filters_only_profile_scores_without_cv() -> None:
    p = profile_from_query(SearchQuery(titles=["Data Scientist"]))
    job = JobPosting(id="d", title="Senior Data Scientist", company="A", work_arrangement="remote")
    s = scorer.score(p, job, 0.2, ScoringWeights())
    assert not p.cv_based and s.missing_required_skills == []
    assert s.total > 60


# ---------------------------------------------------------------- summary memory


def test_summary_is_remembered_per_role_family(master_cv: MasterCV, tmp_path: Any) -> None:
    llm, memory = fake_llm(), ProfileMemory(tmp_path / "mem.json")
    q1 = SearchQuery(titles=["ML Engineer"], locations=["Lisbon"])
    q2 = SearchQuery(titles=["Machine Learning Engineer"], locations=["Remote"])
    _, cached1 = summarize_profile(master_cv, q1, llm, memory)
    _, cached2 = summarize_profile(master_cv, q2, llm, memory)  # same family, other filters
    assert (cached1, cached2) == (False, True)
    assert role_family(q1) == role_family(q2)
    _, cached3 = summarize_profile(master_cv, SearchQuery(titles=["Data Engineer"]), llm, memory)
    assert cached3 is False and len(llm.calls) == 2
    assert ProfileMemory(tmp_path / "mem.json").get  # persisted file reloads


# ---------------------------------------------------------------- smart search pipeline


def test_smart_search_uses_job_matcher_verdicts(ws: Workspace) -> None:
    req = SearchRequest(query=SearchQuery(sources=["demo"]), smart=True, threshold=70)
    out = asyncio.run(run_search(ws, req))
    rep = out.report
    assert rep.screened and rep.summary == SUMMARY
    assert [r.job.id for r in rep.matches] == ["job-strong"]
    assert rep.matches[0].verdict and rep.matches[0].verdict.fit_score == 91
    partial = next(r for r in rep.below_threshold if r.job.id == "job-partial")
    assert partial.verdict and not partial.verdict.match
    assert {r.job.id for r in rep.excluded} == {"job-cert", "job-onsite"}
    assert any(r.job.id == "job-junior" for r in rep.below_threshold)
    out2 = asyncio.run(run_search(ws, req))
    assert out2.summary_from_memory is True


def test_search_without_llm_falls_back_to_prefilter(ws: Workspace, monkeypatch: Any) -> None:
    monkeypatch.setattr(ws, "llm_ready", lambda: False)
    req = SearchRequest(query=SearchQuery(sources=["demo"]), smart=True, threshold=70)
    out = asyncio.run(run_search(ws, req))
    assert not out.report.screened and out.smart_unavailable
    assert [r.job.id for r in out.report.matches] == ["job-strong"]


def test_new_gmail_alerts_marked_only_after_screening(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AlertStub(GmailAlertSource):
        name = "gmail_alerts"

        def __init__(self) -> None:
            self.marked = 0

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            return [
                JobPosting(
                    id="indeed:abc123",
                    title="Senior Machine Learning Engineer",
                    company="Orbit AI",
                    location="London",
                    work_arrangement="remote",
                    source="indeed_alert",
                )
            ]

        def mark_analyzed(self, job_ids: set[str]) -> None:
            assert job_ids == {"indeed:abc123"}
            self.marked += 1

    source = AlertStub()
    keys: list[str | None] = []

    def build_sources(_: Any, *, settings: Settings, cv_key: str | None, **__: Any) -> Any:
        keys.append(cv_key)
        return [source], {}

    monkeypatch.setattr(search_service, "build_sources", build_sources)
    request = SearchRequest(query=SearchQuery(sources=["gmail_alerts"]), smart=True)
    result = asyncio.run(run_search(ws, request))
    assert result.alert_only and result.report.screened and source.marked == 1
    assert keys[0] and keys[0].startswith("master:")

    monkeypatch.setattr(ws, "llm_ready", lambda: False)
    result = asyncio.run(run_search(ws, request))
    assert result.smart_unavailable and source.marked == 1


def test_empty_titles_search_the_cv_profile_roles_on_job_boards(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    searched: dict[str, list[str]] = {}

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            searched[self.name] = list(query.titles)
            return []

    def build_sources(names: list[str], *, settings: Settings, cv_key: str | None, **_: Any) -> Any:
        return [Recorder("reed"), Recorder("demo")], {}

    monkeypatch.setattr(search_service, "build_sources", build_sources)
    request = SearchRequest(query=SearchQuery(), smart=True)

    first = asyncio.run(run_search(ws, request))
    roles = board_terms(SUMMARY.target_roles, SUMMARY.search_keywords, search_service.BOARD_TERMS)
    assert first.cv_titles == roles and first.summary_from_memory is False
    assert searched == {"reed": roles, "demo": []}  # local feeds keep the user's filters

    again = asyncio.run(run_search(ws, request))  # same CV: stored profile, same search
    assert again.cv_titles == roles and again.summary_from_memory is True

    titled = SearchRequest(query=SearchQuery(titles=["Data Engineer"]), smart=True)
    assert asyncio.run(run_search(ws, titled)).cv_titles == []
    assert searched["reed"] == ["Data Engineer"]

    composite = SearchQuery(titles=["Principal Scientist, Cell Therapy / iPSC"])
    asyncio.run(run_search(ws, SearchRequest(query=composite, smart=True)))
    assert searched["reed"] == ["Principal Scientist Cell Therapy", "Principal Scientist iPSC"]


def test_all_sources_include_connected_gmail_and_report_each_source(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.jobs.sources.base import SourceError
    from src.jobs.sources.gmail_alerts import GmailAuth

    class Stub:
        def __init__(self, name: str, fail: bool = False) -> None:
            self.name, self.fail = name, fail

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            if self.fail:
                raise SourceError("service down")
            return [JobPosting(id=f"{self.name}:1", title="ML Engineer", company="Orbit AI")]

    requested: list[list[str]] = []

    def build_sources(names: list[str], *, settings: Settings, cv_key: str | None, **_: Any) -> Any:
        requested.append(list(names))
        return [Stub("reed"), Stub("adzuna", fail=True)], {"cv_library": "no API key"}

    monkeypatch.setattr(search_service, "build_sources", build_sources)
    monkeypatch.setattr(GmailAuth, "connected", property(lambda self: True))
    monkeypatch.setattr(ws, "llm_ready", lambda: False)

    out = asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(), smart=True)))
    assert requested[0][-1] == "gmail_alerts"
    assert "inbox" not in requested[0]  # Gmail carries the LinkedIn/Indeed alerts
    report = {s.name: s for s in out.sources}
    assert (report["reed"].status, report["reed"].fetched) == ("used", 1)
    assert (report["adzuna"].status, report["adzuna"].detail) == ("failed", "service down")
    assert (report["cv_library"].status, report["cv_library"].detail) == ("skipped", "no API key")

    (ws.settings.inbox_dir / "postings").mkdir(parents=True)
    (ws.settings.inbox_dir / "postings" / "saved.html").write_text("<html></html>")
    asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(), smart=True)))
    assert "inbox" in requested[1]  # the user's own saved postings still get read

    out = asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(), smart=False)))
    assert "gmail_alerts" not in requested[2] and "inbox" in requested[2]
    gmail = next(s for s in out.sources if s.name == "gmail_alerts")
    assert gmail.status == "skipped" and "smart matching" in (gmail.detail or "")


# ---------------------------------------------------------------- agent loop


class ScriptedChat:
    """Fake ChatModel: replays canned assistant turns per agent (by system prompt)."""

    def __init__(self, script: dict[str, list[ChatMessage]]) -> None:
        self.script = script

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        return ChatResponse(message=self.script["assistant"].pop(0), stop_reason="end_turn")


def _call(name: str, **args: Any) -> ChatMessage:
    return ChatMessage(
        role="assistant", tool_calls=[ToolCall(id=f"c-{name}", name=name, arguments=args)]
    )


def test_assistant_updates_search_preferences(ws: Workspace, monkeypatch: Any) -> None:
    from src.services import cv_service

    script = {
        "assistant": [
            _call("update_preferences", patch={"locations": ["Cambridge, UK"]}, reason="move"),
            ChatMessage(role="assistant", content="Done: Cambridge, UK."),
        ]
    }
    monkeypatch.setattr(ws, "chat", lambda role: ScriptedChat(script))
    monkeypatch.setattr(cv_service, "save_selected_cv", lambda w, cv: setattr(w, "master_cv", cv))
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    messages = [ChatMessage(role="user", content="I'm moving to Cambridge")]
    answer = asyncio.run(run_agent(ASSISTANT, messages, AgentContext(ws=ws, emit=emit)))
    assert answer == "Done: Cambridge, UK."
    assert ws.master_cv and ws.master_cv.preferences.locations == ["Cambridge, UK"]
    assert "cv_updated" in events and not any(m.is_error for m in messages if m.role == "tool")


def test_tool_errors_are_returned_to_the_model(ws: Workspace, monkeypatch: Any) -> None:
    script = {
        "assistant": [
            _call("search_jobs"),  # searching is the Search button, not an assistant tool
            ChatMessage(role="assistant", content="ok"),
        ]
    }
    monkeypatch.setattr(ws, "chat", lambda role: ScriptedChat(script))

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        return None

    messages = [ChatMessage(role="user", content="hi")]
    asyncio.run(run_agent(ASSISTANT, messages, AgentContext(ws=ws, emit=emit)))
    assert messages[2].is_error and "not available" in messages[2].content


# ---------------------------------------------------------------- web API


@pytest.fixture
def client(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    return TestClient(webapp.app)


def test_api_company_boards_status_and_update(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import company_discovery

    st = client.get("/api/companies/status").json()
    assert st["unchecked"] == -1 and st["boards"] == 0 and not st["running"]
    assert "company" in client.get("/api/state").json()["sources"]["available"]

    modes: list[str] = []
    monkeypatch.setattr(
        company_discovery, "discover_companies", lambda _settings, mode: modes.append(mode)
    )
    assert client.post("/api/companies/discover").json()["directory"] == "biopharmguy-uk"
    assert modes == ["stale"]  # Update re-checks new companies and results over 30 days old


def test_api_state_and_search(client: TestClient) -> None:
    st = client.get("/api/state").json()
    assert st["cv"]["name"] == "Alex Example" and "demo" in st["sources"]["available"]
    assert {"linkedin", "indeed", "reed", "cv_library", "adzuna"} <= (
        set(st["sources"]["available"]) | set(st["sources"]["skipped"])
    )
    assert "agents" not in st  # one assistant; searching and documents are buttons
    out = client.post("/api/search", json={"query": {"sources": ["demo"]}, "smart": True}).json()
    assert out["report"]["screened"] and out["report"]["matches"][0]["job"]["id"] == "job-strong"


def test_gmail_oauth_connect_checks_browser_state(
    client: TestClient, ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from src.jobs.sources.gmail_alerts import GmailAuth

    ws.settings = ws.settings.model_copy(
        update={
            "gmail_account": "alerts@example.com",
            "gmail_client_id": "client-id",
            "gmail_client_secret": SecretStr("client-secret"),
        }
    )
    status = client.get("/api/gmail/status").json()
    assert status == {"configured": True, "connected": False, "account": "alerts@example.com"}
    started = client.get("/api/gmail/connect", follow_redirects=False)
    assert started.status_code == 307
    auth_params = parse_qs(urlsplit(started.headers["location"]).query)
    assert auth_params["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert auth_params["login_hint"] == ["alerts@example.com"]
    state = auth_params["state"][0]
    called: list[str] = []
    monkeypatch.setattr(GmailAuth, "exchange", lambda self, code, verifier: called.append(code))
    assert client.get("/api/gmail/callback?state=wrong&code=code").status_code == 400
    assert called == []
    completed = client.get(f"/api/gmail/callback?state={state}&code=code", follow_redirects=False)
    assert completed.status_code == 200 and called == ["code"]
    assert "Gmail alerts connected for alerts@example.com" in completed.text
    assert "window.close()" in completed.text


def test_state_reports_gmail_source_once_connected(
    client: TestClient, ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from src.jobs.sources.gmail_alerts import GmailAuth

    ws.settings = ws.settings.model_copy(
        update={
            "gmail_account": "alerts@example.com",
            "gmail_client_id": "client-id",
            "gmail_client_secret": SecretStr("client-secret"),
        }
    )
    sources = client.get("/api/state").json()["sources"]
    assert sources["skipped"]["gmail_alerts"] == "Click Connect Gmail alerts"

    monkeypatch.setattr(GmailAuth, "connected", property(lambda self: True))
    sources = client.get("/api/state").json()["sources"]
    assert "gmail_alerts" in sources["available"] and "gmail_alerts" not in sources["skipped"]


def test_api_file_download_is_confined(client: TestClient) -> None:
    assert client.get("/api/files/..%2F.env").status_code == 404


def test_api_cv_upload_accepts_multipart_file(
    client: TestClient,
    ws: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness_checks: list[bool] = []
    monkeypatch.setattr(ws, "llm_ready", lambda: readiness_checks.append(True) or True)
    data = b"A sufficiently detailed CV"
    response = client.post(
        "/api/cv/upload",
        files={"file": ("resume.txt", data, "text/plain")},
    )

    assert response.status_code == 200
    asset = response.json()
    assert asset["filename"] == "resume.txt"
    assert asset["kind"] == "uploaded" and asset["selected"] is True
    assert asset["parsed"] is False and asset["size"] == len(data)
    stored = ws.settings.data_dir / "cvs" / "resume.txt"
    assert stored.read_bytes() == data
    assert ws.master_cv is None
    assert readiness_checks == []

    state = client.get("/api/state").json()
    assert state["cv_files"]["selected"] == asset["id"]
    assert state["cv_files"]["available"][0]["filename"] == "resume.txt"


def test_api_selected_cv_can_be_opened_and_edited(
    client: TestClient, ws: Workspace, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import cv_service

    async def parse_uploaded(*_: Any, **__: Any) -> MasterCV:
        return master_cv

    monkeypatch.setattr(cv_service, "import_cv", parse_uploaded)
    asset = client.post(
        "/api/cv/upload",
        files={"file": ("review-me.txt", b"CV source retained unchanged", "text/plain")},
    ).json()
    opened = client.get("/api/cv/editable")
    assert opened.status_code == 200
    assert opened.json()["basics"]["name"] == master_cv.basics.name
    parsed = ws.settings.data_dir / "cvs" / ".parsed" / f"{asset['id']}.json"
    assert parsed.exists()
    assert (ws.settings.data_dir / "cvs" / "review-me.txt").read_bytes() == (
        b"CV source retained unchanged"
    )

    changed = master_cv.model_copy(deep=True)
    changed.basics.summary = "Reviewed summary"
    saved = client.put("/api/cv", json=changed.model_dump(mode="json"))
    assert saved.status_code == 200
    assert saved.json()["basics"]["summary"] == "Reviewed summary"
    assert ws.master_cv and ws.master_cv.basics.summary == "Reviewed summary"
    assert MasterCV.model_validate_json(parsed.read_text()).basics.summary == "Reviewed summary"


def test_cv_source_is_viewed_and_opened_exactly_as_uploaded(
    client: TestClient, ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import document_files

    original = b"%PDF-1.4 original upload, including publications"
    asset = client.post(
        "/api/cv/upload", files={"file": ("cv.pdf", original, "application/pdf")}
    ).json()
    shown = client.get(f"/api/cv/source/{asset['id']}")
    assert shown.status_code == 200
    assert shown.content == original
    assert shown.headers["content-disposition"].startswith("inline")

    opened: list[Path] = []
    apps: list[str | None] = []

    def fake_open(path: Path, app: str | None = None) -> bool:
        opened.append(path)
        apps.append(app)
        return True

    monkeypatch.setattr(document_files, "open_in_default_app", fake_open)
    assert client.post(f"/api/cv/open/{asset['id']}").json() == {"opened": True}
    assert client.post(f"/api/cv/open/{asset['id']}?app=word").json() == {"opened": True}
    # Apps open the copy in output/cvs/, so Word's saves land there and the upload is untouched.
    assert opened == [ws.output_dir / "cvs" / "cv.pdf"] * 2
    assert apps == [None, "Microsoft Word"]
    assert opened[0].read_bytes() == original
    assert (ws.settings.data_dir / "cvs" / "cv.pdf").read_bytes() == original

    # A file saved outside output/cvs/ (e.g. by Word next to the upload) gets a copy there too.
    saved = ws.settings.data_dir / "cvs" / "cv.docx"
    saved.write_bytes(b"PK edited in Word")
    docx_id = next(
        item["id"]
        for item in client.get("/api/state").json()["cv_files"]["available"]
        if item["filename"] == "cv.docx"
    )
    client.post(f"/api/cv/open/{docx_id}?app=word")
    assert opened[-1] == ws.output_dir / "cvs" / "cv.docx"
    assert opened[-1].read_bytes() == b"PK edited in Word"
    edited_copy = b"PK edited again in output"
    opened[-1].write_bytes(edited_copy)
    client.post(f"/api/cv/open/{docx_id}?app=word")
    assert opened[-1].read_bytes() == edited_copy  # earlier edits in the copy are kept
    assert client.get("/api/cv/source/master").status_code == 404


def test_cv_preview_shows_pdfs_as_stored_and_word_files_via_word(
    client: TestClient, ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import document_files

    pdf = client.post(
        "/api/cv/upload", files={"file": ("cv.pdf", b"%PDF-1.4 as uploaded", "application/pdf")}
    ).json()
    assert client.get(f"/api/cv/preview/{pdf['id']}").content == b"%PDF-1.4 as uploaded"

    edited = ws.settings.data_dir / "cvs" / "cv.docx"
    edited.write_bytes(b"PK saved from Word")
    asset = next(
        item
        for item in client.get("/api/state").json()["cv_files"]["available"]
        if item["filename"] == "cv.docx"
    )
    rendered = ws.settings.data_dir / "word.pdf"
    rendered.write_bytes(b"%PDF-1.7 rendered by Word")
    calls: list[Path] = []

    def fake_word(source: Path, cache_dir: Path) -> Path | None:
        calls.append(source)
        return rendered

    monkeypatch.setattr(document_files, "word_pdf_preview", fake_word)
    shown = client.get(f"/api/cv/preview/{asset['id']}")
    assert shown.content == b"%PDF-1.7 rendered by Word"
    assert calls == [edited]
    assert edited.read_bytes() == b"PK saved from Word"

    monkeypatch.setattr(document_files, "word_pdf_preview", lambda source, cache_dir: None)
    missing = client.get(f"/api/cv/preview/{asset['id']}")
    assert missing.status_code == 422
    assert "Open in Word" in missing.json()["detail"]


def test_upload_saves_an_identical_copy_to_output_cvs_immediately(
    client: TestClient, ws: Workspace
) -> None:
    original = b"PK original word file bytes"
    upload = {"file": ("my_cv.docx", original, "application/octet-stream")}
    body = client.post("/api/cv/upload", files=upload).json()
    copy = ws.output_dir / "cvs" / body["filename"]
    assert copy.read_bytes() == original
    assert (ws.settings.data_dir / "cvs" / body["filename"]).read_bytes() == original
    listed = client.get("/api/state").json()["cv_files"]["available"]
    assert [item["kind"] for item in listed] == ["uploaded"]
    client.post("/api/cv/upload", files=upload)  # re-uploading does not add a second copy
    assert sorted(p.name for p in (ws.output_dir / "cvs").iterdir()) == [body["filename"]]
    assert client.delete(f"/api/cv/{body['id']}").status_code == 200
    assert not copy.exists()


def test_delete_uploaded_cv_removes_its_parsed_copy_profiles_and_intent(
    client: TestClient, ws: Workspace, master_cv: MasterCV
) -> None:
    from src.jobs.models import SearchIntent
    from src.services import intent

    asset = client.post(
        "/api/cv/upload", files={"file": ("resume.txt", b"Candidate CV", "text/plain")}
    ).json()
    parsed = ws.settings.data_dir / "cvs" / ".parsed" / f"{asset['id']}.json"
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text(master_cv.model_dump_json())
    ws.memory.put(asset["id"], "any", SUMMARY, cv_fp="old-fingerprint")
    intent.save_intent(ws, SearchIntent(direction="Research leadership"))

    assert client.delete(f"/api/cv/{asset['id']}").json() == {"deleted": True}
    assert not (ws.settings.data_dir / "cvs" / "resume.txt").exists()
    assert not parsed.exists()
    assert ws.memory.records(asset["id"]) == []
    assert intent.get_intent(ws, asset["id"]).direction == ""
    assert client.get("/api/state").json()["cv_files"]["selected"] is None
    assert client.delete(f"/api/cv/{asset['id']}").status_code == 404


def test_delete_master_and_generated_cv_entries(
    client: TestClient, ws: Workspace, master_cv: MasterCV
) -> None:
    from src.cv import master_cv_manager as mgr
    from src.cv.models import JDAnalysis, TailoringPlan
    from src.cv.tailor import apply_plan
    from src.services import tailored_documents

    mgr.save(master_cv, ws.settings.master_cv_path)
    mgr.save(master_cv, ws.settings.master_cv_path)
    backup = ws.settings.master_cv_path.with_suffix(".json.bak")
    assert backup.exists()
    assert client.delete("/api/cv/master").json() == {"deleted": True}
    assert not ws.settings.master_cv_path.exists() and not backup.exists()

    generated = ws.output_dir / "cvs" / "generated.docx"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"Word file")
    jd = JDAnalysis(job_title="ML Engineer")
    document = tailored_documents.create(
        ws,
        "linked-job",
        master_cv,
        jd,
        apply_plan(master_cv, TailoringPlan(), jd),
        "classic",
        generated,
    )
    assert client.delete("/api/cv/generated%3Agenerated.docx").json() == {"deleted": True}
    assert not generated.exists()
    assert not (ws.settings.data_dir / "tailored_cvs" / f"{document.id}.json").exists()
    assert client.get("/api/state").json()["cv_files"]["available"] == []


def test_api_codex_login_and_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.web import app as webapp

    started: list[bool] = []
    monkeypatch.setattr(
        webapp,
        "codex_status",
        lambda: {
            "installed": True,
            "logged_in": True,
            "path": "/bin/codex",
            "message": "Logged in using ChatGPT",
        },
    )
    monkeypatch.setattr(webapp, "start_login", lambda: started.append(True))

    assert client.get("/api/codex/status").json()["logged_in"] is True
    assert client.post("/api/codex/login").json() == {"started": True}
    assert started == [True]


def test_api_codex_usage(
    client: TestClient, ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.web import app as webapp

    ws.llm = ws.llm.model_copy(update={"provider": "codex"})
    usage = {
        "plan_type": "plus",
        "ordinary_usage_allowed": True,
        "primary": {"used_percent": 10, "remaining_percent": 90},
        "secondary": None,
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "lifetime_tokens": 1000,
        "updated_at": 123,
    }
    monkeypatch.setattr(webapp, "codex_usage", lambda: usage)

    assert client.get("/api/codex/usage").json() == usage


def test_api_claude_code_provider(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.llm import claude_code_backend

    started: list[bool] = []
    status = {
        "installed": True,
        "logged_in": True,
        "path": "/bin/claude",
        "subscription": "pro",
        "message": "Signed in with Claude Pro.",
    }
    monkeypatch.setattr(claude_code_backend, "claude_code_status", lambda: status)
    monkeypatch.setattr(claude_code_backend, "start_login", lambda: started.append(True))

    assert client.get("/api/claude-code/status").json() == status
    assert client.post("/api/claude-code/login").json() == {"started": True}
    assert started == [True]

    llm = client.put(
        "/api/llm",
        json={
            "provider": "claude_code",
            "quality_model": "claude-sonnet-5-5",
            "screening_model": "claude-haiku-4-5",
            "quality_effort": "high",
            "screening_effort": "low",
            "profile_model": "claude-opus-5-5",
            "profile_effort": "xhigh",
        },
    ).json()
    assert llm["provider"] == "claude_code"
    assert (llm["quality_effort"], llm["screening_effort"]) == ("high", "low")
    assert (llm["profile_model"], llm["profile_effort"]) == ("claude-opus-5-5", "xhigh")
    assert llm["profile_provider"] is None and llm["profile_ready"] is True
    assert llm["key_set"] is False
    assert llm["ready"] is True
    models = client.get("/api/llm/models", params={"provider": "claude_code"}).json()
    assert any(m["id"] == "claude-haiku-4-5" and m["input_price"] is None for m in models)


def test_ws_chat_streams_events(client: TestClient, ws: Workspace, monkeypatch: Any) -> None:
    script = {"assistant": [ChatMessage(role="assistant", content="Hello!")]}
    monkeypatch.setattr(ws, "chat", lambda role: ScriptedChat(script))
    chat.SESSIONS.clear()
    with client.websocket_connect("/api/ws/chat") as conn:
        assert conn.receive_json()["type"] == "session"
        conn.send_json({"type": "user_message", "text": "hi", "filters": {"titles": ["ML"]}})
        seen = []
        while not seen or seen[-1]["type"] not in {"done", "error"}:
            seen.append(conn.receive_json())
    assert any(e["type"] == "agent_message" and e["text"] == "Hello!" for e in seen)
    assert any(e["type"] == "model_usage" and e["agent"] == "assistant" for e in seen)
    assert seen[-1]["type"] == "done"
    [session] = chat.SESSIONS.values()
    assert "<ui_context>" in session.messages[0].content and len(session.messages) == 2


def test_search_stream_reports_each_stage_then_results(client: TestClient) -> None:
    import json

    body = {"query": {"sources": ["demo"]}, "smart": True}
    with client.stream("POST", "/api/search/stream", json=body) as response:
        events = [json.loads(line) for line in response.iter_lines() if line]

    progress = [e for e in events if e["type"] == "search_progress"]
    stages = {e["stage"] for e in progress}
    assert {"cv", "summary", "capture", "prefilter", "screen", "done"} <= stages
    demo = [e for e in progress if e.get("source") == "demo"]
    assert [e["status"] for e in demo] == ["running", "done"]
    assert any(e["stage"] == "screen" and e.get("done") == e.get("total") for e in progress)
    reviews = [e for e in progress if e.get("phase") == "review"]  # second opinions, own bar
    assert reviews[0]["done"] == 0 and reviews[-1]["done"] == reviews[-1]["total"] > 0
    assert events[-1]["type"] == "search_results"
    assert events[-1]["outcome"]["report"]["matches"][0]["job"]["id"] == "job-strong"


def test_search_stream_reports_errors_in_the_stream(client: TestClient, ws: Workspace) -> None:
    import json

    ws.active_cv_id = None
    with client.stream("POST", "/api/search/stream", json={"query": {}, "smart": True}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line]

    assert events[-1] == {"type": "error", "message": "Upload or select a CV first"}


def test_profiles_can_be_listed_edited_pinned_and_deleted(
    client: TestClient, ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post("/api/profile-summary", json={"query": {}}).status_code == 200
    listed = client.get("/api/profiles").json()
    assert listed["cv_parsed"] is True
    record = listed["profiles"][0]
    assert record["role_family"] == "any" and record["edited"] is False

    edited = {**record["summary"], "target_roles": ["Staff ML Engineer"], "not_a_fit": []}
    saved = client.put(f"/api/profiles/{record['key']}", json=edited).json()
    assert saved["edited"] is True and saved["summary"]["target_roles"] == ["Staff ML Engineer"]

    searched: list[list[str]] = []

    class Recorder:
        name = "reed"

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            searched.append(list(query.titles))
            return []

    def build_sources(names: list[str], *, settings: Settings, cv_key: str | None, **_: Any) -> Any:
        return [Recorder()], {}

    monkeypatch.setattr(search_service, "build_sources", build_sources)
    pinned = SearchRequest(query=SearchQuery(), smart=True, profile_key=record["key"])
    out = asyncio.run(run_search(ws, pinned))
    assert searched == [["Staff ML Engineer"]] and out.cv_titles == ["Staff ML Engineer"]

    with pytest.raises(ValueError, match="does not belong"):
        asyncio.run(run_search(ws, pinned.model_copy(update={"profile_key": "other:any"})))
    assert client.put("/api/profiles/other:any", json=edited).status_code == 404

    assert client.delete(f"/api/profiles/{record['key']}").json() == {"deleted": True}
    assert client.get("/api/profiles").json()["profiles"] == []


def test_profiles_survive_cv_edits_until_the_user_updates_them(
    client: TestClient, ws: Workspace, master_cv: MasterCV
) -> None:
    titled = {"query": {"titles": ["Senior ML Engineer"]}}
    key = client.post("/api/profile-summary", json=titled).json()["key"]
    assert key.startswith("master:")  # owned by the CV, not its content
    [record] = client.get("/api/profiles").json()["profiles"]
    assert record["cv_name"] == "Master CV" and record["created_at"]
    edited = {**record["summary"], "headline": "Edited by me"}
    client.put(f"/api/profiles/{record['key']}", json=edited)

    ws.master_cv = master_cv.model_copy(update={"skills": [*master_cv.skills[:-1]]})
    [kept] = client.get("/api/profiles").json()["profiles"]  # CV edited: kept, not rebuilt
    assert kept["summary"]["headline"] == "Edited by me" and kept["cv_changed"] is True
    again = client.post("/api/profile-summary", json=titled).json()
    assert again["from_memory"] is True and again["summary"]["headline"] == "Edited by me"

    updated = client.post(f"/api/profiles/refresh/{record['key']}").json()
    assert updated["key"] == record["key"] and updated["edited"] is False
    assert updated["created_at"] == record["created_at"] and updated["cv_name"] == "Master CV"
    [fresh] = client.get("/api/profiles").json()["profiles"]
    assert fresh["cv_changed"] is False and fresh["summary"]["headline"] != "Edited by me"

    ws.active_cv_id = "a" * 24  # a different CV gets its own, newly built profile
    _, cached = asyncio.run(search_service.get_summary(ws, master_cv, None, False))
    assert cached is False and ws.memory.record("a" * 24 + ":any") is not None
    assert [r.key for r in ws.memory.records("master")] == [key]


def test_new_profiles_read_the_original_document_including_publications(
    ws: Workspace, master_cv: MasterCV
) -> None:
    from src.services import cv_service

    original = (
        b"Jane Doe, Senior ML Engineer\n\nPublications\n"
        b"Doe J et al. Graph recommenders at scale. Nature Methods 2023.\n"
        b"Doe J, Roe K. Sparse attention for ranking. NeurIPS 2022.\n"
    )
    cv_service.store_cv(ws, "jane.txt", original)
    ws.master_cv = master_cv  # the parsed extract, which has no publications field
    asyncio.run(search_service.get_summary(ws, master_cv, None, False))
    prompt = ws.structured().calls[-1][0]
    assert "<source_document>" in prompt and "<cv>" in prompt
    assert "] Doe J et al. Graph recommenders at scale. Nature Methods 2023." in prompt
    assert "[src-1] Jane Doe, Senior ML Engineer" in prompt

    calls = len(ws.structured().calls)
    asyncio.run(search_service.get_summary(ws, master_cv, None, False))  # from memory
    assert len(ws.structured().calls) == calls


def test_profiles_record_the_uploaded_cv_file_name(ws: Workspace, master_cv: MasterCV) -> None:
    from src.services import cv_service

    asset = cv_service.store_cv(ws, "Jane CV 2026.txt", b"A sufficiently detailed CV")
    ws.master_cv = master_cv  # parsing is lazy; stand in for the parsed CV
    asyncio.run(search_service.get_summary(ws, master_cv, None, False))
    [profile] = search_service.list_profiles(ws, master_cv)
    assert profile.cv_id == asset.id and profile.cv_name == "Jane CV 2026.txt"


def test_legacy_profiles_keyed_by_cv_content_are_adopted(
    ws: Workspace, master_cv: MasterCV
) -> None:
    from src.jobs.profile_memory import cv_fingerprint

    fp = cv_fingerprint(master_cv)
    ws.memory.put(fp, "any", SUMMARY)  # stored before profiles were keyed by CV id
    [legacy] = search_service.list_profiles(ws, master_cv)
    assert legacy.key == "master:any" and legacy.cv_id == "master" and not legacy.cv_changed
    assert legacy.cv_name == "Master CV"


def test_search_history_keeps_last_ten_and_can_be_reopened_deleted_and_cleared(
    client: TestClient, ws: Workspace
) -> None:
    from src.services import history

    body = {"query": {"sources": ["demo"], "locations": ["Lisbon"], "country": "Portugal"}}
    assert client.post("/api/search", json=body).status_code == 200
    [item] = client.get("/api/history").json()
    assert item["label"].endswith("· Lisbon, Portugal") and item["fetched"] > 0
    assert item["models"] == (
        f"{ws.llm.provider} · screening {ws.llm.screening_model} ({ws.llm.screening_effort}) "
        f"· quality {ws.llm.quality_model} ({ws.llm.quality_effort})"
    )

    ws.last_report = None
    opened = client.get(f"/api/history/{item['id']}").json()
    assert opened["request"]["query"]["country"] == "Portugal"
    assert ws.last_report is not None  # its jobs can be tailored / discussed again

    for _ in range(history.HISTORY_SIZE):
        asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(sources=["demo"]))))
    listed = client.get("/api/history").json()
    assert len(listed) == history.HISTORY_SIZE and item["id"] not in {e["id"] for e in listed}

    assert client.delete(f"/api/history/{listed[0]['id']}").json() == {"deleted": True}
    assert client.delete(f"/api/history/{listed[0]['id']}").status_code == 404
    assert len(client.get("/api/history").json()) == history.HISTORY_SIZE - 1
    assert client.delete("/api/history").json() == {"cleared": True}
    assert client.get("/api/history").json() == []


def test_shortlist_keeps_one_copy_of_a_role_listed_in_several_locations() -> None:
    from src.jobs.models import MatchReport, MatchResult, ScoreBreakdown

    def result(job_id: str, title: str, where: str, total: float) -> MatchResult:
        job = JobPosting(id=job_id, title=title, company="Acme", location=where)
        breakdown = ScoreBreakdown(
            title=1, skills=1, experience=1, location=1, semantic=1, total=total
        )
        return MatchResult(job=job, score=breakdown)

    report = MatchReport.model_construct(  # only the ranked lists and target roles matter here
        profile=SimpleNamespace(target_titles=["Principal Scientist Cell Therapy"]),
        matches=[result("a", "Principal Scientist", "Leeds", 90)],
        below_threshold=[
            result("b", "principal  scientist", "York", 89),
            result("c", "Group Leader", "Leeds", 70),
            result("d", "Group Leader - Holiday Camp", "Leeds", 95),
        ],
    )
    # One copy per role; every title sharing a role word with the targets, then the best
    # `screen_extra` others by score. No fixed size.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert [j.id for j in search_service._shortlist(report, settings)] == ["a", "d", "c"]
    one_extra = settings.model_copy(update={"screen_extra": 1})
    assert [j.id for j in search_service._shortlist(report, one_extra)] == ["a", "d"]


def test_stalled_batch_is_abandoned_and_retried_once(master_cv: MasterCV) -> None:
    import threading

    from src.jobs.screener import screen_jobs

    calls: list[int] = []
    release = threading.Event()

    class StallsOnce:
        def generate(self, *, system: str, prompt: str, output_model: type[Any]) -> Any:
            calls.append(1)
            if len(calls) == 1:
                release.wait(5)  # the first attempt never answers in time
            return _screen(prompt)

    job = JobPosting(id="j1", title="Machine Learning Engineer", company="A")
    verdicts, errors = asyncio.run(
        screen_jobs(SUMMARY, [job], StallsOnce(), batch_timeout=0.2)  # type: ignore[arg-type]
    )
    release.set()
    # stalled attempt, retry, then the match's second opinion
    assert len(calls) == 3 and errors == [] and verdicts["j1"].match


def test_stop_keeps_partial_results_and_continue_judges_the_rest(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from src.services import history

    ws.settings = ws.settings.model_copy(update={"screen_batch_size": 1, "screen_concurrency": 1})
    first_done, release = threading.Event(), threading.Event()
    answered: list[str] = []

    class SlowAfterFirst:
        def generate(self, *, system: str, prompt: str, output_model: type[Any]) -> Any:
            if output_model is ProfileSummary:
                return SUMMARY
            if answered:
                release.wait(5)  # the second batch is still running when Stop arrives
            out = _screen(prompt)
            answered.append(out.verdicts[0].job_id)
            first_done.set()
            return out

    monkeypatch.setattr(ws, "structured", lambda *_: SlowAfterFirst())
    request = SearchRequest(query=SearchQuery(sources=["demo"]), smart=True, run_id="run-x")

    async def stop_after_first_verdict() -> Any:
        task = asyncio.create_task(run_search(ws, request))
        await asyncio.to_thread(first_done.wait, 5)
        assert search_service.stop_search("run-x")
        return await asyncio.wait_for(task, 5)

    stopped = asyncio.run(stop_after_first_verdict())
    release.set()
    judged = [r for r in stopped.report.all_results() if r.verdict]
    assert stopped.cancelled and len(judged) == 1 and stopped.unscreened >= 1
    assert not search_service.stop_search("run-x")  # finished runs are unregistered

    monkeypatch.setattr(ws, "structured", lambda *_: fake_llm())
    assert stopped.history_id is not None
    done = asyncio.run(search_service.continue_search(ws, stopped.history_id))
    assert not done.cancelled and done.unscreened == 0
    kept = next(r for r in done.report.all_results() if r.job.id == judged[0].job.id)
    assert kept.verdict == judged[0].verdict  # earlier verdicts are kept, not re-judged
    assert history.list_history(ws)[0].id == stopped.history_id  # updated in place
    with pytest.raises(ValueError, match="already been judged"):
        asyncio.run(search_service.continue_search(ws, stopped.history_id))


def test_older_history_lists_unread_matches_to_check(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.jobs import screener
    from src.services import history

    monkeypatch.setattr(ws, "structured", lambda *_: fake_llm())
    monkeypatch.setattr(screener, "checkable", lambda job: False)  # no posting is readable
    out = asyncio.run(
        run_search(ws, SearchRequest(query=SearchQuery(sources=["demo"]), smart=True))
    )
    unread = {r.job.id for r in out.report.to_check}
    assert unread and out.history_id is not None
    # Saved before `to_check` existed: unread postings sat among the matches.
    entries = history._entries(ws)
    report = entries[0]["outcome"]["report"]
    report["matches"], report["to_check"] = report["to_check"], []
    history._save(ws, entries)
    _, opened = history.open_entry(ws, out.history_id)
    assert opened.report.matches == []
    assert {r.job.id for r in opened.report.to_check} == unread


def test_postings_without_requirements_can_be_read_later_or_pasted(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.jobs import screener
    from src.services import history

    monkeypatch.setattr(ws, "structured", lambda *_: fake_llm())
    monkeypatch.setattr(screener, "checkable", lambda job: False)  # no posting is readable
    first = asyncio.run(
        run_search(ws, SearchRequest(query=SearchQuery(sources=["demo"]), smart=True))
    )
    judged = [r for r in first.report.all_results() if r.verdict]
    assert judged and not any(r.verdict.requirements_checked for r in judged)  # type: ignore[union-attr]
    # Unread postings that would match are listed to check, never among the matches.
    assert first.report.matches == [] and first.report.to_check
    assert all(r.verdict and r.verdict.match for r in first.report.to_check)
    assert first.history_id is not None
    target, other = judged[0], judged[1]

    monkeypatch.setattr(screener, "checkable", lambda job: len(job.description) >= 600)
    pasted = "Essential: lead ML engineering for recommender systems in production. " * 10
    with pytest.raises(ValueError, match="whole job description"):
        asyncio.run(search_service.recheck_postings(ws, first.history_id, [target.job.id], "short"))
    done = asyncio.run(
        search_service.recheck_postings(ws, first.history_id, [target.job.id], pasted)
    )
    after = {r.job.id: r for r in done.report.all_results()}
    assert target.job.id not in {r.job.id for r in done.report.to_check}  # read: judged in full
    assert after[target.job.id].job.description == pasted.strip()
    assert after[target.job.id].verdict.requirements_checked  # type: ignore[union-attr]
    assert after[other.job.id].verdict == other.verdict  # the rest keep their verdict
    _, saved = history.open_entry(ws, first.history_id)  # updated in place
    assert any(r.job.description == pasted.strip() for r in saved.report.all_results())

    monkeypatch.setattr(search_service, "_enrich", lambda *_: {})  # nothing readable now
    again = asyncio.run(search_service.recheck_postings(ws, first.history_id))
    assert again.report.all_results()  # unchanged, and says so in the progress log


# ---------------------------------------------------------------- job tracker and source yield


def test_applied_and_na_jobs_are_set_aside_before_screening(ws: Workspace) -> None:
    from src.services import tracker

    req = SearchRequest(query=SearchQuery(sources=["demo"]), smart=True, threshold=70)
    first = asyncio.run(run_search(ws, req)).report
    assert first.matches[0].tracking and first.matches[0].tracking.status == "new"
    strong = ws.job("job-strong")
    partial = ws.job("job-partial")
    assert strong is not None and partial is not None
    tracker.set_status(ws, strong, "applied", note="via ATS")
    tracker.set_status(ws, partial, "na")

    screened: list[str] = []
    llm = fake_llm()

    def recording(prompt: str) -> ScreeningBatch:
        screened.extend(re.findall(r'job_id="([^"]+)"', prompt))
        return _screen(prompt)

    llm.responses[ScreeningBatch] = recording
    rep = asyncio.run(run_search(ws, req)).report
    assert [r.job.id for r in rep.applied] == ["job-strong"]
    assert [r.job.id for r in rep.dismissed] == ["job-partial"]
    assert "job-strong" not in screened and "job-partial" not in screened
    applied = rep.applied[0]
    assert applied.tracking and applied.tracking.status == "applied"
    assert applied.tracking.note == "via ATS"
    assert not rep.matches  # an applied role never takes a ranked place


def test_tracker_matches_roles_not_employers_and_ages_out(ws: Workspace) -> None:
    from datetime import date

    from src.services import tracker

    applied = JobPosting(id="a", title="Senior Scientist", company="Acme Therapeutics Ltd")
    tracker.set_status(ws, applied, "applied", today=date(2026, 9, 1))
    same = JobPosting(id="b", title="senior  scientist", company="Acme")  # re-listing
    other = JobPosting(id="c", title="Principal Scientist", company="Acme")
    keep, aside, _ = tracker.set_aside(ws, [same, other], today=date(2026, 10, 2))
    assert [r.job.id for r in aside] == ["b"] and [j.id for j in keep] == ["c"]
    # A year later the application is history: the re-advertised role is ranked again.
    keep, aside, _ = tracker.set_aside(ws, [same], today=date(2027, 10, 2))
    assert not aside and [j.id for j in keep] == ["b"]


def test_new_then_open_and_notes_survive_searches(ws: Workspace) -> None:
    from src.services import tracker

    req = SearchRequest(query=SearchQuery(sources=["demo"]), smart=True, threshold=70)
    asyncio.run(run_search(ws, req))
    job = ws.job("job-strong")
    assert job is not None
    tracker.set_status(ws, job, "open", note="ask about relocation")
    rep = asyncio.run(run_search(ws, req)).report
    tracking = rep.matches[0].tracking
    assert tracking and tracking.status == "open" and tracking.note == "ask about relocation"
    other = next(r for r in rep.below_threshold if r.job.id == "job-partial")
    assert other.tracking and other.tracking.status == "open"


def test_tailoring_keeps_saved_job_open(ws: Workspace, monkeypatch: Any) -> None:
    from src.services import cv_service, saved, tracker

    asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(sources=["demo"]))))
    job = ws.job("job-strong")
    assert job is not None
    saved.save(ws, [job.id])
    tracker.set_status(ws, job, "open", note="Review before applying")
    from src.cv.models import ATSReport, JDAnalysis, TailoringPlan
    from src.cv.tailor import apply_plan

    seen: list[str] = []

    def fake_tailor(cv: MasterCV, jd: str, llm: Any, guidance: str, *_: Any) -> Any:
        seen.append(guidance)
        return apply_plan(cv, TailoringPlan(), JDAnalysis(job_title=job.title))

    report = ATSReport(words=400, est_pages=0.8, keyword_coverage=1.0)
    monkeypatch.setattr(cv_service, "tailor", fake_tailor)
    monkeypatch.setattr(cv_service, "analyze_jd", lambda text, llm: JDAnalysis(job_title="x"))
    monkeypatch.setattr(cv_service, "export_docx", lambda tailored, path, template: path)
    monkeypatch.setattr(
        cv_service,
        "check_docx",
        lambda path, cv, keywords: report.model_copy(
            update={"keyword_coverage": 0.5 if path.name == "source.docx" else 1.0}
        ),
    )
    tailored, _ = asyncio.run(cv_service.tailor_to_job(ws, job))
    assert tailored.ats == report
    assert tailored.source_ats_keyword_coverage == 0.5
    assert seen[0].startswith("fit 91 (exceptional): Direct match")  # the matcher's verdict
    entry = next(e for e in tracker.load(ws).values() if e.title == job.title)
    assert entry.status == "seen" and entry.applied_at is None
    assert entry.note == "Review before applying"
    assert entry.cv_file and entry.cv_file.endswith(".docx")
    assert [item.result.job.id for item in saved.list_saved(ws)] == [job.id]


def test_api_tracking_register_and_source_yield(client: TestClient) -> None:
    client.post("/api/search", json={"query": {"sources": ["demo"]}, "smart": True})
    marked = client.put("/api/jobs/job-strong/tracking", json={"status": "applied", "note": "x"})
    assert marked.status_code == 200 and marked.json()["status"] == "applied"
    assert client.put("/api/jobs/nope/tracking", json={"status": "na"}).status_code == 404
    added = client.post(
        "/api/tracker", json={"title": "Head of CMC", "company": "Oxford Bio", "note": "email"}
    ).json()
    register = client.get("/api/tracker").json()
    assert {e["title"] for e in register} == {"Senior Machine Learning Engineer", "Head of CMC"}
    edited = client.put(f"/api/tracker/{added['id']}", json={"status": "na"}).json()
    assert edited["status"] == "na" and edited["note"] == "email"
    assert client.delete(f"/api/tracker/{added['id']}").json() == {"deleted": True}
    yields = {y["source"]: y for y in client.get("/api/sources/yield").json()}
    demo = yields["example"]  # the demo file's postings name their own origin
    assert demo["searches"] == 1 and demo["found"] == 5 and demo["matches"] >= 1


def test_applied_job_moves_from_saved_to_application_register(client: TestClient) -> None:
    client.post("/api/search", json={"query": {"sources": ["demo"]}, "smart": True})
    assert client.post("/api/saved", json={"job_ids": ["job-strong"]}).status_code == 200
    assert [item["result"]["job"]["id"] for item in client.get("/api/saved").json()] == [
        "job-strong"
    ]

    marked = client.put("/api/jobs/job-strong/tracking", json={"status": "applied"})
    assert marked.status_code == 200
    assert client.get("/api/saved").json() == []
    entry = next(item for item in client.get("/api/tracker").json() if item["status"] == "applied")
    assert entry["application_result"]["job"]["id"] == "job-strong"
    assert entry["application_result"]["verdict"]["fit_score"] == 91

    client.put(f"/api/tracker/{entry['id']}", json={"status": "open"})
    assert [item["result"]["job"]["id"] for item in client.get("/api/saved").json()] == [
        "job-strong"
    ]


def test_note_edits_preserve_current_tracking_status(client: TestClient) -> None:
    client.post("/api/search", json={"query": {"sources": ["demo"]}, "smart": True})
    client.post("/api/saved", json={"job_ids": ["job-strong"]})
    client.put("/api/jobs/job-strong/tracking", json={"status": "applied"})
    entry = next(item for item in client.get("/api/tracker").json() if item["status"] == "applied")
    assert (
        client.put(f"/api/tracker/{entry['id']}", json={"note": "First note"}).json()["status"]
        == "applied"
    )

    client.put("/api/jobs/job-strong/tracking", json={"status": "open"})
    updated = client.put("/api/jobs/job-strong/tracking", json={"note": "Second note"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "open"
    assert updated.json()["note"] == "Second note"
    assert [item["result"]["job"]["id"] for item in client.get("/api/saved").json()] == [
        "job-strong"
    ]


def test_api_state_lists_the_source_catalog_and_company_boards(
    client: TestClient, ws: Workspace
) -> None:
    import json as jsonlib

    catalog = client.get("/api/state").json()["sources"]["catalog"]
    assert {c["category"] for c in catalog} == {"job_boards", "company", "alerts"}
    assert next(c for c in catalog if c["name"] == "company")["category"] == "company"
    ws.settings.companies_path.write_text(
        jsonlib.dumps(
            [
                {"name": "ViiV", "ats": "greenhouse", "token": "gsk"},
                {"name": "GSK", "ats": "greenhouse", "token": "gsk"},
                {"name": "Abcam", "ats": "lever", "token": "abcam"},
            ]
        )
    )
    boards = client.get("/api/companies/boards").json()
    assert [b["key"] for b in boards] == ["lever:abcam", "greenhouse:gsk"]
    assert boards[1]["companies"] == ["ViiV", "GSK"]  # one shared board, read once


def test_a_saved_search_keeps_its_progress_log(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import history

    monkeypatch.setattr(ws, "structured", lambda *_: fake_llm())
    out = asyncio.run(
        run_search(ws, SearchRequest(query=SearchQuery(sources=["demo"]), smart=True))
    )
    assert out.history_id is not None
    _, saved = history.open_entry(ws, out.history_id)
    assert any("Pre-filter" in line for line in saved.progress_log)
    assert saved.progress_log[0].strip().split("s ")[0].replace(".", "").isdigit()  # timed
