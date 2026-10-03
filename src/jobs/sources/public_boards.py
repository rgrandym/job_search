"""UK job boards read from their public, logged-out pages: LinkedIn, Totaljobs, jobs.ac.uk and
NHS Jobs. None needs an account or a key.

Each search lists result cards only (cheap); full postings are opened lazily by `enrich`, which
the search pipeline calls for the shortlist alone. Every board is rate-limited per host.

| Board      | Listing                                        | Full posting (`enrich`)            |
| ---------- | ---------------------------------------------- | ---------------------------------- |
| LinkedIn   | `jobs-guest` search fragment (10 cards a page) | `jobs-guest` posting fragment      |
| Totaljobs  | search page's embedded JSON (25, page 1 only)  | posting page JSON-LD               |
| jobs.ac.uk | search page cards (25 a page, UK-wide)         | posting page JSON-LD               |
| NHS Jobs   | public XML search API                          | posting page sections              |

LinkedIn is read without robots.txt (it disallows every crawler) but slowly: one request per
`linkedin_delay_s`, at most `linkedin_max_pages` pages per term and `linkedin_max_details`
postings opened per search, stopping at the first refusal (HTTP 429/999). The others are read
within their robots.txt; Totaljobs disallows paging a radius search, so only page 1 is read.

UNVERIFIED: LinkedIn's markup and its `f_WT` / `f_TPR` filters are mapped from its public pages
as commonly documented, not checked against a live response; Totaljobs posting pages refused
connections when checked (2026-10-02), so its `enrich` gives up quietly after one failure.
The Totaljobs, jobs.ac.uk and NHS Jobs listings were checked against live responses.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any

from src.core.config import Settings, get_settings
from src.cv.models import WorkArrangement
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import (
    HttpFetcher,
    SourceError,
    extract_jsonld_jobs,
    html_to_text,
    infer_arrangement,
    parse_date,
    posting_from_jsonld,
)
from src.jobs.sources.job_boards import uk_only

LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
LINKEDIN_VIEW = "https://www.linkedin.com/jobs/view/{id}"
TOTALJOBS = "https://www.totaljobs.com"
JOBS_AC_UK = "https://www.jobs.ac.uk"
NHS_SEARCH = "https://www.jobs.nhs.uk/api/v1/search_xml"
LINKEDIN_PAGE = 10  # cards per LinkedIn page
LINKEDIN_WORKPLACE: dict[WorkArrangement, str] = {"onsite": "1", "remote": "2", "hybrid": "3"}
_NOT_A_SALARY = {"", "competitive", "unspecified", "negotiable", "not specified"}


def annual_salary(text: str | None) -> str | None:
    """`text` when it states an annual salary; None for "Competitive" or an hourly rate
    ("£25.76", "£14 - £16 per hour"), which `parse_salary` would misread as thousands."""
    clean = " ".join((text or "").split())
    if clean.lower() in _NOT_A_SALARY:
        return None
    numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", clean)]
    if not numbers or re.search(r"per (hour|day)|hourly|daily", clean, re.I):
        return None
    if all(n < 1000 for n in numbers) and not re.search(r"\d\s*k\b", clean, re.I):
        return None
    return clean


def _search_places(query: SearchQuery) -> list[str]:
    """Where to search: each city (with the country when set), or the country, or the UK."""
    return query.place_names() or ["United Kingdom"]


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.S)
    return html_to_text(match.group(1)) if match else ""


# ------------------------------------------------------------------ LinkedIn


class LinkedInSource:
    """LinkedIn's public job search (no login). See the module docstring for its limits."""

    name = "linkedin_search"

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(
            self.settings,
            delay_s=self.settings.linkedin_delay_s,
            user_agent=self.settings.browser_user_agent,
        )
        self.errors: dict[str, str] = {}
        self._details = 0
        self._refused = False

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        self.errors, self._details, self._refused = {}, 0, False
        jobs: dict[str, JobPosting] = {}
        per_search, cap = query.search_budget()
        pages = min(self.settings.linkedin_max_pages, -(-per_search // LINKEDIN_PAGE))
        # One pass per accepted arrangement, so every card is tagged with LinkedIn's own
        # workplace type instead of a guess (a guess of "onsite" would exclude it).
        modes: list[WorkArrangement | None] = (
            list(query.work_arrangements) if 0 < len(query.work_arrangements) < 3 else [None]
        )
        for term in query.search_terms():
            for place in _search_places(query):
                for mode in modes:
                    if self._refused:
                        break
                    for job in self._search(term, place, mode, query, pages):
                        jobs.setdefault(job.id, job)
        if self._refused and not jobs:
            raise SourceError(self.errors.get("search", "LinkedIn refused the search"))
        return list(jobs.values())[:cap]

    def _search(
        self,
        term: str,
        place: str,
        mode: WorkArrangement | None,
        query: SearchQuery,
        pages: int,
    ) -> list[JobPosting]:
        params: dict[str, Any] = {"keywords": term, "location": place}
        if query.locations and query.distance_miles is not None:
            params["distance"] = query.distance_miles
        if query.posted_within_days:
            params["f_TPR"] = f"r{query.posted_within_days * 86400}"
        if mode is not None:
            params["f_WT"] = LINKEDIN_WORKPLACE[mode]
        found: list[JobPosting] = []
        for page in range(pages):
            try:
                body = self.http.get(LINKEDIN_SEARCH, params={**params, "start": page * 10}).text
            except SourceError as exc:  # 429/999: LinkedIn wants us to slow down; stop here
                self.errors["search"] = f"LinkedIn stopped answering: {exc}"
                self._refused = True
                break
            cards = parse_linkedin_cards(body)
            for job in cards:
                job.within_search_area = bool(query.locations or query.country)
                if mode is not None:
                    job.work_arrangement = mode
            found += cards
            if len(cards) < LINKEDIN_PAGE:
                break
        return found

    def enrich(self, job: JobPosting) -> JobPosting:
        """The full posting, for at most `linkedin_max_details` shortlisted jobs per search."""
        if self._refused or self._details >= self.settings.linkedin_max_details:
            return job
        self._details += 1
        try:
            body = self.http.get(LINKEDIN_POSTING.format(id=job.id.split(":")[-1])).text
        except SourceError:
            self._refused = True
            raise
        return job.model_copy(update=linkedin_details(body, job))


def parse_linkedin_cards(page: str) -> list[JobPosting]:
    """Job cards from a LinkedIn guest search fragment."""
    jobs: list[JobPosting] = []
    for card in re.split(r"<li[\s>]", page)[1:]:
        ident = re.search(r"urn:li:jobPosting:(\d+)", card) or re.search(
            r"/jobs/view/[^\"'?]*?-?(\d{6,})", card
        )
        title = _first(r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</h3>', card)
        if not ident or not title:
            continue
        location = _first(r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</span>', card)
        posted = re.search(r'<time[^>]*datetime="([\d-]+)"', card)
        salary = _first(r'class="[^"]*job-search-card__salary-info[^"]*"[^>]*>(.*?)</span>', card)
        company = _first(r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)</h4>', card)
        jobs.append(
            JobPosting(
                id=f"linkedin:{ident.group(1)}",  # same id as LinkedIn alert emails: merged
                title=title,
                company=company or "unknown",
                location=location or None,
                work_arrangement=infer_arrangement(title, location),
                salary_range=annual_salary(salary),
                url=LINKEDIN_VIEW.format(id=ident.group(1)),
                posted_at=parse_date(posted.group(1)) if posted else None,
                source="linkedin_search",
            )
        )
    return jobs


def linkedin_details(page: str, job: JobPosting) -> dict[str, Any]:
    """Description (with LinkedIn's seniority and employment-type criteria) from a posting."""
    text = _first(r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', page)
    criteria = re.findall(
        r'job-criteria-subheader[^"]*"[^>]*>(.*?)</h3>\s*<span[^>]*>(.*?)</span>', page, re.S
    )
    lines = [f"{html_to_text(k)}: {html_to_text(v)}" for k, v in criteria]
    if "no longer accepting applications" in page.lower():
        lines.insert(0, "LinkedIn: no longer accepting applications")
    description = "\n".join([*lines, "", text]).strip() if text else job.description
    update: dict[str, Any] = {"description": description}
    if job.work_arrangement == "onsite":  # untagged search: the full text says more
        update["work_arrangement"] = infer_arrangement(job.location, text[:1500])
    return update


# ------------------------------------------------------------------ Totaljobs


class TotaljobsSource:
    """Totaljobs search pages (first page of 25 per term and place, within robots.txt)."""

    name = "totaljobs"

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        self._blocked = False

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        uk_only("Totaljobs", query)
        self._blocked = False
        jobs: dict[str, JobPosting] = {}
        places: list[str | None] = [*query.locations] or [None]
        for term in query.search_terms():
            for place in places:
                path = f"/jobs/{_slug(term)}" if term else "/jobs"
                params: dict[str, Any] = {}
                if place:
                    path += f"/in-{_slug(place)}"
                    if query.distance_miles is not None:
                        params["radius"] = query.distance_miles
                page = self.http.get(f"{TOTALJOBS}{path}", params=params, check_robots=True).text
                for job in parse_totaljobs(page):
                    job.within_search_area = bool(place)
                    jobs.setdefault(job.id, job)
        return list(jobs.values())[: query.limit]

    def enrich(self, job: JobPosting) -> JobPosting:
        """Full text from the posting's JSON-LD; after one refusal, keep the snippets."""
        if self._blocked or not job.url:
            return job
        try:
            nodes = extract_jsonld_jobs(self.http.get(job.url, check_robots=True).text)
        except SourceError:
            self._blocked = True
            raise
        if not nodes:
            return job
        full = posting_from_jsonld(nodes[0], source=self.name, url=job.url)
        return job.model_copy(
            update={"description": full.description or job.description, "closes_at": full.closes_at}
        )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


_PRELOADED = re.compile(
    r'__PRELOADED_STATE__\[["\']app-unifiedResultlist["\']\]\s*=\s*(\{.*?\});\s*\n', re.S
)


def parse_totaljobs(page: str) -> list[JobPosting]:
    """Listings from the JSON a Totaljobs search page embeds for its own result list."""
    match = _PRELOADED.search(page)
    if not match:
        raise SourceError("Totaljobs page carried no result list (layout changed?)")
    try:
        items = json.loads(match.group(1))["searchResults"]["items"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"Totaljobs result list unreadable: {exc}") from exc
    jobs: list[JobPosting] = []
    for it in items:
        if not it.get("id") or not it.get("title"):
            continue
        snippet = str(it.get("textSnippet") or "")
        location = it.get("location") or None
        jobs.append(
            JobPosting(
                id=f"totaljobs:{it['id']}",
                title=str(it["title"]),
                company=str(it.get("companyName") or "unknown"),
                location=location,
                work_arrangement=infer_arrangement(
                    it.get("title"), location, it.get("workFromHome"), snippet
                ),
                description=snippet,
                salary_range=annual_salary(str(it.get("salary") or "")),
                url=f"{TOTALJOBS}{it['url']}" if str(it.get("url", "")).startswith("/") else None,
                posted_at=parse_date(it.get("datePosted")),
                source="totaljobs",
            )
        )
    return jobs


# ------------------------------------------------------------------ jobs.ac.uk


class JobsAcUkSource:
    """jobs.ac.uk keyword search (UK-wide; its location filter needs a Google place id, so
    distance is left to the pre-filter and the job_matcher)."""

    name = "jobs_ac_uk"
    page_size = 25
    max_pages = 2

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        uk_only("jobs.ac.uk", query)
        jobs: dict[str, JobPosting] = {}
        today = date.today()
        for term in query.search_terms():
            for page in range(self.max_pages):
                params = {
                    "keywords": term,
                    "pageSize": self.page_size,
                    "startIndex": 1 + page * self.page_size,
                }
                body = self.http.get(f"{JOBS_AC_UK}/search/", params=params, check_robots=True)
                cards = parse_jobs_ac_uk(body.text, today)
                for job in cards:
                    jobs.setdefault(job.id, job)
                if len(cards) < self.page_size:
                    break
        return list(jobs.values())[: query.limit]

    def enrich(self, job: JobPosting) -> JobPosting:
        """Full text and dates from the posting page's JSON-LD."""
        if not job.url:
            return job
        nodes = extract_jsonld_jobs(self.http.get(job.url, check_robots=True).text)
        if not nodes:
            return job
        full = posting_from_jsonld(nodes[0], source=self.name, url=job.url)
        return job.model_copy(
            update={
                "description": full.description or job.description,
                "closes_at": full.closes_at or job.closes_at,
                "posted_at": full.posted_at or job.posted_at,
            }
        )


def _day_month(text: str, today: date, *, future: bool) -> date | None:
    """ "28 Sep" -> a date near `today`: the coming one for a closing date, else the past one."""
    try:
        parsed = datetime.strptime(f"{text.strip()} {today.year}", "%d %b %Y").date()
    except ValueError:
        return None
    if future and parsed < today - timedelta(days=60):  # "12 Jan" seen in October
        return parsed.replace(year=today.year + 1)
    if not future and parsed > today:
        return parsed.replace(year=today.year - 1)
    return parsed


def parse_jobs_ac_uk(page: str, today: date) -> list[JobPosting]:
    """Result cards from a jobs.ac.uk search page."""
    jobs: list[JobPosting] = []
    for card in page.split('class="j-search-result__result')[1:]:
        link = re.search(r'<a href="(/job/[^"]+)">(.*?)</a>', card, re.S)
        ident = re.search(r'data-advert-id="(\d+)"', card) or re.search(r"/job/([A-Z0-9]+)/", card)
        if not link or not ident:
            continue
        department = _first(r'j-search-result__department">(.*?)</div>', card)
        placed = _first(r"Date Placed:\s*</strong>([^<]+)", card)
        closes = _first(r'__date--blue[^"]*">([^<]+)</span>', card)
        jobs.append(
            JobPosting(
                id=f"jobs_ac_uk:{ident.group(1)}",
                title=html_to_text(link.group(2)),
                company=_first(r'j-search-result__employer">\s*<b>(.*?)</b>', card) or "unknown",
                location=_first(r"Location:\s*(.*?)</div>", card) or None,
                description=department,
                salary_range=annual_salary(_first(r"Salary:\s*</strong>(.*?)</div>", card)),
                url=f"{JOBS_AC_UK}{link.group(1)}",
                posted_at=_day_month(placed, today, future=False) if placed else None,
                closes_at=_day_month(closes, today, future=True) if closes else None,
                source="jobs_ac_uk",
            )
        )
    return jobs


# ------------------------------------------------------------------ NHS Jobs


class NHSJobsSource:
    """NHS Jobs' public XML search API (England and Wales NHS employers)."""

    name = "nhs_jobs"
    max_pages = 3

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        uk_only("NHS Jobs", query)
        jobs: dict[str, JobPosting] = {}
        places: list[str | None] = [*query.locations] or [None]
        for term in query.search_terms():
            for place in places:
                params: dict[str, Any] = {"keyword": term}
                if place:
                    params["location"] = place
                    if query.distance_miles is not None:
                        params["distance"] = query.distance_miles
                for page in range(1, self.max_pages + 1):
                    body = self.http.get(NHS_SEARCH, params={**params, "page": page}).text
                    found, pages = parse_nhs_jobs(body)
                    for job in found:
                        job.within_search_area = bool(place)
                        jobs.setdefault(job.id, job)
                    if page >= pages:
                        break
        return list(jobs.values())[: query.limit]

    def enrich(self, job: JobPosting) -> JobPosting:
        """Job overview and description sections from the advert page."""
        if not job.url:
            return job
        page = self.http.get(job.url).text
        overview = _between(page, 'id="job_overview"', 'id="about_organisation"')
        detail = _between(page, 'id="job_description_large"', 'id="contact_details"')
        text = "\n\n".join(t for t in (overview, detail) if t)
        return job.model_copy(update={"description": text or job.description})


def _between(page: str, start: str, end: str) -> str:
    i = page.find(start)
    if i < 0:
        return ""
    j = page.find(end, i)
    chunk = page[page.find(">", i) + 1 : j if j > 0 else None]
    # The page repeats the description for small screens: keep the first copy only.
    return html_to_text(chunk.split(start)[0])


def parse_nhs_jobs(body: str) -> tuple[list[JobPosting], int]:
    """(vacancies, total pages) from an NHS Jobs XML search response."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceError(f"NHS Jobs answer is not XML: {exc}") from exc
    jobs: list[JobPosting] = []
    for v in root.iter("vacancyDetails"):
        text = {child.tag: (child.text or "").strip() for child in v}
        places = [loc.text.strip() for loc in v.iter("location") if loc.text]
        location = "; ".join(places) or None
        contract = text.get("type", "")
        jobs.append(
            JobPosting(
                id=f"nhs_jobs:{text.get('reference') or text.get('id')}",
                title=text.get("title", "") or "Untitled",
                company=text.get("employer") or "NHS",
                location=location,
                work_arrangement=infer_arrangement(text.get("title"), location),
                description=(f"Contract: {contract}\n" if contract else "")
                + text.get("description", ""),
                salary_range=annual_salary(text.get("salary")),
                url=text.get("url") or None,
                posted_at=parse_date(text.get("postDate")),
                closes_at=parse_date(text.get("closeDate")),
                source="nhs_jobs",
            )
        )
    pages = int(root.findtext("totalPages") or 1)
    return jobs, pages
