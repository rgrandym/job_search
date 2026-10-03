"""Model comparison accounting: cost per model, plan usage around a setup, live progress."""

from __future__ import annotations

import asyncio
import io

from src.core.llm import ModelUsage
from src.services import compare_usage as cu


def test_cost_per_model_uses_catalogue_and_plan_prices() -> None:
    usage = [
        ModelUsage(model="claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000),
        ModelUsage(model="claude-opus-5-5", input_tokens=500_000, output_tokens=0),
        ModelUsage(model="gpt-6-luna", input_tokens=2_000_000, output_tokens=200_000),
    ]
    priced = cu.costs(usage, cu.known_prices({"gpt-6-luna": (0.5, 2.0)}))
    by = {c.model: c for c in priced}
    assert (by["claude-opus-5-5"].calls, by["claude-opus-5-5"].cost_usd) == (
        2,
        8.0,
    )  # 1.5M*4 + 0.1M*20
    assert by["gpt-6-luna"].cost_usd == 1.4
    assert cu.total_cost(priced) == 9.4

    unpriced = cu.costs([ModelUsage(model="gpt-6-sol", input_tokens=10)], cu.known_prices())
    assert unpriced[0].cost_usd is None and cu.total_cost(unpriced) is None  # no guess


def test_plan_usage_around_a_setup() -> None:
    before = {
        "session (5 h)": {"percent": 38.0, "resets_at": 100},
        "week": {"percent": 6.0, "resets_at": 900},
    }
    after = {
        "session (5 h)": {"percent": 45.0, "resets_at": 100},
        "week": {"percent": 1.0, "resets_at": 999},  # a new week began mid-run
    }
    usage = {u.window: u for u in cu.plan_usage(before, after)}
    assert usage["session (5 h)"].used == 7.0
    assert usage["week"].used is None and "reset" in usage["week"].note
    [claude] = cu.plan_usage({}, {"five_hour": {"percent": None, "resets_at": 5}})
    assert claude.used is None and "not reported" in claude.note


def test_progress_writes_quarter_lines_when_not_a_terminal() -> None:
    out = io.StringIO()  # not a TTY: a background run's log
    bar = cu.Progress("setup 1/2", total=8, stream=out)
    first, second = bar.first_pass(0), bar.second_opinions(0)

    async def run() -> None:
        for done in range(1, 9):
            await first(done, 8)
        await second(3, 4)

    asyncio.run(run())
    lines = out.getvalue().splitlines()
    assert [line.split("]")[1].split("·")[0].strip() for line in lines] == [
        "1/8 judged",  # started
        "2/8 judged",
        "4/8 judged",
        "6/8 judged",
        "8/8 judged",
    ]  # then one line per quarter; none for the second opinions' update at 100%
    assert "\r" not in out.getvalue()
