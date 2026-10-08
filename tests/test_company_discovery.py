"""Company career feeds (Workable, SmartRecruiters, Recruitee, Personio, Workday, iCIMS) and
discovering them from a company directory (BioPharmGuy). All HTTP is mocked."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from src.core.config import Settings
from src.jobs import fetcher
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import HttpFetcher, country_names, located_in
from src.jobs.sources.companies import (
    CompanyBoard,
    CompanyCareersSource,
    detect_boards,
    load_companies,
    save_companies,
    workday_age_days,
    workday_brand,
    workday_location_facets,
)
from src.jobs.sources.directories import BIOPHARMGUY_UK, parse_biopharmguy
from src.services import company_discovery as cd
from src.services.company_discovery import DiscoveryRecord, discover_companies, merge_boards
from tests.conftest import ENSURE_COMPANY_BOARDS, EXAMPLES

Route = Any  # JSON-able body, text, or a callable(request) -> body


def _http(settings: Settings, routes: dict[str, Route]) -> tuple[HttpFetcher, list[httpx.Request]]:
    """Fetcher whose routes are keyed by "host/path" (or just "/path" for any host)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = routes.get(f"{request.url.host}{request.url.path}", routes.get(request.url.path))
        if callable(body):
            body = body(request)
        if body is None:
            return httpx.Response(404)
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpFetcher(settings, client), seen


# ------------------------------------------------------------------ public ATS feeds

PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?><workzag-jobs><position>
<id>77</id><office>Oxford</office><additionalOffices><office>London</office></additionalOffices>
<name>Bioinformatics Scientist</name><createdAt>2026-09-20T10:00:00+00:00</createdAt>
<jobDescriptions><jobDescription><name>Your role</name><value>&lt;p&gt;Nextflow pipelines&lt;/p&gt;
</value></jobDescription></jobDescriptions></position></workzag-jobs>"""


def test_public_ats_feeds(settings: Settings) -> None:
    http, _ = _http(
        settings,
        {
            "/api/v1/widget/accounts/wk": {
                "jobs": [
                    {"title": "Lab Scientist", "shortcode": "AB1", "city": "Cambridge",
                     "country": "United Kingdom", "telecommuting": False,
                     "description": "<p>qPCR</p>", "url": "https://apply.workable.com/j/AB1",
                     "published_on": "2026-09-01"}
                ]
            },
            "rc.recruitee.com/api/offers/": {
                "offers": [
                    {"id": 5, "title": "Data Scientist", "location": "London, UK",
                     "hybrid": True, "description": "<p>Python</p>", "requirements": "<p>SQL</p>",
                     "careers_url": "https://rc.recruitee.com/o/ds"}
                ]
            },
            "ps.jobs.personio.de/xml": PERSONIO_XML,
            "/v1/companies/SR/postings": {"content": [{"id": "9", "name": "Data Engineer"}]},
            "/v1/companies/SR/postings/9": {
                "name": "Data Engineer", "postingUrl": "https://jobs.smartrecruiters.com/SR/9",
                "location": {"fullLocation": "Leeds, UK", "remote": True},
                "jobAd": {"sections": {"jobDescription": {"text": "<p>Spark</p>"}}},
                "releasedDate": "2026-09-02T10:00:00Z",
            },
        },
    )  # fmt: skip
    companies = [
        CompanyBoard(name="WK", ats="workable", token="wk"),
        CompanyBoard(name="RC", ats="recruitee", token="rc"),
        CompanyBoard(name="PS", ats="personio", token="ps"),
        CompanyBoard(name="SR", ats="smartrecruiters", token="SR"),
    ]
    src = CompanyCareersSource(companies, http, settings)
    jobs = {j.company: j for j in src.fetch(SearchQuery())}
    assert src.errors == {}
    assert jobs["WK"].location == "Cambridge, United Kingdom" and jobs["WK"].description == "qPCR"
    assert jobs["RC"].work_arrangement == "hybrid" and "SQL" in jobs["RC"].description
    assert jobs["PS"].location == "Oxford, London" and "Nextflow" in jobs["PS"].description
    assert jobs["PS"].url == "https://ps.jobs.personio.de/job/77"
    assert jobs["SR"].work_arrangement == "remote" and jobs["SR"].description == "Spark"


def test_smartrecruiters_filters_country_server_side(settings: Settings) -> None:
    http, seen = _http(settings, {"/v1/companies/SR/postings": {"content": []}})
    src = CompanyCareersSource([], http, settings)
    board = CompanyBoard(name="SR", ats="smartrecruiters", token="SR")
    src.fetch_board(board, SearchQuery(titles=["Scientist"], country="United Kingdom"))
    assert seen[0].url.params["country"] == "gb" and seen[0].url.params["q"] == "Scientist"


def test_one_board_shared_by_two_entries_is_read_once(settings: Settings) -> None:
    http, seen = _http(settings, {"/api/v1/widget/accounts/wk": {"jobs": []}})
    twice = [
        CompanyBoard(name="GSK", ats="workable", token="wk"),
        CompanyBoard(name="ViiV", ats="workable", token="WK"),
    ]
    CompanyCareersSource(twice, http, settings).fetch(SearchQuery())
    assert len(seen) == 1


# ------------------------------------------------------------------ Workday

WD = "acme.wd3.myworkdayjobs.com"
WD_API = f"{WD}/wday/cxs/acme/Careers"
WD_FACETS = [
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "UK - Cambridge", "id": "uk1", "count": 3},
            {"descriptor": "Cambridge MA – Binney Street", "id": "us1", "count": 9},
            {"descriptor": "London - England", "id": "uk2", "count": 1},
        ]},
    ]},
    {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "UK Sales", "id": "x"}]},
]  # fmt: skip


def test_workday_location_facets_match_labels_not_names() -> None:
    uk = ["united kingdom", "uk", "england"]
    assert workday_location_facets(WD_FACETS, uk) == {"locations": ["uk1", "uk2"]}
    country = [
        {"facetParameter": "Location_Country", "values": [{"descriptor": "France", "id": "f"}]}
    ]
    assert workday_location_facets(country, uk) is None  # no UK jobs on this board at all
    assert workday_location_facets([], uk) == {}  # no location facet: search unfiltered


def test_a_place_named_like_the_country_elsewhere_is_not_in_it() -> None:
    uk = country_names("United Kingdom")
    msd = [{"facetParameter": "locations", "values": [
        {"descriptor": "USA - Pennsylvania - North Wales (Upper Gwynedd)", "id": "us", "count": 90},
        {"descriptor": "GBR - London", "id": "gb1", "count": 4},
        {"descriptor": "Belfast, Northern Ireland", "id": "gb2", "count": 1},
        {"descriptor": "Dublin, Ireland", "id": "ie", "count": 2},
    ]}]  # fmt: skip
    assert workday_location_facets(msd, uk) == {"locations": ["gb1", "gb2"]}
    in_uk = located_in(uk)
    assert in_uk("Cardiff, Wales") and not in_uk("USA - Pennsylvania - North Wales")


def test_workday_lists_filters_and_opens_matching_titles(settings: Settings) -> None:
    bodies: list[dict[str, Any]] = []

    def jobs(request: httpx.Request) -> dict[str, Any]:
        body = json.loads(request.content)
        bodies.append(body)
        if body["limit"] == 1:
            return {"total": 2, "jobPostings": [], "facets": WD_FACETS}
        return {
            "total": 2,
            "jobPostings": [
                {"title": "Senior Scientist, Biologics", "externalPath": "/job/UK/Senior_R1"},
                {"title": "Sales Representative", "externalPath": "/job/UK/Sales_R2"},
            ],
        }

    detail = {
        "jobPostingInfo": {
            "id": "abc", "jobReqId": "R1", "title": "Senior Scientist, Biologics",
            "jobDescription": "<p>Antibody engineering</p>", "location": "USA - Boston",
            "additionalLocations": ["UK - Cambridge"], "remoteType": "Hybrid (Remote & On-site)",
            "startDate": "2026-09-30", "externalUrl": f"https://{WD}/Careers/job/UK/Senior_R1",
        }
    }  # fmt: skip
    http, seen = _http(
        settings,
        {"/robots.txt": "User-agent: *\nAllow: /", f"{WD_API}/jobs": jobs,
         f"{WD_API}/job/UK/Senior_R1": detail},
    )  # fmt: skip
    board = CompanyBoard(name="Acme", ats="workday", url=f"https://{WD}/en-US/Careers")
    query = SearchQuery(titles=["Scientist"], country="United Kingdom")
    [job] = CompanyCareersSource([], http, settings).fetch_board(board, query)
    assert bodies[1]["appliedFacets"] == {"locations": ["uk1", "uk2"]}
    assert bodies[1]["searchText"] == "Scientist"
    assert job.id == "workday:acme:R1" and job.location == "UK - Cambridge"
    assert job.company == "Acme"  # no brand logo: the board's own company
    assert job.work_arrangement == "hybrid" and job.description == "Antibody engineering"
    assert not any("Sales_R2" in str(r.url) for r in seen)  # off-target titles never opened


def test_workday_respects_robots(settings: Settings) -> None:
    http, _ = _http(settings, {"/robots.txt": "User-agent: *\nDisallow: /wday/"})
    src = CompanyCareersSource(
        [CompanyBoard(name="Acme", ats="workday", url=f"https://{WD}/Careers")], http, settings
    )
    assert src.fetch(SearchQuery()) == [] and "robots.txt" in src.errors["Acme"]


# ------------------------------------------------------------------ iCIMS

ICIMS_LIST = """<a href="https://careers-acme.icims.com/jobs/12/assay-scientist/job?in_iframe=1"
 class="iCIMS_Anchor" title="12 - Assay Scientist"><h3>Assay Scientist</h3></a>
