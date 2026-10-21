"""A shorter CV for this job (optimised mode, user's decisions 2026-10-21).

Employers found the CV long and slow to scan. After the plan, one focused call proposes:
- each long role's entry rewritten for this job: the roles central to the job consolidated
  into 6 to 8 bullets without losing information (every job skill their lines name kept),
  the others into 2 or 3. Code decides which roles are central: the `MAX_CENTRAL` roles the
  job's key and essential requirements cite most (the plan's evidence map), whatever their
  age;
- each skills line cut to the skills this job needs; a skill the posting names always stays.
Code checks every version (`proposals.shorter_role_reason`, `trimmed_skills_reason`: fewer,
readable lines, the job terms printed nowhere else kept, every number left out listed,
nothing added) and asks once more for the roles whose version failed or is missing. Each is
a red proposal; only the user applies it. What failed both times is recorded as a change.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from src.core import progress
from src.core.llm_provider import LLMProvider
from src.cv.models import (
    CLAIMABLE,
    ChangeRecord,
    CompressionCandidate,
    CompressionProposal,
    Experience,
    JDAnalysis,
    RoleVersion,
    RoleVersions,
    SkillsLine,
    TailoredCV,
)
from src.cv.proposals import (
    ROLE_LINE_WORDS,
    checked_compression,
    shorter_role_reason,
    trimmed_skills_reason,
)
from src.tools.search_tools import mentions

MAX_CENTRAL = 3
CENTRAL_LINES = (6, 8)
SUPPORTING_LINES = (2, 3)
SHORT_SKILLS_LINE = 6  # a line this short is read at a glance: nothing to cut

SYSTEM = f"""You make a strong CV shorter for one job without making it weaker. Employers \
said the CV is too long and they cannot find the relevant information in a few seconds. The \
result must be as compelling as the original, a better match for this job and quick to read: \
never shorter for its own sake.

