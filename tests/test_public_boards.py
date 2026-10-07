"""Public UK boards (LinkedIn, Totaljobs, jobs.ac.uk, NHS Jobs) and closing dates, offline."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from src.core.config import Settings
from src.jobs import fetcher, scorer
from src.jobs.matcher import profile_from_query
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import HttpFetcher, SourceError, posting_from_jsonld
from src.jobs.sources.public_boards import (
    JobsAcUkSource,
    LinkedInSource,
    NHSJobsSource,
    TotaljobsSource,
    annual_salary,
    parse_jobs_ac_uk,
    parse_linkedin_cards,
)


def _card(job_id: int, title: str, company: str = "Acme Bio", day: str = "2026-09-28") -> str:
    return f"""<li>
<div class="base-card relative w-full base-search-card base-search-card--link job-search-card"
  data-entity-urn="urn:li:jobPosting:{job_id}">
  <a class="base-card__full-link" href="https://uk.linkedin.com/jobs/view/x-{job_id}?trk=g">
  <span class="sr-only">{title}</span></a>
  <div class="base-search-card__info">
    <h3 class="base-search-card__title">
      {title}
    </h3>
    <h4 class="base-search-card__subtitle">
      <a class="hidden-nested-link" href="https://uk.linkedin.com/company/acme">{company}</a>
    </h4>
    <div class="base-search-card__metadata">
      <span class="job-search-card__location">Oxford, England, United Kingdom</span>
      <span class="job-search-card__salary-info">£55,000.00 - £65,000.00</span>
      <time class="job-search-card__listdate" datetime="{day}">4 days ago</time>
    </div>
  </div>
