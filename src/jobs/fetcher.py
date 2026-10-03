"""Collect postings from all configured sources, structure and de-duplicate them.

Sources live in `src/jobs/sources/`. Sources return `JobPosting` objects. They never
score or filter by candidate fit (that is the matcher's job).

CLI:
    python -m src.jobs.fetcher fetch --keywords "machine learning" --locations London \
        --out data/jobs.json [--sources reed,company,inbox]
    python -m src.jobs.fetcher export-schema
"""

from __future__ import annotations

import argparse
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, TypeAdapter

from src.core.config import PROJECT_ROOT, Settings, get_settings
from src.core.llm_provider import LLMProvider
from src.jobs.models import JobPosting, SearchQuery
from src.tools.search_tools import company_key, posting_description

SCHEMA_PATH = PROJECT_ROOT / ".agent" / "skills" / "job_search" / "job_schema.json"
ALL_SOURCES = (
    "linkedin_search",
    "reed",
    "cv_library",
    "adzuna",
    "totaljobs",
    "jobs_ac_uk",
    "nhs_jobs",
    "biotechnologyjobs",
    "company",
    "inbox",
)
SourceCategory = Literal["job_boards", "company", "alerts"]


class SourceInfo(BaseModel):
    """A source the user can select, and the group the UI lists it under."""

    name: str
    label: str
    category: SourceCategory
    note: str = ""


# Every selectable source, in display order. A new source added here appears in the UI's
# settings panel for its category.
SOURCE_CATALOG: tuple[SourceInfo, ...] = (
    SourceInfo(
        name="linkedin_search",
        label="LinkedIn",
        category="job_boards",
        note="Public job search, no login · slow and capped; full text for the shortlist",
    ),
    SourceInfo(name="reed", label="Reed", category="job_boards", note="Official API"),
    SourceInfo(name="cv_library", label="CV-Library", category="job_boards", note="Official API"),
    SourceInfo(name="adzuna", label="Adzuna", category="job_boards", note="Official API"),
    SourceInfo(
        name="totaljobs",
        label="Totaljobs",
        category="job_boards",
        note="First page per title and place · snippets only",
    ),
    SourceInfo(
        name="jobs_ac_uk",
        label="jobs.ac.uk",
        category="job_boards",
        note="Universities and research institutes · UK-wide, with closing dates",
    ),
    SourceInfo(
        name="nhs_jobs",
        label="NHS Jobs",
        category="job_boards",
        note="NHS employers · official XML search, with closing dates",
    ),
    SourceInfo(
        name="biotechnologyjobs",
        label="Biotechnology Jobs",
        category="job_boards",
        note="Latest 50 UK biotech jobs · public feed, refreshed hourly",
    ),
    SourceInfo(name="demo", label="Demo jobs", category="job_boards", note="Bundled examples"),
    SourceInfo(
        name="company",
        label="Company career sites",
        category="company",
        note="ATS feeds of UK life-science companies",
    ),
    SourceInfo(
        name="gmail_alerts",
        label="Gmail alerts",
        category="alerts",
        note="LinkedIn, Indeed and other alert emails in the connected inbox",
    ),
    SourceInfo(
        name="linkedin",
        label="LinkedIn alert files",
        category="alerts",
        note="Alert emails and postings saved under data/inbox/",
    ),
    SourceInfo(
        name="indeed",
        label="Indeed alert files",
        category="alerts",
        note="Alert emails and postings saved under data/inbox/",
    ),
)
SELECTABLE_SOURCES = tuple(info.name for info in SOURCE_CATALOG)
DEMO_JOBS = PROJECT_ROOT / "data" / "examples" / "jobs.example.json"

PARSE_SYSTEM = """You convert a raw job posting into structured fields. Copy facts only.
- required_skills: skills under "requirements", "must have", "you have".
- preferred_skills: skills under "nice to have", "bonus", "preferred".
- required_certifications: only certifications the posting says are mandatory.
- min_years_experience: the smallest number of years stated, else null.
- work_arrangement: remote, hybrid or onsite. Default onsite if unstated.
Leave a field empty rather than guessing."""


