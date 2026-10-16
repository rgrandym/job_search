"""Focused lookup of one posting online, from clues supplied by the user.

For a job the user found on their own: the posting's link is read directly, once, even where
robots.txt turns automated readers away (the user's own request for one page, user's decision
2026-10-15); otherwise one web
search (DuckDuckGo's HTML endpoint, allowed by its robots.txt) finds the posting's pages, which
are read in parallel (JSON-LD, robots.txt kept; with an ID, only pages that show it count).
Only when the web finds nothing are the named employer's own job feed and the fast board
APIs searched. No LinkedIn search, no waiting out rate limits: one job should take seconds.
No profile, screening or match score is involved: results are candidate postings for the
user to save for CV and letter writing. Jobs already in the app are found in the app.
"""

from __future__ import annotations

import contextvars
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from difflib import SequenceMatcher
from typing import NamedTuple
from urllib.parse import parse_qs, urljoin, urlsplit

from pydantic import BaseModel, Field

from src.core import progress
from src.core.config import Settings
from src.jobs.fetcher import build_sources, dedupe, fetch_all
from src.jobs.models import JobPosting, SavedJob, SearchQuery
from src.jobs.sources.base import (
    HttpFetcher,
    SourceError,
    extract_jsonld_jobs,
    html_to_text,
    posting_from_jsonld,
    stable_id,
)
from src.jobs.sources.companies import CompanyCareersSource, load_companies
from src.jobs.sources.job_boards import REED_DETAILS, ReedSource
from src.services import saved, search_service
from src.services.workspace import Workspace
from src.tools.search_tools import company_key, same_employer, title_core

SOURCES = ["reed", "adzuna"]  # fast APIs, only when the web search finds nothing
WEB_SEARCH = "https://html.duckduckgo.com/html/"
MAX_RESULTS = 8  # result pages read, in parallel
PAGE_WORKERS = 6
BOARD_LIMIT = 50
_RESULT = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SIGNED_IN_HOSTS = ("linkedin.", "indeed.")  # read through their own readers, never as pages
MAX_OPENED = 5
QUICK_BUDGET_S = 15.0
FEED_DETAILS = 8  # employer postings opened for one job (a search opens more)
DATE_SLACK_DAYS = 3
_LINKEDIN_VIEW = re.compile(r"linkedin\.com/jobs/view/(?:[^/?#]*-)?(\d{6,})", re.I)
_COMMON = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "for",
    "from",
    "have",
    "into",
    "job",
    "role",
    "that",
    "the",
    "their",
    "this",
    "with",
    "will",
    "work",
    "your",
}


class OneJobQuery(BaseModel):
    title: str = ""
    company: str = ""
    job_id: str = ""
    keywords: str = ""
    description: str = ""
    posted_at: date | None = None
    url: str = ""


class OneJobResults(BaseModel):
    jobs: list[JobPosting] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


def _clues(description: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9-]{4,}", description.casefold())
    return list(dict.fromkeys(word for word in words if word not in _COMMON))[:4]


def _terms(query: OneJobQuery) -> list[str]:
    return [term.strip().casefold() for term in query.keywords.split(",") if term.strip()]


def _title_score(query: OneJobQuery, job: JobPosting) -> float:
    """0 when the titles name different roles; 1 for the same words."""
    if not query.title.strip():
        return 1.0
    wanted, actual = title_core(query.title), title_core(job.title)
    if not wanted or not actual:
        return 0.0
    ratio = SequenceMatcher(None, sorted(wanted), sorted(actual)).ratio()
    if wanted <= actual or actual <= wanted:
        return max(ratio, 0.8)
    return ratio if ratio >= 0.8 else 0.0


def _employer(typed: str, company: str) -> bool:
    """`same_employer`, also when the user spaces the name differently ("Astra Zeneca")."""
    squashed = company_key(typed).replace(" ", "") == company_key(company).replace(" ", "")
    return same_employer(typed, company) or squashed