</div></li>"""


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_linkedin_cards_are_parsed_with_alert_compatible_ids() -> None:
    jobs = parse_linkedin_cards(_card(4012345678, "Senior Analytical Scientist &amp; Lead"))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == "linkedin:4012345678"  # the id LinkedIn alert emails get: deduped
    assert job.title == "Senior Analytical Scientist & Lead" and job.company == "Acme Bio"
    assert job.location == "Oxford, England, United Kingdom"
    assert (job.salary_min, job.salary_max) == (55000, 65000)
    assert job.posted_at == date(2026, 9, 28)
    assert job.url == "https://www.linkedin.com/jobs/view/4012345678"


def test_linkedin_search_tags_workplace_pages_and_stops_on_refusal(settings: Settings) -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params.get("f_WT") == "3" and params["start"] == "10":
            return httpx.Response(429)  # LinkedIn asks us to slow down
        start = int(params["start"])
        mode = int(params.get("f_WT", "0"))
        count = 10 if start == 0 else 3
        cards = "".join(_card(mode * 1000 + start + i, f"Scientist {i}") for i in range(count))
        return httpx.Response(200, text=cards)

    source = LinkedInSource(HttpFetcher(settings, _client(handler)), settings)
    query = SearchQuery(
        titles=["Analytical Scientist"],
        locations=["Oxford"],
        distance_miles=25,
        posted_within_days=30,
        work_arrangements=["remote", "hybrid"],
    )
    jobs = source.fetch(query)
    first = seen[0]
    assert first["keywords"] == "Analytical Scientist" and first["location"] == "Oxford"
    assert first["distance"] == "25" and first["f_TPR"] == f"r{30 * 86400}"
    remote = [j for j in jobs if j.work_arrangement == "remote"]
    hybrid = [j for j in jobs if j.work_arrangement == "hybrid"]
    assert len(remote) == 13  # page of 10, then a short page ends the term
    assert len(hybrid) == 10  # refused on page 2: what was found is kept
    assert all(j.within_search_area for j in jobs)
    assert "search" in source.errors


def test_linkedin_search_pages_are_capped_to_leave_room_for_postings(settings: Settings) -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["keywords"])
        start = int(request.url.params["start"])
        return httpx.Response(200, text="".join(_card(start + i, "Scientist") for i in range(10)))

    capped = settings.model_copy(update={"linkedin_max_search_requests": 4})
    source = LinkedInSource(HttpFetcher(capped, _client(handler)), capped)
    source.fetch(SearchQuery(titles=["Chemist", "Biologist", "Physicist"]))
    assert len(pages) == 4 and "search" not in source.errors  # a budget, not a refusal


def test_linkedin_waits_when_asked_to_slow_down_then_resumes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.jobs.sources import public_boards

    waits: list[float] = []
    monkeypatch.setattr(public_boards, "pause", waits.append)
    answers = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(429)])

    def handler(request: httpx.Request) -> httpx.Response:
        refused = next(answers, None)
        return refused or httpx.Response(200, text=_card(1, "Chemist"))

    source = LinkedInSource(HttpFetcher(settings, _client(handler)), settings)
    jobs = source.fetch(SearchQuery(titles=["Chemist"]))
    assert [j.id for j in jobs] == ["linkedin:1"] and "search" not in source.errors
    assert waits == [7.0, settings.linkedin_cooldown_s]  # Retry-After honoured, then default


def test_linkedin_first_refusal_fails_the_source(settings: Settings) -> None:
    source = LinkedInSource(HttpFetcher(settings, _client(lambda _: httpx.Response(999))), settings)
    with pytest.raises(SourceError):
        source.fetch(SearchQuery(titles=["Chemist"]))


def test_linkedin_enrich_reads_the_posting_and_respects_the_cap(settings: Settings) -> None:
    posting = """<section class="description">
      <div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5">
        <p>Lead our <strong>HPLC</strong> method development team.</p><p>Hybrid, 2 days.</p>
      </div>
      <ul class="description__job-criteria-list">
        <li class="description__job-criteria-item">
          <h3 class="description__job-criteria-subheader">Seniority level</h3>
          <span class="description__job-criteria-text description__job-criteria-text--criteria">
            Mid-Senior level</span></li>
      </ul></section>"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text=posting)

    capped = settings.model_copy(update={"linkedin_max_details": 1})
    source = LinkedInSource(HttpFetcher(capped, _client(handler)), capped)
    job = parse_linkedin_cards(_card(42, "Principal Scientist"))[0]
    full = source.enrich(job)
    assert calls == ["/jobs-guest/jobs/api/jobPosting/42"]
    assert "Seniority level: Mid-Senior level" in full.description
    assert "HPLC method development" in full.description
    assert full.work_arrangement == "hybrid"  # untagged card: the text says hybrid
    assert source.enrich(job) is job and len(calls) == 1  # cap reached: card kept as is


def test_linkedin_is_searched_by_default_and_gets_the_cv_terms(settings: Settings) -> None:
    from src.services.search_service import TARGETED_SOURCES

    assert "linkedin_search" in fetcher.ALL_SOURCES and "linkedin_search" in TARGETED_SOURCES
    names = [s.name for s in fetcher.build_sources(None, settings=settings)[0]]
    assert {"linkedin_search", "totaljobs", "jobs_ac_uk", "nhs_jobs"} <= set(names)


TOTALJOBS_PAGE = """<html><script>
window.__PRELOADED_STATE__["app-unifiedResultlist"] = %s;
</script></html>"""


