"""Chat sessions: completed turns survive app reloads and backend restarts.

Recent turns keep their tool calls and results, so the model remembers what it read and did
(e.g. the CV text behind a change the user then confirms). A native model (Claude Code, Codex)
keeps its own session, resumed by id on every turn with the same provider.

When the chat model's plan runs out mid-turn, the turn moves to the other plan's equivalent
model (`on_limit` "switch"), which starts with a handover packet (`agents.handoff`); or the
user is offered stop / continue on that model / wait for the reset (`limit_reached`). Each
provider keeps its own session, so coming back resumes it with a shorter packet.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from src.agents import handoff
from src.agents.definitions import ASSISTANT
from src.agents.runtime import AgentContext, Cancelled, Emit, run_agent
from src.core.config import LLMProviderName
from src.core.llm import ChatMessage, LLMError, Role, fallback, usage
from src.core.llm.calls import cancel_group
from src.jobs.models import SearchQuery
from src.services.workspace import Workspace


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: list[ChatMessage] = field(default_factory=list)
    # "<provider>:<CLI session id>" lines, the latest provider first (one session each).
    native_session: str | None = None
    ctx: AgentContext | None = None
    # Tool calls as (message index, row) for handover packets, and where each provider left.
    actions: list[tuple[int, str]] = field(default_factory=list)
    left_at: dict[str, int] = field(default_factory=dict)


SESSIONS: dict[str, ChatSession] = {}
SESSION_ID = re.compile(r"[0-9a-f]{12}")
MAX_TURNS = 20  # user turns kept
TOOL_TURNS = 6  # the latest turns keep their tool calls and results in full


def _path(ws: Workspace, session_id: str) -> Path:
    return ws.settings.data_dir / "chat_sessions" / f"{session_id}.json"


def _native_path(ws: Workspace, session_id: str) -> Path:
    return _path(ws, session_id).with_suffix(".native")


def _native_for(session: ChatSession, provider: str) -> str | None:
    """The CLI session to resume: only one the same provider started."""
    prefix = f"{provider}:"
    for line in (session.native_session or "").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    return None


def _remember_native(session: ChatSession, provider: str, native_id: str) -> None:
    others = [
        line
        for line in (session.native_session or "").splitlines()
        if line and not line.startswith(f"{provider}:")
    ]
    session.native_session = "\n".join([f"{provider}:{native_id}", *others])


def _compact(messages: list[ChatMessage]) -> list[ChatMessage]:
    """The latest turns; older ones keep only the user's message and the final answer."""
    starts = [i for i, m in enumerate(messages) if m.role == "user"]
    kept = messages[starts[-MAX_TURNS] :] if len(starts) > MAX_TURNS else messages
    starts = [i for i, m in enumerate(kept) if m.role == "user"]
    cut = starts[-TOOL_TURNS] if len(starts) > TOOL_TURNS else 0
    final = [
        m for m in kept[:cut] if m.role == "user" or (m.role == "assistant" and not m.tool_calls)
    ]
    return final + kept[cut:]


def get_session(session_id: str | None, ws: Workspace) -> ChatSession:
    if session_id and session_id in SESSIONS:
        return SESSIONS[session_id]
    safe_id = session_id if session_id and SESSION_ID.fullmatch(session_id) else None
    s = ChatSession(id=safe_id or uuid.uuid4().hex[:12])
    path = _path(ws, s.id)
    if path.exists():
        try:
            s.messages = [ChatMessage.model_validate(item) for item in json.loads(path.read_text())]
        except (OSError, ValueError):
            s.messages = []
    native = _native_path(ws, s.id)
    if native.exists():
        s.native_session = native.read_text().strip() or None
    SESSIONS[s.id] = s
    return s


def save_session(ws: Workspace, session: ChatSession) -> None:
    """Keep complete user questions and final answers across app restarts."""
    path = _path(ws, session.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps([m.model_dump(exclude={"raw"}) for m in session.messages]))
    temporary.replace(path)
    if session.native_session:
        _native_path(ws, session.id).write_text(session.native_session)


def clear_session(ws: Workspace, session: ChatSession) -> None:
    session.messages.clear()
    session.native_session = None
    session.actions.clear()
    session.left_at.clear()
    _path(ws, session.id).unlink(missing_ok=True)
    _native_path(ws, session.id).unlink(missing_ok=True)