def _has_id(job: JobPosting, job_id: str) -> bool:
    """The posting carries the ID: as its own id, in its link, or in its text (a board's copy
    of an employer posting quotes the employer's requisition number)."""
    needle = job_id.casefold().strip()
    number = needle.split(":")[-1]
    if needle == job.id.casefold() or number == job.id.casefold().split(":")[-1]:
        return True
    pattern = rf"(?<![\w]){re.escape(number)}(?![\w])"
    return bool(number and re.search(pattern, f"{job.url or ''} {job.description}", re.I))


def _identifies(job: JobPosting, query: OneJobQuery) -> bool:
    """The clues that name a posting: ID, title, employer and link. All given must agree."""
    if query.job_id.strip() and not _has_id(job, query.job_id):
        return False
    if not _title_score(query, job):
        return False
    if query.company.strip() and not _employer(query.company, job.company):
        return False
    url = query.url.strip()
    return not url or (job.url or "").split("?")[0].rstrip("/") == url.split("?")[0].rstrip("/")


def _clue_hits(job: JobPosting, query: OneJobQuery) -> int:
    """Keywords and description words the posting's text contains (a ranking signal: a
    snippet-only posting may not show them yet)."""
    text = job.to_text().casefold()
    return sum(term in text for term in [*_terms(query), *_clues(query.description)])


def _date_ok(job: JobPosting, query: OneJobQuery) -> bool:
    """Boards date the same posting a day or two apart; an undated posting is not ruled out."""
    if not query.posted_at or not job.posted_at:
        return True
    return abs((job.posted_at - query.posted_at).days) <= DATE_SLACK_DAYS


def _quick(ws: Workspace) -> Settings:
    """Settings for reading one posting: a refusal ends the attempt at once (no waiting out
    rate limits, as a long search does) and opening stops after `QUICK_BUDGET_S`."""
    return ws.settings.model_copy(
        update={"linkedin_max_cooldowns": 0, "enrich_budget_s": QUICK_BUDGET_S}
    )


def _public_url(url: str) -> bool:
    """Do not use an outside-posting link to contact a local-network host."""
    from ipaddress import ip_address

    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".")
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
    ):
        return False
    try:
        return ip_address(host).is_global
    except ValueError:
        return True  # a public hostname; the normal source robots check still applies


def _read_public_page(ws: Workspace, url: str, check_robots: bool = True) -> str:
    """Follow only public redirects. Pages are subject to robots.txt, except the posting link
    the user gave (`check_robots=False`): one page they asked for, read once."""
    http = HttpFetcher(ws.settings)
    for _ in range(5):
        if not _public_url(url):
            raise SourceError("Posting link redirects to a local or invalid address")
        response = http.get(url, check_robots=check_robots, follow_redirects=False)
        if not response.is_redirect:
            return response.text
        location = response.headers.get("location")
        if not location:
            raise SourceError("Posting link redirected without a destination")
        url = urljoin(url, location)
    raise SourceError("Posting link redirected too many times")


def _from_link(ws: Workspace, query: OneJobQuery, errors: dict[str, str]) -> list[JobPosting]:
    """The posting at the user's link: its JSON-LD, else the board's own reader or headless
    Chrome (the same readers a search uses for thin postings)."""
    url = query.url.strip()
    linkedin = _LINKEDIN_VIEW.search(url)
    if not linkedin:
        try:
            nodes = extract_jsonld_jobs(_read_public_page(ws, url, check_robots=False))
        except SourceError as exc:
            errors["link"] = str(exc)
            nodes = []
        host = (urlsplit(url).hostname or "unknown").removeprefix("www.")
        jobs = [_named(posting_from_jsonld(node, source="manual", url=url), query, host, url)
                for node in nodes]  # fmt: skip
        if jobs or "local" in errors.get("link", ""):
            return jobs
    stub = JobPosting(
        id=f"linkedin:{linkedin[1]}" if linkedin else f"manual:{stable_id(url)}",
        title=query.title.strip() or "?",  # "?": LinkedIn's reader names it from the page
        company=query.company.strip() or "?",
        url=url,
        source="linkedin_search" if linkedin else "manual",
    )
    read = search_service._enrich([stub], [], errors, _quick(ws)).get(stub.id)
    if read is None:
        errors["link"] = _unreadable_link(errors)
        return []
    errors.pop("link", None)
    host = (urlsplit(url).hostname or "unknown").removeprefix("www.")
    names = {"title": "Job posting" if read.title == "?" else read.title}
    names["company"] = host if read.company == "?" else read.company
    return [read.model_copy(update=names)]


