"""Code checks for the optimised mode's red layer (user's decisions 2026-10-14).

Optimised tailoring proposes, for the user to approve before applying:
- review additions: what each role very probably involved but the CV does not say (a new
  bullet, words added inside an existing bullet, an existing bullet reworded in the job's
  terms, a question for a missing number, a skill in its category, one summary sentence, one
  headline). Each quotes real source text and
  posting text; code checks both quotes, the role or bullet it belongs to, and that it adds no
  number, jargon, superlative or claim the user has already declined.
- compressions: a shorter version of a whole role (its lines synthesised into fewer, every
  job term kept, any number left out listed), overlapping bullets merged or a long bullet
  tightened (every number and job term kept). Nothing new is added, and the app never applies
  one: it is written in red below the originals and only the user's choice replaces them.

Nothing here is a confirmed fact: pending items stay outside the factual CV, its score, its
keyword coverage and cover letters.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Collection, Sequence
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
# A shorter role (user's decision 2026-10-21: employers found the CV long and slow to scan):
# the role's lines synthesised into at most this many, each readable at a glance.
MAX_ROLE_LINES = 8
ROLE_LINE_WORDS = 40
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
    relevant: Collection[str] = (),
) -> list[CompressionProposal]:
    """Keep the compressions that keep every job term (and, except a shorter role's listed
    left-out details, every number) of their source lines, add nothing those lines do not
    show, and stay within one role. A shorter role comes first and takes its role's lines;
    one of a role this job relies on (`relevant`) keeps every skill its lines name."""
    roles = {r.id: r for r in master.experience}
    used: set[str] = set()
    out: list[CompressionProposal] = []
    for item in sorted(proposed, key=lambda c: c.kind != "shorter_role"):
        if item.kind in {"condense_role", "trim_skills"}:
            continue  # built by code or the shorter-CV step, never taken from the plan
        if item.kind == "merge_roles":
            continue  # different roles are never combined (user's decision 2026-10-15)
        sources = _sources(item, roles)
        if sources is None or used & {b.id for b in sources}:
            continue
        if item.kind == "shorter_role":
            role = roles[item.role_ids[0]]
            keep = vocab if role.id in relevant else ()
            if shorter_role_reason(role, item, keywords, vocab, keep):
                continue
        elif _compression_reason(item, sources, _lines(item), keywords, vocab):
            continue
        if item.kind != "shorter_role" and sum(c.kind != "shorter_role" for c in out) == (
            MAX_COMPRESSIONS
        ):
            continue
        used |= {b.id for b in sources}
        out.append(checked_compression(item, sources))
    return out


def _lines(item: CompressionCandidate) -> list[str]:
    return [b.strip() for b in item.bullets if b.strip()]


def shorter_role_reason(
    role: Experience,
    item: CompressionCandidate,
    keywords: Sequence[str],
    vocab: Sequence[str],
    keep_skills: Sequence[str] = (),
) -> str | None:
    """Why a shorter version of `role` is not faithful: the compression checks, and (a role
    central to the job, user's decision 2026-10-21: consolidated without losing information)
    every skill of `keep_skills` its lines name."""
    bullets = _lines(item)
    if not bullets:
        return "no lines"
    if reason := _compression_reason(item, list(role.bullets), bullets, keywords, vocab):
        return reason
    source = " ".join(b.text for b in role.bullets)
    named = extract_skills(source, list(keep_skills)) if keep_skills else []
    lost = [s for s in named if not mentions(" ".join(bullets), s)]
    if lost:
        return "drops skills of a role central to this job: " + ", ".join(lost)
    return None


def trimmed_skills_reason(group: SkillGroup, keep: Sequence[str], job_text: str) -> str | None:
    """Why a shorter skills line is not the line's own skills minus ones this job does not
    name: a kept skill not on the line, nothing left out, or a skill the posting names left
    out."""
    if any(k not in group.items for k in keep):
        return "keeps a skill that is not on this line"
    if len(set(keep)) >= len(group.items):
        return "leaves nothing out"
    named = [i for i in group.items if i not in keep and mentions(job_text, i)]
    if named:
        return "leaves out skills the posting names: " + ", ".join(named)
    return None


def checked_compression(item: CompressionCandidate, sources: list[Bullet]) -> CompressionProposal:
    """A compression that passed its checks, as a proposal with a stable id."""
    bullets = _lines(item)
    identity = f"{item.kind}\0{','.join(b.id for b in sources)}\0{'|'.join(bullets)}"
    return CompressionProposal.model_validate(
        {
            **item.model_dump(),
            "role_ids": list(dict.fromkeys(item.role_ids)),
            "source_ids": [b.id for b in sources],
            "bullets": bullets,
            "id": hashlib.sha256(identity.encode()).hexdigest()[:12],
        }
    )


OLD_ROLE_YEARS = 10
KEEP_OLD_LINES = 2


def condensed_roles(
    cv: MasterCV,
    keywords: Sequence[str],
    today: date | None = None,
    relevant: Collection[str] = (),
) -> list[CompressionProposal]:
    """Roles that ended over `OLD_ROLE_YEARS` years ago with more than three lines, unless
    this job relies on them (`relevant`, user's decision 2026-10-21), cut to
    their `KEEP_OLD_LINES` most relevant ones (lines naming a job term first, then the
    draft's order). Built by code, shown in red, applied only by the user (user's decision
    2026-10-15: a recruiter reads the recent roles; an earlier career reads best brief)."""
    today = today or date.today()
    out: list[CompressionProposal] = []
    for role in cv.experience:
        ended = int(role.end[:4]) if role.end else today.year
        if (
            today.year - ended <= OLD_ROLE_YEARS
            or len(role.bullets) <= KEEP_OLD_LINES + 1
            or role.id in relevant
        ):
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
    if item.kind == "shorter_role":
        return list(chosen[0].bullets) if len(chosen) == 1 else None
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
    if item.kind == "shorter_role":
        if reason := _role_version_reason(sources, bullets, item.removed):
            return reason
    elif extract_numbers(source) - extract_numbers(new):
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


def _role_version_reason(
    sources: list[Bullet], bullets: list[str], removed: list[str]
) -> str | None:
    """Why a shorter role is not fewer, readable lines that list every number they leave out
    (a number is often the result a reader looks for: leaving one out must be visible)."""
    if len(bullets) >= len(sources) or len(bullets) > MAX_ROLE_LINES:
        return f"not fewer lines (at most {MAX_ROLE_LINES})"
    if len(" ".join(bullets).split()) >= len(" ".join(b.text for b in sources).split()):
        return "not shorter"
    if any(len(b.split()) > ROLE_LINE_WORDS for b in bullets):
        return f"a line over {ROLE_LINE_WORDS} words"
    dropped = extract_numbers(" ".join(b.text for b in sources)) - extract_numbers(
        " ".join(bullets)
    )
    if dropped - extract_numbers(" ".join(removed)):
        return "drops a number without listing it as left out"
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


def apply_compression(
    cv: MasterCV, proposal: CompressionProposal, keep: Collection[str] = ()
) -> MasterCV:
    """`cv` with the compressed version in place of its sources (only on the user's choice).
    A shorter role keeps the lines in `keep` (facts the user confirmed since it was written)."""
    out = cv.model_copy(deep=True)
    if proposal.kind == "merge_roles":
        return _merge_roles(out, proposal)
    if proposal.kind == "condense_role":
        for entry in out.experience:
            if entry.id in proposal.role_ids:
                entry.bullets = [b for b in entry.bullets if b.id not in proposal.source_ids]
        return MasterCV.model_validate(out.model_dump())
    if proposal.kind == "shorter_role":
        return _shorter_role(out, proposal, keep)
    if proposal.kind == "trim_skills":
        group = next((g for g in out.skills if g.category == proposal.category), None)
        if group is None:
            raise ValueError("This skills line is no longer in the draft")
        group.items = [i for i in group.items if i in proposal.bullets]
        out.skills = [g for g in out.skills if g.items]
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


def _shorter_role(cv: MasterCV, proposal: CompressionProposal, keep: Collection[str]) -> MasterCV:
    """The role's lines replaced by the shorter version. Each of its lines takes the id of a
    replaced line it draws on (`version_ids`), so the Word writer rewrites that paragraph in
    its own formatting and removes the rest. Lines the version was not written from (a
    confirmed new experience) or that the user confirmed since (`keep`) stay after it: a
    confirmed fact is never lost (user's decision 2026-10-21)."""
    role = next((r for r in cv.experience if r.id == proposal.role_ids[0]), None)
    stay = (
        [b for b in role.bullets if b.id in keep or b.id not in proposal.source_ids] if role else []
    )
    replaced = [b for b in role.bullets if b not in stay] if role else []
    if role is None or len(replaced) < len(proposal.bullets):
        raise ValueError("The role of this shorter version is no longer in the draft")
    skills = list(dict.fromkeys(s for b in role.bullets for s in b.skills))
    metrics = list(dict.fromkeys(m for b in role.bullets for m in b.metrics))
    role.bullets = [
        Bullet(
            id=new_id,
            text=text,
            skills=[s for s in skills if mentions(text, s)],
            metrics=[m for m in metrics if set() < extract_numbers(m) <= extract_numbers(text)],
        )
        for new_id, text in zip(
            version_ids(proposal, [b.id for b in replaced]), proposal.bullets, strict=True
        )
    ] + stay
    return MasterCV.model_validate(cv.model_dump())


def version_ids(proposal: CompressionProposal, replaced: list[str]) -> list[str]:
    """The id each line of a shorter version takes: the first replaced line it draws on not
    taken yet, else the first free replaced line (in the draft's order)."""
    free = list(replaced)
    out: list[str] = []
    for n in range(len(proposal.bullets)):
        sources = proposal.line_sources[n] if n < len(proposal.line_sources) else []
        chosen = next((i for i in sources if i in free), None) or free[0]
        free.remove(chosen)
        out.append(chosen)
    return out


def applied(
    cv: MasterCV, proposal: CompressionProposal, keep: Collection[str] = ()
) -> tuple[MasterCV, CompressionProposal]:
    """`cv` with a compression applied, and the proposal marked applied with what it replaced
    (for "Restore my lines") and, for a shorter role, the draft ids of its lines."""
    out = apply_compression(cv, proposal, keep)
    update: dict[str, object] = {"status": "applied"}
    if proposal.kind == "shorter_role":
        old = next(r for r in cv.experience if r.id == proposal.role_ids[0])
        new = next(r for r in out.experience if r.id == proposal.role_ids[0])
        gone = [b for b in old.bullets if b.id in proposal.source_ids and b.id not in keep]
        update |= {
            "before": [b.text for b in gone],
            "before_ids": [b.id for b in gone],
            "line_ids": [b.id for b in new.bullets[: len(proposal.bullets)]],
        }
    elif proposal.kind == "trim_skills":
        group = next(g for g in cv.skills if g.category == proposal.category)
        update["before"] = list(group.items)
    return out, proposal.model_copy(update=update)


def restored(cv: MasterCV, proposal: CompressionProposal, source: MasterCV) -> MasterCV:
    """`cv` with an applied shorter role or skills line taken back: the lines as they were
    (their skill tags from `source`, the CV the draft came from); lines added since stay."""
    out = cv.model_copy(deep=True)
    if proposal.kind == "trim_skills":
        group = next((g for g in out.skills if g.category == proposal.category), None)
        if group is None:
            out.skills.append(SkillGroup(category=proposal.category, items=proposal.before))
        else:
            group.items = list(dict.fromkeys([*proposal.before, *group.items]))
        return MasterCV.model_validate(out.model_dump())
    role = next((r for r in out.experience if r.id == proposal.role_ids[0]), None)
    if role is None or not proposal.before_ids:
        raise ValueError("This role's lines can no longer be restored in this draft")
    tags = source.bullet_index()
    back = [
        tags[i].model_copy(update={"text": text}) if i in tags else Bullet(id=i, text=text)
        for i, text in zip(proposal.before_ids, proposal.before, strict=False)
    ]
    taken = {*proposal.line_ids, *proposal.before_ids}
    rest = [b for b in role.bullets if b.id not in taken]  # lines added since stay
    role.bullets = back + rest
    return MasterCV.model_validate(out.model_dump())


def covering_line(compressions: Sequence[CompressionProposal], line_id: str) -> str | None:
    """The draft line of an applied shorter version that now carries `line_id` (a line it
    replaced): the line drawing on it, else the version's last line. None when `line_id` was
    not replaced."""
    for c in compressions:
        if c.kind != "shorter_role" or c.status != "applied" or line_id not in c.before_ids:
            continue
        for n, sources in enumerate(c.line_sources):
            if line_id in sources and n < len(c.line_ids):
                return c.line_ids[n]
        return c.line_ids[-1] if c.line_ids else None
    return None


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

MAX_ROLE_SKILLS = 8  # a few skills under the key strengths (user's decision 2026-10-21)


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
    cover = covering_line(out.compressions, item.anchor_id or "")  # a shorter version's line
    anchor = cover or item.anchor_id
    index = next((i for i, b in enumerate(role.bullets) if b.id == anchor), None)
    if item.kind == "enrichment" or (item.kind == "rewording" and cover is None):
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
