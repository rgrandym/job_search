"""The streamlined setup: two model roles (quality, screening) with their own effort, second
opinions on the quality model, one shared JD analysis per job, the assistant's CV-facts tool,
legacy config names, and the offline model comparison."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.agents import tools
from src.agents.runtime import AgentContext
from src.core.config import Settings
from src.core.llm import LLMConfig
from src.cv.models import (
    CoverLetterDraft,
    CVCritique,
    EvidenceProposal,
    EvidenceProposals,
    JDAnalysis,
    LetterParagraph,
    MasterCV,
    TailoringPlan,
)
from src.jobs.matcher import build_profile
from src.jobs.models import (
    FIT_WEIGHTS,
    FitRatings,
    JobAssessment,
    JobPosting,
    MatchReport,
    MatchResult,
    ProfileSummary,
    SearchQuery,
)
from src.jobs.screener import ScreeningBatch, finalize, screen_jobs
from src.services import cv_service, history, model_compare
from src.services.workspace import Workspace, legacy_llm_keys
from tests.conftest import FakeLLM

SUMMARY = ProfileSummary(
    headline="ML engineer",
    seniority="senior",
    years_experience=8,
    core_expertise=["ML"],
    key_skills=[],
    target_roles=["ML Engineer"],
    not_a_fit=[],
    summary="ML engineer.",
)


def _assessments(levels: tuple[int, ...]) -> Any:
    def answer(prompt: str) -> ScreeningBatch:
        ids = re.findall(r'job_id="([^"]+)"', prompt)
        ratings = FitRatings(**dict(zip(FIT_WEIGHTS, levels, strict=True)))
        return ScreeningBatch(
            verdicts=[JobAssessment(job_id=i, ratings=ratings, fit_summary="x") for i in ids]
        )

    return answer


def test_roles_have_their_own_model_and_effort() -> None:
    cfg = LLMConfig(
        provider="codex",
        quality_model="sol",
        screening_model="luna",
        quality_effort="high",
        screening_effort="low",
    )
    assert cfg.for_role("quality").effort == "high" and cfg.model_for("quality") == "sol"
    assert cfg.for_role("screening").effort == "low" and cfg.model_for("screening") == "luna"
    assert "effort" not in cfg.model_dump()  # derived per client, never stored


def test_configs_saved_before_the_rename_still_load(settings: Settings) -> None:
    legacy = {"provider": "codex", "orchestrator_model": "sol", "worker_model": "luna",
              "effort": "high"}  # fmt: skip
    assert legacy_llm_keys(legacy) == {
        "provider": "codex",
        "quality_model": "sol",
        "screening_model": "luna",
        "quality_effort": "high",
        "screening_effort": "high",
    }
    (settings.data_dir / "llm_config.json").write_text(json.dumps(legacy))
    llm = Workspace(settings).llm
    assert (llm.quality_model, llm.screening_model, llm.screening_effort) == ("sol", "luna", "high")


def test_second_opinions_go_to_the_quality_model() -> None:
    near = (3, 2, 2, 2, 2, 2)  # 58: within 5 of the threshold, so it is reviewed
    screening = FakeLLM({ScreeningBatch: _assessments(near)})
    quality = FakeLLM({ScreeningBatch: _assessments((4, 4, 3, 4, 3, 3))})
    jobs = [JobPosting(id="a", title="ML Engineer", company="Acme")]
    verdicts, errors = asyncio.run(
        screen_jobs(SUMMARY, jobs, screening, threshold=60, review_llm=quality)
    )
    assert errors == [] and len(screening.calls) == 1 and len(quality.calls) == 1
    assert verdicts["a"].reviewed and verdicts["a"].fit_score > 58  # averaged with Sol's view


def test_cv_and_cover_letter_share_one_jd_analysis(
    master_cv: MasterCV, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    draft = CoverLetterDraft(
        paragraphs=[LetterParagraph(text="I built ML.", source_ids=["nimbus"])]
    )
    llm = FakeLLM(
        {
            JDAnalysis: JDAnalysis(job_title="ML Engineer"),
            TailoringPlan: TailoringPlan(),
            CVCritique: CVCritique(),
            CoverLetterDraft: draft,
        }
    )
    monkeypatch.setattr(ws, "structured", lambda *a, **k: llm)
    job = JobPosting(id="j", title="ML Engineer", company="Orbit", description="Build ML.")
    asyncio.run(cv_service.tailor_to_job(ws, job))
    asyncio.run(cv_service.write_cover_letter(ws, job))
    assert [m for _, m in llm.calls].count(JDAnalysis) == 1


def test_assistant_turns_stated_facts_into_proposals(
    master_cv: MasterCV, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    said = "I also led a team of 6 analysts at Nimbus."
    proposal = EvidenceProposal(
        kind="bullet", text="Led a team of 6 analysts.", attach_to="nimbus",
        quote="led a team of 6 analysts at Nimbus", confidence="high",
    )  # fmt: skip
    llm = FakeLLM({EvidenceProposals: EvidenceProposals(items=[proposal])})
    monkeypatch.setattr(ws, "structured", lambda *a, **k: llm)
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    out = asyncio.run(
        tools.propose_cv_facts(tools.FactsArgs(text=said), AgentContext(ws=ws, emit=emit))
    )
    assert out["proposed"] == [{"kind": "bullet", "text": "Led a team of 6 analysts.",
                                "role": "nimbus"}]  # fmt: skip
    assert "evidence_proposed" in events
    assert ws.master_cv.bullet_index().keys() == master_cv.bullet_index().keys()  # not written


def test_model_comparison_reports_misses_and_new_accepts(
    master_cv: MasterCV, settings: Settings
) -> None:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"

    def judged(job_id: str, levels: tuple[int, ...]) -> MatchResult:
        ratings = FitRatings(**dict(zip(FIT_WEIGHTS, levels, strict=True)))
        verdict = finalize(JobAssessment(job_id=job_id, ratings=ratings, fit_summary="x"), 60)
        return MatchResult(
            job=JobPosting(id=job_id, title=f"Role {job_id}", company="Co"), verdict=verdict
        )

    report = MatchReport(
        profile=build_profile(master_cv),
        threshold=60,
        matches=[judged("good", (4, 4, 3, 4, 3, 3))],
        below_threshold=[judged("weak", (1, 1, 1, 1, 1, 1))],
        screened=True,
        summary=SUMMARY,
    )
    from src.services.search_service import SearchOutcome, SearchRequest

    history.record(ws, SearchRequest(query=SearchQuery()), SearchOutcome(report=report, fetched=2))
    picks = model_compare.sample(ws, 10)
    assert {p.result.job.id for p in picks} == {"good", "weak"}

    flipped = {"good": (1, 1, 1, 1, 1, 1), "weak": (4, 4, 3, 4, 3, 3)}

    def answer(prompt: str) -> ScreeningBatch:
        out = []
        for job_id in re.findall(r'job_id="([^"]+)"', prompt):
            ratings = FitRatings(**dict(zip(FIT_WEIGHTS, flipped[job_id], strict=True)))
            out.append(JobAssessment(job_id=job_id, ratings=ratings, fit_summary="x"))
        return ScreeningBatch(verdicts=out)

    fake = FakeLLM({ScreeningBatch: answer})
    result = asyncio.run(model_compare.compare(picks, lambda role: fake, "candidate"))
    assert result.compared == 2 and result.agreement == 0
    assert [m.job_id for m in result.missed] == ["good"]
    assert [m.job_id for m in result.new_accepts] == ["weak"]
    assert model_compare.sample(ws, 10, ["screening other-model"]) == []


def test_model_compare_setup_spec() -> None:
    assert model_compare.parse_setup("luna:low/sol:high") == {
        "screening_model": "luna",
        "screening_effort": "low",
        "quality_model": "sol",
        "quality_effort": "high",
    }
    assert model_compare.parse_setup("luna") == {"screening_model": "luna"}
    assert model_compare.parse_setup(":low") == {"screening_effort": "low"}


def test_committed_compare_plan_is_valid(tmp_path: Path, settings: Settings) -> None:
    """model_compare.toml (hand-edited, run by /compare_models) parses into valid setups."""
    from pydantic import ValidationError

    ws = Workspace(settings)
    base = {"provider": "codex", "quality_model": "q", "screening_model": "s"}
    plan = model_compare.load_plan(Path(__file__).parents[1] / "model_compare.toml")
    for spec in plan.setups:
        model_compare.setup_config(ws, base, spec)  # providers and efforts are valid
    typo = tmp_path / "plan.toml"
    typo.write_text('sampel = 10\nsetups = ["luna"]\n')
    with pytest.raises(ValidationError):
        model_compare.load_plan(typo)


def test_compare_setup_on_another_provider(settings: Settings) -> None:
    """A table setup runs on its own provider with its own models, not the current ones."""
    ws = Workspace(settings)
    base = {"provider": "codex", "quality_model": "sol", "screening_model": "luna"}
    spec = model_compare.SetupSpec(
        provider="claude_code", screening="claude-sonnet-5-5:low", quality="claude-opus-5-5"
    )
    cfg = model_compare.setup_config(ws, base, spec)
    assert (cfg.provider, cfg.screening_model, cfg.screening_effort) == (
        "claude_code",
        "claude-sonnet-5-5",
        "low",
    )
    assert cfg.quality_model == "claude-opus-5-5" and cfg.api_key is None
    same = model_compare.setup_config(ws, base, "luna:low")
    assert (same.provider, same.quality_model, same.screening_effort) == ("codex", "sol", "low")
