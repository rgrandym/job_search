"""Profile construction and matching: dated CV text, role families (tiers, evidence checks,
query budget, attribution, yield), career intent (alignment), languages and eligibility."""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

import pytest

from src.core.config import Settings
from src.cv.models import MasterCV
from src.jobs import scorer
from src.jobs.matcher import JobMatcher, build_profile
from src.jobs.models import (
    ALIGNMENT_PENALTY,
    FIT_WEIGHTS,
    FitRatings,
    JobAssessment,
    JobPosting,
    LanguageSkill,
    MatchReport,
    MatchResult,
    ProfileSummary,
    RoleFamily,
    SearchIntent,
    SearchQuery,
)
from src.jobs.profile_memory import (
    ProfileMemory,
    check_families,
    family_of,
    family_terms,
    intent_text,
    numbered_source,
    pivot_titles,
    profile_cv_text,
    restore_missing_family_gaps,
    summarize_profile,
)
from src.jobs.screener import MATCHER_SYSTEM, ScreeningBatch, combine, finalize, job_digest
from src.services import history, intent, search_service
from src.services.search_service import SearchRequest, list_profiles, run_search
from src.services.workspace import Workspace
from src.tools.search_tools import posting_excerpt, shares_role_words
from tests.conftest import FakeLLM

CORE = RoleFamily(name="ML engineering", tier="core", titles=["Machine Learning Engineer"])
LEAD = RoleFamily(
    name="ML leadership",
    tier="progression",
    titles=["Machine Learning Engineering Manager"],
    evidence=["nimbus-2"],
    gap="no line management yet",
)
BD = RoleFamily(
    name="Business development",
    tier="adjacent",
    titles=["Business Development Manager"],
    evidence=["nimbus-1", "[lexa-1]", "made-up-id"],
    gap="no commercial targets",
    rationale="Technical seller for ML products",
    requested=True,
)
SALES = RoleFamily(
    name="Solutions consulting",
    tier="adjacent",
    titles=["Solutions Consultant"],
    evidence=["nimbus-1", "lexa-2"],
    gap="no client-facing role",
)


def _summary(*families: RoleFamily, not_a_fit: list[str] | None = None) -> ProfileSummary:
    return ProfileSummary(
        headline="Senior ML engineer",
        seniority="senior",
        years_experience=8,
        core_expertise=["recommenders"],
        key_skills=[],
        target_roles=["Machine Learning Engineer"],
        not_a_fit=not_a_fit or [],
        summary="ML engineer.",
        role_families=list(families),
    )


def _assessment(levels: tuple[int, ...], **kw: Any) -> JobAssessment:
    ratings = FitRatings(**dict(zip(FIT_WEIGHTS, levels, strict=True)))
    return JobAssessment(job_id="j", ratings=ratings, fit_summary="x", **kw)


# ---------------------------------------------------------------- profile construction


def test_profile_text_keeps_dates_ids_languages_and_location(master_cv: MasterCV) -> None:
    text = profile_cv_text(master_cv)
    assert "[nimbus] Senior Machine Learning Engineer at" in text
    assert "(2021-03 to " in text and " years)" in text  # seniority needs the dates
    assert "  [nimbus-1] " in text  # families cite bullet ids as evidence
    assert "Languages: English (C2), Portuguese (native)" in text
    assert master_cv.basics.location and master_cv.basics.location in text


def test_summary_prompt_uses_dated_text_and_intent(master_cv: MasterCV, tmp_path: Any) -> None:
    llm = FakeLLM({ProfileSummary: _summary(CORE, BD)})
    wants = SearchIntent(direction="Commercial side of ML", target_areas=["business development"])
    summary, _ = summarize_profile(
        master_cv, None, llm, ProfileMemory(tmp_path / "m.json"), intent=wants
    )
    prompt = llm.calls[0][0]
    assert "<career_intent>" in prompt and "target_areas: business development" in prompt
    assert "[nimbus-1]" in prompt and "2021-03" in prompt
    bd = summary.role_families[1]
    assert bd.requested and bd.rejected is None and bd.evidence == ["nimbus-1", "lexa-1"]


