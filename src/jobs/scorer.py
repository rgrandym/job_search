"""Multi-metric candidate scoring (0-100) and hard-exclusion rules.

Weights (default, configurable via `Settings.weights`):
    title 25% · skills 35% · experience 20% · location 10% · semantic 10%

Every sub-score is in [0, 1] and deterministic given the same inputs. The scoring
matrix is documented in `.agent/skills/job_search/SKILL.md`. Keep the two in sync.

Filters-only profiles (`CandidateProfile.cv_based == False`, i.e. a search without a CV)
have no skills or experience to compare, so those two metrics are dropped and the
remaining weights are renormalised.
"""

from __future__ import annotations

import re
from datetime import date

from src.core.config import ScoringWeights, get_settings
from src.jobs.models import LANGUAGE_LEVELS, CandidateProfile, JobPosting, ScoreBreakdown
from src.tools.search_tools import (
    confirms,
    eligibility_requirements,
    extract_skills,
    jaccard,
    language_requirements,
    mentions,
    normalize_skill,
    parse_language,
    seniority_level,
    seniority_name,
    title_core,
)

# Seniority distance affects pre-filter ordering, never hard exclusion by default.
MAX_SENIORITY_GAP = 3
# Raw cosine similarities are mapped linearly from [FLOOR, CEIL] onto [0, 1].
SEMANTIC_FLOOR = 0.05
SEMANTIC_CEIL = 0.50
# Share of the skills sub-score given to required (vs preferred) skills.
REQUIRED_SKILL_SHARE = 0.75
# Location sub-scores. UNVERIFIED: a town we cannot place (no country, no shared name), so
# the distance is unknown; it lowers the score but never excludes (the job_matcher judges it).
LOC_SAME_CITY, LOC_SAME_COUNTRY, LOC_UNKNOWN, LOC_RELOCATE = 1.0, 0.4, 0.7, 0.6
LOC_UNVERIFIED = 0.3

_LOC_NOISE = {
    "greater",
    "city",
    "of",
    "the",
    "area",
    "metropolitan",
    "remote",
    "hybrid",
    "onsite",
    "on-site",
    "office",
    "based",
    "in",
    "and",
    "or",
}
_COUNTRY_ALIASES = {
    "united kingdom": "uk",
    "england": "uk",
    "scotland": "uk",
    "wales": "uk",
    "great britain": "uk",
    "gb": "uk",
    "united states": "usa",
    "us": "usa",
    "united states of america": "usa",
    "deutschland": "germany",
    "españa": "spain",
}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _job_level(job: JobPosting) -> int:
    return seniority_level(job.seniority or job.title)


_KNOWN_COUNTRIES = {
    *_COUNTRY_ALIASES.values(), "france", "ireland", "netherlands", "belgium", "switzerland",
    "italy", "portugal", "sweden", "denmark", "norway", "finland", "austria", "poland",
    "canada", "australia", "india", "singapore", "japan", "china", "new zealand",
    "south africa", "brazil", "mexico",
}  # fmt: skip


def _same_place(a: set[str], b: set[str]) -> bool:
    """Shared place name, counting a county as its town ("oxford" ~ "oxfordshire")."""
    return any(x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x)))
               for x in a for y in b)  # fmt: skip


def _loc_tokens(part: str) -> set[str]:
    part = part.strip().lower()
    part = _COUNTRY_ALIASES.get(part, part)
    words = {w for w in re.findall(r"[a-zà-ÿ\-]+", part) if w not in _LOC_NOISE}
    return {_COUNTRY_ALIASES.get(w, w) for w in words}


def location_match(candidate_locations: list[str], job: JobPosting) -> float:
    """1.0 same town or county / source-verified radius · 0.4 same country · 0.7 unknown ·
    0.3 unplaced town (distance unverified) · 0 a different, known country."""
    if job.within_search_area or job.area_check == "inside":
        return LOC_SAME_CITY
    if not job.location or not candidate_locations:
        return LOC_UNKNOWN
    job_parts = [p for p in job.location.split(",") if p.strip()]
    job_all = set().union(*(_loc_tokens(p) for p in job_parts))
    best = 0.0
    for loc in candidate_locations:
        parts = [p for p in loc.split(",") if p.strip()]
        if not parts:
            continue
        cand_all = set().union(*(_loc_tokens(p) for p in parts))
        if _same_place(_loc_tokens(parts[0]), job_all) or _same_place(
            _loc_tokens(job_parts[0]), cand_all
        ):
            return LOC_SAME_CITY
        if _loc_tokens(parts[-1]) & _loc_tokens(job_parts[-1]):
            best = max(best, LOC_SAME_COUNTRY)
    if best == 0.0 and not job_all & _KNOWN_COUNTRIES:
        # A whole-country search cannot place a town either, but has no distance to verify.
        country_only = all(_loc_tokens(c) <= _KNOWN_COUNTRIES for c in candidate_locations)
        return LOC_UNKNOWN if country_only else LOC_UNVERIFIED  # e.g. "Abingdon"
    return best


