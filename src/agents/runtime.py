"""The agent loop shared by the orchestrator and every subagent.

model turn -> (no tool calls: done) | run tool calls concurrently -> append results -> repeat,
bounded by the agent's `max_turns`. `delegate` runs a subagent in its own fresh conversation
and returns its final answer as the tool result.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from src.agents.registry import AGENTS, TOOLS, AgentDefinition
from src.core.llm import ChatMessage, LLMError, ToolCall
from src.jobs.models import SearchQuery
from src.services.workspace import Workspace

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
MAX_TOOL_RESULT_CHARS = 16_000


class Cancelled(Exception):
    """The user stopped the run."""


@dataclass
class AgentContext:
    ws: Workspace
    emit: Emit
    query: SearchQuery = field(default_factory=SearchQuery)
    use_cv: bool = True
    cancelled: bool = False
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})


async def run_agent(
    defn: AgentDefinition, messages: list[ChatMessage], ctx: AgentContext, depth: int = 0
) -> str:
    """Run `defn` on `messages` (mutated in place) until it answers without tool calls."""
    model = ctx.ws.chat(defn.role)
    for _ in range(defn.max_turns):
        if ctx.cancelled:
            raise Cancelled
        await ctx.emit("agent_status", {"agent": defn.name, "depth": depth, "status": "thinking"})
        resp = await model.chat(
            system=defn.system_prompt(), messages=messages, tools=defn.tool_specs()
        )
        ctx.tokens["input"] += resp.input_tokens
        ctx.tokens["output"] += resp.output_tokens
        msg = resp.message
        messages.append(msg)
        if msg.content:
            await ctx.emit(
                "agent_message",
                {
                    "agent": defn.name,
                    "depth": depth,
                    "text": msg.content,
                    "final": not msg.tool_calls,
                },
            )
        if not msg.tool_calls:
            return msg.content
        results = await asyncio.gather(*(_run_tool(c, defn, ctx, depth) for c in msg.tool_calls))
        messages.extend(results)
    return f"[{defn.name}] stopped after {defn.max_turns} turns without a final answer."


async def _run_tool(
    call: ToolCall, defn: AgentDefinition, ctx: AgentContext, depth: int
) -> ChatMessage:
    await ctx.emit(
        "tool_call",
        {
            "agent": defn.name,
            "depth": depth,
            "id": call.id,
            "tool": call.name,
            "args": call.arguments,
        },
    )
    is_error = False
    try:
        if call.name not in defn.allowed_tools:
            raise ValueError(f"tool {call.name!r} is not available to {defn.name}")
        if call.name == "delegate":
            result: Any = await _delegate(call.arguments, ctx, depth)
        else:
            t = TOOLS[call.name]
            result = await t.fn(t.args_model.model_validate(call.arguments), ctx)
    except Cancelled:
        raise
    except (ValidationError, ValueError, KeyError, LLMError) as exc:
        result, is_error = f"Error: {exc}", True
    text = result if isinstance(result, str) else _to_json(result)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n…[truncated]"
    await ctx.emit(
        "tool_result",
        {
            "agent": defn.name,
            "depth": depth,
            "id": call.id,
            "tool": call.name,
            "ok": not is_error,
            "preview": text[:300],
        },
    )
    return ChatMessage(role="tool", tool_call_id=call.id, content=text, is_error=is_error)


async def _delegate(args: dict[str, Any], ctx: AgentContext, depth: int) -> str:
    name, task = args.get("agent", ""), args.get("task", "")
    worker = AGENTS.get(name)
    if worker is None or worker.role != "worker":
        raise ValueError(f"unknown subagent {name!r}; choose from {subagent_names()}")
    await ctx.emit("delegate_start", {"agent": name, "depth": depth + 1, "task": task})
    answer = await run_agent(worker, [ChatMessage(role="user", content=task)], ctx, depth + 1)
    await ctx.emit("delegate_end", {"agent": name, "depth": depth + 1})
    return answer


def subagent_names() -> list[str]:
    return [n for n, a in AGENTS.items() if a.role == "worker"]


def _to_json(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        return obj.model_dump_json(exclude_none=True)
    return json.dumps(obj, default=str, ensure_ascii=False)