<a href="https://careers-acme.icims.com/jobs/13/nurse/job?in_iframe=1"
 class="iCIMS_Anchor" title="13 - Research Nurse"><h3>Research Nurse</h3></a>"""
ICIMS_JOB = """<script type="application/ld+json">{"@type": "JobPosting",
 "title": "Assay Scientist", "description": "<p>ELISA</p>", "identifier": "12",
 "jobLocation": {"address": {"addressLocality": "London", "addressCountry": "GB"}}}</script>"""


def test_icims_lists_then_reads_jsonld_of_matching_jobs(settings: Settings) -> None:
    pages = iter([ICIMS_LIST, ICIMS_LIST])  # page 2 repeats page 1: the list has ended
    http, seen = _http(
        settings,
        {"/robots.txt": "User-agent: *\nDisallow: /jobs/login",
         "/jobs/search": lambda _: next(pages), "/jobs/12/assay-scientist/job": ICIMS_JOB},
    )  # fmt: skip
    board = CompanyBoard(name="Acme Bio", ats="icims", url="https://careers-acme.icims.com/jobs")
    [job] = CompanyCareersSource([], http, settings).fetch_board(
        board, SearchQuery(titles=["Scientist"])
    )
    assert (job.company, job.title, job.location) == ("Acme Bio", "Assay Scientist", "London, GB")
    assert job.url == "https://careers-acme.icims.com/jobs/12/assay-scientist/job"
    assert not any("/jobs/13/" in str(r.url) for r in seen)


# ------------------------------------------------------------------ detection


def test_detect_boards_from_links_and_embeds() -> None:
    page = """
    <a href="https://boards.greenhouse.io/embed/job_board?for=compass&amp;b=x">Jobs</a>
    <a href="https://jobs.lever.co/acme/123">Apply</a>
    <a href="https://apply.workable.com/j/ABC123">one job</a>
    <a href="https://apply.workable.com/brainomix/">All jobs</a>
    <a href="https://gsk.wd5.myworkdayjobs.com/en-US/GSKCareers/job/x">Job</a>
    <a href="https://careers-hvivo.icims.com/jobs/intro">iCIMS</a>
    <a href="https://www.icims.com/">vendor</a>"""
    found = {b.key for b in detect_boards(page, "Acme")}
    assert found == {
        "greenhouse:compass", "lever:acme", "workable:brainomix",
        "workday:gsk/gskcareers", "icims:careers-hvivo.icims.com",
    }  # fmt: skip


BPG_PAGE = """<table><tr class="sponsor">
<td class="company"><a href="https://ingenucro.com/"><img alt="iNGENū CRO"> </a></td>
<td class="location">UK - London</td><td class="description">CRO</td>
</tr><tr class="even">
<td class="company"><a href="/company.php/Oncimmune" class="company"><img alt="Add'l"></a><a
 href="https://www.oncimmune.com/" target="_blank">Oncimmune&nbsp;</a></td>