def _has_skill(profile: CandidateProfile, skill: str) -> bool:
    return normalize_skill(skill) in profile.skills or mentions(profile.text, skill)


# ------------------------------------------------------------------ exclusions


def hard_exclusions(
    profile: CandidateProfile, job: JobPosting, today: date | None = None
) -> list[str]:
    """Reasons this job must be dropped regardless of score. Empty list = eligible."""
    reasons: list[str] = []

    if job.closes_at is not None and job.closes_at < (today or date.today()):
        reasons.append(f"closed: applications closed on {job.closes_at.isoformat()}")

    missing_certs = [
        c
        for c in job.required_certifications
        if not any(mentions(have, c) or mentions(c, have) for have in profile.certifications)
    ]
    if missing_certs and profile.cv_based:
        reasons.append(f"missing required certification(s): {', '.join(missing_certs)}")

    if profile.work_arrangements and job.work_arrangement not in profile.work_arrangements:
        reasons.append(f"work arrangement '{job.work_arrangement}' not accepted")

    if job.work_arrangement != "remote" and not profile.willing_to_relocate:
        if profile.locations and location_match(profile.locations, job) == 0.0:
            reasons.append(f"location mismatch: {job.location} (no relocation)")
        elif job.area_check == "outside":  # `places.check_area`: the stated town, measured
            reasons.append(
                f"outside the search radius: {job.location} is about "
                f"{job.distance_miles or 0:.0f} miles away (no relocation)"
            )
        elif job.area_check == "abroad":
            reasons.append(f"location mismatch: {job.location} is not a UK place (no relocation)")

    if profile.salary_min and job.salary_max and job.salary_max < profile.salary_min:
        reasons.append(
            f"salary up to {job.salary_max:,.0f} is below minimum {profile.salary_min:,.0f}"
        )

    reasons += _language_exclusions(profile, job)

    level = _job_level(job)
    if profile.seniority_min is not None and level < profile.seniority_min:
        reasons.append(
            f"seniority {seniority_name(level)} is below requested minimum "
            f"{seniority_name(profile.seniority_min)}"
        )
    if profile.seniority_max is not None and level > profile.seniority_max:
        reasons.append(
            f"seniority {seniority_name(level)} is above requested maximum "
            f"{seniority_name(profile.seniority_max)}"
        )
    return reasons


def _known_languages(profile: CandidateProfile) -> dict[str, int]:
    """Declared languages by name and level (1-4). A CV read in English evidences
    professional English even when no language is listed."""
    known = {"english": 3} if profile.cv_based else {}
    for skill in profile.languages:
        parsed = parse_language(skill.language)
        if parsed is not None:
            known[parsed[0]] = max(known.get(parsed[0], 0), LANGUAGE_LEVELS[skill.level])
    return known


def _language_exclusions(profile: CandidateProfile, job: JobPosting) -> list[str]:
    """An essential language the candidate has declared they do not speak at all. Only when
    languages were declared: otherwise the gap is a flag (`eligibility_flags`)."""
    if not profile.languages:
        return []
    known = _known_languages(profile)
    reasons = []
    for group in _language_groups(job):
        required = [(name, level, said) for name, level, essential, said in group if essential]
        if required and not any(name in known for name, _, _ in required):
            names = " or ".join(name.title() for name, _, _ in required)
            reasons.append(f'requires {names}, not among your languages: "{required[0][2]}"')
    return reasons


def _language_groups(job: JobPosting) -> list[list[tuple[str, int, bool, str]]]:
    """Group alternatives in a stated 'one of these languages' requirement."""
    groups: dict[str, list[tuple[str, int, bool, str]]] = {}
    for name, level, essential, said in language_requirements(job.description):
        choice = re.search(r"\b(?:at least one|one of|either|any of)\b", said, re.I)
        named = re.search(rf"\b{re.escape(name)}\b", said, re.I)
        key = said if choice and named and named.start() > choice.start() else f"{said}:{name}"
        groups.setdefault(key, []).append((name, level, essential, said))
    return list(groups.values())


def eligibility_flags(profile: CandidateProfile, job: JobPosting) -> list[str]:
    """Points the user should check before applying, with the posting's own words: a language
    level above the declared one (or undeclared languages), and eligibility conditions
    (right to work, clearance, driving licence, registration) not confirmed in the intent."""
    flags: list[str] = []
    known = _known_languages(profile)
    for group in _language_groups(job):
        required = [(name, level, said) for name, level, essential, said in group if essential]
        if not required or any(known.get(name, 0) >= level for name, level, _ in required):
            continue
        if profile.languages and all(name not in known for name, _, _ in required):
            continue  # already an exclusion
        names = " or ".join(name.title() for name, _, _ in required)
        what = (
            "not declared"
            if all(name not in known for name, _, _ in required)
            else "above your declared level"
        )
        flags.append(f'{names} ({what}): "{required[0][2]}"')
    for kind, said in eligibility_requirements(job.description):
        if not confirms(profile.eligibility, kind):
            flags.append(f'{kind[0].upper()}{kind[1:]}: "{said}"')
    return flags


