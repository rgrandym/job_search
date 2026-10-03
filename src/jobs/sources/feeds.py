"""Job boards that publish an official syndication feed.

Biotechnology Jobs (https://biotechnologyjobs.co.uk/feeds) publishes its 50 most recent active
UK jobs as a JSON Feed 1.1 (`/jobs.json`), each item carrying a schema.org `JobPosting`.
Terms: CC BY 4.0 with a clearly visible link back to the site (the UI shows one on every job
from this source), and no polling more than once per hour (the feed is cached for an hour).
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.core.config import Settings, get_settings
from src.cv.models import WorkArrangement
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import HttpFetcher, SourceError, country_code, posting_from_jsonld

BIOTECH_FEED = "https://biotechnologyjobs.co.uk/jobs.json"
BIOTECH_ATTRIBUTION = "Jobs from Biotechnology Jobs (https://biotechnologyjobs.co.uk)"
FEED_MAX_AGE_S = 3600  # the publisher asks for at most one poll per hour
_TAG_ARRANGEMENT: dict[str, WorkArrangement] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "on-site": "onsite",
    "onsite": "onsite",
}


class BiotechnologyJobsSource:
    """Biotechnology Jobs' official JSON feed: its latest 50 UK biotech jobs (no key needed)."""

    name = "biotechnologyjobs"

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        self.cache_path = self.settings.data_dir / "feed_cache" / "biotechnologyjobs.json"

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        if query.country and country_code(query.country) != "gb":
            raise SourceError(
                f"Biotechnology Jobs lists UK jobs only; not read for {query.country}"
            )
        jobs = [self._to_posting(item) for item in self._items()]
        jobs = [j for j in jobs if query.is_relevant(j) and query.is_recent(j)]
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.limit]

    def _items(self) -> list[dict[str, Any]]:
        """Feed items, from the hour-old cache when there is one (the publisher's polling rule)."""
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if time.time() - float(cached["fetched_at"]) < FEED_MAX_AGE_S:
                return list(cached["items"])
        except (OSError, ValueError, KeyError, TypeError):
            pass  # no usable cache: fetch
        try:
            items = self.http.get(BIOTECH_FEED).json().get("items", [])
        except ValueError:
            raise SourceError("Biotechnology Jobs feed is not valid JSON") from None
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": time.time(), "items": items}
        self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        return list(items)

    def _to_posting(self, item: dict[str, Any]) -> JobPosting:
        node = item.get("_jobposting") or {"title": item.get("title", "")}
        job = posting_from_jsonld(node, source=self.name, url=item.get("url"))
        tags = {str(t).lower() for t in item.get("tags", [])}
        arrangement = next((a for t, a in _TAG_ARRANGEMENT.items() if t in tags), None)
        return job.model_copy(
            update={
                "company": item.get("_company") or job.company,
                "location": job.location or item.get("_location"),
                "work_arrangement": arrangement or job.work_arrangement,
                "description": job.description or str(item.get("summary", "")),
                "url": item.get("url") or job.url,
            }
        )
