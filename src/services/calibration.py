"""Outcome review: patterns in what the user ruled out and how applications went.

Read-only and deterministic. It never changes the intent, families or scoring: it reports
patterns with counts and examples; the user decides what to change (directly, or by telling
the assistant).
Hiring outcomes are noisy, so nothing is reported below `MIN_PATTERN` cases, and the
strongest signal is the user's own reasons for ruling out roles the matcher rated highly.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from src.services import tracker
from src.services.workspace import Workspace

HIGH_FIT = 75  # a ruled-out role above this fit is a disagreement worth learning from
MIN_PATTERN = 3  # cases before anything is called a pattern
QUIET_DAYS = 14  # an application with no news after this long has gone quiet
PROGRESSED = {"interview", "final_round", "offer", "accepted"}
_STOPWORDS = {
    "a", "an", "and", "the", "to", "of", "in", "on", "for", "too", "not", "no", "is", "it",
    "role", "job", "this", "that", "with", "at", "be", "very", "much", "more", "less", "my",
    "i", "me", "would", "but", "or", "as", "than", "there", "its", "it's", "they",
}  # fmt: skip


class Pattern(BaseModel):
    """One pattern worth the user's attention, with a suggestion they may act on."""

    kind: Literal["dismissal_reason", "dismissed_family", "family_outcome"]
    label: str
    count: int
    examples: list[str] = Field(default_factory=list)
    suggestion: str


class OutcomeReview(BaseModel):
    applications: int
    by_stage: dict[str, int] = Field(default_factory=dict)
    high_fit_dismissals: int = 0
    patterns: list[Pattern] = Field(default_factory=list)
    quiet: list[str] = Field(
        default_factory=list, description="Applications with no news after QUIET_DAYS"
    )


def _words(reason: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z\-']+", reason.lower()) if w not in _STOPWORDS}


def _reason_patterns(dismissed: list[tracker.TrackedJob]) -> list[Pattern]:
    """Words that recur across the user's reasons for ruling out high-fit roles."""
    counts = Counter(w for e in dismissed for w in _words(e.reason))
    out = []
    for word, n in counts.most_common():
        if n < MIN_PATTERN:
            break
        cases = [e for e in dismissed if word in _words(e.reason)]
        out.append(
            Pattern(
                kind="dismissal_reason",
                label=word,
                count=n,
                examples=[f"{e.title} at {e.company}: {e.reason}" for e in cases[:3]],
                suggestion=f"You ruled out {n} strong matches citing '{word}'. Add it to work "
                "to avoid or a soft dealbreaker in your career intent?",
            )
        )
    return out


def _family_patterns(entries: list[tracker.TrackedJob]) -> list[Pattern]:
    out: list[Pattern] = []
    dismissed = Counter(e.family for e in entries if e.status == "na" and e.family)
    for family, n in dismissed.items():
        if family and n >= MIN_PATTERN:
            out.append(
                Pattern(
                    kind="dismissed_family",
                    label=family,
                    count=n,
                    suggestion=f"You ruled out {n} roles from '{family}'. Narrow its titles, "
                    "or drop it from your profile?",
                )
            )
    applied = [e for e in entries if e.status == "applied" and e.family]
    for family in sorted({str(e.family) for e in applied}):
        mine = [e for e in applied if e.family == family]
        heard = [e for e in mine if e.stage is not None and e.stage != "no_response"]
        if len(heard) >= MIN_PATTERN and not any(e.stage in PROGRESSED for e in heard):
            out.append(
                Pattern(
                    kind="family_outcome",
                    label=str(family),
                    count=len(heard),
                    suggestion=f"No interview yet from {len(heard)} answered applications in "
                    f"'{family}'. Worth checking how your CV frames this direction; a few "
                    "rejections alone do not make it unrealistic.",
                )
            )
    return out


def review_outcomes(ws: Workspace, today: date | None = None) -> OutcomeReview:
    """Funnel, quiet applications and patterns, from the tracker."""
    today = today or date.today()
    entries = list(tracker.load(ws).values())
    applied = [e for e in entries if e.status == "applied"]
    dismissed = [
        e
        for e in entries
        if e.status == "na" and e.reason and e.fit_score is not None and e.fit_score >= HIGH_FIT
    ]
    stages = Counter(e.stage or "no news yet" for e in applied)
    cutoff = (today - timedelta(days=QUIET_DAYS)).isoformat()
    quiet = [
        f"{e.title} at {e.company} (applied {e.applied_at})"
        for e in applied
        if e.stage is None and e.applied_at and e.applied_at <= cutoff
    ]
    return OutcomeReview(
        applications=len(applied),
        by_stage=dict(stages),
        high_fit_dismissals=len(dismissed),
        patterns=_reason_patterns(dismissed) + _family_patterns(entries),
        quiet=quiet,
    )
