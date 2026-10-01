"""Individual company career sites.

Most companies host jobs on an ATS that publishes a public JSON feed meant for embedding:
    Greenhouse  https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    Lever       https://api.lever.co/v0/postings/{token}?mode=json
    Ashby       https://api.ashbyhq.com/posting-api/job-board/{token}
For any other careers page, `careers_page` reads schema.org `JobPosting` JSON-LD
(which most sites embed for Google Jobs), after checking robots.txt.

Companies are listed in `data/companies.json` (see data/examples/companies.example.json):
    [{"name": "Acme", "ats": "greenhouse", "token": "acme"},
     {"name": "Foo", "ats": "careers_page", "url": "https://foo.com/careers"}]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, TypeAdapter, model_validator

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

AtsKind = Literal["greenhouse", "lever", "ashby", "careers_page"]


class CompanyBoard(BaseModel):
    name: str
    ats: AtsKind
    token: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _check(self) -> CompanyBoard:
        if self.ats == "careers_page" and not self.url:
            raise ValueError(f"{self.name}: careers_page needs `url`")
        if self.ats != "careers_page" and not self.token:
            raise ValueError(f"{self.name}: {self.ats} needs `token` (the board slug)")
        return self


def load_companies(path: Path) -> list[CompanyBoard]:
    """Read the company watch-list."""
    return TypeAdapter(list[CompanyBoard]).validate_json(path.read_bytes())


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
        self.companies = (
            companies
            if companies is not None
            else (
                load_companies(self.settings.companies_path)
                if self.settings.companies_path.exists()
                else []
            )
        )
        self.errors: dict[str, str] = {}

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        for company in self.companies:
            try:
                jobs += getattr(self, f"_{company.ats}")(company)
            except (SourceError, KeyError, ValueError) as exc:
                self.errors[company.name] = str(exc)  # One broken board must not stop the run.
        jobs = [j for j in jobs if query.is_relevant(j)]
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.limit]

    def _greenhouse(self, c: CompanyBoard) -> list[JobPosting]:
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

    def _lever(self, c: CompanyBoard) -> list[JobPosting]:
        data = self.http.get(
            f"https://api.lever.co/v0/postings/{c.token}", params={"mode": "json"}
        ).json()
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

    def _ashby(self, c: CompanyBoard) -> list[JobPosting]:
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

    def _careers_page(self, c: CompanyBoard) -> list[JobPosting]:
        assert c.url is not None
        page = self.http.get(c.url, check_robots=True).text
        nodes: list[dict[str, Any]] = extract_jsonld_jobs(page)
        if not nodes:
            raise SourceError(f"no schema.org JobPosting found at {c.url}; add the ATS instead")
        jobs = [posting_from_jsonld(n, source=f"careers:{c.name}", url=c.url) for n in nodes]
        return [
            j.model_copy(update={"company": c.name}) if j.company == "unknown" else j for j in jobs
        ]
