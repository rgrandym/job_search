"""Check the selected CV before tailoring from it (user's decision 2026-10-15).

Every tailored CV starts from the selected CV: its Word file gives the design, its facts the
content. Two checks, both by code (no model):
- design: what an ATS parser would miss or misread in the Word file (`cv/ats.py`), so the
  user fixes it once, or keeps an ATS-safe copy (`clean_template`) as the design to tailor
  from;
- knowledge: what the CV's facts leave out that tailoring cannot add: roles with one line,
  roles with no number or result, skills used in roles but missing from the skills lists, a
  missing summary. The CV is the record of the user's history and grows as they add what
  they recognise; nothing here changes it.
Plus the readability score of the CV as it is (`cv/readability.py`).
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from src.core.llm import UsageSink
from src.cv.ats import check_docx
from src.cv.docx_exporter import export_docx
from src.cv.models import MasterCV, ReadabilityScore
from src.cv.readability import LONG_BULLET_WORDS, score_readability
from src.cv.tailor import cv_to_text
from src.services import cv_service
from src.services.cv_service import CVAsset
from src.services.workspace import Workspace
from src.tools.search_tools import extract_numbers, mentions

MIN_ROLE_LINES = 2


class CVHealth(BaseModel):
    """What to fix in the selected CV before tailoring from it."""

    cv_name: str
    word_file: str | None = Field(None, description="The Word file used as the design")
    design: list[str] = Field(default_factory=list, description="What an ATS may miss")
    knowledge: list[str] = Field(default_factory=list, description="What the facts leave out")
    readability: ReadabilityScore


async def check(ws: Workspace, usage_sink: UsageSink | None = None) -> CVHealth:
    """Check the selected CV's Word design and its facts."""
    cv = await cv_service.ensure_selected_cv(ws, usage_sink, role="cv")
    word = cv_service.original_docx(ws)
    if word is None:
        design = [
            "No Word version of this CV: tailored CVs use the classic template. Add the Word "
            "file to the library, or make an ATS-safe copy."
        ]
    else:
        design = check_docx(word, cv, []).warnings
    name = cv_service.cv_name(ws, ws.active_cv_id or "") or cv.basics.name
    return CVHealth(
        cv_name=name,
        word_file=word.name if word else None,
        design=design,
        knowledge=knowledge_gaps(cv),
        readability=score_readability(cv, cv_to_text(cv, tags=False), []),
    )


def knowledge_gaps(cv: MasterCV) -> list[str]:
    """What the CV's facts leave out (code only)."""
    out: list[str] = []
    if not (cv.basics.summary or "").strip():
        out.append("No summary: tailoring writes one only from the facts below it")
    listed = " ".join(s for g in cv.skills for s in g.items)
    for role in cv.experience:
        name = f"{role.title}, {role.company}"
        if len(role.bullets) < MIN_ROLE_LINES:
            out.append(f"{name}: {len(role.bullets)} line(s); add what you did there")
        elif not any(extract_numbers(b.text) or b.metrics for b in role.bullets):
            out.append(f"{name}: no number or result; add one where you have it")
        long = sum(len(b.text.split()) > LONG_BULLET_WORDS for b in role.bullets)
        if long:
            out.append(f"{name}: {long} line(s) over {LONG_BULLET_WORDS} words")
    used = {s for role in cv.experience for b in role.bullets for s in b.skills}
    unlisted = sorted(s for s in used if not mentions(listed, s))
    if unlisted:
        out.append("Used in your roles but not in your skills lists: " + ", ".join(unlisted))
    if not cv.education:
        out.append("No education listed")
    return out


async def clean_template(ws: Workspace, usage_sink: UsageSink | None = None) -> CVAsset:
    """An ATS-safe Word copy of the selected CV (single column, standard headings, contact
    in the body, no tables or text boxes) added to the library and selected, so later
    tailored CVs use it as their design. Its facts are the selected CV's, so it needs no new
    parse."""
    cv = await cv_service.ensure_selected_cv(ws, usage_sink, role="cv")
    stem = re.sub(r"[^A-Za-z0-9]+", "_", cv.basics.name).strip("_") or "CV"
    with tempfile.TemporaryDirectory() as tmp:
        path = export_docx(cv, Path(tmp) / f"{stem}_ATS.docx", "classic")
        data = path.read_bytes()
    return cv_service.store_parsed_cv(ws, f"{stem}_ATS.docx", data, cv)
