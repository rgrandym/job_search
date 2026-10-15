"""Code checks for the optimised mode's red layer (user's decisions 2026-10-14).

Optimised tailoring proposes, for the user to approve before applying:
- review additions: what each role very probably involved but the CV does not say (a new
  bullet, words added inside an existing bullet, an existing bullet reworded in the job's
  terms, a question for a missing number, a skill in its category, one summary sentence, one
  headline). Each quotes real source text and
  posting text; code checks both quotes, the role or bullet it belongs to, and that it adds no
  number, jargon, superlative or claim the user has already declined.
- compressions: overlapping bullets merged, a long bullet tightened, or similar roles merged
  into one entry (every institution and its dates kept). Every number and job term of the
  source lines stays, nothing new is added, and the app never applies one: it is written in red
  below the originals and only the user's choice replaces them.

Nothing here is a confirmed fact: pending items stay outside the factual CV, its score, its
keyword coverage and cover letters.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from src.cv.claims import escalation, jargon, overclaim, third_person
from src.cv.models import (
    AdditionCandidate,
    Bullet,
    ChangeRecord,
    CompressionCandidate,
    CompressionProposal,
    Experience,
    MasterCV,
    ProjectedFit,
    RequirementEvidence,
    ReviewAddition,
    SkillGroup,
    TailoredCV,
)
from src.tools.search_tools import (
    extract_numbers,
    extract_skills,
    mentions,
    normalize_skill,
    role_words,
    seniority_level,
)

# Scaled to the job's open gaps and missing terms (user's decision 2026-10-15): the user
# reviews every proposal, so optimised mode may fill each gap the background makes credible.
MAX_ADDITIONS = 30
MIN_ADDITIONS = 12
MAX_PER_ROLE = 8
POSSIBLE_SHARE = 1 / 3  # at most this share may be "possible" rather than "likely"
MAX_METRICS = 5
MAX_COMPRESSIONS = 6
DECLINED_MATCH = 0.8  # this similar to a declined proposal counts as the same proposal
ROLE_KINDS = {"experience", "enrichment", "rewording", "metric"}
LINE_KINDS = {"enrichment", "rewording", "metric"}  # proposals about one existing bullet
# Rejections a red rewording may carry for the user to judge; invented or lost numbers,
# superlatives, jargon and repetition never become proposals.
REWORDABLE = (
    "claims JD skill",
    "names a skill",
    "uses the job's term",
    "claims leadership",
    "adds scope",
)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def plain_words(text: str) -> str:
    """Lowercase words only, for checking that a quote is really in a text."""
    return " ".join(re.findall(r"[\w]+", text.casefold()))


def _quoted(quote: str, *texts: str) -> bool:
    words = plain_words(quote)
    return len(words.split()) >= 2 and any(f" {words} " in f" {plain_words(t)} " for t in texts)


def declined_match(text: str, declined: Sequence[str]) -> bool:
    """`text` repeats a proposal the user has already said they did not do."""
    key = plain_words(text)
    return any(
        key == plain_words(d)
        or SequenceMatcher(None, key, plain_words(d)).ratio() >= DECLINED_MATCH
        for d in declined
        if d.strip()
    )


@dataclass(frozen=True)
class AdditionContext:
    """What the checks compare proposals against."""

    master: MasterCV
    cv_text: str  # the source CV with skill tags
    printed: str  # the draft as printed
    jd_text: str
    requirements: frozenset[str]  # the requirement map's rows, casefolded
    allowed_numbers: frozenset[str]  # the CV's numbers and its years of experience
    background: str = ""  # stored profile and the user's other CVs: grounds for inference
    declined: tuple[str, ...] = ()
    keywords: frozenset[str] = frozenset()  # the job's ATS keywords
    open_items: int = 0  # key gaps and missing job terms the draft leaves open

    def limit(self) -> int:
        """How many proposals this job may get: its open items and some room, within bounds."""
        return min(MAX_ADDITIONS, max(MIN_ADDITIONS, self.open_items + 6))


def check_additions(
    proposed: Sequence[AdditionCandidate], ctx: AdditionContext
) -> list[ReviewAddition]:
    """Keep the proposals whose quotes, place and wording check out: at most `ctx.limit()`
    (scaled to the open gaps), `MAX_PER_ROLE` per role, `MAX_METRICS` number questions, one
    summary and one headline; "possible" ones at most `POSSIBLE_SHARE` of the total, after
    the "likely" ones."""
    bullets = {b.id: (role, b) for role in ctx.master.experience for b in role.bullets}
    roles = {r.id: r for r in ctx.master.experience}
    top = max((seniority_level(r.title) for r in ctx.master.experience), default=0)
    seen: set[str] = set()
    per_role: Counter[str] = Counter()
    per_kind: Counter[str] = Counter()
    out: list[ReviewAddition] = []
    limit = ctx.limit()
    ordered = sorted(proposed, key=lambda a: a.likelihood != "likely")  # stable: plan order
    for item in ordered:
        text = item.text.strip()
        key = f"{item.kind}:{normalize_skill(text)}"
        if key in seen or not _common_ok(item, text, ctx):
            continue
        placed = _placed(item, text, ctx, roles, bullets, top)
        if placed is None:
            continue
        role_id, original = placed
        if item.kind in ROLE_KINDS and per_role[role_id or ""] >= MAX_PER_ROLE:
            continue
        cap = {"metric": MAX_METRICS, "summary": 1, "headline": 1}.get(item.kind)
        if cap is not None and per_kind[item.kind] >= cap:
            continue
        if item.likelihood == "possible" and per_kind["possible"] >= limit * POSSIBLE_SHARE:
            continue
        seen.add(key)
        per_role[role_id or ""] += item.kind in ROLE_KINDS
        per_kind[item.kind] += 1
        per_kind["possible"] += item.likelihood == "possible"
        out.append(_addition(item, text, role_id, original))
        if len(out) == limit:
            break
    return out


def _common_ok(item: AdditionCandidate, text: str, ctx: AdditionContext) -> bool:
    """Checks every kind shares: real posting quote, a requirement of this job, a reason, no
    jargon or superlative, not already printed, not something the user declined. A rewording
    may instead name a job keyword its line does not (its point is the job's terms)."""
    rewords = item.kind == "rewording" and any(mentions(text, k) for k in ctx.keywords)
    return bool(
        text
        and len(text) <= 400
        and item.reason.strip()
        and (_quoted(item.job_quote, ctx.jd_text) or rewords)
        and (
            item.requirement.casefold() in ctx.requirements
            or mentions(ctx.jd_text, item.requirement)
            or rewords
        )
        and not overclaim(text)
        and not jargon(ctx.cv_text, text)
        and not third_person(ctx.cv_text, text)
        and (item.kind == "metric" or not mentions(ctx.printed, text))
        and not declined_match(text, ctx.declined)
    )


def _placed(
    item: AdditionCandidate,
    text: str,
    ctx: AdditionContext,
    roles: dict[str, Experience],
    bullets: dict[str, tuple[Experience, Bullet]],
    top: int,
) -> tuple[str | None, str] | None:
    """(role id, the anchor bullet's text) when the proposal belongs where it says and its
    background quote is real; None otherwise."""
    if item.kind in LINE_KINDS:
        found = bullets.get(item.anchor_id or "")
        if found is None:
            return None
        role, bullet = found
        grounds = (bullet.text, f"{role.title} {role.company}", ctx.background)
        new_numbers = extract_numbers(text) - extract_numbers(bullet.text)
        if not _quoted(item.background_quote, *grounds) or new_numbers:
            return None
        if item.kind == "rewording" and (
            extract_numbers(bullet.text) - extract_numbers(text)
            or plain_words(text) == plain_words(bullet.text)
        ):
            return None  # a rewording keeps the line's numbers and changes its words
        if item.kind == "metric" and extract_numbers(bullet.text):
            return None  # the line already has its figure
        if item.kind == "enrichment" and mentions(bullet.text, text):
            return None
        return role.id, bullet.text
    if item.kind == "experience":
        anchor = bullets.get(item.anchor_id or "")
        home = anchor[0] if anchor else roles.get(item.role_id or "")
        if home is None:
            return None
        role_text = " ".join([home.title, home.company, *(b.text for b in home.bullets)])
        if not _quoted(item.background_quote, role_text, ctx.background):
            return None
        if extract_numbers(text) - extract_numbers(role_text):
            return None
        return home.id, ""
    if item.role_id is not None or not _quoted(item.background_quote, ctx.cv_text, ctx.background):
        return None
    if item.kind == "skill":
        return None, ""
    if extract_numbers(text) - ctx.allowed_numbers:
        return None
    if item.kind == "headline" and seniority_level(text) > top:
        return None
    return None, ""


def _addition(
    item: AdditionCandidate, text: str, role_id: str | None, original: str
) -> ReviewAddition:
    group = item.skill_group if item.kind == "skill" else None
    anchor = item.anchor_id if item.kind in ROLE_KINDS else None
    identity = f"{item.kind}\0{role_id}\0{anchor}\0{text}"
    return ReviewAddition.model_validate(
        {
            **item.model_dump(),
            "text": text,
            "role_id": role_id,
            "anchor_id": anchor,
            "skill_group": group,
            "id": hashlib.sha256(identity.encode()).hexdigest()[:12],
            "original_text": original,
        }
    )


def rewordings(
    changes: Sequence[ChangeRecord], requirements: Sequence[RequirementEvidence]
) -> list[AdditionCandidate]:
    """Rewrites the factual checks rejected for a reason the user can judge (a job skill or
    term the line does not name, more ownership or reach), as red rewordings to confirm or
    delete: in optimised mode, a line in the job's terms is the user's call, not lost."""
    out: list[AdditionCandidate] = []
    for change in changes:
        if change.accepted or not change.tailored or not change.reason:
            continue
        if not change.reason.startswith(REWORDABLE):
            continue
        row = next((r for r in requirements if change.source_id in r.evidence), None)
        out.append(
            AdditionCandidate(
                kind="rewording",
                text=change.tailored,
                anchor_id=change.source_id,
                requirement=row.requirement if row else "",
                background_quote=" ".join(change.original.split()[:12]),
                job_quote="",
                reason=(
                    f"Your line in the job's terms. The factual checks could not confirm it "
                    f"({change.reason}); confirm only if it describes what you did."
                ),
            )
        )
    return out


def skill_group(cv: MasterCV, name: str | None) -> str | None:
    """The CV's own skills category called `name` (case and spacing ignored), or None."""
    if not name:
        return None
    wanted = " ".join(name.casefold().split())
    return next(
        (g.category for g in cv.skills if " ".join(g.category.casefold().split()) == wanted), None
    )


def as_candidate(addition: ReviewAddition) -> AdditionCandidate:
    """A checked addition as a proposal again (to check it beside new ones)."""
    return AdditionCandidate.model_validate(
        addition.model_dump(exclude={"id", "status", "original_text"})
    )


def rank_additions(
    additions: list[ReviewAddition], projection: ProjectedFit | None, missing: list[str]
) -> list[ReviewAddition]:
    """Pending proposals ordered by what confirming them would change (requirements the
    reader newly found, then job terms only they name); once the reader credited some
    proposal, those that change nothing are dropped. Number questions and headlines stay: a
    reader cannot credit a question or a title."""
    if projection is None:
        return additions
    gained = Counter(g.addition_id for g in projection.gained if g.addition_id)
    terms = {a.id: sum(mentions(a.text, k) for k in missing) for a in additions}
    if not gained:  # the reader credited no proposal: nothing tells which change nothing
        return sorted(
            additions,
            key=lambda a: (a.status != "pending", -terms[a.id], a.likelihood != "likely"),
        )

    def worth(a: ReviewAddition) -> bool:
        return (
            a.status != "pending"
            or a.kind in {"metric", "headline"}
            or bool(gained[a.id] or terms[a.id])
        )

    kept = [a for a in additions if worth(a)]
    return sorted(
        kept,
        key=lambda a: (
            a.status != "pending",
            -gained[a.id],
            -terms[a.id],
            a.likelihood != "likely",
        ),
    )


# --- compressions ---------------------------------------------------------------------------


def check_compressions(
    proposed: Sequence[CompressionCandidate],
    master: MasterCV,
    keywords: Sequence[str],
    vocab: Sequence[str],
) -> list[CompressionProposal]:
    """Keep the compressions that keep every number and job term of their source lines, add
    nothing those lines do not show, and stay within one role (or merge similar roles)."""
    roles = {r.id: r for r in master.experience}
    used: set[str] = set()
    out: list[CompressionProposal] = []
    for item in proposed:
        if item.kind == "condense_role":
            continue  # built by code (`condensed_roles`), never taken from the model
        if item.kind == "merge_roles":
            continue  # different roles are never combined (user's decision 2026-10-15)
        sources = _sources(item, roles)
        if sources is None or used & {b.id for b in sources}:
            continue
        bullets = [b.strip() for b in item.bullets if b.strip()]
        if not bullets or _compression_reason(item, sources, bullets, keywords, vocab):
            continue
        used |= {b.id for b in sources}
        chosen = [roles[i] for i in dict.fromkeys(item.role_ids)]
        identity = f"{item.kind}\0{','.join(b.id for b in sources)}\0{'|'.join(bullets)}"
        out.append(
            CompressionProposal.model_validate(
                {
                    **item.model_dump(),
                    "role_ids": [r.id for r in chosen],
                    "source_ids": [b.id for b in sources],
                    "bullets": bullets,
                    "id": hashlib.sha256(identity.encode()).hexdigest()[:12],
                }
            )
        )
        if len(out) == MAX_COMPRESSIONS:
            break
    return out


OLD_ROLE_YEARS = 10
KEEP_OLD_LINES = 2


def condensed_roles(
    cv: MasterCV, keywords: Sequence[str], today: date | None = None
) -> list[CompressionProposal]:
    """Roles that ended over `OLD_ROLE_YEARS` years ago with more than three lines, cut to
    their `KEEP_OLD_LINES` most relevant ones (lines naming a job term first, then the
    draft's order). Built by code, shown in red, applied only by the user (user's decision
    2026-10-15: a recruiter reads the recent roles; an earlier career reads best brief)."""
    today = today or date.today()
    out: list[CompressionProposal] = []
    for role in cv.experience:
        ended = int(role.end[:4]) if role.end else today.year
        if today.year - ended <= OLD_ROLE_YEARS or len(role.bullets) <= KEEP_OLD_LINES + 1:
            continue
        ranked = sorted(role.bullets, key=lambda b: not any(mentions(b.text, k) for k in keywords))
        kept = {b.id for b in ranked[:KEEP_OLD_LINES]}
        left = [b for b in role.bullets if b.id not in kept]
        identity = f"condense_role\0{role.id}\0{','.join(b.id for b in left)}"
        out.append(
            CompressionProposal(
                id=hashlib.sha256(identity.encode()).hexdigest()[:12],
                kind="condense_role",
                role_ids=[role.id],
                source_ids=[b.id for b in left],
                bullets=[b.text for b in role.bullets if b.id in kept],
                removed=[b.text for b in left],
                reason=f"Ended in {ended}: a reader takes in an earlier role from its "
                f"{KEEP_OLD_LINES} most relevant lines",
            )
        )
    return out


def _sources(item: CompressionCandidate, roles: dict[str, Experience]) -> list[Bullet] | None:
    """The source bullets in CV order, or None when the proposal's shape is wrong."""
    ids = list(dict.fromkeys(item.role_ids))
    if not ids or any(i not in roles for i in ids):
        return None
    chosen = [roles[i] for i in ids]
    if item.kind == "merge_roles":
        if (
            len(chosen) < 2
            or not _similar(chosen)
            or len(item.bullets) >= sum(len(r.bullets) for r in chosen)
        ):
            return None
        return [b for r in chosen for b in r.bullets]
    if len(chosen) != 1 or len(item.bullets) != 1:
        return None
    own = {b.id for b in chosen[0].bullets}
    wanted = list(dict.fromkeys(item.source_ids))
    if not wanted or any(i not in own for i in wanted):
        return None
    if (item.kind == "tighten") != (len(wanted) == 1):
        return None  # tighten: exactly one line; merge_bullets: two or more
    return [b for b in chosen[0].bullets if b.id in wanted]


def _similar(roles: list[Experience]) -> bool:
    """Roles of the same kind: the same level and a shared title word (postdoctoral fellow,
    research fellow)."""
    first = set(role_words(roles[0].title))
    level = seniority_level(roles[0].title)
    return all(
        set(role_words(r.title)) & first and seniority_level(r.title) == level for r in roles[1:]
    )


def _compression_reason(
    item: CompressionCandidate,
    sources: list[Bullet],
    bullets: list[str],
    keywords: Sequence[str],
    vocab: Sequence[str],
) -> str | None:
    """Why the compressed wording is not a faithful, shorter version of its sources."""
    source = " ".join(b.text for b in sources)
    evidence = " ".join(f"{b.text} {' '.join(b.skills)} {' '.join(b.metrics)}" for b in sources)
    new = " ".join(bullets)
    if item.kind == "tighten" and len(new.split()) >= len(source.split()):
        return "not shorter"
    if extract_numbers(source) - extract_numbers(new):
        return "drops a number"
    if extract_numbers(new) - extract_numbers(evidence):
        return "adds a number"
    if reason := overclaim(new) or jargon(source, new) or escalation(source, new):
        return reason
    if reason := third_person(source, new):
        return reason
    if any(not mentions(evidence, s) for s in extract_skills(new, list(vocab))):
        return "names a skill its sources do not show"
    if any(mentions(source, k) and not mentions(new, k) for k in keywords):
        return "drops a job term"
    return None


def _month(value: str | None) -> str:
    if value is None:
        return "Present"
    year, month = value.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


def merged_heading(roles: list[Experience]) -> str:
    """One heading for merged roles that keeps every institution and its own dates."""
    ordered = sorted(roles, key=lambda r: r.start)
    same = len({r.title for r in ordered}) == 1
    parts = [
        f"{'' if same else r.title + ', '}{r.company} ({_month(r.start)} – {_month(r.end)})"
        for r in ordered
    ]
    return (f"{ordered[0].title}, " if same else "") + "; ".join(parts)


def apply_compression(cv: MasterCV, proposal: CompressionProposal) -> MasterCV:
    """`cv` with the compressed version in place of its sources (only on the user's choice)."""
    out = cv.model_copy(deep=True)
    if proposal.kind == "merge_roles":
        return _merge_roles(out, proposal)
    if proposal.kind == "condense_role":
        for entry in out.experience:
            if entry.id in proposal.role_ids:
                entry.bullets = [b for b in entry.bullets if b.id not in proposal.source_ids]
        return MasterCV.model_validate(out.model_dump())
    role = next((r for r in out.experience if r.id == proposal.role_ids[0]), None)
    if role is None:
        raise ValueError("The role of this compressed version is no longer in the draft")
    ids = set(proposal.source_ids)
    sources = [b for b in role.bullets if b.id in ids]
    if not sources:
        raise ValueError("The lines of this compressed version are no longer in the draft")
    first = sources[0].model_copy(
        update={
            "text": proposal.bullets[0],
            "skills": list(dict.fromkeys(s for b in sources for s in b.skills)),
            "metrics": list(dict.fromkeys(m for b in sources for m in b.metrics)),
        }
    )
    role.bullets = [
        first if b.id == first.id else b
        for b in role.bullets
        if b.id not in ids or b.id == first.id
    ]
    return MasterCV.model_validate(out.model_dump())


def _merge_roles(cv: MasterCV, proposal: CompressionProposal) -> MasterCV:
    roles = [r for r in cv.experience if r.id in proposal.role_ids]
    if len(roles) != len(proposal.role_ids):
        raise ValueError("A role of this merged entry is no longer in the draft")
    ordered = sorted(roles, key=lambda r: r.start)
    skills = list(dict.fromkeys(s for r in roles for b in r.bullets for s in b.skills))
    prefix = f"merged-{proposal.id}"
    merged = Experience(
        id=prefix,
        company="; ".join(r.company for r in ordered),
        title=" / ".join(dict.fromkeys(r.title for r in ordered)),
        start=ordered[0].start,
        end=None if any(r.end is None for r in roles) else max(r.end or "" for r in roles),
        bullets=[
            Bullet(id=f"{prefix}-{n}", text=text, skills=[s for s in skills if mentions(text, s)])
            for n, text in enumerate(proposal.bullets)
        ],
    )
    first = min(cv.experience.index(r) for r in roles)
    rest = [r for r in cv.experience if r.id not in proposal.role_ids]
    cv.experience = [*rest[:first], merged, *rest[first:]]
    return MasterCV.model_validate(cv.model_dump())


# --- the draft with its proposals in place --------------------------------------------------

MAX_ROLE_SKILLS = (
    12  # the relevant-skills line: a grouped keyword line (user's decision 2026-10-14)
)


def with_proposals(tailored: TailoredCV) -> TailoredCV | None:
    """The draft as it would read with every pending proposal confirmed as worded, placed
    where confirming places it (`tailored_documents.review_addition`): an experience after
    its bullet, words added inside their bullet, a skill in its category (else "Additional")
    and the role-skills line, a summary sentence after the summary, a headline as the
    headline. A number question adds no wording. None without proposals."""
    pending = [a for a in tailored.review_additions if a.status == "pending"]
    if not pending:
        return None
    out = tailored.model_copy(deep=True)
    for n, item in enumerate(pending):
        if item.kind in {"experience", "enrichment", "rewording"}:
            _place_in_role(out, item, f"proposal-{n}")
        elif item.kind == "summary":
            out.cv.basics.summary = f"{out.cv.basics.summary or ''} {item.text}".strip()
        elif item.kind == "headline":
            out.cv.basics.headline = item.text
        elif item.kind == "skill":
            category = item.skill_group or "Additional"
            group = next((g for g in out.cv.skills if g.category == category), None)
            if group is None:
                out.cv.skills.append(SkillGroup(category=category, items=[item.text]))
            elif item.text not in group.items:
                group.items.append(item.text)
            out.role_skills = list(dict.fromkeys([*out.role_skills, item.text]))[:MAX_ROLE_SKILLS]
    return out


def _place_in_role(out: TailoredCV, item: ReviewAddition, new_id: str) -> None:
    role = next((r for r in out.cv.experience if r.id == item.role_id), None)
    if role is None:
        return
    index = next((i for i, b in enumerate(role.bullets) if b.id == item.anchor_id), None)
    if item.kind in {"enrichment", "rewording"}:
        if index is not None:
            bullet = role.bullets[index]
            text = enriched(bullet.text, item.text) if item.kind == "enrichment" else item.text
            role.bullets[index] = bullet.model_copy(update={"text": text})
        return
    bullet = Bullet(id=new_id, text=item.text)
    if index is None:
        role.bullets.append(bullet)
    else:
        role.bullets.insert(index + 1, bullet)


def enriched(bullet: str, words: str) -> str:
    """A bullet with proposed words added at its end (the default wording to confirm)."""
    return f"{bullet.rstrip().rstrip('.;')}, {words.strip().rstrip('.')}."