def visible_history(session: ChatSession) -> list[dict[str, str]]:
    """Return saved turns without the internal UI context wrapper."""
    result = []
    for message in session.messages:
        if message.role == "user":
            result.append(
                {"role": "user", "text": message.content.split("</ui_context>\n\n", 1)[-1]}
            )
        elif message.role == "assistant" and not message.tool_calls and message.content:
            result.append({"role": "assistant", "text": message.content})
    return result


async def handle_user_message(
    ws: Workspace,
    session: ChatSession,
    text: str,
    query: SearchQuery,
    use_cv: bool,
    emit: Emit,
    chat_role: Role = "quality",
    chat_model: str | None = None,
    chat_provider: LLMProviderName | None = None,
    smart: bool = True,
    threshold: float = 60,
    widen: bool = False,
    profile_key: str | None = None,
    mcp_url: str | None = None,
    switch: bool = False,
) -> None:
    """Run one assistant turn. On failure the turn is rolled back so history stays valid.
    `mcp_url` is where the app serves its tools to models that run their own agent loop.
    `switch`: the user chose to continue on the other plan after a limit (`limit_reached`)."""
    ctx = AgentContext(
        ws=ws,
        emit=emit,
        query=query,
        use_cv=use_cv,
        chat_role=chat_role,
        chat_model=chat_model,
        chat_provider=chat_provider,
        smart=smart,
        threshold=threshold,
        widen=widen,
        profile_key=profile_key,
        mcp_url=mcp_url,
        native_session=_native_for(session, chat_provider or ws.llm.provider),
    )
    session.ctx = ctx
    ui: dict[str, object] = {
        "filters": query.model_dump(exclude_defaults=True),
        "match_against_cv": use_cv,
        "cv_selected": ws.active_cv_id is not None,
        "selected_profile": profile_key,
        "smart_match": smart,
        "threshold": threshold,
        "widen": widen,
        "chat_model": chat_model or ws.llm.model_for(chat_role),
        "chat_provider": chat_provider or ws.llm.provider,
    }
    previous_messages = session.messages.copy()
    session.messages.append(
        ChatMessage(role="user", content=f"<ui_context>\n{json.dumps(ui)}\n</ui_context>\n\n{text}")
    )
    try:
        with fallback.switching("switch") if switch else nullcontext():
            await _run_turn(ws, session, ctx)
        session.messages = _compact(session.messages)
        save_session(ws, session)
        await ctx.flush_usage()
        await emit("done", {"tokens": ctx.tokens})
    except Cancelled:
        session.messages = previous_messages
        await ctx.flush_usage()
        await emit("done", {"tokens": ctx.tokens, "cancelled": True})
    except asyncio.CancelledError:
        if not ctx.cancelled:  # not the user's Stop (e.g. the app shutting down)
            raise
        session.messages = previous_messages
        await emit("done", {"tokens": ctx.tokens, "cancelled": True})
    except LimitReached as stop:
        session.messages = previous_messages
        await ctx.flush_usage()
        await emit("limit_reached", {**stop.info, "text": text})
    except (LLMError, OSError, ValueError) as exc:
        session.messages = previous_messages
        await ctx.flush_usage()
        await emit("error", {"message": str(exc)})
    finally:
        _keep_actions(session, ctx, len(previous_messages))
        session.ctx = None


class LimitReached(Exception):
    """The chat model's plan is spent and nothing takes over: the user decides."""

    def __init__(self, info: dict[str, object]) -> None:
        super().__init__("plan limit reached")
        self.info = info


def _route(ws: Workspace, ctx: AgentContext) -> tuple[LLMProviderName, str]:
    return ctx.chat_provider or ws.llm.provider, ctx.chat_model or ws.llm.model_for(ctx.chat_role)


def _replacement(ws: Workspace, ctx: AgentContext) -> tuple[LLMProviderName, str] | None:
    """The other plan's equivalent of the chat model, when it is signed in and not spent."""
    provider, model = _route(ws, ctx)
    found = fallback.replacement_model(
        provider, model, ws.llm.effort_for(ctx.chat_role), ws.llm.fallback_models
    )
    if found is None:
        return None
    other: LLMProviderName = "codex" if found[0] == "codex" else "claude_code"
    if not ws.provider_ready(other) or usage.exhausted_until(other):
        return None
    return other, found[1]


