"""Profile summaries: built by the profile model (the quality model unless one is set),
remembered per (CV, role family).

The same CV searched for the same kind of role reuses the same summary, so screening is
consistent across searches and costs one LLM call per role family, not one per search.
Summaries belong to a CV's identity (its library id), not its content: editing the CV keeps
them, flagged as built from an older version, until the user asks to update them. A different
CV gets its own summaries. Stored in `data/profile_summaries.json` (git-ignored).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from src.core.llm_provider import LLMProvider
from src.cv.models import MasterCV, TailoredCV
from src.jobs.models import ProfileSummary, RoleFamily, SearchIntent, SearchQuery
from src.tools.search_tools import board_terms, role_words, shares_role_words, title_core

SUMMARY_SYSTEM = """You are the profile analyst of a job-search system. Read the candidate's \
whole CV and write an accurate, evidence-based profile. A screening agent scores live \
vacancies against it and its role families decide which searches run, so it must be complete \
as well as precise: a capability left out is a search never run.

Inputs:
- <source_document>: the CV exactly as the candidate wrote it, one [src-N] id per line. It is \
the authority, and may hold sections the extract lacks: publications, patents, grants, talks, \
awards, collaborations, teaching, consulting, committees.
- <cv>: a structured extract with dates, role lengths and ids ([role-id], [bullet-id]). Use \
it for dates and seniority. Where it omits or disagrees with the source document, the source \
document wins.

Work in this order:
1. capabilities: inventory every distinct capability the document evidences, each with brief \
evidence. Cover technical and scientific depth, methods and platforms, delivery (programmes, \
budgets, timelines), line and matrix leadership, external work (clients, partners, CROs and \
CDMOs, vendors, academic collaborators, investors, regulators), commercial contact (customer \
projects, proposals, pitches, licensing, alliances, business or revenue the technical work \
created), communication (publications, talks, training, writing) and strategy.
2. publications: summarise the publication record, plus patents, grants, invited talks, \
reviewing and awards: counts, lead-author share, span, themes, the outputs most relevant to \
the target roles, and what it signals to employers (recognised expertise in a field, \
scientific writing, peer and KOL network, industry collaborations, translational impact). \
Take counts from the document; write "about" when you estimate. null when there are none.
3. Only then derive roles, from the capabilities rather than from past job titles alone.

