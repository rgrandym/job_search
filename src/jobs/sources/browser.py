"""Full postings through the user's installed Chrome, headless and signed out.

The last way to read a shortlisted posting that is still too thin for the job_matcher to check
its requirements: after the posting's own source and its page's JSON-LD (`pages.py`). Chrome
renders what a visitor sees, so it reads pages that fill in with JavaScript or turn plain
scripts away, including LinkedIn's "About the job" and Indeed's description.

Rules (CLAUDE.md, "Job Sources"):
- **Signed out:** every page loads in a fresh, empty Chrome profile (a temporary directory),
  never the user's profile, cookies or accounts.
- **robots.txt** is kept for every host except LinkedIn and Indeed postings, which disallow all
  crawlers; those are opened slowly (`browser_delay_s` per host) and capped like LinkedIn search.
- **No circumvention:** a challenge ("verify you are human", CAPTCHA) or a sign-in wall is
  never solved; after `browser_host_strikes` of them (LinkedIn's come and go) that host is
  left for the rest of the search, and the user can paste the description instead. No
  disguised identity, no proxies, no solving.
- At most `browser_max_pages` pages per search; Stop kills a page that is loading.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from src.core.config import Settings, get_settings
from src.core.llm.calls import call_group, is_cancelled
from src.jobs.models import JobPosting
from src.jobs.sources.base import (
    HttpFetcher,
    SourceError,
    extract_jsonld_jobs,
    html_to_text,
    posting_from_jsonld,
)
from src.jobs.sources.public_boards import linkedin_details

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)
# Hosts whose robots.txt disallows every crawler; their postings are read slowly and capped.
NO_ROBOTS_HOSTS = ("linkedin.com", "indeed.")
_CHALLENGE = re.compile(
    r"captcha|verify (that )?you are (a )?human|are you a robot|challenge-platform|"
    r"cf-challenge|unusual traffic|authwall|sign in to view",
    re.IGNORECASE,
)
_INDEED = re.compile(r'id="jobDescriptionText"[^>]*>(.*?)</div>\s*</div>', re.S)
_MAIN = re.compile(r"<main[^>]*>(.*?)</main>", re.S | re.I)
_NOISE = re.compile(r"<(script|style|noscript|svg|nav|header|footer)[^>]*>.*?</\1>", re.S | re.I)

Runner = Callable[[list[str], float], str]


def find_chrome(settings: Settings) -> str | None:
    """The configured Chrome (`chrome_path`), else an installed Chrome or Chromium."""
    if settings.chrome_path:
        return settings.chrome_path if Path(settings.chrome_path).exists() else None
    found = next((p for p in CHROME_PATHS if Path(p).exists()), None)
    return found or shutil.which("google-chrome") or shutil.which("chromium")


def _run_chrome(cmd: list[str], timeout: float) -> str:
    """Rendered DOM from Chrome. Chrome prints the page once rendered but may keep running
    while the page stays busy (LinkedIn does), so the output is read until the page is complete
    and Chrome is then closed. Stop (the search's call group) or the timeout close it sooner."""
    group = call_group.get()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as exc:
        raise SourceError(f"Chrome could not start: {exc}") from exc
    lines: list[str] = []
    complete = threading.Event()

    def read() -> None:
        for line in proc.stdout or []:
            lines.append(line)
            if "</html>" in line.lower():
                break
        complete.set()

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        while not complete.wait(0.2):
            if is_cancelled(group):
                raise SourceError("stopped")
            if time.monotonic() > deadline:
                raise SourceError("the page did not finish loading in time")
    finally:
        proc.kill()
        proc.wait()
    return "".join(lines)


class BrowserFetcher:
    """Reads posting pages with headless Chrome in a fresh, signed-out profile."""

    name = "browser"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        chrome: str | None = None,
        run: Runner | None = None,
        http: HttpFetcher | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.chrome = chrome or find_chrome(self.settings)
        self.run = run or _run_chrome
        self.http = http or HttpFetcher(self.settings, delay_s=self.settings.browser_delay_s)
        self._opened = 0
        self._strikes: dict[str, int] = {}

    @property
    def available(self) -> bool:
        return self.settings.browser_enabled and self.chrome is not None

    def page(self, url: str) -> str:
        """The rendered page at `url`; raises SourceError when it may not or cannot be read."""
        host = urlsplit(url).netloc.lower()
        if not self.available:
            raise SourceError("Chrome is not available")
        if self._strikes.get(host, 0) >= self.settings.browser_host_strikes:
            raise SourceError(f"{host} asked to verify a human earlier: paste the description")
        if self._opened >= self.settings.browser_max_pages:
            raise SourceError("browser page limit for this search reached")
        if not any(h in host for h in NO_ROBOTS_HOSTS) and not self.http.allowed(url):
            raise SourceError(f"robots.txt disallows {url}")
        self.http.wait_turn(host)
        self._opened += 1
        with tempfile.TemporaryDirectory(prefix="jobsearch-chrome-") as profile:
            cmd = [
                str(self.chrome), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", f"--user-data-dir={profile}",
                f"--virtual-time-budget={self.settings.browser_wait_ms}", "--dump-dom", url,
            ]  # fmt: skip
            dom = self.run(cmd, self.settings.browser_timeout_s)
        if _CHALLENGE.search(dom[:200_000]) and not _has_posting(dom):
            self._strikes[host] = self._strikes.get(host, 0) + 1
            raise SourceError(f"{host} asked to verify a human or sign in: paste the description")
        return dom

    def enrich(self, job: JobPosting) -> JobPosting:
        """`job` with the description its page shows (JSON-LD, the board's description block,
        else the page's main text); unchanged when the page shows no more."""
        if not job.url:
            return job
        dom = self.page(job.url)
        text = posting_text(dom, job)
        if len(text) <= len(job.description):
            return job
        return job.model_copy(update={"description": text})


def _has_posting(dom: str) -> bool:
    return bool(extract_jsonld_jobs(dom)) or "show-more-less-html__markup" in dom


def posting_text(dom: str, job: JobPosting) -> str:
    """The posting's text from a rendered page: JSON-LD, LinkedIn's "About the job", Indeed's
    description, else the visible text of the page's main area."""
    nodes = extract_jsonld_jobs(dom)
    if nodes:
        found = posting_from_jsonld(nodes[0], source=job.source, url=job.url)
        if found.description:
            return found.description
    if "show-more-less-html__markup" in dom:
        return str(linkedin_details(dom, job)["description"])
    if m := _INDEED.search(dom):
        return html_to_text(m.group(1))
    body = _MAIN.search(dom)
    return html_to_text(_NOISE.sub("", body.group(1) if body else dom))[:12_000]
