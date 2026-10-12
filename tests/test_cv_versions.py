"""Tailored CV versions for one job, read side by side against the original."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.cv.fit_check import (
    NOISE_MARGIN,
    best_version,
    consensus,
    read_versions,
    requirements_for,
    verdict,
    verify,
)
from src.cv.models import (
    EvidencedReading,
    EvidencedReadings,
    FitReading,
    JDAnalysis,
    MasterCV,
    MetRequirement,
    RatedLine,
    RequirementEvidence,
    TailoredDocument,
    TailoringPlan,
    TailoringSettings,
    VersionReading,
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
CV_BLOCK = re.compile(r"<cv_(\d+)>\n(.*?)\n</cv_\1>", re.DOTALL)
WEAK_RATINGS = {"function": 2, "domain": 2, "seniority": 2, "leadership": 2, "sector": 2}
STRONG_RATINGS = {"function": 4, "domain": 2, "seniority": 3, "leadership": 1, "sector": 2}


def _evidenced(
    text: str, ratings: dict[str, int], met: list[int], impression: str
) -> EvidencedReading:
    """A reading whose quotes are real lines of `text` (so code accepts them)."""
    line = next(ln for ln in text.splitlines() if len(ln.split()) >= 2)
    return EvidencedReading(
        **{name: RatedLine(rating=r, quote=line) for name, r in ratings.items()},
        requirements_met=[MetRequirement(number=n, quote=line) for n in met],
        impression=impression,
    )


def _reader(prompt: str) -> EvidencedReadings:
    """The role-skills line marks the strong version; every other CV reads weak."""
    out = []
    for _, text in CV_BLOCK.findall(prompt):
        strong = "Relevant skills for this role" in text
        ratings, met = (STRONG_RATINGS, [1, 2, 7]) if strong else (WEAK_RATINGS, [1])
        out.append(_evidenced(text, ratings, met, "clear fit" if strong else "generic"))
    return EvidencedReadings(readings=out)


def _version(
    ws: Workspace, master_cv: MasterCV, settings: TailoringSettings, **plan: object
) -> TailoredDocument:
    tailored = apply_plan(master_cv, TailoringPlan.model_validate(plan), JD)
    path = ws.output_dir / "cvs" / f"{len(tailored_documents.list_all(ws))}.docx"
    return tailored_documents.create(
        ws, JOB.id, master_cv, JD, tailored, "original", path, posting=JOB, settings=settings
    )


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    settings.master_cv_path.write_text(master_cv.model_dump_json())  # the library's Master CV
    workspace = Workspace(settings)
    llm = FakeLLM({EvidencedReadings: _reader})
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


def test_old_comparison_method_is_marked_stale(ws: Workspace, master_cv: MasterCV) -> None:
    version = _version(ws, master_cv, TailoringSettings())
    comparison = asyncio.run(cv_versions.compare(ws, JOB.id, [version.id]))
    old = comparison.model_copy(update={"method_version": 1})
    cv_versions._path(ws, JOB.id).write_text(old.model_dump_json())
    assert cv_versions.saved(ws, JOB.id)[1]


def test_too_many_versions_or_a_foreign_one_is_refused(ws: Workspace, master_cv: MasterCV) -> None:
    for _ in range(6):
        _version(ws, master_cv, TailoringSettings())
    with pytest.raises(ValueError, match="at most 6"):
        asyncio.run(cv_versions.compare(ws, JOB.id, []))
    with pytest.raises(ValueError, match="does not belong"):
        asyncio.run(cv_versions.compare(ws, JOB.id, ["0" * 24]))


def test_one_original_from_the_library_never_a_deleted_cv(
    ws: Workspace, master_cv: MasterCV
) -> None:
    from src.services import cv_service

    main = cv_service.store_cv(ws, "main.txt", b"Main CV\nML engineer")
    other = cv_service.store_cv(ws, "vertex.txt", b"Vertex CV\nPython engineer")
    ws.active_cv_id = other.id
    from_other = _version(ws, master_cv, TailoringSettings())  # tailored from the other CV
    cv_service.select_cv(ws, main.id)
    _version(ws, master_cv, TailoringSettings())
    comparison = asyncio.run(cv_versions.compare(ws, JOB.id, []))
    originals = [r for r in comparison.readings if r.kind == "original"]
    assert [r.id for r in originals] == [f"original:{main.id}"]  # the selected CV, only it
    assert all(r.baseline_id == originals[0].id for r in comparison.readings[1:])

    picked = asyncio.run(cv_versions.compare(ws, JOB.id, [from_other.id], baseline_id=other.id))
    assert picked.readings[0].label == "Original · vertex.txt"
    assert not cv_versions.saved(ws, JOB.id)[1]
    cv_service.delete_cv(ws, other.id)
    assert cv_versions.saved(ws, JOB.id)[1]  # its original is gone: compare again
    with pytest.raises(ValueError, match="Choose a CV from your library"):
        asyncio.run(cv_versions.compare(ws, JOB.id, [], baseline_id=other.id))


def test_comparing_again_gives_the_same_readings_without_rereading(
    ws: Workspace, master_cv: MasterCV
) -> None:
    llm = ws.structured("cv")
    assert isinstance(llm, FakeLLM)
    senior = _version(ws, master_cv, TailoringSettings(level="senior"), role_skills=["PyTorch"])
    first = asyncio.run(cv_versions.compare(ws, JOB.id, [senior.id]))
    assert len(llm.calls) == 3  # three independent reads
    again = asyncio.run(cv_versions.compare(ws, JOB.id, [senior.id]))
    assert len(llm.calls) == 3  # the same CVs are not read (and re-scored) again
    assert again.readings == first.readings and again.best_id == first.best_id

    auto = _version(ws, master_cv, TailoringSettings(), role_skills=["Python"])
    added = asyncio.run(cv_versions.compare(ws, JOB.id, [senior.id, auto.id]))
    assert len(llm.calls) == 6  # the new version needed reading; all were shown again
    by_id = {r.id: r for r in added.readings}
    assert by_id[added.readings[0].id].score == first.readings[0].score  # original kept
    assert by_id[senior.id].score == {r.id: r for r in first.readings}[senior.id].score


def test_a_reader_that_skips_a_cv_is_an_error() -> None:
    one = _evidenced("Python engineer", WEAK_RATINGS, [], "")
    llm = FakeLLM({EvidencedReadings: EvidencedReadings(readings=[one])})
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
        "baseline": "master",
    }
    body = client.post(f"/api/jobs/{JOB.id}/cv-comparison", json={"document_ids": [version.id]})
    assert body.status_code == 200 and body.json()["readings"][1]["id"] == version.id
    assert client.get(f"/api/jobs/{JOB.id}/cv-comparison").json()["comparison"] is not None
    refused = client.post("/api/jobs/other/cv-comparison", json={"document_ids": []})
    assert refused.status_code == 422


def test_a_rating_or_requirement_counts_only_with_a_quote_found_in_that_cv() -> None:
    cv = "Jane Doe\nLed a team of 6 ML engineers shipping PyTorch models\nPython, SQL"
    raw = EvidencedReading(
        function=RatedLine(rating=4, quote="led a team of 6 ML engineers... PyTorch models"),
        domain=RatedLine(rating=4, quote="Built the company's MLOps platform"),  # not in the CV
        seniority=RatedLine(rating=1, quote=""),
        leadership=RatedLine(rating=3, quote="Python"),  # one word is not evidence
        sector=RatedLine(rating=0, quote=""),
        requirements_met=[
            MetRequirement(number=1, quote="Shipping PyTorch models"),
            MetRequirement(number=2, quote="Ran MLOps on Azure"),  # invented
            MetRequirement(number=9, quote="Python, SQL"),  # not a listed requirement
        ],
    )
    reading = verify(raw, cv, total=2)
    assert (reading.function, reading.domain, reading.leadership) == (4, 1, 1)
    assert reading.requirements_shown == [1]
    assert set(reading.evidence) == {"function"}


def test_the_median_of_the_reads_counts() -> None:
    def run(function: int, shown: list[int]) -> FitReading:
        return FitReading(function=function, domain=2, seniority=2, leadership=2, sector=2,
                          requirements_shown=shown,
                          requirement_quotes={str(n): "a line" for n in shown})  # fmt: skip

    merged = consensus([run(4, [1, 2]), run(2, [1]), run(3, [1, 3])])
    assert merged.function == 3  # one high and one low read do not move it
    assert merged.requirements_shown == [1]  # found by most reads


def test_verdicts_and_setting_effects_use_fixed_margins(ws: Workspace, master_cv: MasterCV) -> None:
    auto = _version(ws, master_cv, TailoringSettings())
    senior = _version(ws, master_cv, TailoringSettings(level="senior"), role_skills=["PyTorch"])
    both = _version(
        ws, master_cv, TailoringSettings(level="junior", length="full"), role_skills=["Python"]
    )
    comparison = asyncio.run(cv_versions.compare(ws, JOB.id, []))
    by_id = {r.id: r for r in comparison.readings}
    assert by_id[auto.id].verdict == "same"  # reads as the original does
    assert by_id[auto.id].verdict_reason.startswith("No meaningful change")
    assert by_id[senior.id].verdict == "better"
    assert "1 more requirement shown" in by_id[senior.id].verdict_reason
    assert comparison.reads == 3 and comparison.margin == NOISE_MARGIN
    effects = comparison.setting_effects
    assert len(effects) == 1  # the junior+full version differs in two settings: not attributed
    level = effects[0]
    assert (level.setting, level.from_value, level.to_value) == (
        "Level",
        "Automatic",
        "Level: more senior",
    )
    assert level.effect == "clear" and level.score_change > 0
    assert "seniority 2/4 → 3/4" in level.text
    assert by_id[both.id].verdict == "better"


def test_equal_scores_are_decided_by_checked_requirements_and_say_so() -> None:
    def cv(label: str, score: int, shown: list[int], keywords: list[str]) -> VersionReading:
        return VersionReading(id=label, kind="tailored", label=label, baseline_id="original:m",
                              score=score, requirements_shown=shown, keywords_found=keywords,
                              keyword_coverage=len(keywords) / 11)  # fmt: skip

    original = cv("Original", 42, [1, 2, 6, 13], []).model_copy(update={"kind": "original"})
    v1 = cv("V1", 50, [1, 2, 6], ["Project management"])
    v2 = cv("V2", 50, [1, 2, 6, 13], [])
    best, summary = best_version([original, v2, v1], total=15)
    assert best == "V2"
    assert "score about the same (50 vs 50/100" in summary
    assert "more of the job's requirements (4 vs 3 of 15)" in summary
    assert "V1 has more job keywords (1 vs 0: Project management)" in summary
    # Keyword differences remain visible, but they do not decide overall fit alone.
    judged, reason = verdict(v1, original)
    assert judged == "mixed" and "job keyword added: Project management" in reason


def test_keyword_loss_alone_does_not_make_the_tailored_cv_worse() -> None:
    original = VersionReading(
        id="original:cv",
        kind="original",
        label="Original",
        score=86,
        requirements_shown=[1, 2],
        keywords_found=["Technology transfer"],
    )
    tailored = VersionReading(
        id="tailored",
        kind="tailored",
        label="Tailored",
        baseline_id=original.id,
        score=86,
        requirements_shown=[1, 2],
        keywords_found=[],
    )
    judged, reason = verdict(tailored, original)
    assert judged == "same" and "Technology transfer" in reason
    assert best_version([original, tailored], 2)[0] == tailored.id


def test_key_requirements_are_read_before_long_desirable_lists(master_cv: MasterCV) -> None:
    rows = [
        RequirementEvidence(
            requirement=f"Desirable {i}", priority="desirable", match="not_evidenced"
        )
        for i in range(15)
    ]
    rows.append(
        RequirementEvidence(
            requirement="Core process development",
            priority="desirable",
            importance="key",
            match="direct",
            evidence=["nimbus-2"],
        )
    )
    tailored = apply_plan(master_cv, TailoringPlan(requirements=rows), JD)
    assert requirements_for(tailored, JD)[0] == "Core process development"