def _named(job: JobPosting, query: OneJobQuery, host: str, url: str) -> JobPosting:
    """A posting from the user's link, applied for there; a page that leaves out the employer
    is named by the company the user typed, else the site."""
    company = job.company if not _unnamed(job.company) else query.company.strip() or host
    return job.model_copy(update={"url": url, "company": company})


def _unreadable_link(errors: dict[str, str]) -> str:
    """Why the link gave no posting; a robots.txt refusal replaces the reader's own note."""
    refused = [k for k, v in errors.items() if k.startswith("enrich ") and "robots.txt" in v]
    for key in refused:
        del errors[key]
    if refused:
        return (
            "this site does not allow automated reading of its pages (its robots.txt), so "
            "the app cannot open it; open the link and paste the job description"
        )
    return "no readable posting at that link; open it and paste the job description"


class _Result(NamedTuple):
    url: str
    title: str


def _web_results(ws: Workspace, query: OneJobQuery) -> list[_Result]:
    """One web search for the posting: its ID, title and employer as typed."""
    terms = [query.job_id.strip(), query.title.strip(), query.company.strip()]
    phrase = " ".join(t for t in terms if t) or " ".join(_terms(query) or _clues(query.description))
    if not query.job_id.strip():
        phrase += " job"
    page = HttpFetcher(ws.settings).post(WEB_SEARCH, data={"q": phrase}, check_robots=True).text
    out: list[_Result] = []
    for href, label in _RESULT.findall(page):
        link = html.unescape(href)
        url = parse_qs(urlsplit(link).query).get("uddg", [link])[0]
        url = f"https:{url}" if url.startswith("//") else url
        if _public_url(url) and "duckduckgo.com" not in (urlsplit(url).hostname or ""):
            out.append(_Result(url, html_to_text(label)))
    if not out and "anomaly" in page.casefold():
        raise SourceError("The web search asked to slow down; try again in a minute")
    return out[:MAX_RESULTS]


def _title_for_id(query: OneJobQuery, results: list[_Result]) -> str:
    """With only an ID, the title of the result whose link carries it."""
    job_id = query.job_id.strip()
    if not job_id or query.title.strip():
        return ""
    pattern = rf"(?<![\w]){re.escape(job_id)}(?![\w])"
    for result in results:
        if re.search(pattern, result.url, re.I) and _result_title(result) != "?":
            return _result_title(result)
    return ""


