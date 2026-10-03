"""Guarded cover letters: the LLM drafts, code keeps only what the Master CV backs.

    draft_letter (LLM) -> apply_letter (deterministic guards) -> CoverLetter

Every paragraph cites the Master CV ids its claims come from. A paragraph is dropped if it
cites an unknown id, uses a number that is neither in its cited sources nor in the job
description (facts about the employer may be quoted), or names a skill its cited sources do
not evidence. Motivation comes only from the user's own career intent. Every decision is
recorded in `CoverLetter.changes`.
"""

from __future__ import annotations

import re

from src.core.llm_provider import LLMProvider
from src.cv.claims import overclaim
from src.cv.models import ChangeRecord, CoverLetter, CoverLetterDraft, JDAnalysis, MasterCV
from src.tools.search_tools import extract_numbers, extract_skills, mentions

LETTER_SYSTEM = """You write a one-page cover letter for a candidate applying to one job.

Hard rules (violations are automatically removed):
1. Every paragraph lists in `source_ids` the Master CV ids (bullet, role or project ids) its \
claims come from. A claim without a source is not allowed.
2. Never add numbers, employers, tools or skills that the cited sources do not contain. \
Facts about the employer may come from the job description.
3. Motivation (why this role, why this organisation) uses only the candidate's stated \
motivation in <motivation>; if none is given, keep it to one brief sentence about the role \
itself. Never invent personal history or feelings.
4. Avoid hyperbole and generic praise. Show a specific connection between evidenced work \
and the role; make no promise of outcomes that have not happened.

Shape: 3-4 short paragraphs, under 350 words in total. Open with the role and the strongest \
match, then 1-2 paragraphs of evidence mapped to the job's main requirements, then a short, \
forward-looking close: what the candidate would do in the role, not a repeat of the job \
description. Plain, confident, specific; no clichés."""


def draft_letter(
    master: MasterCV, jd: JDAnalysis, jd_text: str, llm: LLMProvider, motivation: str = ""
) -> CoverLetterDraft:
    """Ask the LLM for a draft. The output is untrusted until `apply_letter`."""
    prompt = (
        f"<job_description>\n{jd_text}\n</job_description>\n\n"
        f"<jd_analysis>\n{jd.model_dump_json(indent=2)}\n</jd_analysis>\n\n"
        f"<master_cv>\n{master.model_dump_json(indent=2, exclude={'preferences'})}\n</master_cv>"
        f"\n\n<motivation>\n{motivation or 'none stated'}\n</motivation>"
    )
    return llm.generate(system=LETTER_SYSTEM, prompt=prompt, output_model=CoverLetterDraft)


def _sources(master: MasterCV) -> dict[str, str]:
    """Evidence text per citable id: bullets, roles (title, company and bullets), projects."""
    out = {
        b.id: f"{b.text} {' '.join(b.skills)} {' '.join(b.metrics)}"
        for b in master.bullet_index().values()
    }
    for e in master.experience:
        bullets = " ".join(out[b.id] for b in e.bullets)
        out[e.id] = f"{e.title} {e.company} {e.start} {e.end or ''} {bullets}"
    for p in master.projects:
        out[p.id] = f"{p.name} {p.description} {' '.join(p.skills)}"
    return out


def _paragraph_reason(
    text: str,
    ids: list[str],
    sources: dict[str, str],
    jd: JDAnalysis,
    jd_text: str,
    vocab: list[str],
) -> str | None:
    unknown = [i for i in ids if i not in sources]
    if unknown:
        return f"unknown source_id: {', '.join(unknown)}"
    if reason := overclaim(text):
        return reason
    personal_claim = re.search(r"\b(?:I|my|me|we|our|us)\b", text, re.IGNORECASE)
    if personal_claim and not ids:
        return "personal claim has no CV source_id"
    evidence = " ".join(sources[i] for i in ids)
    allowed_jd_numbers = set() if personal_claim else extract_numbers(jd_text)
    invented = extract_numbers(text) - extract_numbers(evidence) - allowed_jd_numbers
    if invented:
        return f"introduces numbers not in its sources: {sorted(invented)}"
    unbacked = [s for s in extract_skills(text, vocab) if not mentions(evidence, s)]
    if unbacked:
        return f"names a skill its sources do not evidence: {unbacked[0]!r}"
    return None


def apply_letter(
    master: MasterCV, draft: CoverLetterDraft, jd: JDAnalysis, jd_text: str
) -> CoverLetter:
    """Keep the paragraphs whose claims trace to the Master CV; record every decision."""
    sources = _sources(master)
    vocab = [*jd.hard_skills, *jd.must_have, *master.all_skills()]
    kept: list[str] = []
    changes: list[ChangeRecord] = []
    for i, para in enumerate(draft.paragraphs, 1):
        reason = _paragraph_reason(para.text, para.source_ids, sources, jd, jd_text, vocab)
        changes.append(
            ChangeRecord(
                source_id=f"paragraph-{i}",
                original=", ".join(para.source_ids),
                tailored=para.text,
                accepted=reason is None,
                reason=reason,
            )
        )
        if reason is None:
            kept.append(para.text)
    safe = not (extract_numbers(draft.greeting) or extract_numbers(draft.closing))
    return CoverLetter(
        target_title=jd.job_title,
        target_company=jd.company,
        greeting=draft.greeting if safe else "Dear Hiring Manager,",
        paragraphs=kept,
        closing=draft.closing if safe else "Yours sincerely,",
        changes=changes,
    )


def write_letter(
    master: MasterCV, jd: JDAnalysis, jd_text: str, llm: LLMProvider, motivation: str = ""
) -> CoverLetter:
    """End-to-end: draft, then guard."""
    return apply_letter(master, draft_letter(master, jd, jd_text, llm, motivation), jd, jd_text)
