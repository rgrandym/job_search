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
from src.core.llm import fallback, usage
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
    """Marks each call as in flight in the current task's progress (`core.progress`), records
    who served it (`fallback.served_by`), and marks the provider spent on a limit error."""

    def __init__(self, inner: LLMProvider, purpose: str, model: str, provider: str) -> None:
        self.inner, self.purpose, self.model, self.provider = inner, purpose, model, provider

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        with progress.model_call(self.purpose, self.model):
            try:
                out = self.inner.generate(system=system, prompt=prompt, output_model=output_model)
            except LLMError as exc:
                if exc.kind == "limit":
                    usage.mark_exhausted(self.provider, exc.resets_at, str(exc))
                raise
        fallback.served_by.set(f"{self.provider}:{self.model}")
        return out


def make_structured(
    cfg: LLMConfig,
    role: Role = "screening",
    usage_sink: UsageSink | None = None,
    purpose: str = "structured output",
    fallback_cfg: LLMConfig | None = None,
) -> LLMProvider:
    """Structured-output provider for `cfg.provider`, reporting its calls as task progress.
    Usage reports name the provider that served the call: roles may use different providers.
    With `fallback_cfg` (another plan, see `fallback`), a spent plan hands the role over."""
    primary = _reported(cfg, role, usage_sink, purpose)
    if fallback_cfg is None:
        return primary
    label = f"{cfg.provider}:{cfg.model_for(role)}"

    def replacement() -> LLMProvider:
        return _reported(fallback_cfg, role, usage_sink, purpose, switched_from=label)

    return fallback.FallbackProvider(primary, replacement, cfg.provider, label, cfg.on_limit)


def _reported(
    cfg: LLMConfig,
    role: Role,
    usage_sink: UsageSink | None,
    purpose: str,
    switched_from: str | None = None,
) -> LLMProvider:
    sink = _stamped(usage_sink, cfg.provider, switched_from)
    return _Reported(_backend(cfg, role, sink, purpose), purpose, cfg.model_for(role), cfg.provider)


def _stamped(usage_sink: UsageSink | None, provider: str, switched_from: str | None) -> UsageSink:
    """Every call goes into the spend ledger and the running task's token count, then to the
    caller's sink, if any."""

    def sink(used: ModelUsage) -> None:
        usage.record_spend(
            provider, used.model, used.input_tokens, used.output_tokens, used.estimated
        )
        progress.add_tokens(used.input_tokens, used.output_tokens, used.estimated)
        if usage_sink is not None:
            usage_sink(
                used.model_copy(update={"provider": provider, "switched_from": switched_from})
            )

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
