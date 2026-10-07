"""Individual company career sites, read through the job feed of the ATS that hosts them.

Public feeds published for embedding (no key needed):
    Greenhouse       https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    Lever            https://api.lever.co/v0/postings/{token}?mode=json (EU: api.eu.lever.co)
    Ashby            https://api.ashbyhq.com/posting-api/job-board/{token}
    Workable         https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
    SmartRecruiters  https://api.smartrecruiters.com/v1/companies/{token}/postings
    Recruitee        https://{token}.recruitee.com/api/offers/
    Personio         https://{token}.jobs.personio.de/xml
    Teamtailor       {careers site}/jobs.rss (on *.teamtailor.com or the company's own domain)
    BambooHR         https://{token}.bamboohr.com/careers/list + /careers/{id}/detail
    Pinpoint         https://{token}.pinpointhq.com/postings.json
JSON endpoints behind a careers site (most big pharma), used within the site's robots.txt:
    Workday          POST https://{tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    iCIMS            https://{host}.icims.com/jobs/search (HTML list) + JSON-LD on each job page
For any other careers page, `careers_page` reads schema.org `JobPosting` JSON-LD
(which most sites embed for Google Jobs), after checking robots.txt.

Workday, iCIMS, SmartRecruiters and BambooHR list jobs first and open at most
`settings.company_max_details` of them per company, the ones whose title fits the search.

Companies are listed in `data/companies.json` (see data/examples/companies.example.json):
    [{"name": "Acme", "ats": "greenhouse", "token": "acme"},
     {"name": "GSK", "ats": "workday", "url": "https://gsk.wd5.myworkdayjobs.com/GSKCareers"},
     {"name": "Foo", "ats": "careers_page", "url": "https://foo.com/careers"}]
`src/services/company_discovery.py` fills it from a company directory (BioPharmGuy); the app
runs it before the first search that includes company sites, and on request.
"""

from __future__ import annotations

import contextvars
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, TypeAdapter, model_validator

from src.core.config import Settings, get_settings
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
    parse_date,
    posting_from_jsonld,
)

AtsKind = Literal[
    "greenhouse",
    "lever",
    "lever_eu",
    "ashby",
    "workable",
    "smartrecruiters",
    "recruitee",
    "personio",
    "teamtailor",
    "workday",
    "icims",
    "bamboohr",
    "pinpoint",
    "careers_page",
]
URL_KINDS = (
    "careers_page",
    "workday",
    "icims",
    "teamtailor",
)  # boards identified by a URL, not a slug
WORKDAY_PAGE = 20  # Workday's maximum page size
WORKDAY_MAX_LISTED = 200  # per search term; titles are shortlisted before opening any
ICIMS_MAX_PAGES = 5
_WORKDAY_URL = re.compile(
    r"https://(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"(?P<site>[\w-]+)"
)
_ICIMS_LINK = re.compile(
    r'href="(?P<url>https://[^"]+/jobs/\d+/[^"?]+/job)[^"]*"[^>]*title="\d+ - (?P<title>[^"]+)"'
)


class CompanyBoard(BaseModel):
    """One company's job feed. `origin` names the directory it was discovered from (None =
    added by hand, which discovery never overwrites)."""

    name: str
    ats: AtsKind
    token: str | None = None
    url: str | None = None
    origin: str | None = None

    @model_validator(mode="after")
    def _check(self) -> CompanyBoard:
        if self.ats in URL_KINDS and not self.url:
            raise ValueError(f"{self.name}: {self.ats} needs `url`")
        if self.ats not in URL_KINDS and not self.token:
            raise ValueError(f"{self.name}: {self.ats} needs `token` (the board slug)")
        if self.ats == "workday" and not _WORKDAY_URL.match(self.url or ""):
            raise ValueError(
                f"{self.name}: workday url must be https://<t>.wdN.myworkdayjobs.com/<site>"
            )
        return self

    @property
    def key(self) -> str:
        """Identity of the feed itself: two companies can share one board (GSK and ViiV)."""
        ref = self.token or self.url or ""
        if self.ats == "workday":
            m = _WORKDAY_URL.match(ref)
            ref = f"{m['tenant']}/{m['site']}" if m else ref
        elif self.ats in ("icims", "teamtailor"):
            ref = urlsplit(ref).netloc
        return f"{self.ats}:{ref.lower().rstrip('/')}"


