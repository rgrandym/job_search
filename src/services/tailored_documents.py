"""Persist guarded tailored CV drafts for review and later cover letters."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from src.cv.ats import check_docx
from src.cv.docx_exporter import TEMPLATES, clean_copy, export_docx
from src.cv.docx_original import restore_normalized_indents, write_like_original
from src.cv.models import (
    JDAnalysis,
    MasterCV,
    ReviewAddition,
    SearchFitBaseline,
    SkillGroup,
    TailoredCV,
    TailoredCVEdits,
    TailoredDocument,
    TailoringSettings,
)
from src.cv.proposals import MAX_ROLE_SKILLS, apply_compression, enriched
from src.cv.tailor import keyword_coverage, literal_terms, readability, revise_tailored, screen
from src.jobs.models import JobPosting
from src.services import declined, tracker
from src.services.workspace import Workspace
from src.tools.search_tools import extract_numbers

DOCUMENT_ID = re.compile(r"[a-f0-9]{24}")
SAME_LINE = 0.8  # a source CV line this similar to the draft's line is the same line


ORIGINAL = "original"  # template name: a copy of the CV's own Word file


def _directory(ws: Workspace) -> Path:
    return ws.settings.data_dir / "tailored_cvs"


def _save(ws: Workspace, document: TailoredDocument) -> None:
    directory = _directory(ws)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document.id}.json"
    temporary = directory / f".{document.id}.tmp"
    temporary.write_text(document.model_dump_json(indent=2))
    temporary.replace(path)


def create(
    ws: Workspace,
    job_id: str,
    source_cv: MasterCV,
    jd: JDAnalysis,
    tailored: TailoredCV,
    template: str,
    path: Path,
    *,
    posting: JobPosting | None = None,
    reviewed: bool = True,
    imported: bool = False,
    original_file: Path | None = None,
    settings: TailoringSettings | None = None,
    search_fit: SearchFitBaseline | None = None,
) -> TailoredDocument:
    """Keep the exact CV and source evidence used for an exported Word draft."""
    now = datetime.now(UTC).isoformat()
    document_id = secrets.token_hex(12)
    tailored = tailored.model_copy(update={"document_id": document_id})
    document = TailoredDocument(
        id=document_id,
        cv_id=ws.active_cv_id or "master",
        job_id=job_id,
        job_title=posting.title if posting else jd.job_title,
        job_company=posting.company if posting else jd.company or "",
        job_description=posting.description if posting else "",
        job_location=posting.location if posting else None,
        created_at=now,
        updated_at=now,
        template=template,
        filename=path.name,
        original_file=str(original_file) if original_file else None,
        source_cv=source_cv,
        jd=jd,
        tailored=tailored,
        reviewed=reviewed,
        imported=imported,
        settings=settings,
        search_fit=search_fit,
    )
    _save(ws, document)
    return document


def list_for_job(ws: Workspace, job_id: str) -> list[TailoredDocument]:
    """List saved drafts for one posting, newest first."""
    return [d for d in list_all(ws) if d.job_id == job_id]


def list_all(ws: Workspace) -> list[TailoredDocument]:
    """List every saved tailored draft, newest first."""
    directory = _directory(ws)
    if not directory.exists():
        return []
    documents = [
        TailoredDocument.model_validate_json(path.read_text()) for path in directory.glob("*.json")
    ]
    return sorted(documents, key=lambda d: d.updated_at, reverse=True)


def ensure_original_layout(ws: Workspace, path: Path) -> bool:
    """Repair a saved viewer rewrite when the original CV's indents can be matched safely."""
    if path.suffix.lower() != ".docx" or not path.is_file():
        return False
    document = next((d for d in list_all(ws) if d.filename == path.name), None)
    if document is None or document.template != ORIGINAL or not document.original_file:
        return False
    original = Path(document.original_file)
    if not original.is_file() or original.resolve() == path.resolve():
        return False
    return restore_normalized_indents(original, path)


def delete_for_file(ws: Workspace, filename: str) -> None:
    """Discard saved drafts whose current Word export was removed from the CV library; the
    job's tracker entry then names its newest remaining draft, if any."""
    for document in list_all(ws):
        if document.filename == filename:
            (_directory(ws) / f"{document.id}.json").unlink()
            current = ws.tailored.get(document.job_id)
            if current is not None and current.document_id == document.id:
                ws.tailored.pop(document.job_id, None)
    newest: dict[str, str] = {}
    for document in list_all(ws):  # newest first
        newest.setdefault(document.job_id, document.filename)
    tracker.forget_cv_file(ws, filename, newest)


