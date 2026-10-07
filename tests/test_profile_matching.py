"""Profile construction and matching: dated CV text, role families (tiers, evidence checks,
query budget, attribution, yield), career intent (alignment), languages and eligibility."""

from __future__ import annotations

import asyncio
import re
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
    record = ProfileSummary.model_fields["publications"]
    assert record.default is None  # profiles stored before the field still load


def test_family_checks_require_evidence_and_a_gap(master_cv: MasterCV) -> None:
    thin = SALES.model_copy(update={"evidence": ["nimbus-1"]})
    no_gap = LEAD.model_copy(update={"gap": " "})
    checked = check_families(_summary(CORE, thin, no_gap), master_cv).role_families
    assert checked[0].rejected is None
    assert checked[1].rejected and "2 piece(s) of CV evidence, found 1" in checked[1].rejected
    assert checked[2].rejected and "no gap" in checked[2].rejected


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


def test_generic_titles_match_and_attribute_to_their_family() -> None:
    assert shares_role_words(
        "Senior Business Development Manager, Oncology", ["Business Development Manager"]
    )
    assert not shares_role_words("Business Analyst", ["Business Development Manager"])
    assert family_of("Business Development Director", [CORE, BD]) == "Business development"
    assert family_of("Lead Machine Learning Engineer", [BD, CORE]) == "ML engineering"
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
            summary, [_posting("x")], FakeLLM({ScreeningBatch: screen}), intent=intent_text(wants)
        )
    )
    assert "avoid_work: pure research" in seen[0]
    assert "ML engineering" in seen[0] and "Solutions consulting" not in seen[0]


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
