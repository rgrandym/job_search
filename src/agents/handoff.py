"""The handover packet: what the assistant reads when another plan's model takes a
conversation over, or when it comes back to a conversation another model continued.

Built from recorded data, never from a model summary, so it cannot invent progress: the
user's messages verbatim, the actions taken (tool calls and whether they worked), the last
answer, and why the switch happened. Templates live in `prompts/handoff*.md`.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from src.core.llm import ChatMessage

PROMPTS = Path(__file__).parent / "prompts"
MAX_REQUESTS = 10
MAX_ACTIONS = 15
MAX_LAST_MESSAGE = 2000
REASONS = {
    "limit": "its plan's usage limit ran out",
    "switch": "the user chose to continue on another model",
}


def _fill(name: str, values: dict[str, str]) -> str:
    template = (PROMPTS / name).read_text()
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def visible(message: ChatMessage) -> str:
    """A user message without the app's `<ui_context>` wrapper."""
    return message.content.split("</ui_context>\n\n", 1)[-1].strip()


def requests(messages: list[ChatMessage], since: int = 0) -> str:
    texts = [visible(m) for m in messages[since:] if m.role == "user"][-MAX_REQUESTS:]
    if not texts:
        return "(none)"
    return "\n\n".join(f"{i}. " + t.replace("\n", "\n   ") for i, t in enumerate(texts, 1))


def actions(rows: list[tuple[int, str]], since: int = 0) -> str:
    """Tool calls recorded at message index >= `since`, newest last."""
    kept = [row for at, row in rows if at >= since][-MAX_ACTIONS:]
    return "\n".join(f"- {row}" for row in kept) or "(none)"


def last_answer(messages: list[ChatMessage]) -> str:
    answer = next(
        (m.content for m in reversed(messages) if m.role == "assistant" and not m.tool_calls),
        "",
    )
    if not answer:
        return "(none)"
    return answer if len(answer) <= MAX_LAST_MESSAGE else answer[:MAX_LAST_MESSAGE] + " …"


def build(
    messages: list[ChatMessage],
    rows: list[tuple[int, str]],
    from_label: str,
    reason: str,
) -> str:
    """The full packet for a model that has not seen this conversation."""
    return _fill(
        "handoff.md",
        {
            "previous": from_label,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "reason": REASONS.get(reason, reason),
            "requests": requests(messages),
            "actions": actions(rows),
            "last_answer": last_answer(messages),
        },
    )


def build_return(
    messages: list[ChatMessage], rows: list[tuple[int, str]], since: int, other_label: str
) -> str:
    """The shorter packet for a model resuming its own session after another model worked on
    the conversation from message index `since`."""
    return _fill(
        "handoff_return.md",
        {
            "other": other_label,
            "requests": requests(messages, since),
            "actions": actions(rows, since),
            "last_answer": last_answer(messages[since:]),
        },
    )


def with_message(packet: str, text: str) -> str:
    return f"{packet}\n\n---\n\nThe user's message, to act on now:\n\n{text}"
