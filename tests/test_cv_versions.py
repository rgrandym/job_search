"""Tailored CV versions for one job, read side by side against the original."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.cv.fit_check import read_versions
from src.cv.models import (
    FitReading,
    FitReadings,
    JDAnalysis,
    MasterCV,
    TailoredDocument,
    TailoringPlan,
    TailoringSettings,
)
from src.cv.tailor import apply_plan
from src.jobs.models import JobPosting
from src.services import cv_versions, tailored_documents
from src.services.workspace import Workspace
from tests.conftest import FakeLLM

JD = JDAnalysis(
    job_title="Senior ML Engineer",
    company="Orbit AI",
    hard_skills=["Python", "PyTorch", "Azure"],
    must_have=["MLOps", "Python"],
)
JOB = JobPosting(id="job-1", title="Senior ML Engineer", company="Orbit AI", description="MLOps")
WEAK = FitReading(function=2, domain=2, seniority=2, leadership=2, sector=2,
                  requirements_shown=[1], impression="generic")  # fmt: skip
STRONG = FitReading(function=4, domain=2, seniority=3, leadership=1, sector=2,
                    requirements_shown=[1, 2, 7], impression="clear fit")  # fmt: skip
CV_BLOCK = re.compile(r"<cv_(\d+)>\n(.*?)\n</cv_\1>", re.DOTALL)


def _reader(prompt: str) -> FitReadings:
    """The role-skills line marks the strong version; every other CV reads weak."""
    blocks = CV_BLOCK.findall(prompt)
    return FitReadings(
        readings=[STRONG if "Relevant skills for this role" in text else WEAK for _, text in blocks]
    )


def _version(
    ws: Workspace, master_cv: MasterCV, settings: TailoringSettings, **plan: object
) -> TailoredDocument:
    tailored = apply_plan(master_cv, TailoringPlan.model_validate(plan), JD)
    path = ws.output_dir / "cvs" / f"{len(tailored_documents.list_all(ws))}.docx"
    return tailored_documents.create(
        ws, JOB.id, master_cv, JD, tailored, "original", path, posting=JOB, settings=settings
    )


@pytest.fixture
def ws(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    workspace = Workspace(settings)
    llm = FakeLLM({FitReadings: _reader})
    monkeypatch.setattr(workspace, "structured", lambda *_args, **_kw: llm)
    return workspace


def test_versions_are_read_side_by_side_and_set_against_their_original(
    ws: Workspace, master_cv: MasterCV
) -> None:
    senior = _version(ws, master_cv, TailoringSettings(level="senior"), role_skills=["PyTorch"])
    auto = _version(ws, master_cv, TailoringSettings())
    comparison = asyncio.run(cv_versions.compare(ws, JOB.id, []))
    by_id = {r.id: r for r in comparison.readings}
    assert [r.kind for r in comparison.readings] == ["original", "tailored", "tailored"]
    assert by_id[senior.id].label == "V1 · Level: more senior"
    assert by_id[auto.id].label == "V2 · Automatic"
    assert comparison.best_id == senior.id
    assert comparison.summary.startswith("V1 · Level: more senior shows the fit most clearly")
    areas = {a.area: a for a in by_id[senior.id].areas}
    assert (areas["Function"].before, areas["Function"].after) == ("2/4", "4/4")
    assert areas["Function"].change == "better" and areas["Leadership"].change == "worse"
    assert areas["Requirements shown"].after == "2 of 2"  # 7 is not a listed requirement
    assert areas["Requirement: Python"].change == "better"
    assert all(a.change == "same" for a in by_id[auto.id].areas if a.area != "Fit score")


def test_saved_comparison_goes_stale_when_a_version_changes(
    ws: Workspace, master_cv: MasterCV
) -> None:
    assert cv_versions.saved(ws, JOB.id) == (None, False)
    version = _version(ws, master_cv, TailoringSettings())
    asyncio.run(cv_versions.compare(ws, JOB.id, [version.id]))
    saved, stale = cv_versions.saved(ws, JOB.id)
    assert saved is not None and not stale
    later = version.model_copy(update={"updated_at": "9999-01-01T00:00:00+00:00"})
    tailored_documents._save(ws, later)
    assert cv_versions.saved(ws, JOB.id)[1]


def test_too_many_versions_or_a_foreign_one_is_refused(ws: Workspace, master_cv: MasterCV) -> None:
    for _ in range(6):
        _version(ws, master_cv, TailoringSettings())
    with pytest.raises(ValueError, match="at most 6"):
        asyncio.run(cv_versions.compare(ws, JOB.id, []))
    with pytest.raises(ValueError, match="does not belong"):
        asyncio.run(cv_versions.compare(ws, JOB.id, ["0" * 24]))


def test_a_reader_that_skips_a_cv_is_an_error() -> None:
    llm = FakeLLM({FitReadings: FitReadings(readings=[WEAK])})
    with pytest.raises(ValueError, match="1 readings for 2 CVs"):
        read_versions(["one", "two"], ["Python"], "job", llm)


def test_api_lists_labels_and_compares(
    ws: Workspace, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    client = TestClient(webapp.app)
    version = _version(ws, master_cv, TailoringSettings(length="full"))
    listed = client.get(f"/api/jobs/{JOB.id}/tailored-cvs").json()
    assert listed[0]["label"] == "V1 · Full length" and listed[0]["settings"]["length"] == "full"
    assert client.get(f"/api/jobs/{JOB.id}/cv-comparison").json() == {
        "comparison": None,
        "stale": False,
    }
    body = client.post(f"/api/jobs/{JOB.id}/cv-comparison", json={"document_ids": [version.id]})
    assert body.status_code == 200 and body.json()["readings"][1]["id"] == version.id
    assert client.get(f"/api/jobs/{JOB.id}/cv-comparison").json()["comparison"] is not None
    refused = client.post("/api/jobs/other/cv-comparison", json={"document_ids": []})
    assert refused.status_code == 422