<td class="location">UK - Leeds</td><td class="description">Cancer diagnostics</td>
</tr><tr class="odd">
<td class="company"><a href="https://oncimmune.com/">Oncimmune </a></td>
<td class="location">UK - Nottingham</td><td class="description">Cancer diagnostics</td></tr>
</table>"""


def test_parse_biopharmguy_merges_locations_per_website() -> None:
    sponsor, onc = parse_biopharmguy(BPG_PAGE)
    assert (sponsor.name, sponsor.website) == ("iNGENū CRO", "https://ingenucro.com/")
    assert onc.name == "Oncimmune" and onc.locations == ["UK - Leeds", "UK - Nottingham"]


# ------------------------------------------------------------------ discovery


def test_merge_keeps_hand_added_and_one_entry_per_board_and_company() -> None:
    mine = CompanyBoard(name="Oxbio", ats="lever", token="oxbio")
    old = CompanyBoard(name="Gone", ats="lever", token="gone", origin="bpg")
    found = [
        CompanyBoard(name="Oxbio", ats="greenhouse", token="ox2", origin="bpg"),  # hand-added wins
        CompanyBoard(name="GSK", ats="workday", url=f"https://{WD}/C", origin="bpg"),
        CompanyBoard(name="ViiV", ats="workday", url=f"https://{WD}/C", origin="bpg"),  # same board
        CompanyBoard(name="Foo", ats="careers_page", url="https://foo.com/careers", origin="bpg"),
    ]
    page_too = CompanyBoard(name="GSK", ats="careers_page", url="https://gsk.com/careers")
    merged = merge_boards([mine, old, page_too], found, "bpg")
    assert [(b.name, b.ats) for b in merged] == [
        ("Oxbio", "lever"), ("GSK", "workday"), ("Foo", "careers_page"),
    ]  # fmt: skip


def _site_routes() -> dict[str, Route]:
    return {
        "/robots.txt": "User-agent: *\nAllow: /",
        "biopharmguy.com/links/country-united-kingdom-all-location.php": BPG_PAGE,
        # Oncimmune: homepage -> careers page -> embedded Greenhouse board that answers
        "www.oncimmune.com/": '<a href="/about/careers">Careers</a><a href="/news">News</a>',
        "www.oncimmune.com/about/careers": '<script src="https://boards.greenhouse.io/embed/'
        'job_board/js?for=oncimmune"></script>',
        "boards-api.greenhouse.io/v1/boards/oncimmune/jobs": {"jobs": []},
        # iNGENū: no careers link anywhere
        "ingenucro.com/": "<a href='/contact'>Contact</a>",
    }


def _found(settings: Settings) -> list[CompanyBoard]:
    """Watch-list entries discovery found (the hand-verified employers left out)."""
    boards = load_companies(settings.companies_path)
    return [b for b in boards if b.origin != cd.KNOWN_ORIGIN]


def test_discover_companies_writes_watch_list_and_caches(settings: Settings) -> None:
    assert cd.needs_discovery(settings)  # never run: the directory was never read
    http, seen = _http(settings, _site_routes())
    report = discover_companies(settings, "biopharmguy-uk", mode="new", http=http, workers=2)
    assert (report.companies, report.checked, report.without_feed) == (2, 2, 1)
    assert report.boards == {"greenhouse": 1}
    [board] = _found(settings)
    assert (board.name, board.key, board.origin) == ("Oncimmune", "greenhouse:oncimmune",
                                                     "biopharmguy-uk")  # fmt: skip
    assert len(load_companies(settings.companies_path)) == 1 + len(cd.known_boards())
    assert any(str(r.url) == BIOPHARMGUY_UK for r in seen)
    assert not cd.needs_discovery(settings)
    status = cd.status(settings)
    assert (status.companies, status.checked, status.boards, status.running) == (2, 2, 1, False)

    seen.clear()  # a second pass needs no request: the listing and every company are cached
    again = discover_companies(settings, "biopharmguy-uk", http=http, workers=2)
    assert again.checked == 0 and len(_found(settings)) == 1
    assert seen == []


def test_stale_results_are_rechecked_only_when_asked(settings: Settings) -> None:
    old = date.today() - timedelta(days=cd.MAX_AGE_DAYS + 1)
    companies = parse_biopharmguy(BPG_PAGE)
    cache = cd.DiscoveryCache(
        directories={"biopharmguy-uk": cd.DirectoryListing(read_on=old, companies=companies)},
        records=[
            DiscoveryRecord(name="Oncimmune", website="https://www.oncimmune.com/",
                            origin="biopharmguy-uk", checked_on=old),
            DiscoveryRecord(name="iNGENū CRO", website="https://ingenucro.com/",
                            origin="biopharmguy-uk", checked_on=date.today()),
        ],
    )  # fmt: skip
    cd.discovery_path(settings).write_text(cache.model_dump_json())
    assert cd.status(settings).stale == 1 and not cd.needs_discovery(settings)
    http, _ = _http(settings, _site_routes())
    assert discover_companies(settings, mode="new", http=http, workers=1).checked == 0
    assert discover_companies(settings, mode="stale", http=http, workers=1).checked == 1
    assert discover_companies(settings, mode="all", http=http, workers=1).checked == 2


def test_a_stopped_pass_resumes_with_the_unchecked_companies(settings: Settings) -> None:
    http, _ = _http(settings, _site_routes())
    calls = iter([False, True])  # stop after the first company
    first = discover_companies(
        settings, http=http, workers=1, should_stop=lambda: next(calls, True)
    )
    assert first.checked == 2 and cd.status(settings).unchecked == 1
    assert cd.needs_discovery(settings)
    assert discover_companies(settings, mode="new", http=http, workers=1).checked == 1
    assert not cd.needs_discovery(settings)


def test_search_runs_discovery_only_when_companies_are_unchecked(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.workspace import Workspace

    passes: list[str] = []

    def fake(settings: Settings, **kw: Any) -> cd.DiscoveryReport:
        passes.append(kw["mode"])
        kw["progress"]("checked 1/1 companies, 1 job boards found")
        save_companies(settings.companies_path, [CompanyBoard(name="A", ats="lever", token="a")])
        return cd.DiscoveryReport(companies=1, checked=1, boards={"lever": 1}, without_feed=0,
                                  watch_list=1)  # fmt: skip

    monkeypatch.setattr(cd, "discover_companies", fake)
    events: list[str] = []

    async def emit(_: str, data: dict[str, Any]) -> None:
        events.append(data["message"])

    ws = Workspace(settings)
    asyncio.run(ENSURE_COMPANY_BOARDS(ws, emit))
    assert passes == ["new"] and "1 job boards ready" in events[-1]
    assert any("checked 1/1" in e for e in events)

    monkeypatch.setattr(cd, "needs_discovery", lambda _: False)
    asyncio.run(ENSURE_COMPANY_BOARDS(ws, emit))
    assert passes == ["new"]  # nothing unchecked: the search goes straight to capture


def test_company_source_builds_before_discovery_and_says_why_it_is_empty(
    settings: Settings,
) -> None:
    [src], skipped = fetcher.build_sources(["company"], settings=settings)
    assert skipped == {}  # the toggle stays available before the first pass
    _, errors = fetcher.fetch_all([src], SearchQuery())
    assert "No company job boards found yet" in errors["company"]


def test_known_big_pharma_boards_skip_crawling(settings: Settings) -> None:
    http, seen = _http(settings, {})
    src = CompanyCareersSource([], http, settings)
    gsk = cd.DirectoryCompany(name="GSK", website="https://www.gsk.com/en-gb/")
    board, note = cd.discover_board(src, gsk, "biopharmguy-uk")
    assert board is not None and board.key == "workday:gsk/gskcareers" and note == "known"
    assert seen == []


def test_discovery_falls_back_to_jsonld_and_respects_robots(settings: Settings) -> None:
    one = '<script type="application/ld+json">{"@type": "JobPosting", "title": "x"}</script>'
    jsonld = one + one
    http, _ = _http(
        settings, {"/robots.txt": "User-agent: *\nAllow: /", "a.com/": jsonld + "<p>Hi</p>"}
    )
    src = CompanyCareersSource([], http, settings)
    board, _ = cd.discover_board(src, cd.DirectoryCompany(name="A", website="https://a.com/"), "d")
    assert board is not None and board.ats == "careers_page" and board.url == "https://a.com/"

    single, _ = _http(settings, {"/robots.txt": "User-agent: *\nAllow: /", "a.com/": one})
    src = CompanyCareersSource([], single, settings)
    board, _ = cd.discover_board(src, cd.DirectoryCompany(name="A", website="https://a.com/"), "d")
    assert board is None  # a single job's page is not a careers list

    blocked, _ = _http(settings, {"/robots.txt": "User-agent: *\nDisallow: /", "a.com/": jsonld})
    src = CompanyCareersSource([], blocked, settings)
    board, note = cd.discover_board(
        src, cd.DirectoryCompany(name="A", website="https://a.com/"), "d"
    )
    assert board is None and "robots.txt" in note


def test_example_watch_list_is_valid() -> None:
    boards = load_companies(EXAMPLES / "companies.example.json")
    assert {b.ats for b in boards} >= {"greenhouse", "workday", "icims", "careers_page"}


# ------------------------------------------------------------------ Teamtailor · Lever EU

TT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel><item>
<title>Senior Study Manager</title><description>&lt;p&gt;Immunology studies&lt;/p&gt;</description>
<pubDate>Thu, 01 Oct 2026 13:27:58 +0100</pubDate>
<link>https://roukenbio.teamtailor.com/jobs/8169685-senior-study-manager</link>
<remoteStatus>hybrid</remoteStatus><guid>1ab3</guid>
<tt:locations><tt:location><tt:city>Motherwell</tt:city><tt:country>United Kingdom</tt:country>
</tt:location></tt:locations></item></channel></rss>"""


