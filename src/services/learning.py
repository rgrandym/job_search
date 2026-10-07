"""Learn general preferences from your labels and notes, so every search matches better.

Models do not learn by themselves: the job_matcher judges each posting against the profile
summary. So your labels become *general* profile preferences, proposed by the quality model,
checked by code and accepted (or edited, or rejected) by you:

- requirement_gap   a capability area postings require that your CV does not show
                    ("hands-on statistical genetics") -> not_a_fit
- seniority_floor   roles below your level ("postdoc or fixed-term research posts") -> not_a_fit
- not_a_fit         another kind of role you reject                              -> not_a_fit
- target_role       a role type you would take now                            -> target_roles
- transferable_strength  leadership, team management, scientific business development
                    and the like, which carry into roles beyond your title -> transferable_strengths
- adjacent_family   a role family to search when widening, with CV evidence  -> role_families
                    (and its name added to the career intent's target areas)

Code decides (`_vet`): every proposal cites labelled jobs pointing its way (exclusions a
"no", inclusions a "yes"), names no employer (it must hold beyond one posting), a requirement
gap never names a skill the profile shows, nothing repeats an existing entry or a proposal
you rejected, and an adjacent family passes the same CV-evidence checks as any other.

Evidence is your labels with their notes, plus the reasons and notes on jobs you applied for
(read as "yes") or marked N/A (read as "no"). Each search first learns from evidence it has not
read yet (`learn_new`): proposals that pass the checks are accepted at once, marked `auto`, and
you can remove any of them (removed ones are never proposed again). "Suggest profile updates"
still proposes from all the evidence for you to review.

Accepted preferences are kept per CV in `data/learned_preferences.json` (personal) and applied
to whichever profile summary a search uses (`apply_learned`), so they survive profile rebuilds
and hold for every role family. Changing them changes the matcher's input, so remembered
verdicts are judged again.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.llm_provider import LLMProvider
from src.cv.models import MasterCV
from src.jobs.models import JobPosting, ProfileSummary, RoleFamily, SearchIntent, SearchQuery
from src.jobs.profile_memory import check_families, intent_text, profile_cv_text
from src.services import intent as intent_service
from src.services import labels, tracker
from src.services.workspace import Workspace

Kind = Literal[
    "requirement_gap",
    "seniority_floor",
    "not_a_fit",
    "target_role",
    "transferable_strength",
    "adjacent_family",
]
EXCLUDING: set[str] = {"requirement_gap", "seniority_floor", "not_a_fit"}
MAX_PROPOSALS = 8

SYSTEM = """You turn a job seeker's own calls on job postings into GENERAL preferences that \
make an automated job matcher better on postings it has not seen yet.

You get: the candidate's profile summary (what the matcher reads), their career intent, \
their CV with bracketed ids, preferences already accepted or rejected, and labelled postings: \
the candidate's call (yes = would apply, no = not a good match, maybe = torn), their own note, \
and what the matcher concluded (fit score, match, gaps, unmet essentials).

Read the notes carefully; they say WHY. Then propose at most 8 preferences, each one of:
- requirement_gap: a capability area that postings make a core requirement and the CV does \
not evidence, phrased as a role type to reject with the reason, e.g. "Roles whose core \
requirement is hands-on statistical genetics: not in my CV". Only from "no" notes that say a \
key requirement is missing; never for anything the CV or profile shows.
- seniority_floor: roles below the candidate's level, phrased by level and typical titles, e \
.g. "Postdoctoral, Research Scientist or fixed-term junior research posts: below my 20 years \
and leadership level". Only from notes saying a role is too junior or short.
- not_a_fit: another kind of role the candidate rejects, with the reason.
- target_role: a role type the candidate would take now that the profile misses, from "yes" \
labels.
- transferable_strength: experience that makes the candidate credible beyond their title \
(leadership, team management, business development or partnering in a scientific context, \
programme management, data/AI work), stated with what in the CV shows it.
- adjacent_family: a role family to search when the user widens to adjacent roles, where \
those transferable strengths matter most (e.g. scientific business development, R&D lab \
or team leadership, applied AI in life science). Give 2-4 titles as employers advertise them, \
1-3 domain terms, at least 2 CV ids as evidence, the main gap, and a one-line rationale.

