"""Cover letters written from the CV the user approved, in one fixed template.

    draft_letter (LLM) -> apply_letter (fabrication guards) -> CoverLetter

The model reads the approved CV in the job's terms, but named skills must appear in the cited
CV evidence. Every paragraph cites the CV ids it describes, and a paragraph is rejected if it
cites an unknown id, makes a personal claim without a source, or uses a number that is neither
in its cited sources nor in the job description (facts about the employer may be quoted).
Motivation comes only from the user's career intent. Every decision is recorded in
`CoverLetter.changes`.
"""

from __future__ import annotations

import re

from src.core import progress
from src.core.llm_provider import LLMProvider
from src.cv.claims import overclaim
from src.cv.models import (
    ChangeRecord,
    CoverLetter,
    CoverLetterDraft,
    JDAnalysis,
    LetterParagraph,
    LetterSections,
    MasterCV,
)
from src.tools.search_tools import extract_numbers, extract_skills, mentions, role_words

LETTER_SYSTEM = """You write a compelling one-page cover letter for a candidate applying to \
one job.

The candidate has reviewed and approved the CV in <cv>; it is your only source for their \
experience. Read it as a hiring manager in this field would and make the strongest honest \
case: relate the candidate's work to what the job asks for, using named skills only when the \
cited CV entries state them. Interpret the work honestly; do not invent.

Rules:
1. The fit, evidence and conclusion paragraphs list in `source_ids` the CV ids (bullet, role \
or project ids) whose work they describe.
2. Never invent experiences, employers, results, skills or numbers. Named skills must appear \
in the cited CV entries. Numbers come only from those entries, or from the job description \
when describing the employer.
3. Motivation (why this role, why this organisation) uses only the candidate's stated \
motivation in <motivation>; if none is given, ground interest in the work described in the \
posting. Never invent personal history or feelings.
4. No hyperbole, clichés or generic praise, and no promise of outcomes that have not happened.

Template. Fill every field; the letter always has these four paragraphs, under 300 words:
1. `opening.interest`: one sentence naming the exact job title and a specific reason the work \
or organisation interests the candidate.
   `opening.fit`: one sentence stating the candidate's strongest overall fit for the role, \
with `source_ids`. Do not begin with past employers or repeat the CV headline.
2. `evidence`: exactly two paragraphs. Each takes one strong CV example (what the candidate \
did and its result) and ties it to the role's work or a specific requirement. Include \
transferable foundational research when it strengthens the case, even if the posting does not \
name its methods. Use different examples; \
do not recite career history or list every technique.
3. `conclusion`: one or two sentences on the contribution the candidate could make, inviting \
a conversation.

Use a natural greeting and sign-off. Plain, warm, confident and specific. The Word document \
already carries the candidate's name at the end, so put no contact details, header, date or \
signature inside any paragraph."""

MAX_LETTER_WORDS = 300


def draft_letter(
    master: MasterCV,
    jd: JDAnalysis,
    jd_text: str,
    llm: LLMProvider,
    motivation: str = "",
    feedback: str = "",
) -> CoverLetterDraft:
    """Ask the LLM for a draft. The output is untrusted until `apply_letter`."""
    prompt = (
        f"<job_description>\n{jd_text}\n</job_description>\n\n"
        f"<jd_analysis>\n{jd.model_dump_json(indent=2)}\n</jd_analysis>\n\n"
        f"<cv>\n{master.model_dump_json(indent=2, exclude={'preferences'})}\n</cv>"
        f"\n\n<motivation>\n{motivation or 'none stated'}\n</motivation>"
    )
    if feedback:
        prompt += f"\n\n<revision_feedback>\n{feedback}\n</revision_feedback>"
    sections = llm.generate(system=LETTER_SYSTEM, prompt=prompt, output_model=LetterSections)
    return CoverLetterDraft(**sections.model_dump())


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
    text: str, ids: list[str], sources: dict[str, str], jd_text: str, vocab: list[str]
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
    unbacked = [skill for skill in extract_skills(text, vocab) if not mentions(evidence, skill)]
    if unbacked:
        return f"names a skill its sources do not evidence: {unbacked[0]!r}"
    return None


def apply_letter(
    master: MasterCV, draft: CoverLetterDraft, jd: JDAnalysis, jd_text: str
) -> CoverLetter:
    """Keep the paragraphs that trace to the CV without fabrication; record every decision."""
    sources = _sources(master)
    vocab = [*jd.hard_skills, *jd.must_have, *master.all_skills()]
    kept: list[str] = []
    changes: list[ChangeRecord] = []
    paragraphs = list(draft.paragraphs)
    if draft.opening is not None:
        opening = draft.opening
        paragraphs = [
            LetterParagraph(
                text=f"{opening.interest} {opening.fit.text}",
                source_ids=opening.fit.source_ids,
            ),
            *draft.evidence,
        ]
        if draft.conclusion is not None:
            paragraphs.append(draft.conclusion)
    for i, para in enumerate(paragraphs, 1):
        reason = _paragraph_reason(para.text, para.source_ids, sources, jd_text, vocab)
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
    """Draft a guarded letter, retrying once for structure or unsupported claims."""
    feedback = ""
    for attempt in range(2):
        progress.step("Drafting the letter" if attempt == 0 else "Redrafting to fix the checks")
        draft = draft_letter(master, jd, jd_text, llm, motivation, feedback)
        progress.step("Checking every claim against your CV")
        letter = apply_letter(master, draft, jd, jd_text)
        problems = _letter_problems(draft, letter, jd)
        if not problems:
            return letter
        feedback = "Revise the letter to fix these issues: " + "; ".join(problems)
    raise ValueError(
        f"The cover letter could not pass its evidence and structure checks: {feedback}"
    )


def _letter_problems(draft: CoverLetterDraft, letter: CoverLetter, jd: JDAnalysis) -> list[str]:
    """Check each required section after the claim guards have run."""
    problems: list[str] = []
    if draft.opening is None or draft.conclusion is None or not draft.evidence:
        return ["include an opening, two evidence paragraphs, and a conclusion"]
    if not _names_role(draft.opening.interest, jd.job_title):
        problems.append(f"name the exact role, {jd.job_title}, in the opening")
    if not draft.opening.fit.text.strip() or not draft.opening.fit.source_ids:
        problems.append("state a concrete fit in the opening with CV source_ids")
    if len(draft.evidence) != 2:
        problems.append("write exactly two evidence paragraphs")
    for paragraph in draft.evidence:
        if not paragraph.source_ids:
            problems.append("cite CV source_ids for each evidence paragraph")
        if not paragraph.text.strip():
            problems.append("write text for each evidence paragraph")
    if not draft.conclusion.text.strip():
        problems.append("write a conclusion")
    sections = ["opening", *("evidence" for _ in draft.evidence), "conclusion"]
    for section, change in zip(sections, letter.changes, strict=True):
        if not change.accepted:
            problems.append(f"{section}: {change.reason}")
    if sum(len(paragraph.split()) for paragraph in letter.paragraphs) > MAX_LETTER_WORDS:
        problems.append(f"keep the body under {MAX_LETTER_WORDS} words")
    return problems


def _names_role(text: str, job_title: str) -> bool:
    """The opening names the role: the exact title, or all its role words in any order."""
    words = role_words(job_title)
    return mentions(text, job_title) or (bool(words) and all(mentions(text, w) for w in words))
