"""Provider-neutral LLM types: config, chat messages, tool specs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.core.config import LLMProviderName, Settings

Role = Literal["orchestrator", "worker"]


class ModelUsage(BaseModel):
    """Token usage reported by a provider, or explicitly marked as estimated."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False
    purpose: str = ""


UsageSink = Callable[[ModelUsage], None]


class LLMError(RuntimeError):
    """Raised when the model refuses, truncates, or returns unparseable output."""


class LLMConfig(BaseModel):
    """Which provider/models to use. Built from Settings, editable from the web UI."""

    provider: LLMProviderName
    orchestrator_model: str
    worker_model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = 16000
    refusal_fallback: bool = True
    api_key: SecretStr | None = None

    def model_for(self, role: Role) -> str:
        return self.orchestrator_model if role == "orchestrator" else self.worker_model

    @classmethod
    def from_settings(cls, s: Settings, provider: LLMProviderName | None = None) -> LLMConfig:
        provider = provider or s.llm_provider
        key = {
            "anthropic": s.anthropic_api_key,
            "openai": s.openai_api_key,
            "openrouter": s.openrouter_api_key,
            "codex": None,  # the Codex CLI signs in with the ChatGPT account itself
        }[provider]
        return cls(
            provider=provider,
            orchestrator_model=s.orchestrator_model,
            worker_model=s.worker_model,
            effort=s.llm_effort,
            max_tokens=s.llm_max_tokens,
            refusal_fallback=s.llm_refusal_fallback,
            api_key=key,
        )


class ToolSpec(BaseModel):
    """A function the model may call. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    """One conversation turn. `raw` keeps provider-native assistant content (e.g. Claude
    thinking blocks), which must be replayed unchanged on the next request."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    raw: Any = Field(None, exclude=True)


class ChatResponse(BaseModel):
    message: ChatMessage
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    usage_estimated: bool = False


class ChatModel(Protocol):
    """Async chat with tool calling."""

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        """One model turn."""
        ...
