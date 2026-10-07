"""job_matcher screening: rated levels, remembered verdicts and second opinions on matches
and near the threshold. The LLM is faked; prompts show which postings each call carried."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.jobs.models import FIT_WEIGHTS, FitRatings, JobAssessment, JobPosting, ProfileSummary
from src.jobs.screener import ScreeningBatch, VerdictCache, combine, screen_jobs
from tests.conftest import FakeLLM

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
STRONG = (4, 4, 3, 4, 3, 3)  # 91
NEAR = (3, 2, 2, 2, 2, 2)  # 58: within 5 of 60
LOWER = (2, 2, 2, 2, 2, 2)  # 50


def _ratings(levels: tuple[int, ...]) -> FitRatings:
    return FitRatings(**dict(zip(FIT_WEIGHTS, levels, strict=True)))


def _job(job_id: str, title: str, description: str = "") -> JobPosting:
    return JobPosting(id=job_id, title=title, company="Acme", description=description)


class Matcher:
    """Fake job_matcher: levels by title; records the job ids of every call."""

    def __init__(self, levels: dict[str, list[tuple[int, ...]]]) -> None:
        self.levels = levels  # title -> levels for the 1st, 2nd, ... assessment
        self.calls: list[list[str]] = []
        self.seen: dict[str, int] = {}

    def __call__(self, prompt: str) -> ScreeningBatch:
        found = re.findall(r'job_id="([^"]+)">\ntitle: ([^\n]+)', prompt)
        self.calls.append([job_id for job_id, _ in found])
        out = []
        for job_id, title in found:
            n = self.seen.get(title, 0)
            self.seen[title] = n + 1
            options = self.levels[title]
            levels = options[min(n, len(options) - 1)]
            out.append(JobAssessment(job_id=job_id, ratings=_ratings(levels), fit_summary=title))
        return ScreeningBatch(verdicts=out)


def _screen(matcher: Matcher, jobs: list[JobPosting], cache: VerdictCache | None, **kw: object):
    llm = FakeLLM({ScreeningBatch: matcher})
    return asyncio.run(
        screen_jobs(SUMMARY, jobs, llm, threshold=60, cache=cache, model="m", **kw)  # type: ignore[arg-type]
    )


def test_remembered_verdicts_are_reused_and_identical(tmp_path: Path) -> None:
    jobs = [_job("a", "Principal Scientist"), _job("b", "Lab Technician")]
    matcher = Matcher({"Principal Scientist": [STRONG], "Lab Technician": [LOWER]})
    first, errors = _screen(matcher, jobs, VerdictCache(tmp_path / "v.json"))
    assert errors == [] and first["a"].fit_score == 91 and not first["a"].from_memory

    assert matcher.calls[-1] == ["a"]  # the match got its second opinion
    again, _ = _screen(matcher, jobs, VerdictCache(tmp_path / "v.json"))
    assert len(matcher.calls) == 2  # nothing new to judge: no model call at all
    assert again["a"].fit_score == 91 and again["a"].from_memory
    assert again["b"].fit_score == first["b"].fit_score

    # The same posting seen through another source keeps its verdict; a changed one is re-judged
    moved = [_job("reed:9", "Principal Scientist"), _job("b", "Lab Technician", "new text")]
    third, _ = _screen(matcher, moved, VerdictCache(tmp_path / "v.json"))
    assert third["reed:9"].from_memory and not third["b"].from_memory
    assert matcher.calls[-1] == ["b"]


def test_another_model_or_profile_is_not_served_from_memory(tmp_path: Path) -> None:
    jobs = [_job("a", "Principal Scientist")]
    matcher = Matcher({"Principal Scientist": [STRONG]})
    cache = tmp_path / "v.json"
    _screen(matcher, jobs, VerdictCache(cache))
    llm = FakeLLM({ScreeningBatch: matcher})
    asyncio.run(screen_jobs(SUMMARY, jobs, llm, cache=VerdictCache(cache), model="other"))
    assert len(matcher.calls) == 4  # first pass + second opinion, for each model


def test_borderline_postings_get_a_second_opinion_averaged_down(tmp_path: Path) -> None:
    jobs = [_job("a", "Principal Scientist"), _job("b", "Senior Scientist")]
    # b: 58 first (borderline), 66 second -> mean of levels rounded down = NEAR again
    matcher = Matcher(
        {"Principal Scientist": [STRONG], "Senior Scientist": [NEAR, (3, 3, 2, 3, 2, 2)]}
    )
    notes: list[str] = []

    async def note(message: str) -> None:
        notes.append(message)

    reviews: list[tuple[int, int]] = []

    async def review_progress(done: int, total: int) -> None:
        reviews.append((done, total))

    verdicts, _ = _screen(
        matcher, jobs, VerdictCache(tmp_path / "v.json"), note=note, review_progress=review_progress
    )
    assert sorted(matcher.calls[1:]) == [["a"], ["b"]]  # second opinions: one posting each
    assert reviews == [(0, 2), (1, 2), (2, 2)]  # a bar from the start, then each one done
    assert verdicts["b"].reviewed and verdicts["b"].fit_score == 58
    assert verdicts["a"].reviewed and "Second opinion on 2" in notes[0]

    again, _ = _screen(matcher, jobs, VerdictCache(tmp_path / "v.json"))
    assert len(matcher.calls) == 3 and again["b"].reviewed and again["b"].fit_score == 58


def test_clear_matches_get_a_second_opinion_that_can_cap_them(tmp_path: Path) -> None:
    """A lenient first pass scores 91; the reviewer finds an unmet core requirement -> capped
    below the floor and no longer a match."""
    jobs = [_job("a", "Principal Bioinformatician"), _job("b", "Lab Technician")]
    matcher = Matcher({"Principal Bioinformatician": [STRONG], "Lab Technician": [LOWER]})
    first = FakeLLM({ScreeningBatch: matcher})

    def reviewer_answer(prompt: str) -> ScreeningBatch:
        out = matcher(prompt)
        for v in out.verdicts:
            v.essential_unmet = ["Python coding not evidenced"]
        return out

    reviewer = FakeLLM({ScreeningBatch: reviewer_answer})
    verdicts, _ = asyncio.run(
        screen_jobs(SUMMARY, jobs, first, threshold=60, review_llm=reviewer)  # type: ignore[arg-type]
    )
    assert verdicts["a"].reviewed and verdicts["a"].fit_score == 55 and not verdicts["a"].match
    assert verdicts["a"].cap_reason and "Python" in verdicts["a"].cap_reason
    assert not verdicts["b"].reviewed  # a clear rejection is not asked twice


def test_description_label_and_employer_alias_reuse_verdict(tmp_path: Path) -> None:
    matcher = Matcher({"Investigator Scientist": [STRONG]})
    first = _job("a", "Investigator Scientist", "Study cell therapy methods.")
    first = first.model_copy(update={"company": "UKRI"})
    _screen(matcher, [first], VerdictCache(tmp_path / "v.json"))
    relisted = first.model_copy(
        update={
            "id": "b",
            "company": "UK Research and Innovation",
            "description": "Description:\n Study cell therapy methods. ",
        }
    )
    again, _ = _screen(matcher, [relisted], VerdictCache(tmp_path / "v.json"))
    assert len(matcher.calls) == 2  # first pass + second opinion, then nothing
    assert again["b"].from_memory and again["b"].fit_score == 91


def test_midrange_seniority_and_leadership_get_second_opinion(tmp_path: Path) -> None:
    levels = (3, 3, 3, 3, 3, 3)
    matcher = Matcher({"Investigator Scientist": [levels, (3, 3, 2, 2, 3, 3)]})
    verdicts, _ = _screen(
        matcher, [_job("a", "Investigator Scientist")], VerdictCache(tmp_path / "v.json")
    )
    assert len(matcher.calls) == 2
    assert verdicts["a"].reviewed


def test_combine_keeps_blockers_from_either_reading() -> None:
    first = JobAssessment(job_id="j", ratings=_ratings((4, 4, 4, 4, 4, 4)), fit_summary="x")
    second = JobAssessment(
        job_id="j", ratings=_ratings((3, 3, 4, 2, 4, 3)), fit_summary="y", dealbreakers=["sales"]
    )
    both = combine(first, second)
    assert both.ratings.model_dump() == {
        "function": 3, "domain": 3, "seniority": 4, "leadership": 3, "sector": 4, "practicality": 3
    }  # fmt: skip
    assert both.dealbreakers == ["sales"] and both.fit_summary == "x"


def test_batches_are_small_and_always_the_same(tmp_path: Path) -> None:
    jobs = [_job(f"j{i}", f"Scientist {i}") for i in range(7)]
    levels = {f"Scientist {i}": [LOWER] for i in range(7)}  # no second opinions
    one, two = Matcher(levels), Matcher(levels)
    _screen(one, jobs, None, batch_size=3)
    _screen(two, list(reversed(jobs)), None, batch_size=3)
    assert [len(c) for c in one.calls] == [3, 3, 1]
    assert sorted(map(sorted, one.calls)) == sorted(map(sorted, two.calls))  # same groupings


def test_a_posting_without_readable_requirements_is_listed_as_a_low_stretch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LinkedIn card LinkedIn refused to open (no text) scores high on its title alone; its
    requirements were never checked, so it stays listed but capped under the checked matches,
    and is not sent for a second opinion (a second reading cannot see more)."""
    from src.jobs import screener

    monkeypatch.setattr(screener, "CHECKABLE_CHARS", 600)
    full = "Responsibilities and requirements. " * 30
    jobs = [_job("card", "Principal Scientist"), _job("full", "Principal Scientist", full)]
    matcher = Matcher({"Principal Scientist": [STRONG]})
    llm = FakeLLM({ScreeningBatch: matcher})
    verdicts, _ = asyncio.run(screen_jobs(SUMMARY, jobs, llm, threshold=60))
    card = verdicts["card"]
    assert (card.fit_score, card.match, card.reviewed, card.requirements_checked) == (
        62, True, False, False,
    )  # fmt: skip
    assert card.cap_reason and "requirements not checked" in card.cap_reason
    assert verdicts["full"].match and verdicts["full"].reviewed
    assert verdicts["full"].requirements_checked and verdicts["full"].fit_score == 91


