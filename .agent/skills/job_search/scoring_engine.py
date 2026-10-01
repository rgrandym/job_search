"""job_search skill entrypoint: score a CV against a set of job postings.

Thin CLI over `src.jobs.matcher` / `src.jobs.scorer`. Change scoring logic in `src/`,
then update the matrix in SKILL.md to match.

Usage (from the repo root):
    python .agent/skills/job_search/scoring_engine.py --cv data/master_cv.json \
        --jobs data/jobs.json [--threshold 70] [--json out.json] [--show-rejected]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.cv.models import MasterCV, TailoredCV  # noqa: E402
from src.jobs.fetcher import JsonFileSource, SearchQuery  # noqa: E402
from src.jobs.matcher import JobMatcher  # noqa: E402
from src.jobs.models import MatchResult  # noqa: E402


def _row(r: MatchResult) -> str:
    s = r.score
    assert s is not None
    return (
        f"{s.total:6.1f}  {r.job.title[:38]:38s} {r.job.company[:20]:20s} "
        f"T{s.title:.2f} S{s.skills:.2f} E{s.experience:.2f} "
        f"L{s.location:.2f} M{s.semantic:.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cv", type=Path, required=True, help="MasterCV or TailoredCV JSON")
    parser.add_argument("--jobs", type=Path, required=True, help="JSON array of JobPosting")
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--json", type=Path, default=None, help="Write full MatchReport here")
    parser.add_argument("--show-rejected", action="store_true")
    args = parser.parse_args()

    data = json.loads(args.cv.read_text(encoding="utf-8"))
    cv: MasterCV | TailoredCV = (
        TailoredCV.model_validate(data) if "cv" in data else MasterCV.model_validate(data)
    )
    jobs = JsonFileSource(args.jobs).fetch(SearchQuery(limit=10_000))
    report = JobMatcher().match(cv, jobs, threshold=args.threshold)

    print(
        f"Candidate: {report.profile.name}  ({report.profile.years_experience} yrs)  "
        f"threshold={report.threshold}"
    )
    print(
        f"{len(report.matches)} match(es) · {len(report.below_threshold)} below threshold · "
        f"{len(report.excluded)} excluded · {len(report.not_retrieved)} not retrieved\n"
    )
    for r in report.matches:
        print(_row(r))
    if args.show_rejected:
        print("\n-- below threshold --")
        for r in report.below_threshold:
            print(_row(r))
        print("\n-- excluded --")
        for r in report.excluded:
            print(f"        {r.job.title[:38]:38s} {'; '.join(r.exclusion_reasons)}")
    if args.json:
        args.json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
