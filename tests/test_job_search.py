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
from src.jobs.sources.base import HttpFetcher, SourceError, extract_jsonld_jobs, posting_from_jsonld
from src.jobs.sources.companies import CompanyBoard, CompanyCareersSource
from src.jobs.sources.inbox import InboxSource, parse_alert_email
from src.jobs.sources.job_boards import AdzunaSource, ReedSource
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


def test_country_filter_scopes_sources_and_location_matching(
    settings: Settings, master_cv: MasterCV
) -> None:
    q = SearchQuery(titles=["scientist"], locations=["Oxford"], country="United Kingdom")
    assert q.place_names() == ["Oxford, United Kingdom"]
    assert SearchQuery(country="United Kingdom").place_names() == ["United Kingdom"]

    reed = ReedSource(
        _http(settings, {}), settings.model_copy(update={"reed_api_key": SecretStr("k")})
    )
    with pytest.raises(SourceError, match="UK jobs only"):
        reed.fetch(SearchQuery(titles=["scientist"], country="Germany"))

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"count": 0, "results": []})

    keys = {"adzuna_app_id": SecretStr("id"), "adzuna_app_key": SecretStr("k")}
    adz_settings = settings.model_copy(update=keys)
    http = HttpFetcher(adz_settings, httpx.Client(transport=httpx.MockTransport(handler)))
    AdzunaSource(http, adz_settings).fetch(SearchQuery(titles=["scientist"], country="Germany"))
    assert seen[0].url.path == "/v1/api/jobs/de/search/1"

    whole_uk = build_profile(master_cv, query=SearchQuery(country="United Kingdom"))
    town = JobPosting(id="t", title="Scientist", company="A", location="Abingdon")
    assert scorer.location_match(whole_uk.locations, town) == scorer.LOC_UNKNOWN
    abroad = JobPosting(id="b", title="Scientist", company="A", location="Berlin, Germany")
    assert scorer.location_match(whole_uk.locations, abroad) == 0.0


def test_seniority_is_only_a_hard_filter_when_requested(master_cv: MasterCV) -> None:
    executive = build_profile(master_cv).model_copy(
        update={"seniority_level": 8, "locations": [], "work_arrangements": []}
    )
    junior = JobPosting(id="junior", title="Graduate Lab Technician", company="A")
    chief = JobPosting(id="chief", title="Chief Scientific Officer", company="A")
    assert scorer.hard_exclusions(executive, junior) == []
    minimum = build_profile(master_cv, query=SearchQuery(seniority_min=3))
    assert any("below requested minimum" in r for r in scorer.hard_exclusions(minimum, junior))
    maximum = build_profile(master_cv, query=SearchQuery(seniority_max=7))
    assert any("above requested maximum" in r for r in scorer.hard_exclusions(maximum, chief))
    with pytest.raises(ValidationError, match="Minimum seniority"):
        SearchQuery(seniority_min=7, seniority_max=3)


def test_county_matches_town_and_unplaced_towns_are_kept(master_cv: MasterCV) -> None:
    profile = build_profile(master_cv).model_copy(
        update={"locations": ["Oxford, UK"], "work_arrangements": [], "willing_to_relocate": False}
    )

    def at(where: str) -> JobPosting:
        return JobPosting(id=where, title="Scientist", company="A", location=where)

    assert scorer.location_match(profile.locations, at("Oxfordshire (On-site)")) == 1.0
    assert scorer.hard_exclusions(profile, at("Oxfordshire (On-site)")) == []
    abingdon = at("Abingdon")  # no country, no shared name: distance unknown, not excluded
    assert scorer.location_match(profile.locations, abingdon) == scorer.LOC_UNVERIFIED
    assert not any("location" in r for r in scorer.hard_exclusions(profile, abingdon))
    assert any("location" in r for r in scorer.hard_exclusions(profile, at("Berlin, Germany")))


