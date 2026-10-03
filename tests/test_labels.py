"""Your labels (would apply / maybe / no): snapshots that outlive the search history, and each
model setup's verdicts compared with them."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.cv.models import MasterCV
from src.jobs.matcher import build_profile
from src.jobs.models import (
    FIT_WEIGHTS,
    FitRatings,
    JobAssessment,
    JobPosting,
    MatchReport,
    MatchResult,
    ProfileSummary,
    SearchQuery,
)
from src.jobs.screener import finalize
from src.services import history, labels, tracker
from src.services.workspace import Workspace

SUMMARY = ProfileSummary(
    headline="Stem cell scientist",
    seniority="senior",
    years_experience=10,
    core_expertise=["iPSC differentiation"],
    key_skills=[],
    target_roles=["Principal Scientist"],
    not_a_fit=[],
    summary="Cell therapy scientist.",
)
STRONG = (4, 4, 3, 4, 3, 3)  # 91: a match
MID = (3, 3, 2, 3, 2, 2)  # 66: a match
LOW = (2, 2, 2, 2, 2, 2)  # 50: not a match


def _judged(job_id: str, levels: tuple[int, ...], title: str | None = None) -> MatchResult:
    ratings = FitRatings(**dict(zip(FIT_WEIGHTS, levels, strict=True)))
    verdict = finalize(JobAssessment(job_id=job_id, ratings=ratings, fit_summary="x"), 60)
    job = JobPosting(id=job_id, title=title or f"Role {job_id}", company="Co")
    return MatchResult(job=job, verdict=verdict)


def _setup(ws: Workspace, screening: str) -> None:
    """Switch the screening model, so the next search is judged by another setup."""
    ws.llm = ws.llm.model_copy(update={"screening_model": screening})


def _search(ws: Workspace, master_cv: MasterCV, results: list[MatchResult]) -> None:
    """Record a screened search, as run_search does: it becomes the current report."""
    from src.services.search_service import SearchOutcome, SearchRequest

    report = MatchReport(
        profile=build_profile(master_cv),
        threshold=60,
        matches=[r for r in results if r.verdict and r.verdict.match],
        below_threshold=[r for r in results if not (r.verdict and r.verdict.match)],
        screened=True,
        summary=SUMMARY,
    )
    query = SearchQuery(titles=["Principal Scientist"])
    ws.last_query, ws.last_report = query, report
    history.record(
        ws, SearchRequest(query=query), SearchOutcome(report=report, fetched=len(results))
    )


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV) -> Workspace:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    return ws


def test_label_keeps_a_snapshot_that_outlives_the_search(
    ws: Workspace, master_cv: MasterCV
) -> None:
    _search(ws, master_cv, [_judged("a", STRONG), _judged("b", LOW)])
    assert labels.set_label(ws, "a", "yes")
    assert labels.set_label(ws, "b", "no")
    assert not labels.set_label(ws, "zzz", "yes")  # not in the current search

    ws.last_report = None  # the search is gone: the labels still hold everything needed
    store = labels.load(ws)
    first = next(x for x in store.labels if x.job_id == "a")
    assert first.job.title == "Role a" and first.threshold == 60
    assert list(first.verdicts) == [ws.last_models] and "screening" in (ws.last_models or "")
    assert first.verdicts[ws.last_models].fit_score == 91
    assert first.query.titles == ["Principal Scientist"]
    assert list(store.profiles.values()) == [SUMMARY]  # one summary, shared by both labels
    assert {x.profile_key for x in store.labels} == set(store.profiles)


def test_label_note_is_the_tracker_note_kept_in_sync(ws: Workspace, master_cv: MasterCV) -> None:
    _search(ws, master_cv, [_judged("a", STRONG), _judged("b", LOW)])
    job_a = ws.last_report.matches[0].job  # type: ignore[union-attr]
    tracker.set_status(ws, job_a, "open", "too junior")
    labels.set_label(ws, "a", "no")
    labels.set_label(ws, "b", "yes")  # never noted: empty
    notes = {x.job_id: x.note for x in labels.load(ws).labels}
    assert notes == {"a": "too junior", "b": ""}

    tracker.set_status(ws, job_a, "open", "too junior, wet lab only")  # edited afterwards
    review = labels.review(ws)  # reading the review syncs the stored labels
    assert {x.job_id: x.note for x in labels.load(ws).labels}["a"] == "too junior, wet lab only"
    assert review.setups[0].false_accepts == [
        "Role a · Co (91) — your note: too junior, wet lab only"
    ]


def test_relabel_replaces_and_removal_prunes_the_summary(
    ws: Workspace, master_cv: MasterCV
) -> None:
    _search(ws, master_cv, [_judged("a", STRONG)])
    labels.set_label(ws, "a", "yes")
    labels.set_label(ws, "a", "maybe")
    assert [x.label for x in labels.load(ws).labels] == ["maybe"]
    labels.set_label(ws, "a", None)
    store = labels.load(ws)
    assert store.labels == [] and store.profiles == {}


def test_review_compares_each_setup_with_the_labels(ws: Workspace, master_cv: MasterCV) -> None:
    results = [_judged("y1", STRONG), _judged("y2", LOW), _judged("n1", MID), _judged("n2", LOW)]
    _search(ws, master_cv, [*results, _judged("m", MID)])
    for job_id, label in [("y1", "yes"), ("y2", "yes"), ("n1", "no"), ("n2", "no"), ("m", "maybe")]:
        labels.set_label(ws, job_id, label)  # type: ignore[arg-type]

    review = labels.review(ws)
    assert (review.total, review.yes, review.maybe, review.no) == (5, 2, 1, 2)
    assert not review.ready and review.by_job["y1"] == "yes"
    [setup] = review.setups
    assert (setup.yes, setup.no, setup.kept_yes, setup.passed_no) == (2, 2, 1, 1)
    assert setup.agreement == 0.5  # y1 kept, n2 rejected; y2 missed, n1 let through
    # pairs: y1(91) beats n1(66) and n2(50); y2(50) loses to n1, ties n2 -> 2.5 / 4
    assert setup.ranking == 0.625
    assert setup.misses == ["Role y2 · Co (50)"] and setup.false_accepts == ["Role n1 · Co (66)"]


def test_label_routes(ws: Workspace, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    client = TestClient(webapp.app)
    _search(ws, master_cv, [_judged("a", STRONG)])
    assert client.put("/api/jobs/a/label", json={"label": "yes"}).json() == {"label": "yes"}
    assert client.put("/api/jobs/zzz/label", json={"label": "no"}).status_code == 404
    review = client.get("/api/labels").json()
    assert review["by_job"] == {"a": "yes"} and review["target_total"] == labels.TARGET_TOTAL
    client.put("/api/jobs/a/label", json={"label": None})
    assert json.loads((ws.settings.data_dir / "job_labels.json").read_text())["labels"] == []


def test_same_posting_elsewhere_shares_one_label_and_gains_verdicts(
    ws: Workspace, master_cv: MasterCV
) -> None:
    _search(ws, master_cv, [_judged("linkedin:1", STRONG, "Principal Scientist")])
    labels.set_label(ws, "linkedin:1", "yes")
    first_setup = ws.last_models

    # A later search, another setup: the same role at the same employer, on another board
    _setup(ws, "luna")
    _search(ws, master_cv, [_judged("totaljobs:9", LOW, "Principal Scientist")])
    assert labels.attach_verdicts(ws, ws.last_report, ws.last_models) == 1  # type: ignore[arg-type]
    review = labels.review(ws)
    assert review.total == 1 and review.by_job["totaljobs:9"] == "yes"  # its card shows it

    labels.set_label(ws, "totaljobs:9", "no")  # relabelled from the other board: still one
    [item] = labels.load(ws).labels
    assert item.label == "no" and item.ids == ["linkedin:1", "totaljobs:9"]
    assert item.job.id == "linkedin:1"  # the first snapshot is kept
    assert {m: v.fit_score for m, v in item.verdicts.items()} == {
        first_setup: 91,
        ws.last_models: 50,
    }


def test_setups_are_compared_on_the_jobs_both_judged(ws: Workspace, master_cv: MasterCV) -> None:
    jobs = {"y1": STRONG, "y2": STRONG, "y3": LOW, "n1": LOW, "n2": LOW, "n3": MID}
    _setup(ws, "sol")
    _search(ws, master_cv, [_judged(j, lv) for j, lv in jobs.items()])  # stays in the history
    sol = ws.last_models
    lenient = {**jobs, "y3": MID, "n1": STRONG, "n2": MID}  # finds y3, lets n1 and n2 through
    _setup(ws, "luna")
    _search(ws, master_cv, [_judged(j, lv) for j, lv in lenient.items()])
    for job_id in jobs:
        labels.set_label(ws, job_id, "yes" if job_id.startswith("y") else "no")

    review = labels.review(ws)  # reads the sol search from the history
    assert labels.load(ws).searches_read and len(labels.review(ws).head_to_head) == 1
    [pair] = review.head_to_head
    assert pair.shared == 6
    by_setup = {pair.first.models: pair.first, pair.second.models: pair.second}
    assert (by_setup[sol].kept_yes, by_setup[sol].passed_no) == (2, 1)
    assert (by_setup[ws.last_models].kept_yes, by_setup[ws.last_models].passed_no) == (3, 3)
    assert len(pair.split) == 3  # y3, n1, n2 decided differently


def test_labels_saved_in_the_first_format_are_upgraded(ws: Workspace, master_cv: MasterCV) -> None:
    _search(ws, master_cv, [_judged("a", STRONG)])
    labels.set_label(ws, "a", "yes")
    path = ws.settings.data_dir / "job_labels.json"
    data = json.loads(path.read_text())
    old = data["labels"][0]
    [(models, verdict)] = old.pop("verdicts").items()
    old.pop("ids")
    old |= {"verdict": verdict, "models": models}
    path.write_text(json.dumps({"version": 1, "profiles": data["profiles"], "labels": [old]}))

    [item] = labels.load(ws).labels
    assert item.ids == ["a"] and item.verdicts[models].fit_score == 91


def test_report_keeps_the_match_threshold_and_old_labels_are_corrected(
    ws: Workspace, master_cv: MasterCV
) -> None:
    from src.jobs.screener import apply_verdicts

    result = _judged("a", STRONG)
    report = MatchReport(
        profile=build_profile(master_cv), threshold=0, below_threshold=[result], matches=[]
    )  # smart mode: the pre-filter cut is 0
    apply_verdicts(report, {"a": result.verdict}, 60)  # type: ignore[dict-item]
    assert report.threshold == 60 and report.matches == [result]

    _search(ws, master_cv, [_judged("a", STRONG)])
    labels.set_label(ws, "a", "yes")
    path = ws.settings.data_dir / "job_labels.json"
    data = json.loads(path.read_text())
    data["labels"][0]["threshold"] = 0.0  # as saved before the fix
    path.write_text(json.dumps(data))
    assert labels.load(ws).labels[0].threshold == labels.SCREENING_DEFAULT == 60


def test_model_comparison_scores_each_setup_against_your_labels() -> None:
    import asyncio
    import re

    from src.jobs.screener import ScreeningBatch
    from src.services import model_compare
    from tests.conftest import FakeLLM

    levels = {"Yes strong": STRONG, "Yes weak": LOW, "No but scored": MID, "No weak": LOW}
    prompts: list[str] = []

    def answer(prompt: str) -> ScreeningBatch:
        prompts.append(prompt)
        found = re.findall(r'job_id="([^"]+)">\ntitle: ([^\n]+)', prompt)
        return ScreeningBatch(
            verdicts=[
                JobAssessment(
                    job_id=job_id,
                    ratings=FitRatings(**dict(zip(FIT_WEIGHTS, levels[title], strict=True))),
                    fit_summary=title,
                )
                for job_id, title in found
            ]
        )

    def labelled(i: int, title: str) -> labels.LabelledJob:
        return labels.LabelledJob(
            job_id=f"j{i}",
            label="yes" if title.startswith("Yes") else "no",
            labelled_at="2026-10-03T00:00:00+00:00",
            job=JobPosting(id=f"j{i}", title=title, company="Co"),
            query=SearchQuery(titles=["Principal Scientist"]),
            threshold=60,
            profile_key="p",
        )

    items = [labelled(i, t) for i, t in enumerate(levels)]
    fake = FakeLLM({ScreeningBatch: answer})
    report = asyncio.run(
        model_compare.compare_labels(
            items,
            {"p": SUMMARY},
            lambda role: fake,
            "setup A",
            ("<career_intent>wet lab</career_intent>", lambda q: "Cambridge (hybrid)"),
        )
    )
    s = report.score
    assert (s.yes, s.no, s.kept_yes, s.passed_no) == (2, 2, 1, 1)
    assert s.agreement == 0.5 and s.ranking == 0.625  # 91 > 66, 91 > 50, 50 < 66, 50 = 50
    assert s.misses == ["Yes weak · Co (50)"]
    assert all("Cambridge (hybrid)" in p and "wet lab" in p for p in prompts)  # as the app sees it