Rules:
- General, not about one posting: never name an employer, and phrase each preference so it \
applies to many postings. Prefer patterns that 2 or more labels share; a single label is \
enough only when its note states the reason plainly.
- Cite the labelled job ids each preference comes from (label_ids). Exclusions must rest on \
"no" labels; inclusions on "yes" labels ("maybe" may support either).
- Do not repeat what the profile already says, or anything already accepted or rejected.
- Never invent experience: transferable strengths and families rest on the CV.
- rationale: one sentence on what in the labels and notes shows this."""


class DraftPreference(BaseModel):
    kind: Kind
    text: str = Field(description="The preference as it will appear in the profile")
    rationale: str = ""
    label_ids: list[str] = Field(default_factory=list)
    family: RoleFamily | None = Field(None, description="adjacent_family only")


class DraftPreferences(BaseModel):
    proposals: list[DraftPreference]


class LearnedPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Kind
    text: str
    proposed: str = Field("", description="The model's wording, when you reworded it")
    rationale: str = ""
    label_ids: list[str] = Field(default_factory=list)
    family: RoleFamily | None = None
    status: Literal["pending", "accepted", "rejected"] = "pending"
    auto: bool = Field(False, description="Accepted automatically from new labels or reasons")
    created_at: str
    decided_at: str | None = None


class LearningState(BaseModel):
    """What the UI shows: proposals to review, preferences in force, and why some proposals
    from the last suggestion were set aside."""

    pending: list[LearnedPreference]
    accepted: list[LearnedPreference]
    rejected: int
    set_aside: list[str] = Field(default_factory=list)


def _path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "learned_preferences.json"


def _owner(ws: Workspace) -> str:
    return ws.active_cv_id or intent_service.NO_CV


def _load_all(ws: Workspace) -> dict[str, list[LearnedPreference]]:
    try:
        raw = json.loads(_path(ws).read_text(encoding="utf-8"))
        return {k: [LearnedPreference.model_validate(p) for p in v] for k, v in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def load(ws: Workspace) -> list[LearnedPreference]:
    """The selected CV's preferences, every status."""
    return _load_all(ws).get(_owner(ws), [])


def _save(ws: Workspace, items: list[LearnedPreference]) -> None:
    data = _load_all(ws) | {_owner(ws): items}
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = {k: [p.model_dump(mode="json") for p in v] for k, v in data.items()}
    path.write_text(json.dumps(dumped, indent=1), encoding="utf-8")


def state(ws: Workspace, set_aside: list[str] | None = None) -> LearningState:
    items = load(ws)
    return LearningState(
        pending=[p for p in items if p.status == "pending"],
        accepted=[p for p in items if p.status == "accepted"],
        rejected=sum(p.status == "rejected" for p in items),
        set_aside=set_aside or [],
    )


# ---------------------------------------------------------------- applying


def apply_learned(
    summary: ProfileSummary,
    prefs: list[LearnedPreference],
    cv: MasterCV | None,
    intent: SearchIntent | None = None,
) -> ProfileSummary:
    """The summary with accepted preferences added (no duplicates). Idempotent. Added role
    families are checked like any other, against the CV and the career `intent`."""
    accepted = [p for p in prefs if p.status == "accepted"]
    if not accepted:
        return summary

    def add(items: list[str], kinds: set[str]) -> list[str]:
        seen = {i.casefold() for i in items}
        new = [p.text for p in accepted if p.kind in kinds and p.text.casefold() not in seen]
        return [*items, *dict.fromkeys(new)]

    names = {f.name.casefold() for f in summary.role_families}
    families = [
        p.family.model_copy(update={"tier": "adjacent", "requested": True, "rejected": None})
        for p in accepted
        if p.kind == "adjacent_family" and p.family and p.family.name.casefold() not in names
    ]
    merged = summary.model_copy(
        update={
            "not_a_fit": add(summary.not_a_fit, EXCLUDING),
            "target_roles": add(summary.target_roles, {"target_role"}),
            "transferable_strengths": add(
                summary.transferable_strengths, {"transferable_strength"}
            ),
            "role_families": [*summary.role_families, *families],
        }
    )
    return check_families(merged, cv, intent) if families and cv is not None else merged


def learned_for(ws: Workspace, summary: ProfileSummary, cv: MasterCV | None) -> ProfileSummary:
    """`apply_learned` with the selected CV's accepted preferences (what searches use)."""
    return apply_learned(summary, load(ws), cv, intent_service.get_intent(ws))


# ---------------------------------------------------------------- proposing


def _evidence(items: list[labels.LabelledJob]) -> str:
    """Labelled postings as the model reads them: call, note, and the matcher's latest view."""
    lines = []
    for x in items:
        v = list(x.verdicts.values())[-1] if x.verdicts else None
        seen = (
            f"matcher: fit {v.fit_score}, {'match' if v.match else 'not a match'}; "
            f"unmet essentials: {'; '.join(v.essential_unmet) or 'none'}; "
            f"gaps: {'; '.join(v.gaps) or 'none'}"
            if v
            else "matcher: not judged"
        )
        note = f' note: "{x.note}"' if x.note else ""
        lines.append(
            f'<label id="{x.job_id}" call="{x.label}">{x.job.title}{note} · {seen}</label>'
        )
    return "\n".join(lines)