Rules:
- Every claim must trace to the document. Never invent skills, experience, employers, \
numbers or qualifications; mark anything inferred with "(inferred)".
- Direct vs transferable: core_expertise and key_skills are direct experience; \
transferable_strengths is experience that carries into other functions. Never let the second \
stand in for the first, and never drop it either: give 6-12 specific transferable strengths, \
each naming its evidence.
- key_skills.level: "expert" = used deeply/recently with results; "proficient" = solid \
practical use; "familiar" = mentioned or light use. Cite the evidence briefly.
- seniority and years_experience: current level, job family and total relevant years.
- domains: technical domains and sectors/organisation types (pharma, biotech, CRO, CDMO, \
academia, services, ...). leadership: line, matrix and external leadership with team size, \
budget and decision scope. qualifications: degrees, PhD, registrations, certifications. \
achievements: the 3-6 strongest results, quantified where the document quantifies them.
- target_roles: 3-5 obvious titles plus 3-6 credible adjacent ones, written as employers \
advertise them (short, e.g. "Principal Scientist", "Head of Cell Biology"), including \
equivalent titles a recruiter would consider. Do not force the candidate into one category.
- stretch_roles: plausible but would need a strong pitch.
- not_a_fit: role types that superficially match keywords but are wrong for this person \
(different discipline, wrong level, wrong focus). Give a reason for each.
- search_keywords: short terms that find these roles on job boards.
- If the search intent names role titles, orient the summary towards that role family \
without distorting the facts.
- role_families: the roles to search, grouped. 2-4 "core" families (the work they do now), \
0-2 "progression" families (the next level in the same function) and 0-4 "adjacent" \
families (a different function the capabilities credibly carry into). Look for every route \
a recruiter would accept where the evidence exists, for example: a technical expert who \
worked with industry clients, partners or customers, scoped projects or grew collaborations \
or business -> business development, alliance or partnership management, technical or \
scientific sales, field application or solutions science; a scientific leader with a \
publication record and networks -> scientific consulting, venture or investment due \
diligence, technology transfer and licensing, medical or scientific affairs; programme and \
portfolio delivery -> programme or portfolio management, operations, strategy. Direct \
experience of the new function may be missing: that is the `gap`, not a reason to leave the \
family out. Each family: 2-4 titles as employers advertise them, 1-3 short domain terms, \
`evidence` = ids in square brackets showing the transferable work ([role-id], [bullet-id] \
or [src-N] lines, e.g. publications; at least one for progression, two for adjacent), the \
main `gap`, and a one-line `rationale` a hiring manager would accept. Propose an adjacent \
family only if a hiring manager would genuinely shortlist this candidate; never one that \
needs a licence, registration or degree they lack.
- career_intent: the candidate's own goals. For every target area, return one adjacent \
family with requested=true under the same evidence rules; if the CV offers no credible \
route in, still return it, with the evidence you found and a gap saying what is missing. Use \
the intent to orient families and wording, never to change the facts. Areas they want to \
avoid belong in not_a_fit.
- summary: 4-6 sentences a recruiter would find compelling and accurate: who they are, depth \
and breadth, the publication record if any, and the directions they credibly fit."""

# Evidence a family needs to be searched, by tier (ids that exist in the CV).
MIN_EVIDENCE = {"core": 0, "progression": 1, "adjacent": 2}
MAX_FAMILIES = {"core": 4, "progression": 2, "adjacent": 4}  # model-suggested, per tier
SOURCE_ID = re.compile(r"src-\d+")
SOURCE_CHARS = 60_000  # the whole of any real CV; guards against pasted books


def numbered_source(text: str) -> tuple[str, set[str]]:
    """The original CV document as the summary reads it: one [src-N] id per non-empty line, so
    families can cite content the structured extract has no field for (e.g. publications)."""
    lines = [line.strip() for line in text[:SOURCE_CHARS].splitlines() if line.strip()]
    ids = [f"src-{n}" for n in range(1, len(lines) + 1)]
    body = "\n".join(f"[{i}] {line}" for i, line in zip(ids, lines, strict=True))
    return body, set(ids)


def profile_cv_text(cv: MasterCV) -> str:
    """The CV as the summary reads it: dated roles with their length (seniority and years
    depend on them), bracketed ids (role families cite them as evidence), education,
    certifications, languages and location."""
    b = cv.basics
    lines = [f"{b.name} · {b.headline or ''} · {b.location or 'location not given'}"]
    if b.summary:
        lines.append(f"Summary: {b.summary}")
    for e in sorted(cv.experience, key=lambda e: e.start, reverse=True):
        years = round(e.months() / 12, 1)
        lines.append(
            f"\n[{e.id}] {e.title} at {e.company}{f', {e.location}' if e.location else ''} "
            f"({e.start} to {e.end or 'present'}, {years} years)"
        )
        lines += [f"  [{bl.id}] {bl.text}" for bl in e.bullets]
    for p in cv.projects:
        lines.append(f"\n[{p.id}] Project: {p.name}: {p.description} ({', '.join(p.skills)})")
    for ed in cv.education:
        dates = f" ({ed.start or '?'} to {ed.end or '?'})" if ed.start or ed.end else ""
        lines.append(f"Education: {ed.degree} {ed.field or ''}, {ed.institution}{dates}")
        lines += [f"  {d}" for d in ed.details]
    lines += [f"Certification: {c.name} {c.issuer or ''} {c.year or ''}" for c in cv.certifications]
    lines += [f"Skills ({g.category}): {', '.join(g.items)}" for g in cv.skills]
    if cv.languages:
        lines.append(f"Languages: {', '.join(cv.languages)}")
    return "\n".join(line.rstrip() for line in lines)


def intent_text(intent: SearchIntent | None) -> str:
    """The parts of the career intent that steer summaries and screening, as prompt text."""
    if intent is None or intent.is_empty():
        return ""
    data = intent.model_dump(exclude={"updated_at", "languages", "eligibility"})
    lines = [f"{k}: {v if isinstance(v, str) else '; '.join(v)}" for k, v in data.items() if v]
    return "<career_intent>\n" + "\n".join(lines) + "\n</career_intent>\n\n" if lines else ""


def intent_fingerprint(intent: SearchIntent | None) -> str:
    """Hash of what in the intent shapes a summary ("" when nothing is stated)."""
    text = intent_text(intent)
    return hashlib.sha256(text.encode()).hexdigest()[:16] if text else ""


def _cv_ids(cv: MasterCV) -> set[str]:
    ids = {e.id for e in cv.experience} | {p.id for p in cv.projects}
    return ids | {bl.id for e in cv.experience for bl in e.bullets}


def _family_problem(fam: RoleFamily) -> str | None:
    """Only what code can verify. Whether a role fits (e.g. a clinical role for a lab
    scientist) is never decided from title words here: the job_matcher judges each posting's
    requirements against the profile, where `not_a_fit` informs its verdict."""
    if not fam.titles:
        return "no advertised titles"
    if len(fam.evidence) < MIN_EVIDENCE[fam.tier]:
        need = MIN_EVIDENCE[fam.tier]
        return f"needs {need} piece(s) of CV evidence, found {len(fam.evidence)}"
    if fam.tier != "core" and not fam.gap.strip():
        return "no gap stated: an honest transfer always has one"
    return None


def check_families(
    summary: ProfileSummary,
    cv: MasterCV | None,
    intent: SearchIntent | None = None,
    source_ids: set[str] | None = None,
) -> ProfileSummary:
    """Code checks what it can verify before a family is searched: evidence ids must exist in
    the CV (two for adjacent, one for progression), and non-core families state a gap. Fit itself
    is the job_matcher's call, per posting. Failures are kept with
    `rejected` so the user sees why. Without a CV only core families can be searched.
    Idempotent: re-running it on a stored summary with the same CV and intent changes nothing.
    `source_ids` are the original document's [src-N] lines when the summary is built; later
    re-checks (without the document) keep the src ids that were verified then."""
    ids = _cv_ids(cv) if cv is not None else set()

    def known(i: str) -> bool:
        if SOURCE_ID.fullmatch(i):
            return cv is not None and (source_ids is None or i in source_ids)
        return i in ids

    asked = bool(intent and intent.target_areas)
    counts = dict.fromkeys(MAX_FAMILIES, 0)
    out: list[RoleFamily] = []
    for fam in summary.role_families:
        requested = fam.requested and asked
        evidence = [i.strip("[] ") for i in fam.evidence if known(i.strip("[] "))]
        fam = fam.model_copy(update={"evidence": evidence, "requested": requested})
        problem = _family_problem(fam)
        if cv is None and fam.tier != "core":
            problem = "no CV to show the experience transfers"
        if problem is None and not requested:
            counts[fam.tier] += 1
            if counts[fam.tier] > MAX_FAMILIES[fam.tier]:
                problem = f"more than {MAX_FAMILIES[fam.tier]} {fam.tier} families proposed"
        out.append(fam.model_copy(update={"rejected": problem}))
    return summary.model_copy(update={"role_families": out})


class ProfileRecord(BaseModel):
    """One remembered summary: which CV, CV version and role family it was built for."""

    key: str
    cv_fingerprint: str  # content of the CV when the summary was (re)built
    cv_id: str | None = None  # owning CV; None on records stored before CV ids were used
    cv_name: str | None = None  # the CV's file name, as shown to the user
    role_family: str
    created_at: str
    summary: ProfileSummary
    edited: bool = False  # the user changed it by hand; it is never rebuilt silently
    updated_at: str | None = None
    intent_fingerprint: str = ""  # career intent the summary was (re)built with


def profile_markdown(rec: ProfileRecord) -> str:
    """A stored profile as a readable document (the copy kept in output/profiles/)."""
    s = rec.summary
    family = "General profile" if rec.role_family == "any" else rec.role_family
    out = [f"# {s.headline}", "", f"*{family} · from {rec.cv_name or rec.cv_id or 'CV'}*", ""]
    out += [f"**{s.seniority}** · {s.years_experience:g} years of experience", "", s.summary]

    def section(title: str, items: list[str]) -> None:
        if items:
            out.extend(["", f"## {title}", *(f"- {item}" for item in items)])

    if (p := s.publications) is not None:
        counts = [f"{p.count} publications" if p.count else "", p.years]
        if p.lead_author:
            counts.insert(1, f"{p.lead_author} as first, last or corresponding author")
        out += ["", "## Publications", " · ".join(c for c in counts if c), "", p.summary]
        for title, items in [("Themes", p.themes), ("Most relevant", p.notable)]:
            section(title, items)
        section("Other outputs", p.other_outputs)
        section("What the record shows employers", p.signals)
    families = [
        f"**{f.name}** ({f.tier}): {', '.join(f.titles)}"
        + (f". {f.rationale}" if f.rationale else "")
        + (f" Gap: {f.gap}" if f.gap else "")
        + (f" *Not searched: {f.rejected}*" if f.rejected else "")
        for f in s.role_families
    ]
    section("Role families", families)
    section("Key skills", [f"{k.skill} ({k.level}): {k.evidence}" for k in s.key_skills])
    for title, items in [
        ("Core expertise", s.core_expertise),
        ("Capabilities", s.capabilities),
        ("Transferable strengths", s.transferable_strengths),
        ("Domains and sectors", s.domains),
        ("Leadership", s.leadership),
        ("Qualifications", s.qualifications),
        ("Key achievements", s.achievements),
        ("Target roles", s.target_roles),
        ("Stretch roles", s.stretch_roles),
        ("Not a fit", s.not_a_fit),
        ("Search keywords", s.search_keywords),
    ]:
        section(title, items)
    return "\n".join(out).strip() + "\n"


def profile_filename(rec: ProfileRecord) -> str:
    """`<CV name>_<role family>.md`, safe for any file system."""
    cv = Path(rec.cv_name or rec.cv_id or rec.cv_fingerprint).stem
    family = "general" if rec.role_family == "any" else rec.role_family
    stem = re.sub(r"[^A-Za-z0-9]+", "_", f"{cv}_{family}").strip("_")
    return f"{stem[:120] or 'profile'}.md"


def cv_fingerprint(cv: MasterCV | TailoredCV | None) -> str:
    """Stable hash of CV content ("no-cv" for filter-only searches)."""
    if cv is None:
        return "no-cv"
    master = cv.cv if isinstance(cv, TailoredCV) else cv
    payload = master.model_dump_json(exclude={"preferences"})
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def role_family(query: SearchQuery | None) -> str:
    """Normalised key for "the same type of search": the set of title core words."""
    if query is None or not query.titles:
        return "any"
    words = sorted(set().union(*(title_core(t) for t in query.titles)))
    return " ".join(words) or "any"


class ProfileMemory:
    """JSON-file store of profile summaries."""

    def __init__(self, path: Path, export_dir: Path | None = None) -> None:
        self.path = path
        self.export_dir = export_dir  # readable copies, one Markdown file per profile
        self._records: dict[str, ProfileRecord] = {}
        if path.exists():
            items = TypeAdapter(list[ProfileRecord]).validate_json(path.read_bytes())
            self._records = {r.key: r for r in items}
            self.export()  # profiles stored before the readable copies existed

    @staticmethod
    def key(owner: str, family: str) -> str:
        """`owner` is the CV id (or the content fingerprint when the CV has no id)."""
        return f"{owner}:{family}"

    def get(self, owner: str, family: str) -> ProfileSummary | None:
        rec = self._records.get(self.key(owner, family))
        return rec.summary if rec else None

    def put(
        self,
        owner: str,
        family: str,
        summary: ProfileSummary,
        cv_fp: str = "",
        intent_fp: str = "",
    ) -> None:
        """Store a freshly built summary (replacing any earlier one, edits included)."""
        key = self.key(owner, family)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        old = self._records.get(key)
        self._records[key] = ProfileRecord(
            key=key,
            cv_fingerprint=cv_fp or owner,
            cv_id=owner if cv_fp and cv_fp != owner else None,
            cv_name=old.cv_name if old else None,
            role_family=family,
            created_at=old.created_at if old else now,
            updated_at=now if old else None,
            summary=summary,
            intent_fingerprint=intent_fp,
        )
        self._save()

    def records(self, owner: str) -> list[ProfileRecord]:
        """Stored summaries for one CV, oldest first."""
        found = [r for r in self._records.values() if r.key.startswith(f"{owner}:")]
        return sorted(found, key=lambda r: r.created_at)

    def adopt(self, cv_id: str, cv_fp: str) -> None:
        """Move summaries stored under this CV's content fingerprint to its id (migration)."""
        records = self._records.values()
        legacy = [r for r in records if r.cv_id is None and r.cv_fingerprint == cv_fp]
        for rec in legacy:
            key = self.key(cv_id, rec.role_family)
            del self._records[rec.key]
            if key not in self._records:
                self._records[key] = rec.model_copy(update={"key": key, "cv_id": cv_id})
        if legacy:
            self._save()

    def label(self, owner: str, cv_name: str) -> None:
        """Record the CV's name on its summaries (new ones, and ones stored before names)."""
        changed = False
        for rec in self.records(owner):
            if rec.cv_name != cv_name:
                self._records[rec.key] = rec.model_copy(update={"cv_name": cv_name})
                changed = True
        if changed:
            self._save()

    def record(self, key: str) -> ProfileRecord | None:
        return self._records.get(key)

    def update(self, key: str, summary: ProfileSummary) -> ProfileRecord:
        """Replace a stored summary with the user's edited version."""
        rec = self._records[key]
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._records[key] = rec.model_copy(
            update={"summary": summary, "edited": True, "updated_at": now}
        )
        self._save()
        return self._records[key]

    def recheck(
        self, key: str, cv: MasterCV | None, intent: SearchIntent | None
    ) -> ProfileSummary | None:
        """The stored summary with `check_families` re-run against the current CV and intent
        (no LLM call), saved when the result differs. Profiles the user edited are left as
        they are: their families are the user's decision."""
        rec = self._records.get(key)
        if rec is None:
            return None
        if rec.edited:
            return rec.summary
        checked = check_families(rec.summary, cv, intent)
        if checked != rec.summary:
            self._records[key] = rec.model_copy(update={"summary": checked})
            self._save()
        return checked

    def delete(self, key: str) -> bool:
        removed = self._records.pop(key, None) is not None
        if removed:
            self._save()
        return removed

    def forget_cv(self, cv_fp: str) -> None:
        self._records = {k: r for k, r in self._records.items() if r.cv_fingerprint != cv_fp}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [r.model_dump(mode="json") for r in self._records.values()]
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.export()

    def export(self) -> None:
        """Write each profile to `export_dir` as Markdown; remove copies of deleted ones."""
        if self.export_dir is None:
            return
        self.export_dir.mkdir(parents=True, exist_ok=True)
        wanted: dict[str, str] = {}
        for rec in self._records.values():
            name = profile_filename(rec)
            if name in wanted:  # same CV name and family under another CV id
                name = f"{name[:-3]}_{hashlib.sha256(rec.key.encode()).hexdigest()[:6]}.md"
            wanted[name] = profile_markdown(rec)
        for stale in self.export_dir.glob("*.md"):
            if stale.name not in wanted:
                stale.unlink()
        for name, text in wanted.items():
            target = self.export_dir / name
            if not target.is_file() or target.read_text(encoding="utf-8") != text:
                target.write_text(text, encoding="utf-8")


