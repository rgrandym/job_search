"""What a model comparison costs, and its live progress (used by `model_compare`).

- Cost per model: the calls and tokens each model used, priced at its API rate (USD per 1M
  tokens). Claude prices come from the model catalogue; models without a published price (the
  ChatGPT-account Codex models) take theirs from the plan's `[prices]` table, or show no cost.
  On Codex and Claude Code you pay with plan limits, not per token: the price is a like-for-like
  yardstick between models.
- Plan usage: the share of each plan window (Codex: 5-hour session and week) used during a
  setup, read before and after it. Codex reports whole percents. Claude Code only reports a
  window's utilisation as it nears its limit, so its usage is often "not reported".
- Progress: a live bar on a terminal; when output goes to a file (a background run), a line at
  each quarter instead, so logs stay readable.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, TextIO

from pydantic import BaseModel

from src.core.llm import LLMConfig, ModelUsage
from src.core.llm.catalog import claude_models

WINDOW_NAMES = {"primary": "session (5 h)", "secondary": "week"}


class ModelCost(BaseModel):
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None  # None: no price known for this model


class PlanUsage(BaseModel):
    window: str
    before: float | None = None  # percent used
    after: float | None = None
    used: float | None = None  # after - before, when both are known and the window held
    note: str = ""


# ---------------------------------------------------------------- cost per model


def known_prices(
    extra: dict[str, tuple[float, float]] | None = None,
) -> dict[str, tuple[float, float]]:
    """USD per 1M (input, output) tokens: the catalogue's Claude prices, then `extra`."""
    prices = {
        m.id: (m.input_price, m.output_price)
        for m in claude_models()
        if m.input_price is not None and m.output_price is not None
    }
    return {**prices, **(extra or {})}


def costs(usage: list[ModelUsage], prices: dict[str, tuple[float, float]]) -> list[ModelCost]:
    """Calls, tokens and priced cost per model, most expensive first."""
    by_model: dict[str, ModelCost] = {}
    for u in usage:
        c = by_model.setdefault(
            u.model, ModelCost(model=u.model, calls=0, input_tokens=0, output_tokens=0)
        )
        c.calls += 1
        c.input_tokens += u.input_tokens
        c.output_tokens += u.output_tokens
    for c in by_model.values():
        if price := prices.get(c.model):
            c.cost_usd = round((c.input_tokens * price[0] + c.output_tokens * price[1]) / 1e6, 4)
    return sorted(by_model.values(), key=lambda c: (c.cost_usd or 0, c.calls), reverse=True)


def total_cost(items: list[ModelCost]) -> float | None:
    """The setup's cost, or None when any model it used has no known price."""
    if not items or any(c.cost_usd is None for c in items):
        return None
    return round(sum(c.cost_usd or 0 for c in items), 4)


# ---------------------------------------------------------------- plan usage


def plan_snapshot(cfg: LLMConfig) -> dict[str, dict[str, Any]]:
    """Window -> {percent, resets_at} for the plan `cfg` runs on (empty when not a plan or not
    readable: a comparison never fails over this)."""
    try:
        if cfg.provider == "codex":
            from src.core.llm.codex_backend import codex_usage

            view = codex_usage()
            return {
                WINDOW_NAMES[k]: {
                    "percent": float(w["used_percent"]),
                    "resets_at": w.get("resets_at"),
                }
                for k in ("primary", "secondary")
                if (w := view.get(k))
            }
        if cfg.provider == "claude_code":
            from src.core.llm.claude_code_backend import claude_code_usage

            out = {}
            for w in claude_code_usage()["windows"]:
                u = w.get("utilization")
                pct = None if u is None else float(u) * (100 if float(u) <= 1 else 1)
                out[str(w["window"])] = {"percent": pct, "resets_at": w.get("resets_at")}
            return out
    except Exception:  # noqa: BLE001 - usage is informative; the comparison goes on without it
        return {}
    return {}


def plan_usage(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[PlanUsage]:
    """Share of each plan window a setup used, from snapshots taken around it."""
    out = []
    for window in sorted(set(before) | set(after)):
        b, a = before.get(window, {}), after.get(window, {})
        pb, pa = b.get("percent"), a.get("percent")
        if pb is not None and pa is not None and b.get("resets_at") == a.get("resets_at"):
            out.append(PlanUsage(window=window, before=pb, after=pa, used=round(pa - pb, 1)))
        elif pb is not None and pa is not None:
            out.append(
                PlanUsage(window=window, before=pb, after=pa, note="window reset during the run")
            )
        else:
            out.append(
                PlanUsage(window=window, before=pb, after=pa, note="not reported by the plan")
            )
    return out


# ---------------------------------------------------------------- progress


class Progress:
    """Progress of one setup: first-pass postings and second opinions, over all its groups."""

    WIDTH = 28

    def __init__(self, label: str, total: int, stream: TextIO | None = None) -> None:
        self.label, self.total = label, total
        self.stream = stream or sys.stderr
        self.live = self.stream.isatty()
        self.first: dict[int, int] = {}
        self.second: dict[int, tuple[int, int]] = {}
        self.t0 = time.monotonic()
        self._quarter = -1

    def first_pass(self, group: int) -> Callable[[int, int], Awaitable[None]]:
        async def report(done: int, _total: int) -> None:
            self.first[group] = done
            self._show()

        return report

    def second_opinions(self, group: int) -> Callable[[int, int], Awaitable[None]]:
        async def report(done: int, total: int) -> None:
            self.second[group] = (done, total)
            self._show()

        return report

    def _show(self) -> None:
        done = sum(self.first.values())
        reviews_done = sum(d for d, _ in self.second.values())
        reviews = sum(t for _, t in self.second.values())
        share = done / self.total if self.total else 1.0
        elapsed = int(time.monotonic() - self.t0)
        text = (
            f"{self.label} [{'#' * round(share * self.WIDTH):<{self.WIDTH}}] "
            f"{done}/{self.total} judged · second opinions {reviews_done}/{reviews} · "
            f"{elapsed // 60}:{elapsed % 60:02d}"
        )
        if self.live:
            self.stream.write(f"\r{text}\033[K")
        elif (quarter := int(share * 4)) > self._quarter:
            self._quarter = quarter
            self.stream.write(f"{text}\n")
        self.stream.flush()

    def close(self) -> None:
        if self.live:
            self.stream.write("\n")
            self.stream.flush()
