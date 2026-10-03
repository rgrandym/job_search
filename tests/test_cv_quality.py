"""CV writing quality: tighter no-fabrication guards (per-bullet skills, headline, summary),
restoring dropped keywords, matcher guidance, the critic -> revise pass, the ATS read-back
and guarded cover letters."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from docx import Document

from src.cv.ats import check_docx
from src.cv.cover_letter import apply_letter, draft_letter
from src.cv.docx_exporter import export_cover_letter, export_docx
from src.cv.models import (
    BulletIssue,
    CoverLetterDraft,
    CVCritique,
    JDAnalysis,
    LetterParagraph,
    MasterCV,
    MissedRequirement,
    RewrittenBullet,
    TailoringPlan,
)
from src.cv.tailor import apply_plan, tailor
from src.jobs.models import JobPosting, SearchIntent
from src.services import cv_service, intent
from src.services.workspace import Workspace
from tests.conftest import FakeLLM

JD = JDAnalysis(
    job_title="Senior ML Engineer",
    company="Orbit AI",
    hard_skills=["Python", "PyTorch", "Kubernetes", "Spark"],
    must_have=["MLOps"],
)


def _rewrite(source_id: str, text: str) -> TailoringPlan:
    return TailoringPlan(rewritten_bullets=[RewrittenBullet(source_id=source_id, text=text)])


# ---------------------------------------------------------------- guards


def test_skill_from_another_bullet_is_rejected(master_cv: MasterCV) -> None:
    # Kubernetes is in the CV (nimbus-2), but nimbus-1 never used it.
    plan = _rewrite("nimbus-1", "Built a Kubernetes recommendation service serving 4M users.")
    change = apply_plan(master_cv, plan, JD).changes[0]
    assert not change.accepted and "Kubernetes" in (change.reason or "").title()


def test_headline_is_capped_at_the_level_and_roles_held(master_cv: MasterCV) -> None:
    def headline(text: str) -> Any:
        out = apply_plan(master_cv, TailoringPlan(headline=text), JD)
        return out.cv.basics.headline, out.changes[0]

    kept, change = headline("Staff Machine Learning Engineer")
    assert kept == master_cv.basics.headline and not change.accepted
    assert change.source_id == "headline" and "higher level (staff)" in (change.reason or "")
    kept, change = headline("Senior Data Scientist")
    assert kept == master_cv.basics.headline and "scientist" in (change.reason or "")
    kept, change = headline("Senior Machine Learning Engineer, NLP and recommenders")
    assert change.accepted and kept == "Senior Machine Learning Engineer, NLP and recommenders"


def test_summary_may_not_name_skills_the_cv_lacks(master_cv: MasterCV) -> None:
    plan = TailoringPlan(summary="ML engineer shipping PyTorch and Spark systems.")
    out = apply_plan(master_cv, plan, JD)
    assert out.cv.basics.summary == master_cv.basics.summary
    assert out.changes[0].source_id == "summary" and "spark" in (out.changes[0].reason or "")
    ok = apply_plan(master_cv, TailoringPlan(summary="ML engineer shipping PyTorch systems."), JD)
    assert ok.changes[0].accepted


def test_new_cv_claims_reject_stock_superlatives(master_cv: MasterCV) -> None:
    plan = TailoringPlan(
        headline="Visionary Senior Machine Learning Engineer",
        summary="World-class ML engineer with PyTorch experience.",
        rewritten_bullets=[
            RewrittenBullet(source_id="nimbus-1", text="Built a best-in-class recommender.")
        ],
    )
    out = apply_plan(master_cv, plan, JD)
    assert all(not change.accepted for change in out.changes)
    assert all("superlative" in (change.reason or "") for change in out.changes)
    assert out.cv.bullet_index()["nimbus-1"].text == master_cv.bullet_index()["nimbus-1"].text


def test_a_dropped_bullet_carrying_a_jd_keyword_is_put_back(master_cv: MasterCV) -> None:
    plan = TailoringPlan(bullet_order={"nimbus": ["nimbus-1", "nimbus-3"]})  # drops nimbus-2
    out = apply_plan(master_cv, plan, JD)
    assert [b.id for b in out.cv.experience[0].bullets] == ["nimbus-1", "nimbus-3", "nimbus-2"]
    assert out.restored_keywords == ["MLOps"]  # Kubernetes is still in the skills section
    assert out.missing_keywords == ["Spark"]  # the only true gap


def test_tailoring_keeps_two_bullets_per_role(master_cv: MasterCV) -> None:
    out = apply_plan(master_cv, TailoringPlan(bullet_order={"nimbus": ["nimbus-1"]}), JD)
    assert len(out.cv.experience[0].bullets) >= 2
    assert len({b.id for b in out.cv.experience[0].bullets}) == len(out.cv.experience[0].bullets)


# ---------------------------------------------------------------- guidance and review


def test_matcher_guidance_reaches_the_plan_and_review_drives_one_revision(
    master_cv: MasterCV,
) -> None:
    review = CVCritique(
        missed_requirements=[
            MissedRequirement(requirement="MLOps", source_id="nimbus-2"),
            MissedRequirement(requirement="Spark", source_id="ghost"),  # not a CV fact
        ],
        weak_bullets=[BulletIssue(source_id="nimbus-1", issue="result buried")],
    )
    plans = iter(
        [TailoringPlan(), TailoringPlan(bullet_order={"nimbus": ["nimbus-2", "nimbus-1"]})]
    )
    llm = FakeLLM({JDAnalysis: JD, TailoringPlan: lambda _: next(plans), CVCritique: review})
    out = tailor(master_cv, "JD text", llm, guidance="reasons: recsys at scale")
    prompts = [p for p, m in llm.calls if m is TailoringPlan]
    assert len(prompts) == 2 and "<match_assessment>\nreasons: recsys at scale" in prompts[0]
    assert "<review>" in prompts[1] and "ghost" not in prompts[1]
    assert out.critique == ["Surface MLOps (nimbus-2)", "Strengthen nimbus-1: result buried"]
    assert out.cv.experience[0].bullets[0].id == "nimbus-2"


# ---------------------------------------------------------------- ATS read-back


def test_ats_check_reads_contact_keywords_and_length(master_cv: MasterCV, tmp_path: Path) -> None:
    path = export_docx(master_cv, tmp_path / "cv.docx")
    report = check_docx(path, master_cv, ["PyTorch", "Spark"])
    assert report.contact_missing == [] and report.missing_keywords == ["Spark"]
    assert report.keyword_coverage == 0.5 and 0 < report.est_pages < 2 and report.warnings == []
    doc = Document(str(path))
    doc.add_table(rows=1, cols=2)
    doc.save(str(path))
    assert any("table" in w for w in check_docx(path, master_cv, []).warnings)


# ---------------------------------------------------------------- cover letters


def test_cover_letter_keeps_only_backed_paragraphs(master_cv: MasterCV, tmp_path: Path) -> None:
    draft = CoverLetterDraft(
        paragraphs=[
            LetterParagraph(
                text="At Nimbus I built a PyTorch recommender serving 4M users.",
                source_ids=["nimbus-1"],
            ),
            LetterParagraph(text="Your team of 40 engineers ships weekly.", source_ids=[]),
            LetterParagraph(text="I cut costs by 50%.", source_ids=["nimbus-2"]),
            LetterParagraph(text="I ran Spark jobs daily.", source_ids=["nimbus"]),
            LetterParagraph(text="Proven leader.", source_ids=["ghost"]),
        ]
    )
    jd_text = "Join our team of 40 engineers."
    letter = apply_letter(master_cv, draft, JD, jd_text)
    assert letter.paragraphs == [
        "At Nimbus I built a PyTorch recommender serving 4M users.",
        "Your team of 40 engineers ships weekly.",  # a fact about the employer, from the JD
    ]
    reasons = [c.reason or "" for c in letter.changes if not c.accepted]
    assert "50%" in reasons[0] and "spark" in reasons[1] and "ghost" in reasons[2]
    path = export_cover_letter(letter, master_cv, tmp_path / "letter.docx")
    text = "\n".join(p.text for p in Document(str(path)).paragraphs)
    assert "4M users" in text and "Alex Example" in text and "50%" not in text


def test_cover_letter_rejects_unsourced_personal_claims_and_job_numbers(
    master_cv: MasterCV,
) -> None:
    draft = CoverLetterDraft(
        paragraphs=[
            LetterParagraph(text="I managed multiple teams.", source_ids=[]),
            LetterParagraph(text="I led 40 engineers.", source_ids=["nimbus-1"]),
            LetterParagraph(text="I am a proven leader.", source_ids=["nimbus"]),
            LetterParagraph(text="Your team has 40 engineers.", source_ids=[]),
        ]
    )
    letter = apply_letter(master_cv, draft, JD, "Your team has 40 engineers.")
    assert letter.paragraphs == ["Your team has 40 engineers."]
    assert "source_id" in (letter.changes[0].reason or "")
    assert "40" in (letter.changes[1].reason or "")
    assert "superlative" in (letter.changes[2].reason or "")


def test_letter_motivation_comes_only_from_the_career_intent(
    master_cv: MasterCV, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    intent.save_intent(ws, SearchIntent(direction="Lead applied ML in health"))
    draft = CoverLetterDraft(
        paragraphs=[LetterParagraph(text="I built recommenders at Nimbus.", source_ids=["nimbus"])]
    )
    llm = FakeLLM({JDAnalysis: JD, CoverLetterDraft: draft})
    monkeypatch.setattr(ws, "structured", lambda *a, **k: llm)
    job = JobPosting(id="j", title="Senior ML Engineer", company="Orbit AI", description="ML")
    letter, path = asyncio.run(cv_service.write_cover_letter(ws, job))
    assert path.name == "Alex_Example_Orbit_AI_Senior_ML_Engineer_cover_letter.docx"
    assert letter.paragraphs
    prompt = next(p for p, m in llm.calls if m is CoverLetterDraft)
    assert "<motivation>\nLead applied ML in health\n</motivation>" in prompt
    assert draft_letter  # the drafting step is the only LLM call besides the JD analysis


def test_general_cv_exports_all_roles_and_keeps_prior_versions(
    master_cv: MasterCV, settings: Any
) -> None:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    first, roles = asyncio.run(cv_service.export_general_cv(ws))
    second, _ = asyncio.run(cv_service.export_general_cv(ws))
    assert roles == len(master_cv.experience)
    assert first != second and first.exists() and second.exists()
    body = "\n".join(p.text for p in Document(str(first)).paragraphs)
    assert all(role.title in body for role in master_cv.experience)