def prune_missing(ws: Workspace) -> None:
    """Forget drafts, and tracker file names, whose Word file is gone (deleted outside the app,
    or before deletions cleaned up after themselves), so job cards stop showing them."""
    present = {
        p.name for folder in (ws.output_dir, ws.output_dir / "cvs") for p in folder.glob("*.docx")
    }
    for filename in {d.filename for d in list_all(ws)} - present:
        delete_for_file(ws, filename)
    named = {e.cv_file for e in tracker.load(ws).values() if e.cv_file is not None}
    for filename in named - present:
        tracker.forget_cv_file(ws, filename, {})


def load(ws: Workspace, document_id: str, job_id: str | None = None) -> TailoredDocument:
    """Load a draft and ensure it belongs to the requested posting, if given."""
    if not DOCUMENT_ID.fullmatch(document_id):
        raise ValueError("Invalid tailored CV identifier")
    path = _directory(ws) / f"{document_id}.json"
    if not path.is_file():
        raise ValueError("Tailored CV was not found")
    document = TailoredDocument.model_validate_json(path.read_text())
    if job_id is not None and document.job_id != job_id:
        raise ValueError("Tailored CV does not belong to this job")
    return document


def edit(ws: Workspace, document_id: str, edits: TailoredCVEdits) -> TailoredDocument:
    """Guard edits against the source CV, then rewrite the draft's Word file."""
    document = load(ws, document_id)
    pending = any(a.status == "pending" for a in document.tailored.review_additions)
    if edits.headline is None and edits.summary is None and not edits.bullets:
        if document.reviewed or pending:
            return document
        updated = document.model_copy(
            update={"reviewed": True, "updated_at": datetime.now(UTC).isoformat()}
        )
        _save(ws, updated)
        return updated
    revised = revise_tailored(document.source_cv, document.tailored, edits, document.jd)
    return _write_revision(ws, document, revised, [] if edits.headline is not None else None)


def _write_revision(
    ws: Workspace,
    document: TailoredDocument,
    revised: TailoredCV,
    options: list[str] | None = None,
    source_cv: MasterCV | None = None,
) -> TailoredDocument:
    """Rewrite the draft's own Word file after a guarded edit or a proposal decision, so a
    draft stays one file however often it is edited. A file changed outside the app since the
    app last wrote it (edited in Word) is never overwritten: the edit goes to a new file and a
    note says so."""
    current = Path(document.filename)
    if current.name != document.filename:
        raise ValueError("Invalid tailored CV filename")
    directory = ws.output_dir / "cvs"
    path = directory / current.name
    if _changed_outside(path, document):
        path = _free_path(directory, f"{current.stem}_edited")
        note = (
            f"{current.name} was changed outside the app, so it was kept as it is; this "
            f"edit was saved as {path.name}."
        )
        revised = revised.model_copy(update={"document_notes": [*revised.document_notes, note]})
    # Suggested headlines stay in the file until the user picks one.
    headlines = revised.headline_options if options is None else options
    source = source_cv or document.source_cv
    directory.mkdir(parents=True, exist_ok=True)
    written = path.with_name(f".{path.stem}.writing.docx")  # replaced whole, never half-written
    _write_file(document, source, revised, headlines, written)
    written.replace(path)
    keywords = revised.matched_keywords + revised.missing_keywords
    revised = revised.model_copy(update={"ats": check_docx(path, revised.cv, keywords)})
    updated = document.model_copy(
        update={
            "tailored": revised,
            "source_cv": source,
            "filename": path.name,
            "updated_at": datetime.now(UTC).isoformat(),
            "reviewed": not any(a.status == "pending" for a in revised.review_additions),
            "final_file": None,  # the draft changed: a final version is written again
            "final_notes": [],
        }
    )
    _save(ws, updated)
    return updated