def test_teamtailor_rss_and_lever_eu(settings: Settings) -> None:
    http, seen = _http(
        settings,
        {
            "/robots.txt": "User-agent: *\nDisallow: /app/",
            "roukenbio.teamtailor.com/jobs.rss": TT_RSS,
            "api.eu.lever.co/v0/postings/oni": [
                {"id": "x", "text": "Imaging Scientist", "categories": {"location": "Oxford"}}
            ],
        },
    )
    boards = [
        CompanyBoard(name="Rouken", ats="teamtailor", url="https://roukenbio.teamtailor.com/jobs"),
        CompanyBoard(name="ONI", ats="lever_eu", token="oni"),
    ]
    src = CompanyCareersSource(boards, http, settings)
    jobs = {j.company: j for j in src.fetch(SearchQuery())}
    assert src.errors == {}
    tt = jobs["Rouken"]
    assert (tt.id, tt.location, tt.work_arrangement) == (
        "teamtailor:roukenbio.teamtailor.com:8169685", "Motherwell, United Kingdom", "hybrid",
    )  # fmt: skip
    assert tt.posted_at == date(2026, 10, 1) and tt.description == "Immunology studies"
    assert jobs["ONI"].title == "Imaging Scientist"
    assert any(r.url.host == "api.eu.lever.co" for r in seen)