Rewrite the whole entry of each role given, as its `lines` attribute asks:
- central="yes" (this job needs its work most, whatever the role's age): consolidate \
without losing information: combine the lines about related work, concepts and methods into \
one bullet each, keeping every method, technique, system, model, result and number that \
matters for this job, and the scientific and technical depth. Name every job skill the lines \
name. Lead with the work this job needs.
- central="no": keep what a reader for this job would value (the transferable work, the \
strongest result, the skills this job uses); list what you leave out in `removed`.

Give each bullet the ids of the role's lines it draws on (`sources`, shown in brackets). \
Each bullet: one or two clear sentences, at most {ROLE_LINE_WORDS - 5} words: what was done, \
how and with what result. Action verb first, in the CV's implied first person; never the \
third person. Plain words, no corporate jargon ("leveraged", "robust", "passionate", \
"results-driven", "spearheaded"), no adjectives about the candidate: compelling through \
specifics. Only facts in the role's lines: no new number, skill, tool, scope, leadership or \
ownership. Write numbers exactly as they appear; list in `removed` every number and detail \
you leave out. Keep every term listed under must_keep for that role. Every role keeps its \
own heading: never combine roles.

Then `skills`: for each skills line given, the skills this job needs, copied exactly, most \
relevant first. Leave out what a hiring manager for this job would not look for (for a \
hands-on lab scientist post, business, operations or machine-learning skills the posting \
does not ask for), so the relevant skills stand out. Keep every skill the posting names. \
Return an empty `keep` when nothing on the line is related; omit a line that stays whole."""


@dataclass(frozen=True)
class ShorterCV:
    """The checked proposals and what the checks rejected twice."""

    proposals: list[CompressionProposal]
    rejected: list[ChangeRecord]


def shorter_cv(
    llm: LLMProvider,
    draft: TailoredCV,
    jd: JDAnalysis,
    jd_text: str,
    keywords: Sequence[str],
    vocab: Sequence[str],
) -> ShorterCV:
    """Checked shorter versions of the draft's long roles and skills lines: one call, and one
    more for the roles whose version failed its checks or is missing. A failed call leaves
    the draft as it is."""
    central = central_roles(draft, keywords)
    roles = [r for r in draft.cv.experience if len(r.bullets) > sizes(r, r.id in central)[1]]
    if not roles and all(len(g.items) <= SHORT_SKILLS_LINE for g in draft.cv.skills):
        return ShorterCV([], [])
    progress.step("Writing a shorter, sharper version of each long role and skills line")
    terms = {r.id: must_keep(draft, r, keywords) for r in roles}
    job_skills = [*jd.hard_skills, *jd.must_have]
    prompt = _prompt(draft, jd, roles, terms, central)
    answer = _ask(llm, prompt)
    kept, problems = _checked(answer, roles, terms, vocab, central, job_skills)
    if problems:
        progress.step("Revising the shorter versions the checks questioned")
        again = [r for r in roles if r.id in problems]
        retry = _ask(llm, prompt + _feedback(problems))
        more, problems = _checked(retry, again, terms, vocab, central, job_skills)
        kept.update(more)
    rejected = [
        ChangeRecord(source_id=f"shorter:{role}", original="", tailored="", accepted=False,
                     reason=f"No shorter version of this role: {reason.split('. Your')[0]}")
        for role, reason in problems.items()
    ]  # fmt: skip
    trims = _skill_trims(answer.skills, draft, jd_text)
    return ShorterCV([kept[r.id] for r in roles if r.id in kept] + trims, rejected)


def central_roles(draft: TailoredCV, keywords: Sequence[str] = ()) -> set[str]:
    """The `MAX_CENTRAL` roles whose lines the job's key and essential requirements cite
    most (the plan's semantic evidence map), then whose lines name the job's keywords most;
    a tie goes to the role higher in the CV. With no evidence at all, the most recent role
    (the first listed)."""
    owner = {b.id: r.id for r in draft.cv.experience for b in r.bullets}
    owner.update({r.id: r.id for r in draft.cv.experience})
    cited = Counter(
        owner[e]
        for row in draft.requirements
        if (row.importance == "key" or row.priority == "essential") and row.match in CLAIMABLE
        for e in row.evidence
        if e in owner
    )
    named = {
        r.id: sum(any(mentions(b.text, k) for k in keywords) for b in r.bullets)
        for r in draft.cv.experience
    }
    order = [r.id for r in draft.cv.experience]
    scored = [role for role in order if cited[role] or named[role]]
    ranked = sorted(scored, key=lambda role: (-cited[role], -named[role], order.index(role)))
    return set(ranked[:MAX_CENTRAL] or order[:1])


def must_keep(draft: TailoredCV, role: Experience, keywords: Sequence[str]) -> list[str]:
    """The job terms `role`'s lines use that the draft prints nowhere else: leaving one out
    would take it off the CV."""
    cv = draft.cv
    elsewhere = " ".join(
        [
            cv.basics.headline or "",
            cv.basics.summary or "",
            *draft.key_strengths,
            *draft.role_skills,
            *(item for group in cv.skills for item in group.items),
            *(b.text for r in cv.experience if r.id != role.id for b in r.bullets),
        ]
    )
    return [
        k
        for k in keywords
        if any(mentions(b.text, k) for b in role.bullets) and not mentions(elsewhere, k)
    ]


def sizes(role: Experience, central: bool) -> tuple[int, int]:
    """How many lines a role's shorter version has: 6 to 8 for a central role, at least two
    fewer than now (one of 7 lines or fewer already reads well: it keeps them); 2 or 3 for
    the others."""
    if not central:
        return SUPPORTING_LINES
    if len(role.bullets) <= CENTRAL_LINES[0] + 1:
        return len(role.bullets), len(role.bullets)
    high = min(CENTRAL_LINES[1], len(role.bullets) - 2)
    return CENTRAL_LINES[0], high


def _ask(llm: LLMProvider, prompt: str) -> RoleVersions:
    try:
        return llm.generate(system=SYSTEM, prompt=prompt, output_model=RoleVersions)
    except Exception:  # noqa: BLE001 - the draft stands without shorter versions
        return RoleVersions()


def _checked(
    versions: RoleVersions,
    roles: list[Experience],
    terms: dict[str, list[str]],
    vocab: Sequence[str],
    central: Collection[str],
    job_skills: Sequence[str],
) -> tuple[dict[str, CompressionProposal], dict[str, str]]:
    """The versions that pass, by role, and why each other long role has none."""
    by_id = {r.id: r for r in roles}
    kept: dict[str, CompressionProposal] = {}
    problems: dict[str, str] = {}
    for version in versions.roles:
        role = by_id.get(version.role_id)
        if role is None or role.id in kept:
            continue
        is_central = role.id in central
        item = _candidate(version, is_central, role)
        keep = job_skills if is_central else ()
        reason = _size_reason(role, item, is_central) or shorter_role_reason(
            role, item, terms[role.id], vocab, keep
        )
        if reason:
            problems[role.id] = f"{reason}. Your version: " + " | ".join(item.bullets)
        else:
            problems.pop(role.id, None)
            kept[role.id] = checked_compression(item, list(role.bullets))
    for role in roles:
        low, high = sizes(role, role.id in central)
        if role.id not in kept and role.id not in problems and len(role.bullets) > high:
            problems[role.id] = f"missing: {len(role.bullets)} lines need {low} to {high}"
    return kept, problems


def _candidate(version: RoleVersion, central: bool, role: Experience) -> CompressionCandidate:
    label = "Central to this job" if central else "Less related to this job"
    own = {b.id for b in role.bullets}
    lines = [line for line in version.bullets if line.text.strip()]
    return CompressionCandidate(
        kind="shorter_role",
        role_ids=[version.role_id],
        bullets=[line.text.strip() for line in lines],
        line_sources=[[i for i in line.sources if i in own] for line in lines],
        removed=version.removed,
        reason=f"{label}: {version.reason}",
    )


def _size_reason(role: Experience, item: CompressionCandidate, central: bool) -> str | None:
    low, high = sizes(role, central)
    n = len([b for b in item.bullets if b.strip()])
    if low <= n <= high:
        return None
    kind = "central to this job" if central else "less related to this job"
    return f"{n} lines; a role {kind} with {len(role.bullets)} lines needs {low} to {high}"


def _skill_trims(
    lines: list[SkillsLine], draft: TailoredCV, jd_text: str
) -> list[CompressionProposal]:
    """The skills lines cut to what this job needs, each checked (`trimmed_skills_reason`)."""
    groups = {_plain(g.category): g for g in draft.cv.skills}
    out: list[CompressionProposal] = []
    for line in lines:
        group = groups.pop(_plain(line.category), None)
        if group is None:
            continue
        own = {_plain(i): i for i in group.items}  # the line's own spelling of each skill
        keep = list(dict.fromkeys(own.get(_plain(k), k) for k in line.keep if k.strip()))
        if trimmed_skills_reason(group, keep, jd_text):
            continue
        identity = f"trim_skills\0{group.category}\0{'|'.join(keep)}"
        out.append(
            CompressionProposal(
                id=hashlib.sha256(identity.encode()).hexdigest()[:12],
                kind="trim_skills",
                category=group.category,
                role_ids=[],
                bullets=keep,
                removed=[i for i in group.items if i not in keep],
                reason=line.reason,
            )
        )
    return out


def _plain(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .;:")


def _prompt(
    draft: TailoredCV,
    jd: JDAnalysis,
    roles: list[Experience],
    terms: dict[str, list[str]],
    central: Collection[str],
) -> str:
    key = [
        f"- {r.requirement} ({r.priority}, {r.match}; evidence: {', '.join(r.evidence) or '-'})"
        for r in draft.requirements
        if r.importance == "key" or r.priority == "essential"
    ]
    job = [
        f"<job>\n{jd.job_title}{f' at {jd.company}' if jd.company else ''}",
        "Must have: " + "; ".join(jd.must_have),
        "Skills: " + ", ".join(jd.hard_skills),
        "Responsibilities: " + "; ".join(jd.responsibilities),
        "Key requirements and the CV lines that show them:",
        *key,
        "</job>",
    ]
    blocks = []
    for r in roles:
        low, high = sizes(r, r.id in central)
        blocks.append(
            f'<role id="{r.id}" central="{"yes" if r.id in central else "no"}" '
            f'lines="{low} to {high}">\n{r.title}, {r.company} ({r.start} to '
            f"{r.end or 'present'})\nmust_keep: {', '.join(terms[r.id]) or '-'}\n"
            + "\n".join(f"- [{b.id}] {b.text}" for b in r.bullets)
            + "\n</role>"
        )
    skills = [f"- {g.category}: {', '.join(g.items)}" for g in draft.cv.skills]
    return (
        "\n".join(job)
        + "\n\n"
        + "\n\n".join(blocks)
        + "\n\n<skills>\n"
        + "\n".join(skills)
        + "\n</skills>"
    )


def _feedback(problems: dict[str, str]) -> str:
    lines = [f"- {role}: {reason}" for role, reason in problems.items()]
    return (
        "\n\n<feedback>\nThe checks rejected or missed these roles. Return a corrected "
        "version of each (skills may be left empty), under the same rules:\n"
        + "\n".join(lines)
        + "\n</feedback>"
    )