async def _run_turn(ws: Workspace, session: ChatSession, ctx: AgentContext) -> None:
    """Run the turn; on a spent plan, hand it to the other plan or let the user decide."""
    start = len(session.messages)
    switching = fallback.current_mode(ws.llm.on_limit) == "switch"
    provider, _ = _route(ws, ctx)
    if switching and usage.exhausted_until(provider) and (target := _replacement(ws, ctx)):
        await _hand_over(ws, session, ctx, target, "limit")  # spent already: skip the refusal
    _prepare_return(ws, session, ctx)
    try:
        await run_agent(ASSISTANT, session.messages, ctx)
    except LLMError as exc:
        if exc.kind != "limit" or ctx.cancelled:
            raise
        provider, model = _route(ws, ctx)
        until = usage.mark_exhausted(provider, exc.resets_at, str(exc))
        target = _replacement(ws, ctx)
        if not switching or target is None:
            raise LimitReached(_limit_info(provider, model, until, target)) from exc
        del session.messages[start:]  # a half-run attempt; its tool calls stay in `actions`
        await _hand_over(ws, session, ctx, target, "limit")
        await run_agent(ASSISTANT, session.messages, ctx)
    provider, _ = _route(ws, ctx)
    if ctx.native_session:
        _remember_native(session, provider, ctx.native_session)
    session.left_at[provider] = len(session.messages)


def _limit_info(
    provider: str, model: str, until: float, target: tuple[str, str] | None
) -> dict[str, object]:
    return {
        "provider": provider,
        "model": model,
        "resets_at": until,
        "replacement": {"provider": target[0], "model": target[1]} if target else None,
    }


async def _hand_over(
    ws: Workspace,
    session: ChatSession,
    ctx: AgentContext,
    target: tuple[LLMProviderName, str],
    reason: str,
) -> None:
    """Point `ctx` at `target`, with the packet the new model starts from."""
    old_provider, old_model = _route(ws, ctx)
    session.left_at.setdefault(old_provider, len(session.messages) - 1)
    rows = session.actions + [(len(session.messages) - 1, row) for row in ctx.actions]
    ctx.chat_provider, ctx.chat_model = target
    ctx.native_session = _native_for(session, target[0])
    label = f"{old_provider} {old_model}"
    history = session.messages[:-1]
    since = session.left_at.get(target[0])
    if ctx.native_session and since is not None:
        if since >= max(session.left_at.values(), default=0):
            return  # it ran the last turn itself: its own session is up to date
        ctx.handoff = handoff.build_return(history, rows, since, label)
    else:
        ctx.native_session = None
        ctx.handoff = handoff.build(history, rows, label, reason)
    await ctx.emit(
        "handover",
        {
            "from": {"provider": old_provider, "model": old_model},
            "to": {"provider": target[0], "model": target[1]},
            "reason": reason,
        },
    )


def _prepare_return(ws: Workspace, session: ChatSession, ctx: AgentContext) -> None:
    """Coming back to a provider whose session another model continued: the return packet."""
    provider, _ = _route(ws, ctx)
    since = session.left_at.get(provider)
    latest = max(session.left_at.values(), default=0)
    if ctx.handoff or not ctx.native_session or since is None or since >= latest:
        return
    others = [p for p, at in session.left_at.items() if p != provider and at > since]
    ctx.handoff = handoff.build_return(
        session.messages[:-1], session.actions, since, ", ".join(others) or "another model"
    )


def _keep_actions(session: ChatSession, ctx: AgentContext, at: int) -> None:
    session.actions = (session.actions + [(at, row) for row in ctx.actions])[-40:]


def cancel(session: ChatSession) -> None:
    """Stop the running turn: its tools' model calls are killed or cut off at once and any
    search it started is stopped (the caller also cancels the turn's task)."""
    if session.ctx is not None:
        session.ctx.cancelled = True
        cancel_group(session.ctx.group)
        if session.ctx.active_search_id:
            from src.services.search_service import stop_search

            stop_search(session.ctx.active_search_id)