def test_matcher_pipeline_buckets_every_job(
    master_cv: MasterCV, jobs: list[JobPosting], settings: Settings
) -> None:
    report = JobMatcher(HashingEmbedder(256), settings).match(master_cv, jobs, threshold=70)
    assert [m.job.id for m in report.matches] == ["job-strong"]
    assert {r.job.id for r in report.excluded} == {"job-cert", "job-onsite"}
    assert {r.job.id for r in report.below_threshold} == {"job-partial", "job-junior"}
    total = (
        len(report.matches)
        + len(report.below_threshold)
        + len(report.excluded)
        + len(report.not_retrieved)
    )
    assert total == len(jobs)


def test_retrieval_has_no_cap_unless_configured(
    master_cv: MasterCV, jobs: list[JobPosting], settings: Settings
) -> None:
    report = JobMatcher(HashingEmbedder(256), settings).match(master_cv, jobs, threshold=0)
    assert report.not_retrieved == []  # scoring is local: every eligible posting is scored
    capped = settings.model_copy(update={"retrieval_top_k": 1})
    report = JobMatcher(HashingEmbedder(256), capped).match(master_cv, jobs, threshold=0)
    assert len(report.matches) == 1 and len(report.not_retrieved) == 2


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


def test_blank_keys_and_missing_watchlist_are_skipped_not_failed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("JOBSEARCH_REED_API_KEY=\nJOBSEARCH_CV_LIBRARY_API_KEY=\n", encoding="utf-8")
    monkeypatch.delenv("JOBSEARCH_REED_API_KEY", raising=False)
    monkeypatch.delenv("JOBSEARCH_CV_LIBRARY_API_KEY", raising=False)
    blank = Settings(_env_file=env)  # type: ignore[call-arg]
    assert blank.reed_api_key is None and blank.cv_library_api_key is None

    blank = blank.model_copy(update={"companies_path": settings.companies_path})
    sources, skipped = fetcher.build_sources(["reed", "cv_library", "company"], settings=blank)
    assert set(skipped) == {"reed", "cv_library"}
    assert [s.name for s in sources] == ["company"]  # boards are discovered at search time


def test_adzuna_source_uk_search(settings: Settings) -> None:
    settings = settings.model_copy(
        update={"adzuna_app_id": SecretStr("id"), "adzuna_app_key": SecretStr("secret")}
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.path.rsplit("/", 1)[-1])
        size = int(request.url.params["results_per_page"])
        item = {
            "id": "",
            "title": "Remote ML Engineer",
            "company": {"display_name": "Acme"},
            "location": {"display_name": "London"},
            "description": "<p>Python role</p>",
            "salary_min": 70000,
            "salary_max": 90000,
            "salary_is_predicted": 0,
            "redirect_url": "https://www.adzuna.co.uk/jobs/land/ad/1",
            "created": "2026-09-15T12:00:00Z",
        }
        items = [{**item, "id": f"{page}-{i}"} for i in range(size)]
        return httpx.Response(200, json={"count": 500, "results": items})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    jobs = AdzunaSource(http, settings).fetch(
        SearchQuery(
            titles=["ml engineer"],
            locations=["London"],
            distance_miles=25,
            salary_min=60000,
            limit=60,
            work_arrangements=["remote"],
        )
    )
    assert len(jobs) == 60 and jobs[0].id == "adzuna:1-0" and jobs[-1].id == "adzuna:2-9"
    assert len(seen) == 2  # one search's share (60) = a full page of 50, then a second page
    assert jobs[0].description == "Python role"
    assert jobs[0].salary_min == 70000 and jobs[0].salary_max == 90000
    assert jobs[0].within_search_area and jobs[0].posted_at == date(2026, 9, 15)
    assert seen[0].url.path == "/v1/api/jobs/gb/search/1"
    assert seen[1].url.path == "/v1/api/jobs/gb/search/2"
    assert seen[0].url.params["distance"] == "41"
    assert seen[0].url.params["where"] == "London"
    assert seen[0].url.params["salary_min"] == "60000"


