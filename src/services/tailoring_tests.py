"""The tailoring test set: CVs and jobs the optimiser is measured on (user's decision
2026-10-21).

The user saves a tailored CV's inputs to the set from the app ("Save to test set"): the CV
it came from, the job description and its analysis, the settings, and what steered it (the
job_matcher's guidance, the stored profile, the user's other CVs, declined proposals). A run
tailors every case again in optimised mode through the same pipeline as the app
(`tailor.tailor`) and measures each result by code only: how much shorter it is, whether
the summary, key strengths and relevant skills have their format, whether a job keyword was
lost, the screening and readability scores, and the gap fillers. Running a case more than
once (`repeat`) shows how consistently the summary and the shorter version are built.

    python -m src.services.tailoring_tests list
    python -m src.services.tailoring_tests run [--repeat 2] [--only CASE_ID]
    python -m src.services.tailoring_tests remove CASE_ID

A run calls the CV model for each case (paid or plan usage): run it by hand.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.cv.models import JDAnalysis, MasterCV, TailoredCV, TailoringSettings
from src.cv.tailor import ats_keywords, cv_to_text, printed_text, tailor
from src.services import declined, tailored_documents
from src.services.workspace import Workspace
from src.tools.search_tools import mentions

SUMMARY_SENTENCES = (2, 3)
SUMMARY_WORDS = 60
KEY_STRENGTHS = 3
RELEVANT_SKILLS = (6, 8)
MIN_SHORTER = 0.2  # a short version at least this much shorter than the CV it came from


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TestCase(_Model):
    """One CV and job the optimiser is measured on, with everything that steered it."""

    id: str
    saved_at: str
    name: str
    cv_id: str | None = None
    job_id: str
    cv: MasterCV
    job_description: str
    jd: JDAnalysis
    settings: TailoringSettings = Field(default_factory=TailoringSettings)
    guidance: str = ""
    profile: str = ""
    background: str = ""
    declined: list[str] = Field(default_factory=list)


class CaseMetrics(_Model):
    """What code measures on one optimised CV."""

    words_before: int
    words_after: int
    shorter_by: float
    lines_before: int
    lines_after: int
    lines_per_role: dict[str, int]
    skills_before: int
    skills_after: int
    summary_words: int
    summary_sentences: int
    key_strengths: int
    relevant_skills: int
    shorter_roles: int
    versions_rejected: int
    strengths_rejected: int
    gap_fillers: int
    gap_fillers_likely: int
    screening_before: int | None = None
    screening_after: int | None = None
    screening_if_confirmed: int | None = None
    readability_before: int | None = None
    readability_after: int | None = None
    keywords_lost: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class CaseRun(_Model):
    case_id: str
    name: str
    runs: list[CaseMetrics] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    spread: dict[str, float] = Field(
        default_factory=dict, description="max - min across repeats (0 = built the same way)"
    )


class TestReport(_Model):
    ran_at: str
    model: str
    repeat: int
    cases: list[CaseRun] = Field(default_factory=list)
    averages: dict[str, float] = Field(default_factory=dict)


def _path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "tailoring_tests.json"


def cases(ws: Workspace) -> list[TestCase]:
    """The saved test cases, oldest first."""
    path = _path(ws)
    if not path.exists():
        return []
    return [TestCase.model_validate(item) for item in json.loads(path.read_text())]


def _save(ws: Workspace, items: list[TestCase]) -> None:
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([c.model_dump(mode="json") for c in items], indent=1))


def add(ws: Workspace, document_id: str) -> TestCase:
    """Save a tailored CV's inputs to the test set (replacing the case for the same CV and
    job): the CV it came from, the job, its settings and what steered it."""
    from src.services.cv_service import library_background, matcher_guidance, stored_profile

    document = tailored_documents.load(ws, document_id)
    if not document.job_description:
        raise ValueError("This CV has no job description to test against")
    result = ws.result(document.job_id)
    company = f" at {document.job_company}" if document.job_company else ""
    case = TestCase(
        id=uuid.uuid4().hex[:10],
        saved_at=datetime.now(UTC).isoformat(timespec="seconds"),
        name=f"{document.job_title}{company} · {document.cv_id or 'CV'}",
        cv_id=document.cv_id,
        job_id=document.job_id,
        cv=document.source_cv,
        job_description=document.job_description,
        jd=document.jd,
        settings=document.settings or TailoringSettings(mode="optimised"),
        guidance=matcher_guidance(result.verdict if result is not None else None),
        profile=stored_profile(ws, result.family if result is not None else None),
        background=library_background(ws, document.cv_id),
        declined=declined.texts(ws, document.cv_id),
    )
    kept = [c for c in cases(ws) if (c.cv_id, c.job_id) != (case.cv_id, case.job_id)]
    _save(ws, [*kept, case])
    return case


def remove(ws: Workspace, case_id: str) -> None:
    """Take a case out of the test set."""
    items = cases(ws)
    if not any(c.id == case_id for c in items):
        raise ValueError("Test case not found")
    _save(ws, [c for c in items if c.id != case_id])


def measure(case: TestCase, out: TailoredCV) -> CaseMetrics:
    """What code reads from one optimised CV (no model): length, format, scores, gap
    fillers, and the expectations it misses (`problems`)."""
    before, after = cv_to_text(case.cv, tags=False), printed_text(out)
    summary = out.cv.basics.summary or ""
    lost = [k for k in ats_keywords(case.jd) if mentions(before, k) and not mentions(after, k)]
    gaps = [a for a in out.review_additions if a.status == "pending"]
    sc, rd = out.screening, out.readability
    metrics = CaseMetrics(
        words_before=len(before.split()),
        words_after=len(after.split()),
        shorter_by=round(1 - len(after.split()) / max(1, len(before.split())), 3),
        lines_before=sum(len(r.bullets) for r in case.cv.experience),
        lines_after=sum(len(r.bullets) for r in out.cv.experience),
        lines_per_role={r.id: len(r.bullets) for r in out.cv.experience},
        skills_before=sum(len(g.items) for g in case.cv.skills),
        skills_after=sum(len(g.items) for g in out.cv.skills),
        summary_words=len(summary.split()),
        summary_sentences=len([s for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]),
        key_strengths=len(out.key_strengths),
        relevant_skills=len(out.role_skills),
        shorter_roles=sum(
            c.kind == "shorter_role" and c.status == "applied" for c in out.compressions
        ),
        versions_rejected=sum(c.source_id.startswith("shorter:") for c in out.changes),
        strengths_rejected=sum(
            c.source_id == "key_strengths" and not c.accepted for c in out.changes
        ),
        gap_fillers=len(gaps),
        gap_fillers_likely=sum(a.likelihood == "likely" for a in gaps),
        screening_before=sc.before.score if sc else None,
        screening_after=sc.after.score if sc else None,
        screening_if_confirmed=sc.if_confirmed.score if sc and sc.if_confirmed else None,
        readability_before=rd.before.score if rd else None,
        readability_after=rd.after.score if rd else None,
        keywords_lost=lost,
    )
    return metrics.model_copy(update={"problems": problems(metrics)})


def problems(m: CaseMetrics) -> list[str]:
    """The expectations of an optimised CV that `m` misses."""
    out: list[str] = []
    if not SUMMARY_SENTENCES[0] <= m.summary_sentences <= SUMMARY_SENTENCES[1]:
        out.append(f"summary has {m.summary_sentences} sentences (2 or 3)")
    if m.summary_words > SUMMARY_WORDS:
        out.append(f"summary has {m.summary_words} words (at most {SUMMARY_WORDS})")
    if m.key_strengths != KEY_STRENGTHS:
        out.append(f"{m.key_strengths} key strengths ({KEY_STRENGTHS})")
    if not RELEVANT_SKILLS[0] <= m.relevant_skills <= RELEVANT_SKILLS[1]:
        out.append(f"{m.relevant_skills} relevant skills (6 to 8)")
    if m.shorter_by < MIN_SHORTER:
        out.append(f"only {m.shorter_by:.0%} shorter (at least {MIN_SHORTER:.0%})")
    if m.keywords_lost:
        out.append("job keywords lost: " + ", ".join(m.keywords_lost))
    if m.versions_rejected:
        out.append(f"{m.versions_rejected} role(s) without a shorter version")
    if m.gap_fillers == 0:
        out.append("no gap fillers")
    if m.screening_before is not None and (m.screening_after or 0) < m.screening_before:
        out.append(f"screening score fell ({m.screening_before} → {m.screening_after})")
    return out


SPREAD_FIELDS = ("words_after", "lines_after", "summary_words", "key_strengths", "skills_after")


def run(ws: Workspace, repeat: int = 1, only: str | None = None) -> TestReport:
    """Tailor every case (or `only` one) `repeat` times in optimised mode, as the app does,
    and measure each result; the report is saved under data/tailoring_tests/."""
    llm = ws.structured("cv", None, "Tailoring test set")
    chosen = [c for c in cases(ws) if only is None or c.id == only]
    report = TestReport(
        ran_at=datetime.now(UTC).isoformat(timespec="seconds"),
        model=f"{ws.llm.provider_for('cv')}:{ws.llm.model_for('cv')}:{ws.llm.effort_for('cv')}",
        repeat=repeat,
    )
    for case in chosen:
        result = CaseRun(case_id=case.id, name=case.name)
        for _ in range(repeat):
            try:
                picked: Any = case.settings  # its fields are the choices `tailor` takes
                out = tailor(
                    case.cv, case.job_description, llm, case.guidance, True, case.jd,
                    picked.emphasis, picked.level, picked.length, case.profile,
                    picked.audience, False, "optimised", None,
                    background=case.background, declined=case.declined,
                )  # fmt: skip
                result.runs.append(measure(case, out))
            except Exception as exc:  # noqa: BLE001 - one failed case does not end the run
                result.errors.append(str(exc))
        result.spread = _spread(result.runs)
        report.cases.append(result)
    report.averages = _averages(report)
    _store(ws, report)
    return report


def _spread(runs: list[CaseMetrics]) -> dict[str, float]:
    if len(runs) < 2:
        return {}
    out = {f: float(max(getattr(r, f) for r in runs) - min(getattr(r, f) for r in runs))
           for f in SPREAD_FIELDS}  # fmt: skip
    roles = {role for r in runs for role in r.lines_per_role}
    out["lines_per_role"] = float(max(
        max(r.lines_per_role.get(role, 0) for r in runs)
        - min(r.lines_per_role.get(role, 0) for r in runs)
        for role in roles
    )) if roles else 0.0  # fmt: skip
    return out


def _averages(report: TestReport) -> dict[str, float]:
    runs = [m for case in report.cases for m in case.runs]
    if not runs:
        return {}
    fields = ("shorter_by", "screening_after", "readability_after", "gap_fillers")
    out = {f: round(statistics.mean(float(getattr(m, f) or 0) for m in runs), 3) for f in fields}
    out["with_problems"] = round(sum(bool(m.problems) for m in runs) / len(runs), 3)
    return out


def _store(ws: Workspace, report: TestReport) -> Path:
    folder = ws.settings.data_dir / "tailoring_tests"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"report_{report.ran_at.replace(':', '-')}.json"
    path.write_text(report.model_dump_json(indent=1))
    return path


def _print(report: TestReport) -> None:
    print(f"{report.ran_at} · {report.model} · {report.repeat} run(s) per case")
    for case in report.cases:
        print(f"\n{case.name} [{case.case_id}]")
        for m in case.runs:
            print(
                f"  {m.words_before}→{m.words_after} words ({m.shorter_by:.0%} shorter), "
                f"{m.lines_before}→{m.lines_after} lines, skills {m.skills_before}→"
                f"{m.skills_after}, screening {m.screening_before}→{m.screening_after} "
                f"(if confirmed {m.screening_if_confirmed}), {m.gap_fillers} gap fillers"
            )
            for problem in m.problems:
                print(f"    ! {problem}")
        for error in case.errors:
            print(f"  error: {error}")
        if case.spread:
            print(
                "  spread across runs: " + ", ".join(f"{k} {v:g}" for k, v in case.spread.items())
            )
    print("\naverages: " + ", ".join(f"{k} {v:g}" for k, v in report.averages.items()))


def main() -> None:
    from src.core.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    runner = sub.add_parser("run")
    runner.add_argument("--repeat", type=int, default=1)
    runner.add_argument("--only")
    drop = sub.add_parser("remove")
    drop.add_argument("case_id")
    args = parser.parse_args()
    ws = Workspace(get_settings())
    if args.command == "list":
        for case in cases(ws):
            print(f"{case.id}  {case.saved_at}  {case.name}")
    elif args.command == "remove":
        remove(ws, args.case_id)
    else:
        _print(run(ws, args.repeat, args.only))


if __name__ == "__main__":
    main()
