"""The stated town against the search radius (`src/jobs/places.py`), with a mocked lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.core.config import Settings
from src.jobs import scorer
from src.jobs.matcher import profile_from_query
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.places import PlaceLookup, check_area
from src.jobs.sources.base import HttpFetcher

REAL_FIND = PlaceLookup.find  # conftest keeps searches offline; these tests mock the API

# What postcodes.io answered for these names (2026-10-19), trimmed to the fields used.
PLACES: dict[str, list[dict[str, Any]]] = {
    "oxford": [{"name_1": "Oxford", "local_type": "City", "latitude": 51.75, "longitude": -1.26}],
    "abingdon": [
        {
            "name_1": "Abingdon-on-Thames",
            "local_type": "Town",
            "latitude": 51.67,
            "longitude": -1.28,
        }
    ],
    "edinburgh": [
        {"name_1": "Edinburgh", "local_type": "City", "latitude": 55.95, "longitude": -3.19}
    ],
    "cambridge": [
        {"name_1": "Cambridge", "local_type": "Village", "latitude": 51.73, "longitude": -2.37},
        {"name_1": "Cambridge", "local_type": "City", "latitude": 52.2, "longitude": 0.12},
    ],
    "marlow": [
        {"name_1": "Marlow", "local_type": "Hamlet", "latitude": 52.37, "longitude": -2.86},
        {"name_1": "Marlow", "local_type": "Town", "latitude": 51.57, "longitude": -0.77},
    ],
    "alderley": [
        {"name_1": "Alderley", "local_type": "Hamlet", "latitude": 51.6, "longitude": -2.3}
    ],
}


@pytest.fixture
def lookup(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PlaceLookup:
    monkeypatch.setattr(PlaceLookup, "find", REAL_FIND)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"].lower()
        asked.append(q)
        return httpx.Response(200, json={"status": 200, "result": PLACES.get(q, [])})

    found = PlaceLookup(HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler))))
    found.asked = asked  # type: ignore[attr-defined]
    return found


def _job(location: str | None, arrangement: str = "onsite") -> JobPosting:
    return JobPosting(
        id=f"x:{location}:{arrangement}",
        title="Scientist",
        company="Acme",
        location=location,
        work_arrangement=arrangement,  # type: ignore[arg-type]
    )


OXFORD_80 = SearchQuery(locations=["Oxford"], distance_miles=80, country="United Kingdom")


def test_each_stated_town_is_measured_against_the_radius(lookup: PlaceLookup) -> None:
    places = [
        "Abingdon",  # 6 miles: kept
        "Edinburgh (On-site)",  # about 290 miles
        "Seoul",  # no UK place of that name
        "Salt Lake City, Utah",
        "United Kingdom - Marlow",  # the town in a Workday line, 25 miles
        "United Kingdom-Marlow",  # the same, joined without spaces
        "Nowhereton, United Kingdom",  # unknown, but the line says UK: kept
        "Cambridge",  # the city, not the Gloucestershire village of that name
        "Kent (On-site)",  # a county: no point to measure from
        "Alderley",  # only a hamlet answers to it: not placed
    ]
    jobs = check_area(
        [_job(p) for p in places] + [_job("Seoul", "remote"), _job(None)], OXFORD_80, lookup
    )
    found = {j.location: (j.area_check, j.distance_miles) for j in jobs[:-2]}  # on-site ones

    assert found["Abingdon"][0] == "inside" and (found["Abingdon"][1] or 99) < 10
    assert (
        found["Edinburgh (On-site)"][0] == "outside"
        and (found["Edinburgh (On-site)"][1] or 0) > 250
    )
    assert found["Seoul"] == ("abroad", None) and found["Salt Lake City, Utah"] == ("abroad", None)
    assert found["United Kingdom - Marlow"][0] == "inside"
    assert found["United Kingdom-Marlow"][0] == "inside"
    assert found["Nowhereton, United Kingdom"] == (None, None)
    assert found["Cambridge"][0] == "inside" and (found["Cambridge"][1] or 0) > 60
    assert found["Kent (On-site)"] == (None, None) and found["Alderley"] == (None, None)
    assert jobs[-2].area_check is None and jobs[-1].area_check is None  # remote · no location


def test_outside_or_abroad_is_excluded_unless_remote_or_willing_to_relocate(
    lookup: PlaceLookup,
) -> None:
    jobs = {
        j.location: j
        for j in check_area([_job("Edinburgh"), _job("Seoul"), _job("Abingdon")], OXFORD_80, lookup)
    }
    oxford = profile_from_query(OXFORD_80)

    assert "outside the search radius" in scorer.hard_exclusions(oxford, jobs["Edinburgh"])[0]
    assert "not a UK place" in scorer.hard_exclusions(oxford, jobs["Seoul"])[0]
    assert scorer.hard_exclusions(oxford, jobs["Abingdon"]) == []
    assert scorer.location_match(oxford.locations, jobs["Abingdon"]) == scorer.LOC_SAME_CITY
    movable = oxford.model_copy(update={"willing_to_relocate": True})
    assert scorer.hard_exclusions(movable, jobs["Edinburgh"]) == []


def test_no_check_without_a_uk_radius_or_when_the_lookup_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, lookup: PlaceLookup
) -> None:
    far = [_job("Edinburgh")]
    assert check_area(far, OXFORD_80.model_copy(update={"distance_miles": None}), lookup) == far
    assert check_area(far, OXFORD_80.model_copy(update={"country": "Germany"}), lookup) == far

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    broken = PlaceLookup(HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(down))))
    assert check_area(far, OXFORD_80, broken)[0].area_check is None  # unplaced: kept


def test_each_town_is_asked_once_and_remembered(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(PlaceLookup, "find", REAL_FIND)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["q"].lower())
        return httpx.Response(200, json={"result": PLACES.get(asked[-1], [])})

    def fresh() -> PlaceLookup:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return PlaceLookup(HttpFetcher(settings, client), tmp_path / "places.json")

    check_area([_job("Edinburgh"), _job("Edinburgh, Scotland")], OXFORD_80, fresh())
    assert asked == ["oxford", "edinburgh"]
    again = check_area([_job("Edinburgh")], OXFORD_80, fresh())  # a later search
    assert asked == ["oxford", "edinburgh"] and again[0].area_check == "outside"
