"""Multi-stage candidate <-> job matching.

    Stage 1  Hard exclusions     scorer.hard_exclusions (certs, arrangement, location, seniority)
    Stage 2  Semantic retrieval  embed profile + jobs, keep top-k by cosine similarity
    Stage 3  Multi-metric score  scorer.score -> 0-100 with per-metric breakdown
    Stage 4  Threshold + rank    keep total >= threshold, sort descending

Every job ends up in exactly one bucket of the `MatchReport`, with the reason.
"""

from __future__ import annotations

from datetime import date

from src.core.config import Settings, get_settings
from src.core.llm_provider import Embedder, cosine, get_embedder
from src.cv.models import Experience, MasterCV, TailoredCV
from src.cv.tailor import cv_to_text
from src.jobs import scorer
from src.jobs.models import (
    LANGUAGE_LEVELS,
    CandidateProfile,
    JobPosting,
    LanguageSkill,
    MatchReport,
    MatchResult,
    SearchIntent,
    SearchQuery,
)
from src.tools.search_tools import (
    DEFAULT_LEVEL,
    normalize_skills,
    parse_language,
    seniority_level,
    shares_role_words,
)

_LEVEL_NAMES = {v: k for k, v in LANGUAGE_LEVELS.items()}


def years_of_experience(experience: list[Experience], today: date | None = None) -> float:
    """Total professional years, merging overlapping roles so they are not double-counted."""
    today = today or date.today()

    def to_month(ym: str | None) -> int:
        y, m = (today.year, today.month) if ym is None else map(int, ym.split("-"))
        return y * 12 + m

    spans = sorted((to_month(e.start), to_month(e.end)) for e in experience)
    months, cur_start, cur_end = 0, None, None
    for s, e in spans:
        if cur_end is None or s > cur_end + 1:
            if cur_end is not None and cur_start is not None:
                months += cur_end - cur_start + 1
            cur_start, cur_end = s, e
        else:
            cur_end = max(cur_end, e)
    if cur_end is not None and cur_start is not None:
        months += cur_end - cur_start + 1
    return round(months / 12, 1)


def build_profile(
    cv: MasterCV | TailoredCV, today: date | None = None, query: SearchQuery | None = None
) -> CandidateProfile:
    """Derive a matching profile from a Master or Tailored CV.

    Search filters in `query` (titles, locations, arrangements, salary, keywords) override
    the CV's own preferences: what the user asks for now beats what the CV says.
    """
    master = cv.cv if isinstance(cv, TailoredCV) else cv
    roles = sorted(master.experience, key=lambda e: e.start, reverse=True)
    titles = [e.title for e in roles]
    prefs = master.preferences
    level = seniority_level(titles[0]) if titles else 0
    profile = CandidateProfile(
        name=master.basics.name,
        titles=titles,
        target_titles=prefs.target_titles,
        skills=sorted(normalize_skills(master.all_skills())),
        years_experience=years_of_experience(master.experience, today),
        seniority_level=level,
        locations=prefs.locations or ([master.basics.location] if master.basics.location else []),
        work_arrangements=prefs.work_arrangements,
        willing_to_relocate=prefs.willing_to_relocate,
        certifications=[c.name for c in master.certifications],
        salary_min=prefs.min_salary,
        text=cv_to_text(master),
        languages=cv_languages(master.languages),
    )
    return apply_query(profile, query) if query else profile


def cv_languages(entries: list[str]) -> list[LanguageSkill]:
    """The CV's free-text languages ("Spanish (C1)") as structured skills; unknown names skip."""
    out: list[LanguageSkill] = []
    for entry in entries:
        parsed = parse_language(entry)
        if parsed is not None:
            name, level = parsed
            out.append(LanguageSkill(language=name.title(), level=_LEVEL_NAMES[level]))
    return out


def apply_intent(
    profile: CandidateProfile, intent: SearchIntent | None, pivot_titles: list[str]
) -> CandidateProfile:
    """Overlay the user's confirmed languages and eligibility, and the titles of areas they
    chose to move into (career intent), onto a profile."""
    update: dict[str, object] = {"pivot_titles": pivot_titles}
    if intent is not None:
        if intent.languages:
            update["languages"] = intent.languages
        update["eligibility"] = intent.eligibility
    return profile.model_copy(update=update)


