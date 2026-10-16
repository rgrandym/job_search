"""Focused posting lookup never starts profile search or screening."""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.jobs.models import JobPosting
from src.jobs.sources.base import SourceError
from src.services import one_job
from src.services.workspace import Workspace


@pytest.fixture
def ws(settings: Settings) -> Workspace:
    return Workspace(settings)


WEB_RESULTS = one_job._web_results  # the real search, for the tests that drive it offline


@pytest.fixture(autouse=True)
def _no_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Board and feed tests run with an empty web search (never the network)."""
    monkeypatch.setattr(one_job, "_web_results", lambda *_a: [])


@pytest.fixture(autouse=True)
def _no_posting_reads(monkeypatch: pytest.MonkeyPatch) -> list[list[JobPosting]]:
    """Opening thin postings is the search's own routine; offline here, it reads nothing."""
    opened: list[list[JobPosting]] = []

    def enrich(jobs: list[JobPosting], *_args: object, **_kw: object) -> dict[str, JobPosting]:
        opened.append(jobs)
        return {}

    monkeypatch.setattr(one_job.search_service, "_enrich", enrich)
    return opened


def test_find_one_uses_small_source_query_and_all_supplied_clues(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(one_job, "build_sources", lambda names, **_: (names, {}))

    def fetched(sources: object, query: object) -> tuple[list[JobPosting], dict[str, str]]:
        seen.extend([sources, query])
        return [
            JobPosting(
                id="adzuna:123",
                title="Senior Research Manager",
                company="Example Bio",
                description="Lead laboratory research and technical reports.",
                posted_at=date(2026, 10, 10),
                source="adzuna",
            ),
            JobPosting(
                id="adzuna:456",
                title="Senior Research Manager",
                company="Other Bio",
                description="Lead laboratory research and technical reports.",
                posted_at=date(2026, 10, 10),
                source="adzuna",
            ),
        ], {}

    monkeypatch.setattr(one_job, "fetch_all", fetched)
    found = one_job.find(
        ws,
        one_job.OneJobQuery(
            title="Senior Research Manager",
            company="Example Bio",
            posted_at=date(2026, 10, 10),
            description="laboratory research reports",
        ),
    )
    assert [job.id for job in found.jobs] == ["adzuna:123"]
    assert seen[0] == one_job.SOURCES
    # The employer goes into the board search with the title, so its posting is not lost
    # among every other employer's postings with that title.
    assert seen[1].limit == one_job.BOARD_LIMIT
    assert seen[1].search_terms() == ["Senior Research Manager Example Bio"]


def test_keyword_only_lookup_does_not_expand_to_profile_search(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[object] = []
    monkeypatch.setattr(one_job, "build_sources", lambda names, **_: (names, {}))

    def fetched(_sources: object, query: object) -> tuple[list[JobPosting], dict[str, str]]:
        captured.append(query)
        return [
            JobPosting(
                id="a", title="Researcher", company="Acme", description="GMP work with cell therapy"
            ),
            JobPosting(
                id="b", title="Researcher", company="Acme", description="GMP work with genomics"
            ),
        ], {}

    monkeypatch.setattr(one_job, "fetch_all", fetched)
    found = one_job.find(ws, one_job.OneJobQuery(keywords="GMP, cell therapy"))
    assert [job.id for job in found.jobs] == ["a"]
    assert captured[0].search_terms() == ["gmp cell therapy"]


def test_find_one_rejects_date_only_and_local_links(ws: Workspace) -> None:
    with pytest.raises(ValueError, match="date alone"):
        one_job.find(ws, one_job.OneJobQuery(posted_at=date(2026, 10, 10)))
    with pytest.raises(ValueError, match="public"):
        one_job.find(ws, one_job.OneJobQuery(url="http://192.168.1.1/job"))


def test_posting_link_refuses_redirect_to_local_network(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Fetcher:
        def __init__(self, _settings: Settings) -> None:
            pass

        def get(self, url: str, **_kwargs: object) -> httpx.Response:
            calls.append(url)
            return httpx.Response(302, headers={"location": "http://192.168.1.1/admin"})

    monkeypatch.setattr(one_job, "HttpFetcher", Fetcher)
    found = one_job.find(ws, one_job.OneJobQuery(url="https://example.org/jobs/123"))
    assert calls == ["https://example.org/jobs/123"]
    assert "local" in found.errors["link"]


def test_specific_posting_link_reads_jsonld_without_searching_boards(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = """<script type="application/ld+json">{
      "@context":"https://schema.org","@type":"JobPosting",
      "title":"Research Manager","hiringOrganization":{"name":"Example Bio"},
      "description":"Lead a research programme.",
      "url":"https://careers.example.org/postings/123"
    }</script>"""

    class Fetcher:
        def __init__(self, _settings: Settings) -> None:
            pass

        def get(self, _url: str, **_kwargs: object) -> httpx.Response:
            return httpx.Response(200, text=page)

    monkeypatch.setattr(one_job, "HttpFetcher", Fetcher)
    monkeypatch.setattr(one_job, "build_sources", lambda *_a, **_kw: pytest.fail("board queried"))
    found = one_job.find(ws, one_job.OneJobQuery(url="https://example.org/jobs/123"))
    assert [(job.title, job.company) for job in found.jobs] == [("Research Manager", "Example Bio")]


POSTING = """<script type="application/ld+json">{
  "@context":"https://schema.org","@type":"JobPosting",
  "title":"Senior Program Manager R&D","hiringOrganization":{"name":"Lonza"},
  "description":"Lead complex cross-functional research programs."
}</script><p>Reference: R79349</p>"""
EMPLOYER = "https://www.lonza.com/jobs/R79349"
MIRROR = "https://jobs.example.org/senior-program-manager-rd-at-lonza-r79349"


def _results(*links: tuple[str, str]) -> str:
    """A DuckDuckGo HTML results page."""
    return "".join(
        f'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg='
        f'{quote(url, safe="")}&amp;rut=x">{title}</a>'
        for url, title in links
    )


def _web(monkeypatch: pytest.MonkeyPatch, searches: list[str], pages: dict[str, str]) -> list[str]:
    """Drive the real web search offline: each POST answers with the next results page; each
    page GET with `pages` (a missing page is refused, as a bot-blocking site does)."""
    asked: list[str] = []

    class Fetcher:
        def __init__(self, _settings: Settings) -> None:
            pass

        def post(self, url: str, *, data: dict[str, str], **_kw: object) -> httpx.Response:
            assert url == one_job.WEB_SEARCH
            asked.append(data["q"])
            return httpx.Response(200, text=searches.pop(0))

        def get(self, url: str, **_kw: object) -> httpx.Response:
            if url not in pages:
                raise SourceError(f"GET {url} failed: 403 Forbidden")
            return httpx.Response(200, text=pages[url])

    monkeypatch.setattr(one_job, "_web_results", WEB_RESULTS)
    monkeypatch.setattr(one_job, "HttpFetcher", Fetcher)
    return asked


def test_web_search_finds_the_posting_by_requisition_and_links_the_employer(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = _results(
        (EMPLOYER, "Senior Program Manager R&amp;D - lonza.com"),
        (MIRROR, "Senior Program Manager R&amp;D at Lonza | Jobs"),
    )
    asked = _web(monkeypatch, [results], {MIRROR: POSTING})
    monkeypatch.setattr(one_job, "build_sources", lambda *_a, **_kw: pytest.fail("board queried"))
    found = one_job.find(
        ws, one_job.OneJobQuery(title="Senior Program Manager R&D", job_id="R79349")
    )
    assert asked == ["R79349 Senior Program Manager R&D"]  # one search
    # read from the copy whose page loads; applied for on the employer's own page
    assert [(job.title, job.company, job.url) for job in found.jobs] == [
        ("Senior Program Manager R&D", "Lonza", EMPLOYER)
    ]
    assert "cross-functional" in found.jobs[0].description


def test_an_id_alone_searches_again_with_the_title_it_finds(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _results((EMPLOYER, "Senior Program Manager R&amp;D - lonza.com"))
    second = _results((MIRROR, "Senior Program Manager R&amp;D at Lonza"))
    asked = _web(monkeypatch, [first, second], {MIRROR: POSTING})
    found = one_job.find(ws, one_job.OneJobQuery(job_id="R79349"))
    assert asked == ["R79349", "R79349 Senior Program Manager R&D"]
    assert [(job.title, job.company) for job in found.jobs] == [
        ("Senior Program Manager R&D", "Lonza")
    ]


def test_a_page_without_the_requisition_does_not_count(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = POSTING.replace("R79349", "R10000")
    _web(monkeypatch, [_results((MIRROR, "Senior Program Manager R&amp;D"))], {MIRROR: other})
    monkeypatch.setattr(one_job, "build_sources", lambda *_a, **_kw: ([], {}))
    found = one_job.find(
        ws, one_job.OneJobQuery(title="Senior Program Manager R&D", job_id="R79349")
    )
    assert not found.jobs


def test_a_linkedin_result_is_read_by_linkedin_without_rate_limit_waits(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = "https://uk.linkedin.com/jobs/view/senior-program-manager-r-d-at-lonza-4468066649"
    _web(monkeypatch, [_results((view, "Senior Program Manager R&amp;D at Lonza"))], {})
    used: list[int] = []

    def enrich(jobs: list[JobPosting], _s: object, _e: object, settings: Settings) -> object:
        used.append(settings.linkedin_max_cooldowns)
        job = jobs[0].model_copy(update={"company": "Lonza", "description": "R79349 ..."})
        return {job.id: job}

    monkeypatch.setattr(one_job.search_service, "_enrich", enrich)
    found = one_job.find(
        ws, one_job.OneJobQuery(title="Senior Program Manager R&D", job_id="R79349")
    )
    assert [job.id for job in found.jobs] == ["linkedin:4468066649"]
    assert used == [0]  # a refusal ends the attempt; one job never waits out a rate limit


def test_without_web_results_the_named_employers_own_feed_is_read(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.jobs.sources.companies import CompanyBoard, save_companies

    save_companies(
        ws.settings.companies_path,
        [CompanyBoard(name="AstraZeneca", ats="greenhouse", token="az"),
         CompanyBoard(name="Lonza", ats="greenhouse", token="lonza")],
    )  # fmt: skip
    read: list[list[str]] = []

    def feed(self: object, query: object) -> list[JobPosting]:
        read.append([c.name for c in self.companies])  # type: ignore[attr-defined]
        return [
            JobPosting(
                id="greenhouse:az:1",
                title="Scientist, Bioscience",
                company="AstraZeneca",
                description="Cell biology. " * 60,
            )
        ]

    monkeypatch.setattr(one_job.CompanyCareersSource, "fetch", feed)
    monkeypatch.setattr(one_job, "build_sources", lambda *_a, **_kw: ([], {}))
    found = one_job.find(ws, one_job.OneJobQuery(title="Scientist", company="Astra Zeneca plc"))
    assert read == [["AstraZeneca"]]
    assert [job.id for job in found.jobs] == ["greenhouse:az:1"]


def test_posting_date_allows_board_dating_differences_and_keywords_rank(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def posting(ident: str, day: int, text: str) -> JobPosting:
        return JobPosting(
            id=ident,
            title="Research Scientist",
            company="Acme",
            description=text,
            posted_at=date(2026, 10, day),
        )

    jobs = [
        posting("plain", 10, "General laboratory research."),
        posting("crispr", 11, "CRISPR screens in cell therapy."),
        posting("old", 1, "CRISPR screens in cell therapy."),
    ]
    monkeypatch.setattr(one_job, "build_sources", lambda names, **_: (names, {}))
    monkeypatch.setattr(one_job, "fetch_all", lambda *_a: (jobs, {}))
    found = one_job.find(ws, one_job.OneJobQuery(
        title="Research Scientist", keywords="CRISPR, cell therapy", posted_at=date(2026, 10, 10),
    ))  # fmt: skip
    assert [job.id for job in found.jobs] == ["crispr"]


def test_pasted_linkedin_link_is_read_through_linkedin(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def enrich(jobs: list[JobPosting], *_args: object, **_kw: object) -> dict[str, JobPosting]:
        job = jobs[0]
        assert (job.id, job.source) == ("linkedin:4012345678", "linkedin_search")
        return {job.id: job.model_copy(update={
            "title": "Scientist", "company": "Acme", "description": "Run assays.",
        })}  # fmt: skip

    monkeypatch.setattr(one_job.search_service, "_enrich", enrich)
    monkeypatch.setattr(one_job, "HttpFetcher", lambda *_a: pytest.fail("page fetched"))
    found = one_job.find(ws, one_job.OneJobQuery(
        url="https://www.linkedin.com/jobs/view/scientist-at-acme-4012345678/?trk=x",
    ))  # fmt: skip
    assert [(job.title, job.company) for job in found.jobs] == [("Scientist", "Acme")]


def test_linkedin_posting_page_names_a_job_opened_from_a_link() -> None:
    from src.jobs.sources.public_boards import linkedin_details

    page = """<h2 class="top-card-layout__title topcard__title">Senior Scientist</h2>
    <a class="topcard__org-name-link topcard-link" href="#">Acme Bio</a>
    <div class="show-more-less-html__markup">Run assays.</div>"""
    stub = JobPosting(id="linkedin:1", title="?", company="?")
    update = linkedin_details(page, stub)
    assert (update["title"], update["company"]) == ("Senior Scientist", "Acme Bio")
    named = JobPosting(id="linkedin:2", title="Scientist", company="Acme")
    assert "title" not in linkedin_details(page, named)


def test_api_find_and_save_found_job(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.web import app as webapp

    job = JobPosting(
        id="reed:123",
        title="Research Manager",
        company="Example Bio",
        description="Manage laboratory research programmes.",
        source="reed",
    )
    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    monkeypatch.setattr(one_job, "find", lambda *_args: one_job.OneJobResults(jobs=[job]))
    client = TestClient(webapp.app)
    response = client.post("/api/jobs/find-one", json={"title": "Research Manager"})
    assert response.status_code == 200 and response.json()["jobs"][0]["id"] == job.id
    saved_response = client.post("/api/saved/found", json=job.model_dump(mode="json"))
    assert saved_response.status_code == 200
    assert ws.job(job.id) is not None
    assert client.get("/api/saved").json()[0]["result"]["job"]["id"] == job.id


def test_a_saved_job_without_its_posting_can_read_it_again_or_take_pasted_text(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import saved
    from src.web import app as webapp

    thin = JobPosting(
        id="linkedin:4478453986",
        title="Senior Manager Preclinical Research",
        company="Immunocore",
        url="https://www.linkedin.com/jobs/view/4478453986",
        source="linkedin_search",
    )
    full = thin.model_copy(update={"description": "Lead preclinical programmes. " * 40})
    saved.save_found(ws, thin.model_copy(update={"description": "Snippet"}))
    assert saved.save_found(ws, full).result.job.description == full.description  # fuller kept
    saved.update_posting(ws, thin)  # empty again
    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    client = TestClient(webapp.app)

    monkeypatch.setattr(one_job, "find", lambda *_a: one_job.OneJobResults())
    failed = client.post(f"/api/saved/{thin.id}/posting", json={})
    assert failed.status_code == 422 and "paste" in failed.json()["detail"]
    short = client.post(f"/api/saved/{thin.id}/posting", json={"description": "short"})
    assert short.status_code == 422

    queries: list[one_job.OneJobQuery] = []

    def find(_ws: Workspace, query: one_job.OneJobQuery) -> one_job.OneJobResults:
        queries.append(query)
        return one_job.OneJobResults(jobs=[full])

    monkeypatch.setattr(one_job, "find", find)
    read = client.post(f"/api/saved/{thin.id}/posting", json={})
    assert read.status_code == 200
    assert read.json()["result"]["job"]["description"] == full.description
    assert queries[0].url == thin.url  # the posting's own link first
    pasted = "Essential: preclinical study design and programme management. " * 5
    done = client.post(f"/api/saved/{thin.id}/posting", json={"description": pasted})
    assert done.json()["result"]["job"]["description"] == pasted.strip()
    assert client.get("/api/saved").json()[0]["result"]["job"]["description"] == pasted.strip()


def test_a_link_that_blocks_reading_is_found_on_the_web_by_its_id(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = _results((MIRROR, "Senior Program Manager R&amp;D at Lonza"))
    # the employer page is refused; the ID in its link finds the copy
    asked = _web(monkeypatch, [results, results], {MIRROR: POSTING})
    found = one_job.find(ws, one_job.OneJobQuery(url=EMPLOYER))
    assert asked == ["R79349", "R79349 Senior Program Manager R&D"]
    assert [(job.title, job.company, job.url) for job in found.jobs] == [
        ("Senior Program Manager R&D", "Lonza", EMPLOYER)  # applied for on the user's link
    ]
    assert found.errors == {}


def test_an_id_in_the_link_query_is_a_search_clue() -> None:
    sf = "https://peoplecore.my.salesforce-sites.com/recruit/fRecruit__ApplyJob?vacancyNo=VN887"
    assert one_job._id_in_link(sf) == "VN887"
    assert one_job._id_in_link(EMPLOYER) == "R79349"
    assert one_job._id_in_link("https://example.com/careers?lang=en&page=2") == ""


def test_a_link_robots_txt_refuses_says_so_and_is_searched_by_its_id(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = "https://careers.example.com/recruit/ApplyJob?vacancyNo=VN887"

    def enrich(jobs: list[JobPosting], _s: object, errors: dict[str, str], *_a: object) -> dict:
        errors[f"enrich {jobs[0].id}"] = f"robots.txt disallows {link}"
        return {}

    monkeypatch.setattr(one_job.search_service, "_enrich", enrich)
    asked = _web(monkeypatch, [_results()], {})  # no page is readable, the web finds nothing
    found = one_job.find(ws, one_job.OneJobQuery(url=link))
    assert asked == ["VN887"]  # the vacancy number in the link was searched for
    assert found.jobs == []
    assert "robots.txt" in found.errors["link"] and "paste" in found.errors["link"]
    assert not any(key.startswith("enrich ") for key in found.errors)


def test_the_users_own_link_is_read_even_where_robots_txt_turns_readers_away(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[bool] = []

    class Fetcher:
        def __init__(self, _settings: Settings) -> None:
            pass

        def get(self, url: str, *, check_robots: bool = False, **_kw: object) -> httpx.Response:
            checked.append(check_robots)
            if check_robots:
                raise SourceError(f"robots.txt disallows {url}")
            return httpx.Response(200, text=POSTING)

    monkeypatch.setattr(one_job, "HttpFetcher", Fetcher)
    found = one_job.find(ws, one_job.OneJobQuery(url=EMPLOYER))
    assert [(job.title, job.company, job.url) for job in found.jobs] == [
        ("Senior Program Manager R&D", "Lonza", EMPLOYER)
    ]
    assert checked == [False]  # one page, the one the user asked for