def summarize_profile(
    cv: MasterCV | TailoredCV | None,
    query: SearchQuery | None,
    llm: LLMProvider,
    memory: ProfileMemory,
    refresh: bool = False,
    cv_id: str | None = None,
    intent: SearchIntent | None = None,
    source_text: str | None = None,
) -> tuple[ProfileSummary, bool]:
    """Return (summary, from_memory). Builds and stores a new one on a miss or `refresh`.

    `source_text` is the original CV document's text: the summary reads it in full (with the
    structured extract for dates and ids), so nothing the extract omits is lost.

    With `cv_id`, the summary belongs to that CV whatever its current content, so CV edits
    never trigger a silent rebuild; only `refresh` does. The career `intent` orients role
    families (its target areas become requested adjacent families), checked by
    `check_families`.
    """
    fp, family = cv_fingerprint(cv), role_family(query)
    owner = cv_id if cv is not None and cv_id else fp
    if cv_id and cv is not None:
        memory.adopt(cv_id, fp)
    master = cv.cv if isinstance(cv, TailoredCV) else cv
    if not refresh and (cached := memory.recheck(memory.key(owner, family), master, intent)):
        return cached, True
    search = ""
    if query is not None:
        search = (
            f"<search_intent>\ntitles: {', '.join(query.titles) or 'any'}\n"
            f"keywords: {', '.join(query.keywords) or 'none'}\n</search_intent>\n\n"
        )
    if master is None:
        body = (
            "No CV provided. Build the summary from the search intent only; leave "
            "key_skills empty and years_experience 0."
        )
    else:
        body = f"<cv>\n{profile_cv_text(master)}\n</cv>"
    source_ids: set[str] | None = None
    if master is not None and source_text and source_text.strip():
        numbered, source_ids = numbered_source(source_text)
        body = f"<source_document>\n{numbered}\n</source_document>\n\n" + body
    prompt = search + intent_text(intent) + body
    summary = llm.generate(system=SUMMARY_SYSTEM, prompt=prompt, output_model=ProfileSummary)
    summary = check_families(summary, master, intent, source_ids or set())
    memory.put(owner, family, summary, fp, intent_fingerprint(intent))
    return summary, False


