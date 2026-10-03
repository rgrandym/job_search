"""Shared language checks for newly drafted CV and cover-letter claims."""

from __future__ import annotations

import re

_PUFFERY = re.compile(
    r"\b(?:world[- ]class|best[- ]in[- ]class|industry[- ]leading|unparalleled|"
    r"visionary|rockstar|guru|proven leader|exceptional|outstanding)\b",
    re.IGNORECASE,
)


def overclaim(text: str) -> str | None:
    """Identify stock superlatives that do not make a factual application stronger."""
    match = _PUFFERY.search(text)
    return f"uses unsupported superlative: {match.group()}" if match else None
