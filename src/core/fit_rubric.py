"""Shared scoring arithmetic for search verdicts and CV presentation comparisons."""

from __future__ import annotations

FIT_WEIGHTS: dict[str, int] = {
    "function": 30,
    "domain": 20,
    "seniority": 20,
    "leadership": 10,
    "sector": 10,
    "practicality": 10,
}

RATING_LEVELS = 4
CAP_SCORE = 65
UNMET_CAP = 55
UNCHECKED_CAP = 62
WEAK_DIMENSION_SHARE = 0.4


def points_for(levels: dict[str, int]) -> dict[str, int]:
    """The search matcher's half-up conversion from 0-4 levels to weighted points."""
    return {
        name: (levels[name] * weight * 2 + RATING_LEVELS) // (2 * RATING_LEVELS)
        for name, weight in FIT_WEIGHTS.items()
    }


def capped_score(points: dict[str, int], *, unmet: bool = False, checked: bool = True) -> int:
    """The search matcher's total with its caps: an unmet core requirement, an unread
    posting, or any one dimension below 40% of its weight."""
    total = sum(min(points[name], weight) for name, weight in FIT_WEIGHTS.items())
    if unmet:
        return min(total, UNMET_CAP)
    if not checked:
        return min(total, UNCHECKED_CAP)
    if any(points[n] < WEAK_DIMENSION_SHARE * w for n, w in FIT_WEIGHTS.items()):
        return min(total, CAP_SCORE)
    return total