def finalise(ws: Workspace, document_id: str) -> TailoredDocument:
    """Write the clean final version beside the draft (user's decision 2026-10-15): the
    draft's content with every change in black, its chosen headline only, and nothing still
    pending review (red proposals, compressions and skills to confirm are left out and
    listed). The draft itself is unchanged."""
    document = load(ws, document_id)
    draft = document.tailored
    pending = sum(a.status == "pending" for a in draft.review_additions)
    pending += sum(c.status == "pending" for c in draft.compressions)
    notes = [f"{pending} suggestion(s) still pending were left out"] if pending else []
    if draft.skills_to_confirm:
        notes.append("The skills to confirm were left out")
    if len(draft.headline_options) > 1:
        notes.append("Suggested headlines were left out: the CV keeps its headline")
    clean = clean_copy(draft)
    clean = clean.model_copy(
        update={"compressions": [c for c in draft.compressions if c.status != "pending"]}
    )
    directory = ws.output_dir / "cvs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{Path(document.filename).stem}_final.docx"
    original = Path(document.original_file) if document.original_file else None
    if document.template == ORIGINAL and original is not None and original.is_file():
        write_like_original(
            original, document.source_cv, clean.cv, path, (), clean.role_skills,
            clean.academic, (), clean.review_additions, clean.compressions, mark_changes=False,
            key_strengths=clean.key_strengths,
        )  # fmt: skip
    else:
        template = document.template if document.template in TEMPLATES else "classic"
        export_docx(clean, path, template, final=True)
    keywords = draft.matched_keywords + draft.missing_keywords
    report = check_docx(path, clean.cv, keywords)
    notes += [f"ATS read-back: {w}" for w in report.warnings]
    updated = document.model_copy(update={"final_file": path.name, "final_notes": notes})
    _save(ws, updated)
    return updated


def _write_file(
    document: TailoredDocument,
    source: MasterCV,
    revised: TailoredCV,
    headlines: list[str],
    written: Path,
) -> None:
    """The draft's Word file: a copy of the original's design, or its template."""
    original = Path(document.original_file) if document.original_file else None
    if document.template == ORIGINAL and original is not None and original.is_file():
        write_like_original(
            original,
            source,
            revised.cv,
            written,
            headlines,
            revised.role_skills,
            revised.academic,
            revised.skills_to_confirm,
            revised.review_additions,
            revised.compressions,
            key_strengths=revised.key_strengths,
        )
    else:
        export_docx(
            revised.model_copy(update={"headline_options": headlines}),
            written,
            document.template if document.template in TEMPLATES else "classic",
        )


OUTSIDE_EDIT_SLACK_S = 5.0  # the app writes the file just before it saves the draft record


def _changed_outside(path: Path, document: TailoredDocument) -> bool:
    """The Word file was saved after the app last wrote it (the user edited it in Word)."""
    if not path.is_file():
        return False
    written = datetime.fromisoformat(document.updated_at).timestamp()
    return path.stat().st_mtime > written + OUTSIDE_EDIT_SLACK_S


def _free_path(directory: Path, stem: str) -> Path:
    path = directory / f"{stem}.docx"
    number = 2
    while path.exists():
        path = directory / f"{stem}_{number}.docx"
        number += 1
    return path


def review_addition(
    ws: Workspace,
    document_id: str,
    addition_id: str,
    decision: Literal["confirm", "delete"],
    text: str | None = None,
    reason: str = "",
) -> TailoredDocument:
    """Accept a user-confirmed fact into the CV the draft came from, or remove the question
    (remembered for that CV, with the user's `reason`, so it is not proposed again); either
    way the draft's own Word file is rewritten."""
    document = load(ws, document_id)
    addition = next((a for a in document.tailored.review_additions if a.id == addition_id), None)
    if addition is None or addition.status != "pending":
        raise ValueError("Pending review addition was not found")
    wording = _wording(addition, text) if decision == "confirm" else addition.text
    source = document.source_cv.model_copy(deep=True)
    revised = document.tailored.model_copy(deep=True)
    if decision == "confirm":
        from src.services import cv_service

        # the fact goes into the CV this draft came from, whichever CV is selected now
        with cv_service.working_on(ws, document.cv_id) as current:
            _confirm(ws, addition, wording, current, source, revised)
        _refresh(revised, document)
    else:
        declined.record(ws, document.cv_id, addition, reason)
    revised.review_additions = [
        a.model_copy(
            update={"status": "confirmed" if decision == "confirm" else "deleted", "text": wording}
        )
        if a.id == addition_id
        else a
        for a in revised.review_additions
    ]
    return _write_revision(ws, document, revised, source_cv=source)


