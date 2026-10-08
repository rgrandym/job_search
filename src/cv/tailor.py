"""Tailor a Master CV to a job description.

Pipeline:
    analyze_jd (LLM) -> propose_plan (LLM, steered by the job_matcher's verdict)
    -> apply_plan (deterministic guards) -> critique_cv (LLM, a second reader)
    -> revise_plan (LLM) -> apply_plan again -> TailoredCV

The LLM only *proposes* changes. `apply_plan` decides what is accepted. A rewrite is
rejected (the original bullet is kept) if it introduces numbers or skills that its source
bullet does not contain; a summary if it adds numbers or skills the CV lacks. The CV's own
headline is kept; up to 4 suggested headlines that pass the headline guard are offered.
Nothing is removed: relevant bullets and skills move up, the rest move to the end of their
section. Bullets may be left out only when the user's length choice or `assess_trim` (decided
here, not by the model) allows it, keeping at least two per role; a JD keyword lost that way
is put back. Every decision is recorded in `TailoredCV.changes`.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Literal

from src.core import progress
from src.core.llm_provider import LLMProvider
from src.cv.claims import overclaim
from src.cv.models import (
    Bullet,
    ChangeRecord,
    CVCritique,
    JDAnalysis,
    MasterCV,
    SkillGroup,
    TailoredCV,
    TailoredCVEdits,
    TailoringPlan,
    TrimAssessment,
)
from src.tools.search_tools import (
    extract_numbers,
    extract_skills,
    mentions,
    normalize_skill,
    normalize_skills,
    role_words,
    seniority_level,
    seniority_name,
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
3. Write concise, specific accomplishments. State a result only when the source records one; \
do not imply leadership, ownership, scale, seniority or impact beyond the source.
4. Keep every fact, detail and number of the source bullet: rephrase and reorder clauses \
to fit the job, but never shorten away information.
5. Use the job's terms only for the same evidenced skill or work. Avoid keyword stuffing, \
superlatives and generic claims such as 'expert' or 'proven leader'.

6. Keep everything. The CV's own headline, titles, section headings and publications stay \
as they are. Every bullet stays: `bullet_order` lists each role's bullet ids with the most \
relevant first, and the rest follow. Rewrite only bullets that gain from the job's wording; \
leave the others unchanged.

Also return `headline_options`: up to 4 alternative headlines for this job, for the user to \
choose from (the CV keeps its own unless they do). Each names the candidate's own level and \
function (never a level above any title held), a role the CV shows, and only skills the CV \
evidences.

Also return a 3-4 sentence summary built only from facts in the CV. Open with the strongest \
evidence for this job, then draw relevant general context from the original CV summary and \
retain distinctive breadth from the full CV: leadership, hands-on practice, or adjacent \
experience when evidenced. Do not reduce the candidate to the job description or repeat a \
keyword list. If the CV's summary has several paragraphs (separated by a blank line), \
return the same number, separated the same way, at about the same length. \
Return `bullet_order` for every role. Return `skills_priority`: the CV's skills ordered by \
relevance to the job \
(skills you leave out stay, after the listed ones).

If a <trimming> block allows it, you may leave out of `bullet_order` bullets that would \
overstate the candidate for this more junior role, keeping at least two per role. Without \
it, never leave a bullet out. If a <match_assessment> is given, lead with the evidence it \
names under reasons and transferable, and never paper over its gaps. If a <review> is given, \
revise your previous plan to address it under the same rules."""

CRITIC_SYSTEM = """You are a demanding hiring manager reviewing a CV tailored to one job. You \
see the job description, the Master CV (every fact the candidate has) and the tailored CV. \
Report only:
- missed_requirements: requirements in the job description that a Master CV bullet \
evidences but the tailored CV leaves out or buries; cite that bullet's id. Never cite a \
requirement the Master CV does not evidence.
- weak_bullets: tailored bullets that are passive, generic, off-target or hide their \
result or overstate their scope (by id), and what to fix using only that bullet's own facts.
- order_notes: at most 3 notes on the order of bullets or sections for this job. Flag a \
summary that is too terse, only restates the job description, or omits distinctive, relevant \
breadth evidenced in the Master CV.
Return empty lists when the CV is already strong. Never suggest adding a fact.
The tailored CV keeps every line (only the user's length setting leaves lines out), \
its roles in date order, the design and sections of their own Word CV, and their own \
headline (suggested headlines are offered separately). Suggest only rewording and \
reordering within those rules: never removing or cutting lines, merging bullets, \
restructuring sections or changing the headline."""