def apply_query(profile: CandidateProfile, query: SearchQuery) -> CandidateProfile:
    """Overlay search filters onto a profile."""
    update: dict[str, object] = {}
    if query.titles:
        update["target_titles"] = query.titles
    if query.locations or query.country:
        update["locations"] = query.place_names()
    if query.work_arrangements:
        update["work_arrangements"] = query.work_arrangements
    if query.salary_min:
        update["salary_min"] = float(query.salary_min)
    update["seniority_min"] = query.seniority_min
    update["seniority_max"] = query.seniority_max
    if query.keywords or query.titles:
        update["text"] = "\n".join([profile.text, *query.titles, *query.keywords])
    return profile.model_copy(update=update)


def profile_from_query(query: SearchQuery) -> CandidateProfile:
    """A CV-less profile for searches driven only by the filter form."""
    return CandidateProfile(
        name="Search filters",
        titles=[],
        target_titles=query.titles,
        skills=sorted(normalize_skills(query.keywords)),
        years_experience=0,
        seniority_level=seniority_level(query.titles[0]) if query.titles else DEFAULT_LEVEL,
        locations=query.place_names(),
        work_arrangements=query.work_arrangements,
        salary_min=float(query.salary_min) if query.salary_min else None,
        seniority_min=query.seniority_min,
        seniority_max=query.seniority_max,
        cv_based=False,
        text="\n".join([*query.titles, *query.keywords]),
    )


def _exclude(
    profile: CandidateProfile, jobs: list[JobPosting]
) -> tuple[list[MatchResult], list[JobPosting]]:
    """Split jobs into (excluded with reasons, eligible)."""
    excluded: list[MatchResult] = []
    eligible: list[JobPosting] = []
    for job in jobs:
        reasons = scorer.hard_exclusions(profile, job)
        if reasons:
            excluded.append(MatchResult(job=job, excluded=True, exclusion_reasons=reasons))
        else:
            eligible.append(job)
    return excluded, eligible


class JobMatcher:
    """Runs the four-stage pipeline for one candidate against many jobs."""

    def __init__(self, embedder: Embedder | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or get_embedder()

    def match(
        self,
        cv: MasterCV | TailoredCV | None,
        jobs: list[JobPosting],
        threshold: float | None = None,
        query: SearchQuery | None = None,
        intent: SearchIntent | None = None,
        pivot_titles: list[str] | None = None,
    ) -> MatchReport:
        """Score `jobs` for `cv` (or, with no CV, for the `query` filters alone). `intent` adds
        confirmed languages and eligibility; `pivot_titles` are areas the user chose to enter."""
        threshold = self.settings.score_threshold if threshold is None else threshold
        if cv is None:
            profile = profile_from_query(query or SearchQuery())
        else:
            profile = build_profile(cv, query=query)
        profile = apply_intent(profile, intent, pivot_titles or [])

        excluded, eligible = _exclude(profile, jobs)  # Stage 1: hard exclusions

        # Stage 2: semantic retrieval. Titles sharing a role-specific word with the target roles
        # rank first, so an optional cap never cuts a scientist role for an unrelated one.
        sims = self._similarities(profile, eligible)
        targets = profile.target_titles
        ranked = sorted(
            zip(eligible, sims, strict=True),
            key=lambda p: (not targets or shares_role_words(p[0].title, targets), p[1]),
            reverse=True,
        )
        cut = self.settings.retrieval_top_k or len(ranked)  # scoring is local: no cap by default
        retrieved = ranked[:cut]
        not_retrieved = [j.id for j, _ in ranked[cut:]]

        # Stage 3 + 4: score, threshold, rank
        passed: list[MatchResult] = []
        below: list[MatchResult] = []
        for job, sim in retrieved:
            breakdown = scorer.score(profile, job, sim, self.settings.weights)
            ok = breakdown.total >= threshold
            result = MatchResult(
                job=job,
                similarity=round(sim, 4),
                score=breakdown,
                passed=ok,
                flags=scorer.eligibility_flags(profile, job),
            )
            (passed if ok else below).append(result)

        def by_total(r: MatchResult) -> float:
            return r.score.total if r.score else 0.0

        return MatchReport(
            profile=profile,
            threshold=threshold,
            matches=sorted(passed, key=by_total, reverse=True),
            below_threshold=sorted(below, key=by_total, reverse=True),
            excluded=excluded,
            not_retrieved=not_retrieved,
        )

    def _similarities(self, profile: CandidateProfile, jobs: list[JobPosting]) -> list[float]:
        if not jobs:
            return []
        vectors = self.embedder.embed([profile.text] + [j.to_text() for j in jobs])
        return [cosine(vectors[0], v) for v in vectors[1:]]
