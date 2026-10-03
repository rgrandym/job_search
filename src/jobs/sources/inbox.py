"""LinkedIn & Indeed (and any board) via the user's own inbox.

Indeed blocks plain HTTP clients, so its alerts are its only path into the app; LinkedIn also
has a public search adapter (`public_boards.LinkedInSource`), and its alerts merge with it
(same `linkedin:<id>` ids). This module captures postings through channels the user controls:

1. Job-alert emails  `data/inbox/*.eml`
   Alerts from LinkedIn, Indeed, Reed, CV-Library, … saved as .eml (or pulled by an agent
   through a mail connector). Each job link becomes a *partial* posting (title, company,
   location, URL), which is enough for title/location scoring and triage.
2. Saved postings    `data/inbox/postings/*.html | *.txt | *.md`
   A job page the user saved from their browser, or pasted text. HTML is read via its
   schema.org JSON-LD. Text falls back to LLM structuring (`fetcher.parse_posting`).
   This gives a full posting with description and skills.
"""

from __future__ import annotations

import email
import re
from datetime import date
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path

from src.core.config import Settings, get_settings
from src.core.llm_provider import LLMProvider
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import (
    extract_jsonld_jobs,
    html_to_text,
    infer_arrangement,
    posting_from_jsonld,
    stable_id,
)

# board -> regex over hrefs; group 1 is the board's job id.
JOB_LINK_PATTERNS: dict[str, re.Pattern[str]] = {
    "linkedin": re.compile(r"https?://(?:[\w-]+\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)"),
    "indeed": re.compile(r"https?://(?:[\w-]+\.)?indeed\.[a-z.]+/[^\"'\s>]*?[?&]jk=([0-9a-f]+)"),
    "reed": re.compile(r"https?://(?:www\.)?reed\.co\.uk/jobs/[\w-]+/(\d+)"),
    "cv_library": re.compile(r"https?://(?:www\.)?cv-library\.co\.uk/job/(\d+)"),
}
CANONICAL_URL = {
    "linkedin": "https://www.linkedin.com/jobs/view/{id}",
    "indeed": "https://uk.indeed.com/viewjob?jk={id}",
    "reed": "https://www.reed.co.uk/jobs/{id}",
    "cv_library": "https://www.cv-library.co.uk/job/{id}",
}
_ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
# Bump when parsing changes so cached alert results are rebuilt (see gmail_alerts).
ALERT_PARSER_VERSION = 3  # 3: posted_at from the email date
# Indeed alerts wrap every job link in a click-tracking redirect (engage./cts.indeed.com),
# so the job id is not in the URL. Jobs are recognised by layout instead: a title link
# followed by "Company [rating] - Location" (or company and location on separate lines).
_INDEED_HOST = re.compile(r"^https?://(?:[\w-]+\.)*indeed\.[a-z.]+/", re.I)
_NOT_A_TITLE = {
    "unsubscribe", "privacy policy", "terms", "help centre", "help center", "manage job alerts",
    "unsubscribe from this job alert", "view job", "learn more", "edit profile", "edit",
    "pause these emails", "yes", "no", "find jobs", "sign in", "this is a bad match",
}  # fmt: skip
_SALARY_LINE = re.compile(r"£|\$|€|\ba (?:year|month|week|day)\b|\ban hour\b", re.I)
_NOISE_LINE = re.compile(
    r"^(?:easily apply|just posted|salary|new|\d+\+? days? ago|urgently hiring)$", re.I
)
_RATING = re.compile(r"\s+\d\.\d$")
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2000-\u200a\u2060\ufeff]")


