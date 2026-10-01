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
from pathlib import Path
from typing import Any, Protocol

from pydantic import TypeAdapter

from src.core.config import PROJECT_ROOT, Settings, get_settings
from src.core.llm_provider import LLMProvider
from src.jobs.models import JobPosting, SearchQuery

SCHEMA_PATH = PROJECT_ROOT / ".agent" / "skills" / "job_search" / "job_schema.json"
ALL_SOURCES = ("reed", "cv_library", "company", "inbox")
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
        jobs = [j for j in jobs if query.is_relevant(j)]
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.limit]


def parse_posting(raw: str, job_id: str, llm: LLMProvider, source: str = "manual") -> JobPosting:
    """Structure a pasted job description into a `JobPosting` via the LLM."""
    job = llm.generate(system=PARSE_SYSTEM, prompt=raw, output_model=JobPosting)
    return job.model_copy(update={"id": job_id, "source": source, "description": raw})


def dedupe(jobs: list[JobPosting]) -> list[JobPosting]:
    """Merge duplicates by id, URL, or (company, title, location), keeping the richest copy."""
    kept: list[JobPosting] = []
    index: dict[object, int] = {}
    for job in jobs:
        keys = [
            ("id", job.id),
            ("key", job.company.lower(), job.title.lower(), (job.location or "").lower()),
        ]
        if job.url:
            keys.append(("url", job.url.split("?")[0].rstrip("/")))
        hit = next((index[k] for k in keys if k in index), None)
        if hit is None:
            hit = len(kept)
            kept.append(job)
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
) -> tuple[list[JobSource], dict[str, str]]:
    """Instantiate the requested sources, skipping (and reporting) unconfigured ones."""
    from src.jobs.sources.base import SourceError
    from src.jobs.sources.companies import CompanyCareersSource
    from src.jobs.sources.inbox import InboxSource
    from src.jobs.sources.job_boards import CVLibrarySource, ReedSource

    settings = settings or get_settings()
    factories: dict[str, Any] = {
        "reed": lambda: ReedSource(settings=settings),
        "cv_library": lambda: CVLibrarySource(settings=settings),
        "company": lambda: CompanyCareersSource(settings=settings),
        "inbox": lambda: InboxSource(settings=settings, llm=llm),
        "demo": lambda: JsonFileSource(DEMO_JOBS, name="demo"),
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
    sources: list[JobSource], query: SearchQuery
) -> tuple[list[JobPosting], dict[str, str]]:
    """Fetch from every source and de-duplicate. One failing source never aborts the run."""
    from src.jobs.sources.base import SourceError

    jobs: list[JobPosting] = []
    errors: dict[str, str] = {}
    for src in sources:
        try:
            jobs += src.fetch(query)
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
