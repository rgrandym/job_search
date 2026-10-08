"""Find the job feed behind each company in a directory and keep `companies.json` current.

For every company: an employer whose feed was verified by hand (`_KNOWN`, mostly big pharma,
CROs and tools makers behind Workday) is taken as is; otherwise the
homepage, then its careers links (two hops at most) are read, checking robots.txt, until an ATS
board is found (`companies.detect_boards`) and answers. A homepage without a careers link gets
the usual paths tried (/careers, /jobs, /join-us, /vacancies). A page carrying schema.org
JobPosting JSON-LD for at least two jobs is the fallback. Careers sites on SuccessFactors,
Phenom or Radancy CWS are recognised by the assets they load (`detect_boards`). A page on a
platform the app cannot read (an older SuccessFactors site, Avature, Taleo, Eightfold,
Salesforce, a recruiter's talent community) is named in the note, so the user knows why.

The hand-verified employers also join the watch-list themselves (origin "known",
`add_known_boards`), whether or not the directory lists them.

Companies the user adds by hand (`add_company`: a name and its website, careers page or ATS
link) go through the same search at once and are kept with no `origin`: discovery never
overwrites them, and they win over a directory entry for the same company (except that an ATS
feed found later still supersedes a hand-added careers page, as for any company).

The app runs this, never the user by hand: a search with company sites first checks every
directory company not checked yet (the whole list on first use, about 10 minutes), and the
sidebar's Update button also re-checks results older than `MAX_AGE_DAYS`. The directory listing
and every company's result are kept in `data/company_discovery.json`, so a stopped run resumes
where it stopped. One run at a time (`_RUN_LOCK`); `status()` reports progress to the UI.

The watch-list then holds one entry per board and per company: hand-added entries (no
`origin`) always win, and a company read through an ATS feed is not also read via its page.

CLI (same code):
    python -m src.services.company_discovery [--directory biopharmguy-uk] [--stale | --all]
"""

from __future__ import annotations

import argparse
import html
import re
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel

from src.core.config import Settings, get_settings
from src.jobs.models import SearchQuery
from src.jobs.sources.base import HttpFetcher, SourceError, extract_jsonld_jobs, html_to_text
from src.jobs.sources.companies import (
    URL_KINDS,
    AtsKind,
    CompanyBoard,
    CompanyCareersSource,
    detect_boards,
    load_companies,
    save_companies,
)
from src.jobs.sources.directories import DirectoryCompany, fetch_directory

