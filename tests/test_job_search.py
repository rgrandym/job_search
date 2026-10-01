"""job_search: scoring matrix, exclusions, matching pipeline, and job sources."""

from __future__ import annotations

import json
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.core.config import ScoringWeights, Settings
from src.core.llm_provider import HashingEmbedder, cosine
from src.cv.models import Experience, MasterCV
from src.jobs import fetcher, scorer
from src.jobs.matcher import JobMatcher, build_profile, years_of_experience
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import HttpFetcher, extract_jsonld_jobs, posting_from_jsonld
from src.jobs.sources.companies import CompanyBoard, CompanyCareersSource
from src.jobs.sources.inbox import InboxSource, parse_alert_email
from src.jobs.sources.job_boards import ReedSource
from tests.conftest import EXAMPLES


@pytest.fixture
def jobs() -> list[JobPosting]:
    return fetcher.JsonFileSource(EXAMPLES / "jobs.example.json").fetch(SearchQuery())


def _job(jobs: list[JobPosting], job_id: str) -> JobPosting:
    return next(j for j in jobs if j.id == job_id)


# ---------------------------------------------------------------- scoring


def test_default_weights_match_spec() -> None:
    w = ScoringWeights()
    assert (w.title, w.skills, w.experience, w.location, w.semantic) == (0.25, 0.35, 0.2, 0.1, 0.1)


def test_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match="sum to 1.0"):
        ScoringWeights(title=0.5)


def test_years_merge_overlaps() -> None:
    exps = [
        Experience(id="a", company="A", title="x", start="2020-01", end="2020-12"),
        Experience(id="b", company="B", title="x", start="2020-07", end="2021-12"),
        Experience(id="c", company="C", title="x", start="2023-01", end="2023-12"),
    ]
    assert years_of_experience(exps, date(2024, 1, 1)) == 3.0


def test_profile_from_cv(master_cv: MasterCV) -> None:
    p = build_profile(master_cv, today=date(2026, 3, 1))
    assert p.titles[0] == "Senior Machine Learning Engineer"
    assert p.years_experience == 8.6  # Sep 2017 - Mar 2026 inclusive, no overlap
    assert "kubernetes" in p.skills and "python" in p.skills
    assert p.work_arrangements == ["remote", "hybrid"]


def test_strong_match_scores_above_threshold(master_cv: MasterCV, jobs: list[JobPosting]) -> None:
    profile = build_profile(master_cv)
    s = scorer.score(profile, _job(jobs, "job-strong"), similarity=0.4, weights=ScoringWeights())
    assert s.total >= 85
    assert s.missing_required_skills == []
    assert s.title > 0.9 and s.location == 1.0


def test_partial_match_scores_lower(master_cv: MasterCV, jobs: list[JobPosting]) -> None:
    profile = build_profile(master_cv)
    strong = scorer.score(profile, _job(jobs, "job-strong"), 0.3, ScoringWeights())
    partial = scorer.score(profile, _job(jobs, "job-partial"), 0.3, ScoringWeights())
    assert partial.total < 70 < strong.total
    assert set(partial.missing_required_skills) == {"Spark", "Airflow", "Snowflake"}


@pytest.mark.parametrize(
    ("job_id", "reason"),
    [
        ("job-cert", "certification"),
        ("job-onsite", "work arrangement"),
        ("job-junior", "seniority"),
    ],
)
def test_hard_exclusions(
    master_cv: MasterCV, jobs: list[JobPosting], job_id: str, reason: str
) -> None:
    reasons = scorer.hard_exclusions(build_profile(master_cv), _job(jobs, job_id))
    assert any(reason in r for r in reasons), reasons


def test_location_mismatch_excluded_without_relocation(
    master_cv: MasterCV, jobs: list[JobPosting]
) -> None:
    profile = build_profile(master_cv).model_copy(
        update={"work_arrangements": ["remote", "hybrid", "onsite"]}
    )
    berlin = _job(jobs, "job-onsite")
    assert any("location mismatch" in r for r in scorer.hard_exclusions(profile, berlin))
    relocatable = profile.model_copy(update={"willing_to_relocate": True})
    assert scorer.hard_exclusions(relocatable, berlin) == []
    assert scorer.score_location(relocatable, berlin) == 0.6


