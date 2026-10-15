"""The selected CV's health check and its ATS-safe copy (user's decision 2026-10-15)."""

from __future__ import annotations

import asyncio

import pytest

from src.core.config import Settings
from src.cv.models import Bullet, MasterCV
from src.services import cv_health, cv_service
from src.services.workspace import Workspace


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV) -> Workspace:
    settings.master_cv_path.write_text(master_cv.model_dump_json())
    workspace = Workspace(settings)
    cv_service.select_cv(workspace, "master")
    return workspace


def test_knowledge_gaps_name_what_the_facts_leave_out(master_cv: MasterCV) -> None:
    assert cv_health.knowledge_gaps(master_cv) == [
        "Used in your roles but not in your skills lists: Feature Engineering, MLOps, "
        "Mentoring, spaCy"
    ]
    cv = master_cv.model_copy(deep=True)
    cv.experience[1].bullets = [Bullet(id="lexa-1", text="Built NLP models.", skills=["spaCy"])]
    cv.experience[0].bullets = [Bullet(id="n1", text="Ran experiments."), *cv.experience[0].bullets]
    cv.skills = [g for g in cv.skills if g.category != "ML"]
    gaps = " | ".join(cv_health.knowledge_gaps(cv))
    assert "Lexa Labs: 1 line(s)" in gaps
    assert "not in your skills lists" in gaps and "spaCy" in gaps


def test_health_check_and_ats_safe_copy(ws: Workspace) -> None:
    health = asyncio.run(cv_health.check(ws))
    assert health.word_file is None and "No Word version" in health.design[0]
    assert 0 <= health.readability.score <= 100
    asset = asyncio.run(cv_health.clean_template(ws))
    assert asset.filename.endswith("_ATS.docx") and ws.active_cv_id == asset.id
    assert ws.master_cv is not None  # its facts are known: no new parse
    again = asyncio.run(cv_health.check(ws))
    assert again.word_file == asset.filename and again.design == []
