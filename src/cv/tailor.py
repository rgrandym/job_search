"""Tailor a Master CV to a job description.

Pipeline:
    analyze_jd (LLM) -> propose_plan (LLM) -> apply_plan (deterministic guards) -> TailoredCV

The LLM only *proposes* changes. `apply_plan` decides what is accepted. A rewrite is
rejected (the original bullet is kept) if it introduces numbers or JD skills that the
source bullet and the Master CV do not support. Every decision is recorded in
`TailoredCV.changes`.
"""

from __future__ import annotations

import json

from src.core.llm_provider import LLMProvider
from src.cv.models import (
    ChangeRecord,
    JDAnalysis,
    MasterCV,
    SkillGroup,
    TailoredCV,
    TailoringPlan,
)
from src.tools.search_tools import (
    extract_numbers,
    mentions,
    normalize_skill,
    normalize_skills,
)

ANALYZE_SYSTEM = """You analyse job descriptions for ATS optimisation. Extract:
- hard_skills: concrete technologies, tools, methods, domains (canonical names, e.g. "Python", \
"Kubernetes", "A/B testing").
- soft_skills: interpersonal or organisational competencies.
- must_have vs nice_to_have: use the JD's own wording ("required", "preferred", "bonus").
- seniority: one of intern, junior, mid, senior, staff, principal, manager, director, executive.
Do not add requirements the JD does not state."""

PLAN_SYSTEM = """You tailor a candidate's Master CV to a job description.

Hard rules (violations are automatically rejected):
1. Every rewritten bullet sets `source_id` to the id of exactly one Master CV bullet and \
restates only that bullet's facts.
2. Never add numbers, metrics, employers, tools or skills that the source bullet does not \
contain. You may rephrase, reorder clauses and use the JD's terminology for the same thing.
3. Write bullets in STAR form compressed to one sentence: strong action verb, then the task \
or context, then the action, then the measurable result if the source has one.
4. Keep each bullet under 30 words.

Also return: a headline matching the target role, a 2-3 sentence summary built only from \
facts in the CV, `bullet_order` (per experience id, most relevant first; omit irrelevant \
bullets but keep at least two per role), and `skills_priority` (CV skills ordered by JD \
relevance)."""


def analyze_jd(jd_text: str, llm: LLMProvider) -> JDAnalysis:
    """Extract structured requirements and ATS keywords from a JD."""
    return llm.generate(system=ANALYZE_SYSTEM, prompt=jd_text, output_model=JDAnalysis)


def propose_plan(master: MasterCV, jd: JDAnalysis, jd_text: str, llm: LLMProvider) -> TailoringPlan:
    """Ask the LLM for a tailoring plan. The output is untrusted until `apply_plan`."""
    prompt = (
        f"<job_description>\n{jd_text}\n</job_description>\n\n"
        f"<jd_analysis>\n{jd.model_dump_json(indent=2)}\n</jd_analysis>\n\n"
        f"<master_cv>\n{master.model_dump_json(indent=2, exclude={'preferences'})}\n</master_cv>"
    )
    return llm.generate(system=PLAN_SYSTEM, prompt=prompt, output_model=TailoringPlan)


def apply_plan(master: MasterCV, plan: TailoringPlan, jd: JDAnalysis) -> TailoredCV:
    """Apply `plan` to a copy of `master`, rejecting any change that adds unsupported facts."""
    cv = master.model_copy(deep=True)
    bullets = cv.bullet_index()
    cv_skills = normalize_skills(master.all_skills())
    changes: list[ChangeRecord] = []

    for rw in plan.rewritten_bullets:
        src = bullets.get(rw.source_id)
        if src is None:
            changes.append(
                ChangeRecord(
                    source_id=rw.source_id,
                    original="",
                    tailored=rw.text,
                    accepted=False,
                    reason="unknown source_id",
                )
            )
            continue
        reason = _fabrication_reason(src.text, src.metrics, rw.text, jd, cv_skills)
        changes.append(
            ChangeRecord(
                source_id=src.id,
                original=src.text,
                tailored=rw.text,
                accepted=reason is None,
                reason=reason,
            )
        )
        if reason is None:
            src.text = rw.text

    _apply_bullet_order(cv, plan.bullet_order)
    cv.skills = _prioritise_skills(cv.skills, plan.skills_priority)
    if plan.headline:
        cv.basics.headline = plan.headline
    if plan.summary and extract_numbers(plan.summary) <= extract_numbers(cv_to_text(master)):
        cv.basics.summary = plan.summary

    matched, missing = keyword_coverage(cv, jd)
    total = len(matched) + len(missing)
    return TailoredCV(
        cv=cv,
        target_title=jd.job_title,
        target_company=jd.company,
        keyword_coverage=len(matched) / total if total else 1.0,
        matched_keywords=matched,
        missing_keywords=missing,
        changes=changes,
    )