def _signed_in_host(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return any(part in host for part in _SIGNED_IN_HOSTS)


def _read_result(ws: Workspace, result: _Result, job_id: str) -> list[JobPosting]:
    """The postings a result page holds (JSON-LD); with an ID, only if the page shows it."""
    try:
        page = _read_public_page(ws, result.url)
    except SourceError:
        return []
    if job_id and not re.search(rf"(?<![\w]){re.escape(job_id)}(?![\w])", page, re.I):
        return []
    ident = f"web:{job_id}" if job_id else f"web:{stable_id(result.url)}"
    return [
        posting_from_jsonld(node, source="web", url=result.url).model_copy(update={"id": ident})
        for node in extract_jsonld_jobs(page)
    ]


def _web_candidates(ws: Workspace, query: OneJobQuery) -> tuple[list[JobPosting], list[_Result]]:
    """Search the web, read the result pages in parallel, and keep the employer's own page as
    the posting's link when it was among the results."""
    progress.step("Searching the web for this posting")
    results = _web_results(ws, query)
    named = _title_for_id(query, results)
    if named:  # an ID alone finds the employer's page; its title also finds readable copies
        progress.step(f"Found “{named}”; searching for its full posting")
        query = query.model_copy(update={"title": named})
        results = _web_results(ws, query) + results
    readable = [r for r in results if not _signed_in_host(r.url)]
    job_id = query.job_id.strip()
    progress.step(f"Reading {len(readable)} result page(s)")
    runs = [(contextvars.copy_context(), r) for r in readable]
    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
        pages = list(pool.map(lambda run: run[0].run(_read_result, ws, run[1], job_id), runs))
    found = [job for jobs in pages for job in jobs]
    if not any(_identifies(job, query) for job in found):
        found += _unreadable_result(ws, query, results)
    return found, results


def _unreadable_result(
    ws: Workspace, query: OneJobQuery, results: list[_Result]
) -> list[JobPosting]:
    """No readable page: the top result that names the job, opened by the same readers a
    search uses for thin postings (LinkedIn's own reader, else headless Chrome)."""
    errors: dict[str, str] = {}
    for result in results[:3]:
        linkedin = _LINKEDIN_VIEW.search(result.url)
        if _signed_in_host(result.url) and not linkedin:
            continue  # a board's search listing, not a posting
        title = result.title.split(" | ")[0].split(" - ")[0].strip()
        stub = JobPosting(
            id=f"linkedin:{linkedin[1]}"
            if linkedin
            else f"web:{query.job_id.strip() or stable_id(result.url)}",
            title=query.title.strip() or title or "?",
            company=query.company.strip() or "?",
            url=result.url,
            source="linkedin_search" if linkedin else "web",
        )
        if not _title_score(query, stub):
            continue
        progress.step(f"Opening {urlsplit(result.url).hostname}")
        read = search_service._enrich([stub], [], errors, _quick(ws)).get(stub.id)
        if read is not None:
            host = (urlsplit(result.url).hostname or "?").removeprefix("www.")
            return [read.model_copy(update={"company": host}) if read.company == "?" else read]
    return []


def _employer_link(job: JobPosting, results: list[_Result]) -> JobPosting:
    """A posting read from an aggregator's copy is applied for on the employer's own site
    when the search found that page (its title names the same job)."""
    if job.source != "web":
        return job
    key = company_key(job.company).replace(" ", "")
    probe = OneJobQuery(title=job.title)
    for result in results:
        host = (urlsplit(result.url).hostname or "").replace("-", "")
        same = _title_score(probe, JobPosting(id="?", title=_result_title(result), company="?"))
        if len(key) > 2 and key in host and same and not _signed_in_host(result.url):
            return job.model_copy(update={"url": result.url})
    return job


def _result_title(result: _Result) -> str:
    """ "Senior Program Manager R&D at Lonza | LinkedIn" -> the job title."""
    return re.split(r"\s(?:at|[|–—-])\s", result.title)[0].strip() or "?"


def _reed_by_id(ws: Workspace, job_id: str, errors: dict[str, str]) -> list[JobPosting]:
    try:
        source = ReedSource(settings=ws.settings)
        data = source.http.get(REED_DETAILS.format(id=job_id.split(":")[-1]), auth=source._auth)
        return [source._to_posting(data.json())]
    except (SourceError, ValueError, KeyError) as exc:
        errors["reed"] = str(exc)
        return []


def _search_query(query: OneJobQuery, *, with_company: bool) -> SearchQuery:
    """Title plus employer as one board search ("Scientist AstraZeneca"), so the employer's
    posting is not lost among every other employer's postings with that title."""
    title = query.title.strip()
    keywords = [query.company.strip()] if with_company and query.company.strip() else []
    keywords += _terms(query) if not title else []
    if not title and not keywords:
        keywords = _clues(query.description)[:3] or [query.job_id.strip()]
    return SearchQuery(titles=[title] if title else [], keywords=keywords, limit=BOARD_LIMIT)


def _company_feeds(ws: Workspace, query: OneJobQuery, errors: dict[str, str]) -> list[JobPosting]:
    """The named employer's own ATS feed, when discovery found it (the most reliable route
    to a posting the user saw on the employer's site)."""
    company = query.company.strip()
    path = ws.settings.companies_path
    if not company or not path.exists():
        return []
    boards = [c for c in load_companies(path) if _employer(company, c.name)]
    if not boards:
        return []
    progress.step(f"Reading {boards[0].name}'s own job board")
    settings = ws.settings.model_copy(update={"company_max_details": FEED_DETAILS})
    source = CompanyCareersSource(companies=boards, settings=settings)
    try:
        jobs = source.fetch(_search_query(query, with_company=False))
    except SourceError as exc:
        errors["company"] = str(exc)
        return []
    errors.update({f"company:{k}": v for k, v in source.errors.items()})
    return jobs


def _boards(ws: Workspace, query: OneJobQuery, errors: dict[str, str]) -> list[JobPosting]:
    """The fast board APIs (an unconfigured one is skipped silently)."""
    sources, _skipped = build_sources(SOURCES, settings=ws.settings)
    found, failed = fetch_all(sources, _search_query(query, with_company=True))
    errors.update(failed)
    return found


def _search(
    ws: Workspace, query: OneJobQuery, errors: dict[str, str]
) -> tuple[list[JobPosting], list[_Result]]:
    try:
        return _web_candidates(ws, query)
    except SourceError as exc:
        errors["web"] = str(exc)
        return [], []


def _fallback(ws: Workspace, query: OneJobQuery, errors: dict[str, str]) -> list[JobPosting]:
    """The employer's own feed and the board APIs, side by side."""
    progress.step("Not found on the web; checking the employer's job board and job boards")
    steps = (_company_feeds, _boards)
    runs = [(contextvars.copy_context(), step) for step in steps]
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        found = list(pool.map(lambda run: run[0].run(run[1], ws, query, errors), runs))
    return [job for jobs in found for job in jobs]


def _one_per_posting(jobs: list[JobPosting], results: list[_Result]) -> list[JobPosting]:
    """Aggregators copy one posting under other locations: keep its fullest copy, named by
    the employer another copy (or a result title, "... at Lonza") gives."""
    named = {" ".join(title_core(j.title)): j.company for j in jobs if not _unnamed(j.company)}
    best: dict[tuple[str, str], JobPosting] = {}
    for job in jobs:
        title = " ".join(title_core(job.title))
        if _unnamed(job.company):
            company = named.get(title) or _company_from_results(job, results)
            job = job.model_copy(update={"company": company or job.company})
        key = (company_key(job.company), title)
        if key not in best or len(job.description) > len(best[key].description):
            best[key] = job
    return [_employer_link(job, results) for job in best.values()]


def _unnamed(company: str) -> bool:
    return company.strip().casefold() in ("", "?", "unknown")


def _company_from_results(job: JobPosting, results: list[_Result]) -> str:
    for result in results:
        match = re.search(r"\bat ([^|–—-]+?)(?:\s[|–—-]|$)", result.title)
        probe = JobPosting(id="?", title=_result_title(result), company="?")
        if match and _title_score(OneJobQuery(title=job.title), probe):
            return match[1].strip()
    return ""


def _validate(query: OneJobQuery) -> None:
    clues = (query.title, query.company, query.job_id, query.keywords, query.description)
    if not any(clue.strip() for clue in (*clues, query.url)):
        raise ValueError(
            "Enter a title, company, job ID, keyword, description or link; "
            "a date alone cannot identify a job"
        )
    if query.url.strip() and not _public_url(query.url.strip()):
        raise ValueError("Enter a public http or https posting link")


_ID_IN_LINK = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{0,4}-?\d{4,})(?![A-Za-z0-9])")
_ID_PARAM = re.compile(r"id|no|num|job|vacanc|req|ref", re.I)


