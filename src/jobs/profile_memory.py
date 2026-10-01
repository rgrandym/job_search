"""Profile summaries: built by the orchestrator, remembered per (CV, role family).

The same CV searched for the same kind of role reuses the same summary, so screening is
consistent across searches and costs one LLM call per role family, not one per search.
Stored in `data/profile_summaries.json` (git-ignored).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from src.core.llm_provider import LLMProvider
from src.cv.models import MasterCV, TailoredCV
from src.cv.tailor import cv_to_text
from src.jobs.models import ProfileSummary, SearchQuery
from src.tools.search_tools import title_core

SUMMARY_SYSTEM = """You are the orchestrator of a job-search system. Write an accurate, \
evidence-based summary of the candidate that a screening agent will use to accept or reject \
job postings. Precision matters more than flattery.

Rules:
- Use only facts in the CV (and the search intent, if given). Never invent skills or experience.
- key_skills.level: "expert" = used deeply/recently with results; "proficient" = solid \
practical use; "familiar" = mentioned or light use. Cite the evidence briefly.
- target_roles: titles this person is credibly competitive for now, including adjacent \
titles a recruiter would consider equivalent (e.g. "ML Engineer" ~ "Applied Scientist").
- stretch_roles: plausible but would need a strong pitch.
- not_a_fit: role types that superficially match keywords but are wrong for this person \
(different discipline, wrong level, wrong focus). Give a reason for each.
- If the search intent names role titles, orient the summary towards that role family \
without distorting the facts."""


class _Record(BaseModel):
    key: str
    cv_fingerprint: str
    role_family: str
    created_at: str
    summary: ProfileSummary


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

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, _Record] = {}
        if path.exists():
            items = TypeAdapter(list[_Record]).validate_json(path.read_bytes())
            self._records = {r.key: r for r in items}

    @staticmethod
    def key(cv_fp: str, family: str) -> str:
        return f"{cv_fp}:{family}"

    def get(self, cv_fp: str, family: str) -> ProfileSummary | None:
        rec = self._records.get(self.key(cv_fp, family))
        return rec.summary if rec else None

    def put(self, cv_fp: str, family: str, summary: ProfileSummary) -> None:
        key = self.key(cv_fp, family)
        self._records[key] = _Record(
            key=key,
            cv_fingerprint=cv_fp,
            role_family=family,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            summary=summary,
        )
        self._save()

    def forget_cv(self, cv_fp: str) -> None:
        self._records = {k: r for k, r in self._records.items() if r.cv_fingerprint != cv_fp}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [r.model_dump(mode="json") for r in self._records.values()]
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def summarize_profile(
    cv: MasterCV | TailoredCV | None,
    query: SearchQuery | None,
    llm: LLMProvider,
    memory: ProfileMemory,
    refresh: bool = False,
) -> tuple[ProfileSummary, bool]:
    """Return (summary, from_memory). Builds and stores a new one on a miss or `refresh`."""
    fp, family = cv_fingerprint(cv), role_family(query)
    if not refresh and (cached := memory.get(fp, family)) is not None:
        return cached, True
    intent = ""
    if query is not None:
        intent = (
            f"<search_intent>\ntitles: {', '.join(query.titles) or 'any'}\n"
            f"keywords: {', '.join(query.keywords) or 'none'}\n</search_intent>\n\n"
        )
    if cv is None:
        body = (
            "No CV provided. Build the summary from the search intent only; leave "
            "key_skills empty and years_experience 0."
        )
    else:
        master = cv.cv if isinstance(cv, TailoredCV) else cv
        body = f"<cv>\n{cv_to_text(master)}\n</cv>"
    summary = llm.generate(system=SUMMARY_SYSTEM, prompt=intent + body, output_model=ProfileSummary)
    memory.put(fp, family, summary)
    return summary, False