def test_adzuna_error_does_not_expose_key(settings: Settings) -> None:
    settings = settings.model_copy(
        update={"adzuna_app_id": SecretStr("id"), "adzuna_app_key": SecretStr("secret")}
    )
    http = _http(settings, {})
    with pytest.raises(SourceError, match=r"Adzuna API request failed \(HTTP 404\)") as exc:
        AdzunaSource(http, settings).fetch(SearchQuery(titles=["engineer"]))
    assert "secret" not in str(exc.value)

    rejected = HttpFetcher(
        settings, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401)))
    )
    with pytest.raises(SourceError, match="rejected the credentials") as exc:
        AdzunaSource(rejected, settings).fetch(SearchQuery(titles=["engineer"]))
    assert "secret" not in str(exc.value) and "ADZUNA_APP_KEY" in str(exc.value)


def test_adzuna_retries_server_errors_and_skips_a_failing_search(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.jobs.sources import job_boards

    monkeypatch.setattr(job_boards, "ADZUNA_RETRY_S", 0)
    settings = settings.model_copy(
        update={"adzuna_app_id": SecretStr("id"), "adzuna_app_key": SecretStr("secret")}
    )
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        what = request.url.params["what"]
        calls[what] = calls.get(what, 0) + 1
        if what == "down" or (what == "flaky" and calls[what] == 1):
            return httpx.Response(503)
        item = {"id": what, "title": what, "company": {"display_name": "Acme"}}
        return httpx.Response(200, json={"results": [item], "count": 1})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)), delay_s=0)
    jobs = AdzunaSource(http, settings).fetch(SearchQuery(titles=["down", "flaky", "fine"]))
    assert sorted(j.id for j in jobs) == ["adzuna:fine", "adzuna:flaky"]
    assert calls == {"down": 3, "flaky": 2, "fine": 1}  # retried twice, then skipped

    with pytest.raises(SourceError, match=r"HTTP 503") as exc:  # every search failed
        AdzunaSource(http, settings).fetch(SearchQuery(titles=["down"]))
    assert "secret" not in str(exc.value)


def test_requests_stop_once_the_search_is_stopped(settings: Settings) -> None:
    from src.core.llm.calls import call_group, cancel_group, clear_group

    http = _http(settings, {"/x": {"ok": True}})
    token = call_group.set("run-1")
    try:
        assert http.get("https://example.com/x").json() == {"ok": True}
        cancel_group("run-1")
        with pytest.raises(SourceError, match="stopped"):
            http.get("https://example.com/x")
    finally:
        clear_group("run-1")
        call_group.reset(token)


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
    skipped = {j.company for j in src.fetch(SearchQuery(exclude_boards=["lever:lv"]))}
    assert skipped == {"GH", "AB"}  # a board switched off in the UI is not read
    with pytest.raises(SourceError, match="switched off"):
        src.fetch(SearchQuery(exclude_boards=[c.key for c in companies]))


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


