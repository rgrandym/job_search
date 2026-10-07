"""Full posting text from a posting's own page, for any board: schema.org JobPosting JSON-LD.

Many job pages (employers' careers sites, ATS pages, board detail pages) embed the posting as
JSON-LD. When a shortlisted posting is only a snippet or a card and its own source cannot open
it, this reads the page at `job.url` once, **only where that host's robots.txt allows**, and
takes the description and requirement lists from the JSON-LD. Pages without JSON-LD leave the
posting as it is. LinkedIn is opened by `LinkedInSource.enrich` instead, and Indeed blocks
plain HTTP clients, so neither is read here. At most `page_max_details` pages per search.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from src.core.config import Settings, get_settings
from src.jobs.models import JobPosting
from src.jobs.sources.base import HttpFetcher, extract_jsonld_jobs, posting_from_jsonld

SKIP_HOSTS = ("linkedin.com", "indeed.")


class PostingPages:
    """Opens a posting's own page and reads its JSON-LD (robots.txt checked)."""

    name = "posting_pages"

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        self._opened = 0

    def enrich(self, job: JobPosting) -> JobPosting:
        """`job` with the page's fuller description and requirements; raises SourceError when
        the page cannot be read (robots.txt, HTTP error)."""
        host = urlsplit(job.url or "").netloc.lower()
        if not host or any(h in host for h in SKIP_HOSTS):
            return job
        if self._opened >= self.settings.page_max_details:
            return job
        self._opened += 1
        nodes = extract_jsonld_jobs(self.http.get(job.url or "", check_robots=True).text)
        if not nodes:
            return job
        found = posting_from_jsonld(nodes[0], source=job.source, url=job.url)
        if len(found.description) <= len(job.description):
            return job
        update: dict[str, object] = {"description": found.description}
        for field in ("required_skills", "preferred_skills", "min_years_experience", "closes_at"):
            if not getattr(job, field) and getattr(found, field):
                update[field] = getattr(found, field)
        return job.model_copy(update=update)
