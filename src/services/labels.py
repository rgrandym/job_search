"""Your own call on jobs ("would apply" / "maybe" / "no"): a labelled set to measure the models.

Stored in `data/job_labels.json` (git-ignored, personal data). A label never changes a search:
applied / N/A in `services.tracker` decide what searches set aside; labels only record what
you would have chosen, so the job_matcher's verdicts can be checked against it.

One label per posting: the same job on another board or in a later search (same id, same
link, or the same title at the same employer, as the tracker matches roles) shares the label.
Each label keeps a snapshot from when it was first given (posting, search constraints,
threshold, profile summary), so it stays usable after the search leaves the history and
`model_compare` can re-screen it later, plus every verdict a model setup gave the posting,
keyed by that setup's models line: searches add theirs (`attach_verdicts`) and past searches
in the history are read once. Setups are then compared on the jobs both judged.

A label's note is the job's note in the tracker (the card's note box): copied when you label,
and brought up to date each time the review is read, so one note per job serves both.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.config import Settings
from src.jobs.models import JobPosting, JobVerdict, MatchReport, ProfileSummary, SearchQuery
from src.services import history, tracker
from src.services.workspace import Workspace

UserLabel = Literal["yes", "maybe", "no"]

# Enough to compare model setups: with 15 "yes" each missed good job moves recall by ~7%,
# and 60 labels in all keep borderline cases in the mix. Below this, read the numbers as hints.
TARGET_TOTAL = 60
TARGET_EACH = 15  # of "yes" and of "no"
MAX_LISTED = 8  # jobs listed per setup or comparison
MIN_SHARED = 5  # yes/no labels two setups must both have judged to be compared head to head
UNKNOWN_MODELS = "unknown models"
SCREENING_DEFAULT = Settings.model_fields["score_threshold"].default


class LabelledJob(BaseModel):
    """One label: the snapshot needed to judge the posting again, and every setup's verdict."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(description="The id it was first labelled under")
    ids: list[str] = Field(default_factory=list, description="Every id seen for this posting")
    label: UserLabel
    note: str = Field("", description="The job's tracker note (kept in sync by the review)")
    labelled_at: str
    job: JobPosting
    family: str | None = None
    verdicts: dict[str, JobVerdict] = Field(
        default_factory=dict, description="Models line -> the verdict that setup gave"
    )
    query: SearchQuery
    threshold: float
    profile_key: str | None = Field(None, description="Key into LabelStore.profiles")

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: Any) -> Any:
        """Labels saved with a single `verdict` / `models` keep it under that setup."""
        if isinstance(data, dict) and "verdict" in data:
            data = dict(data)
            verdict, models = data.pop("verdict"), data.pop("models", None)
            if verdict is not None:
                data.setdefault("verdicts", {})[models or UNKNOWN_MODELS] = verdict
        if isinstance(data, dict) and not data.get("ids") and data.get("job_id"):
            data = {**data, "ids": [data["job_id"]]}
        if isinstance(data, dict) and data.get("threshold") == 0:
            # Saved before reports kept the match threshold: smart searches stored the
            # pre-filter's cut (0) instead; every one of them used the default.
            data = {**data, "threshold": SCREENING_DEFAULT}
        return data


class LabelStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 2
    profiles: dict[str, ProfileSummary] = Field(default_factory=dict)
    labels: list[LabelledJob] = Field(default_factory=list)
    searches_read: list[str] = Field(
        default_factory=list, description="History entries already read for verdicts"
    )


class SetupAgreement(BaseModel):
    """How one model setup's verdicts compare with your labels ("maybe" is left out)."""

    models: str
    yes: int = Field(description="Your 'yes' labels this setup judged")
    no: int = Field(description="Your 'no' labels this setup judged")
    kept_yes: int = Field(description="'yes' jobs it matched")
    passed_no: int = Field(description="'no' jobs it matched anyway")
    agreement: float | None = Field(None, description="Share of yes/no with the same decision")
    ranking: float | None = Field(
        None, description="Share of (yes, no) pairs where the 'yes' job scored higher"
    )
    misses: list[str] = Field(default_factory=list, description="'yes' jobs it did not match")
    false_accepts: list[str] = Field(default_factory=list, description="'no' jobs it matched")


class HeadToHead(BaseModel):
    """Two setups on the same labelled jobs: the fair comparison."""

    first: SetupAgreement
    second: SetupAgreement
    shared: int = Field(description="Your yes/no labels both setups judged")
    split: list[str] = Field(
        default_factory=list, description="Jobs where they decided differently"
    )


class LabelReview(BaseModel):
    total: int
    yes: int
    maybe: int
    no: int
    target_total: int = TARGET_TOTAL
    target_each: int = TARGET_EACH
    ready: bool = Field(description="Enough labels to compare model setups")
    setups: list[SetupAgreement]
    head_to_head: list[HeadToHead] = Field(default_factory=list)
    by_job: dict[str, UserLabel] = Field(description="Job id -> your label, for the job cards")