def test_indeed_alerts_with_tracking_links_are_parsed_by_layout() -> None:
    track = "https://engage.indeed.com/f/a/Abc~~/AAR9hBA~/x{}"
    digest = _alert_eml(
        f'<a href="{track.format(1)}">\u200b Edit \u200b</a><p>Salaries estimated if unavailable.'
        " When a job posting doesn't include a salary, we estimate it from similar jobs.</p>"
        f'<a href="{track.format(2)}">Principal Scientist, Stem Cells</a>'
        "<p>Cellica Bio\xa0\xa04.1\xa0\xa0- Oxford</p><p>£60,000 - £70,000 a year</p>"
        "<p>Easily apply</p><p>Lead iPSC differentiation programmes and a team of four scientists"
        " across our cell therapy pipeline.</p><p>Just posted</p>"
        f'<a href="{track.format(3)}">unsubscribe</a><p>.</p>'
        '<a href="https://www.linkedin.com/comm/jobs/view/42/">Jobs similar to Scientist at X</a>'
    )
    [job] = parse_alert_email(digest)
    assert (job.title, job.company, job.location) == (
        "Principal Scientist, Stem Cells",
        "Cellica Bio",
        "Oxford",
    )
    assert job.source == "indeed_alert" and job.id.startswith("indeed:")
    assert job.salary_range == "£60,000 - £70,000 a year" and job.salary_min is None
    assert job.description.startswith("Lead iPSC") and job.url == track.format(2)

    match = _alert_eml(
        '<a href="https://cts.indeed.com/v3/H4sI">\u200b View job \u200b</a>'
        '<a href="https://cts.indeed.com/v3/H4sJ">Group Leader, Cell Biology</a>'
        "<p>Oxbridge Therapeutics</p><p>Abingdon</p><p>Salary</p>"
        '<a href="https://cts.indeed.com/v3/H4sK">\u200b Yes \u200b</a>'
        "<p>Keep your Indeed profile up to date</p><p>Some Name</p>"
    )
    [lead] = parse_alert_email(match)
    assert (lead.company, lead.location) == ("Oxbridge Therapeutics", "Abingdon")


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
    assert ind.url == "https://uk.indeed.com/viewjob?jk=a1b2c3d4e5"

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


def test_dedupe_merges_acronym_and_near_identical_full_text() -> None:
    body = "Investigate stem cell differentiation and lead six month research projects. " * 5
    first = JobPosting(
        id="a",
        title="Investigator Scientist",
        company="UKRI",
        location="Oxford",
        description=body,
    )
    renamed = first.model_copy(
        update={
            "id": "b",
            "company": "UK Research and Innovation",
            "description": "Description:\n" + body,
        }
    )
    different = first.model_copy(
        update={
            "id": "c",
            "company": "Another Company",
            "description": body.replace("stem cell", "clinical"),
        }
    )
    assert fetcher.dedupe([first, renamed, different]) == [renamed, different]
    other = renamed.model_copy(update={"company": "Other"})
    assert fetcher.dedupe([first, other]) == [other]
    one_word = other.model_copy(update={"description": body + " Apply now."})
    assert fetcher.dedupe([first, one_word]) == [one_word]


def test_build_sources_skips_unconfigured(settings: Settings) -> None:
    sources, skipped = fetcher.build_sources(
        ["reed", "cv_library", "adzuna", "linkedin", "indeed", "inbox"], settings=settings
    )
    assert [s.name for s in sources] == ["linkedin", "indeed", "inbox"]
    assert set(skipped) == {"reed", "cv_library", "adzuna"}


def test_inbox_source_can_focus_on_one_board(tmp_path: Path, settings: Settings) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "alerts.eml").write_bytes(
        b"Content-Type: text/plain; charset=utf-8\n\n"
        b"LinkedIn role\nhttps://www.linkedin.com/jobs/view/123\n"
        b"Indeed role\nhttps://uk.indeed.com/viewjob?jk=abc123\n"
    )

    linkedin = InboxSource(inbox_dir=inbox, settings=settings, board="linkedin")
    indeed = InboxSource(inbox_dir=inbox, settings=settings, board="indeed")

    assert {job.source for job in linkedin.fetch(SearchQuery())} == {"linkedin_alert"}
    assert {job.source for job in indeed.fetch(SearchQuery())} == {"indeed_alert"}