def analyze_jd(jd_text: str, llm: LLMProvider) -> JDAnalysis:
    """Extract structured requirements and ATS keywords from a JD."""
    return llm.generate(system=ANALYZE_SYSTEM, prompt=jd_text, output_model=JDAnalysis)


def _context(master: MasterCV, jd: JDAnalysis, jd_text: str) -> str:
    return (
        f"<job_description>\n{jd_text}\n</job_description>\n\n"
        f"<jd_analysis>\n{jd.model_dump_json(indent=2)}\n</jd_analysis>\n\n"
        f"<master_cv>\n{master.model_dump_json(indent=2, exclude={'preferences'})}\n</master_cv>"
    )


LevelEmphasis = Literal["auto", "senior", "junior"]
LengthChoice = Literal["auto", "full", "junior"]
MAX_HEADLINE_OPTIONS = 4
YEARS_ASKED = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|to)\s*\d{1,2}\s*)?years?", re.IGNORECASE)


def _career_years(master: MasterCV) -> float:
    starts = [e.start for e in master.experience if e.start]
    if not starts:
        return 0.0
    year, month = map(int, min(starts).split("-"))
    today = date.today()
    return (today.year - year) + (today.month - month) / 12


def assess_trim(
    master: MasterCV, jd: JDAnalysis, jd_text: str, length: LengthChoice = "auto"
) -> TrimAssessment:
    """May this tailoring leave bullets out? The user's choice decides: "full" never, "junior"
    yes. "auto" only when the role is clearly more junior than the candidate: its stated level
    at least two steps below the highest title held, or one step below while asking for at
    most half the candidate's years. Unknown levels never trim."""
    if length == "full":
        return TrimAssessment(allowed=False, reason="you chose to keep the full length")
    if length == "junior":
        return TrimAssessment(allowed=True, reason="you chose to tailor for a junior role")
    if not jd.seniority or not master.experience:
        return TrimAssessment(allowed=False, reason="the job does not state its level")
    job = seniority_level(jd.seniority)
    held = max(seniority_level(e.title) for e in master.experience)
    gap = held - job
    asked = [int(n) for n in YEARS_ASKED.findall(jd_text)]
    years = _career_years(master)
    job_name, held_name = seniority_name(job), seniority_name(held)
    if gap >= 2:
        reason = f"a {job_name}-level role; you have held {held_name}-level roles"
        return TrimAssessment(allowed=True, reason=reason)
    if gap == 1 and asked and max(asked) * 2 <= years:
        reason = (
            f"a {job_name}-level role asking for {max(asked)}+ years; you have about {years:.0f}"
        )
        return TrimAssessment(allowed=True, reason=reason)
    return TrimAssessment(
        allowed=False, reason=f"a {job_name}-level role: not more junior than your experience"
    )


def _trimming(trim: TrimAssessment) -> str:
    if not trim.allowed:
        return ""
    return f"\n\n<trimming>\nAllowed: {trim.reason}.\n</trimming>"


TailoringEmphasis = Literal["auto", "leadership", "hands_on"]


def _preferences(emphasis: TailoringEmphasis, level: LevelEmphasis) -> str:
    """Steer which true facts to foreground without changing the candidate's level."""
    focus = {
        "auto": "Choose the strongest evidenced emphasis for this job.",
        "leadership": "Foreground evidenced people, matrix, or project leadership and scope.",
        "hands_on": (
            "Foreground evidenced hands-on delivery, methods, and wet-lab work where relevant."
        ),
    }[emphasis]
    tone = {
        "auto": "Use the candidate's evidenced level and the role's context.",
        "senior": (
            "Emphasise evidenced strategic scope and senior achievements; "
            "do not claim a higher title."
        ),
        "junior": (
            "Emphasise evidenced practical contribution and learning; do not imply less experience."
        ),
    }[level]
    return f"\n\n<tailoring_preferences>\n{focus} {tone}\n</tailoring_preferences>"


