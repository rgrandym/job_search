"""Compare the CV versions tailored for one job, side by side, against one original CV.

compare(ws, job_id, document_ids, baseline_id) -> VersionComparison: the chosen tailored
versions and one original from the CV library (the Master CV, else the selected CV, unless
the user picks another), read by one blind reader in one call (`fit_check.score_versions`),
then saved per job so the report opens again without a model call. A CV deleted from the
library is never read as the original. Each CV's reading is remembered (`readings.json`, keyed
by `fit_check.reading_key`), so comparing the same CVs again gives the same scores. An
optimised version with pending proposals is also read with them in place
(`VersionReading.if_confirmed`, hypothetical), in further batches when needed.
labels(documents): "V1 · Automatic", "V2 · Level: senior" ...
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from src.core import progress
from src.core.llm import UsageSink
from src.cv import master_cv_manager as mgr
from src.cv.fit_check import (
    MAX_VERSIONS,
    NOISE_MARGIN,
    READS,
    best_version,
    project,
    read_versions,
    reading_key,
    requirements_for,
    score_versions,
    with_keywords,
    with_proposals,
)
from src.cv.models import (
    DimensionChange,
    FitReading,
    SearchFitBaseline,
    SettingEffect,
    TailoredDocument,
    VersionComparison,
    VersionReading,
)
from src.cv.tailor import ats_keywords, cv_to_text, printed_text
from src.services import cv_service, tailored_documents
from src.services.workspace import Workspace

SETTING_LABELS = {
    "mode": {"standard": "Standard CV", "optimised": "Optimised draft"},
    "level": {"senior": "Level: more senior", "junior": "Level: more junior"},
    "length": {"full": "Full length", "junior": "Trimmed (junior)"},
    "emphasis": {"leadership": "Leadership", "hands_on": "Hands-on"},
    "audience": {"industry": "Industry", "academic": "Academic"},
    "template": {"classic": "Classic design", "modern": "Modern design", "compact": "Compact"},
}
METHOD_VERSION = 7


def _describe(document: TailoredDocument) -> str:
    if document.imported:
        return "Imported Word CV"
    if document.settings is None:
        return "Tailored"
    chosen = [
        names[value]
        for field, names in SETTING_LABELS.items()
        if (value := getattr(document.settings, field)) in names
    ]
    if document.settings.mode == "standard":
        chosen = [name for name in chosen if name != "Standard CV"]
    return " · ".join(chosen) or "Automatic"


def labels(documents: list[TailoredDocument]) -> dict[str, str]:
    """A short name per tailored CV, numbered per job in the order they were made."""
    out: dict[str, str] = {}
    counts: dict[str, int] = {}
    for document in sorted(documents, key=lambda d: d.created_at):
        counts[document.job_id] = counts.get(document.job_id, 0) + 1
        out[document.id] = f"V{counts[document.job_id]} · {_describe(document)}"
    return out


EFFECT_FIELDS = ("mode", "level", "length", "emphasis", "audience")


def _value(field: str, value: str) -> str:
    return SETTING_LABELS[field].get(value, "Automatic" if value == "auto" else value)


def _effect(field: str, first: VersionReading, second: VersionReading, total: int) -> SettingEffect:
    """What changing one setting did, from the first version to the second."""
    assert first.settings is not None and second.settings is not None
    score = second.score - first.score
    requirements = len(second.requirements_shown) - len(first.requirements_shown)
    keywords = second.keyword_coverage - first.keyword_coverage
    dims = [
        DimensionChange(name=a.name, before=a.after, after=b.after)
        for a, b in zip(first.dimensions, second.dimensions, strict=True)
    ]
    clear = (
        abs(score) >= NOISE_MARGIN
        or set(first.requirements_shown) != set(second.requirements_shown)
        or set(first.keywords_found) != set(second.keywords_found)  # exact, no margin
    )
    moved = [f"{d.name} {d.before}/4 → {d.after}/4" for d in dims if d.before != d.after]
    name = field.capitalize()
    old, new = (
        _value(field, getattr(first.settings, field)),
        _value(field, getattr(second.settings, field)),
    )
    text = (
        f"{name} {old} → {new} ({first.label} → {second.label}): fit score {first.score} → "
        f"{second.score} ({score:+d}), requirements shown {len(first.requirements_shown)} → "
        f"{len(second.requirements_shown)} of {total}, job keywords "
        f"{len(first.keywords_found)} → {len(second.keywords_found)}"
        + (f", {', '.join(moved)}" if moved else "")
        + (". A clear effect." if clear else f". No effect beyond ±{NOISE_MARGIN - 1} points.")
    )
    return SettingEffect(
        setting=name,
        from_id=first.id,
        to_id=second.id,
        from_value=old,
        to_value=new,
        score_change=score,
        requirements_change=requirements,
        keyword_change=keywords,
        dimensions=dims,
        effect="clear" if clear else "none",
        text=text,
    )


def setting_effects(readings: list[VersionReading], total: int) -> list[SettingEffect]:
    """For each pair of versions that differ in exactly one setting (level, length, emphasis,
    audience), what that setting changed; from the automatic choice when one pair member has
    it. Pairs differing in more than one setting cannot say which one did it, so are skipped."""
    tailored = [r for r in readings if r.kind == "tailored" and r.settings is not None]
    out: list[SettingEffect] = []
    for a, b in combinations(tailored, 2):
        assert a.settings is not None and b.settings is not None
        fields = [f for f in EFFECT_FIELDS if getattr(a.settings, f) != getattr(b.settings, f)]
        if len(fields) != 1:
            continue
        field = fields[0]
        first, second = (b, a) if getattr(b.settings, field) == "auto" else (a, b)
        out.append(_effect(field, first, second, total))
    return out


def _path(ws: Workspace, job_id: str) -> Path:
    key = hashlib.sha256(job_id.encode()).hexdigest()[:24]
    return ws.settings.data_dir / "cv_comparisons" / f"{key}.json"


def saved(ws: Workspace, job_id: str) -> tuple[VersionComparison | None, bool]:
    """The last comparison for this job, and whether a version it read has changed since."""
    path = _path(ws, job_id)
    if not path.is_file():
        return None, False
    comparison = VersionComparison.model_validate_json(path.read_text())
    current = {d.id: d for d in tailored_documents.list_for_job(ws, job_id)}
    library = {f"original:{asset.id}" for asset in cv_service.list_cvs(ws)}
    stale = any(
        (r.kind == "tailored" and r.id not in current)
        or (r.kind == "tailored" and current[r.id].updated_at > comparison.created_at)
        or (r.kind == "original" and r.id not in library)
        for r in comparison.readings
    )
    stale = stale or comparison.method_version < METHOD_VERSION
    return comparison, stale


def _readings_path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "cv_comparisons" / "readings.json"


def _reader(ws: Workspace) -> str:
    """The model that reads: another model may read differently, so it reads afresh."""
    llm = ws.llm
    return f"{llm.provider_for('cv')}:{llm.model_for('cv')}:{llm.effort_for('cv')}"


def _known(ws: Workspace, keys: list[str]) -> list[FitReading | None]:
    path = _readings_path(ws)
    stored = json.loads(path.read_text()) if path.is_file() else {}
    return [FitReading.model_validate(stored[k]) if k in stored else None for k in keys]


def _remember(ws: Workspace, keys: list[str], readings: list[FitReading]) -> None:
    path = _readings_path(ws)
    stored = json.loads(path.read_text()) if path.is_file() else {}
    for key, reading in zip(keys, readings, strict=True):
        stored[key] = reading.model_dump()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, indent=1))


def _chosen(ws: Workspace, job_id: str, document_ids: list[str]) -> list[TailoredDocument]:
    documents = tailored_documents.list_for_job(ws, job_id)
    if not documents:
        raise ValueError("Tailor a CV for this job first")
    if not document_ids:
        return documents
    known = {d.id: d for d in documents}
    missing = [i for i in document_ids if i not in known]
    if missing:
        raise ValueError("A chosen CV does not belong to this job")
    return [known[i] for i in dict.fromkeys(document_ids)]


def default_baseline(ws: Workspace) -> str | None:
    """The original read by default: the Master CV, else the selected CV, else the only one."""
    ids = [asset.id for asset in cv_service.list_cvs(ws)]
    for choice in ("master", ws.active_cv_id):
        if choice in ids:
            return choice
    return ids[0] if len(ids) == 1 else None


def _baseline(
    ws: Workspace, baseline_id: str | None, documents: list[TailoredDocument]
) -> tuple[VersionReading, str]:
    """The one original, from the CV library as it is now: a deleted CV is refused."""
    cv_id = baseline_id or default_baseline(ws)
    asset = next((a for a in cv_service.list_cvs(ws) if a.id == cv_id), None)
    if cv_id is None or asset is None:
        raise ValueError("Choose a CV from your library to compare against")
    snapshot = _oldest([d for d in documents if not d.imported and d.cv_id == cv_id])
    if snapshot is not None:  # the CV as the first of these tailorings read it
        text = cv_to_text(tailored_documents.as_tailored(snapshot), tags=False)
    elif cv_id == "master":
        text = cv_to_text(mgr.load(ws.settings.master_cv_path), tags=False)
    else:
        path = cv_service.source_file(ws, cv_id)
        text = cv_service.extract_text(path.name, path.read_bytes())
    name = "Master CV" if cv_id == "master" else asset.filename
    reading = VersionReading(
        id=f"original:{cv_id}", kind="original", label=f"Original · {name}", score=0
    )
    return reading, text


def _entries(
    ws: Workspace, documents: list[TailoredDocument], names: dict[str, str], baseline_id: str | None
) -> tuple[list[VersionReading], list[str]]:
    """The one original then the tailored versions, with the text a reader sees for each."""
    original, text = _baseline(ws, baseline_id, documents)
    items: list[VersionReading] = [original]
    texts: list[str] = [text]
    for document in documents:
        tailored = document.tailored
        items.append(
            VersionReading(
                id=document.id,
                kind="tailored",
                label=names.get(document.id, "Tailored"),
                baseline_id=original.id,
                created_at=document.created_at,
                filename=document.filename,
                settings=document.settings,
                score=0,
                rejected_changes=sum(not c.accepted for c in tailored.changes),
                left_out=sum(c.accepted and not c.tailored for c in tailored.changes),
                rewritten=sum(
                    c.accepted and bool(c.tailored) and c.tailored != c.original
                    for c in tailored.changes
                ),
            )
        )
        texts.append(printed_text(tailored))
    return items, texts


def _projections(
    documents: list[TailoredDocument],
) -> tuple[list[tuple[int, TailoredDocument]], list[str]]:
    """(item index, document) for each version read again with its pending proposals in
    place, and their texts."""
    chosen: list[tuple[int, TailoredDocument]] = []
    texts: list[str] = []
    for index, document in enumerate(documents, start=1):
        proposed = with_proposals(document.tailored)
        if proposed is not None:
            chosen.append((index, document))
            texts.append(printed_text(proposed))
    return chosen, texts


def _oldest(documents: list[TailoredDocument]) -> TailoredDocument | None:
    return min(documents, key=lambda d: d.created_at, default=None)


def _search_fit(
    ws: Workspace, job_id: str, documents: list[TailoredDocument], cv_id: str
) -> tuple[SearchFitBaseline | None, str]:
    """The search verdict every version's score is anchored to, or why there is none. The
    oldest draft's saved verdict goes with the original, which is that draft's source CV."""
    if any(d.cv_id != cv_id for d in documents):
        return None, "The versions come from different CVs, so no one search score applies."
    saved = _oldest([d for d in documents if d.search_fit is not None])
    if saved is not None:
        return saved.search_fit, ""
    result = ws.result(job_id)
    found = cv_service.search_fit_baseline(result, cv_id)
    if found is not None:
        return found, ""
    if result is not None and result.verdict is not None and result.verdict.ratings is not None:
        return None, "The saved search verdict judged another CV; search with this CV to use it."
    return None, "No saved search verdict was available for this job."


def _job_text(document: TailoredDocument) -> str:
    """The job as tailoring read it (`cv_service._job_text`)."""
    title = document.job_title or document.jd.job_title
    company = document.job_company or document.jd.company or ""
    return f"{title} at {company}\n{document.job_location or ''}\n\n{document.job_description}"


async def compare(
    ws: Workspace,
    job_id: str,
    document_ids: list[str],
    usage_sink: UsageSink | None = None,
    baseline_id: str | None = None,
) -> VersionComparison:
    """Read the chosen tailored CVs for this job and one original side by side, and save the
    comparison."""
    progress.step("Gathering the CV versions", total=3)
    documents = _chosen(ws, job_id, document_ids)
    names = labels(tailored_documents.list_for_job(ws, job_id))
    if len(documents) + 1 > MAX_VERSIONS:
        raise ValueError(
            f"That is {len(documents) + 1} CVs with the original; "
            f"choose at most {MAX_VERSIONS} in all"
        )
    items, texts = _entries(ws, documents, names, baseline_id)
    search_fit, search_note = _search_fit(
        ws, job_id, documents, items[0].id.removeprefix("original:")
    )
    lead = next((d for d in documents if not d.imported), documents[0])
    requirements = requirements_for(lead.tailored, lead.jd)
    keywords = ats_keywords(lead.jd)
    progress.step(f"Reading {len(items)} CVs side by side for this job")
    llm = ws.structured("cv", usage_sink, "CV version comparison")
    jd_text = _job_text(lead)
    projected, extra = _projections(documents)
    first_extra = extra[: MAX_VERSIONS - len(texts)]
    every = texts + first_extra
    keys = [reading_key(t, requirements, jd_text, _reader(ws)) for t in every]
    known = _known(ws, keys)  # CVs read before for this job: same scores, no re-roll
    fit = await asyncio.to_thread(read_versions, every, requirements, jd_text, llm, known)
    _remember(ws, keys, fit)
    readings = score_versions(items, texts, fit[: len(texts)], requirements, keywords, search_fit)
    projected_readings = fit[len(texts) :]
    for start in range(len(first_extra), len(extra), MAX_VERSIONS - 1):
        batch = [texts[0], *extra[start : start + MAX_VERSIONS - 1]]
        batch_keys = [reading_key(t, requirements, jd_text, _reader(ws)) for t in batch]
        batch_fit = await asyncio.to_thread(
            read_versions, batch, requirements, jd_text, llm, _known(ws, batch_keys)
        )
        _remember(ws, batch_keys, batch_fit)
        projected_readings.extend(batch_fit[1:])
    for (index, document), reading, text in zip(projected, projected_readings, extra, strict=True):
        pending = [a for a in document.tailored.review_additions if a.status == "pending"]
        outlook = project(fit[0], fit[index], reading, pending, requirements, search_fit)
        outlook = with_keywords(outlook, keywords, texts[index], text)
        readings[index] = readings[index].model_copy(update={"if_confirmed": outlook})
    progress.step("Ranking the versions")
    best_id, summary = best_version(readings, len(requirements))
    comparison = VersionComparison(
        job_id=job_id,
        job_title=lead.job_title or lead.jd.job_title,
        job_company=lead.job_company or lead.jd.company or "",
        search_fit_score=search_fit.score if search_fit is not None else None,
        search_fit_note=search_note,
        created_at=datetime.now(UTC).isoformat(),
        requirements=requirements,
        keywords=keywords,
        readings=readings,
        best_id=best_id,
        summary=summary,
        setting_effects=setting_effects(readings, len(requirements)),
        reads=READS,
        margin=NOISE_MARGIN,
        method_version=METHOD_VERSION,
    )
    path = _path(ws, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(comparison.model_dump_json(indent=2))
    return comparison