def _vet(
    drafts: list[DraftPreference],
    items: dict[str, labels.LabelledJob],
    summary: ProfileSummary,
    known: list[LearnedPreference],
    cv: MasterCV | None,
) -> tuple[list[DraftPreference], list[str]]:
    """Code's checks; returns (kept, one line per proposal set aside, with the reason)."""
    taken = {t.casefold() for t in [*summary.not_a_fit, *summary.target_roles]}
    taken |= {t.casefold() for t in summary.transferable_strengths}
    taken |= {t.casefold() for p in known for t in (p.text, p.proposed) if t}
    skills = [s.skill.casefold() for s in summary.key_skills if len(s.skill) > 2]
    kept, aside = [], []
    for d in drafts[:MAX_PROPOSALS]:
        cited = [items[i] for i in d.label_ids if i in items]
        calls = {x.label for x in cited}
        companies = [x.job.company.casefold() for x in cited if len(x.job.company) > 2]
        problem = None
        if not cited:
            problem = "cites no labelled job"
        elif d.kind in EXCLUDING and "no" not in calls:
            problem = "an exclusion needs a job you said no to"
        elif d.kind not in EXCLUDING and "yes" not in calls:
            problem = "an addition needs a job you would apply for"
        elif any(c in d.text.casefold() for c in companies):
            problem = "names an employer: it would not hold beyond one posting"
        elif d.kind == "requirement_gap" and any(s in d.text.casefold() for s in skills):
            problem = "your profile shows this skill"
        elif d.text.casefold() in taken:
            problem = "already in your profile or already decided"
        elif d.kind == "adjacent_family":
            problem = _family_problem(d, summary, cv)
        if problem:
            aside.append(f"{d.text} — {problem}")
        else:
            kept.append(d.model_copy(update={"label_ids": [x.job_id for x in cited]}))
            taken.add(d.text.casefold())
    return kept, aside


def _family_problem(d: DraftPreference, summary: ProfileSummary, cv: MasterCV | None) -> str | None:
    if d.family is None:
        return "no role family given"
    if d.family.name.casefold() in {f.name.casefold() for f in summary.role_families}:
        return "that role family is already in your profile"
    fam = d.family.model_copy(update={"tier": "adjacent", "requested": False, "rejected": None})
    checked = check_families(summary.model_copy(update={"role_families": [fam]}), cv, None)
    return checked.role_families[0].rejected


def _tracker_evidence(ws: Workspace, labelled: set[str]) -> list[labels.LabelledJob]:
    """Applied (a "yes") and N/A (a "no") jobs whose reason or note says why, as labels.
    Jobs you also labelled are left to their label."""
    out = []
    for entry in tracker.load(ws).values():
        why = "; ".join(t for t in (entry.reason, entry.note) if t.strip())
        if entry.status not in ("applied", "na") or not why:
            continue
        job = entry.application_result.job if entry.application_result else JobPosting(
            id=entry.job_id or entry.id, title=entry.title, company=entry.company,
            location=entry.location, url=entry.url, source=entry.source,
        )  # fmt: skip
        if job.id in labelled or entry.id in labelled:
            continue
        out.append(
            labels.LabelledJob(
                job_id=entry.id,
                label="yes" if entry.status == "applied" else "no",
                note=f"{'applied' if entry.status == 'applied' else 'ruled out (N/A)'}: {why}",
                labelled_at=entry.applied_at or entry.last_seen,
                job=job,
                query=SearchQuery(),
                threshold=labels.SCREENING_DEFAULT,
            )
        )
    return out


def _evidence_items(ws: Workspace) -> tuple[list[labels.LabelledJob], labels.LabelStore]:
    """Labels with a note or where the matcher disagreed, and applied / N/A jobs with a reason."""
    store = labels.load(ws)
    items = [
        x
        for x in store.labels
        if x.note or any(v.match != (x.label == "yes") for v in x.verdicts.values())
    ]
    labelled = {
        i for x in store.labels for i in (*x.ids, tracker.role_id(x.job.title, x.job.company))
    }
    return [*items, *_tracker_evidence(ws, labelled)], store


def _base_summary(ws: Workspace, store: labels.LabelStore) -> ProfileSummary:
    """The profile the evidence is read against: the newest labelled search's, else the
    selected CV's most recent stored profile."""
    newest = next((x for x in store.labels if x.profile_key in store.profiles), None)
    if newest is not None and newest.profile_key is not None:
        return store.profiles[newest.profile_key]
    records = ws.memory.records(_owner(ws)) if ws.active_cv_id else []
    if not records:
        raise ValueError("No profile summary yet: run a search first")
    return records[-1].summary