MAX_AGE_DAYS = 30
MAX_HOPS = 3  # homepage -> careers page -> job list
MAX_LINKS = 3  # careers-looking links followed per page
# Tried when the homepage links to no careers page (plenty of small sites hide it in a menu).
STANDARD_PATHS = ("/careers", "/jobs", "/join-us", "/vacancies")
# Verifies a detected board answers, without opening postings (no title matches this).
PROBE_QUERY = SearchQuery(titles=["zz discovery probe"])
KNOWN_ORIGIN = "known"
_WD = "myworkdayjobs.com"
# UK life-science employers whose job feed was verified by hand (2026-10-08: answers, robots.txt
# allows it, UK postings listed). Most front a Workday board their homepage does not link to, or
# block automated reading (Lonza, Agilent), so crawling misses them. They join the watch-list
# (origin "known") and answer `add_company` by name. (name, ats, url or token, other names)
_KNOWN: list[tuple[str, AtsKind, str, tuple[str, ...]]] = [
    ("AstraZeneca", "workday", f"https://astrazeneca.wd3.{_WD}/Careers", ()),
    ("GSK", "workday", f"https://gsk.wd5.{_WD}/GSKCareers", ()),
    ("Pfizer", "workday", f"https://pfizer.wd1.{_WD}/PfizerCareers", ()),
    ("Roche", "workday", f"https://roche.wd3.{_WD}/roche-ext", ()),
    ("Sanofi", "workday", f"https://sanofi.wd3.{_WD}/SanofiCareers", ()),
    ("Moderna", "workday", f"https://modernatx.wd1.{_WD}/M_tx", ("moderna therapeutics",)),
    ("Novartis", "workday", f"https://novartis.wd3.{_WD}/Novartis_Careers", ()),
    ("MSD", "workday", f"https://msd.wd5.{_WD}/SearchJobs", ("merck sharp & dohme",)),
    ("Bristol Myers Squibb", "workday", f"https://bristolmyerssquibb.wd5.{_WD}/BMS", ("bms",)),
    ("Johnson & Johnson", "workday", f"https://jj.wd5.{_WD}/JJ", ("janssen", "j&j")),
    ("Takeda", "workday", f"https://takeda.wd502.{_WD}/External", ()),
    ("Amgen", "workday", f"https://amgen.wd1.{_WD}/Careers", ()),
    ("Biogen", "workday", f"https://biibhr.wd3.{_WD}/external", ()),
    ("Gilead Sciences", "workday", f"https://gilead.wd1.{_WD}/gileadcareers", ("gilead",)),
    ("Regeneron", "workday", f"https://regeneron.wd1.{_WD}/Careers", ()),
    ("Vertex Pharmaceuticals", "workday", f"https://vrtx.wd501.{_WD}/vertex_careers", ("vertex",)),
    ("Eisai", "workday", f"https://eisai.wd5.{_WD}/Eisai", ()),
    ("Viatris", "workday", f"https://viatris.wd5.{_WD}/External", ()),
    ("Sandoz", "workday", f"https://sandoz.wd103.{_WD}/SANDOZ_Careers", ()),
    ("CSL Seqirus", "workday", f"https://csl.wd1.{_WD}/CSL_External", ("csl", "seqirus")),
    ("Indivior", "workday", f"https://indivior.wd3.{_WD}/INDIVIOR", ()),
    ("Galderma", "workday", f"https://galderma.wd3.{_WD}/External", ()),
    ("Autolus Therapeutics", "workday", f"https://autolus.wd103.{_WD}/External", ("autolus",)),
    ("Lonza", "workday", f"https://lonza.wd3.{_WD}/Lonza_Careers", ()),
    ("Thermo Fisher", "workday", f"https://thermofisher.wd5.{_WD}/ThermoFisherCareers",
     ("thermo fisher scientific",)),
    ("Danaher", "workday", f"https://danaher.wd1.{_WD}/DanaherJobs",
     ("abcam", "cytiva", "leica biosystems", "beckman coulter")),
    ("Agilent", "workday", f"https://agilent.wd5.{_WD}/Agilent_Careers",
     ("agilent technologies",)),
    ("Illumina", "workday", f"https://illumina.wd1.{_WD}/illumina-careers", ()),
    ("Qiagen", "workday", f"https://qiagen.wd502.{_WD}/QIAGEN", ()),
    ("Bio-Techne", "workday", f"https://biotechne.wd5.{_WD}/biotechne", ()),
    ("Revvity", "workday", f"https://revvity.wd103.{_WD}/External", ("perkinelmer",)),
    ("Sartorius", "workday", f"https://sartorius.wd3.{_WD}/SartoriusCareers", ()),
    ("IQVIA", "workday", f"https://iqvia.wd1.{_WD}/IQVIA", ()),
    ("ICON", "workday", f"https://icon.wd3.{_WD}/broadbean_external", ("icon plc",)),
    ("Parexel", "workday", f"https://parexel.wd1.{_WD}/PAREXEL_External_Careers", ()),
    ("Syneos Health", "workday", f"https://syneoshealth.wd12.{_WD}/Syneos_Health_External_Site",
     ()),
    ("Labcorp", "workday", f"https://labcorp.wd1.{_WD}/External", ()),
    ("Fortrea", "workday", f"https://fortrea.wd1.{_WD}/fortrea", ()),
    ("Catalent", "workday", f"https://catalent.wd1.{_WD}/External", ()),
    ("Medtronic", "workday", f"https://medtronic.wd1.{_WD}/MedtronicCareers", ()),
    ("Abbott", "workday", f"https://abbott.wd5.{_WD}/abbottcareers", ()),
    ("GE HealthCare", "workday", f"https://gehc.wd5.{_WD}/GEHC_ExternalSite", ("ge healthcare",)),
    ("Eurofins", "smartrecruiters", "Eurofins", ()),
    ("Recursion", "greenhouse", "recursionpharmaceuticals", ("exscientia",)),
    ("Owkin", "ashby", "owkin", ()),
    # Careers sites read through their own search (career_sites.py), checked 2026-10-08.
    ("Bayer", "successfactors", "https://jobs.bayer.com", ()),
    ("Boehringer Ingelheim", "successfactors", "https://jobs.boehringer-ingelheim.com",
     ("boehringer",)),
    ("Astellas", "successfactors", "https://careers.astellas.com", ("astellas pharma",)),
    ("UCB", "phenom", "https://careers.ucb.com/global/en", ("ucb pharma",)),
    ("Merck KGaA", "phenom", "https://careers.emdgroup.com/us/en",
     ("merck group", "emd group", "milliporesigma", "merck life science")),
    ("Siemens Healthineers", "phenom", "https://careers.siemens-healthineers.com/global/en", ()),
    ("Hikma", "phenom", "https://talents.hikma.com/us/en", ("hikma pharmaceuticals",)),
    ("Oxford Nanopore", "oracle",
     "https://ejnh.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     ("oxford nanopore technologies",)),
    ("BioMarin", "jobvite", "biomarin", ("biomarin pharmaceutical",)),
    ("Charles River Laboratories", "cws", "https://jobs.criver.com/en-gb/job-search-results/",
     ("charles river",)),
]  # fmt: skip
# Platforms a careers page can sit on that the app cannot read, so the user is told why.
_UNREADABLE: list[tuple[str, re.Pattern[str]]] = [
    (
        "this is a recruitment agency's talent community (a sign-up pool), not the company's "
        "job list",
        re.compile(r"talent-community\.com|talent-pool\.com|randstadsourceright", re.I),
    ),
    *(
        (f"its jobs are on {name}, which the app cannot read yet", re.compile(pattern, re.I))
        for name, pattern in (
            ("an older SAP SuccessFactors site", r"career\d*\.successfactors\.(?:com|eu)/career"),
            ("Avature", r"\.avature\.net"),
            ("Taleo", r"\.taleo\.net"),
            ("Eightfold", r"\.eightfold\.ai|/api/apply/v2/jobs"),
            ("Salesforce", r"\.my\.salesforce-sites\.com|\.my\.site\.com"),
        )
    ),
]
# Shown when nothing readable is found for a company the user adds.
_PASTE_HINT = (
    "Open one of its job postings and paste the address of the job list behind it instead "
    "(on myworkdayjobs.com, greenhouse.io, lever.co, ashbyhq.com, smartrecruiters.com, "
    "workable.com, teamtailor.com, bamboohr.com, pinpointhq.com, recruitee.com, personio, "
    "jobs.jobvite.com or oraclecloud.com, or a SuccessFactors or Phenom careers site)"
)
_CAREERS_HINT = re.compile(
    r"career|vacanc|\bjobs?\b|join[\s-]*(?:us|our|the)|work[\s-]*(?:with|for)[\s-]*us"
    r"|opportunit|openings|recruit",
    re.IGNORECASE,
)
_ANCHOR = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>'"]+))[^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)  # double-, single- or un-quoted href
_OFF_SITE = ("linkedin.", "indeed.", "glassdoor.", "facebook.", "twitter.", "x.com", "instagram.",
             "youtube.", "biopharmguy.")  # fmt: skip


class DiscoveryRecord(BaseModel):
    """What discovery found for one directory company."""

    name: str
    website: str
    origin: str
    board: CompanyBoard | None = None
    note: str = ""
    checked_on: date


class DiscoveryReport(BaseModel):
    """Summary of a discovery pass."""

    companies: int
    checked: int
    boards: dict[str, int]
    without_feed: int
    watch_list: int


class DirectoryListing(BaseModel):
    """A directory's companies as last read (re-read when older than MAX_AGE_DAYS)."""

    read_on: date
    companies: list[DirectoryCompany]


class DiscoveryCache(BaseModel):
    """Everything discovery knows: directory listings and one record per company website."""

    directories: dict[str, DirectoryListing] = {}
    records: list[DiscoveryRecord] = []


class DiscoveryStatus(BaseModel):
    """What the UI shows under the company-sites toggle."""

    directory: str
    running: bool = False
    message: str = ""
    companies: int = 0
    checked: int = 0
    unchecked: int = 0
    stale: int = 0
    boards: int = 0
    by_ats: dict[str, int] = {}
    updated_on: date | None = None
    error: str | None = None


DiscoveryMode = Literal["new", "stale", "all"]
DEFAULT_DIRECTORY = "biopharmguy-uk"
_RUN_LOCK = threading.Lock()
_progress: dict[str, str] = {}  # directory -> latest progress message of the running pass
_last_error: dict[str, str] = {}


def discovery_path(settings: Settings) -> Path:
    """The discovery cache lives next to the watch-list."""
    return settings.companies_path.with_name("company_discovery.json")


def careers_links(page_html: str, base_url: str) -> list[str]:
    """Links on a page that look like they lead to job openings, careers pages first."""
    links: list[str] = []
    for double, single, bare, label in _ANCHOR.findall(page_html):
        href = (double or single or bare).split("#")[0].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if not _CAREERS_HINT.search(f"{href} {html_to_text(label)}"):
            continue
        url = urljoin(base_url, html.unescape(href.strip()))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.path.lower().endswith(".pdf"):
            continue
        if any(host in parts.netloc.lower() for host in _OFF_SITE) or url in links:
            continue
        links.append(url)
    links.sort(key=lambda u: "career" not in u.lower())
    return links[:MAX_LINKS]


def known_boards() -> list[CompanyBoard]:
    """The hand-verified employers as watch-list entries."""
    return [_known(name, ats, ref, KNOWN_ORIGIN) for name, ats, ref, _ in _KNOWN]


def known_board(name: str, origin: str | None) -> CompanyBoard | None:
    """The verified feed of a company known by this name (or one of its other names)."""
    key = " ".join(name.lower().split())
    for known, ats, ref, aliases in _KNOWN:
        if key == known.lower() or key in aliases:
            return _known(name, ats, ref, origin)
    return None


def _known(name: str, ats: AtsKind, ref: str, origin: str | None) -> CompanyBoard:
    by_url = ats in URL_KINDS
    return CompanyBoard(
        name=name, ats=ats, url=ref if by_url else None, token=None if by_url else ref,
        origin=origin,
    )  # fmt: skip


def add_known_boards(settings: Settings) -> None:
    """Keep the hand-verified employers on the watch-list (written only when it changes)."""
    path = settings.companies_path
    existing = load_companies(path) if path.exists() else []
    merged = merge_boards(existing, known_boards(), KNOWN_ORIGIN)
    if merged != existing:
        save_companies(path, merged)


def unreadable_platform(page: str) -> str | None:
    """Why a careers page with jobs on it gives no feed: the platform it sits on, if known."""
    return next((note for note, pattern in _UNREADABLE if pattern.search(page)), None)


def discover_board(
    source: CompanyCareersSource, company: DirectoryCompany, origin: str | None
) -> tuple[CompanyBoard | None, str]:
    """The company's job feed (or None) and a note on how it was found or why not."""
    if known := known_board(company.name, origin):
        return known, "known"
    queue, seen, guessed = [company.website], set[str](), set[str]()
    platform = unreadable_platform(company.website)
    jsonld_url: str | None = None
    note = "no careers link on the homepage"
    for hop in range(MAX_HOPS):
        found_links: list[str] = []
        loaded = False
        for url in queue:
            seen.add(url)
            try:
                page = source.http.get(url, check_robots=True).text
            except SourceError as exc:
                if url not in guessed:  # a guessed path that does not exist says nothing
                    note = str(exc)[:200]
                continue
            loaded = True
            platform = platform or unreadable_platform(page)
            if hop:
                note = "careers pages have no readable job feed"
            for board in detect_boards(page, company.name, origin, page_url=url):
                try:
                    source.fetch_board(board, PROBE_QUERY)
                    return board, f"found on {url}"
                except SourceError as exc:
                    note = f"{board.key} did not answer: {exc}"[:200]
            # One posting means a single job's page, which goes stale; a list has several.
            if jsonld_url is None and len(extract_jsonld_jobs(page)) >= 2:
                jsonld_url = url
            found_links += careers_links(page, url)
        if hop == 0 and loaded and not found_links:
            found_links = [urljoin(company.website, path) for path in STANDARD_PATHS]
            guessed = set(found_links)
        limit = max(MAX_LINKS, len(guessed)) if hop == 0 else MAX_LINKS
        queue = [u for u in dict.fromkeys(found_links) if u not in seen][:limit]
        if not queue:
            break
    if jsonld_url:
        board = CompanyBoard(name=company.name, ats="careers_page", url=jsonld_url, origin=origin)
        return board, "schema.org JobPosting"
    return None, platform or note


def merge_boards(
    existing: list[CompanyBoard], discovered: list[CompanyBoard], origin: str
) -> list[CompanyBoard]:
    """New watch-list: entries from other origins, then this run's, one per board and company.
    A company's own feed is never replaced, but an ATS feed supersedes its careers page."""
    kept = [b for b in existing if b.origin != origin]
    keys = {b.key for b in kept}
    names = {b.name.lower() for b in kept if b.ats != "careers_page"}
    names_any = {b.name.lower() for b in kept}
    for board in discovered:
        name = board.name.lower()
        if (
            board.key in keys
            or name in names
            or (board.ats == "careers_page" and name in names_any)
        ):
            continue
        kept.append(board)
        keys.add(board.key)
        if board.ats != "careers_page":
            names.add(name)
        names_any.add(name)
    with_feed = {b.name.lower() for b in kept if b.ats != "careers_page"}
    return [b for b in kept if b.ats != "careers_page" or b.name.lower() not in with_feed]


def _load_cache(path: Path) -> DiscoveryCache:
    if not path.exists():
        return DiscoveryCache()
    try:
        return DiscoveryCache.model_validate_json(path.read_bytes())
    except ValueError:
        return DiscoveryCache()  # unreadable: start over rather than block searches


def _stale(record: DiscoveryRecord, today: date) -> bool:
    return (today - record.checked_on).days > MAX_AGE_DAYS


def status(settings: Settings, directory: str = DEFAULT_DIRECTORY) -> DiscoveryStatus:
    """Progress of the running pass, or what the last pass left."""
    cache, today = _load_cache(discovery_path(settings)), date.today()
    listing = cache.directories.get(directory)
    sites = {c.website for c in listing.companies} if listing else set()
    records = [r for r in cache.records if r.origin == directory and r.website in sites]
    boards = [r.board for r in records if r.board]
    return DiscoveryStatus(
        directory=directory,
        running=_RUN_LOCK.locked(),
        message=_progress.get(directory, ""),
        companies=len(sites),
        checked=len(records),
        unchecked=len(sites) - len(records) if listing else -1,
        stale=sum(_stale(r, today) for r in records),
        boards=len(boards),
        by_ats=dict(Counter(b.ats for b in boards)),
        updated_on=max((r.checked_on for r in records), default=None),
        error=_last_error.get(directory),
    )


class BoardView(BaseModel):
    """One company job board as the UI lists it (several companies can share one board)."""

    key: str
    ats: str
    companies: list[str]


def list_boards(settings: Settings) -> list[BoardView]:
    """The job boards company searches read, by company name (switch them off per search)."""
    add_known_boards(settings)
    path = settings.companies_path
    boards: dict[str, BoardView] = {}
    for c in load_companies(path) if path.exists() else []:
        view = boards.setdefault(c.key, BoardView(key=c.key, ats=c.ats, companies=[]))
        if c.name not in view.companies:
            view.companies.append(c.name)
    return sorted(boards.values(), key=lambda b: b.companies[0].casefold())


def needs_discovery(settings: Settings, directory: str = DEFAULT_DIRECTORY) -> bool:
    """Whether some directory companies were never checked (or the directory never read)."""
    return status(settings, directory).unchecked != 0


def discover_companies(
    settings: Settings | None = None,
    directory: str = DEFAULT_DIRECTORY,
    *,
    mode: DiscoveryMode = "stale",
    workers: int = 16,
    http: HttpFetcher | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> DiscoveryReport:
    """Check directory companies (`mode`: never-checked ones; plus results older than
    MAX_AGE_DAYS; or all) and update the watch-list. Waits for a pass already running."""
    settings = settings or get_settings()
    with _RUN_LOCK:
        _last_error.pop(directory, None)
        try:
            return _discover(settings, directory, mode, workers, http, progress, should_stop)
        except SourceError as exc:
            _last_error[directory] = str(exc)
            raise
        finally:
            _progress.pop(directory, None)


def _discover(
    settings: Settings,
    directory: str,
    mode: DiscoveryMode,
    workers: int,
    http: HttpFetcher | None,
    progress: Callable[[str], None] | None,
    should_stop: Callable[[], bool],
) -> DiscoveryReport:
    http = http or HttpFetcher(settings)
    source = CompanyCareersSource([], http, settings)
    cache_path, today = discovery_path(settings), date.today()
    cache = _load_cache(cache_path)
    listing = cache.directories.get(directory)
    if listing is None or mode != "new" and (today - listing.read_on).days > MAX_AGE_DAYS:
        listing = DirectoryListing(read_on=today, companies=fetch_directory(http, directory))
        cache.directories[directory] = listing
    companies = listing.companies
    records = {r.website: r for r in cache.records}
    todo = [
        c
        for c in companies
        if mode == "all"
        or c.website not in records
        or mode == "stale"
        and _stale(records[c.website], today)
    ]

    def say(message: str) -> None:
        _progress[directory] = message
        if progress:
            progress(message)

    def check(company: DirectoryCompany) -> DiscoveryRecord | None:
        if should_stop():
            return None
        board, note = discover_board(source, company, directory)
        return DiscoveryRecord(
            name=company.name, website=company.website, origin=directory,
            board=board, note=note, checked_on=today,
        )  # fmt: skip

    say(f"checking {len(todo)} of {len(companies)} companies")
    found = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, record in enumerate(pool.map(check, todo), 1):
            if record is not None:
                records[record.website] = record
                found += record.board is not None
            if i % 25 == 0 or i == len(todo):
                say(f"checked {i}/{len(todo)} companies, {found} job boards found")
    return _save(settings, cache, records, companies, directory, len(todo))


def _save(
    settings: Settings,
    cache: DiscoveryCache,
    records: dict[str, DiscoveryRecord],
    companies: list[DirectoryCompany],
    directory: str,
    checked: int,
) -> DiscoveryReport:
    """Write the cache and merge this directory's boards into the watch-list."""
    cache.records = list(records.values())
    path = discovery_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache.model_dump_json(indent=2), encoding="utf-8")
    found = [records[c.website].board for c in companies if c.website in records]
    boards = [b for b in found if b is not None]
    watch_path = settings.companies_path
    existing = load_companies(watch_path) if watch_path.exists() else []
    watch_list = merge_boards(existing, boards, directory)
    watch_list = merge_boards(watch_list, known_boards(), KNOWN_ORIGIN)
    save_companies(watch_path, watch_list)
    return DiscoveryReport(
        companies=len(companies),
        checked=checked,
        boards=dict(Counter(b.ats for b in boards)),
        without_feed=len(companies) - len(boards),
        watch_list=len(watch_list),
    )


# ---------------------------------------------------------------- the user's own companies


def your_companies(settings: Settings) -> list[CompanyBoard]:
    """Companies the user added by hand (no `origin`), in the order added."""
    path = settings.companies_path
    return [b for b in (load_companies(path) if path.exists() else []) if b.origin is None]


def _ats_link(source: CompanyCareersSource, name: str, url: str) -> CompanyBoard | None:
    """A board named by the link itself (a Greenhouse, Lever, Workday ... URL), if it answers."""
    for board in detect_boards(url, name, None):
        try:
            source.fetch_board(board, PROBE_QUERY)
            return board
        except SourceError:
            continue
    return None


def add_company(
    settings: Settings, name: str, url: str, http: HttpFetcher | None = None
) -> CompanyBoard:
    """Find the job feed of a company the user names (website, careers page or ATS link) and
    keep it on the watch-list, replacing any entry for that company. Raises ValueError, with
    the reason, when no readable feed is found."""
    name, url = name.strip(), url.strip()
    if not urlsplit(url).scheme:
        url = f"https://{url}"
    if not name or urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
        raise ValueError("Give the company's name and its website or careers page link")
    source = CompanyCareersSource([], http or HttpFetcher(settings), settings)
    board = _ats_link(source, name, url)
    note = ""
    if board is None:
        board, note = discover_board(source, DirectoryCompany(name=name, website=url), None)
    if board is None:
        if "403 Forbidden" in note:
            note = "the site refuses automated reading (403)"
        raise ValueError(f"No readable job list found for {name}: {note}. {_PASTE_HINT}")
    path = settings.companies_path
    existing = load_companies(path) if path.exists() else []
    save_companies(path, [*(b for b in existing if b.name.lower() != name.lower()), board])
    return board


def remove_company(settings: Settings, name: str) -> bool:
    """Remove a company the user added (directory entries come back with the next update)."""
    path = settings.companies_path
    existing = load_companies(path) if path.exists() else []
    kept = [b for b in existing if not (b.origin is None and b.name.lower() == name.lower())]
    if len(kept) == len(existing):
        return False
    save_companies(path, kept)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover company job feeds from a directory")
    parser.add_argument("--directory", default=DEFAULT_DIRECTORY)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stale", action="store_true", help="Also re-check old results")
    group.add_argument("--all", action="store_true", help="Re-check every company")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--out", type=Path, default=None, help="Watch-list path override")
    args = parser.parse_args()
    settings = get_settings()
    if args.out:
        settings = settings.model_copy(update={"companies_path": args.out})
    mode: DiscoveryMode = "all" if args.all else "stale" if args.stale else "new"
    report = discover_companies(
        settings, args.directory, mode=mode, workers=args.workers, progress=print
    )
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