def load_companies(path: Path) -> list[CompanyBoard]:
    """Read the company watch-list."""
    return TypeAdapter(list[CompanyBoard]).validate_json(path.read_bytes())


def save_companies(path: Path, companies: list[CompanyBoard]) -> None:
    """Write the company watch-list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = TypeAdapter(list[CompanyBoard]).dump_json(companies, indent=2, exclude_none=True)
    path.write_bytes(data + b"\n")


# ------------------------------------------------------------------ ATS detection

_DETECT: list[tuple[AtsKind, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)")),
    ("greenhouse", re.compile(
        r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)"
    )),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)")),
    ("lever_eu", re.compile(r"jobs\.eu\.lever\.co/([\w.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?([\w-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)")),
    ("recruitee", re.compile(r"([\w-]+)\.recruitee\.com")),
    ("personio", re.compile(r"([\w-]+)\.jobs\.personio\.(?:de|com)")),
    ("workday", _WORKDAY_URL),
    ("icims", re.compile(r"(https://[\w-]+\.icims\.com)")),
    ("teamtailor", re.compile(r"(https://[\w-]+\.teamtailor\.com)")),
    ("bamboohr", re.compile(r"([\w-]+)\.bamboohr\.com")),
    ("pinpoint", re.compile(r"([\w-]+)\.pinpointhq\.com")),
]  # fmt: skip
# Path segments or subdomains that are part of the ATS's own site, never a company's board.
_NOT_A_BOARD = {
    "embed", "v1", "j", "api", "jobs", "job", "static", "assets", "widget", "careers", "www",
    "app", "cdn", "login", "wday", "js", "css", "images", "privacy", "terms", "https", "http",
    "resources", "help", "support", "status", "marketplace", "partners", "blog", "developers",
}  # fmt: skip


def detect_boards(
    page_html: str, company: str, origin: str | None = None, page_url: str | None = None
) -> list[CompanyBoard]:
    """Every ATS board a page links to or embeds, in detection order. `page_url` lets a
    Teamtailor site on the company's own domain (it loads teamtailor-cdn assets) be found."""
    text = html.unescape(page_html)
    found: dict[str, CompanyBoard] = {}
    if page_url and "teamtailor-cdn.com" in text:
        parts = urlsplit(page_url)
        own = CompanyBoard(
            name=company, ats="teamtailor", url=f"{parts.scheme}://{parts.netloc}", origin=origin
        )
        found[own.key] = own
    for ats, pattern in _DETECT:
        for m in pattern.finditer(text):
            if ats == "workday":
                if m["site"].lower() in _NOT_A_BOARD:
                    continue
                board = CompanyBoard(name=company, ats=ats, url=m.group(0), origin=origin)
            elif ats in ("icims", "teamtailor"):
                sub = urlsplit(m.group(1)).netloc.split(".")[0]
                if sub in _NOT_A_BOARD:
                    continue
                board = CompanyBoard(name=company, ats=ats, url=m.group(1), origin=origin)
            else:
                token = m.group(1).strip(".")
                if not token or token.lower() in _NOT_A_BOARD:
                    continue
                board = CompanyBoard(name=company, ats=ats, token=token, origin=origin)
            found.setdefault(board.key, board)
    return list(found.values())


# ------------------------------------------------------------------ the source