def test_detects_teamtailor_on_own_domain_and_lever_eu() -> None:
    page = """<link href="https://assets-aws.teamtailor-cdn.com/x.css">
    <a href="https://jobs.eu.lever.co/oni/72b4">Apply</a>
    <a href="https://www.teamtailor.com/">Powered by Teamtailor</a>"""
    found = {b.key for b in detect_boards(page, "B", page_url="https://careers.bionical.com/jobs")}
    assert found == {"teamtailor:careers.bionical.com", "lever_eu:oni"}


# ------------------------------------------------------------------ date posted


def test_workday_age_labels() -> None:
    labels = ["Posted Today", "Posted Yesterday", "Posted 3 Days Ago", "Posted 30+ Days Ago", None]
    assert [workday_age_days(x) for x in labels] == [0, 1, 3, 31, 0]


def test_workday_skips_old_postings_before_opening_them(settings: Settings) -> None:
    listing = {
        "total": 2,
        "jobPostings": [
            {"title": "Scientist", "externalPath": "/job/new_R1", "postedOn": "Posted 2 Days Ago"},
            {
                "title": "Scientist",
                "externalPath": "/job/old_R2",
                "postedOn": "Posted 30+ Days Ago",
            },
        ],
    }
    detail = {"jobPostingInfo": {"id": "1", "title": "Scientist", "startDate": "2026-09-29"}}
    http, seen = _http(
        settings,
        {"/robots.txt": "", f"{WD_API}/jobs": listing, f"{WD_API}/job/new_R1": detail},
    )
    board = CompanyBoard(name="Acme", ats="workday", url=f"https://{WD}/Careers")
    query = SearchQuery(titles=["Scientist"], posted_within_days=7)
    assert len(CompanyCareersSource([], http, settings).fetch_board(board, query)) == 1
    assert not any("old_R2" in str(r.url) for r in seen)


def test_posted_within_days_filters_every_source_but_keeps_undated() -> None:
    today = date.today()

    class Fixed:
        name = "fixed"

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            return [
                JobPosting(id=f"j{age}", title=f"Scientist {age}", company="A",
                           posted_at=None if age is None else today - timedelta(days=age))
                for age in (0, 1, 5, 40, None)
            ]  # fmt: skip

    def ids(days: int | None) -> list[str]:
        jobs, _ = fetcher.fetch_all([Fixed()], SearchQuery(posted_within_days=days))
        return [j.id for j in jobs]

    assert ids(None) == ["j0", "j1", "j5", "j40", "jNone"]
    assert ids(1) == ["j0", "j1", "jNone"]  # "last 24 hours" = today or yesterday
    assert ids(7) == ["j0", "j1", "j5", "jNone"]


def test_workday_never_opens_off_target_roles(settings: Settings) -> None:
    listing = {
        "total": 3,
        "jobPostings": [
            {"title": "Global Credit Controller", "externalPath": "/job/credit_R1"},
            {"title": "Scientist, Genetic Assays", "externalPath": "/job/sci_R2"},
            {"title": "HR Business Lead", "externalPath": "/job/hr_R3"},
        ],
    }
    detail = {"jobPostingInfo": {"id": "2", "title": "Scientist, Genetic Assays"}}
    http, seen = _http(
        settings, {"/robots.txt": "", f"{WD_API}/jobs": listing, f"{WD_API}/job/sci_R2": detail}
    )
    board = CompanyBoard(name="Acme", ats="workday", url=f"https://{WD}/Careers")
    query = SearchQuery(titles=["Principal Scientist Cell Therapy", "Group Leader Stem Cell"])
    jobs = CompanyCareersSource([board], http, settings).fetch(query)
    assert [j.title for j in jobs] == ["Scientist, Genetic Assays"]
    assert not any("credit" in str(r.url) or "hr_R3" in str(r.url) for r in seen)


# ------------------------------------------------------------------ BambooHR · Pinpoint

ROBOTS_OK = "User-agent: *\nAllow: /"