def test_job_matcher_verdict_is_computed_by_code_from_levels() -> None:
    from src.jobs.models import FIT_WEIGHTS, FitRatings, JobAssessment
    from src.jobs.screener import finalize

    def assess(levels: tuple[int, ...], **extra: Any) -> JobAssessment:
        return JobAssessment(
            job_id="j",
            ratings=FitRatings(**dict(zip(FIT_WEIGHTS, levels, strict=True))),
            fit_summary="test",
            **extra,
        )

    strong = finalize(assess((4, 4, 3, 3, 4, 3)), threshold=60)  # 30+20+15+8+10+8
    assert (strong.fit_score, strong.band, strong.priority, strong.match) == (
        91,
        "exceptional",
        "apply_now",
        True,
    )
    assert strong.cap_reason is None and strong.dimensions.leadership == 8

    # A missing core requirement is never a match, whatever the threshold.
    unmet = finalize(assess((4, 4, 3, 3, 4, 3), essential_unmet=["GMP licence"]), 60)
    assert unmet.fit_score == 55 and "GMP licence" in (unmet.cap_reason or "")
    assert (unmet.band, unmet.priority, unmet.match) == ("weak", "low", False)
    assert not finalize(assess((4, 4, 3, 3, 4, 3), essential_unmet=["GMP licence"]), 40).match

    far_away = finalize(assess((4, 4, 4, 4, 4, 1)), 60)  # practicality level 1: 3/10 < 40%
    assert far_away.fit_score == 65 and far_away.cap_reason == "weak on practicality"

    blocked = finalize(assess((3, 3, 3, 3, 3, 3), dealbreakers=["sales role"]), 60)
    assert blocked.fit_score == 77 and not blocked.match and blocked.priority == "low"

    assert not strong.borderline and not unmet.borderline  # an unmet core is not borderline
    near_miss = finalize(assess((3, 2, 2, 2, 2, 2)), 60)  # 58: just below
    assert near_miss.borderline and not near_miss.match
    assert not blocked.borderline  # a dealbreaker is a clear "no", however it scores

    weak = finalize(assess((1, 2, 2, 1, 1, 1)), 60)
    assert (weak.fit_score, weak.band, weak.match) == (37, "weak", False)
    assert weak.cap_reason is None  # already below the cap: nothing to explain


def test_board_terms_split_composite_roles_and_cover_every_family() -> None:
    from src.tools.search_tools import board_terms

    roles = [
        "Principal Scientist, Cell Therapy / iPSC",
        "Group Leader / Associate Director, Stem Cell Research",
        "Head of iPSC / Cell Therapy Process Development or Research",
        "Scientist",
    ]
    terms = board_terms(roles, ["iPSC", "stem cell"], limit=16)
    assert terms[:4] == [  # first variant of every role family, before any second variant
        "Principal Scientist Cell Therapy",
        "Group Leader Stem Cell Research",
        "Head of iPSC",
        "Scientist",  # a plain one-word title is kept
    ]
    assert terms[4:6] == ["iPSC", "stem cell"]  # broad keywords next
    assert {"Principal Scientist iPSC", "Associate Director Stem Cell Research"} <= set(terms)
    assert "Research" not in terms  # a lone word left over from "or Research" is not a title
    assert len(board_terms(roles * 10, limit=5)) == 5


def test_search_budget_shares_the_limit_across_terms() -> None:
    q = SearchQuery(titles=[f"t{i}" for i in range(16)], limit=200)
    assert q.search_budget() == (20, 320)  # every term gets 20, not the first term all 200
    assert SearchQuery(titles=["a"], limit=200).search_budget() == (200, 200)


