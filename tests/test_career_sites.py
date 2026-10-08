"""Careers sites read through their own search pages or JSON (SuccessFactors, Phenom, Oracle,
Jobvite, Radancy CWS), found by discovery and kept within robots.txt. All HTTP is mocked."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from src.core.config import Settings
from src.jobs.models import SearchQuery
from src.jobs.sources.base import SourceError
from src.jobs.sources.career_sites import balanced_element, successfactors_rows
from src.jobs.sources.companies import CompanyBoard, CompanyCareersSource, detect_boards
from tests.test_company_discovery import ROBOTS_OK, _http

UK = SearchQuery(titles=["Scientist"], country="United Kingdom")


def _jsonld(title: str, place: str) -> str:
    node = {
        "@type": "JobPosting", "title": title, "description": "<p>Assay development</p>",
        "datePosted": "2026-10-01", "hiringOrganization": {"name": "Acme Group plc"},
        "jobLocation": {"address": {"addressLocality": place, "addressCountry": "GB"}},
    }  # fmt: skip
    return f'<script type="application/ld+json">{json.dumps(node)}</script>'


# ------------------------------------------------------------------ SAP SuccessFactors

SF_TABLE = """<table><tr class="data-row"><td>
<a class="jobTitle-link" href="/job/Reading-Scientist/111/">Senior Scientist</a></td>
<td><span class="jobLocation"> Reading, Berkshire, GB </span></td>
<td><span class="jobDate">Oct 5, 2026</span></td></tr>
<tr class="data-row"><td><a class="jobTitle-link" href="/job/Boston-Scientist/222/">Scientist II</a>
</td><td><span class="jobLocation">Boston, MA, US</span></td></tr></table>"""
SF_TILES = """<ul><li class="job-tile job-id-333">
<a class="jobTitle-link x" href="/job/Pirbright-Scientist/333/"> Lab Scientist </a>
<div id="job-333-desktop-section-location-value">Pirbright, United Kingdom</div></li>
<li class="job-tile job-id-444"><a class="jobTitle-link" href="/job/X/444/">Scientist</a></li>
</ul>"""
SF_JOB = """<div><span itemprop="description" class="x"><span class="jobdescription"><p>Run
<span>qPCR</span> assays</p></span></span><span itemprop="industry">Pharma</span></div>"""


def test_successfactors_reads_both_layouts_and_filters_by_country(settings: Settings) -> None:
    seen_params: list[dict[str, str]] = []

    def search(request: httpx.Request) -> str:
        seen_params.append(dict(request.url.params))
        if request.url.params.get("q") == "":  # the first look: does the site filter by country?
            return '<select name="optionsFacetsDD_country"></select>'
        return SF_TABLE if request.url.params.get("startrow") == "0" else ""

    http, _ = _http(
        settings,
        {"/robots.txt": ROBOTS_OK, "jobs.acme.com/search/": search,
         "jobs.acme.com/job/Reading-Scientist/111/": SF_JOB},
    )  # fmt: skip
    board = CompanyBoard(name="Acme", ats="successfactors", url="https://jobs.acme.com")
    [job] = CompanyCareersSource([], http, settings).fetch_board(board, UK)
    assert seen_params[1]["optionsFacetsDD_country"] == "GB" and seen_params[1]["q"] == "Scientist"
    assert (job.id, job.title, job.location) == (
        "successfactors:jobs.acme.com:111",
        "Senior Scientist",
        "Reading, Berkshire, GB",
    )  # the Boston row is outside the searched country
    assert " ".join(job.description.split()) == "Run qPCR assays"
    assert job.posted_at == date(2026, 10, 5)

    tiles = successfactors_rows(SF_TILES)
    assert tiles["/job/Pirbright-Scientist/333/"] == (
        "Lab Scientist",
        "Pirbright, United Kingdom",
        "",
    )
    assert tiles["/job/X/444/"][1] == ""  # no location: kept only where the site filtered


def test_successfactors_without_country_filter_drops_unplaced_rows(settings: Settings) -> None:
    http, seen = _http(
        settings,
        {"/robots.txt": ROBOTS_OK, "jobs.acme.com/search/": SF_TILES,
         "/job/Pirbright-Scientist/333/": SF_JOB},
    )  # fmt: skip
    board = CompanyBoard(name="Acme", ats="successfactors", url="https://jobs.acme.com")
    jobs = CompanyCareersSource([], http, settings).fetch_board(board, UK)
    assert [j.title for j in jobs] == ["Lab Scientist"]
    assert not any("/job/X/444/" in str(r.url) for r in seen)


def test_successfactors_keeps_robots_txt(settings: Settings) -> None:
    http, _ = _http(settings, {"/robots.txt": "User-agent: *\nDisallow: /search/"})
    board = CompanyBoard(name="Acme", ats="successfactors", url="https://jobs.acme.com")
    with pytest.raises(SourceError, match="robots.txt disallows"):
        CompanyCareersSource([], http, settings).fetch_board(board, UK)


def test_balanced_element_keeps_nested_tags() -> None:
    page = '<p>x</p><span itemprop="description"><span>a</span><b>b</b></span><span>c</span>'
    assert balanced_element(page, 'itemprop="description"').endswith("<b>b</b></span>")
    assert balanced_element(page, "missing") == ""


# ------------------------------------------------------------------ Phenom


def _phenom_page(jobs: list[dict[str, Any]], total: int) -> str:
    ddo = {"eagerLoadRefineSearch": {"totalHits": total, "data": {"jobs": jobs}}}
    return f"<script>phApp.ddo = {json.dumps(ddo)}; phApp.experimentData = {{}};</script>"


def test_phenom_lists_embedded_jobs_then_reads_jsonld(settings: Settings) -> None:
    params: list[dict[str, str]] = []

    def search(request: httpx.Request) -> str:
        params.append(dict(request.url.params))
        return _phenom_page(
            [{"jobSeqNo": "UCB1", "title": "Principal Scientist",
              "location": "Slough, United Kingdom"},
             {"jobSeqNo": "UCB2", "title": "Scientist", "location": "Braine, Belgium"},
             {"jobSeqNo": "UCB3", "title": "Sales Lead", "location": "London, United Kingdom"}],
            total=3,
        )  # fmt: skip

    http, seen = _http(
        settings,
        {"/robots.txt": ROBOTS_OK, "careers.acme.com/global/en/search-results": search,
         "careers.acme.com/global/en/job/UCB1": _jsonld("Principal Scientist", "Slough")},
    )  # fmt: skip
    board = CompanyBoard(name="Acme", ats="phenom", url="https://careers.acme.com/global/en")
    [job] = CompanyCareersSource([], http, settings).fetch_board(board, UK)
    assert json.loads(params[0]["selected_fields"]) == {"country": ["United Kingdom"]}
    assert (job.id, job.company, job.location) == ("phenom:careers.acme.com:UCB1", "Acme",
                                                   "Slough, GB")  # fmt: skip
    assert job.description == "Assay development"
    assert not any("UCB2" in str(r.url) or "UCB3" in str(r.url) for r in seen)


def test_phenom_page_without_its_data_is_an_error(settings: Settings) -> None:
    http, _ = _http(settings, {"/robots.txt": ROBOTS_OK, "/global/en/search-results": "<p>x</p>"})
    board = CompanyBoard(name="Acme", ats="phenom", url="https://careers.acme.com/global/en")
    with pytest.raises(SourceError, match="no Phenom search data"):
        CompanyCareersSource([], http, settings).fetch_board(board, UK)


# ------------------------------------------------------------------ Oracle, Jobvite, CWS

ORACLE = "https://abcd.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"


def test_oracle_lists_requisitions_then_reads_details(settings: Settings) -> None:
    finders: list[str] = []

    def requisitions(request: httpx.Request) -> dict[str, Any]:
        finders.append(request.url.params["finder"])
        return {"items": [{"TotalJobsCount": 2, "requisitionList": [
            {"Id": "3252", "Title": "Development Scientist",
             "PrimaryLocation": "OXFORD, OXFORDSHIRE, United Kingdom"},
            {"Id": "9", "Title": "Scientist", "PrimaryLocation": "New York, United States",
             "secondaryLocations": [{"Name": "Boston, United States"}]},
        ]}]}  # fmt: skip

    detail = {"items": [{
        "Title": "Development Scientist", "PrimaryLocation": "OXFORD, OXFORDSHIRE, United Kingdom",
        "ExternalDescriptionStr": "<p>Nanopore assays</p>",
        "ExternalQualificationsStr": "<p>PhD</p>",
        "WorkplaceTypeCode": "ORA_HYBRID", "ExternalPostedStartDate": "2026-10-05T08:00:00+00:00",
    }]}  # fmt: skip
    api = "abcd.fa.em2.oraclecloud.com/hcmRestApi/resources/latest"
    http, seen = _http(
        settings,
        {f"{api}/recruitingCEJobRequisitions": requisitions,
         f"{api}/recruitingCEJobRequisitionDetails": detail},
    )  # fmt: skip
    board = CompanyBoard(name="ONT", ats="oracle", url=ORACLE)
    [job] = CompanyCareersSource([], http, settings).fetch_board(board, UK)
    assert finders[0].startswith('findReqs;siteNumber=CX_1,keyword="Scientist",limit=25,offset=0')
    assert job.id == "oracle:abcd.fa.em2.oraclecloud.com:3252" and job.work_arrangement == "hybrid"
    assert "Nanopore assays" in job.description and "PhD" in job.description
    assert job.url == f"{ORACLE}/job/3252" and job.posted_at == date(2026, 10, 5)
    assert sum("Details" in str(r.url) for r in seen) == 1  # the US posting is never opened


JOBVITE_LIST = """<table class="jv-job-list"><tr><td class="jv-job-list-name">
<a href="/acme/job/oA1">Senior Scientist Genomics</a></td><td class="jv-job-list-location">
 London, United Kingdom </td></tr><tr><td class="jv-job-list-name"><a href="/acme/job/oB2">