def test_bamboohr_and_pinpoint_feeds(settings: Settings) -> None:
    http, seen = _http(
        settings,
        {
            "/robots.txt": ROBOTS_OK,
            "acme.bamboohr.com/careers/list": {
                "result": [
                    {"id": "12", "jobOpeningName": "Principal Scientist"},
                    {"id": "13", "jobOpeningName": "Finance Manager"},
                ]
            },
            "acme.bamboohr.com/careers/12/detail": {
                "result": {
                    "jobOpening": {
                        "jobOpeningName": "Principal Scientist",
                        "description": "<p>iPSC differentiation</p>",
                        "datePosted": "2026-09-28",
                        # as live boards send it: atsLocation all null, the place in location
                        "location": {
                            "city": "Cambridge",
                            "state": "Cambridgeshire",
                            "addressCountry": "United Kingdom",
                        },
                        "atsLocation": {"country": None, "state": None, "city": None},
                        "isRemote": False,
                        "jobOpeningShareUrl": "https://acme.bamboohr.com/careers/12",
                    }
                }
            },
            "beta.pinpointhq.com/postings.json": {
                "data": [
                    {
                        "id": 7,
                        "title": "Lab Scientist",
                        "description": "<p>Cell culture</p>",
                        "url": "https://beta.pinpointhq.com/postings/7",
                        "location": {"name": "Oxford"},
                        "workplace_type": "hybrid",
                        "deadline_at": "2026-11-01",
                    }
                ]
            },
        },
    )
    src = CompanyCareersSource([], http, settings)
    query = SearchQuery(titles=["Principal Scientist"])
    [job] = src.fetch_board(CompanyBoard(name="Acme", ats="bamboohr", token="acme"), query)
    assert (job.id, job.location, job.description) == (
        "bamboohr:acme:12",
        "Cambridge, Cambridgeshire, United Kingdom",
        "iPSC differentiation",
    )
    assert job.posted_at == date(2026, 9, 28)
    assert not any("/careers/13/" in str(r.url) for r in seen)  # off-target titles never opened

    [lab] = src.fetch_board(CompanyBoard(name="Beta", ats="pinpoint", token="beta"), query)
    assert (lab.work_arrangement, lab.location, lab.closes_at) == (
        "hybrid",
        "Oxford",
        date(2026, 11, 1),
    )
    page = '<a href="https://acme.bamboohr.com/careers">Jobs</a> <iframe src="https://beta.pinpointhq.com/"></iframe>'
    assert {b.key for b in detect_boards(page, "X")} == {"bamboohr:acme", "pinpoint:beta"}
    assert detect_boards("https://resources.bamboohr.com/guide", "X") == []


def test_discovery_tries_usual_paths_and_loosely_written_links(settings: Settings) -> None:
    feed = {"result": []}
    http, _ = _http(
        settings,
        {
            "/robots.txt": ROBOTS_OK,
            # no careers link on the homepage, but /careers exists and embeds BambooHR
            "a.com/": "<a href='/about'>About</a>",
            "a.com/careers": '<script src="https://acme.bamboohr.com/js/embed.js"></script>',
            "acme.bamboohr.com/careers/list": feed,
            # an unquoted link with a fragment, to a page using Pinpoint
            "b.com/": "<a href=/work-with-us#top>Join us</a>",
            "b.com/work-with-us": "<a href='https://beta.pinpointhq.com'>Openings</a>",
            "beta.pinpointhq.com/postings.json": {"data": []},
            # nothing anywhere: no careers page at all
            "c.com/": "<a href='/about'>About</a>",
        },
    )
    src = CompanyCareersSource([], http, settings)

    def find(site: str) -> tuple[CompanyBoard | None, str]:
        return cd.discover_board(
            src, cd.DirectoryCompany(name=site, website=f"https://{site}/"), "d"
        )

    assert find("a.com")[0] == CompanyBoard(name="a.com", ats="bamboohr", token="acme", origin="d")
    assert find("b.com")[0] == CompanyBoard(name="b.com", ats="pinpoint", token="beta", origin="d")
    assert find("c.com") == (None, "no careers link on the homepage")


# ------------------------------------------------------------------ the user's own companies


def test_your_companies_are_added_kept_over_discovery_and_removed(settings: Settings) -> None:
    http, _ = _http(
        settings,
        {
            "/robots.txt": ROBOTS_OK,
            "boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": []},
            "foo.com/": '<a href="/careers">Careers</a>',
            "foo.com/careers": "<p>Email us your CV</p>",
            "beta.com/": "<a href='https://beta.pinpointhq.com'>Openings</a>",
            "beta.pinpointhq.com/postings.json": {"data": []},
        },
    )
    acme = cd.add_company(settings, "Acme", "https://boards.greenhouse.io/acme", http)
    assert (acme.ats, acme.token, acme.origin) == ("greenhouse", "acme", None)  # the link itself
    beta = cd.add_company(settings, " Beta ", "beta.com", http)  # no scheme: https assumed
    assert (beta.name, beta.ats) == ("Beta", "pinpoint")
    with pytest.raises(ValueError, match="No readable job list found for Foo"):
        cd.add_company(settings, "Foo", "https://foo.com/", http)
    with pytest.raises(ValueError, match="name and its website"):
        cd.add_company(settings, "", "https://foo.com/", http)
    assert [c.name for c in cd.your_companies(settings)] == ["Acme", "Beta"]

    # Discovery finds Beta on another board: the user's entry stays the only one
    found = CompanyBoard(name="Beta", ats="greenhouse", token="beta2", origin="d")
    merged = merge_boards(load_companies(settings.companies_path), [found], "d")
    assert [(b.name, b.ats) for b in merged] == [("Acme", "greenhouse"), ("Beta", "pinpoint")]

    assert cd.remove_company(settings, "beta") and not cd.remove_company(settings, "Beta")
    assert [c.name for c in cd.your_companies(settings)] == ["Acme"]