class CompanyCareersSource:
    """Fetches every company in the watch-list via its ATS feed or careers page."""

    name = "company"

    def __init__(
        self,
        companies: list[CompanyBoard] | None = None,
        http: HttpFetcher | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        path = self.settings.companies_path
        if companies is None:  # before the app's first discovery pass there is no list yet
            companies = load_companies(path) if path.exists() else []
        self.companies = companies
        self.errors: dict[str, str] = {}

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        if not self.companies:
            raise SourceError("No company job boards found yet")
        boards: dict[str, CompanyBoard] = {}
        skip = set(query.exclude_boards)
        for c in self.companies:
            if c.key not in skip:
                boards.setdefault(c.key, c)  # a board shared by two entries is read once
        if not boards:
            raise SourceError("Every company job board is switched off")
        # Each board runs in the caller's context, so a stopped search stops its requests.
        runs = [(contextvars.copy_context(), c) for c in boards.values()]
        with ThreadPoolExecutor(max_workers=self.settings.company_workers) as pool:
            results = list(pool.map(lambda r: r[0].run(self._fetch_one, r[1], query), runs))
        jobs = [j for found in results for j in found]
        jobs = [j for j in jobs if query.is_loosely_relevant(j) and query.is_recent(j)]
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.limit]

    def fetch_board(self, company: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        """One board's postings; raises SourceError (used by discovery to verify a board)."""
        try:
            jobs: list[JobPosting] = getattr(self, f"_{company.ats}")(company, query)
        except (KeyError, ValueError, TypeError, ElementTree.ParseError) as exc:
            raise SourceError(f"unexpected {company.ats} response: {exc}") from exc
        return jobs

    def _fetch_one(self, company: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        try:
            return self.fetch_board(company, query)
        except SourceError as exc:
            self.errors[company.name] = str(exc)  # One broken board must not stop the run.
            return []

    def _shortlist(self, query: SearchQuery, listed: dict[str, str]) -> list[str]:
        """Keys of listed postings worth opening: titles sharing a role word with the search
        titles (so a pharma's HR or finance roles are never opened), at most N."""
        by_title = SearchQuery(titles=query.titles)
        keep = [
            key
            for key, title in listed.items()
            if by_title.is_loosely_relevant(JobPosting(id=key, title=title or "?", company="?"))
        ]
        return keep[: self.settings.company_max_details]

    # -------------------------------------------------------------- public ATS feeds

    def _greenhouse(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{c.token}/jobs"
        data = self.http.get(url, params={"content": "true"}).json()
        out = []
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name")
            text = html_to_text(j.get("content", ""))
            out.append(
                JobPosting(
                    id=f"greenhouse:{c.token}:{j['id']}",
                    title=j.get("title", ""),
                    company=c.name,
                    location=loc,
                    work_arrangement=infer_arrangement(j.get("title"), loc, text[:500]),
                    description=text,
                    url=j.get("absolute_url"),
                    posted_at=parse_date(j.get("updated_at")),
                    source=f"greenhouse:{c.token}",
                )
            )
        return out

    def _lever_eu(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        return self._lever(c, query, api="https://api.eu.lever.co")

    def _lever(
        self, c: CompanyBoard, query: SearchQuery, api: str = "https://api.lever.co"
    ) -> list[JobPosting]:
        data = self.http.get(f"{api}/v0/postings/{c.token}", params={"mode": "json"}).json()
        out = []
        for j in data:
            cats = j.get("categories") or {}
            wt = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite"}.get(
                str(j.get("workplaceType", "")).lower()
            )
            text = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
            lists = " ".join(html_to_text(x.get("content", "")) for x in j.get("lists", []))
            arrangement: WorkArrangement = wt or infer_arrangement(cats.get("location"), text[:500])  # type: ignore[assignment]
            out.append(
                JobPosting(
                    id=f"lever:{c.token}:{j['id']}",
                    title=j.get("text", ""),
                    company=c.name,
                    location=cats.get("location"),
                    work_arrangement=arrangement,
                    description=f"{text}\n{lists}".strip(),
                    url=j.get("hostedUrl"),
                    posted_at=parse_date(j.get("createdAt")),
                    source=f"lever:{c.token}",
                )
            )
        return out

    def _ashby(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        data = self.http.get(f"https://api.ashbyhq.com/posting-api/job-board/{c.token}").json()
        out = []
        for j in data.get("jobs", []):
            wt = str(j.get("workplaceType") or "").lower()
            arrangement: WorkArrangement = (
                "remote"
                if j.get("isRemote") or wt == "remote"
                else "hybrid"
                if wt == "hybrid"
                else "onsite"
            )
            out.append(
                JobPosting(
                    id=f"ashby:{c.token}:{j['id']}",
                    title=j.get("title", ""),
                    company=c.name,
                    location=j.get("location"),
                    work_arrangement=arrangement,
                    description=j.get("descriptionPlain")
                    or html_to_text(j.get("descriptionHtml", "")),
                    url=j.get("jobUrl"),
                    posted_at=parse_date(j.get("publishedAt")),
                    source=f"ashby:{c.token}",
                )
            )
        return out

    def _workable(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        data = self.http.get(
            f"https://apply.workable.com/api/v1/widget/accounts/{c.token}",
            params={"details": "true"},
        ).json()
        out = []
        for j in data.get("jobs", []):
            loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x) or None
            text = html_to_text(j.get("description") or "")
            arrangement: WorkArrangement = (
                "remote" if j.get("telecommuting") else infer_arrangement(j.get("title"), loc)
            )
            out.append(
                JobPosting(
                    id=f"workable:{c.token}:{j['shortcode']}",
                    title=j.get("title", ""),
                    company=c.name,
                    location=loc,
                    work_arrangement=arrangement,
                    description=text,
                    url=j.get("url"),
                    posted_at=parse_date(j.get("published_on") or j.get("created_at")),
                    source=f"workable:{c.token}",
                )
            )
        return out

    def _recruitee(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        data = self.http.get(f"https://{c.token}.recruitee.com/api/offers/").json()
        out = []
        for j in data.get("offers", []):
            loc = j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
            text = html_to_text(f"{j.get('description') or ''}\n{j.get('requirements') or ''}")
            arrangement: WorkArrangement = (
                "remote"
                if j.get("remote")
                else "hybrid"
                if j.get("hybrid")
                else infer_arrangement(j.get("title"), loc)
            )
            out.append(
                JobPosting(
                    id=f"recruitee:{c.token}:{j['id']}",
                    title=j.get("title", ""),
                    company=c.name,
                    location=loc or None,
                    work_arrangement=arrangement,
                    description=text,
                    url=j.get("careers_url"),
                    posted_at=parse_date(j.get("published_at")),
                    source=f"recruitee:{c.token}",
                )
            )
        return out

    def _personio(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        resp = self.http.get(f"https://{c.token}.jobs.personio.de/xml")
        out = []
        for pos in ElementTree.fromstring(resp.content).iter("position"):
            ident = (pos.findtext("id") or "").strip()
            offices = [
                pos.findtext("office"),
                *(o.text for o in pos.iterfind("additionalOffices/office")),
            ]
            loc = ", ".join(o.strip() for o in offices if o and o.strip()) or None
            text = "\n\n".join(
                f"{d.findtext('name') or ''}\n{html_to_text(d.findtext('value') or '')}".strip()
                for d in pos.iter("jobDescription")
            )
            title = (pos.findtext("name") or "").strip()
            out.append(
                JobPosting(
                    id=f"personio:{c.token}:{ident}",
                    title=title,
                    company=c.name,
                    location=loc,
                    work_arrangement=infer_arrangement(title, loc, text[:500]),
                    description=text,
                    url=f"https://{c.token}.jobs.personio.de/job/{ident}",
                    posted_at=parse_date(pos.findtext("createdAt")),
                    source=f"personio:{c.token}",
                )
            )
        return out

    def _smartrecruiters(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        api = f"https://api.smartrecruiters.com/v1/companies/{c.token}/postings"
        params: dict[str, Any] = {"limit": 100}
        if cc := country_code(query.country):
            params["country"] = cc  # server-side, so other countries' jobs are never opened
        listed: dict[str, str] = {}
        since = query.posted_since()
        for term in query.search_terms():
            data = self.http.get(api, params={**params, **({"q": term} if term else {})}).json()
            for p in data.get("content", []):
                released = parse_date(p.get("releasedDate"))
                if since is None or released is None or released >= since:
                    listed.setdefault(str(p["id"]), p.get("name", ""))
        return [self._smartrecruiters_job(c, api, pid) for pid in self._shortlist(query, listed)]

    def _smartrecruiters_job(self, c: CompanyBoard, api: str, pid: str) -> JobPosting:
        j = self.http.get(f"{api}/{pid}").json()
        sections = (j.get("jobAd") or {}).get("sections") or {}
        text = "\n\n".join(
            html_to_text(sections[k].get("text") or "")
            for k in ("jobDescription", "qualifications", "additionalInformation")
            if sections.get(k)
        )
        loc = j.get("location") or {}
        arrangement: WorkArrangement = (
            "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "onsite"
        )
        return JobPosting(
            id=f"smartrecruiters:{c.token}:{pid}",
            title=j.get("name", ""),
            company=c.name,
            location=loc.get("fullLocation"),
            work_arrangement=arrangement,
            description=text,
            url=j.get("postingUrl"),
            posted_at=parse_date(j.get("releasedDate")),
            source=f"smartrecruiters:{c.token}",
        )

    def _bamboohr(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        """BambooHR's careers-site JSON (the list its own page loads), within robots.txt.
        Endpoints and fields checked against live boards (2026-10-02)."""
        base = f"https://{c.token}.bamboohr.com/careers"
        data = self.http.get(f"{base}/list", check_robots=True).json()
        listed = {str(j["id"]): j.get("jobOpeningName", "") for j in data.get("result", [])}
        return [self._bamboohr_job(c, base, jid) for jid in self._shortlist(query, listed)]

    def _bamboohr_job(self, c: CompanyBoard, base: str, jid: str) -> JobPosting:
        j = self.http.get(f"{base}/{jid}/detail", check_robots=True).json()["result"]["jobOpening"]
        # `atsLocation` often comes back with every field null: take each part from either.
        both = [j.get("location") or {}, j.get("atsLocation") or {}]
        parts = [
            next((str(d[k]) for d in both for k in keys if d.get(k)), "")
            for keys in (("city",), ("state",), ("addressCountry", "country"))
        ]
        place = ", ".join(p for p in parts if p) or None
        title = j.get("jobOpeningName", "")
        text = html_to_text(j.get("description") or "")
        arrangement: WorkArrangement = (
            "remote" if j.get("isRemote") else infer_arrangement(title, place, text[:500])
        )
        return JobPosting(
            id=f"bamboohr:{c.token}:{jid}",
            title=title,
            company=c.name,
            location=place,
            work_arrangement=arrangement,
            description=text,
            url=j.get("jobOpeningShareUrl") or f"{base}/{jid}",
            posted_at=parse_date(j.get("datePosted")),
            source=f"bamboohr:{c.token}",
        )

    def _pinpoint(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        """Pinpoint's public postings feed, within robots.txt. Field mapping from public
        examples, not verified against a live board."""
        url = f"https://{c.token}.pinpointhq.com/postings.json"
        data = self.http.get(url, check_robots=True).json()
        out = []
        for j in data.get("data", []):
            loc = j.get("location") or {}
            place = loc.get("name") or loc.get("city")
            title = j.get("title", "")
            parts = ("description", "key_responsibilities", "skills_knowledge_expertise")
            text = html_to_text("\n".join(str(j.get(k) or "") for k in parts))
            workplace = str(j.get("workplace_type") or "").lower().replace("-", "_")
            arrangement: WorkArrangement = {
                "remote": "remote",
                "hybrid": "hybrid",
                "onsite": "onsite",
                "on_site": "onsite",
            }.get(workplace) or infer_arrangement(title, place, text[:500])  # type: ignore[assignment]
            out.append(
                JobPosting(
                    id=f"pinpoint:{c.token}:{j['id']}",
                    title=title,
                    company=c.name,
                    location=place,
                    work_arrangement=arrangement,
                    description=text,
                    url=j.get("url"),
                    closes_at=parse_date(j.get("deadline_at")),
                    source=f"pinpoint:{c.token}",
                )
            )
        return out

    def _teamtailor(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        parts = urlsplit(c.url or "")
        base = f"{parts.scheme}://{parts.netloc}"
        feed = self.http.get(f"{base}/jobs.rss", check_robots=True)
        out = []
        for item in ElementTree.fromstring(feed.content).iter("item"):
            link = (item.findtext("link") or "").strip()
            ident = re.search(r"/jobs/(\d+)", link)
            places = [
                ", ".join(
                    x
                    for x in (loc.findtext(f"{{{_TT}}}city"), loc.findtext(f"{{{_TT}}}country"))
                    if x
                )
                for loc in item.iter(f"{{{_TT}}}location")
            ]
            location = "; ".join(p for p in places if p) or None
            title = (item.findtext("title") or "").strip()
            status = (item.findtext("remoteStatus") or "").lower()
            arrangement: WorkArrangement = (
                "remote" if status in ("fully", "remote") else "hybrid" if status == "hybrid"
                else infer_arrangement(title, location)
            )  # fmt: skip
            out.append(
                JobPosting(
                    id=f"teamtailor:{parts.netloc}:{ident[1] if ident else item.findtext('guid')}",
                    title=title,
                    company=c.name,
                    location=location,
                    work_arrangement=arrangement,
                    description=html_to_text(item.findtext("description") or ""),
                    url=link or None,
                    posted_at=_rfc822_date(item.findtext("pubDate")),
                    source=f"teamtailor:{parts.netloc}",
                )
            )
        return out

    # -------------------------------------------------------------- careers-site endpoints

    def _workday(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        m = _WORKDAY_URL.match(c.url or "")
        assert m is not None  # checked by CompanyBoard
        api = (
            f"https://{m['tenant']}.{m['dc']}.myworkdayjobs.com/wday/cxs/{m['tenant']}/{m['site']}"
        )
        places = country_names(query.country) or [p.lower() for p in query.locations]
        facets: dict[str, list[str]] | None = {}
        if places:
            first = self.http.post(
                f"{api}/jobs", json=_workday_body({}, "", 0, 1), check_robots=True
            ).json()
            facets = workday_location_facets(first.get("facets") or [], places)
        if facets is None:
            return []  # the board's country filter has no entry for the searched country
        listed: dict[str, str] = {}
        terms = query.search_terms()
        per_term = max(WORKDAY_PAGE, WORKDAY_MAX_LISTED // len(terms))  # 16 CV terms: 1 page each
        for term in terms:
            listed.update(self._workday_list(api, facets, term, query.posted_within_days, per_term))
        return [
            self._workday_job(c, api, m["tenant"], path, places)
            for path in self._shortlist(query, listed)
        ]

    def _workday_list(
        self,
        api: str,
        facets: dict[str, list[str]],
        term: str,
        max_days: int | None = None,
        max_listed: int = WORKDAY_MAX_LISTED,
    ) -> dict[str, str]:
        """externalPath -> title for one search term, paging up to WORKDAY_MAX_LISTED and
        skipping postings older than `max_days` (their "Posted 3 Days Ago" label)."""
        listed: dict[str, str] = {}
        total = max_listed
        for offset in range(0, max_listed, WORKDAY_PAGE):
            body = _workday_body(facets, term, offset, WORKDAY_PAGE)
            data = self.http.post(f"{api}/jobs", json=body, check_robots=True).json()
            if offset == 0:
                total = int(data.get("total") or 0)  # later pages report 0
            page = data.get("jobPostings") or []
            listed.update(
                {
                    p["externalPath"]: p.get("title", "")
                    for p in page
                    if max_days is None or workday_age_days(p.get("postedOn")) <= max_days
                }
            )
            if len(page) < WORKDAY_PAGE or offset + WORKDAY_PAGE >= total:
                break
        return listed

    def _workday_job(
        self, c: CompanyBoard, api: str, tenant: str, path: str, places: list[str]
    ) -> JobPosting:
        info = self.http.get(f"{api}{path}", check_robots=True).json()["jobPostingInfo"]
        text = html_to_text(info.get("jobDescription") or "")
        locations = [info.get("location"), *(info.get("additionalLocations") or [])]
        location = _pick_location([str(x) for x in locations if x], places)
        return JobPosting(
            id=f"workday:{tenant}:{info.get('jobReqId') or info['id']}",
            title=info.get("title", ""),
            company=c.name,
            location=location,
            work_arrangement=infer_arrangement(info.get("remoteType"), info.get("title"), location),
            description=text,
            url=info.get("externalUrl"),
            posted_at=parse_date(info.get("startDate")),
            source=f"workday:{tenant}",
        )

    def _icims(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        parts = urlsplit(c.url or "")
        base = f"{parts.scheme}://{parts.netloc}"
        listed: dict[str, str] = {}
        for term in query.search_terms():
            for page in range(ICIMS_MAX_PAGES):
                params = {"ss": 1, "searchKeyword": term, "in_iframe": 1, "pr": page}
                body = self.http.get(f"{base}/jobs/search", params=params, check_robots=True).text
                found = {m["url"]: html.unescape(m["title"]) for m in _ICIMS_LINK.finditer(body)}
                if not found.keys() - listed.keys():
                    break
                listed.update(found)
        jobs = []
        for url in self._shortlist(query, listed):
            page_html = self.http.get(url, params={"in_iframe": 1}, check_robots=True).text
            for node in extract_jsonld_jobs(page_html)[:1]:
                job = posting_from_jsonld(node, source=f"icims:{parts.netloc}", url=url)
                jobs.append(job.model_copy(update={"company": c.name, "url": url}))
        return jobs

    def _careers_page(self, c: CompanyBoard, query: SearchQuery) -> list[JobPosting]:
        assert c.url is not None
        page = self.http.get(c.url, check_robots=True).text
        nodes: list[dict[str, Any]] = extract_jsonld_jobs(page)
        if not nodes:
            raise SourceError(f"no schema.org JobPosting found at {c.url}; add the ATS instead")
        jobs = [posting_from_jsonld(n, source=f"careers:{c.name}", url=c.url) for n in nodes]
        return [
            j.model_copy(update={"company": c.name}) if j.company == "unknown" else j for j in jobs
        ]


def _workday_body(
    facets: dict[str, list[str]], term: str, offset: int, limit: int
) -> dict[str, Any]:
    return {"appliedFacets": facets, "limit": limit, "offset": offset, "searchText": term}


def _leaf_facets(facets: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """(facetParameter, values) for every facet; Workday nests some under group facets."""
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for f in facets:
        values = f.get("values") or []
        nested = [v for v in values if "facetParameter" in v]
        out += _leaf_facets(nested)
        leaves = [v for v in values if "facetParameter" not in v and "id" in v]
        if leaves:
            out.append((str(f.get("facetParameter", "")), leaves))
    return out


def workday_location_facets(
    facets: list[dict[str, Any]], places: list[str]
) -> dict[str, list[str]] | None:
    """Workday filter selecting `places`. Each tenant names its location facets differently
    (Location_Country, locationCountry, locations, primarylocation), so match value labels.
    {} = no usable facet (search unfiltered); None = a country facet exists but lacks the place.
    """
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, places)) + r")\b", re.IGNORECASE)
    best: tuple[str, list[str]] | None = None
    country_facet = False
    for param, values in _leaf_facets(facets):
        low = param.lower()
        if "location" not in low and "country" not in low:
            continue
        ids = [str(v["id"]) for v in values if pattern.search(str(v.get("descriptor", "")))]
        if "country" in low:
            country_facet = True
            if ids:
                return {param: ids}
        elif ids and (best is None or len(ids) > len(best[1])):
            best = (param, ids)
    if best:
        return {best[0]: best[1]}
    return None if country_facet else {}


def _pick_location(locations: list[str], places: list[str]) -> str | None:
    """The posting's location that is in the searched area, else its primary location."""
    if places:
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, places)) + r")\b", re.IGNORECASE)
        for loc in locations:
            if pattern.search(loc):
                return loc
    return locations[0] if locations else None


_TT = "https://teamtailor.com/locations"  # namespace of Teamtailor's RSS location fields
_POSTED_AGO = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def workday_age_days(label: str | None) -> int:
    """Days since posting from Workday's list label ("Posted Today", "Posted 30+ Days Ago").
    Unreadable labels count as new, so a posting is never skipped on a guess."""
    low = (label or "").lower()
    if "yesterday" in low:
        return 1
    m = _POSTED_AGO.search(low)
    if m:
        return int(m[1]) + (1 if "+" in low else 0)  # "30+" means more than 30
    return 0


def _rfc822_date(value: str | None) -> date | None:
    try:
        return parsedate_to_datetime(value).date() if value else None
    except (TypeError, ValueError):
        return None
