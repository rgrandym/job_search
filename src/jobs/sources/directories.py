"""Company directories: lists of employers whose career feeds we then discover.

BioPharmGuy (https://biopharmguy.com) publishes per-country lists of life-science companies
as plain HTML tables (company + website · location · description). Its robots.txt allows
`/links/`; the page is read once per discovery run, not per search.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from pydantic import BaseModel

from src.jobs.sources.base import HttpFetcher, SourceError, html_to_text

BIOPHARMGUY_UK = "https://biopharmguy.com/links/country-united-kingdom-all-location.php"
DIRECTORIES = {"biopharmguy-uk": BIOPHARMGUY_UK}

_ROW = re.compile(
    r'<td class="company">(?P<company>.*?)</td>\s*<td class="location">(?P<location>.*?)</td>'
    r'(?:\s*<td class="description">(?P<description>.*?)</td>)?',
    re.DOTALL,
)
_LINK = re.compile(r'<a\s[^>]*?href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_ALT = re.compile(r'alt="([^"]+)"')


class DirectoryCompany(BaseModel):
    """A company as listed in a directory."""

    name: str
    website: str
    locations: list[str] = []
    description: str = ""


def parse_biopharmguy(page_html: str) -> list[DirectoryCompany]:
    """Companies in a BioPharmGuy list, one per website (sites are merged across locations)."""
    by_site: dict[str, DirectoryCompany] = {}
    for row in _ROW.finditer(page_html):
        link = _LINK.search(row["company"])
        if not link:
            continue
        website = link.group(1)
        name = html_to_text(link.group(2)) or html.unescape(
            next(iter(_ALT.findall(row["company"])), "")
        )
        name = " ".join(name.split())  # also folds the &nbsp; the site puts in names
        if not name:
            continue
        location = html_to_text(row["location"])
        site = urlsplit(website).netloc.lower().removeprefix("www.")
        company = by_site.setdefault(
            site,
            DirectoryCompany(
                name=name,
                website=website,
                description=" ".join(html_to_text(row["description"] or "").split()),
            ),
        )
        if location and location not in company.locations:
            company.locations.append(location)
    return list(by_site.values())


def fetch_directory(http: HttpFetcher, directory: str) -> list[DirectoryCompany]:
    """Read a known directory (see DIRECTORIES) through the polite fetcher."""
    if directory not in DIRECTORIES:
        raise SourceError(f"unknown directory {directory!r}; known: {', '.join(DIRECTORIES)}")
    return parse_biopharmguy(http.get(DIRECTORIES[directory], check_robots=True).text)