Scientist</a></td><td class="jv-job-list-location"> San Rafael, California </td></tr></table>"""


def test_jobvite_lists_then_reads_jsonld(settings: Settings) -> None:
    http, seen = _http(
        settings,
        {"/robots.txt": ROBOTS_OK, "jobs.jobvite.com/acme/jobs": JOBVITE_LIST,
         "jobs.jobvite.com/acme/job/oA1": _jsonld("Senior Scientist Genomics", "London")},
    )  # fmt: skip
    board = CompanyBoard(name="Acme", ats="jobvite", token="acme")
    [job] = CompanyCareersSource([], http, settings).fetch_board(board, UK)
    assert (job.id, job.company, job.url) == (
        "jobvite:acme:oA1",
        "Acme",
        "https://jobs.jobvite.com/acme/job/oA1",
    )
    assert not any("oB2" in str(r.url) for r in seen)


def test_cws_reads_the_jobs_api_its_page_names(settings: Settings) -> None:
    opts = {"org": "companies/abc", "api": "https://jobsapi.example.io/api/"}
    page = f"<script>var cws_opts = {json.dumps(opts)};</script>"
    params: list[dict[str, str]] = []

    def search(request: httpx.Request) -> dict[str, Any]:
        params.append(dict(request.url.params))
        return {"searchResults": [{"job": {
            "title": "Scientific Associate", "ref": "236_en", "primary_city": "Tranent",
            "primary_country": "GB", "description": "<p>Bioanalysis</p>",
            "open_date": "2026-09-28T10:53:08", "url": "https://jobs.acme.com/job/236/x/",
        }}]}  # fmt: skip

    http, _ = _http(
        settings,
        {"/robots.txt": ROBOTS_OK, "jobs.acme.com/search/": page,
         "jobsapi.example.io/api/job/search": search},
    )  # fmt: skip
    board = CompanyBoard(name="CRL", ats="cws", url="https://jobs.acme.com/search/")
    [job] = CompanyCareersSource([], http, settings).fetch_board(board, UK)
    assert params[0]["companyName"] == "companies/abc" and params[0]["query"] == "Scientist"
    assert params[0]["customAttributeFilter"] == 'primary_country="GB"'
    assert (job.id, job.location, job.description) == ("cws:jobs.acme.com:236_en",
                                                       "Tranent, GB", "Bioanalysis")  # fmt: skip


# ------------------------------------------------------------------ discovery


def test_discovery_recognises_careers_sites_by_their_assets() -> None:
    def kinds(page: str, url: str) -> list[tuple[str, str | None, str | None]]:
        return [(b.ats, b.url, b.token) for b in detect_boards(page, "Acme", page_url=url)]

    rmk = '<link href="https://rmkcdn.successfactors.com/abc/site.css">'
    assert kinds(rmk, "https://jobs.acme.com/go/Science/123/") == [
        ("successfactors", "https://jobs.acme.com", None)
    ]
    phenom = "<script>phApp.ddo = {};</script>"
    assert kinds(phenom, "https://careers.acme.com/global/en/home") == [
        ("phenom", "https://careers.acme.com/global/en", None)
    ]
    cws = '<script>var cws_opts = {"api":"https://jobsapi-google.m-cloud.io/api/"};</script>'
    assert kinds(cws, "https://jobs.acme.com/jobs/") == [
        ("cws", "https://jobs.acme.com/jobs/", None)
    ]
    links = (
        f'<a href="{ORACLE}/requisitions">Jobs</a> <a href="https://jobs.jobvite.com/acme">J</a>'
    )
    assert kinds(links, "https://acme.com/careers") == [
        ("oracle", ORACLE, None),
        ("jobvite", None, "acme"),
    ]