def test_workday_names_the_brand_on_a_group_board() -> None:
    def info(alt: str | None) -> dict[str, Any]:
        return {"logoImage": {"alt": alt}} if alt is not None else {}

    assert workday_brand(info("Abcam Logo"), "Danaher") == "Abcam"  # Danaher's shared board
    assert workday_brand(info("Worldwide Clinical Trials"), "WCT") == "Worldwide Clinical Trials"
    assert workday_brand(info("BED Logo"), "Blue Earth Diagnostics") == "Blue Earth Diagnostics"
    assert workday_brand(info("Company Logo"), "Acme") == "Acme"
    assert workday_brand(info(None), "Acme") == "Acme"


# ------------------------------------------------------------------ hand-verified employers


def test_known_employers_join_the_watch_list_once(settings: Settings) -> None:
    lonza = CompanyBoard(name="Lonza", ats="workday",
                         url="https://lonza.wd3.myworkdayjobs.com/Lonza_Careers")  # fmt: skip
    cytiva = CompanyBoard(name="Cytiva", ats="workday", origin="biopharmguy-uk",
                          url="https://danaher.wd1.myworkdayjobs.com/DanaherJobs")  # fmt: skip
    save_companies(settings.companies_path, [lonza, cytiva])  # added by hand; from the directory
    cd.add_known_boards(settings)
    boards = load_companies(settings.companies_path)
    assert boards[:2] == [lonza, cytiva]  # theirs kept: the same board is never listed twice
    names = {b.name for b in boards}
    assert {"MSD", "IQVIA", "Takeda", "Agilent"} <= names and not {"Danaher"} & names
    assert len({b.key for b in boards}) == len(boards)
    before = settings.companies_path.stat().st_mtime_ns
    cd.add_known_boards(settings)  # nothing new: the file is not rewritten
    assert settings.companies_path.stat().st_mtime_ns == before
    assert any(v.companies == ["Agilent"] for v in cd.list_boards(settings))


def test_known_employers_answer_by_name_or_other_name(settings: Settings) -> None:
    http, seen = _http(settings, {})
    abcam = cd.known_board("Abcam", "biopharmguy-uk")
    assert abcam is not None and abcam.key == "workday:danaher/danaherjobs"
    assert abcam.name == "Abcam" and abcam.origin == "biopharmguy-uk"
    assert cd.known_board("Unknown Bio", None) is None
    # The talent-community link the user tried: Lonza's verified Workday board is used instead
    board = cd.add_company(settings, "Lonza", "https://lonza.talent-community.com", http)
    assert (board.key, board.origin) == ("workday:lonza/lonza_careers", None)
    assert seen == []


def test_search_adds_known_employers_before_any_discovery(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.workspace import Workspace

    monkeypatch.setattr(cd, "needs_discovery", lambda _: False)

    async def emit(_: str, __: dict[str, Any]) -> None:
        return None

    asyncio.run(ENSURE_COMPANY_BOARDS(Workspace(settings), emit))
    assert len(load_companies(settings.companies_path)) == len(cd.known_boards())


def test_adding_a_company_says_why_nothing_was_found(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        if host == "acme.talent-community.com":
            return httpx.Response(200, text='<link href="https://talent-pool.com"><p>Join</p>')
        if host == "beta.com":
            return httpx.Response(
                200,
                text='<a href="/careers">Careers</a>'
                if path == "/"
                else '<a href="https://acme.avature.net/careers">Jobs</a>',
            )
        return httpx.Response(403)  # gamma.com refuses automated reading

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    hint = "paste the address of the job list"
    with pytest.raises(ValueError, match="talent community") as err:
        cd.add_company(settings, "Acme", "https://acme.talent-community.com", http)
    assert hint in str(err.value)
    with pytest.raises(ValueError, match="on Avature, which the app cannot read"):
        cd.add_company(settings, "Beta", "https://beta.com/", http)
    with pytest.raises(ValueError, match=r"refuses automated reading \(403\)"):
        cd.add_company(settings, "Gamma", "https://gamma.com/", http)
    assert cd.your_companies(settings) == []
