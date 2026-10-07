"""Job boards with official APIs: Reed, CV-Library and Adzuna.

Reed:       https://www.reed.co.uk/developers/jobseeker   (free key, HTTP Basic auth, key as user)
CV-Library: https://www.cv-library.co.uk/developers/job-search-api  (key on request, partner access)
Adzuna:     https://developer.adzuna.com/docs/search       (app ID and app key)
"""

from __future__ import annotations

import math
import time
from typing import Any

import httpx

from src.core.config import Settings, get_settings
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import (
    HttpFetcher,
    SourceError,
    country_code,
    html_to_text,
    infer_arrangement,
    parse_date,
)

REED_SEARCH = "https://www.reed.co.uk/api/1.0/search"
REED_DETAILS = "https://www.reed.co.uk/api/1.0/jobs/{id}"
CVL_SEARCH = "https://www.cv-library.co.uk/search-jobs-json"
ADZUNA_SEARCH = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
# Adzuna answers 5xx now and then under load: retry a page twice, then skip that search.
ADZUNA_RETRIES = 2
ADZUNA_RETRY_S = 2.0
# Countries Adzuna's search API covers (https://developer.adzuna.com/overview).
ADZUNA_COUNTRIES = {
    "gb", "us", "ca", "au", "nz", "de", "fr", "nl", "be", "ch", "at", "es", "it", "pl", "sg",
    "in", "za", "br", "mx",
}  # fmt: skip


def uk_only(source: str, query: SearchQuery) -> bool:
    """True when the search is in the UK (or unset). UK-only boards refuse other countries."""
    if query.country and country_code(query.country) != "gb":
        raise SourceError(f"{source} lists UK jobs only; not searched for {query.country}")
    return query.country is not None


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
        in_country = uk_only("Reed", query)
        jobs: dict[str, JobPosting] = {}
        per_search, cap = query.search_budget()
        locations: list[str | None] = [*query.locations] or [None]
        for term in query.search_terms():
            for location in locations:
                skip = 0
                while skip < per_search:
                    params: dict[str, Any] = {
                        "keywords": term,
                        "resultsToTake": min(100, per_search - skip),
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
                        if location or in_country:
                            job.within_search_area = True
                        jobs.setdefault(job.id, job)
                    skip += len(results)
                    if not results or skip >= data.get("totalResults", 0):
                        break
        out = list(jobs.values())
        if query.remote_only:
            out = [j for j in out if j.work_arrangement == "remote"]
        return out[:cap]

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
            closes_at=parse_date(r.get("expirationDate")),
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
        in_country = uk_only("CV-Library", query)
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
                    job.within_search_area = bool(location) or in_country
                    jobs.append(job)
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.search_budget()[1]]

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


def _adzuna_error(exc: Exception) -> str:
    """Why an Adzuna call failed, without echoing the request URL (it carries the app key)."""
    cause = exc.__cause__
    if not isinstance(cause, httpx.HTTPStatusError):
        return "Adzuna API request failed (network error or unreadable response)"
    status = cause.response.status_code
    if status in (401, 403):
        return (
            f"Adzuna rejected the credentials (HTTP {status}); check "
            "JOBSEARCH_ADZUNA_APP_ID and JOBSEARCH_ADZUNA_APP_KEY"
        )
    if status == 429:
        return "Adzuna rate limit reached (HTTP 429); try again later"
    return f"Adzuna API request failed (HTTP {status})"


class _AdzunaTransient(SourceError):
    """A search that still failed after retries (5xx, network, unreadable): skip it."""


def _adzuna_transient(exc: Exception) -> bool:
    """Worth retrying: a server error, a network error or an unreadable body. Bad credentials,
    the rate limit and a stopped search are not."""
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code >= 500
    return isinstance(exc, ValueError) or isinstance(cause, httpx.TransportError)


