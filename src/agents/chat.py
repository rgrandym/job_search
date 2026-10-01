"""Chat sessions: the orchestrator's conversation history per browser tab (in memory)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from src.agents.definitions import ORCHESTRATOR
from src.agents.runtime import AgentContext, Cancelled, Emit, run_agent
from src.core.llm import ChatMessage, LLMError
from src.jobs.models import SearchQuery
from src.services.workspace import Workspace


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: list[ChatMessage] = field(default_factory=list)
    ctx: AgentContext | None = None


SESSIONS: dict[str, ChatSession] = {}


def get_session(session_id: str | None) -> ChatSession:
    if session_id and session_id in SESSIONS:
        return SESSIONS[session_id]
    s = ChatSession(id=session_id or uuid.uuid4().hex[:12])
    SESSIONS[s.id] = s
    return s


async def handle_user_message(
    ws: Workspace, session: ChatSession, text: str, query: SearchQuery, use_cv: bool, emit: Emit
) -> None:
    """Run one orchestrator turn. On failure the turn is rolled back so history stays valid."""
    ctx = AgentContext(ws=ws, emit=emit, query=query, use_cv=use_cv)
    session.ctx = ctx
    ui = {
        "filters": query.model_dump(exclude_defaults=True),
        "match_against_cv": use_cv,
        "cv_loaded": ws.master_cv is not None,
    }
    start = len(session.messages)
    session.messages.append(
        ChatMessage(role="user", content=f"<ui_context>\n{json.dumps(ui)}\n</ui_context>\n\n{text}")
    )
    try:
        await run_agent(ORCHESTRATOR, session.messages, ctx)
        await emit("done", {"tokens": ctx.tokens})
    except Cancelled:
        del session.messages[start:]
        await emit("done", {"tokens": ctx.tokens, "cancelled": True})
    except (LLMError, OSError, ValueError) as exc:
        del session.messages[start:]
        await emit("error", {"message": str(exc)})
    finally:
        session.ctx = None


def cancel(session: ChatSession) -> None:
    if session.ctx is not None:
        session.ctx.cancelled = True