def test_matcher_pipeline_buckets_every_job(
    master_cv: MasterCV, jobs: list[JobPosting], settings: Settings
) -> None:
    report = JobMatcher(HashingEmbedder(256), settings).match(master_cv, jobs, threshold=70)
    assert [m.job.id for m in report.matches] == ["job-strong"]
    assert {r.job.id for r in report.excluded} == {"job-cert", "job-onsite", "job-junior"}
    assert [r.job.id for r in report.below_threshold] == ["job-partial"]
    total = (
        len(report.matches)
        + len(report.below_threshold)
        + len(report.excluded)
        + len(report.not_retrieved)
    )
    assert total == len(jobs)


def test_retrieval_top_k_cut(
    master_cv: MasterCV, jobs: list[JobPosting], settings: Settings
) -> None:
    settings = settings.model_copy(update={"retrieval_top_k": 1})
    report = JobMatcher(HashingEmbedder(256), settings).match(master_cv, jobs, threshold=0)
    assert len(report.matches) == 1 and len(report.not_retrieved) == 1


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    e = HashingEmbedder(128)
    a, b = e.embed(["python pytorch kubernetes", "python pytorch kubernetes"])
    assert a == b and abs(cosine(a, a) - 1) < 1e-9


def test_committed_job_schema_matches_model() -> None:
    committed = json.loads(fetcher.SCHEMA_PATH.read_text())
    assert committed == fetcher.json_schema(), "Run: python -m src.jobs.fetcher export-schema"


# ---------------------------------------------------------------- sources


def _http(settings: Settings, routes: dict[str, Any]) -> HttpFetcher:
    """HttpFetcher backed by a MockTransport. `routes` maps URL path -> JSON or text."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    return HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))


def test_reed_source(settings: Settings) -> None:
    settings = settings.model_copy(update={"reed_api_key": SecretStr("k")})
    http = _http(
        settings,
        {
            "/api/1.0/search": {
                "totalResults": 1,
                "results": [
                    {
                        "jobId": 42,
                        "employerName": "Acme",
                        "jobTitle": "ML Engineer (Remote)",
                        "locationName": "London",
                        "minimumSalary": 70000,
                        "maximumSalary": 90000,
                        "date": "15/09/2026",
                        "jobDescription": "snippet",
                        "jobUrl": "https://www.reed.co.uk/jobs/ml-engineer/42",
                    }
                ],
            },
            "/api/1.0/jobs/42": {"jobDescription": "<p>Full <b>Python</b> role</p>"},
        },
    )
    src = ReedSource(http, settings)
    [job] = src.fetch(SearchQuery(titles=["ml engineer"], salary_min=60000))
    assert job.id == "reed:42" and job.company == "Acme"
    assert job.description == "snippet" and job.salary_min == 70000
    job = src.enrich(job)
    assert job.description == "Full Python role"
    assert job.work_arrangement == "remote"
    assert job.posted_at == date(2026, 9, 15)


def test_company_ats_sources(settings: Settings) -> None:
    http = _http(
        settings,
        {
            "/v1/boards/gh/jobs": {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Data Scientist",
                        "location": {"name": "Remote - EU"},
                        "content": "&lt;p&gt;Python&lt;/p&gt;",
                        "absolute_url": "https://gh/1",
                    }
                ]
            },
            "/v0/postings/lv": [
                {
                    "id": "abc",
                    "text": "ML Engineer",
                    "workplaceType": "hybrid",
                    "categories": {"location": "Lisbon"},
                    "descriptionPlain": "PyTorch",
                    "hostedUrl": "https://lv/abc",
                    "createdAt": 1758000000000,
                }
            ],
            "/posting-api/job-board/ab": {
                "jobs": [
                    {
                        "id": "z",
                        "title": "AI Engineer",
                        "location": "NYC",
                        "isRemote": True,
                        "descriptionPlain": "LLMs",
                        "jobUrl": "https://ab/z",
                    }
                ]
            },
        },
    )
    companies = [
        CompanyBoard(name="GH", ats="greenhouse", token="gh"),
        CompanyBoard(name="LV", ats="lever", token="lv"),
        CompanyBoard(name="AB", ats="ashby", token="ab"),
        CompanyBoard(name="Broken", ats="greenhouse", token="missing"),
    ]
    src = CompanyCareersSource(companies, http, settings)
    jobs = {j.company: j for j in src.fetch(SearchQuery())}
    assert jobs["GH"].description == "Python" and jobs["GH"].work_arrangement == "remote"
    assert jobs["LV"].work_arrangement == "hybrid"
    assert jobs["AB"].work_arrangement == "remote"
    assert "Broken" in src.errors  # one failing board does not abort the others


JSONLD_PAGE = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "JobPosting", "title": "Platform Engineer",
 "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
 "jobLocation": {"@type": "Place", "address": {"addressLocality": "Leeds", "addressCountry": "UK"}},
 "description": "<p>Kubernetes and Go</p>", "datePosted": "2026-09-01",
 "identifier": {"@type": "PropertyValue", "value": "PE-7"}}
</script></head><body>Careers</body></html>"""


