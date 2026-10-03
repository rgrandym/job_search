"""Provider-neutral LLM types: config, chat messages, tool specs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.core.config import LLMProviderName, Settings

# quality: rare, accuracy-critical work (profile summary, CV parsing and tailoring, cover
# letters, second opinions, the assistant). screening: the job_matcher's first pass, hundreds
# of calls per search.
Role = Literal["quality", "screening"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


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
    quality_model: str
    screening_model: str
    quality_effort: Effort = "medium"
    screening_effort: Effort = "medium"
    max_tokens: int = 16000
    refusal_fallback: bool = True
    api_key: SecretStr | None = None
    effort: Effort = Field(
        "medium", exclude=True, description="Set by `for_role`: the effort this client uses"
    )

    def model_for(self, role: Role) -> str:
        return self.quality_model if role == "quality" else self.screening_model

    def effort_for(self, role: Role) -> Effort:
        return self.quality_effort if role == "quality" else self.screening_effort

    def for_role(self, role: Role) -> LLMConfig:
        """The config one client uses: its role's effort in `effort`."""
        return self.model_copy(update={"effort": self.effort_for(role)})

    @classmethod
    def from_settings(cls, s: Settings, provider: LLMProviderName | None = None) -> LLMConfig:
        provider = provider or s.llm_provider
        key = {
            "anthropic": s.anthropic_api_key,
            "openai": s.openai_api_key,
            "openrouter": s.openrouter_api_key,
            "codex": None,  # the Codex CLI signs in with the ChatGPT account itself
            "claude_code": None,  # the Claude Code CLI signs in with the Claude plan itself
        }[provider]
        if key is not None and not key.get_secret_value().strip():
            key = None  # `ANTHROPIC_API_KEY=` left empty in .env is not a credential
        return cls(
            provider=provider,
            quality_model=s.quality_model,
            screening_model=s.screening_model,
            quality_effort=s.quality_effort,
            screening_effort=s.screening_effort,
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
