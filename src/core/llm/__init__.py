"""Model backends. The ONLY package (with `src/core/llm_provider.py`) that talks to model APIs.

    make_structured(cfg, role) -> LLMProvider  (one call -> validated Pydantic object)
    make_chat(cfg, role)       -> ChatModel    (tool-calling turns for the agent loop)

Providers: "anthropic" (Claude, official SDK), "openai" and "openrouter" (Chat Completions),
"codex" (the official Codex CLI signed in with a ChatGPT account; see codex_backend),
"claude_code" (the official Claude Code CLI signed in with a Claude plan; see claude_code_backend).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from src.core import progress
from src.core.llm.types import (
    ChatMessage,
    ChatModel,
    ChatResponse,
    LLMConfig,
    LLMError,
    ModelUsage,
    NativeAgentModel,
    NativeTurn,
    Role,
    ToolCall,
    ToolSpec,
    UsageSink,
)

if TYPE_CHECKING:
    from src.core.llm.catalog import ModelInfo
    from src.core.llm_provider import LLMProvider

__all__ = [
    "ChatMessage",
    "ChatModel",
    "ChatResponse",
    "LLMConfig",
    "LLMError",
    "ModelUsage",
    "NativeAgentModel",
    "NativeTurn",
    "Role",
    "ToolCall",
    "ToolSpec",
    "UsageSink",
    "available_models",
    "make_chat",
    "make_structured",
]


T = TypeVar("T", bound=BaseModel)


class _Reported:
    """Marks each call as in flight in the current task's progress (`core.progress`)."""

    def __init__(self, inner: LLMProvider, purpose: str, model: str) -> None:
        self.inner, self.purpose, self.model = inner, purpose, model

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        with progress.model_call(self.purpose, self.model):
            return self.inner.generate(system=system, prompt=prompt, output_model=output_model)


def make_structured(
    cfg: LLMConfig,
    role: Role = "screening",
    usage_sink: UsageSink | None = None,
    purpose: str = "structured output",
) -> LLMProvider:
    """Structured-output provider for `cfg.provider`, reporting its calls as task progress.
    Usage reports name `cfg.provider`: roles may use different providers."""
    sink = _stamped(usage_sink, cfg.provider) if usage_sink is not None else None
    return _Reported(_backend(cfg, role, sink, purpose), purpose, cfg.model_for(role))


def _stamped(usage_sink: UsageSink, provider: str) -> UsageSink:
    def sink(usage: ModelUsage) -> None:
        usage_sink(usage.model_copy(update={"provider": provider}))

    return sink


def _backend(
    cfg: LLMConfig,
    role: Role = "screening",
    usage_sink: UsageSink | None = None,
    purpose: str = "structured output",
) -> LLMProvider:
    """The provider-specific structured backend."""
    if cfg.provider == "codex":
        from src.core.llm.codex_backend import CodexStructured

        return CodexStructured(cfg, role, usage_sink, purpose)
    if cfg.provider == "claude_code":
        from src.core.llm.claude_code_backend import ClaudeCodeStructured

        return ClaudeCodeStructured(cfg, role, usage_sink, purpose)
    if cfg.provider == "anthropic":
        from src.core.llm.anthropic_backend import AnthropicStructured

        return AnthropicStructured(cfg, role, usage_sink, purpose)
    from src.core.llm.openai_backend import OpenAICompatStructured

    return OpenAICompatStructured(cfg, role, usage_sink, purpose)


def make_chat(cfg: LLMConfig, role: Role) -> ChatModel:
    """Tool-calling chat model for `cfg.provider`."""
    if cfg.provider == "codex":
        from src.core.llm.codex_backend import CodexChat

        return CodexChat(cfg, role)
    if cfg.provider == "claude_code":
        from src.core.llm.claude_code_backend import ClaudeCodeChat

        return ClaudeCodeChat(cfg, role)
    if cfg.provider == "anthropic":
        from src.core.llm.anthropic_backend import AnthropicChat

        return AnthropicChat(cfg, role)
    from src.core.llm.openai_backend import OpenAICompatChat

    return OpenAICompatChat(cfg, role)


def available_models(cfg: LLMConfig) -> list[ModelInfo]:
    """Model catalogue the Settings picker offers for this provider."""
    from src.core.llm.catalog import models_for

    return models_for(cfg)