def test_careers_page_jsonld_respects_robots(settings: Settings) -> None:
    http = _http(settings, {"/robots.txt": "User-agent: *\nAllow: /", "/jobs": JSONLD_PAGE})
    src = CompanyCareersSource(
        [CompanyBoard(name="Example Corp", ats="careers_page", url="https://ex.com/jobs")],
        http,
        settings,
    )
    [job] = src.fetch(SearchQuery())
    assert job.title == "Platform Engineer" and job.location == "Leeds, UK"

    blocked = _http(settings, {"/robots.txt": "User-agent: *\nDisallow: /", "/jobs": JSONLD_PAGE})
    src = CompanyCareersSource(
        [CompanyBoard(name="Example Corp", ats="careers_page", url="https://ex.com/jobs")],
        blocked,
        settings,
    )
    assert src.fetch(SearchQuery()) == [] and "robots.txt" in src.errors["Example Corp"]


def test_jsonld_graph_and_mapping() -> None:
    page = JSONLD_PAGE.replace('{"@context"', '{"@graph": [{"@context"').replace(
        '"PE-7"}}\n</script>', '"PE-7"}}]}\n</script>'
    )
    [node] = extract_jsonld_jobs(page)
    job = posting_from_jsonld(node, source="careers", url=None)
    assert job.id == "careers:PE-7" and job.description == "Kubernetes and Go"


def _alert_eml(html: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "New jobs for you"
    msg.set_content("plain fallback")
    msg.add_alternative(html, subtype="html")
    return bytes(msg)


def test_linkedin_and_indeed_alert_emails(settings: Settings, tmp_path: Path) -> None:
    linkedin = _alert_eml(
        '<a href="https://www.linkedin.com/comm/jobs/view/3901234567/?trk=x">Senior ML Engineer</a>'
        "<p>Orbit AI · Lisbon, Portugal (Hybrid)</p>"
        '<a href="https://www.linkedin.com/comm/jobs/view/3901234567/?trk=y">View job</a>'
    )
    indeed = _alert_eml(
        '<a href="https://uk.indeed.com/rc/clk?jk=a1b2c3d4e5&amp;from=ja">Data Scientist</a>'
        "<div>Datawell - London</div>"
    )
    [li] = parse_alert_email(linkedin)
    assert li.id == "linkedin:3901234567" and li.company == "Orbit AI"
    assert li.location == "Lisbon, Portugal (Hybrid)" and li.work_arrangement == "hybrid"
    assert li.url == "https://www.linkedin.com/jobs/view/3901234567"
    [ind] = parse_alert_email(indeed)
    assert ind.id == "indeed:a1b2c3d4e5" and ind.company == "Datawell"

    inbox = tmp_path / "inbox"
    (inbox / "postings").mkdir(parents=True)
    (inbox / "li.eml").write_bytes(linkedin)
    (inbox / "postings" / "saved.html").write_text(JSONLD_PAGE.replace('"PE-7"', '"3901234567"'))
    (inbox / "postings" / "pasted.txt").write_text("Some pasted JD")  # no LLM -> reported
    src = InboxSource(inbox, llm=None, settings=settings)
    found = src.fetch(SearchQuery())
    assert {j.source for j in found} == {"linkedin_alert", "manual_saved"}
    assert "pasted.txt" in src.errors


def test_dedupe_prefers_richer_posting() -> None:
    partial = JobPosting(
        id="linkedin:1", title="ML Eng", company="Acme", url="https://www.linkedin.com/jobs/view/1"
    )
    full = partial.model_copy(update={"id": "linkedin_saved:1", "description": "Full text"})
    assert fetcher.dedupe([partial, full]) == [full]


def test_build_sources_skips_unconfigured(settings: Settings) -> None:
    sources, skipped = fetcher.build_sources(["reed", "cv_library", "inbox"], settings=settings)
    assert [s.name for s in sources] == ["inbox"]
    assert set(skipped) == {"reed", "cv_library"}