# Share of the job-board searches each tier gets (rescaled over the tiers being searched).
TIER_BUDGET = {"core": 0.6, "progression": 0.2, "adjacent": 0.2}


def searched_families(summary: ProfileSummary, widen: bool) -> list[RoleFamily]:
    """Families a search runs: every realistic core and progression family, adjacent ones the
    user asked for, and (with `widen`, for a tough market) the other adjacent ones."""
    return [
        f
        for f in summary.role_families
        if f.rejected is None and (f.tier != "adjacent" or f.requested or widen)
    ]


def family_terms(summary: ProfileSummary, widen: bool, limit: int) -> list[tuple[str, str]]:
    """Job-board search terms with the family each one serves: (term, family name).

    Each tier gets its budget share (core 60%, progression 20%, adjacent 20%, rescaled over
    the tiers searched) but every family keeps at least its first title; within a tier the
    families take turns, so a limit never drops a whole family. Unused budget goes to the
    remaining terms in tier order."""
    families = searched_families(summary, widen)
    variants = {f.name: board_terms(f.titles, f.domain_terms, limit) for f in families}
    tiers = {t: [f for f in families if f.tier == t] for t in TIER_BUDGET}
    total = sum(TIER_BUDGET[t] for t, fs in tiers.items() if fs) or 1.0
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def take(fams: list[RoleFamily], quota: int) -> None:
        depth = max((len(variants[f.name]) for f in fams), default=0)
        for i in range(depth):
            for f in fams:
                if quota <= 0 or len(out) >= limit:
                    return
                if i < len(variants[f.name]) and variants[f.name][i].lower() not in seen:
                    seen.add(variants[f.name][i].lower())
                    out.append((variants[f.name][i], f.name))
                    quota -= 1

    for tier, fams in tiers.items():
        take(fams, max(len(fams), round(limit * TIER_BUDGET[tier] / total)))
    for fams in tiers.values():
        take(fams, limit)
    return out[:limit]


