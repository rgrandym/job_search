"""Chat sessions: completed turns survive app reloads and backend restarts.

Recent turns keep their tool calls and results, so the model remembers what it read and did
(e.g. the CV text behind a change the user then confirms). A native model (Claude Code, Codex)
keeps its own session, resumed by id on every turn with the same provider.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.agents.definitions import ASSISTANT
from src.agents.runtime import AgentContext, Cancelled, Emit, run_agent
from src.core.config import LLMProviderName
from src.core.llm import ChatMessage, LLMError, Role
from src.jobs.models import SearchQuery
from src.services.workspace import Workspace


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: list[ChatMessage] = field(default_factory=list)
    native_session: str | None = None  # "<provider>:<CLI session id>"
    ctx: AgentContext | None = None


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
    saved, prefix = session.native_session or "", f"{provider}:"
    return saved.removeprefix(prefix) if saved.startswith(prefix) else None


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
) -> None:
    """Run one assistant turn. On failure the turn is rolled back so history stays valid.
    `mcp_url` is where the app serves its tools to models that run their own agent loop."""
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
        await run_agent(ASSISTANT, session.messages, ctx)
        session.messages = _compact(session.messages)
        if ctx.native_session:
            session.native_session = f"{chat_provider or ws.llm.provider}:{ctx.native_session}"
        save_session(ws, session)
        await ctx.flush_usage()
        await emit("done", {"tokens": ctx.tokens})
    except Cancelled:
        session.messages = previous_messages
        await ctx.flush_usage()
        await emit("done", {"tokens": ctx.tokens, "cancelled": True})
    except (LLMError, OSError, ValueError) as exc:
        session.messages = previous_messages
        await ctx.flush_usage()
        await emit("error", {"message": str(exc)})
    finally:
        session.ctx = None


def cancel(session: ChatSession) -> None:
    if session.ctx is not None:
        session.ctx.cancelled = True
        if session.ctx.active_search_id:
            from src.services.search_service import stop_search

            stop_search(session.ctx.active_search_id)