class AdzunaSource:
    """Adzuna's official UK search API; descriptions are snippets only.

    Set `JOBSEARCH_ADZUNA_APP_ID` and `JOBSEARCH_ADZUNA_APP_KEY`.
    """

    name = "adzuna"

    def __init__(self, http: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        if self.settings.adzuna_app_id is None or self.settings.adzuna_app_key is None:
            raise SourceError(
                "Adzuna requires JOBSEARCH_ADZUNA_APP_ID and JOBSEARCH_ADZUNA_APP_KEY"
            )
        self._id = self.settings.adzuna_app_id.get_secret_value()
        self._key = self.settings.adzuna_app_key.get_secret_value()

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        country = country_code(query.country) or "gb"
        if country not in ADZUNA_COUNTRIES:
            raise SourceError(f"Adzuna does not cover {query.country}")
        jobs: dict[str, JobPosting] = {}
        failed: list[str] = []
        locations: list[str | None] = [*query.locations] or [None]
        searches = [(term, loc) for term in query.search_terms() for loc in locations]
        for term, location in searches:
            try:
                for job in self._search(country, term, location, query):
                    jobs.setdefault(job.id, job)
            except _AdzunaTransient as exc:
                failed.append(str(exc))  # one bad search must not lose the others
        if failed and len(failed) == len(searches):
            raise SourceError(failed[0])
        _, cap = query.search_budget()
        out = list(jobs.values())
        if query.remote_only:
            out = [job for job in out if job.work_arrangement == "remote"]
        return out[:cap]

    def _search(
        self, country: str, term: str, location: str | None, query: SearchQuery
    ) -> list[JobPosting]:
        """One term in one place, page by page up to the search budget."""
        jobs: list[JobPosting] = []
        per_search, _ = query.search_budget()
        page_size = min(50, per_search)
        page = 1
        while (page - 1) * page_size < per_search:
            params: dict[str, Any] = {
                "app_id": self._id,
                "app_key": self._key,
                "results_per_page": page_size,
                "what": term,
                "content-type": "application/json",
            }
            if location:
                params["where"] = location
                if query.distance_miles is not None:
                    params["distance"] = math.ceil(query.distance_miles * 1.609344)
            if query.salary_min is not None:
                params["salary_min"] = query.salary_min
            if query.salary_max is not None:
                params["salary_max"] = query.salary_max
            if query.posted_within_days is not None:
                params["max_days_old"] = query.posted_within_days
            data = self._page(ADZUNA_SEARCH.format(country=country, page=page), params)
            results = data.get("results", [])
            for item in results:
                job = self._to_posting(item)
                job.within_search_area = bool(location) or query.country is not None
                jobs.append(job)
            if not results or page * page_size >= data.get("count", 0):
                break
            page += 1
        return jobs

    def _page(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """One results page, retried on transient failures."""
        for attempt in range(ADZUNA_RETRIES + 1):
            try:
                data: dict[str, Any] = self.http.get(url, params=params).json()
                return data
            except (SourceError, ValueError) as exc:
                # HttpFetcher errors may contain query parameters, including the app key.
                if not _adzuna_transient(exc):
                    raise SourceError(_adzuna_error(exc)) from None
                if attempt == ADZUNA_RETRIES:
                    raise _AdzunaTransient(_adzuna_error(exc)) from None
                time.sleep(ADZUNA_RETRY_S * (attempt + 1))
        raise AssertionError("unreachable")

    def _to_posting(self, item: dict[str, Any]) -> JobPosting:
        text = html_to_text(str(item.get("description") or ""))
        location = item.get("location") or {}
        company = item.get("company") or {}
        low, high = item.get("salary_min"), item.get("salary_max")
        has_salary = not item.get("salary_is_predicted") and (low is not None or high is not None)
        salary_range = None
        if has_salary:
            salary_from = low if low is not None else high
            salary_to = high if high is not None else low
            salary_range = f"£{salary_from:g}–£{salary_to:g}"
        return JobPosting(
            id=f"adzuna:{item['id']}",
            title=str(item.get("title") or ""),
            company=str(company.get("display_name") or "unknown"),
            location=location.get("display_name"),
            work_arrangement=infer_arrangement(
                item.get("title"), location.get("display_name"), text
            ),
            description=text,
            salary_range=salary_range,
            salary_min=float(low) if has_salary and low is not None else None,
            salary_max=float(high) if has_salary and high is not None else None,
            url=item.get("redirect_url"),
            posted_at=parse_date(item.get("created")),
            source=self.name,
        )
