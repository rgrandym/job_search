"""Job boards with official APIs: Reed and CV-Library.

Reed:       https://www.reed.co.uk/developers/jobseeker   (free key, HTTP Basic auth, key as user)
CV-Library: https://www.cv-library.co.uk/developers/job-search-api  (key on request, partner access)
"""

from __future__ import annotations

from typing import Any

from src.core.config import Settings, get_settings
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import (
    HttpFetcher,
    SourceError,
    html_to_text,
    infer_arrangement,
    parse_date,
)

REED_SEARCH = "https://www.reed.co.uk/api/1.0/search"
REED_DETAILS = "https://www.reed.co.uk/api/1.0/jobs/{id}"
CVL_SEARCH = "https://www.cv-library.co.uk/search-jobs-json"


class ReedSource:
    """Reed.co.uk Jobseeker API. Set `JOBSEARCH_REED_API_KEY`."""

    name = "reed"

    def __init__(
        self,
        http: HttpFetcher | None = None,
        settings: Settings | None = None,
        fetch_details: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        self.fetch_details = fetch_details
        if self.settings.reed_api_key is None:
            raise SourceError("Reed requires JOBSEARCH_REED_API_KEY")
        self._auth = (self.settings.reed_api_key.get_secret_value(), "")

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        jobs: dict[str, JobPosting] = {}
        locations: list[str | None] = [*query.locations] or [None]
        for term in query.search_terms():
            for location in locations:
                skip = 0
                while len(jobs) < query.limit:
                    params: dict[str, Any] = {
                        "keywords": term,
                        "resultsToTake": min(100, query.limit - len(jobs)),
                        "resultsToSkip": skip,
                    }
                    if location:
                        params["locationName"] = location
                        if query.distance_miles is not None:
                            params["distanceFromLocation"] = query.distance_miles
                    if query.salary_min:
                        params["minimumSalary"] = query.salary_min
                    if query.salary_max:
                        params["maximumSalary"] = query.salary_max
                    data = self.http.get(REED_SEARCH, params=params, auth=self._auth).json()
                    results = data.get("results", [])
                    for r in results:
                        job = self._to_posting(r)
                        if location:
                            job.within_search_area = True
                        jobs.setdefault(job.id, job)
                    skip += len(results)
                    if not results or skip >= data.get("totalResults", 0):
                        break
        out = list(jobs.values())
        if query.remote_only:
            out = [j for j in out if j.work_arrangement == "remote"]
        return out[: query.limit]

    def enrich(self, job: JobPosting) -> JobPosting:
        """Replace the search snippet with the full description from the details endpoint."""
        detail = self.http.get(
            REED_DETAILS.format(id=job.id.removeprefix("reed:")), auth=self._auth
        ).json()
        text = html_to_text(detail.get("jobDescription", "")) or job.description
        return job.model_copy(update={"description": text})

    def _to_posting(self, r: dict[str, Any]) -> JobPosting:
        description = r.get("jobDescription", "")
        if self.fetch_details:  # Search returns a snippet; prefer lazy `enrich` for shortlists.
            try:
                detail = self.http.get(REED_DETAILS.format(id=r["jobId"]), auth=self._auth).json()
                description = detail.get("jobDescription", description)
            except SourceError:
                pass
        text = html_to_text(description)
        lo, hi = r.get("minimumSalary"), r.get("maximumSalary")
        return JobPosting(
            id=f"reed:{r['jobId']}",
            title=r.get("jobTitle", ""),
            company=r.get("employerName", "unknown"),
            location=r.get("locationName"),
            work_arrangement=infer_arrangement(r.get("jobTitle"), r.get("locationName"), text),
            description=text,
            salary_range=f"{lo}-{hi} {r.get('currency', 'GBP')}" if lo or hi else None,
            salary_min=float(lo) if lo else None,
            salary_max=float(hi) if hi else None,
            url=r.get("jobUrl"),
            posted_at=parse_date(r.get("date")),
            source=self.name,
        )


class CVLibrarySource:
    """CV-Library Job Search API. Requires a partner key (`JOBSEARCH_CV_LIBRARY_API_KEY`).

    NOTE: CV-Library's docs are not publicly fetchable. The field mapping in `_to_posting`
    is defensive and must be verified against the docs you receive with your key.
    """

    name = "cv_library"

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        if self.settings.cv_library_api_key is None:
            raise SourceError("CV-Library requires JOBSEARCH_CV_LIBRARY_API_KEY (partner access)")
        self._key = self.settings.cv_library_api_key.get_secret_value()

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        locations: list[str | None] = [*query.locations] or [None]
        for term in query.search_terms():
            for location in locations:
                params: dict[str, Any] = {
                    "key": self._key,
                    "q": term,
                    "description_full": 1,
                    "nohl": 1,
                }
                if location:
                    params["geo"] = location
                    if query.distance_miles is not None:
                        params["distance"] = query.distance_miles
                data = self.http.get(CVL_SEARCH, params=params).json()
                for r in data.get("jobs", []):
                    job = self._to_posting(r)
                    job.within_search_area = bool(location)
                    jobs.append(job)
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.limit]

    def _to_posting(self, r: dict[str, Any]) -> JobPosting:
        text = html_to_text(str(r.get("description", "")))
        agency = r.get("agency")
        company = agency.get("title") if isinstance(agency, dict) else r.get("company")
        url = r.get("url")
        if url and url.startswith("/"):
            url = f"https://www.cv-library.co.uk{url}"
        return JobPosting(
            id=f"cv_library:{r.get('id')}",
            title=str(r.get("title", "")),
            company=company or "unknown",
            location=r.get("location"),
            work_arrangement=infer_arrangement(r.get("title"), r.get("location"), text),
            description=text,
            salary_range=r.get("salary"),
            url=url,
            posted_at=parse_date(r.get("posted")),
            source=self.name,
        )