def parse_alert_email(raw: bytes) -> list[JobPosting]:
    """Extract partial postings from one job-alert email (any supported board)."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    body = msg.get_body(preferencelist=("html", "plain"))
    content = body.get_content() if body is not None else ""
    is_html = body is not None and body.get_content_type() == "text/html"

    jobs: dict[str, JobPosting] = {}
    if is_html:
        anchors = list(_ANCHOR_RE.finditer(content))
        for i, m in enumerate(anchors):
            href, inner = m.group(1).replace("&amp;", "&"), html_to_text(m.group(2))
            if len(inner) < 4 or inner.lower().startswith("jobs similar to"):
                continue
            # Alert layouts put "Company · Location" in the text right after the title link.
            nxt = anchors[i + 1].start() if i + 1 < len(anchors) else m.end() + 400
            tail = [
                ln.strip()
                for ln in html_to_text(content[m.end() : nxt]).splitlines()
                if ln.strip() and not ln.strip().startswith("<")
            ]
            board, job_id = _match_board(href)
            if board and f"{board}:{job_id}" not in jobs:
                jobs[f"{board}:{job_id}"] = _partial(board, job_id, inner, tail)
            elif not board and _INDEED_HOST.match(href):
                job = _indeed_tracked(href, inner, tail)
                if job is not None:
                    jobs.setdefault(job.id, job)
    else:
        for board, pattern in JOB_LINK_PATTERNS.items():
            for m in pattern.finditer(content):
                key = f"{board}:{m.group(1)}"
                if key not in jobs:
                    line = content[: m.start()].rstrip().splitlines()[-1:] or ["Untitled"]
                    jobs[key] = _partial(board, m.group(1), line[0].strip(), [])
    # An alert lists jobs posted no later than it was sent: the closest date we have.
    sent = _sent_on(msg.get("Date"))
    return [j.model_copy(update={"posted_at": j.posted_at or sent}) for j in jobs.values()]


def _sent_on(header: object) -> date | None:
    try:
        return parsedate_to_datetime(str(header)).date() if header else None
    except (TypeError, ValueError):
        return None


def _match_board(href: str) -> tuple[str | None, str]:
    for board, pattern in JOB_LINK_PATTERNS.items():
        m = pattern.search(href)
        if m:
            return board, m.group(1)
    return None, ""


def _partial(board: str, job_id: str, title: str, tail: list[str]) -> JobPosting:
    parts = re.split(r"\s+[·•|–-]\s+", tail[0]) if tail else []
    company = parts[0] if parts else (tail[0] if tail else "unknown")
    location = parts[1] if len(parts) > 1 else (tail[1] if len(tail) > 1 else None)
    return JobPosting(
        id=f"{board}:{job_id}",
        title=title[:200],
        company=company[:120] or "unknown",
        location=location,
        work_arrangement=infer_arrangement(title, location),
        url=CANONICAL_URL[board].format(id=job_id),
        source=f"{board}_alert",
    )


def _indeed_tracked(href: str, title: str, tail: list[str]) -> JobPosting | None:
    """A job from an Indeed alert whose link is a tracking redirect (no job id in the URL)."""
    title = " ".join(_INVISIBLE.sub(" ", title).split())
    if title.lower() in _NOT_A_TITLE or not tail or len(title) > 150:
        return None
    first = tail[0].replace("\xa0", " ")
    if " - " in first:  # "Company  4.1  - Location"
        company, location = (x.strip() for x in first.rsplit(" - ", 1))
        rest = tail[1:]
    elif len(tail) > 1 and not _SALARY_LINE.search(tail[1]):  # "Company" / "Location"
        company, location, rest = first.strip(), tail[1].strip(), tail[2:]
    else:
        return None
    company = _RATING.sub("", " ".join(company.split()))
    # Real entries are short lines; footers and banners are not.
    if not company or company.lower() in _NOT_A_TITLE or len(company) > 80 or len(location) > 60:
        return None
    rest = [r for r in rest if not _NOISE_LINE.match(r)]
    salary = next((r for r in rest[:2] if _SALARY_LINE.search(r) and len(r) < 60), None)
    snippet = next((r for r in rest if len(r) > 60), "")
    return JobPosting(
        id=f"indeed:{stable_id(title.lower(), company.lower(), location.lower())}",
        title=title,
        company=company[:120],
        location=location or None,
        work_arrangement=infer_arrangement(title, location, snippet),
        description=snippet,
        # Indeed estimates missing salaries, so keep it as text only: never filter on it.
        salary_range=salary,
        salary_maybe_estimated=salary is not None,
        url=href,
        source="indeed_alert",
    )


def parse_saved_posting(path: Path, llm: LLMProvider | None = None) -> list[JobPosting]:
    """Full posting(s) from a saved page (.html via JSON-LD) or pasted text (.txt/.md via LLM)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    board = next((b for b, p in JOB_LINK_PATTERNS.items() if p.search(raw)), "manual")
    if path.suffix.lower() in {".html", ".htm"}:
        nodes = extract_jsonld_jobs(raw)
        if nodes:
            return [posting_from_jsonld(n, source=f"{board}_saved", url=None) for n in nodes]
        raw = html_to_text(raw)
    if llm is None:
        raise ValueError(f"{path.name}: no JSON-LD found; pass an LLMProvider to structure text")
    from src.jobs.fetcher import parse_posting  # Local import: fetcher imports this module.

    return [
        parse_posting(
            raw, f"{board}:{stable_id(path.name, raw[:200])}", llm, source=f"{board}_saved"
        )
    ]


class InboxSource:
    """Reads alert emails and saved postings from `Settings.inbox_dir`."""

    def __init__(
        self,
        inbox_dir: Path | None = None,
        llm: LLMProvider | None = None,
        settings: Settings | None = None,
        *,
        board: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.inbox_dir = inbox_dir or self.settings.inbox_dir
        self.llm = llm
        self.board = board
        self.name = board or "inbox"
        self.errors: dict[str, str] = {}

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        if not self.inbox_dir.exists():
            return jobs
        for eml in sorted(self.inbox_dir.glob("*.eml")):
            jobs += parse_alert_email(eml.read_bytes())
        saved = self.inbox_dir / "postings"
        for path in sorted(saved.glob("*")) if saved.exists() else []:
            if path.suffix.lower() in {".html", ".htm", ".txt", ".md"}:
                try:
                    jobs += parse_saved_posting(path, self.llm)
                except ValueError as exc:
                    self.errors[path.name] = str(exc)
        if self.board:
            jobs = [j for j in jobs if j.source.startswith(f"{self.board}_")]
        jobs = [j for j in jobs if query.is_relevant(j) and query.is_recent(j)]
        if query.remote_only:
            jobs = [j for j in jobs if j.work_arrangement == "remote"]
        return jobs[: query.limit]
