"""Moving a role to another plan when its own plan's limit is spent.

Only plan providers (Claude Code, Codex) hand over to each other: switching to a provider
billed per token is never automatic. The replacement is the model of the same standing on the
other plan: its family tier (frontier, top, mid, fast), then the same generation within that
family (the newest maps to the newest, the previous to the previous). So `claude-opus-5`, the
generation before Opus 5.5, maps to `gpt-6-sol`, the sol before `gpt-6.1-sol`. The effort is
kept, clamped to what the replacement accepts. `LLMConfig.fallback_models` overrides the map.

`LLMConfig.on_limit` decides what happens: "switch" hands over at once; "ask" lets the limit
error reach the caller, which offers stop / switch / wait. `switching(mode)` overrides it for
one run (the user chose "continue on the other model").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal, TypeVar

from pydantic import BaseModel

from src.core.llm import usage
from src.core.llm.types import LLMError

if TYPE_CHECKING:
    from src.core.llm_provider import LLMProvider

T = TypeVar("T", bound=BaseModel)
OnLimit = Literal["switch", "ask"]

TIERS = ("frontier", "top", "mid", "fast")
FAMILY_TIER = {
    "fable": "frontier",
    "astra": "frontier",
    "opus": "top",
    "sol": "top",
    "sonnet": "mid",
    "terra": "mid",
    "haiku": "fast",
    "luna": "fast",
}
# When a family's tier is unknown, the model's own description decides.
DESCRIPTION_TIER = (
    (re.compile(r"frontier|most capable|most demanding", re.I), "frontier"),
    (re.compile(r"fast|affordable|efficient|cheap", re.I), "fast"),
    (re.compile(r"balanced|straightforward", re.I), "mid"),
    (re.compile(r"workhorse", re.I), "top"),
)
OTHER_PLAN = {"claude_code": "codex", "codex": "claude_code"}

# Overrides `on_limit` for the current run (a context variable, so it follows threads).
_mode: ContextVar[OnLimit | None] = ContextVar("fallback_mode", default=None)
# "provider:model" that served the latest call in this context (set by every structured call).
served_by: ContextVar[str | None] = ContextVar("served_by", default=None)


@contextmanager
def switching(mode: OnLimit) -> Iterator[None]:
    """Run the block with `mode` in place of the configured `on_limit`."""
    token = _mode.set(mode)
    try:
        yield
    finally:
        _mode.reset(token)


def current_mode(configured: OnLimit) -> OnLimit:
    return _mode.get() or configured


class Candidate(BaseModel):
    """A model one plan offers, in the order the plan lists them (newest first per family)."""

    id: str
    description: str = ""
    efforts: list[str] | None = None  # None: unknown, take the effort as it is


def family(model_id: str) -> str | None:
    lowered = model_id.lower()
    return next((f for f in FAMILY_TIER if re.search(rf"(^|[-_.]){f}($|[-_.\d])", lowered)), None)


def tier(candidate: Candidate) -> str | None:
    fam = family(candidate.id)
    if fam:
        return FAMILY_TIER[fam]
    return next((t for rx, t in DESCRIPTION_TIER if rx.search(candidate.description)), None)


def _generation(model_id: str, models: list[Candidate]) -> int:
    """Position among the models of the same family (0 = newest)."""
    fam = family(model_id)
    same = [m.id for m in models if family(m.id) == fam] if fam else [model_id]
    return same.index(model_id) if model_id in same else 0


def equivalent(model_id: str, source: list[Candidate], target: list[Candidate]) -> str | None:
    """The target model of the same tier and generation as `model_id` (the nearest tier when
    the target has none of that tier)."""
    me = next((m for m in source if m.id == model_id), Candidate(id=model_id))
    want = tier(me)
    if want is None or not target:
        return None
    by_tier: dict[str, list[Candidate]] = {}
    for m in target:
        if (t := tier(m)) is not None:
            by_tier.setdefault(t, []).append(m)
    order = sorted(TIERS, key=lambda t: abs(TIERS.index(t) - TIERS.index(want)))
    for t in order:
        options = by_tier.get(t)
        if not options:
            continue
        # The same family in the target, in its own newest-first order.
        families = [m for m in options if family(m.id) == family(options[0].id)]
        step = _generation(model_id, source)
        return families[min(step, len(families) - 1)].id
    return None


def clamp_effort(effort: str, candidate: Candidate | None) -> str | None:
    """The same effort if the model accepts it, else the nearest one it does (None: none)."""
    if candidate is None or candidate.efforts is None:
        return effort
    if not candidate.efforts:
        return None
    if effort in candidate.efforts:
        return effort
    ladder = ["low", "medium", "high", "xhigh", "max", "ultra"]
    want = ladder.index(effort) if effort in ladder else 1
    known = [e for e in candidate.efforts if e in ladder]
    if not known:
        return None
    return min(known, key=lambda e: abs(ladder.index(e) - want))


class FallbackProvider:
    """An `LLMProvider` that hands a role to another plan when its own limit is spent.

    While the primary is known to be spent (`usage.exhausted_until`), calls go straight to the
    replacement in "switch" mode, so a spent plan costs one refused call, not one per job."""

    def __init__(
        self,
        primary: LLMProvider,
        replacement: Callable[[], LLMProvider],
        primary_provider: str,
        primary_label: str,
        on_limit: OnLimit,
    ) -> None:
        self.primary = primary
        self._make = replacement
        self._replacement: LLMProvider | None = None
        self.primary_provider = primary_provider
        self.primary_label = primary_label  # "provider:model", to tell switched calls apart
        self.on_limit = on_limit

    def _alt(self) -> LLMProvider:
        if self._replacement is None:
            self._replacement = self._make()
        return self._replacement

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        switch = current_mode(self.on_limit) == "switch"
        if switch and usage.exhausted_until(self.primary_provider):
            return self._alt().generate(system=system, prompt=prompt, output_model=output_model)
        try:
            return self.primary.generate(system=system, prompt=prompt, output_model=output_model)
        except LLMError as exc:
            if exc.kind != "limit" or not switch:
                raise
            return self._alt().generate(system=system, prompt=prompt, output_model=output_model)


# ------------------------------------------------------------------ what each plan offers

_CANDIDATE_TTL_S = 600
_candidates: dict[str, tuple[float, list[Candidate]]] = {}


def candidates(provider: str) -> list[Candidate]:
    """The models a plan provider offers, newest first per family (cached for ten minutes:
    the Codex list starts a process)."""
    import time

    hit = _candidates.get(provider)
    if hit and time.monotonic() - hit[0] < _CANDIDATE_TTL_S:
        return hit[1]
    found = _claude_candidates() if provider == "claude_code" else _codex_candidates()
    _candidates[provider] = (time.monotonic(), found)
    return found


def _claude_candidates() -> list[Candidate]:
    from src.core.llm.anthropic_backend import NO_EFFORT, NO_XHIGH
    from src.core.llm.catalog import CLAUDE

    out = []
    for model_id, _name, _ctx, _in, _out, note in CLAUDE:
        efforts = ["low", "medium", "high", "xhigh", "max"]
        if model_id in NO_EFFORT:
            efforts = []
        elif model_id in NO_XHIGH:
            efforts = ["low", "medium", "high", "max"]
        out.append(Candidate(id=model_id, description=note, efforts=efforts))
    return out


def _codex_candidates() -> list[Candidate]:
    from src.core.llm.codex_backend import list_codex_models

    try:
        listed = sorted(list_codex_models(), key=lambda m: m.get("priority", 99))
    except (OSError, ValueError, LLMError):
        return []
    out = []
    for m in listed:
        levels = m.get("supported_reasoning_levels")
        efforts = None
        if isinstance(levels, list):
            efforts = [str(e.get("effort") if isinstance(e, dict) else e) for e in levels]
        out.append(
            Candidate(id=str(m["slug"]), description=m.get("description") or "", efforts=efforts)
        )
    return out


def replacement_model(
    provider: str, model: str, effort: str, overrides: dict[str, str]
) -> tuple[str, str, str | None] | None:
    """(other plan, its model, the effort it gets) for `model` on plan `provider`; None when
    `provider` is not a plan or the other plan offers nothing comparable."""
    other = OTHER_PLAN.get(provider)
    if other is None:
        return None
    target = candidates(other)
    chosen = overrides.get(model) or equivalent(model, candidates(provider), target)
    if not chosen:
        return None
    return other, chosen, clamp_effort(effort, next((m for m in target if m.id == chosen), None))
