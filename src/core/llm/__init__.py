"""Model backends. The ONLY package (with `src/core/llm_provider.py`) that talks to model APIs.

    make_structured(cfg, role) -> LLMProvider  (one call -> validated Pydantic object)
    make_chat(cfg, role)       -> ChatModel    (tool-calling turns for the agent loop)

Providers: "anthropic" (Claude, official SDK), "openai" and "openrouter" (Chat Completions),
"codex" (the official Codex CLI signed in with a ChatGPT account; see codex_backend).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.core.llm.types import (
    ChatMessage,
    ChatModel,
    ChatResponse,
    LLMConfig,
    LLMError,
    Role,
    ToolCall,
    ToolSpec,
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
    "Role",
    "ToolCall",
    "ToolSpec",
    "available_models",
    "make_chat",
    "make_structured",
]


def make_structured(cfg: LLMConfig, role: Role = "worker") -> LLMProvider:
    """Structured-output provider for `cfg.provider`."""
    if cfg.provider == "codex":
        from src.core.llm.codex_backend import CodexStructured

        return CodexStructured(cfg, role)
    if cfg.provider == "anthropic":
        from src.core.llm.anthropic_backend import AnthropicStructured

        return AnthropicStructured(cfg, role)
    from src.core.llm.openai_backend import OpenAICompatStructured

    return OpenAICompatStructured(cfg, role)


def make_chat(cfg: LLMConfig, role: Role) -> ChatModel:
    """Tool-calling chat model for `cfg.provider`."""
    if cfg.provider == "codex":
        from src.core.llm.codex_backend import CodexChat

        return CodexChat(cfg, role)
    if cfg.provider == "anthropic":
        from src.core.llm.anthropic_backend import AnthropicChat

        return AnthropicChat(cfg, role)
    from src.core.llm.openai_backend import OpenAICompatChat

    return OpenAICompatChat(cfg, role)


def available_models(cfg: LLMConfig) -> list[ModelInfo]:
    """Model catalogue the Settings picker offers for this provider."""
    from src.core.llm.catalog import models_for

    return models_for(cfg)
