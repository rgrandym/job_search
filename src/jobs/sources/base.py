"""Shared plumbing for job sources: polite HTTP, robots.txt, HTML/JSON-LD parsing.

Compliance rules (enforced here, documented in CLAUDE.md):
- Only official APIs, public ATS feeds, or pages whose robots.txt allows us.
- Identify ourselves with a real User-Agent and rate-limit every host.
- No login walls, no CAPTCHA/anti-bot circumvention, no proxies to evade blocks.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from src.core.config import Settings, get_settings
from src.cv.models import WorkArrangement
from src.jobs.models import JobPosting


class SourceError(RuntimeError):
    """A source could not be queried (missing key, HTTP error, disallowed by robots.txt)."""


class HttpFetcher:
    """httpx wrapper with per-host delay and robots.txt checks. Inject `client` in tests."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(
            timeout=self.settings.http_timeout_s,
            headers={"User-Agent": self.settings.http_user_agent},
            follow_redirects=True,
        )
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}

    def get(self, url: str, *, check_robots: bool = False, **kwargs: Any) -> httpx.Response:
        """GET with politeness delay. `check_robots=True` for non-API page fetches."""
        host = urlsplit(url).netloc
        if check_robots and not self._allowed(url):
            raise SourceError(f"robots.txt disallows {url}")
        wait = self.settings.request_delay_s - (time.monotonic() - self._last_hit.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.client.get(url, **kwargs)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError(f"GET {url} failed: {exc}") from exc
        finally:
            self._last_hit[host] = time.monotonic()
        return resp

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            rp = RobotFileParser()
            try:
                resp = self.client.get(f"{root}/robots.txt")
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[root] = rp
        return self._robots[root].can_fetch(self.settings.http_user_agent, url)


# ------------------------------------------------------------------ parsing helpers

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr)[^>]*>", re.IGNORECASE)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def html_to_text(raw: str) -> str:
    """Crude but dependency-free HTML -> text (handles escaped HTML too)."""
    text = html.unescape(raw)
    text = _BLOCK_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def stable_id(*parts: object) -> str:
    """Deterministic short id from arbitrary parts (process-independent, unlike hash())."""
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def infer_arrangement(*texts: str | None) -> WorkArrangement:
    """Guess remote/hybrid/onsite from free text."""
    low = " ".join(t for t in texts if t).lower()
    if "hybrid" in low:
        return "hybrid"
    if re.search(r"\bremote\b|work from home|\bwfh\b|telecommute", low):
        return "remote"
    return "onsite"


def parse_date(value: Any) -> date | None:
    """Accept ISO strings, dd/mm/yyyy, or epoch milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000).date()
    s = str(value)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def extract_jsonld_jobs(page_html: str) -> list[dict[str, Any]]:
    """All schema.org `JobPosting` objects embedded in a page's JSON-LD."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            types = node.get("@type")
            if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
                found.append(node)
            for key in ("@graph", "itemListElement", "item"):
                if key in node:
                    walk(node[key])

    for block in _JSONLD_RE.findall(page_html):
        try:
            walk(json.loads(block.strip()))
        except json.JSONDecodeError:
            continue
    return found


def posting_from_jsonld(node: dict[str, Any], *, source: str, url: str | None) -> JobPosting:
    """Map a schema.org JobPosting dict to our `JobPosting`."""
    org = node.get("hiringOrganization") or {}
    company = org.get("name", "unknown") if isinstance(org, dict) else str(org)
    loc = node.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
    location = (
        ", ".join(
            str(addr[k])
            for k in ("addressLocality", "addressRegion", "addressCountry")
            if isinstance(addr, dict) and addr.get(k) and not isinstance(addr[k], dict)
        )
        or None
    )
    description = html_to_text(str(node.get("description", "")))
    arrangement: WorkArrangement = (
        "remote"
        if node.get("jobLocationType") == "TELECOMMUTE"
        else infer_arrangement(node.get("title"), location, description[:500])
    )
    ident = node.get("identifier")
    ext_id = ident.get("value") if isinstance(ident, dict) else ident
    skills = node.get("skills")
    return JobPosting(
        id=f"{source}:{ext_id or stable_id(node.get('title'), company, location)}",
        title=str(node.get("title", "")).strip() or "Untitled",
        company=company,
        location=location,
        work_arrangement=arrangement,
        description=description,
        required_skills=[s.strip() for s in skills.split(",")] if isinstance(skills, str) else [],
        url=node.get("url") or url,
        posted_at=parse_date(node.get("datePosted")),
        source=source,
    )