def test_biotechnology_jobs_feed_is_parsed_cached_hourly_and_uk_only(settings: Settings) -> None:
    from src.jobs.sources.feeds import BiotechnologyJobsSource

    calls: list[str] = []
    item = {
        "id": "https://biotechnologyjobs.co.uk/jobs/principal-scientist-ipsc-acme",
        "url": "https://biotechnologyjobs.co.uk/jobs/principal-scientist-ipsc-acme",
        "title": "Principal Scientist, iPSC · Acme Bio · Oxford",
        "summary": "Lead iPSC differentiation.",
        "tags": ["permanent", "hybrid", "senior"],
        "_company": "Acme Bio",
        "_location": "Oxford, Oxfordshire",
        "_jobposting": {
            "@type": "JobPosting",
            "title": "Principal Scientist, iPSC",
            "hiringOrganization": {"@type": "Organization", "name": "Acme Bio"},
            "identifier": {"@type": "PropertyValue", "value": "principal-scientist-ipsc-acme"},
            "datePosted": "2026-10-01T13:44:11+00:00",
            "description": "<p>Lead our <b>iPSC</b> team.</p>",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /dashboard\n")
        return httpx.Response(200, json={"version": "1.1", "items": [item]})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    src = BiotechnologyJobsSource(http, settings)
    [job] = src.fetch(SearchQuery(titles=["principal scientist"]))
    assert (job.id, job.title, job.company) == (
        "biotechnologyjobs:principal-scientist-ipsc-acme",
        "Principal Scientist, iPSC",
        "Acme Bio",
    )
    assert job.location == "Oxford, Oxfordshire" and job.work_arrangement == "hybrid"
    assert job.description == "Lead our iPSC team." and job.url == item["url"]
    assert src.fetch(SearchQuery(titles=["data engineer"])) == []  # local title filter

    feed_calls = [c for c in calls if c == "/jobs.json"]
    assert feed_calls == ["/jobs.json"]  # second search served from the hourly cache
    with pytest.raises(SourceError, match="UK jobs only"):
        src.fetch(SearchQuery(country="Germany"))
    assert fetcher.build_sources(["biotechnologyjobs"], settings=settings)[1] == {}


def test_adzuna_gets_the_date_window(settings: Settings) -> None:
    settings = settings.model_copy(
        update={"adzuna_app_id": SecretStr("id"), "adzuna_app_key": SecretStr("secret")}
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"count": 0, "results": []})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    AdzunaSource(http, settings).fetch(SearchQuery(titles=["x"], posted_within_days=3))
    assert seen[0].url.params["max_days_old"] == "3"


def test_alert_postings_are_dated_by_the_email() -> None:
    msg = EmailMessage()
    msg["Subject"] = "Jobs for you"
    msg["Date"] = "Tue, 29 Sep 2026 08:00:00 +0000"
    msg.set_content("ML Engineer\nhttps://www.linkedin.com/jobs/view/3901234567/\n")
    [job] = parse_alert_email(bytes(msg))
    assert job.posted_at == date(2026, 9, 29)


def test_role_words_ignore_words_every_department_uses() -> None:
    from src.tools.search_tools import company_key, role_words, shares_role_words

    targets = [
        "Principal Scientist Cell Therapy",
        "Group Leader Stem Cell Research",
        "Head of iPSC",
    ]
    assert role_words("Group Leader Stem Cell Research") == {"stem", "cell"}
    assert shares_role_words("Scientist, Genetic Assays", targets)
    assert shares_role_words("iPSC Process Development Lead", targets)
    for unrelated in ("Group Leader - Holiday Camp", "HR Business Lead", "Global Credit Controller",
                      "Head, Global Market Access and Pricing"):  # fmt: skip
        assert not shares_role_words(unrelated, targets), unrelated
    assert company_key("Moderna Therapeutics") == company_key("Moderna") == "moderna"
    assert company_key("Oxford Biomedica (UK) Ltd") == "oxford biomedica"
    assert company_key("UKRI") == company_key("UK Research and Innovation")
    assert company_key("GSK") == company_key("GlaxoSmithKline")


def test_retrieval_cap_keeps_role_relevant_titles(master_cv: MasterCV, settings: Settings) -> None:
    settings = settings.model_copy(update={"retrieval_top_k": 1})
    camp = JobPosting(
        id="camp", title="Group Leader - Holiday Camp", company="A", work_arrangement="remote",
        description="Python machine learning PyTorch group leader",
    )  # fmt: skip
    role = JobPosting(
        id="role", title="Machine Learning Engineer", company="B", work_arrangement="remote"
    )
    query = SearchQuery(titles=["Machine Learning Engineer"])
    report = JobMatcher(HashingEmbedder(256), settings).match(
        master_cv, [camp, role], threshold=0, query=query
    )
    retrieved = [r.job.id for r in (*report.matches, *report.below_threshold)]
    assert retrieved == ["role"] and report.not_retrieved == ["camp"]