def test_totaljobs_reads_the_embedded_result_list(settings: Settings) -> None:
    items = [
        {
            "id": 108066339,
            "title": "Analytical Chemist",
            "url": "/job/analytical-chemist/manpower-uk-job108066339",
            "companyName": "Manpower UK",
            "datePosted": "2026-10-01T07:53:40.593Z",
            "location": "Ginge, Wantage (OX12), OX12 9BY",
            "salary": "Up to £42000 per annum",
            "workFromHome": "",
            "textSnippet": "Analytical chemistry expertise across product development",
        },
        {
            "id": 2,
            "title": "Lab Technician",
            "url": "/job/x/y-job2",
            "salary": "£14 - £16 per hour",
        },
    ]
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /jobs\n")
        paths.append(f"{request.url.path}?{request.url.query.decode()}")
        state = {"searchResults": {"items": items}}
        return httpx.Response(200, text=TOTALJOBS_PAGE % json.dumps(state))

    source = TotaljobsSource(HttpFetcher(settings, _client(handler)), settings)
    jobs = source.fetch(
        SearchQuery(titles=["Analytical Chemist"], locations=["Oxford"], distance_miles=30)
    )
    assert paths == ["/jobs/analytical-chemist/in-oxford?radius=30"]  # page 1 only (robots)
    first = jobs[0]
    assert first.id == "totaljobs:108066339" and first.company == "Manpower UK"
    assert first.salary_max == 42000 and first.within_search_area
    assert first.url == "https://www.totaljobs.com/job/analytical-chemist/manpower-uk-job108066339"
    assert jobs[1].salary_range is None  # an hourly rate is not an annual salary


def test_annual_salary_drops_rates_and_placeholders() -> None:
    assert annual_salary("£49387.00 to £56515.00") == "£49387.00 to £56515.00"
    assert annual_salary("£25k - 30k per year") == "£25k - 30k per year"
    assert annual_salary("£25.76") is None and annual_salary("Competitive") is None
    assert annual_salary("£14.42 per hour") is None


JOBS_AC_UK_CARD = """<div class="j-search-result__result ie-border-left" data-advert-id="%s">
    <div class="j-search-result__text">
        <a href="/job/DTB831/technician-biochemistry-fmi">
            Technician (Biochemistry), FMI
        </a>
        <div class="j-search-result__department">Future Medicines Institute (FMI)</div>
        <div class="j-search-result__employer"><b>Queen&#039;s University Belfast</b></div>
        <div>Location:
            Belfast
        </div>
        <div class="j-search-result__info"><strong>Salary: </strong>
                £33,312 to £38,204 per annum
        </div>
        <div><strong>Date Placed: </strong>28 Sep</div>
    </div>
    <div class="j-search-result__date-logos"><div class="j-search-result__date">
        <span class="j-search-result__date-span j-search-result__date">Closes</span>
        <span class="j-search-result__date-span j-search-result__date--blue ">%s</span>
    </div></div>
</div>"""


def test_jobs_ac_uk_cards_and_closing_dates_across_the_new_year() -> None:
    page = JOBS_AC_UK_CARD % ("1089196", "12 Oct") + JOBS_AC_UK_CARD % ("1089197", "12 Jan")
    jobs = parse_jobs_ac_uk(page, today=date(2026, 10, 2))
    first = jobs[0]
    assert first.id == "jobs_ac_uk:1089196" and first.title == "Technician (Biochemistry), FMI"
    assert first.company == "Queen's University Belfast" and first.location == "Belfast"
    assert first.posted_at == date(2026, 9, 28) and first.closes_at == date(2026, 10, 12)
    assert first.salary_min == 33312 and first.url.endswith(
        "/job/DTB831/technician-biochemistry-fmi"
    )
    assert jobs[1].closes_at == date(2027, 1, 12)  # "12 Jan" seen in October is next year