def _propose(
    ws: Workspace, llm: LLMProvider, items: list[labels.LabelledJob], store: labels.LabelStore
) -> tuple[list[DraftPreference], list[str], list[LearnedPreference]]:
    """The quality model's proposals from `items`, vetted by code: (kept, set aside, known)."""
    cv = ws.master_cv
    known = load(ws)
    summary = apply_learned(_base_summary(ws, store), known, cv, intent_service.get_intent(ws))
    decided = "\n".join(f"- [{p.status}] {p.kind}: {p.text}" for p in known) or "none"
    prompt = (
        f"<profile_summary>\n{summary.model_dump_json()}\n</profile_summary>\n"
        f"{intent_text(intent_service.get_intent(ws))}"
        f"<cv>\n{profile_cv_text(cv) if cv else 'no CV'}\n</cv>\n"
        f"<already_decided>\n{decided}\n</already_decided>\n"
        f"<labels>\n{_evidence(items)}\n</labels>"
    )
    out = llm.generate(system=SYSTEM, prompt=prompt, output_model=DraftPreferences)
    kept, aside = _vet(out.proposals, {x.job_id: x for x in items}, summary, known, cv)
    return kept, aside, known


def suggest(ws: Workspace, llm: LLMProvider) -> LearningState:
    """Ask the quality model for preferences from your labels and reasons; keep what passes
    the checks as proposals to review."""
    items, store = _evidence_items(ws)
    if not items:
        raise ValueError("Label some jobs (ideally with a note on why) before asking for this")
    kept, aside, known = _propose(ws, llm, items, store)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    new = [
        LearnedPreference(id=uuid.uuid4().hex[:10], created_at=now, **d.model_dump()) for d in kept
    ]
    _save(ws, [*known, *new])
    return state(ws, aside)


def _fingerprint(x: labels.LabelledJob) -> str:
    return hashlib.sha1(f"{x.job_id}|{x.label}|{x.note}".encode()).hexdigest()[:16]


def _read_path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "learned_from.json"


def _read(ws: Workspace) -> dict[str, list[str]]:
    try:
        raw = json.loads(_read_path(ws).read_text(encoding="utf-8"))
        return {str(k): [str(f) for f in v] for k, v in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def learn_new(ws: Workspace, llm: LLMProvider) -> list[LearnedPreference] | None:
    """Learn from labels and reasons not read yet (a note or reason says why): proposals that
    pass the checks go straight into the profile. None when there was nothing new to read."""
    items, store = _evidence_items(ws)
    seen = set(_read(ws).get(_owner(ws), []))
    fresh = [x for x in items if x.note.strip() and _fingerprint(x) not in seen]
    if not fresh:
        return None
    kept, _, known = _propose(ws, llm, items, store)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    new = [
        LearnedPreference(
            id=uuid.uuid4().hex[:10], created_at=now, decided_at=now, status="accepted",
            auto=True, **d.model_dump(),
        )
        for d in kept
    ]  # fmt: skip
    for pref in new:
        _on_accept(ws, pref)
    _save(ws, [*known, *new])
    read = _read(ws) | {_owner(ws): sorted(seen | {_fingerprint(x) for x in items if x.note})}
    _read_path(ws).write_text(json.dumps(read, indent=1), encoding="utf-8")
    return new


# ---------------------------------------------------------------- your decisions


def decide(ws: Workspace, pref_id: str, accept: bool, text: str | None = None) -> LearningState:
    """Accept (optionally reworded) or reject a proposal. Accepting an adjacent family also
    adds it to the career intent's target areas, so widened searches include it."""
    items = load(ws)
    pref = next((p for p in items if p.id == pref_id and p.status == "pending"), None)
    if pref is None:
        raise ValueError("That proposal is no longer waiting for a decision")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    pref.status, pref.decided_at = ("accepted" if accept else "rejected"), now
    if accept and text and text.strip() and text.strip() != pref.text:
        pref.proposed, pref.text = pref.text, text.strip()
    if accept:
        _on_accept(ws, pref)
    _save(ws, items)
    return state(ws)


def _on_accept(ws: Workspace, pref: LearnedPreference) -> None:
    """An accepted adjacent family joins the career intent's target areas, so widened searches
    include it."""
    if pref.kind == "adjacent_family" and pref.family:
        areas = intent_service.get_intent(ws).target_areas
        if pref.family.name not in areas:
            intent_service.patch_intent(ws, {"target_areas": [*areas, pref.family.name]})


def remove(ws: Workspace, pref_id: str) -> LearningState:
    """Withdraw an accepted preference (it is kept as rejected, so it is not proposed again)."""
    items = load(ws)
    pref = next((p for p in items if p.id == pref_id and p.status == "accepted"), None)
    if pref is None:
        raise ValueError("That preference is not in force")
    pref.status = "rejected"
    pref.decided_at = datetime.now(UTC).isoformat(timespec="seconds")
    _save(ws, items)
    return state(ws)