def propose_plan(
    master: MasterCV,
    jd: JDAnalysis,
    jd_text: str,
    llm: LLMProvider,
    guidance: str = "",
    emphasis: TailoringEmphasis = "auto",
    level: LevelEmphasis = "auto",
    trim: TrimAssessment | None = None,
) -> TailoringPlan:
    """Ask the LLM for a tailoring plan. The output is untrusted until `apply_plan`.
    `guidance` is the job_matcher's assessment of this job (what fits, what transfers)."""
    steer = f"\n\n<match_assessment>\n{guidance}\n</match_assessment>" if guidance else ""
    prompt = _context(master, jd, jd_text) + steer + _preferences(emphasis, level)
    prompt += _trimming(trim) if trim else ""
    return llm.generate(system=PLAN_SYSTEM, prompt=prompt, output_model=TailoringPlan)


def critique_cv(
    master: MasterCV, tailored: TailoredCV, jd: JDAnalysis, jd_text: str, llm: LLMProvider
) -> CVCritique:
    """A second, fresh reader reviews the tailored CV. Points citing ids that are not Master
    CV bullets are dropped (a review may only point at facts that exist)."""
    prompt = (
        _context(master, jd, jd_text)
        + f"\n\n<tailored_cv>\n{cv_to_text(tailored.cv, tags=False)}\n</tailored_cv>"
    )
    review = llm.generate(system=CRITIC_SYSTEM, prompt=prompt, output_model=CVCritique)
    ids = set(master.bullet_index())
    return review.model_copy(
        update={
            "missed_requirements": [m for m in review.missed_requirements if m.source_id in ids],
            "weak_bullets": [b for b in review.weak_bullets if b.source_id in ids],
            "order_notes": review.order_notes[:3],
        }
    )


def revise_plan(
    master: MasterCV,
    jd: JDAnalysis,
    jd_text: str,
    plan: TailoringPlan,
    critique: CVCritique,
    llm: LLMProvider,
    guidance: str = "",
    emphasis: TailoringEmphasis = "auto",
    level: LevelEmphasis = "auto",
    trim: TrimAssessment | None = None,
) -> TailoringPlan:
    """A revised plan that addresses the review (still untrusted until `apply_plan`)."""
    steer = f"\n\n<match_assessment>\n{guidance}\n</match_assessment>" if guidance else ""
    prompt = (
        _context(master, jd, jd_text)
        + steer
        + _preferences(emphasis, level)
        + (_trimming(trim) if trim else "")
        + f"\n\n<previous_plan>\n{plan.model_dump_json(indent=2)}\n</previous_plan>"
        + "\n\n<review>\n"
        + "\n".join(f"- {n}" for n in critique.notes())
        + "\n</review>"
    )
    return llm.generate(system=PLAN_SYSTEM, prompt=prompt, output_model=TailoringPlan)


def apply_plan(
    master: MasterCV, plan: TailoringPlan, jd: JDAnalysis, trim: TrimAssessment | None = None
) -> TailoredCV:
    """Apply `plan` to a copy of `master`, rejecting any change that adds unsupported facts.
    The headline stays; bullets and skills are reordered, and bullets are left out only when
    `trim` allows it."""
    cv = master.model_copy(deep=True)
    changes = _apply_rewrites(cv, master, plan, jd)
    before_order = {e.id: list(e.bullets) for e in cv.experience}
    _apply_bullet_order(cv, plan.order_by_role(), trim is not None and trim.allowed)
    cv.skills = _prioritise_skills(cv.skills, plan.skills_priority)
    changes += _apply_summary(cv, master, plan.summary, jd)
    options, rejected = _headline_options(cv, master, plan.headline_options, jd)
    changes += rejected
    restored = _restore_dropped(cv, before_order, jd)
    changes += _removals(cv, before_order, trim)

    matched, missing = keyword_coverage(cv, jd)
    total = len(matched) + len(missing)
    return TailoredCV(
        cv=cv,
        target_title=jd.job_title,
        target_company=jd.company,
        keyword_coverage=len(matched) / total if total else 1.0,
        matched_keywords=matched,
        missing_keywords=missing,
        restored_keywords=restored,
        changes=changes,
        trim=trim,
        headline_options=options,
    )


