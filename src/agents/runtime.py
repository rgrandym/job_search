"""The assistant's loop.

model turn -> (no tool calls: done) | run tool calls concurrently -> append results -> repeat,
bounded by the agent's `max_turns`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from src.agents.registry import TOOLS, AgentDefinition
from src.core.config import LLMProviderName
from src.core.llm import ChatMessage, LLMError, ModelUsage, Role, ToolCall, UsageSink
from src.jobs.models import SearchQuery
from src.jobs.sources.base import SourceError
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
    chat_role: Role = "quality"
    chat_model: str | None = None
    chat_provider: LLMProviderName | None = None
    smart: bool = True
    threshold: float = 60
    widen: bool = False
    profile_key: str | None = None
    cancelled: bool = False
    active_search_id: str | None = None
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    _usage_futures: list[Future[None]] = field(default_factory=list, repr=False)
    _usage_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def usage_sink(self, agent: str, depth: int = 0) -> UsageSink:
        """Return a thread-safe sink that streams structured-call usage to the UI."""
        loop = asyncio.get_running_loop()

        async def emit_usage(usage: ModelUsage) -> None:
            await self.emit(
                "model_usage",
                {
                    "agent": agent,
                    "depth": depth,
                    "provider": self.ws.llm.provider,
                    **usage.model_dump(),
                },
            )

        def record(usage: ModelUsage) -> None:
            future: Future[None] = asyncio.run_coroutine_threadsafe(emit_usage(usage), loop)
            with self._usage_lock:
                self.tokens["input"] += usage.input_tokens
                self.tokens["output"] += usage.output_tokens
                self._usage_futures.append(future)

        return record

    async def flush_usage(self) -> None:
        """Wait until pending usage events have been emitted."""
        with self._usage_lock:
            pending, self._usage_futures = self._usage_futures, []
        if pending:
            await asyncio.gather(*(asyncio.wrap_future(item) for item in pending))


async def run_agent(
    defn: AgentDefinition, messages: list[ChatMessage], ctx: AgentContext, depth: int = 0
) -> str:
    """Run `defn` on `messages` (mutated in place) until it answers without tool calls."""
    if ctx.chat_provider:
        model = ctx.ws.chat(ctx.chat_role, ctx.chat_model, ctx.chat_provider)
    elif ctx.chat_model:
        model = ctx.ws.chat(ctx.chat_role, ctx.chat_model)
    else:
        model = ctx.ws.chat(ctx.chat_role)
    for _ in range(defn.max_turns):
        if ctx.cancelled:
            raise Cancelled
        await ctx.emit("agent_status", {"agent": defn.name, "depth": depth, "status": "thinking"})
        resp = await model.chat(
            system=defn.system_prompt(), messages=messages, tools=defn.tool_specs()
        )
        ctx.tokens["input"] += resp.input_tokens
        ctx.tokens["output"] += resp.output_tokens
        await ctx.emit(
            "model_usage",
            {
                "agent": defn.name,
                "depth": depth,
                "model": ctx.chat_model or ctx.ws.llm.model_for(ctx.chat_role),
                "provider": ctx.chat_provider or ctx.ws.llm.provider,
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "estimated": resp.usage_estimated,
                "purpose": defn.description,
            },
        )
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
        results = [await _run_tool(call, defn, ctx, depth) for call in msg.tool_calls]
        messages.extend(results)
    answer = "I reached the limit for this request. Please tell me which step to continue."
    messages.append(ChatMessage(role="assistant", content=answer))
    await ctx.emit(
        "agent_message", {"agent": defn.name, "depth": depth, "text": answer, "final": True}
    )
    return answer


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
        t = TOOLS[call.name]
        result: Any = await t.fn(t.args_model.model_validate(call.arguments), ctx)
    except Cancelled:
        raise
    except (ValidationError, ValueError, KeyError, LLMError, OSError, SourceError) as exc:
        result, is_error = f"Error: {exc}", True
    text = result if isinstance(result, str) else _to_json(result)
    await ctx.flush_usage()
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


def _to_json(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        return obj.model_dump_json(exclude_none=True)
    return json.dumps(obj, default=str, ensure_ascii=False)