def test_source_document_lines_are_citable_evidence(master_cv: MasterCV, tmp_path: Any) -> None:
    source = "Jane Doe\n\n  Publications  \nDoe J. Graph recommenders. Nature 2023.\n"
    numbered, ids = numbered_source(source)
    assert numbered.splitlines() == [
        "[src-1] Jane Doe",
        "[src-2] Publications",
        "[src-3] Doe J. Graph recommenders. Nature 2023.",
    ]
    assert ids == {"src-1", "src-2", "src-3"}

    consulting = RoleFamily(
        name="Scientific consulting",
        tier="adjacent",
        titles=["Scientific Consultant"],
        evidence=["[src-3]", "nimbus-1", "src-99"],
        gap="no consulting role",
    )
    llm = FakeLLM({ProfileSummary: _summary(CORE, consulting)})
    memory = ProfileMemory(tmp_path / "m.json")
    summary, _ = summarize_profile(master_cv, None, llm, memory, source_text=source)
    prompt = llm.calls[0][0]
    assert "<source_document>\n[src-1] Jane Doe" in prompt and "<cv>" in prompt
    fam = summary.role_families[1]
    assert fam.evidence == ["src-3", "nimbus-1"] and fam.rejected is None  # src-99 doesn't exist
    # Re-checks run without the document and keep the src ids verified at build time.
    again, cached = summarize_profile(master_cv, None, llm, memory)
    assert cached and again.role_families[1].evidence == ["src-3", "nimbus-1"]
    # Without a document no src id is accepted.
    plain, _ = summarize_profile(master_cv, None, llm, memory, refresh=True)
    assert plain.role_families[1].evidence == ["nimbus-1"] and plain.role_families[1].rejected


def test_summary_prompt_asks_for_capabilities_publications_and_adjacent_routes() -> None:
    from src.jobs.profile_memory import SUMMARY_SYSTEM

    for phrase in ("capabilities", "publications", "business development", "[src-N]", "gap"):
        assert phrase in SUMMARY_SYSTEM
    # Leadership carries into senior roles in a neighbouring function, at the scope it shows.
    assert "leadership-level adjacent families" in SUMMARY_SYSTEM
    assert "preferably Y" in MATCHER_SYSTEM
    assert "underlying activities match" in MATCHER_SYSTEM


def test_matcher_receives_confirmed_languages_as_evidence() -> None:
    from src.jobs.screener import _constraints

    constraints = _constraints(
        SearchQuery(country="United Kingdom"),
        languages=[
            LanguageSkill(language="English", level="professional"),
            LanguageSkill(language="Spanish", level="professional"),
        ],
    )
    assert "Spanish (professional)" in constraints


def test_preferred_part_of_a_requirement_does_not_block_match() -> None:
    from src.jobs.screener import _separate_preferred_requirements

    assessed = _assessment(
        (3, 3, 3, 3, 3, 3),
        essential_unmet=[
            "Proven understanding of immuno assay and preferably Luminex workflows",
            "Preferably prior Luminex experience",
        ],
    )
    cleaned = _separate_preferred_requirements(assessed)
    assert cleaned.essential_unmet == ["Proven understanding of immuno assay"]
    assert any("Luminex" in gap for gap in cleaned.gaps)
    assert "founding or running a company" in MATCHER_SYSTEM
    record = ProfileSummary.model_fields["publications"]
    assert record.default is None  # profiles stored before the field still load


def test_family_checks_require_evidence_and_a_gap(master_cv: MasterCV) -> None:
    thin = SALES.model_copy(update={"evidence": ["nimbus-1"]})
    no_gap = LEAD.model_copy(update={"gap": " "})
    checked = check_families(_summary(CORE, thin, no_gap), master_cv).role_families
    assert checked[0].rejected is None
    assert checked[1].rejected and "2 piece(s) of CV evidence, found 1" in checked[1].rejected
    assert checked[2].rejected is None and checked[2].gap


