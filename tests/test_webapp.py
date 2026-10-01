"""Smart matching pipeline, summary memory, agent loop, and the web API (all offline)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.agents import chat
from src.agents.definitions import ORCHESTRATOR
from src.agents.runtime import AgentContext, run_agent
from src.core.config import ScoringWeights, Settings
from src.core.llm import ChatMessage, ChatResponse, ToolCall, ToolSpec
from src.cv.models import MasterCV
from src.jobs import scorer
from src.jobs.matcher import build_profile, profile_from_query
from src.jobs.models import JobPosting, JobVerdict, ProfileSummary, SearchQuery, SkillEvidence
from src.jobs.profile_memory import ProfileMemory, role_family, summarize_profile
from src.jobs.screener import ScreeningBatch
from src.services.search_service import SearchRequest, run_search
from src.services.workspace import Workspace
from src.tools.search_tools import extract_skills, parse_salary, title_core
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
        verdicts.append(
            JobVerdict(
                job_id=job_id,
                match=ok,
                fit_score=91 if ok else 35,
                verdict="strong" if ok else "poor",
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
    llm = fake_llm()
    monkeypatch.setattr(w, "structured", lambda role="worker": llm)
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
    assert {r.job.id for r in rep.excluded} == {"job-cert", "job-onsite", "job-junior"}
    out2 = asyncio.run(run_search(ws, req))
    assert out2.summary_from_memory is True


def test_search_without_llm_falls_back_to_prefilter(ws: Workspace, monkeypatch: Any) -> None:
    monkeypatch.setattr(ws, "llm_ready", lambda: False)
    req = SearchRequest(query=SearchQuery(sources=["demo"]), smart=True, threshold=70)
    out = asyncio.run(run_search(ws, req))
    assert not out.report.screened and out.smart_unavailable
    assert [r.job.id for r in out.report.matches] == ["job-strong"]


# ---------------------------------------------------------------- agent loop


class ScriptedChat:
    """Fake ChatModel: replays canned assistant turns per agent (by system prompt)."""

    def __init__(self, script: dict[str, list[ChatMessage]]) -> None:
        self.script = script

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        agent = "job_search_expert" if "job_search_expert" in system[:40] else "orchestrator"
        return ChatResponse(message=self.script[agent].pop(0), stop_reason="end_turn")


def _call(name: str, **args: Any) -> ChatMessage:
    return ChatMessage(
        role="assistant", tool_calls=[ToolCall(id=f"c-{name}", name=name, arguments=args)]
    )


def test_orchestrator_summarises_delegates_and_answers(ws: Workspace, monkeypatch: Any) -> None:
    script = {
        "orchestrator": [
            _call("summarize_profile"),
            _call("delegate", agent="job_search_expert", task="Find ML roles"),
            ChatMessage(role="assistant", content="Top match: Orbit AI (fit 91)."),
        ],
        "job_search_expert": [
            _call("search_jobs", sources=["demo"]),
            ChatMessage(role="assistant", content="1 true match: job-strong"),
        ],
    }
    monkeypatch.setattr(ws, "chat", lambda role: ScriptedChat(script))
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    messages = [ChatMessage(role="user", content="find me jobs")]
    answer = asyncio.run(run_agent(ORCHESTRATOR, messages, AgentContext(ws=ws, emit=emit)))
    assert answer == "Top match: Orbit AI (fit 91)."
    kinds = [k for k, _ in events]
    assert kinds.index("profile_summary") < kinds.index("delegate_start")
    assert "search_results" in kinds
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 2 and not any(m.is_error for m in tool_msgs)
    assert "1 true match" in tool_msgs[1].content


def test_tool_errors_are_returned_to_the_model(ws: Workspace, monkeypatch: Any) -> None:
    script = {
        "orchestrator": [
            _call("search_jobs"),  # not in the orchestrator's allowed tools
            ChatMessage(role="assistant", content="ok"),
        ]
    }
    monkeypatch.setattr(ws, "chat", lambda role: ScriptedChat(script))

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        return None

    messages = [ChatMessage(role="user", content="hi")]
    asyncio.run(run_agent(ORCHESTRATOR, messages, AgentContext(ws=ws, emit=emit)))
    assert messages[2].is_error and "not available" in messages[2].content


# ---------------------------------------------------------------- web API


@pytest.fixture
def client(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    return TestClient(webapp.app)


def test_api_state_and_search(client: TestClient) -> None:
    st = client.get("/api/state").json()
    assert st["cv"]["name"] == "Alex Example" and "demo" in st["sources"]["available"]
    assert {a["name"] for a in st["agents"]} >= {"orchestrator", "job_matcher", "cv_expert"}
    out = client.post("/api/search", json={"query": {"sources": ["demo"]}, "smart": True}).json()
    assert out["report"]["screened"] and out["report"]["matches"][0]["job"]["id"] == "job-strong"


def test_api_file_download_is_confined(client: TestClient) -> None:
    assert client.get("/api/files/..%2F.env").status_code == 404


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


def test_ws_chat_streams_events(client: TestClient, ws: Workspace, monkeypatch: Any) -> None:
    script = {"orchestrator": [ChatMessage(role="assistant", content="Hello!")]}
    monkeypatch.setattr(ws, "chat", lambda role: ScriptedChat(script))
    chat.SESSIONS.clear()
    with client.websocket_connect("/api/ws/chat") as conn:
        assert conn.receive_json()["type"] == "session"
        conn.send_json({"type": "user_message", "text": "hi", "filters": {"titles": ["ML"]}})
        seen = []
        while not seen or seen[-1]["type"] not in {"done", "error"}:
            seen.append(conn.receive_json())
    assert any(e["type"] == "agent_message" and e["text"] == "Hello!" for e in seen)
    assert seen[-1]["type"] == "done"
    [session] = chat.SESSIONS.values()
    assert "<ui_context>" in session.messages[0].content and len(session.messages) == 2