def _wording(addition: ReviewAddition, text: str | None) -> str:
    """The confirmed wording: the user's edit, else the proposal as worded (for added words,
    the bullet with them). A number question needs the line with its number."""
    if addition.kind == "enrichment" and text is None:
        wording = enriched(addition.original_text, addition.text)
    else:
        wording = (text if text is not None else addition.text).strip()
    if not wording or len(wording) > 400:
        raise ValueError("Enter a concise, accurate skill or experience before confirming")
    if addition.kind == "metric" and not (
        extract_numbers(wording) - extract_numbers(addition.original_text)
    ):
        raise ValueError("Write the line with its number before confirming")
    return wording


def _refresh(revised: TailoredCV, document: TailoredDocument) -> None:
    """The draft's evidence changed: its keyword coverage and screening score again, and no
    stale reading."""
    jd = document.jd
    revised.fit_check = None
    revised.screening = screen(as_tailored(document), revised, jd, document.job_description)
    revised.readability = readability(as_tailored(document), revised, jd)
    matched, missing = keyword_coverage(revised.cv, jd)
    revised.matched_keywords = matched
    revised.missing_keywords = missing
    total = len(matched) + len(missing)
    revised.keyword_coverage = len(matched) / total if total else 1.0


def _confirm(
    ws: Workspace,
    addition: ReviewAddition,
    wording: str,
    current: MasterCV,
    source: MasterCV,
    revised: TailoredCV,
) -> None:
    """Add the confirmed fact to the source CV (`current`, saved with its Word file), to the
    draft's copy of it and to the draft. A summary sentence or headline is this draft's
    presentation only: it changes the draft, not the source CV."""
    from src.services import cv_service

    if addition.kind == "summary":
        revised.cv.basics.summary = f"{revised.cv.basics.summary or ''} {wording}".strip()
        return
    if addition.kind == "headline":
        revised.cv.basics.headline = wording
        return
    if addition.kind == "rewording":  # the job's terms for this draft; the source keeps its own
        bullet = revised.cv.bullet_index().get(addition.anchor_id or "")
        if bullet is None:
            raise ValueError("The line is no longer in this draft")
        bullet.text = wording
        return
    if addition.kind == "experience":
        updated = _confirm_experience(ws, addition, wording, current, source, revised)
    elif addition.kind in {"enrichment", "metric"}:
        updated = _confirm_rewording(ws, addition, wording, current, source, revised)
    else:
        updated = _confirm_skill(ws, addition, wording, current, source, revised)
    cv_service.save_selected_cv(ws, updated)


def _confirm_experience(
    ws: Workspace,
    addition: ReviewAddition,
    wording: str,
    current: MasterCV,
    source: MasterCV,
    revised: TailoredCV,
) -> MasterCV:
    from src.cv.edits import CVEdit, apply_edits
    from src.services import cv_document

    role_id = addition.role_id or ""
    if not any(r.id == role_id for r in current.experience):
        raise ValueError("The source role is no longer in the CV this draft came from")
    after = addition.anchor_id if addition.anchor_id in current.bullet_index() else None
    edit = CVEdit(action="add_bullet", role_id=role_id, text=wording, after=after)
    updated = apply_edits(current, [edit])
    created = next(b for b in updated.bullet_index().values() if b.id not in current.bullet_index())
    for cv in (source, revised.cv):
        role = next((r for r in cv.experience if r.id == role_id), None)
        if role is None:
            raise ValueError("The proposed role is no longer in this draft")
        index = next((i for i, b in enumerate(role.bullets) if b.id == addition.anchor_id), None)
        position = len(role.bullets) if index is None else index + 1
        role.bullets.insert(position, created.model_copy(deep=True))
    synced = cv_document.sync_bullets(ws, current, updated)
    if synced.not_written:
        raise ValueError("Could not place the confirmed experience in the source Word CV")
    return updated


def _confirm_rewording(
    ws: Workspace,
    addition: ReviewAddition,
    wording: str,
    current: MasterCV,
    source: MasterCV,
    revised: TailoredCV,
) -> MasterCV:
    """Words or a number confirmed for an existing bullet: the bullet is reworded."""
    from src.cv.edits import CVEdit, apply_edits
    from src.services import cv_document

    target = _same_bullet(current, addition.anchor_id or "", addition.original_text)
    if target is None:
        raise ValueError("The line is no longer in the CV this draft came from")
    updated = apply_edits(current, [CVEdit(action="edit_bullet", bullet_id=target, text=wording)])
    for cv in (source, revised.cv):
        bullet = cv.bullet_index().get(addition.anchor_id or "")
        if bullet is not None:
            bullet.text = wording
    synced = cv_document.sync_bullets(ws, current, updated)
    if synced.not_written:
        raise ValueError("Could not reword the line in the source Word CV")
    return updated