def revise_tailored(
    source: MasterCV, tailored: TailoredCV, edits: TailoredCVEdits, jd: JDAnalysis
) -> TailoredCV:
    """Apply user text edits only when the original CV supports every changed claim."""
    cv = tailored.cv.model_copy(deep=True)
    visible = cv.bullet_index()
    original = source.bullet_index()
    cv_skills = normalize_skills(source.all_skills())
    for bullet_id, text in edits.bullets.items():
        if bullet_id not in visible or bullet_id not in original:
            raise ValueError(f"Unknown visible bullet: {bullet_id}")
        if reason := _fabrication_reason(original[bullet_id], text, jd, cv_skills, source):
            raise ValueError(f"Bullet {bullet_id}: {reason}")
        visible[bullet_id].text = text
    if edits.headline is not None:
        if not edits.headline.strip():
            raise ValueError("Headline cannot be empty")
        changes = _apply_headline(cv, source, edits.headline, jd)
        if changes and not changes[0].accepted:
            raise ValueError(f"Headline: {changes[0].reason}")
        cv.basics.headline = edits.headline
    if edits.summary is not None:
        if not edits.summary.strip():
            raise ValueError("Summary cannot be empty")
        changes = _apply_summary(cv, source, edits.summary, jd)
        if changes and not changes[0].accepted:
            raise ValueError(f"Summary: {changes[0].reason}")
        cv.basics.summary = edits.summary
    matched, missing = keyword_coverage(cv, jd)
    total = len(matched) + len(missing)
    return tailored.model_copy(
        update={
            "cv": cv,
            "matched_keywords": matched,
            "missing_keywords": missing,
            "keyword_coverage": len(matched) / total if total else 1.0,
            "ats": None,
        }
    )


def tailor(
    master: MasterCV,
    jd_text: str,
    llm: LLMProvider,
    guidance: str = "",
    review: bool = True,
    jd: JDAnalysis | None = None,
    emphasis: TailoringEmphasis = "auto",
    level: LevelEmphasis = "auto",
    length: LengthChoice = "auto",
) -> TailoredCV:
    """End-to-end: JD text + Master CV -> guarded TailoredCV. With `review`, a second reader
    critiques the result and, if it finds issues, one revised plan replaces the first (both
    pass the same guards). `jd`: an analysis already made of this JD text (saves a call)."""
    if jd is None:
        progress.step("Analysing the job description")
        jd = analyze_jd(jd_text, llm)
    trim = assess_trim(master, jd, jd_text, length)
    progress.step("Planning the tailored CV")
    plan = propose_plan(master, jd, jd_text, llm, guidance, emphasis, level, trim)
    progress.step("Checking every change against your CV")
    out = apply_plan(master, plan, jd, trim)
    if not review:
        return out
    progress.step("Reviewing the draft as a second reader")
    critique = critique_cv(master, out, jd, jd_text, llm)
    if not critique.has_issues():
        progress.step("No revision needed")
        return out
    progress.step("Revising the draft from the review")
    revised = revise_plan(master, jd, jd_text, plan, critique, llm, guidance, emphasis, level, trim)
    return apply_plan(master, revised, jd, trim).model_copy(update={"critique": critique.notes()})


def _apply_rewrites(
    cv: MasterCV, master: MasterCV, plan: TailoringPlan, jd: JDAnalysis
) -> list[ChangeRecord]:
    """Accept each rewritten bullet that passes `_fabrication_reason`; record every one."""
    bullets = cv.bullet_index()
    cv_skills = normalize_skills(master.all_skills())
    changes: list[ChangeRecord] = []
    for rw in plan.rewritten_bullets:
        src = bullets.get(rw.source_id)
        if src is None:
            changes.append(_record(rw.source_id, "", rw.text, "unknown source_id"))
            continue
        reason = _fabrication_reason(src, rw.text, jd, cv_skills, master) or _loss_reason(
            src.text, rw.text
        )
        changes.append(_record(src.id, src.text, rw.text, reason))
        if reason is None:
            src.text = rw.text
    return changes


def _record(source_id: str, original: str, tailored: str, reason: str | None) -> ChangeRecord:
    return ChangeRecord(
        source_id=source_id,
        original=original,
        tailored=tailored,
        accepted=reason is None,
        reason=reason,
    )


def _skill_vocab(master: MasterCV, jd: JDAnalysis) -> list[str]:
    return [*jd.hard_skills, *jd.must_have, *master.all_skills()]


def _fabrication_reason(
    source: Bullet, new: str, jd: JDAnalysis, cv_skills: set[str], master: MasterCV
) -> str | None:
    """Return why `new` is not a faithful rewrite of `source`, or None if it is. A rewrite
    restates one bullet: numbers and skills must come from that bullet (its text, metrics or
    listed skills), not from elsewhere in the CV."""
    if not new.strip():
        return "empty rewrite"
    if reason := overclaim(new):
        return reason
    allowed_numbers = extract_numbers(source.text) | extract_numbers(" ".join(source.metrics))
    invented = extract_numbers(new) - allowed_numbers
    if invented:
        return f"introduces numbers not in source: {sorted(invented)}"
    evidence = f"{source.text} {' '.join(source.skills)}"
    for skill in [*jd.hard_skills, *jd.must_have]:
        unsupported = normalize_skills([skill]) - cv_skills
        if unsupported and mentions(new, skill) and not mentions(evidence, skill):
            return f"claims JD skill not evidenced in Master CV: {skill!r}"
    for skill in extract_skills(new, _skill_vocab(master, jd)):
        if not mentions(evidence, skill):
            return f"names a skill this bullet does not evidence: {skill!r}"
    return None


