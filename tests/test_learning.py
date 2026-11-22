"""Learning general preferences from your labels: proposals, code's checks, your decisions, and
their effect on the profile searches and model comparisons use. The LLM is faked."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.core.config import Settings
from src.cv.models import MasterCV
from src.jobs.models import (
    JobPosting,
    MatchResult,
    RoleFamily,
    SearchQuery,
    SkillEvidence,
)
from src.jobs.screener import ScreeningBatch
from src.services import intent, labels, learning, model_compare, tracker
from src.services.learning import DraftPreference, DraftPreferences
from src.services.workspace import Workspace
from tests.conftest import FakeLLM
from tests.test_labels import LOW, STRONG, SUMMARY, _judged, _search

FAMILY = RoleFamily(
    name="Scientific business development",
    tier="adjacent",
    titles=["Business Development Manager"],
    domain_terms=["life science partnering"],
    evidence=["nimbus-1", "lexa-1"],
    gap="no quota-carrying sales role",
    rationale="Leads partner projects and teams",
)


@pytest.fixture
def ws(settings: Settings, master_cv: MasterCV) -> Workspace:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    return ws


def _at(job_id: str, levels: tuple[int, ...], title: str, company: str) -> MatchResult:
    r = _judged(job_id, levels, title)
    return r.model_copy(update={"job": r.job.model_copy(update={"company": company})})


def _labelled(ws: Workspace, master_cv: MasterCV) -> None:
    """A search with a good adjacent role you would apply for and two posts too junior."""
    _search(
        ws,
        master_cv,
        [
            _at("y1", LOW, "Head of Partnerships", "Acme Bio"),
            _at("n1", STRONG, "Postdoctoral Researcher", "Acme Bio"),
            _at("n2", STRONG, "Research Scientist (fixed term)", "Uni Lab"),
        ],
    )
    for job_id, call, note in [
        ("y1", "yes", "business development in a scientific context suits my leadership"),
        ("n1", "no", "too junior"),
        ("n2", "no", "too junior and short"),
    ]:
        job = next(r.job for r in ws.last_report.all_results() if r.job.id == job_id)  # type: ignore[union-attr]
        tracker.set_status(ws, job, "open", note)
        labels.set_label(ws, job_id, call)  # type: ignore[arg-type]


def test_suggestions_are_checked_then_decided_and_applied(
    ws: Workspace, master_cv: MasterCV
) -> None:
    _labelled(ws, master_cv)
    drafts = [
        DraftPreference(
            kind="seniority_floor",
            text="Postdoctoral or fixed-term junior research posts: below my level",
            label_ids=["n1", "n2"],
        ),
        DraftPreference(kind="not_a_fit", text="Roles at Acme Bio", label_ids=["n1"]),
        DraftPreference(kind="target_role", text="Research Scientist", label_ids=["n2"]),
        DraftPreference(
            kind="adjacent_family", text="Scientific BD", label_ids=["y1"], family=FAMILY
        ),
        DraftPreference(
            kind="adjacent_family",
            text="Clinical operations",
            label_ids=["y1"],
            family=FAMILY.model_copy(update={"name": "Clinical ops", "evidence": ["nope"]}),
        ),
        DraftPreference(kind="not_a_fit", text="Sales roles", label_ids=["ghost"]),
    ]
    fake = FakeLLM({DraftPreferences: DraftPreferences(proposals=drafts)})
    st = learning.suggest(ws, fake)  # type: ignore[arg-type]

    prompt = fake.calls[0][0]
    assert "too junior and short" in prompt and "[nimbus-1]" in prompt  # notes and CV ids
    assert [p.text for p in st.pending] == [drafts[0].text, "Scientific BD"]
    reasons = " | ".join(st.set_aside)
    assert "names an employer" in reasons and "needs a job you would apply for" in reasons
    assert "CV evidence" in reasons and "cites no labelled job" in reasons

    floor, family = st.pending
    learning.decide(ws, floor.id, True, "Postdoc and fixed-term junior research posts: too junior")
    learning.decide(ws, family.id, True)
    assert FAMILY.name in intent.get_intent(ws).target_areas  # widened searches include it

    used = learning.learned_for(ws, SUMMARY, master_cv)
    assert "Postdoc and fixed-term junior research posts: too junior" in used.not_a_fit
    [fam] = [f for f in used.role_families if f.name == FAMILY.name]
    assert fam.tier == "adjacent" and fam.requested and fam.rejected is None
    assert learning.learned_for(ws, used, master_cv) == used  # idempotent

    learning.remove(ws, floor.id)
    assert [p.kind for p in learning.state(ws).accepted] == ["adjacent_family"]
    again = learning.suggest(
        ws, FakeLLM({DraftPreferences: DraftPreferences(proposals=drafts[:1])})
    )  # type: ignore[arg-type]
    assert again.pending == [] and "already decided" in again.set_aside[0]  # never re-proposed


def test_a_requirement_gap_never_names_a_skill_you_have(ws: Workspace, master_cv: MasterCV) -> None:
    _labelled(ws, master_cv)
    items = {x.job_id: x for x in labels.load(ws).labels}
    summary = SUMMARY.model_copy(
        update={"key_skills": [SkillEvidence(skill="Python", level="expert", evidence="x")]}
    )
    draft = DraftPreference(
        kind="requirement_gap",
        text="Roles whose core requirement is Python engineering",
        label_ids=["n1"],
    )
    kept, aside = learning._vet([draft], items, summary, [], master_cv)
    assert kept == [] and "profile shows this skill" in aside[0]


def test_learning_routes(
    ws: Workspace, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    client = TestClient(webapp.app)
    assert client.get("/api/learning").json()["pending"] == []
    assert client.post("/api/learning/nope", json={"accept": True}).status_code == 404


def test_comparison_can_apply_your_learned_preferences() -> None:
    prompts: list[str] = []

    def answer(prompt: str) -> ScreeningBatch:
        prompts.append(prompt)
        return ScreeningBatch(verdicts=[])

    item = labels.LabelledJob(
        job_id="j1",
        label="no",
        labelled_at="2026-10-03T00:00:00+00:00",
        job=JobPosting(id="j1", title="Postdoc", company="Co"),
        query=SearchQuery(),
        threshold=60,
        profile_key="p",
    )
    rule = "Postdoctoral posts: below my level"
    fake = FakeLLM({ScreeningBatch: answer})
    asyncio.run(
        model_compare.compare_labels(
            [item],
            {"p": SUMMARY},
            lambda role: fake,
            "setup",
            adjust=lambda s: s.model_copy(update={"not_a_fit": [*s.not_a_fit, rule]}),
        )
    )
    assert prompts and rule in prompts[0]


def test_any_label_sets_the_job_aside_in_later_searches(ws: Workspace, master_cv: MasterCV) -> None:
    from src.services import search_service

    _labelled(ws, master_cv)
    again = [
        JobPosting(id="n1-again", title="Postdoctoral Researcher", company="Acme Bio"),
        JobPosting(id="y1-again", title="Head of Partnerships", company="Acme Bio Ltd"),
        JobPosting(id="fresh", title="Head of Partnerships", company="Beta"),
    ]
    keep, applied, dismissed = asyncio.run(search_service._set_aside(ws, again, _quiet))
    assert [j.id for j in keep] == ["fresh"] and applied == []
    assert dismissed[0].exclusion_reasons == ["you labelled it no: too junior"]
    assert dismissed[1].exclusion_reasons[0].startswith("you labelled it yes: business")


async def _quiet(kind: str, payload: dict[str, object]) -> None:
    return None


def test_new_reasons_are_read_once_and_an_exclusion_waits_for_the_user(
    ws: Workspace, master_cv: MasterCV
) -> None:
    _labelled(ws, master_cv)
    ruled_out = JobPosting(id="t1", title="Clinical Research Associate", company="Gamma")
    tracker.set_status(ws, ruled_out, "na", reason="site monitoring travel, not my field")
    entry = tracker.find(tracker.load(ws), ruled_out)
    assert entry is not None
    floor = DraftPreference(
        kind="not_a_fit",
        text="Clinical monitoring roles with heavy site travel",
        label_ids=[entry.id],
    )
    fake = FakeLLM({DraftPreferences: DraftPreferences(proposals=[floor])})

    learned = learning.learn_new(ws, fake)  # type: ignore[arg-type]
    assert learned is not None and [p.text for p in learned] == [floor.text]
    # An N/A reason proposes an exclusion, and an exclusion is the user's call: it is read
    # from the reason once, then waits (user's decision 2026-11-22).
    assert learned[0].status == "pending" and not learned[0].auto
    assert "ruled out (N/A): site monitoring travel" in fake.calls[0][0]
    summary = learning.learned_for(ws, SUMMARY, master_cv)
    assert floor.text not in summary.not_a_fit  # not in force until accepted
    learning.decide(ws, learned[0].id, True)
    assert floor.text in learning.learned_for(ws, SUMMARY, master_cv).not_a_fit

    assert learning.learn_new(ws, fake) is None  # nothing new: no model call
    assert len(fake.calls) == 1
    tracker.set_status(ws, ruled_out, "na", reason="site monitoring travel; no lab work")
    assert learning.learn_new(ws, fake) is not None  # a changed reason is read again


def test_a_job_can_be_deleted_from_the_results_and_its_search(
    ws: Workspace, master_cv: MasterCV
) -> None:
    from src.services import history, search_service

    _labelled(ws, master_cv)
    entry_id = history.list_history(ws)[0].id
    assert search_service.remove_result(ws, "n2", entry_id)
    assert "n2" not in {r.job.id for r in ws.last_report.all_results()}  # type: ignore[union-attr]
    _, outcome = history.open_entry(ws, entry_id)
    assert "n2" not in {r.job.id for r in outcome.report.all_results()}
    assert not search_service.remove_result(ws, "n2", entry_id)


def test_a_requirement_gap_is_a_gap_not_a_rejected_role(ws: Workspace, master_cv: MasterCV) -> None:
    """User's decision 2026-11-22: "not evidenced in my CV" states a gap for the matcher; it
    never joins `not_a_fit`, where it became a dealbreaker and dropped roles the user had also
    accepted as targets. A proposal still has to cite a job they said no to."""
    _labelled(ws, master_cv)
    items = {x.job_id: x for x in labels.load(ws).labels}
    gap = DraftPreference(
        kind="requirement_gap",
        text="Roles whose core requirement is hands-on statistical genetics",
        label_ids=["n1"],
    )
    kept, aside = learning._vet([gap], items, SUMMARY, [], master_cv)
    assert kept == [gap] and aside == []  # learned from a "no", as before

    pref = learning.LearnedPreference(
        id="g1",
        kind="requirement_gap",
        text=gap.text,
        status="accepted",
        label_ids=["n1"],
        created_at="2026-11-22T00:00:00",
    )
    used = learning.apply_learned(SUMMARY, [pref], master_cv)
    assert gap.text in used.evidence_gaps  # stated as a gap
    assert gap.text not in used.not_a_fit  # never a reason to reject the role
    assert learning.apply_learned(used, [pref], master_cv) == used  # idempotent


def test_a_search_applies_widening_preferences_but_holds_exclusions(
    ws: Workspace, master_cv: MasterCV
) -> None:
    """User's decision 2026-11-22: `learn_new` applies what widens the search at once and
    leaves what narrows it pending, because a wrong exclusion hides jobs from every later
    search while a wrong inclusion costs one job to skip."""
    _labelled(ws, master_cv)
    drafts = [
        DraftPreference(
            kind="target_role", text="Head of Computational Biology roles", label_ids=["y1"]
        ),
        DraftPreference(
            kind="not_a_fit", text="Roles centred on plant biotechnology", label_ids=["n1"]
        ),
    ]
    llm = FakeLLM({DraftPreferences: DraftPreferences(proposals=drafts)})
    new = learning.learn_new(ws, llm)  # type: ignore[arg-type]
    assert new is not None
    by_kind = {p.kind: p for p in new}
    assert by_kind["target_role"].status == "accepted" and by_kind["target_role"].auto
    assert by_kind["not_a_fit"].status == "pending"  # the user decides an exclusion
    assert by_kind["not_a_fit"].auto is False and by_kind["not_a_fit"].decided_at is None

    st = learning.state(ws)
    assert [p.kind for p in st.pending] == ["not_a_fit"]
    assert (
        "Roles centred on plant biotechnology"
        not in learning.learned_for(ws, SUMMARY, master_cv).not_a_fit
    )  # not in force until accepted