def _id_in_link(url: str) -> str:
    """A posting ID in the link: in its path ("/jobs/R79349"), else a query parameter that
    names one ("?vacancyNo=VN887")."""
    parts = urlsplit(url)
    in_path = _ID_IN_LINK.findall(parts.path)
    if in_path:
        return str(in_path[-1])
    for key, values in parse_qs(parts.query).items():
        value = values[0].strip() if values else ""
        named = _ID_PARAM.search(key) and re.fullmatch(r"[A-Za-z0-9-]{3,24}", value)
        if named and re.search(r"\d", value):
            return value
    return ""


def _blocked_link(ws: Workspace, query: OneJobQuery, errors: dict[str, str]) -> list[JobPosting]:
    """A link whose page cannot be read in full (an employer site that blocks automated
    reads): search the web for the posting by the ID in the link, apply on the link."""
    url = query.url.strip()
    job_id = query.job_id.strip() or _id_in_link(url)
    if not job_id and not query.title.strip():
        return []
    clue = job_id or query.title.strip()
    progress.step(f"The page could not be read in full; searching the web for {clue}")
    search = query.model_copy(update={"url": "", "job_id": job_id})
    found = find(ws, search)
    errors.update(found.errors)
    if found.jobs:  # the link's own read failures no longer matter
        for key in [k for k in errors if k == "link" or k.startswith("enrich ")]:
            del errors[key]
    return [job.model_copy(update={"url": url}) for job in found.jobs]