def _loss_reason(source: str, new: str) -> str | None:
    """Why a rewrite loses information from its source line, or None. Tailoring rephrases;
    it never drops the line's numbers or most of its detail."""
    dropped = extract_numbers(source) - extract_numbers(new)
    if dropped:
        return f"drops numbers from the original: {sorted(dropped)}"
    if len(new.strip()) < 0.6 * len(source.strip()):
        return "drops too much of the original line"
    return None


def _apply_headline(
    cv: MasterCV, master: MasterCV, headline: str | None, jd: JDAnalysis
) -> list[ChangeRecord]:
    """The headline may target the job, but never above the highest level held, never a
    role the CV does not show, and never with numbers or skills the CV lacks."""
    if not headline or headline == master.basics.headline:
        return []
    text = cv_to_text(master)
    held = [e.title for e in master.experience] + [master.basics.headline or ""]
    top = max(seniority_level(t) for t in held if t) if any(held) else 0
    title = re.split(r"\s[|–—-]\s|,|\|", headline)[0]  # the claimed title; the rest is focus
    unseen = sorted(w for w in role_words(title) if not mentions(text, w))
    skills = [
        s for s in extract_skills(headline, _skill_vocab(master, jd)) if not mentions(text, s)
    ]
    reason = None
    if tone := overclaim(headline):
        reason = tone
    elif seniority_level(headline) > top:
        level = seniority_name(seniority_level(headline))
        reason = f"claims a higher level ({level}) than any title held"
    elif unseen:
        reason = f"names a role the CV does not show: {', '.join(unseen)}"
    elif skills or extract_numbers(headline) - extract_numbers(text):
        reason = "adds skills or numbers the CV does not evidence"
    if reason is None:
        cv.basics.headline = headline
    return [_record("headline", master.basics.headline or "", headline, reason)]


def _headline_options(
    cv: MasterCV, master: MasterCV, proposed: list[str], jd: JDAnalysis
) -> tuple[list[str], list[ChangeRecord]]:
    """Suggested headlines that pass the headline guards (the CV keeps its own); rejected
    suggestions are recorded."""
    options: list[str] = []
    rejected: list[ChangeRecord] = []
    own = _plain_text(master.basics.headline or "")
    for text in dict.fromkeys(h.strip() for h in proposed if h.strip()):
        if _plain_text(text) == own or len(options) == MAX_HEADLINE_OPTIONS:
            continue
        record = _apply_headline(cv.model_copy(deep=True), master, text, jd)
        if record and not record[0].accepted:
            rejected.extend(record)
        else:
            options.append(text)
    return options, rejected


def _plain_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _apply_summary(
    cv: MasterCV, master: MasterCV, summary: str | None, jd: JDAnalysis
) -> list[ChangeRecord]:
    """The summary may only use numbers and skills that appear somewhere in the CV."""
    if not summary or summary == master.basics.summary:
        return []
    text = cv_to_text(master)
    invented = extract_numbers(summary) - extract_numbers(text)
    skills = [s for s in extract_skills(summary, _skill_vocab(master, jd)) if not mentions(text, s)]
    reason = None
    if tone := overclaim(summary):
        reason = tone
    elif invented:
        reason = f"introduces numbers not in the CV: {sorted(invented)}"
    elif skills:
        reason = f"names skills the CV does not evidence: {', '.join(skills)}"
    if reason is None:
        cv.basics.summary = summary
    return [_record("summary", master.basics.summary or "", summary, reason)]


