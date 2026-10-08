"""The assistant's loop.

A model that runs its own agent loop (`NativeAgentModel`: Claude Code) gets the user's turn
whole and calls the app's tools through the MCP endpoint (`src/agents/mcp.py`), keeping its
own session across turns. Other models run here:
model turn -> (no tool calls: done) | run tool calls -> append results -> repeat, bounded by
the agent's `max_turns`.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from src.agents.registry import TOOLS, AgentDefinition
from src.core.config import LLMProviderName
from src.core.llm import (
    ChatMessage,
    ChatModel,
    LLMError,
    ModelUsage,
    NativeAgentModel,
    Role,
    ToolCall,
    UsageSink,
)
from src.jobs.models import SearchQuery
from src.jobs.sources.base import SourceError
from src.services.workspace import Workspace

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
MAX_TOOL_RESULT_CHARS = 400_000  # whole documents; only runaway output is cut


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
    # Native runs: where the app's MCP endpoint is served, and the model's resumable session.
    mcp_url: str | None = None
    native_session: str | None = None
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
                    **usage.model_dump(),
                    "provider": usage.provider or self.ws.llm.provider,
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
    if ctx.mcp_url and isinstance(model, NativeAgentModel):
        return await _run_native(defn, model, messages, ctx, depth)
    return await _run_loop(defn, model, messages, ctx, depth)


async def _run_loop(
    defn: AgentDefinition,
    model: ChatModel,
    messages: list[ChatMessage],
    ctx: AgentContext,
    depth: int,
) -> str:
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


@dataclass
class NativeRun:
    """A native run in progress, reachable by its token from the MCP endpoint."""

    defn: AgentDefinition
    ctx: AgentContext
    depth: int


RUNS: dict[str, NativeRun] = {}


def _native_prompt(messages: list[ChatMessage], ctx: AgentContext) -> str:
    """The new user message; on a fresh session, earlier turns come along as background."""
    latest = messages[-1].content
    if ctx.native_session:
        return latest
    earlier = [
        f"[{m.role}]\n{m.content}"
        for m in messages[:-1]
        if m.role == "user" or (m.role == "assistant" and not m.tool_calls and m.content)
    ]
    if not earlier:
        return latest
    history = "\n\n".join(earlier)
    return f"<earlier_conversation>\n{history}\n</earlier_conversation>\n\n{latest}"


async def _run_native(
    defn: AgentDefinition,
    model: NativeAgentModel,
    messages: list[ChatMessage],
    ctx: AgentContext,
    depth: int,
) -> str:
    """Hand the turn to the model's own agent loop; tools run here via the MCP endpoint."""
    token = uuid.uuid4().hex
    RUNS[token] = NativeRun(defn, ctx, depth)

    async def on_text(text: str) -> None:
        await ctx.emit(
            "agent_message", {"agent": defn.name, "depth": depth, "text": text, "final": False}
        )

    await ctx.emit("agent_status", {"agent": defn.name, "depth": depth, "status": "thinking"})
    try:
        for attempt in range(2):
            try:
                turn = await model.run_native(
                    system=defn.system_prompt(),
                    prompt=_native_prompt(messages, ctx),
                    mcp_url=f"{ctx.mcp_url}/{token}",
                    session_id=ctx.native_session,
                    workdir=ctx.ws.settings.data_dir / "chat_sessions" / "native",
                    on_text=on_text,
                    cancelled=lambda: ctx.cancelled,
                )
                break
            except LLMError:
                if ctx.cancelled:
                    raise Cancelled from None
                if attempt or not ctx.native_session:
                    raise
                ctx.native_session = None  # a lost session: start again with the history
    finally:
        RUNS.pop(token, None)
    ctx.native_session = turn.session_id or ctx.native_session
    ctx.tokens["input"] += turn.input_tokens
    ctx.tokens["output"] += turn.output_tokens
    await ctx.emit(
        "model_usage",
        {
            "agent": defn.name,
            "depth": depth,
            "model": ctx.chat_model or ctx.ws.llm.model_for(ctx.chat_role),
            "provider": ctx.chat_provider or ctx.ws.llm.provider,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "estimated": False,
            "purpose": defn.description,
        },
    )
    messages.append(ChatMessage(role="assistant", content=turn.text))
    await ctx.emit(
        "agent_message", {"agent": defn.name, "depth": depth, "text": turn.text, "final": True}
    )
    return turn.text


async def run_tool(call: ToolCall, run: NativeRun) -> ChatMessage:
    """Run one tool for a native run (called by the MCP endpoint)."""
    if run.ctx.cancelled:
        raise Cancelled
    return await _run_tool(call, run.defn, run.ctx, run.depth)


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
