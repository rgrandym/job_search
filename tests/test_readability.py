"""Readability score, key strengths and shorter earlier roles (user's decision 2026-10-15)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document

from src.cv.docx_original import write_like_original
from src.cv.models import Bullet, JDAnalysis, KeyStrength, MasterCV, TailoringPlan
from src.cv.proposals import apply_compression, condensed_roles
from src.cv.readability import score_readability
from src.cv.tailor import apply_plan, cv_to_text, printed_text

JD = JDAnalysis(job_title="Senior ML Engineer", hard_skills=["Python", "PyTorch", "Kubernetes"])


def test_readability_is_deterministic_and_names_what_to_fix(master_cv: MasterCV) -> None:
    text = cv_to_text(master_cv, tags=False)
    clean = score_readability(master_cv, text, ["Python"])
    assert clean == score_readability(master_cv, text, ["Python"])
    cv = master_cv.model_copy(deep=True)
    role = cv.experience[0]
    role.bullets[0].text = "Responsible for " + " ".join(["model"] * 40) + "."
    role.bullets[1].text = "Pipelines were migrated to Kubernetes, cutting costs by 35%."
    role.bullets[2].text = "Responsible for code review standards."
    worse = score_readability(cv, cv_to_text(cv, tags=False), ["Python"])
    assert worse.score < clean.score
    joined = " | ".join(worse.notes)
    assert "over 35 words" in joined and "open weakly" in joined and "passive" in joined
    assert worse.bullets == 5 and worse.grade in {"A", "B", "C", "D"}


def test_key_strengths_state_only_what_their_bullets_show(master_cv: MasterCV) -> None:
    plan = TailoringPlan(
        key_strengths=[
            KeyStrength(
                text="Recommendation service in Python for 4M users", source_ids=["nimbus-1"]
            ),
            KeyStrength(text="Cut training costs by 50% on Kubernetes", source_ids=["nimbus-2"]),
            KeyStrength(text="NLP models with 92% accuracy", source_ids=["ghost"]),
        ]
    )
    out = apply_plan(master_cv, plan, JD)
    assert out.key_strengths == ["Recommendation service in Python for 4M users"]
    reasons = [c.reason for c in out.changes if c.source_id == "key_strengths" and not c.accepted]
    assert any("numbers" in (r or "") for r in reasons) and "cites no CV bullet" in reasons
    printed = printed_text(out)
    assert "• Recommendation service in Python for 4M users" in printed
    assert out.readability is None  # scored with the draft (`tailor`), not by apply_plan


def test_earlier_roles_get_a_shorter_entry_the_user_may_apply(master_cv: MasterCV) -> None:
    cv = master_cv.model_copy(deep=True)
    old = cv.experience[1]
    old.start, old.end = "2009-01", "2013-06"
    old.bullets += [
        Bullet(id="lexa-3", text="Organised the team's weekly reading group."),
        Bullet(id="lexa-4", text="Wrote internal documentation for Kubernetes jobs."),
    ]
    found = condensed_roles(cv, ["Kubernetes", "NLP"], today=date(2026, 10, 15))
    assert len(found) == 1 and found[0].kind == "condense_role"
    item = found[0]
    assert item.source_ids == ["lexa-2", "lexa-3"]  # the lines it leaves out
    assert "Organised the team's weekly reading group." in item.removed
    applied = apply_compression(cv, item)
    assert [b.id for b in applied.experience[1].bullets] == ["lexa-1", "lexa-4"]
    assert condensed_roles(master_cv, ["NLP"], today=date(2026, 10, 15)) == []  # recent roles


def test_key_strengths_go_under_the_summary_in_red(master_cv: MasterCV, tmp_path: Path) -> None:
    from tests.test_cv_writer import _original_cv, _red_texts

    original = _original_cv(master_cv, tmp_path / "original.docx")
    out = tmp_path / "draft.docx"
    write_like_original(
        original, master_cv, master_cv, out, role_skills=["Python"],
        key_strengths=["Recommendation service in Python for 4M users"],
    )  # fmt: skip
    texts = [p.text for p in Document(str(out)).paragraphs]
    strength = texts.index("• Recommendation service in Python for 4M users")
    assert texts[strength - 1] == master_cv.basics.summary
    assert texts[strength + 1] == "Relevant skills for this role: Python"
    assert "• Recommendation service in Python for 4M users" in _red_texts(out)


def test_the_letter_score_is_deterministic_and_says_what_to_improve() -> None:
    from src.cv.cover_letter import letter_terms, score_letter
    from src.cv.models import CoverLetter, LetterFlag

    jd = JDAnalysis(
        job_title="Senior ML Engineer", company="Orbit", hard_skills=["PyTorch", "MLOps"]
    )
    terms = letter_terms(jd, "Senior ML Engineer. MLOps, MLOps and PyTorch.")
    assert terms == ["MLOps", "PyTorch"]  # most used first
    body = (
        "I am applying for the Senior ML Engineer role at Orbit. " + "I built PyTorch models. " * 40
    )
    letter = CoverLetter(
        target_title="Senior ML Engineer", target_company="Orbit", greeting="Dear team,",
        paragraphs=[body], closing="Yours,",
    )  # fmt: skip
    good = score_letter(letter, terms)
    assert good == score_letter(letter, terms) and good.score >= 80 and good.grade == "A"
    short = letter.model_copy(
        update={"paragraphs": ["I like ML."], "flags": [LetterFlag(paragraph=0, reason="x")]}
    )
    weak = score_letter(short, terms)
    assert weak.score < 50
    joined = " | ".join(weak.notes)
    assert "Name more of what the job asks for" in joined and "questioned" in joined