def _path(ws: Workspace) -> Path:
    return ws.settings.data_dir / "job_labels.json"


def load(ws: Workspace) -> LabelStore:
    """The stored labels (empty when none or unreadable)."""
    try:
        return LabelStore.model_validate_json(_path(ws).read_bytes())
    except (OSError, ValueError):
        return LabelStore()


def _save(ws: Workspace, store: LabelStore) -> None:
    store.version = LabelStore.model_fields["version"].default  # upgraded labels are saved as such
    used = {x.profile_key for x in store.labels}
    store.profiles = {k: v for k, v in store.profiles.items() if k in used}
    path = _path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store.model_dump(mode="json"), indent=1), encoding="utf-8")


def _profile_key(summary: ProfileSummary) -> str:
    return hashlib.sha256(summary.model_dump_json().encode()).hexdigest()[:16]


# ---------------------------------------------------------------- one label per posting


def _url(url: str | None) -> str:
    return (url or "").split("?")[0].rstrip("/")


class _Index:
    """Finds a posting's label by id, link, or the same title at the same employer."""

    def __init__(self, store: LabelStore) -> None:
        self.ids: dict[str, LabelledJob] = {}
        self.roles: dict[str, LabelledJob] = {}
        self.urls: dict[str, LabelledJob] = {}
        for x in store.labels:
            self.ids.update(dict.fromkeys(x.ids, x))
            self.roles.setdefault(tracker.role_id(x.job.title, x.job.company), x)
            if url := _url(x.job.url):
                self.urls.setdefault(url, x)

    def find(self, job: JobPosting) -> LabelledJob | None:
        return (
            self.ids.get(job.id)
            or self.roles.get(tracker.role_id(job.title, job.company))
            or self.urls.get(_url(job.url) or "\0")
        )


def _remember(
    item: LabelledJob, job_id: str, models: str | None, verdict: JobVerdict | None
) -> bool:
    """Add an id and a setup's verdict to a label (an existing verdict is kept)."""
    changed = job_id not in item.ids
    if changed:
        item.ids.append(job_id)
    if verdict is not None and (models or UNKNOWN_MODELS) not in item.verdicts:
        item.verdicts[models or UNKNOWN_MODELS] = verdict.model_copy(update={"from_memory": False})
        changed = True
    return changed


def attach_verdicts(ws: Workspace, report: MatchReport, models: str | None) -> int:
    """Add a search's verdicts to the labels of the postings it judged; returns how many."""
    store = load(ws)
    added = _attach(store, report, models)
    if added:
        _save(ws, store)
    return added


def _attach(store: LabelStore, report: MatchReport, models: str | None) -> int:
    if not store.labels or models is None:
        return 0
    index, added = _Index(store), 0
    for r in [*report.matches, *report.below_threshold]:
        item = index.find(r.job) if r.verdict else None
        if item is not None and _remember(item, r.job.id, models, r.verdict):
            added += 1
    return added


def _read_history(ws: Workspace, store: LabelStore) -> bool:
    """Verdicts from past searches not read yet (each search is read once)."""
    changed = False
    for entry_id, models, report in history.screened_reports(ws, skip=set(store.searches_read)):
        _attach(store, report, models)
        store.searches_read.append(entry_id)
        changed = True
    return changed


# ---------------------------------------------------------------- labelling


def _note(entries: dict[str, tracker.TrackedJob], job: JobPosting) -> str | None:
    """The job's tracker note, or None when the tracker has no entry for it."""
    entry = tracker.find(entries, job)
    return entry.note if entry is not None else None


def _sync_notes(ws: Workspace, store: LabelStore) -> bool:
    """Copy each labelled job's current tracker note into its label; True if any changed.
    A job the tracker no longer knows keeps the note it had."""
    entries = tracker.load(ws)
    changed = False
    for item in store.labels:
        note = _note(entries, item.job)
        if note is not None and note != item.note:
            item.note, changed = note, True
    return changed


def _snapshot(ws: Workspace, store: LabelStore, report: MatchReport, job_id: str) -> LabelledJob:
    """A new label's snapshot of the current search: posting, constraints and summary."""
    result = next(r for r in report.all_results() if r.job.id == job_id)
    key = None
    if report.summary is not None:
        key = _profile_key(report.summary)
        store.profiles.setdefault(key, report.summary)
    return LabelledJob(
        job_id=job_id,
        ids=[job_id],
        label="maybe",  # set by the caller
        note=_note(tracker.load(ws), result.job) or "",
        labelled_at=datetime.now(UTC).isoformat(timespec="seconds"),
        job=result.job,
        family=result.family,
        query=ws.last_query or SearchQuery(),
        threshold=report.threshold,
        profile_key=key,
    )


