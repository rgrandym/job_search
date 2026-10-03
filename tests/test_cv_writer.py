"""cv_writer: Master CV management, guarded tailoring, .docx export."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from pydantic import ValidationError

from src.cv import master_cv_manager as mgr
from src.cv.docx_exporter import TEMPLATES, export_docx, fmt_date
from src.cv.models import CVCritique, JDAnalysis, MasterCV, RewrittenBullet, TailoringPlan
from src.cv.tailor import apply_plan, keyword_coverage, tailor
from src.services import cv_service
from src.services.workspace import Workspace
from tests.conftest import FakeLLM

JD = JDAnalysis(
    job_title="Staff ML Engineer",
    company="Orbit AI",
    hard_skills=["Python", "PyTorch", "Kubernetes", "Spark"],
    must_have=["MLOps"],
)


# ---------------------------------------------------------------- master CV


def test_committed_schema_matches_model() -> None:
    committed = json.loads(mgr.SCHEMA_PATH.read_text())
    assert committed == mgr.json_schema(), "Run: python -m src.cv.master_cv_manager export-schema"


def test_example_cv_validates_against_json_schema(master_cv: MasterCV) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(master_cv.model_dump(mode="json"), mgr.json_schema())


def test_invalid_dates_and_duplicate_ids_rejected(master_cv: MasterCV) -> None:
    data = master_cv.model_dump(mode="json")
    data["experience"][0]["start"] = "2021/03"
    with pytest.raises(ValidationError):
        MasterCV.model_validate(data)
    data = master_cv.model_dump(mode="json")
    data["experience"][1]["bullets"][0]["id"] = "nimbus-1"
    with pytest.raises(ValidationError, match="Duplicate ids"):
        MasterCV.model_validate(data)


def test_save_load_roundtrip_keeps_backup(master_cv: MasterCV, tmp_path: Path) -> None:
    path = tmp_path / "cv.json"
    mgr.save(master_cv, path)
    mgr.save(master_cv, path)
    assert mgr.load(path) == master_cv
    assert path.with_suffix(".json.bak").exists()


def test_update_merge_patch(master_cv: MasterCV) -> None:
    updated = mgr.update(master_cv, {"basics": {"phone": "+351 900 000 000", "summary": None}})
    assert updated.basics.phone == "+351 900 000 000"
    assert updated.basics.summary is None
    assert updated.basics.name == master_cv.basics.name


def test_from_text_uses_llm(master_cv: MasterCV) -> None:
    llm = FakeLLM({MasterCV: master_cv})
    assert mgr.from_text("raw cv text", llm) == master_cv
    assert llm.calls[0][0] == "raw cv text"


def test_upload_stores_without_llm_then_imports_on_demand(
    master_cv: MasterCV, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(settings)
    llm = FakeLLM({MasterCV: master_cv})
    monkeypatch.setattr(ws, "structured", lambda role="worker", *_: llm)
    monkeypatch.setattr(ws, "llm_ready", lambda: True)
    raw = ("Alex Example\nSenior engineer\n" + "Built reliable systems.\n" * 8).encode()

    asset = cv_service.store_cv(ws, "resume.txt", raw)

    assert asset.filename == "resume.txt"
    stored = settings.data_dir / "cvs" / "resume.txt"
    assert stored.read_bytes() == raw
    assert ws.master_cv is None
    assert llm.calls == []

    imported = asyncio.run(cv_service.ensure_selected_cv(ws))
    assert imported == master_cv
    assert llm.calls[0][0].startswith("Alex Example")
    assert len(llm.calls) == 1
    edited = imported.model_copy(
        update={"basics": imported.basics.model_copy(update={"headline": "Edited headline"})}
    )
    cv_service.save_selected_cv(ws, edited)
    ws.master_cv = None
    assert asyncio.run(cv_service.ensure_selected_cv(ws)).basics.headline == "Edited headline"
    assert len(llm.calls) == 1
    with pytest.raises(ValueError, match="empty"):
        cv_service.store_cv(ws, "empty.txt", b"")
    with pytest.raises(ValueError, match="10 MB"):
        cv_service.store_cv(ws, "large.txt", b"x" * (cv_service.MAX_UPLOAD_BYTES + 1))


def test_upload_deduplicates_content_and_preserves_same_filename(settings: Any) -> None:
    ws = Workspace(settings)
    original = b"A sufficiently detailed CV"

    first = cv_service.store_cv(ws, "My CV.txt", original)
    duplicate = cv_service.store_cv(ws, "Copy of My CV.txt", original)

    assert duplicate.id == first.id
    assert duplicate.filename == "My CV.txt"
    assert [path.name for path in (settings.data_dir / "cvs").iterdir()] == ["My CV.txt"]

    replacement = cv_service.store_cv(ws, "My CV.txt", b"An updated and sufficiently detailed CV")

    assert replacement.id != first.id
    assert replacement.filename == "My CV (2).txt"
    assert [item.filename for item in cv_service.list_cvs(ws)] == ["My CV (2).txt", "My CV.txt"]


def test_upload_hidden_filename_remains_selectable(settings: Any) -> None:
    ws = Workspace(settings)

    asset = cv_service.store_cv(ws, ".resume.txt", b"A sufficiently detailed hidden CV")

    assert asset.filename == "resume.txt"
    assert cv_service.list_cvs(ws)[0].id == asset.id


def test_uploaded_file_replaces_legacy_master_library_entry(
    master_cv: MasterCV, settings: Any
) -> None:
    ws = Workspace(settings)
    ws.save_master_cv(master_cv)

    uploaded = cv_service.store_cv(ws, "My CV.txt", b"A sufficiently detailed CV")

    assert [(item.filename, item.selected) for item in cv_service.list_cvs(ws)] == [
        ("My CV.txt", True)
    ]
    assert ws.active_cv_id == uploaded.id


def test_legacy_hashed_uploads_migrate_to_real_filename(settings: Any) -> None:
    ws = Workspace(settings)
    directory = settings.data_dir / "cvs"
    directory.mkdir()
    data = b"A sufficiently detailed CV"
    real_id = "1" * 24
    duplicate_id = "2" * 24
    (directory / f"{real_id}.source.pdf").write_bytes(data)
    (directory / f"{real_id}.json").write_text(
        json.dumps(
            {
                "id": real_id,
                "filename": "My Real CV.pdf",
                "size": len(data),
                "kind": "uploaded",
            }
        )
    )
    (directory / f"{duplicate_id}.source.pdf").write_bytes(data)
    (directory / f"{duplicate_id}.json").write_text(
        json.dumps(
            {
                "id": duplicate_id,
                "filename": f"{real_id}.source.pdf",
                "size": len(data),
                "kind": "uploaded",
            }
        )
    )

    assets = cv_service.list_cvs(ws)

    assert [(item.filename, item.kind, item.selected) for item in assets] == [
        ("My Real CV.pdf", "uploaded", True)
    ]
    assert [path.name for path in directory.iterdir()] == ["My Real CV.pdf"]


def test_cv_library_lists_and_selects_generated_files(master_cv: MasterCV, settings: Any) -> None:
    ws = Workspace(settings)
    ws.save_master_cv(master_cv)
    ws.active_cv_id = "master"
    settings.output_dir.mkdir(parents=True)
    generated = settings.output_dir / "Tailored.docx"
    generated.write_bytes(b"word document")

    assets = cv_service.list_cvs(ws)

    assert [(item.id, item.selected) for item in assets] == [
        ("master", True),
        ("generated:Tailored.docx", False),
    ]
    selected = cv_service.select_cv(ws, "generated:Tailored.docx")
    assert selected.selected and selected.kind == "generated" and ws.master_cv is None


# ---------------------------------------------------------------- tailoring


def test_faithful_rewrite_accepted(master_cv: MasterCV) -> None:
    plan = TailoringPlan(
        rewritten_bullets=[
            RewrittenBullet(
                source_id="nimbus-2",
                text="Drove MLOps migration of training pipelines to Kubernetes, cutting cost 35%.",
            )
        ]
    )
    out = apply_plan(master_cv, plan, JD)
    assert out.changes[0].accepted
    assert out.cv.bullet_index()["nimbus-2"].text.startswith("Drove MLOps")
    assert master_cv.bullet_index()["nimbus-2"].text.startswith("Led migration")  # untouched


def test_invented_metric_rejected(master_cv: MasterCV) -> None:
    plan = TailoringPlan(
        rewritten_bullets=[
            RewrittenBullet(
                source_id="nimbus-2",
                text="Migrated pipelines to Kubernetes, cutting costs by 60%.",
            )
        ]
    )
    out = apply_plan(master_cv, plan, JD)
    assert not out.changes[0].accepted
    assert "60%" in (out.changes[0].reason or "")
    assert out.cv.bullet_index()["nimbus-2"].text == master_cv.bullet_index()["nimbus-2"].text


def test_unevidenced_jd_skill_rejected(master_cv: MasterCV) -> None:
    plan = TailoringPlan(
        rewritten_bullets=[
            RewrittenBullet(
                source_id="nimbus-2",
                text="Migrated Spark pipelines to Kubernetes, cutting costs by 35%.",
            )
        ]
    )
    out = apply_plan(master_cv, plan, JD)
    assert not out.changes[0].accepted
    assert "Spark" in (out.changes[0].reason or "")


def test_unknown_source_id_rejected(master_cv: MasterCV) -> None:
    plan = TailoringPlan(rewritten_bullets=[RewrittenBullet(source_id="ghost", text="x")])
    assert not apply_plan(master_cv, plan, JD).changes[0].accepted


def test_bullet_order_and_skill_priority(master_cv: MasterCV) -> None:
    plan = TailoringPlan(
        bullet_order={"nimbus": ["nimbus-2", "nimbus-1"]},
        skills_priority=["Kubernetes", "PyTorch"],
    )
    cv = apply_plan(master_cv, plan, JD).cv
    assert [b.id for b in cv.experience[0].bullets] == ["nimbus-2", "nimbus-1"]
    assert cv.skills[0].category == "Infrastructure"
    assert cv.skills[0].items[0] == "Kubernetes"
    assert master_cv.all_skills() == cv.all_skills() | {"Mentoring"}  # nimbus-3 dropped


def test_keyword_coverage(master_cv: MasterCV) -> None:
    matched, missing = keyword_coverage(master_cv, JD)
    assert set(matched) == {"Python", "PyTorch", "Kubernetes", "MLOps"}
    assert missing == ["Spark"]


def test_tailor_end_to_end(master_cv: MasterCV) -> None:
    plan = TailoringPlan(headline="Senior Machine Learning Engineer | MLOps")
    llm = FakeLLM({JDAnalysis: JD, TailoringPlan: plan, CVCritique: CVCritique()})
    out = tailor(master_cv, "We need a Staff ML Engineer...", llm)
    assert out.cv.basics.headline == "Senior Machine Learning Engineer | MLOps"
    assert out.target_company == "Orbit AI"
    assert 0 < out.keyword_coverage < 1
    assert [m for _, m in llm.calls].count(TailoringPlan) == 1  # no issues: no revision


# ---------------------------------------------------------------- docx export


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_export_docx(master_cv: MasterCV, tmp_path: Path, template: str) -> None:
    path = export_docx(master_cv, tmp_path / f"cv_{template}.docx", template)
    text = "\n".join(p.text for p in Document(str(path)).paragraphs)
    assert "Alex Example" in text
    assert "Nimbus Analytics" in text
    assert "Mar 2021 – Present" in text
    assert "4M users" in text


def test_export_unknown_template(master_cv: MasterCV, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown template"):
        export_docx(master_cv, tmp_path / "x.docx", "neon")


def test_fmt_date() -> None:
    assert fmt_date("2019-12") == "Dec 2019"
    assert fmt_date(None) == "Present"
    assert fmt_date("2018") == "2018"