def test_not_a_fit_never_rejects_families_by_title_words(master_cv: MasterCV) -> None:
    # Regression: a not_a_fit sentence naming "Clinical Scientist" rejected every family with
    # "Scientist" in a title, so only Head/Director roles were searched. Fit is the
    # job_matcher's call, per posting, against the posting's requirements.
    scientist = CORE.model_copy(update={"name": "Cell therapy", "titles": ["Principal Scientist"]})
    summary = _summary(
        scientist,
        SALES,
        not_a_fit=[
            "Clinical Scientist or clinical laboratory director roles requiring registration "
            "or clinical diagnostic leadership absent from the CV.",
            "Solutions Consultant - pre-sales, not engineering",
        ],
    )
    assert [f.rejected for f in check_families(summary, master_cv).role_families] == [None, None]


def test_stored_profiles_are_rechecked_unless_edited(master_cv: MasterCV, tmp_path: Any) -> None:
    memory = ProfileMemory(tmp_path / "m.json")
    stale = CORE.model_copy(update={"rejected": "listed as not a fit (Clinical Scientist)"})
    memory.put("cv", "any", _summary(stale))
    fixed = memory.recheck("cv:any", master_cv, None)
    assert fixed and fixed.role_families[0].rejected is None
    assert ProfileMemory(tmp_path / "m.json").get("cv", "any") == fixed  # saved
    memory.update("cv:any", _summary(LEAD.model_copy(update={"evidence": []})))  # user's edit
    kept = memory.recheck("cv:any", master_cv, None)
    assert kept and kept.role_families[0].evidence == []  # edits are the user's decision
    assert kept.role_families[0].rejected is None


def test_a_profile_without_a_searchable_core_family_falls_back_to_target_roles(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    searched: list[str] = []

    class Board:
        name = "reed"

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            searched.extend(query.titles)
            return []

    monkeypatch.setattr(search_service, "build_sources", lambda *a, **k: ([Board()], {}))
    only_up = _summary(LEAD, CORE.model_copy(update={"titles": []}))
    monkeypatch.setattr(ws, "structured", lambda *a, **k: FakeLLM({ProfileSummary: only_up}))
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(str(payload.get("message", "")))

    asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(), smart=True), emit))
    assert searched == ["Machine Learning Engineer"]  # target_roles, not only the level up
    assert any("No core role family" in e for e in events)


def test_requested_needs_an_intent_and_cvless_profiles_search_core_only(
    master_cv: MasterCV,
) -> None:
    # The model may mark a family requested; only the user's target areas make it so.
    unasked = check_families(_summary(BD), master_cv, SearchIntent()).role_families[0]
    assert unasked.requested is False and unasked.rejected is None
    cvless = check_families(_summary(CORE, LEAD), None).role_families
    assert (
        cvless[0].rejected is None
        and cvless[1].rejected == "no CV to show the experience transfers"
    )


def test_family_titles_are_not_silently_filtered_by_candidate_level(master_cv: MasterCV) -> None:
    far = SALES.model_copy(update={"titles": ["Chief Revenue Officer", "Solutions Consultant"]})
    fam = check_families(_summary(far), master_cv).role_families[0]
    assert fam.titles == ["Chief Revenue Officer", "Solutions Consultant"]
    assert fam.rejected is None


def test_up_to_six_adjacent_families_are_searched(master_cv: MasterCV) -> None:
    many = [SALES.model_copy(update={"name": f"Area {n}"}) for n in range(7)]
    checked = check_families(_summary(CORE, *many), master_cv).role_families
    assert [f.rejected is None for f in checked[1:]] == [True] * 6 + [False]
    without_gap = LEAD.model_copy(update={"gap": ""})
    repaired = check_families(_summary(CORE, without_gap), master_cv).role_families[1]
    assert repaired.rejected is None and repaired.gap