def _unmet_by_reading(flags: list[bool]):  # type: ignore[no-untyped-def]
    """A matcher answering STRONG levels whose n-th reading of a posting flags an unmet core
    requirement when `flags[n]` is true."""
    matcher = Matcher({"Principal Scientist": [STRONG]})
    readings: list[bool] = list(flags)

    def answer(prompt: str) -> ScreeningBatch:
        out = matcher(prompt)
        for v in out.verdicts:
            if readings.pop(0):
                v.essential_unmet = ["Proven bioconjugation process experience"]
        return out

    return answer, matcher


@pytest.mark.parametrize(
    ("flags", "blocked"),
    [
        ([True, False, False], False),  # one over-cautious reading is outvoted
        ([True, False, True], True),  # two of three agree: the role needs what the CV lacks
        ([True, True], True),  # both readings agree: no third reading needed
    ],
)
def test_an_unmet_core_requirement_blocks_only_when_most_readings_agree(
    flags: list[bool], blocked: bool
) -> None:
    answer, matcher = _unmet_by_reading(flags)
    llm = FakeLLM({ScreeningBatch: answer})
    job = _job("a", "Principal Scientist", "Requirements and responsibilities. " * 30)
    verdicts, _ = asyncio.run(screen_jobs(SUMMARY, [job], llm, threshold=60))
    v = verdicts["a"]
    assert len(matcher.calls) == len(flags) and v.reviewed
    assert v.match is not blocked and bool(v.essential_unmet) is blocked