def test_jobs_ac_uk_pages_and_enriches_from_json_ld(settings: Settings) -> None:
    starts: list[str] = []
    detail = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Technician (Biochemistry), FMI",
        "description": "<p>Compound synthesis, purification and analytical workflows.</p>",
        "datePosted": "2026-09-28",
        "validThrough": "2026-10-12T23:59",
        "hiringOrganization": {"name": "Queen's University Belfast"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /job/feedback/\n")
        if request.url.path == "/search/":
            starts.append(request.url.params["startIndex"])
            count = 25 if request.url.params["startIndex"] == "1" else 1
            ids = range(int(request.url.params["startIndex"]), 100)[:count]
            return httpx.Response(200, text="".join(JOBS_AC_UK_CARD % (i, "12 Oct") for i in ids))
        ld = f'<script type="application/ld+json">{json.dumps(detail)}</script>'
        return httpx.Response(200, text=ld)

    source = JobsAcUkSource(HttpFetcher(settings, _client(handler)), settings)
    jobs = source.fetch(SearchQuery(titles=["Technician"]))
    assert starts == ["1", "26"] and len(jobs) == 26
    full = source.enrich(jobs[0])
    assert "analytical workflows" in full.description and full.closes_at == date(2026, 10, 12)


NHS_XML = """<?xml version='1.0' encoding='UTF-8'?><nhsJobs><totalPages>%d</totalPages>
<totalResults>2</totalResults><vacancyDetails><id>%s</id><reference>C9321-26-%s</reference>
<title>Senior Clinical Scientist</title><description>Immunology service...</description>
<employer>Oxford University Hospitals NHS Foundation Trust</employer><type>Fixed-Term</type>
<salary>£49387.00 to £56515.00</salary><closeDate>2026-10-08</closeDate>
<postDate>2026-09-24T14:47:26.356776913</postDate>
<url>https://beta.jobs.nhs.uk/candidate/jobadvert/C9321-26-%s</url>
<locations><location>Oxford, OX3 7LE</location></locations></vacancyDetails></nhsJobs>"""


def test_nhs_jobs_xml_api_pages_and_dates(settings: Settings) -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/candidate/jobadvert/C9321-26-1":
            return httpx.Response(
                200,
                text='<p id="job_overview"><p>Run the immunology lab.</p></p>'
                '<p id="about_organisation">OUH</p>'
                '<p id="job_description_large"><p>HCPC registration essential.</p></p>'
                '<p id="job_description_large"><p>HCPC registration essential.</p></p>'
                '<div id="contact_details">x</div>',
            )
        page = request.url.params["page"]
        pages.append(page)
        assert request.url.params["location"] == "Oxford"
        assert request.url.params["distance"] == "25"
        return httpx.Response(200, text=NHS_XML % (2, page, page, page))

    source = NHSJobsSource(HttpFetcher(settings, _client(handler)), settings)
    jobs = source.fetch(SearchQuery(titles=["Clinical Scientist"], locations=["Oxford"]))
    assert pages == ["1", "2"] and len(jobs) == 2
    job = jobs[0]
    assert job.id == "nhs_jobs:C9321-26-1" and job.salary_min == 49387
    assert job.closes_at == date(2026, 10, 8) and job.posted_at == date(2026, 9, 24)
    assert job.location == "Oxford, OX3 7LE" and job.within_search_area
    full = source.enrich(job)
    assert "Run the immunology lab." in full.description
    assert full.description.count("HCPC registration essential.") == 1  # mobile copy dropped


def test_closed_postings_are_excluded_and_json_ld_carries_the_closing_date() -> None:
    job = posting_from_jsonld(
        {"title": "Chemist", "validThrough": "2026-09-30T23:59:00Z"}, source="careers", url=None
    )
    assert job.closes_at == date(2026, 9, 30)
    profile = profile_from_query(SearchQuery())
    reasons = scorer.hard_exclusions(profile, job, today=date(2026, 10, 2))
    assert reasons == ["closed: applications closed on 2026-09-30"]
    assert scorer.hard_exclusions(profile, job, today=date(2026, 9, 30)) == []  # last day: open
    open_job = JobPosting(id="x", title="Chemist", company="Acme")
    assert scorer.hard_exclusions(profile, open_job, today=date(2026, 10, 2)) == []


def test_every_selectable_source_has_a_category_and_a_factory(settings: Settings) -> None:
    names = [info.name for info in fetcher.SOURCE_CATALOG]
    assert names == list(fetcher.SELECTABLE_SOURCES) and len(set(names)) == len(names)
    assert {info.category for info in fetcher.SOURCE_CATALOG} == {"job_boards", "company", "alerts"}
    _, skipped = fetcher.build_sources(names, settings=settings)
    assert not any(why == "unknown source" for why in skipped.values())


def test_posting_pages_read_json_ld_only_where_robots_allow(settings: Settings) -> None:
    from src.jobs.sources.pages import PostingPages

    ld = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Senior Scientist",
        "description": "<p>Essential: designing novel algorithms for sequencing data.</p>" * 20,
        "hiringOrganization": {"name": "Acme Bio"},
    }
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        return httpx.Response(200, text=page)

    pages = PostingPages(HttpFetcher(settings, _client(handler), delay_s=0), settings)
    snippet = JobPosting(id="a", title="Senior Scientist", company="Acme Bio",
                         url="https://jobs.example/job/1", description="Short card")  # fmt: skip
    full = pages.enrich(snippet)
    assert "novel algorithms" in full.description and full.title == "Senior Scientist"

    private = snippet.model_copy(update={"url": "https://jobs.example/private/2"})
    with pytest.raises(SourceError, match="robots"):
        pages.enrich(private)
    linkedin = snippet.model_copy(update={"url": "https://www.linkedin.com/jobs/view/3"})
    assert pages.enrich(linkedin) is linkedin  # LinkedIn is opened by its own source