def set_label(ws: Workspace, job_id: str, label: UserLabel | None) -> bool:
    """Label a job of the current search (None removes its label). The same posting seen
    before keeps one label: its value changes, its first snapshot stays, and this search's
    verdict is added. Returns False when the job is not in the current search."""
    report = ws.last_report
    result = next((r for r in report.all_results() if r.job.id == job_id), None) if report else None
    if report is None or result is None:
        return False
    store = load(ws)
    item = _Index(store).find(result.job)
    if label is None:
        store.labels = [x for x in store.labels if x is not item]
    else:
        if item is None:
            item = _snapshot(ws, store, report, job_id)
            store.labels.insert(0, item)
        item.label = label
        item.labelled_at = datetime.now(UTC).isoformat(timespec="seconds")
        _remember(item, job_id, ws.last_models, result.verdict)
    _save(ws, store)
    return True


# ---------------------------------------------------------------- review


def _name(x: LabelledJob, verdict: JobVerdict) -> str:
    note = f" — your note: {x.note}" if x.note else ""
    return f"{x.job.title} · {x.job.company} ({verdict.fit_score}){note}"


def _ranking(yes: list[int], no: list[int]) -> float | None:
    """Share of (yes, no) pairs where the 'yes' job scored higher; a tie counts half."""
    if not yes or not no:
        return None
    wins = sum(1.0 if y > n else 0.5 if y == n else 0.0 for y in yes for n in no)
    return round(wins / (len(yes) * len(no)), 3)


def agreement(models: str, items: list[LabelledJob]) -> SetupAgreement:
    """Compare one setup's verdicts on `items` with the labels."""
    yes = [(x, x.verdicts[models]) for x in items if x.label == "yes" and models in x.verdicts]
    no = [(x, x.verdicts[models]) for x in items if x.label == "no" and models in x.verdicts]
    misses = [(x, v) for x, v in yes if not v.match]
    passed = [(x, v) for x, v in no if v.match]
    decided = len(yes) + len(no)
    return SetupAgreement(
        models=models,
        yes=len(yes),
        no=len(no),
        kept_yes=len(yes) - len(misses),
        passed_no=len(passed),
        agreement=round((decided - len(misses) - len(passed)) / decided, 3) if decided else None,
        ranking=_ranking([v.fit_score for _, v in yes], [v.fit_score for _, v in no]),
        misses=[_name(x, v) for x, v in misses][:MAX_LISTED],
        false_accepts=[_name(x, v) for x, v in passed][:MAX_LISTED],
    )


def head_to_head(a: str, b: str, labels: list[LabelledJob]) -> HeadToHead:
    """Setups `a` and `b` on the yes/no labels both judged."""
    shared = [x for x in labels if x.label != "maybe" and a in x.verdicts and b in x.verdicts]
    split = [
        f"{x.job.title} · {x.job.company} — you: {x.label}, "
        f"{x.verdicts[a].fit_score} vs {x.verdicts[b].fit_score}"
        for x in shared
        if x.verdicts[a].match != x.verdicts[b].match
    ]
    return HeadToHead(
        first=agreement(a, shared),
        second=agreement(b, shared),
        shared=len(shared),
        split=split[:MAX_LISTED],
    )


def _by_job(ws: Workspace, store: LabelStore) -> dict[str, UserLabel]:
    """Labels by job id, including current results that are a labelled posting under
    another id (another board), so their cards show the label too."""
    out = {i: x.label for x in store.labels for i in x.ids}
    if ws.last_report is not None:
        index = _Index(store)
        for r in ws.last_report.all_results():
            if r.job.id not in out and (item := index.find(r.job)) is not None:
                out[r.job.id] = item.label
    return out


def review(ws: Workspace) -> LabelReview:
    """Label counts, progress to a usable set, each setup against your labels, and pairs of
    setups on the same jobs. Tracker notes and past searches' verdicts are brought in first."""
    store = load(ws)
    if _read_history(ws, store) | _sync_notes(ws, store):
        _save(ws, store)
    labels = store.labels
    count = {k: sum(x.label == k for x in labels) for k in ("yes", "maybe", "no")}
    models = sorted({m for x in labels for m in x.verdicts})
    setups = sorted(
        (agreement(m, labels) for m in models), key=lambda s: s.yes + s.no, reverse=True
    )
    pairs = [head_to_head(a, b, labels) for a, b in combinations(models, 2)]
    return LabelReview(
        total=len(labels),
        yes=count["yes"],
        maybe=count["maybe"],
        no=count["no"],
        ready=len(labels) >= TARGET_TOTAL and min(count["yes"], count["no"]) >= TARGET_EACH,
        setups=setups,
        head_to_head=sorted(
            (p for p in pairs if p.shared >= MIN_SHARED), key=lambda p: p.shared, reverse=True
        ),
        by_job=_by_job(ws, store),
    )