# ------------------------------------------------------------------ sub-scores


def score_title(profile: CandidateProfile, job: JobPosting) -> float:
    """Role-family overlap (70%) x seniority proximity (30%), best over held/target titles."""
    job_core = title_core(job.title)
    candidates = profile.target_titles + profile.titles[:3]
    family = 0.0
    for t in candidates:
        core = title_core(t)
        overlap = jaccard(core, job_core)
        if core and core <= job_core:  # "ml engineer" fully inside "senior ml platform engineer"
            overlap = max(overlap, 0.9)
        family = max(family, overlap)
    gap = abs(_job_level(job) - profile.seniority_level)
    proximity = _clip(1 - gap / MAX_SENIORITY_GAP)
    return _clip(0.7 * family + 0.3 * proximity)


def score_skills(
    profile: CandidateProfile, job: JobPosting
) -> tuple[float, list[str], list[str], bool]:
    """Weighted coverage of required (75%) and preferred (25%) skills.

    When the posting has no structured skill lists, skills are extracted from its text
    (`inferred=True`) and scored by plain coverage.

    Returns (score, matched skills, missing required skills, inferred).
    """
    req = list(dict.fromkeys(job.required_skills))
    pref = [s for s in dict.fromkeys(job.preferred_skills) if s not in req]
    inferred = not req and not pref
    if inferred:
        req = extract_skills(f"{job.title}\n{job.description}", extra_vocab=profile.skills)
        if not req:
            return 0.5, [], [], True  # Nothing to compare: neutral.
    matched_req = [s for s in req if _has_skill(profile, s)]
    matched_pref = [s for s in pref if _has_skill(profile, s)]
    missing = [s for s in req if s not in matched_req]

    if not pref:
        return len(matched_req) / len(req), matched_req, missing, inferred
    if not req:
        return len(matched_pref) / len(pref), matched_pref, missing, inferred
    value = REQUIRED_SKILL_SHARE * len(matched_req) / len(req) + (1 - REQUIRED_SKILL_SHARE) * len(
        matched_pref
    ) / len(pref)
    return value, matched_req + matched_pref, missing, inferred


def score_experience(profile: CandidateProfile, job: JobPosting) -> float:
    """Years vs requirement (50%) and seniority-level fit (50%)."""
    if job.min_years_experience:
        years = _clip(profile.years_experience / job.min_years_experience)
        if profile.years_experience > job.min_years_experience + 10:
            years = 0.85  # Likely over-qualified
    else:
        years = 1.0
    level = _clip(1 - 0.35 * abs(_job_level(job) - profile.seniority_level))
    return 0.5 * years + 0.5 * level


def score_location(profile: CandidateProfile, job: JobPosting) -> float:
    """Work-arrangement and geography alignment."""
    if profile.work_arrangements and job.work_arrangement not in profile.work_arrangements:
        return 0.0
    if job.work_arrangement == "remote" or not profile.locations:
        return 1.0
    match = location_match(profile.locations, job)
    if match < LOC_SAME_CITY and profile.willing_to_relocate:
        return max(match, LOC_RELOCATE)
    return match


def score_semantic(similarity: float) -> float:
    """Map a raw cosine similarity onto [0, 1]."""
    return _clip((similarity - SEMANTIC_FLOOR) / (SEMANTIC_CEIL - SEMANTIC_FLOOR))


# ------------------------------------------------------------------ total


def score(
    profile: CandidateProfile,
    job: JobPosting,
    similarity: float,
    weights: ScoringWeights | None = None,
) -> ScoreBreakdown:
    """Compute the weighted 0-100 score for one (profile, job) pair."""
    w = weights or get_settings().weights
    title = score_title(profile, job)
    skills, matched, missing, inferred = score_skills(profile, job)
    experience = score_experience(profile, job)
    location = score_location(profile, job)
    semantic = score_semantic(similarity)

    parts = {
        "title": (w.title, title),
        "location": (w.location, location),
        "semantic": (w.semantic, semantic),
    }
    if profile.cv_based:
        parts |= {"skills": (w.skills, skills), "experience": (w.experience, experience)}
    weight_sum = sum(wt for wt, _ in parts.values())
    total = 100 * sum(wt * v for wt, v in parts.values()) / weight_sum

    notes = []
    if not profile.cv_based:
        notes.append("no CV: scored on title, location and semantic fit only")
    elif missing:
        notes.append(f"missing {len(missing)} {'' if inferred else 'required '}skill(s)")
    if inferred:
        notes.append("skills inferred from the description")
    if location == LOC_SAME_COUNTRY:
        notes.append("same country; exact distance unknown")
    elif location == LOC_UNVERIFIED:
        notes.append("town not recognised; distance not verified")
    return ScoreBreakdown(
        title=round(title, 4),
        skills=round(skills, 4),
        experience=round(experience, 4),
        location=round(location, 4),
        semantic=round(semantic, 4),
        total=round(total, 2),
        matched_skills=matched,
        missing_required_skills=missing if profile.cv_based else [],
        notes=notes,
        metrics_used=list(parts),
    )