def tailor(master: MasterCV, jd_text: str, llm: LLMProvider) -> TailoredCV:
    """End-to-end: JD text + Master CV -> guarded TailoredCV."""
    jd = analyze_jd(jd_text, llm)
    plan = propose_plan(master, jd, jd_text, llm)
    return apply_plan(master, plan, jd)


def _fabrication_reason(
    source: str, metrics: list[str], new: str, jd: JDAnalysis, cv_skills: set[str]
) -> str | None:
    """Return why `new` is not a faithful rewrite of `source`, or None if it is."""
    if not new.strip():
        return "empty rewrite"
    allowed_numbers = extract_numbers(source) | extract_numbers(" ".join(metrics))
    invented = extract_numbers(new) - allowed_numbers
    if invented:
        return f"introduces numbers not in source: {sorted(invented)}"
    for skill in jd.hard_skills + jd.must_have:
        unsupported = normalize_skills([skill]) - cv_skills
        if unsupported and mentions(new, skill) and not mentions(source, skill):
            return f"claims JD skill not evidenced in Master CV: {skill!r}"
    return None


def _apply_bullet_order(cv: MasterCV, order: dict[str, list[str]]) -> None:
    for exp in cv.experience:
        ids = order.get(exp.id)
        if not ids:
            continue
        by_id = {b.id: b for b in exp.bullets}
        kept = [by_id[i] for i in ids if i in by_id]
        if kept:
            exp.bullets = kept


def _prioritise_skills(groups: list[SkillGroup], priority: list[str]) -> list[SkillGroup]:
    """Reorder skills (and groups) by JD priority. Never adds or removes a skill."""
    rank: dict[str, int] = {}
    for i, skill in enumerate(priority):
        rank.setdefault(normalize_skill(skill), i)

    def r(skill: str) -> int:
        return rank.get(normalize_skill(skill), len(rank))

    ordered = [g.model_copy(update={"items": sorted(g.items, key=r)}) for g in groups]
    return sorted(ordered, key=lambda g: min(map(r, g.items)))


def keyword_coverage(cv: MasterCV, jd: JDAnalysis) -> tuple[list[str], list[str]]:
    """Split the JD's hard skills + must-haves into (present in CV, missing from CV)."""
    text = cv_to_text(cv)
    keywords = list(dict.fromkeys(jd.hard_skills + jd.must_have))
    matched = [k for k in keywords if mentions(text, k)]
    return matched, [k for k in keywords if k not in matched]


def cv_to_text(cv: MasterCV) -> str:
    """Flatten a CV to plain text (for keyword checks and embeddings)."""
    parts: list[str] = [cv.basics.headline or "", cv.basics.summary or ""]
    for e in cv.experience:
        parts.append(f"{e.title} at {e.company}")
        parts += [b.text for b in e.bullets]
        parts += [s for b in e.bullets for s in b.skills]
    parts += [s for g in cv.skills for s in g.items]
    parts += [f"{p.name}: {p.description} {' '.join(p.skills)}" for p in cv.projects]
    parts += [c.name for c in cv.certifications]
    parts += [f"{ed.degree} {ed.field or ''} {ed.institution}" for ed in cv.education]
    return "\n".join(p for p in parts if p)


def dump_tailored(tailored: TailoredCV) -> str:
    """Pretty JSON for saving a tailored CV with its audit trail."""
    return json.dumps(tailored.model_dump(mode="json", exclude_none=True), indent=2)