def _same_bullet(cv: MasterCV, bullet_id: str, text: str) -> str | None:
    """The bullet in `cv` that a draft's bullet came from: the same id, else the same line."""
    if bullet_id in cv.bullet_index():
        return bullet_id
    scored = [
        (SequenceMatcher(None, b.text.casefold(), text.casefold()).ratio(), b.id)
        for b in cv.bullet_index().values()
    ]
    best = max(scored, default=(0.0, None))
    return best[1] if best[0] >= SAME_LINE else None


def _confirm_skill(
    ws: Workspace,
    addition: ReviewAddition,
    wording: str,
    current: MasterCV,
    source: MasterCV,
    revised: TailoredCV,
) -> MasterCV:
    from src.services import cv_document

    if any(wording.casefold() == s.casefold() for s in current.all_skills()):
        raise ValueError("This skill is already in the source CV")
    updated = current.model_copy(deep=True)
    for cv in (updated, source, revised.cv):
        category = addition.skill_group or "Additional"
        if not any(g.category == category for g in cv.skills):
            category = "Additional"
        group = next((g for g in cv.skills if g.category == category), None)
        if group is None:
            cv.skills.append(SkillGroup(category=category, items=[wording]))
        elif wording not in group.items:
            group.items.append(wording)
    cv_document.sync_confirmed_skill(ws, wording)
    revised.role_skills = list(dict.fromkeys([*revised.role_skills, wording]))[:MAX_ROLE_SKILLS]
    return updated


def review_compression(
    ws: Workspace,
    document_id: str,
    compression_id: str,
    decision: Literal["apply", "reject"],
) -> TailoredDocument:
    """The user's choice on a compressed version: applied, it replaces its lines in this
    draft (never in the source CV); rejected, the red version goes. Either way the draft's
    own Word file is rewritten. The app never applies one by itself."""
    document = load(ws, document_id)
    item = next((c for c in document.tailored.compressions if c.id == compression_id), None)
    if item is None or item.status != "pending":
        raise ValueError("Pending compressed version was not found")
    if decision == "apply" and item.kind == "merge_roles":  # from a draft made before 2026-10-15
        raise ValueError("Different roles are never combined: keep your lines")
    revised = document.tailored.model_copy(deep=True)
    if decision == "apply":
        revised.cv = apply_compression(revised.cv, item)
        _refresh(revised, document)
    status = "applied" if decision == "apply" else "rejected"
    revised.compressions = [
        c.model_copy(update={"status": status}) if c.id == compression_id else c
        for c in revised.compressions
    ]
    return _write_revision(ws, document, revised)


def with_screening(document: TailoredDocument) -> TailoredCV:
    """The draft with its screening and readability scores: as saved, or for a draft saved
    before a score existed, computed now (code only, no model; the same every time)."""
    tailored = document.tailored
    if document.imported or not document.job_description:
        return tailored
    jd = literal_terms(document.jd, document.job_description)
    update: dict[str, object] = {}
    if tailored.screening is None:
        update["screening"] = screen(as_tailored(document), tailored, jd, document.job_description)
    if tailored.readability is None:
        update["readability"] = readability(as_tailored(document), tailored, jd)
    return tailored.model_copy(update=update) if update else tailored


def as_tailored(document: TailoredDocument) -> MasterCV:
    """The source CV as tailoring read it: without the red suggestions confirmed since, which
    were added to it. This is the CV the search scored, so comparisons keep it as the original."""
    cv = document.source_cv.model_copy(deep=True)
    for item in document.tailored.review_additions:
        if item.status != "confirmed" or item.kind in {"summary", "headline", "rewording"}:
            continue
        if item.kind == "experience":
            for role in cv.experience:
                if role.id == item.role_id:
                    role.bullets = [b for b in role.bullets if b.text != item.text]
        elif item.kind in {"enrichment", "metric"}:
            bullet = cv.bullet_index().get(item.anchor_id or "")
            if bullet is not None and item.original_text:
                bullet.text = item.original_text
        else:
            for group in cv.skills:
                group.items = [s for s in group.items if s != item.text]
            cv.skills = [g for g in cv.skills if g.items]
    return cv
