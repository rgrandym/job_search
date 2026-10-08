"""Careers sites read through the pages and JSON their own job search uses (within robots.txt).

Checked against live sites (2026-10-08):
    successfactors  SAP SuccessFactors career site (Bayer, Boehringer, Astellas): the HTML of
                    {site}/search/?q=..&locationsearch=.. (table or tile layout, filtered by
                    country where the site offers it), then each {site}/job/... page's
                    `itemprop="description"`. `/services/` (its RSS) is disallowed, so unused.
    phenom          Phenom career site (UCB, Merck KGaA, Siemens Healthineers): the job list
                    the search-results page embeds (`phApp.ddo`), filtered by country with
                    `selected_fields`, then each job page's schema.org JobPosting.
    oracle          Oracle Recruiting Cloud (Oxford Nanopore): the candidate site's REST API,
                    recruitingCEJobRequisitions (list) and ...Details (one job).
    jobvite         jobs.jobvite.com/{token}/jobs (one HTML list), then each job page's
                    schema.org JobPosting.
    cws             Radancy career sites on Google Cloud Talent (Charles River): the jobs API
                    named in the page's `cws_opts`, filtered by country; it returns full text.
Like Workday, the list is read first and only titles fitting the search are opened.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from src.core.config import Settings
from src.cv.models import WorkArrangement
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import (
    HttpFetcher,
    SourceError,
    country_code,
    country_names,
    extract_jsonld_jobs,
    html_to_text,
    infer_arrangement,
    located_in,
    parse_date,
    posting_from_jsonld,
)

if TYPE_CHECKING:
    from src.jobs.sources.companies import CompanyBoard

LIST_MAX_PAGES = 5  # per search term
PHENOM_PAGE = 10  # Phenom's page size
ORACLE_PAGE = 25
CWS_PAGE = 100
_SF_ROW = re.compile(r'<tr class="data-row|<li class="job-tile')
_SF_LINK = re.compile(r'class="jobTitle-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SF_LOCATION = re.compile(
    r'<span class="jobLocation">(.*?)</span>|-section-location-value">(.*?)</div>', re.S
)
_SF_DATE = re.compile(r'<span class="jobDate">(.*?)</span>|-section-date-value">(.*?)</div>', re.S)
_SF_JOB_ID = re.compile(r"/(\d+)/?$")
_PHENOM_DDO = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp", re.S)
_ORACLE_URL = re.compile(
    r"https://(?P<host>[\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
    r"(?P<lang>[a-z]{2})/sites/(?P<site>[\w-]+)"
)
_JOBVITE_ROW = re.compile(
    r'<td class="jv-job-list-name">\s*<a href="([^"]+)">(.*?)</a>\s*</td>\s*'
    r'<td class="jv-job-list-location">(.*?)</td>',
    re.S,
)
_ORACLE_TEXT = ("Description", "Responsibilities", "Qualifications")
_CWS_OPTS = re.compile(r"var cws_opts\s*=\s*(\{.*?\});", re.S)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _places(query: SearchQuery) -> list[str]:
    """Names of the searched area as location labels use them (none: anywhere)."""
    return country_names(query.country) or [p.lower() for p in query.locations]


def _text(raw: str) -> str:
    return " ".join(html_to_text(raw).split())


def balanced_element(page: str, marker: str) -> str:
    """The element (span or div) opening at `marker`, with everything nested in it."""
    start = page.find(marker)
    if start < 0:
        return ""
    start = page.rfind("<", 0, start)
    tag = re.match(r"<(\w+)", page[start:])
    if not tag:
        return ""
    name, depth = tag[1].lower(), 0
    for m in re.finditer(rf"<(/?){name}\b[^>]*>", page[start:], re.IGNORECASE):
        depth += -1 if m[1] else 1
        if depth == 0:
            return page[start : start + m.end()]
    return page[start:]


def phenom_jobs(page: str) -> tuple[list[dict[str, Any]], int]:
    """(jobs, total hits) a Phenom search-results page embeds for its first render."""
    m = _PHENOM_DDO.search(page)
    if not m:
        raise SourceError("no Phenom search data on the page")
    found = json.loads(m[1]).get("eagerLoadRefineSearch") or {}
    jobs = (found.get("data") or {}).get("jobs") or []
    return [j for j in jobs if isinstance(j, dict)], int(found.get("totalHits") or 0)


class CareerSiteFeeds:
    """Readers for careers sites without a public ATS feed (mixed into CompanyCareersSource)."""

    http: HttpFetcher
    settings: Settings

    def _shortlist(self, query: SearchQuery, listed: dict[str, str]) -> list[str]:
        raise NotImplementedError

    # -------------------------------------------------------------- SAP SuccessFactors

    def _successfactors(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        base = _origin(c.url or "")
        params: dict[str, Any] = {"locationsearch": query.country or " ".join(query.locations)}
        cc = country_code(query.country)
        first = self.http.get(f"{base}/search/", params={"q": ""}, check_robots=True).text
        if cc and 'name="optionsFacetsDD_country"' in first:
            params["optionsFacetsDD_country"] = cc.upper()  # a site without the facet shows none
        rows: dict[str, tuple[str, str, str]] = {}
        in_area = located_in(_places(query)) if _places(query) else None
        for term in query.search_terms():
            start = 0
            for _ in range(LIST_MAX_PAGES):
                body = self.http.get(
                    f"{base}/search/",
                    params={**params, "q": term, "startrow": start},
                    check_robots=True,
                ).text
                found = successfactors_rows(body)
                new = found.keys() - rows.keys()
                rows.update(found)
                if not new:
                    break
                start += len(found)
        # A row without a location is kept only when the site itself filtered by country.
        filtered = "optionsFacetsDD_country" in params
        rows = {
            path: r
            for path, r in rows.items()
            if in_area is None or (in_area(r[1]) if r[1] else filtered)
        }
        listed = {path: title for path, (title, _, _) in rows.items()}
        keep = self._shortlist(query, listed)
        return [self._successfactors_job(c, base, path, rows[path]) for path in keep]

    def _successfactors_job(
        self, c: CompanyBoard, base: str, path: str, row: tuple[str, str, str]
    ) -> JobPosting:
        title, location, posted = row
        page = self.http.get(f"{base}{path}", check_robots=True).text
        text = html_to_text(balanced_element(page, 'itemprop="description"'))
        ident = _SF_JOB_ID.search(path)
        host = urlsplit(base).netloc
        return JobPosting(
            id=f"successfactors:{host}:{ident[1] if ident else path}",
            title=title,
            company=c.name,
            location=location or None,
            work_arrangement=infer_arrangement(title, location, text[:500]),
            description=text,
            url=f"{base}{path}",
            posted_at=successfactors_date(posted),
            source=f"successfactors:{host}",
        )

    # -------------------------------------------------------------- Phenom

    def _phenom(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        base = (c.url or "").rstrip("/")
        params: dict[str, Any] = {}
        if query.country:
            params["selected_fields"] = json.dumps({"country": [query.country]})
        in_area = located_in(_places(query)) if _places(query) else None
        listed: dict[str, str] = {}
        for term in query.search_terms():
            for start in range(0, LIST_MAX_PAGES * PHENOM_PAGE, PHENOM_PAGE):
                extra = {"from": start} if start else {}
                page = self.http.get(
                    f"{base}/search-results",
                    params={**params, "keywords": term, **extra},
                    check_robots=True,
                ).text
                jobs, total = phenom_jobs(page)
                for j in jobs:
                    places = [j.get("location") or "", *(j.get("multi_location") or [])]
                    if in_area is None or any(in_area(str(p)) for p in places):
                        seq = str(j.get("jobSeqNo") or j.get("jobId"))
                        listed.setdefault(seq, j.get("title", ""))
                if len(jobs) < PHENOM_PAGE or start + PHENOM_PAGE >= total:
                    break
        found = [self._phenom_job(c, base, seq) for seq in self._shortlist(query, listed)]
        return [j for j in found if j]

    def _phenom_job(self, c: CompanyBoard, base: str, seq: str) -> JobPosting | None:
        url = f"{base}/job/{seq}"
        nodes = extract_jsonld_jobs(self.http.get(url, check_robots=True).text)
        if not nodes:
            return None  # withdrawn since it was listed
        host = urlsplit(base).netloc
        job = posting_from_jsonld(nodes[0], source=f"phenom:{host}", url=url)
        return job.model_copy(update={"id": f"phenom:{host}:{seq}", "company": c.name, "url": url})

    # -------------------------------------------------------------- Oracle Recruiting Cloud

    def _oracle(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        m = _ORACLE_URL.match(c.url or "")
        if not m:
            raise SourceError(f"not an Oracle candidate site: {c.url}")
        api = f"https://{m['host']}/hcmRestApi/resources/latest"
        in_area = located_in(_places(query)) if _places(query) else None
        listed: dict[str, str] = {}
        for term in query.search_terms():
            for offset in range(0, LIST_MAX_PAGES * ORACLE_PAGE, ORACLE_PAGE):
                keyword = f',keyword="{term}"' if term else ""
                finder = (
                    f"findReqs;siteNumber={m['site']}{keyword},limit={ORACLE_PAGE},"
                    f"offset={offset},sortBy=POSTING_DATES_DESC"
                )
                data = self.http.get(
                    f"{api}/recruitingCEJobRequisitions",
                    params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                            "finder": finder},
                    check_robots=True,
                ).json()  # fmt: skip
                found = (data.get("items") or [{}])[0]
                reqs = found.get("requisitionList") or []
                for r in reqs:
                    places = [r.get("PrimaryLocation") or ""]
                    places += [s.get("Name") or "" for s in r.get("secondaryLocations") or []]
                    if in_area is None or any(in_area(str(p)) for p in places):
                        listed.setdefault(str(r["Id"]), r.get("Title", ""))
                if len(reqs) < ORACLE_PAGE or offset + ORACLE_PAGE >= int(
                    found.get("TotalJobsCount") or 0
                ):
                    break
        return [self._oracle_job(c, m, api, rid) for rid in self._shortlist(query, listed)]

    def _oracle_job(self, c: CompanyBoard, m: re.Match[str], api: str, rid: str) -> JobPosting:
        finder = f'ById;Id="{rid}",siteNumber={m["site"]}'
        data = self.http.get(
            f"{api}/recruitingCEJobRequisitionDetails",
            params={"onlyData": "true", "expand": "all", "finder": finder},
            check_robots=True,
        ).json()
        j = (data.get("items") or [{}])[0]
        text = html_to_text("\n".join(str(j.get(f"External{k}Str") or "") for k in _ORACLE_TEXT))
        title, location = j.get("Title", ""), j.get("PrimaryLocation")
        workplace = str(j.get("WorkplaceTypeCode") or j.get("WorkplaceType") or "").lower()
        arrangement: WorkArrangement = (
            "remote" if "remote" in workplace else "hybrid" if "hybrid" in workplace
            else infer_arrangement(title, location, text[:500])
        )  # fmt: skip
        return JobPosting(
            id=f"oracle:{m['host']}:{rid}",
            title=title,
            company=c.name,
            location=location,
            work_arrangement=arrangement,
            description=text,
            url=f"https://{m['host']}/hcmUI/CandidateExperience/{m['lang']}/sites/{m['site']}/job/{rid}",
            posted_at=parse_date(j.get("ExternalPostedStartDate")),
            source=f"oracle:{m['host']}",
        )

    # -------------------------------------------------------------- Jobvite

    def _jobvite(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        base = "https://jobs.jobvite.com"
        page = self.http.get(f"{base}/{c.token}/jobs", check_robots=True).text
        in_area = located_in(_places(query)) if _places(query) else None
        listed: dict[str, str] = {}
        for path, title, location in _JOBVITE_ROW.findall(page):
            if in_area is None or in_area(_text(location)):
                listed.setdefault(path, _text(title))
        jobs = []
        for path in self._shortlist(query, listed):
            url = f"{base}{path}"
            for node in extract_jsonld_jobs(self.http.get(url, check_robots=True).text)[:1]:
                job = posting_from_jsonld(node, source=f"jobvite:{c.token}", url=url)
                ident = f"jobvite:{c.token}:{path.rsplit('/', 1)[-1]}"
                jobs.append(job.model_copy(update={"id": ident, "company": c.name, "url": url}))
        return jobs

    # -------------------------------------------------------------- Radancy CWS

    def _cws(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        page = self.http.get(c.url or "", check_robots=True).text
        m = _CWS_OPTS.search(page)
        if not m:
            raise SourceError(f"no job search settings (cws_opts) on {c.url}")
        opts = json.loads(m[1])
        api, org = str(opts.get("api") or "").strip(), str(opts.get("org") or "")
        if not api.startswith("https://") or not org:
            raise SourceError(f"unreadable job search settings on {c.url}")
        params: dict[str, Any] = {"companyName": org, "pageSize": CWS_PAGE, "offset": 0}
        if cc := country_code(query.country):
            params["customAttributeFilter"] = f'primary_country="{cc.upper()}"'
        jobs: dict[str, JobPosting] = {}
        for term in query.search_terms():
            data = self.http.get(
                f"{api.rstrip('/')}/job/search",
                params={**params, **({"query": term} if term else {})},
                check_robots=True,
            ).json()
            for result in data.get("searchResults") or []:
                job = cws_posting(c, result.get("job") or {})
                if job:
                    jobs.setdefault(job.id, job)
        return list(jobs.values())


def successfactors_rows(page: str) -> dict[str, tuple[str, str, str]]:
    """Job path -> (title, location, posted label) from a SuccessFactors results page, in
    either of its layouts (table rows or tiles)."""
    rows: dict[str, tuple[str, str, str]] = {}
    for chunk in _SF_ROW.split(page)[1:]:
        link = _SF_LINK.search(chunk)
        if not link:
            continue
        loc, posted = _SF_LOCATION.search(chunk), _SF_DATE.search(chunk)
        location = _text(next((g for g in loc.groups() if g), "")) if loc else ""
        date_label = _text(next((g for g in posted.groups() if g), "")) if posted else ""
        rows.setdefault(html.unescape(link[1]), (_text(link[2]), location, date_label))
    return rows


def successfactors_date(label: str) -> date | None:
    """A results page's date label ("Oct 5, 2026", "5 Oct 2026" or ISO)."""
    for fmt in ("%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(label.strip(), fmt).date()
        except ValueError:
            pass
    return parse_date(label)


def cws_posting(c: CompanyBoard, j: dict[str, Any]) -> JobPosting | None:
    """One job from a Radancy CWS search result (full description included)."""
    if not j.get("title") or not (j.get("ref") or j.get("id")):
        return None
    location = ", ".join(str(j[k]) for k in ("primary_city", "primary_country") if j.get(k))
    text = html_to_text(str(j.get("description") or ""))
    remote = str(j.get("location_type") or "").lower() == "remote"
    arrangement: WorkArrangement = (
        "remote" if remote else infer_arrangement(j["title"], location, text[:500])
    )
    host = urlsplit(c.url or "").netloc
    return JobPosting(
        id=f"cws:{host}:{j.get('ref') or j.get('id')}",
        title=str(j["title"]),
        company=c.name,
        location=location or None,
        work_arrangement=arrangement,
        description=text,
        url=j.get("url"),
        posted_at=parse_date(j.get("open_date")),
        source=f"cws:{host}",
    )


def phenom_site(page_url: str) -> str:
    """A Phenom site's base (origin + locale path, e.g. https://careers.ucb.com/global/en)."""
    parts = urlsplit(page_url)
    locale = re.match(r"/([a-z]{2,6})/([a-z]{2})(?:/|$)", parts.path)
    return f"{parts.scheme}://{parts.netloc}" + (locale[0].rstrip("/") if locale else "")