# ---------------------------------------------------------------- search planning


def test_family_terms_budget_tiers_and_widen() -> None:
    summary = _summary(CORE, LEAD, BD.model_copy(update={"evidence": ["a", "b"]}), SALES)
    plain = family_terms(summary, widen=False, limit=16)
    assert {f for _, f in plain} == {"ML engineering", "ML leadership", "Business development"}
    assert plain[0] == ("Machine Learning Engineer", "ML engineering")  # core first
    wide = family_terms(summary, widen=True, limit=16)
    assert ("Solutions Consultant", "Solutions consulting") in wide
    tight = family_terms(summary, widen=True, limit=4)
    assert len({f for _, f in tight}) == 4  # a limit never drops a whole family
    rejected = SALES.model_copy(update={"rejected": "unrealistic"})
    assert all(f != "Solutions consulting" for _, f in family_terms(_summary(rejected), True, 16))


def test_family_terms_mix_tiers_so_a_capped_source_reaches_each_one() -> None:
    core = CORE.model_copy(
        update={"titles": ["Machine Learning Engineer", "Recommender Engineer", "Ranking Engineer",
                           "Search Relevance Engineer", "Personalisation Engineer"]}
    )  # fmt: skip
    terms = family_terms(_summary(core, LEAD, BD, SALES), widen=True, limit=16)
    assert terms[0][1] == "ML engineering"  # core first on ties
    tiers = {"ML engineering": "core", "ML leadership": "progression"}
    first = {tiers.get(f, "adjacent") for _, f in terms[:5]}
    assert first == {"core", "progression", "adjacent"}  # LinkedIn stops after a few searches


def test_widen_adds_adjacent_terms_without_replacing_normal_searches() -> None:
    summary = _summary(CORE, LEAD, BD, SALES)
    normal = search_service.planned_family_terms(summary, False)
    wide = search_service.planned_family_terms(summary, True)
    assert wide[: len(normal)] == normal
    assert ("Solutions Consultant", "Solutions consulting") in wide


def test_nationwide_search_does_not_invent_a_commute_dealbreaker(master_cv: MasterCV) -> None:
    from src.jobs.screener import _correct_nationwide_location

    job = JobPosting(
        id="scotland",
        title="Senior Scientist",
        company="A",
        location="Scotland",
        description=LONG,
        work_arrangement="onsite",
    )
    assessment = _assessment(
        (3, 3, 3, 3, 3, 1),
        dealbreakers=["Onsite Edinburgh location: candidate will not relocate from UK base"],
    ).model_copy(
        update={"fit_summary": "Strong scientific match; onsite Edinburgh cannot be commuted to"}
    )
    corrected = _correct_nationwide_location(assessment, job, SearchQuery(country="United Kingdom"))
    verdict = finalize(corrected, 60)
    assert verdict.match
    assert verdict.fit_score > 65
    assert verdict.priority != "low"
    assert not verdict.dealbreakers
    assert "commuted" not in verdict.fit_summary
    assert "no city, commute radius or relocation limit" in search_service.screening_base(
        master_cv, SearchQuery(country="United Kingdom")
    )
    assert "home: Lisbon, Portugal" in search_service.screening_base(
        master_cv, SearchQuery(country="United Kingdom")
    )
    city_search = SearchQuery(country="United Kingdom", locations=["Oxford"])
    assert _correct_nationwide_location(assessment, job, city_search) == assessment
    foreign = job.model_copy(update={"location": "Santa Monica, California, United States"})
    assert (
        _correct_nationwide_location(assessment, foreign, SearchQuery(country="United Kingdom"))
        == assessment
    )
    with_other_blocker = assessment.model_copy(
        update={"dealbreakers": [*assessment.dealbreakers, "Required clearance is missing"]}
    )
    retained = _correct_nationwide_location(
        with_other_blocker, job, SearchQuery(country="United Kingdom")
    )
    assert retained.dealbreakers == ["Required clearance is missing"]
    from src.jobs.screener import correct_nationwide_report

    old = finalize(assessment, 60)
    report = MatchReport(
        profile=build_profile(master_cv),
        threshold=60,
        screened=True,
        matches=[],
        below_threshold=[MatchResult(job=job, verdict=old)],
    )
    correct_nationwide_report(report, SearchQuery(country="United Kingdom"))
    assert [r.job.id for r in report.matches] == ["scotland"]
    assert report.matches[0].verdict and report.matches[0].verdict.fit_score > 65
    assert old.dealbreakers  # stored assessment was not mutated