def test_linkedin_alert_jobs_are_opened_through_linkedin(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import search_service

    opened: list[str] = []

    def enrich(self: LinkedInSource, job: JobPosting) -> JobPosting:
        opened.append(job.id)
        return job.model_copy(update={"description": "Full posting text. " * 50})

    monkeypatch.setattr(LinkedInSource, "enrich", enrich)
    alert = JobPosting(id="linkedin:77", title="Scientist", company="Acme", source="linkedin_alert")
    errors: dict[str, str] = {}
    out = search_service._enrich([alert], [], errors, settings)
    assert opened == ["linkedin:77"] and "Full posting text" in out["linkedin:77"].description


def _chrome(settings: Settings, pages: dict[str, str]) -> Any:
    """A BrowserFetcher whose Chrome returns `pages[url]`; robots.txt allows /jobs only."""
    from src.jobs.sources.browser import BrowserFetcher

    robots = HttpFetcher(
        settings,
        _client(lambda _: httpx.Response(200, text="User-agent: *\nDisallow: /private/")),
        delay_s=0,
    )
    opened: list[list[str]] = []

    def run(cmd: list[str], timeout: float) -> str:
        opened.append(cmd)
        return pages[cmd[-1]]

    fetcher = BrowserFetcher(settings, chrome="/chrome", run=run, http=robots)
    return fetcher, opened


def test_chrome_reads_linkedin_about_the_job_signed_out(settings: Settings) -> None:
    view = """<html><main><section class="description">
      <div class="show-more-less-html__markup">
        <p>About the job: you will <strong>design novel algorithms</strong>.</p></div>
      </section></main></html>"""
    url = "https://www.linkedin.com/jobs/view/42"
    fetcher, opened = _chrome(settings, {url: view})
    card = JobPosting(id="linkedin:42", title="Senior Bioinformatics Scientist", company="Acme",
                      url=url)  # fmt: skip
    full = fetcher.enrich(card)
    assert "design novel algorithms" in full.description
    cmd = opened[0]
    assert "--headless=new" in cmd and "--dump-dom" in cmd
    assert any(c.startswith("--user-data-dir=") and "jobsearch-chrome-" in c for c in cmd)


def test_chrome_stops_at_a_challenge_and_keeps_robots_txt(settings: Settings) -> None:
    challenge = "<html><body>Please verify you are human (captcha)</body></html>"
    indeed = "https://uk.indeed.com/viewjob?jk=1"
    fetcher, opened = _chrome(settings.model_copy(update={"browser_host_strikes": 2}),
                              {indeed: challenge})  # fmt: skip
    job = JobPosting(id="indeed:1", title="Scientist", company="Acme", url=indeed)
    for _ in range(2):  # these come and go: a site gets a few chances
        with pytest.raises(SourceError, match="verify a human"):
            fetcher.enrich(job)
    with pytest.raises(SourceError, match="earlier"):  # then it is left for this search
        fetcher.enrich(job)
    assert len(opened) == 2

    private = JobPosting(id="c:1", title="Scientist", company="Acme",
                         url="https://careers.example/private/1")  # fmt: skip
    with pytest.raises(SourceError, match="robots"):
        fetcher.enrich(private)
    assert len(opened) == 2  # never loaded


def test_chrome_reads_a_careers_page_main_text_and_respects_the_cap(settings: Settings) -> None:
    page = "<html><nav>Menu</nav><main><h1>Scientist</h1><p>Requirements: GMP. </p></main></html>"
    url = "https://careers.example/jobs/9"
    capped = settings.model_copy(update={"browser_max_pages": 1})
    fetcher, _ = _chrome(capped, {url: page})
    job = JobPosting(id="c:9", title="Scientist", company="Acme", url=url)
    full = fetcher.enrich(job)
    assert "Requirements: GMP" in full.description and "Menu" not in full.description
    with pytest.raises(SourceError, match="limit"):
        fetcher.enrich(job)


def test_chrome_is_closed_once_the_page_is_printed() -> None:
    """Chrome prints the rendered page, then may keep running while the page stays busy."""
    import sys
    import time

    from src.jobs.sources.browser import _run_chrome

    lingers = [sys.executable, "-c",
               "import time; print('<html><main>About the job</main></html>', flush=True); "
               "time.sleep(60)"]  # fmt: skip
    t0 = time.monotonic()
    assert "About the job" in _run_chrome(lingers, timeout=30)
    assert time.monotonic() - t0 < 10

    silent = [sys.executable, "-c", "import time; time.sleep(60)"]
    with pytest.raises(SourceError, match="in time"):
        _run_chrome(silent, timeout=0.5)


def test_opening_postings_reports_each_one_and_keeps_to_its_time_budget(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from src.services import search_service

    def slow_enrich(self: LinkedInSource, job: JobPosting) -> JobPosting:
        time.sleep(0.3)
        return job.model_copy(update={"description": "Full posting text. " * 50})

    monkeypatch.setattr(LinkedInSource, "enrich", slow_enrich)
    jobs = [JobPosting(id=f"linkedin:{i}", title=f"Scientist {i}", company="Acme",
                       source="linkedin_search") for i in range(5)]  # fmt: skip
    notes: list[str] = []
    budget = settings.model_copy(update={"enrich_budget_s": 0.5})
    out = search_service._enrich(jobs, [], {}, budget, False, notes.append)
    assert notes[0] == "LinkedIn: opening full posting 1/5: Scientist 0 (Acme)"
    assert 1 <= len(out) < 5
    assert any("time budget" in n and "fetch later" in n for n in notes)
    assert notes[-1].startswith(f"Full text read for {len(out)} of 5")
    assert "via LinkedIn" in notes[-1]


def test_linkedin_postings_do_not_hold_other_boards_back(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from src.jobs.sources.pages import PostingPages
    from src.services import search_service

    other_done = threading.Event()

    def linkedin_enrich(self: LinkedInSource, job: JobPosting) -> JobPosting:
        assert other_done.wait(5), "the other lane should finish while LinkedIn is still busy"
        return job.model_copy(update={"description": "LinkedIn text. " * 60})

    def page_enrich(self: PostingPages, job: JobPosting) -> JobPosting:
        other_done.set()
        return job.model_copy(update={"description": "Careers page text. " * 60})

    monkeypatch.setattr(LinkedInSource, "enrich", linkedin_enrich)
    monkeypatch.setattr(PostingPages, "enrich", page_enrich)
    jobs = [
        JobPosting(id="linkedin:1", title="Scientist", company="A", source="linkedin_search"),
        JobPosting(id="c:1", title="Scientist", company="B", url="https://careers.b/1"),
    ]
    out = search_service._enrich(jobs, [], {}, settings)
    assert set(out) == {"linkedin:1", "c:1"}
