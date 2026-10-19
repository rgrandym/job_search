"""Where a posting's stated town lies against the search radius (a UK place lookup).

Boards that apply the radius server-side mark their postings (`within_search_area`). Other
postings (company feeds, alerts) state a town the matcher cannot place by its name alone. This
looks each stated town up once (postcodes.io `places`: free, no key, UK only) and marks the
posting's `area_check`: "inside" or "outside" the radius (with `distance_miles`), or "abroad"
(no UK place of that name, on a UK search). No town, a county or region, a hamlet-only name or
a failed lookup leaves it unchecked: the advert does not say where, so the posting is kept.
Remote postings are never checked. Answers are remembered in `data/places.json`.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import HttpFetcher, SourceError

PLACES_API = "https://api.postcodes.io/places"
FILE = "places.json"
LOOKUP_DELAY_S = 0.1  # a keyless API meant for this; each town is asked once and remembered
EARTH_MILES = 3958.8

# Settlement types trusted for a town name; a hamlet is too often a namesake of the real town.
_RANK = {"City": 0, "Town": 1, "Suburban Area": 2, "Village": 3, "Other Settlement": 4}
_UK = {
    "uk", "united kingdom", "england", "scotland", "wales", "northern ireland",
    "great britain", "britain", "gb",
}  # fmt: skip
# Counties and regions: a real UK area, but no single point to measure from.
UK_AREAS = frozenset({
    "bedfordshire", "berkshire", "buckinghamshire", "cambridgeshire", "cheshire", "cornwall",
    "cumbria", "derbyshire", "devon", "dorset", "durham", "county durham", "east sussex",
    "essex", "gloucestershire", "greater london", "greater manchester", "hampshire",
    "herefordshire", "hertfordshire", "isle of wight", "kent", "lancashire", "leicestershire",
    "lincolnshire", "merseyside", "norfolk", "north yorkshire", "northamptonshire",
    "northumberland", "nottinghamshire", "oxfordshire", "rutland", "shropshire", "somerset",
    "south yorkshire", "staffordshire", "suffolk", "surrey", "tyne and wear", "warwickshire",
    "west midlands", "west sussex", "west yorkshire", "wiltshire", "worcestershire",
    "east riding of yorkshire", "yorkshire", "south east", "south west", "east of england",
    "east anglia", "east midlands", "midlands", "north east", "north west",
    "yorkshire and the humber", "home counties", "thames valley", "golden triangle",
    "lanarkshire", "north lanarkshire", "south lanarkshire", "fife", "lothian", "west lothian",
    "east lothian", "midlothian", "aberdeenshire", "ayrshire", "highland", "highlands",
    "perthshire", "renfrewshire", "dunbartonshire", "stirlingshire", "angus", "moray",
    "argyll", "scottish borders", "dumfries and galloway", "gwynedd", "powys", "ceredigion",
    "pembrokeshire", "carmarthenshire", "glamorgan", "vale of glamorgan", "monmouthshire",
    "flintshire", "denbighshire", "conwy", "anglesey", "south wales", "north wales",
    "mid wales", "county antrim", "antrim", "county down", "armagh", "tyrone", "fermanagh",
})  # fmt: skip
_NOISE = re.compile(
    r"\([^)]*\)|\b(?:remote|hybrid|on-?site|in[- ]office|flexible)\b"
    r"|\b[A-Z]{1,2}\d[A-Z\d]?(?:\s*\d[A-Z]{2})?\b",  # postcodes and outcodes
    re.I,
)
_SPLIT = re.compile(r"\s*(?:[,;/|]|\s-\s|\bor\b)\s*", re.I)
_UK_WORDS = r"(?:united kingdom|great britain|northern ireland|england|scotland|wales|uk|gb)"
# "United Kingdom-Harrow", "Harrow-UK": the country joined to the town without spaces.
_UK_JOINED = re.compile(rf"^\s*{_UK_WORDS}\s*[-:]\s*|\s*[-:]\s*{_UK_WORDS}\s*$", re.I)
_UK_NAMED = re.compile(rf"\b{_UK_WORDS}\b", re.I)

Point = tuple[float, float]


class PlaceLookup:
    """UK places by name, asked once each and remembered under `cache` (None: in memory)."""

    def __init__(self, http: HttpFetcher, cache: Path | None = None) -> None:
        self.http = http
        self.cache = cache
        self._known: dict[str, dict[str, Any]] = {}
        if cache is not None:
            try:
                self._known = json.loads(cache.read_text())
            except (OSError, ValueError):
                self._known = {}

    def find(self, name: str) -> tuple[bool, list[Point]] | None:
        """(any UK place answers to `name`, the points of the most significant settlements
        named exactly so); None when the lookup failed."""
        key = name.strip().lower()
        if key not in self._known:
            try:
                data = self.http.get(PLACES_API, params={"q": name, "limit": 20}).json()
            except (SourceError, ValueError):
                return None
            rows = [r for r in data.get("result") or [] if isinstance(r, dict)]
            self._known[key] = {"uk": bool(rows), "points": _settlements(rows, key)}
            self._save()
        hit = self._known[key]
        return bool(hit.get("uk")), [(float(a), float(b)) for a, b in hit.get("points") or []]

    def _save(self) -> None:
        if self.cache is None:
            return
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps(self._known))
        except OSError:
            pass  # answers are asked again next time


def _settlements(rows: list[dict[str, Any]], key: str) -> list[list[float]]:
    """Points of the settlements named `key` (or `key`-on-..., as Abingdon-on-Thames), of the
    most significant type among them (Cambridge the city, not the village of that name)."""

    def same(row: dict[str, Any]) -> bool:
        name = str(row.get("name_1", "")).lower()
        return name == key or name.startswith(f"{key}-")

    named = [r for r in rows if r.get("local_type") in _RANK and same(r)]
    if not named:
        return []
    best = min(_RANK[r["local_type"]] for r in named)
    return [
        [float(r["latitude"]), float(r["longitude"])]
        for r in named
        if _RANK[r["local_type"]] == best
    ]


def miles(a: Point, b: Point) -> float:
    """Great-circle distance in miles."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_MILES * math.asin(math.sqrt(h))