def _restore_dropped(
    cv: MasterCV, before_order: dict[str, list[Bullet]], jd: JDAnalysis
) -> list[str]:
    """Put back a left-out bullet when it was the only place a JD keyword appeared; returns
    the keywords restored (they were never gaps, only unsurfaced)."""
    restored: list[str] = []
    for keyword in dict.fromkeys(jd.hard_skills + jd.must_have):
        if mentions(cv_to_text(cv), keyword):
            continue
        for exp in cv.experience:
            kept = {b.id for b in exp.bullets}
            hit = next(
                (
                    b
                    for b in before_order.get(exp.id, [])
                    if b.id not in kept and mentions(f"{b.text} {' '.join(b.skills)}", keyword)
                ),
                None,
            )
            if hit is not None:
                exp.bullets.append(hit)
                restored.append(keyword)
                break
    return restored


def _apply_bullet_order(cv: MasterCV, order: dict[str, list[str]], trim: bool) -> None:
    """Most relevant bullets first. Unlisted bullets follow in their original order, unless
    trimming is allowed: then they are left out, keeping at least two per role."""
    for exp in cv.experience:
        ids = order.get(exp.id)
        if not ids:
            continue
        by_id = {b.id: b for b in exp.bullets}
        kept = list(dict.fromkeys(i for i in ids if i in by_id))
        rest = [i for i in by_id if i not in kept]
        if trim:
            needed = max(0, min(2, len(exp.bullets)) - len(kept))
            kept.extend(rest[:needed])
        else:
            kept.extend(rest)
        exp.bullets = [by_id[i] for i in kept]


def _removals(
    cv: MasterCV, before: dict[str, list[Bullet]], trim: TrimAssessment | None
) -> list[ChangeRecord]:
    """One record per bullet left out (only possible when trimming was allowed)."""
    kept = set(cv.bullet_index())
    reason = f"left out: {trim.reason}" if trim else "left out"
    return [
        _record(b.id, b.text, "", None).model_copy(update={"reason": reason})
        for bullets in before.values()
        for b in bullets
        if b.id not in kept
    ]


def _prioritise_skills(groups: list[SkillGroup], priority: list[str]) -> list[SkillGroup]:
    """Reorder skills (and groups) by JD priority. Never adds or removes a skill."""
    rank: dict[str, int] = {}
    for i, skill in enumerate(priority):
        rank.setdefault(normalize_skill(skill), i)

    def r(skill: str) -> int:
        return rank.get(normalize_skill(skill), len(rank))

    ordered = [g.model_copy(update={"items": sorted(g.items, key=r)}) for g in groups]
    return sorted(ordered, key=lambda g: min(map(r, g.items)))


MAX_KEYWORD_WORDS = 4  # longer must-haves are requirement sentences, not ATS keywords


def ats_keywords(jd: JDAnalysis) -> list[str]:
    """The JD's keywords an ATS matches word for word: hard skills and short must-haves.
    Requirement sentences ("Experience leading scientific teams.") never appear verbatim in
    a CV; the job_matcher judges them on meaning instead."""
    terms = [k for k in jd.hard_skills + jd.must_have if k.strip()]
    short = [
        k for k in terms if len(k.split()) <= MAX_KEYWORD_WORDS and not k.rstrip().endswith(".")
    ]
    return list(dict.fromkeys(short))


def keyword_coverage(cv: MasterCV, jd: JDAnalysis) -> tuple[list[str], list[str]]:
    """Split the JD's ATS keywords (`ats_keywords`) into (present in CV, missing from CV)."""
    text = cv_to_text(cv)
    keywords = ats_keywords(jd)
    matched = [k for k in keywords if mentions(text, k)]
    return matched, [k for k in keywords if k not in matched]


def cv_to_text(cv: MasterCV, tags: bool = True) -> str:
    """Flatten a CV to plain text (for keyword checks and embeddings). `tags=False` leaves
    out each bullet's skill tags, which are evidence for the guards but not printed."""
    parts: list[str] = [cv.basics.headline or "", cv.basics.summary or ""]
    for e in cv.experience:
        parts.append(f"{e.title} at {e.company}")
        parts += [b.text for b in e.bullets]
        parts += [s for b in e.bullets for s in b.skills] if tags else []
    parts += [s for g in cv.skills for s in g.items]
    parts += [f"{p.name}: {p.description} {' '.join(p.skills)}" for p in cv.projects]
    parts += [c.name for c in cv.certifications]
    parts += [f"{ed.degree} {ed.field or ''} {ed.institution}" for ed in cv.education]
    return "\n".join(p for p in parts if p)


def dump_tailored(tailored: TailoredCV) -> str:
    """Pretty JSON for saving a tailored CV with its audit trail."""
    return json.dumps(tailored.model_dump(mode="json", exclude_none=True), indent=2)