def test_generic_titles_match_and_attribute_to_their_family() -> None:
    assert shares_role_words(
        "Senior Business Development Manager, Oncology", ["Business Development Manager"]
    )
    assert not shares_role_words("Business Analyst", ["Business Development Manager"])
    assert family_of("Business Development Director", [CORE, BD]) == "Business development"
    assert family_of("Lead Machine Learning Engineer", [BD, CORE]) == "ML engineering"
    process = RoleFamily(
        name="Cell therapy process & analytical development (CMC)",
        tier="progression",
        titles=["Principal Scientist, Analytical Development"],
        evidence=["nimbus-2"],
        gap="No cGMP operations",
    )
    assert family_of("Associate Director, Process Development", [CORE, process]) == process.name
    rejected = process.model_copy(
        update={"gap": "", "rejected": "no gap stated: an honest transfer always has one"}
    )
    restored = restore_missing_family_gaps(_summary(CORE, rejected)).role_families[1]
    assert restored.rejected is None and restored.gap
    cell = RoleFamily(name="iPSC R&D", tier="core", titles=["Principal Scientist iPSC"],
                      domain_terms=["cell therapy"])  # fmt: skip
    ops = RoleFamily(name="R&D operations", tier="adjacent", titles=["Scientific Operations Lead"],
                     evidence=["nimbus-1", "lexa-1"], gap="no ops role")  # fmt: skip
    # A job noun ("scientist") says nothing about the field; the other words decide.
    assert family_of("Implementation Scientist", [cell, ops]) is None
    assert family_of("Principal Scientist", [cell, ops]) == "iPSC R&D"
    assert family_of("Senior Scientist, Cell Therapy Process", [cell, ops]) == "iPSC R&D"
    assert family_of("Head of Scientific Operations", [cell, ops]) == "R&D operations"


def test_search_plans_by_family_attributes_results_and_logs_yield(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    searched: list[str] = []
    jobs = [
        JobPosting(
            id="bd", title="Business Development Manager", company="A", work_arrangement="remote"
        ),
        JobPosting(
            id="ml", title="Machine Learning Engineer", company="B", work_arrangement="remote"
        ),
    ]

    class Board:
        name = "reed"

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            searched.extend(query.titles)
            return jobs

    monkeypatch.setattr(search_service, "build_sources", lambda *a, **k: ([Board()], {}))
    out = asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(), smart=True)))
    assert "Business Development Manager" in searched and "Solutions Consultant" not in searched
    assert out.families == {"ML engineering": "core", "Business development": "adjacent"}
    family = {r.job.id: r.family for r in out.report.all_results()}
    assert family == {"bd": "Business development", "ml": "ML engineering"}
    for _ in range(2):
        asyncio.run(run_search(ws, SearchRequest(query=SearchQuery(), smart=True)))
    yields = {y.family: y for y in history.family_yield(ws, "master")}
    assert yields["ML engineering"].landing is True and yields["ML engineering"].matches == 3
    assert yields["Business development"].landing is False  # 3 searches, no match


LONG = "Requirements: " + "solutions consulting with enterprise clients, " * 20