def _names(location: str) -> list[str]:
    """The place names a location line states ("United Kingdom - Marlow (Hybrid)" -> marlow)."""
    out = []
    for part in _SPLIT.split(_NOISE.sub(" ", location)):
        name = _UK_JOINED.sub("", re.sub(r"\s+", " ", part)).strip().lower()
        name = re.sub(r"^(?:greater|city of)\s+|\s+area$", "", name)
        if name and name not in _UK and not name.isdigit():
            out.append(name)
    return out


def _verdict(
    location: str, centres: list[Point], radius: float, lookup: PlaceLookup
) -> dict[str, Any] | None:
    """`area_check` and distance for a stated location; None when it cannot be placed."""
    kinds: list[tuple[str, float | None]] = []
    for name in _names(location):
        found = None if name in UK_AREAS else lookup.find(name)
        if found is None:
            kinds.append(("unknown", None))
            continue
        uk, points = found
        if not points:
            # a line that names the UK is never abroad, whatever its town is called
            abroad = not uk and not _UK_NAMED.search(location)
            kinds.append(("abroad" if abroad else "unknown", None))
            continue
        far = min(miles(c, p) for c in centres for p in points)
        kinds.append(("inside" if far <= radius else "outside", far))
    inside = [m for k, m in kinds if k == "inside" and m is not None]
    if inside:  # one stated place in reach is enough
        return {"area_check": "inside", "distance_miles": round(min(inside), 1)}
    if not kinds or any(k == "unknown" for k, _ in kinds):
        return None
    outside = [m for k, m in kinds if k == "outside" and m is not None]
    if outside:
        return {"area_check": "outside", "distance_miles": round(min(outside), 1)}
    return {"area_check": "abroad"}


def check_area(jobs: list[JobPosting], query: SearchQuery, lookup: PlaceLookup) -> list[JobPosting]:
    """Mark each non-remote posting's stated town inside or outside the search radius. Only
    for a UK search with a radius around towns the lookup can place."""
    country = (query.country or "").strip().lower()
    if query.distance_miles is None or not query.locations or (country and country not in _UK):
        return jobs
    centres = [p for loc in query.locations for p in (lookup.find(loc) or (False, []))[1]]
    if not centres:
        return jobs
    out = []
    for job in jobs:
        if job.work_arrangement == "remote" or job.within_search_area or not job.location:
            out.append(job)
            continue
        verdict = _verdict(job.location, centres, query.distance_miles, lookup)
        out.append(job.model_copy(update=verdict) if verdict else job)
    return out