def find(ws: Workspace, query: OneJobQuery) -> OneJobResults:
    """Find a specific posting online without starting a general job search."""
    _validate(query)
    errors: dict[str, str] = {}
    if query.url.strip():
        progress.step("Reading the posting link")
        linked = _from_link(ws, query, errors)
        if any(len(j.description) > search_service.ENRICH_BELOW for j in linked):
            return OneJobResults(jobs=linked[:15], errors=errors)
        found = _blocked_link(ws, query, errors)
        return OneJobResults(jobs=(found or linked)[:15], errors=errors)
    job_id = query.job_id.strip()
    candidates: list[JobPosting] = []
    if re.fullmatch(r"reed:\d+", job_id, re.I):
        candidates += _reed_by_id(ws, job_id, errors)
    found, results = _search(ws, query, errors)
    candidates += found
    if not any(_identifies(job, query) for job in candidates):
        candidates += _fallback(ws, query, errors)
    hits = [j for j in dedupe(candidates) if _identifies(j, query) and _date_ok(j, query)]
    hits = _one_per_posting(hits, results)
    opened = {j.id for j in found}  # the web step read these already
    thin = [
        j for j in hits if len(j.description) <= search_service.ENRICH_BELOW and j.id not in opened
    ][:MAX_OPENED]
    if thin:
        progress.step(f"Opening {len(thin)} posting(s) for their full text")
        read = search_service._enrich(thin, [], errors, _quick(ws))
        hits = [read.get(j.id, j) for j in hits]
    # Keywords and description words narrow the list to the postings showing them; with no
    # title, employer or ID they are all that ties a posting to the search.
    named = any(c.strip() for c in (query.title, query.company, job_id))
    if any(_clue_hits(j, query) for j in hits) or not named:
        hits = [j for j in hits if _clue_hits(j, query) or not (_terms(query) or query.description)]
    hits.sort(
        key=lambda j: (_clue_hits(j, query), _title_score(query, j), len(j.description)),
        reverse=True,
    )
    # A posting that could not be opened is not an unavailable source.
    errors = {k: v for k, v in errors.items() if not k.startswith("enrich ")}
    return OneJobResults(jobs=hits[:15], errors=errors)


def refresh_saved(ws: Workspace, job_id: str, description: str | None = None) -> SavedJob:
    """Read a saved job's full posting again (its link, else the web by title and employer),
    or use the `description` the user pasted; the saved copy is updated with it."""
    item = next((i for i in ws.saved_jobs() if i.result.job.id == job_id), None)
    if item is None:
        raise ValueError("This job is not in Saved")
    job = item.result.job
    if description is not None:
        text = description.strip()
        if len(text) < 200:
            raise ValueError("Paste the whole job description, requirements included")
        return saved.update_posting(ws, job.model_copy(update={"description": text}))
    query = OneJobQuery(title=job.title, company=job.company, url=job.url or "")
    found = find(ws, query).jobs
    if job.url and not any(len(j.description) > len(job.description) for j in found):
        found = find(ws, query.model_copy(update={"url": ""})).jobs  # the link failed: the web
    fuller = max(found, key=lambda j: len(j.description), default=None)
    if fuller is None or len(fuller.description) <= len(job.description):
        raise ValueError("No fuller posting could be read now; open it and paste the description")
    missing = {k: getattr(fuller, k) for k in ("location", "posted_at") if not getattr(job, k)}
    return saved.update_posting(
        ws, job.model_copy(update={"description": fuller.description, **missing})
    )