def _screen_areas(prompt: str) -> ScreeningBatch:
    """ML roles fit well; a solutions role is a sound adjacent stretch (57.5 points)."""
    out = []
    for job_id, title in re.findall(r'job_id="([^"]+)">\ntitle: ([^\n]+)', prompt):
        levels = (4, 4, 3, 4, 3, 3) if "Machine Learning" in title else (2, 2, 2, 2, 3, 4)
        out.append(_assessment(levels).model_copy(update={"job_id": job_id}))
    return ScreeningBatch(verdicts=out)


def test_widened_search_covers_adjacent_roles_with_their_own_threshold(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    searched: list[list[str]] = []
    jobs = [
        JobPosting(id="sc", title="Solutions Consultant", company="A", description=LONG,
                   work_arrangement="remote"),
        JobPosting(id="ml", title="Machine Learning Engineer", company="B", description=LONG,
                   work_arrangement="remote"),
    ]  # fmt: skip

    class Board:
        name = "reed"

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            searched.append(list(query.titles))
            return jobs

    monkeypatch.setattr(search_service, "build_sources", lambda *a, **k: ([Board()], {}))
    llm = FakeLLM({ProfileSummary: _summary(CORE, BD, SALES), ScreeningBatch: _screen_areas})
    monkeypatch.setattr(ws, "structured", lambda *a, **k: llm)

    def listed(**kw: Any) -> set[str]:
        req = SearchRequest(query=SearchQuery(), smart=True, widen=True, **kw)
        out = asyncio.run(run_search(ws, req))
        return {r.job.id for r in [*out.report.matches, *out.report.to_check]}

    assert listed() == {"ml"}  # 57 is below the search's threshold of 60
    assert "Solutions Consultant" in searched[-1]  # widen searched the adjacent family
    assert listed(area_thresholds={"adjacent": 50}) == {"ml", "sc"}
    assert listed(area_thresholds={"progression": 50}) == {"ml"}  # only its own area


def test_titles_given_win_over_widen_and_say_so(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    searched: list[str] = []

    class Board:
        name = "reed"

        def fetch(self, query: SearchQuery) -> list[JobPosting]:
            searched.extend(query.titles)
            return []

    monkeypatch.setattr(search_service, "build_sources", lambda *a, **k: ([Board()], {}))
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(str(payload.get("message", "")))

    req = SearchRequest(query=SearchQuery(titles=["Data Engineer"]), smart=True, widen=True)
    asyncio.run(run_search(ws, req, emit))
    assert searched == ["Data Engineer"]
    assert any("Titles were given" in e for e in events)
    assert search_service.widen_note(ws, req.model_copy(update={"widen": False})) is None


def test_candidate_level_comes_from_established_roles_not_a_founder_title() -> None:
    from src.cv.models import Experience
    from src.jobs.matcher import established_level
    from src.tools.search_tools import seniority_level

    today = date(2026, 10, 1)
    principal = Experience(id="p", company="A", title="Principal Scientist", start="2018-01",
                           end="2023-06")  # fmt: skip
    ceo = Experience(id="c", company="B", title="Co-Founder & CEO", start="2023-07")
    brief = Experience(id="d", company="C", title="Director", start="2017-01", end="2017-05")
    level = established_level([ceo, principal, brief], today)
    assert level == seniority_level("Principal Scientist")
    assert established_level([ceo], today) == seniority_level("CEO")  # nothing else to go on


def test_requested_pivot_does_not_imply_a_seniority_filter(master_cv: MasterCV) -> None:
    junior = JobPosting(
        id="vc", title="Investment Analyst Intern", company="Fund", work_arrangement="remote"
    )
    profile = build_profile(master_cv)
    assert scorer.hard_exclusions(profile, junior) == []
    pivot = profile.model_copy(update={"pivot_titles": ["Investment Analyst"]})
    assert scorer.hard_exclusions(pivot, junior) == []
    assert pivot_titles(_summary(BD, SALES)) == ["Business Development Manager"]


# ---------------------------------------------------------------- languages & eligibility


def _posting(description: str) -> JobPosting:
    return JobPosting(
        id="p",
        title="Machine Learning Engineer",
        company="Acme",
        description=description,
        work_arrangement="remote",
    )


def test_undeclared_essential_language_excludes_and_levels_flag(master_cv: MasterCV) -> None:
    profile = build_profile(master_cv)
    assert {s.language for s in profile.languages} == {"English", "Portuguese"}
    german = _posting("Requirements: fluent German is essential.")
    assert scorer.hard_exclusions(profile, german) == [
        'requires German, not among your languages: "Requirements: fluent German is essential."'
    ]
    assert (
        scorer.hard_exclusions(
            profile, _posting("German would be a plus; fluent German desirable.")
        )
        == []
    )
    weaker = profile.model_copy(
        update={"languages": [LanguageSkill(language="German", level="conversational")]}
    )
    assert scorer.hard_exclusions(weaker, german) == []
    assert scorer.eligibility_flags(weaker, german)[0].startswith("German (above your declared")


def test_one_of_several_required_languages_accepts_fluent_spanish(master_cv: MasterCV) -> None:
    profile = build_profile(master_cv).model_copy(
        update={
            "languages": [
                LanguageSkill(language="English", level="professional"),
                LanguageSkill(language="Spanish", level="professional"),
            ]
        }
    )
    job = _posting(
        "Fluent in English and at least one other European language "
        "(Italian, Spanish, German, or French)."
    )
    assert scorer.hard_exclusions(profile, job) == []
    assert scorer.eligibility_flags(profile, job) == []
    without_spanish = profile.model_copy(
        update={"languages": [LanguageSkill(language="English", level="professional")]}
    )
    reasons = scorer.hard_exclusions(without_spanish, job)
    assert len(reasons) == 1
    assert all(name in reasons[0] for name in ("Italian", "Spanish", "German", "French"))


def test_undeclared_languages_flag_instead_of_excluding(master_cv: MasterCV) -> None:
    profile = build_profile(master_cv).model_copy(update={"languages": []})
    german = _posting("Native German speaker required.")
    assert not any("German" in r for r in scorer.hard_exclusions(profile, german))
    assert scorer.eligibility_flags(profile, german)[0].startswith("German (not declared)")


def test_eligibility_is_flagged_unless_confirmed_in_the_intent(
    master_cv: MasterCV, settings: Settings
) -> None:
    job = _posting("We cannot offer visa sponsorship. SC clearance required.")
    plain = JobMatcher(settings=settings).match(master_cv, [job], threshold=0)
    flags = plain.matches[0].flags
    assert [f.split(":")[0] for f in flags] == ["Right to work", "Security clearance"]
    told = SearchIntent(eligibility=["Right to work in the UK"])
    confirmed = JobMatcher(settings=settings).match(master_cv, [job], threshold=0, intent=told)
    assert [f.split(":")[0] for f in confirmed.matches[0].flags] == ["Security clearance"]


# ---------------------------------------------------------------- job_matcher


def test_long_postings_keep_their_requirements_for_the_matcher() -> None:
    text = "About us. " + "Company story. " * 300 + "Essential criteria: PhD and GMP experience."
    assert "Essential criteria: PhD and GMP" in posting_excerpt(text, 3000)
    assert "Essential criteria: PhD and GMP" in job_digest(_posting(text))


def test_matcher_prompt_is_profile_driven_not_tied_to_one_industry() -> None:
    for term in ("pharma", "biotech", "CRO", "CDMO", "iPSC"):
        assert term not in MATCHER_SYSTEM
    assert "adjacent" in MATCHER_SYSTEM and "alignment" in MATCHER_SYSTEM


def test_alignment_steers_priority_and_rank_but_not_the_score() -> None:
    strong = (4, 4, 3, 4, 3, 3)  # 91
    aligned = finalize(_assessment(strong, alignment="aligned"), 60)
    against = finalize(_assessment(strong, alignment="against", alignment_note="sales"), 60)
    assert aligned.fit_score == against.fit_score == 91 and against.match
    assert (aligned.priority, against.priority) == ("apply_now", "consider")
    job = _posting("")
    ranks = [MatchResult(job=job, verdict=v).rank_key() for v in (aligned, against)]
    assert ranks == [91, 91 - ALIGNMENT_PENALTY]
    both = combine(_assessment(strong), _assessment(strong, alignment="against"))
    assert both.alignment == "against"


def test_matcher_sees_intent_and_only_realistic_families(ws: Workspace) -> None:
    seen: list[str] = []

    def screen(prompt: str) -> ScreeningBatch:
        seen.append(prompt)
        ids = re.findall(r'job_id="([^"]+)"', prompt)
        return ScreeningBatch(
            verdicts=[_assessment((4, 4, 3, 4, 3, 3)).model_copy(update={"job_id": i}) for i in ids]
        )

    from src.jobs.screener import screen_jobs

    summary = _summary(CORE, SALES.model_copy(update={"rejected": "unrealistic"}))
    wants = SearchIntent(avoid_work=["pure research"])
    asyncio.run(
        screen_jobs(
            summary,
            [_posting("x")],
            FakeLLM({ScreeningBatch: screen}),
            intent=intent_text(wants),
            confirmed_languages=[LanguageSkill(language="Spanish", level="professional")],
        )
    )
    assert "avoid_work: pure research" in seen[0]
    assert "ML engineering" in seen[0] and "Solutions consulting" not in seen[0]
    assert "Spanish (professional)" in seen[0]


# ---------------------------------------------------------------- career intent store


def test_profile_summary_is_built_by_the_profile_model(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles: list[str] = []
    llm = FakeLLM({ProfileSummary: _summary(CORE, BD, SALES)})
    monkeypatch.setattr(ws, "structured", lambda role="screening", *_: roles.append(role) or llm)
    asyncio.run(search_service.get_summary(ws, ws.master_cv, SearchQuery(), True))
    assert roles == ["profile"]


def test_intent_patch_reports_changes_and_flags_stale_profiles(ws: Workspace) -> None:
    asyncio.run(search_service.get_summary(ws, ws.master_cv, SearchQuery(), False))
    assert [p.intent_changed for p in list_profiles(ws, ws.master_cv)] == [False]
    areas = ["business development", "venture investment"]
    new, changes = intent.patch_intent(ws, {"target_areas": areas})
    assert new.target_areas == areas and new.updated_at
    assert changes == ["target_areas: + venture investment"]
    assert intent.patch_intent(ws, {"target_areas": areas})[1] == []
    assert [p.intent_changed for p in list_profiles(ws, ws.master_cv)] == [True]
    ws.active_cv_id = "other.pdf"  # intent is per CV
    assert intent.get_intent(ws).is_empty()


# ---------------------------------------------------------------- fixtures


def _screen_all(prompt: str) -> ScreeningBatch:
    """Fake job_matcher: ML engineering matches, everything else is weak."""
    out = []
    for job_id, title in re.findall(r'job_id="([^"]+)">\ntitle: ([^\n]+)', prompt):
        levels = (4, 4, 3, 4, 3, 3) if "Machine Learning" in title else (1, 2, 2, 1, 1, 1)
        out.append(_assessment(levels).model_copy(update={"job_id": job_id}))
    return ScreeningBatch(verdicts=out)


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    w = Workspace(settings)
    w.master_cv, w.active_cv_id = master_cv, "master"
    intent.save_intent(w, SearchIntent(target_areas=["business development"]))
    llm = FakeLLM({ProfileSummary: _summary(CORE, BD, SALES), ScreeningBatch: _screen_all})
    monkeypatch.setattr(w, "structured", lambda role="worker", *_: llm)
    monkeypatch.setattr(w, "llm_ready", lambda: True)
    return w