# The job noun says what kind of job, not which field: in life sciences every family is
# "scientist". Families are told apart by the other (domain) words of a title.
_ROLE_NOUNS = {
    "scientist", "engineer", "analyst", "consultant", "technician", "researcher", "advisor",
    "adviser", "expert", "professional", "executive", "partner", "fellow",
}  # fmt: skip


def family_of(title: str, families: list[RoleFamily]) -> str | None:
    """The searched family a job title belongs to: the one sharing the most distinctive domain
    words with it (from the family's titles and domain terms; a word fewer families use counts
    more). Job nouns ("scientist") do not tell families apart. A title made only of words one
    family has ("Principal Scientist") belongs to it; a title whose domain words no family has
    ("Implementation Scientist") is left unattributed, unless a family made only of generic
    words ("Business Development Manager") matches it whole. Ties go to core first."""
    order = sorted(families, key=lambda f: list(TIER_BUDGET).index(f.tier))
    vocab = {
        f.name: set().union(*(role_words(t) for t in [*f.titles, *f.domain_terms]))
        for f in order
    }
    spread = Counter(w for words in vocab.values() for w in words)
    words = role_words(title)
    domain = words - _ROLE_NOUNS
    best, best_score = None, 0.0
    for f in order:
        score = sum(1 / spread[w] for w in domain & vocab[f.name])
        if score > best_score:
            best, best_score = f.name, score
    if best is None and words:
        best = next((f.name for f in order if words <= vocab[f.name]), None)
    if best is not None:
        return best
    generic = [f for f in order if not vocab[f.name]]
    return next((f.name for f in generic if shares_role_words(title, f.titles)), None)


def pivot_titles(summary: ProfileSummary | None) -> list[str]:
    """Titles of the areas the user asked to move into (realistic requested families)."""
    if summary is None:
        return []
    return [
        t for f in summary.role_families if f.requested and f.rejected is None for t in f.titles
    ]