class JobSource(Protocol):
    name: str

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Return postings loosely matching `query` (recall over precision)."""
        ...


class JsonFileSource:
    """Postings from a local JSON array (e.g. a previous `fetch` run or an export)."""

    def __init__(self, path: Path, name: str | None = None) -> None:
        self.path = path
        self.name = name or f"file:{path.name}"

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        jobs = TypeAdapter(list[JobPosting]).validate_json(self.path.read_bytes())
        jobs = [j for j in jobs if query.is_relevant(j) and query.is_recent(j)]
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.limit]


def parse_posting(raw: str, job_id: str, llm: LLMProvider, source: str = "manual") -> JobPosting:
    """Structure a pasted job description into a `JobPosting` via the LLM."""
    job = llm.generate(system=PARSE_SYSTEM, prompt=raw, output_model=JobPosting)
    return job.model_copy(update={"id": job_id, "source": source, "description": raw})


def dedupe(jobs: list[JobPosting]) -> list[JobPosting]:
    """Merge duplicates by identity or near-identical full text, keeping the richest copy."""
    kept: list[JobPosting] = []
    index: dict[object, int] = {}
    by_title: dict[str, list[int]] = {}
    for job in jobs:
        title = " ".join(job.title.casefold().split())
        description = posting_description(job.description)
        keys = [
            ("id", job.id),
            ("key", company_key(job.company), title, (job.location or "").casefold()),
        ]
        if job.url:
            keys.append(("url", job.url.split("?")[0].rstrip("/")))
        hit = next((index[k] for k in keys if k in index), None)
        if hit is None and len(description) >= 200:
            for candidate in by_title.get(title, []):
                other = posting_description(kept[candidate].description)
                if len(other) >= 200 and SequenceMatcher(None, description, other).ratio() >= 0.98:
                    hit = candidate
                    break
        if hit is None:
            hit = len(kept)
            kept.append(job)
            by_title.setdefault(title, []).append(hit)
        elif len(job.description) > len(kept[hit].description):
            kept[hit] = job  # e.g. a saved full posting supersedes a partial alert entry
        for k in keys:
            index[k] = hit
    return kept


def build_sources(
    names: list[str] | None = None,
    *,
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
    cv_key: str | None = None,
    new_alerts_only: bool = False,
) -> tuple[list[JobSource], dict[str, str]]:
    """Instantiate the requested sources, skipping (and reporting) unconfigured ones.
    `new_alerts_only`: Gmail alerts skip jobs the CV has already had judged."""
    from src.jobs.sources.base import SourceError
    from src.jobs.sources.companies import CompanyCareersSource
    from src.jobs.sources.feeds import BiotechnologyJobsSource
    from src.jobs.sources.gmail_alerts import GmailAlertSource
    from src.jobs.sources.inbox import InboxSource
    from src.jobs.sources.job_boards import AdzunaSource, CVLibrarySource, ReedSource
    from src.jobs.sources.public_boards import (
        JobsAcUkSource,
        LinkedInSource,
        NHSJobsSource,
        TotaljobsSource,
    )

    settings = settings or get_settings()
    factories: dict[str, Any] = {
        "reed": lambda: ReedSource(settings=settings),
        "cv_library": lambda: CVLibrarySource(settings=settings),
        "adzuna": lambda: AdzunaSource(settings=settings),
        "linkedin_search": lambda: LinkedInSource(settings=settings),
        "totaljobs": lambda: TotaljobsSource(settings=settings),
        "jobs_ac_uk": lambda: JobsAcUkSource(settings=settings),
        "nhs_jobs": lambda: NHSJobsSource(settings=settings),
        "biotechnologyjobs": lambda: BiotechnologyJobsSource(settings=settings),
        "company": lambda: CompanyCareersSource(settings=settings),
        "inbox": lambda: InboxSource(settings=settings, llm=llm),
        "linkedin": lambda: InboxSource(settings=settings, llm=llm, board="linkedin"),
        "indeed": lambda: InboxSource(settings=settings, llm=llm, board="indeed"),
        "demo": lambda: JsonFileSource(DEMO_JOBS, name="demo"),
        "gmail_alerts": lambda: GmailAlertSource(
            settings=settings, cv_key=cv_key, only_new=new_alerts_only
        ),
    }
    sources: list[JobSource] = []
    skipped: dict[str, str] = {}
    for name in names or list(ALL_SOURCES):
        if name not in factories:
            skipped[name] = "unknown source"
            continue
        try:
            sources.append(factories[name]())
        except SourceError as exc:
            skipped[name] = str(exc)
    return sources, skipped


def fetch_all(
    sources: list[JobSource], query: SearchQuery, counts: dict[str, int] | None = None
) -> tuple[list[JobPosting], dict[str, str]]:
    """Fetch from every source, keep postings within `query.posted_within_days`, and
    de-duplicate. One failing source never aborts the run.

    `counts`, when given, receives the number of postings each source returned (pre-dedupe).
    """
    from src.jobs.sources.base import SourceError

    jobs: list[JobPosting] = []
    errors: dict[str, str] = {}
    for src in sources:
        try:
            found = [j for j in src.fetch(query) if query.is_recent(j)]
            jobs += found
            if counts is not None:
                counts[src.name] = counts.get(src.name, 0) + len(found)
        except SourceError as exc:
            errors[src.name] = str(exc)
        errors.update({f"{src.name}:{k}": v for k, v in getattr(src, "errors", {}).items()})
    return dedupe(jobs), errors


def json_schema() -> dict[str, Any]:
    """JSON Schema for a list of `JobPosting`, as committed to the job_search skill."""
    schema = TypeAdapter(list[JobPosting]).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "job_schema.json"
    return schema


def export_schema(path: Path = SCHEMA_PATH) -> Path:
    """Regenerate the committed JSON Schema from the Pydantic model."""
    path.write_text(json.dumps(json_schema(), indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Job source utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--titles", nargs="*", default=[])
    f.add_argument("--keywords", nargs="*", default=[])
    f.add_argument("--locations", nargs="*", default=[])
    f.add_argument("--distance", type=int, default=25, help="Miles from location")
    f.add_argument("--salary-min", type=int, default=None)
    f.add_argument("--remote-only", action="store_true")
    f.add_argument("--posted-within", type=int, default=None, help="Days (1 = last 24 h)")
    f.add_argument("--limit", type=int, default=200)
    f.add_argument("--sources", default=",".join(ALL_SOURCES))
    f.add_argument("--out", type=Path, default=get_settings().data_dir / "jobs.json")
    sub.add_parser("export-schema")
    args = parser.parse_args()

    if args.cmd == "export-schema":
        print(f"Wrote {export_schema()}")
        return
    query = SearchQuery(
        titles=args.titles,
        keywords=args.keywords,
        locations=args.locations,
        distance_miles=args.distance,
        salary_min=args.salary_min,
        work_arrangements=["remote"] if args.remote_only else [],
        posted_within_days=args.posted_within,
        limit=args.limit,
    )
    sources, skipped = build_sources(args.sources.split(","))
    jobs, errors = fetch_all(sources, query)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps([j.model_dump(mode="json", exclude_none=True) for j in jobs], indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(jobs)} postings to {args.out}")
    for name, why in {**skipped, **errors}.items():
        print(f"  ! {name}: {why}")


if __name__ == "__main__":
    main()
