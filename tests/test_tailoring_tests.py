"""The tailoring test set: cases saved from tailored CVs, run and measured by code."""

from __future__ import annotations

from typing import Any

import pytest

from src.core.config import Settings
from src.cv.models import (
    KeyStrength,
    MasterCV,
    RequirementEvidence,
    RoleVersion,
    RoleVersions,
    TailoringPlan,
)
from src.cv.tailor import apply_plan, tailor
from src.services import tailoring_tests
from src.services.workspace import Workspace
from tests.conftest import FakeLLM
from tests.test_optimiser import (
    JD,
    JOB_TEXT,
    LEXA_TWO,
    NIMBUS_SIX,
    NIMBUS_SOURCES,
    _draft,
    _long_roles,
    _version_lines,
)


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV) -> Workspace:
    cv = _long_roles(master_cv)
    settings.master_cv_path.write_text(cv.model_dump_json())
    return Workspace(settings)


def _plan() -> TailoringPlan:
    return TailoringPlan(
        summary="I am a machine learning engineer who builds recommendation services. "
        "I bring production experience in Python and PyTorch to product teams.",
        key_strengths=[
            KeyStrength(text="Training pipelines moved to Kubernetes", source_ids=["nimbus-2"]),
            KeyStrength(text="Mentored engineers on ML code review", source_ids=["nimbus-3"]),
            KeyStrength(text="NLP models for document classification", source_ids=["lexa-1"]),
        ],
        requirements=[
            RequirementEvidence(
                requirement="recommendation systems", priority="essential", importance="key",
                match="direct", evidence=["nimbus-1"],
            )
        ],
    )  # fmt: skip


def _llm() -> FakeLLM:
    nimbus = RoleVersion(
        role_id="nimbus", bullets=_version_lines(NIMBUS_SIX, NIMBUS_SOURCES), reason="r"
    )
    lexa = RoleVersion(
        role_id="lexa", bullets=_version_lines(LEXA_TWO),
        removed=["reading group", "documentation", "reviews"], reason="r",
    )  # fmt: skip
    return FakeLLM({TailoringPlan: _plan(), RoleVersions: RoleVersions(roles=[nimbus, lexa])})


def test_a_tailored_cv_is_saved_to_the_test_set_once(ws: Workspace, master_cv: MasterCV) -> None:
    cv = _long_roles(master_cv)
    tailored = apply_plan(cv, TailoringPlan(), JD, mode="optimised", jd_text=JOB_TEXT)
    document_id = _draft(ws, cv, tailored)
    case = tailoring_tests.add(ws, document_id)
    assert case.job_description and case.cv.experience[0].id == "nimbus"
    tailoring_tests.add(ws, document_id)  # the same CV and job replace the case
    assert [c.job_id for c in tailoring_tests.cases(ws)] == [case.job_id]
    tailoring_tests.remove(ws, tailoring_tests.cases(ws)[0].id)
    assert tailoring_tests.cases(ws) == []
    with pytest.raises(ValueError, match="not found"):
        tailoring_tests.remove(ws, "nope")


def test_code_measures_an_optimised_cv(ws: Workspace, master_cv: MasterCV) -> None:
    cv = _long_roles(master_cv)
    out = tailor(cv, JOB_TEXT, _llm(), review=False, jd=JD, mode="optimised")
    case = tailoring_tests.TestCase(
        id="c1", saved_at="now", name="n", job_id="j", cv=cv, job_description=JOB_TEXT, jd=JD
    )
    m = tailoring_tests.measure(case, out)
    assert (m.lines_before, m.lines_after) == (15, 8)  # nimbus 10 -> 6, lexa 5 -> 2
    assert m.shorter_roles == 2 and m.key_strengths == 3 and m.summary_sentences == 2
    assert m.keywords_lost == [] and m.shorter_by > 0
    assert "no gap fillers" in m.problems  # the fake plan proposes none
    assert any(p.startswith("2 relevant skills") or "relevant skills" in p for p in m.problems)


def test_a_run_tailors_each_case_again_and_reports_the_spread(
    ws: Workspace, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch
) -> None:
    cv = _long_roles(master_cv)
    tailored = apply_plan(cv, TailoringPlan(), JD, mode="optimised", jd_text=JOB_TEXT)
    tailoring_tests.add(ws, _draft(ws, cv, tailored))
    llm = _llm()

    def structured(*_args: Any, **_kwargs: Any) -> FakeLLM:
        return llm

    monkeypatch.setattr(ws, "structured", structured)
    report = tailoring_tests.run(ws, repeat=2)
    (case,) = report.cases
    assert len(case.runs) == 2 and case.errors == []
    assert case.spread["lines_after"] == 0  # built the same way both times
    assert report.averages["shorter_by"] > 0
    stored = list((ws.settings.data_dir / "tailoring_tests").glob("report_*.json"))
    assert len(stored) == 1
