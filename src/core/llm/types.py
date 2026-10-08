"""Provider-neutral LLM types: config, chat messages, tool specs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.core.config import LLMProviderName, Settings

# quality: rare, accuracy-critical work (CV parsing and tailoring, cover letters, second
# opinions, the assistant). screening: the job_matcher's first pass, hundreds of calls per
# search. profile: the profile summary, which every verdict is judged against. cv: tailored
# CVs. letter: cover letters. These three use the quality model unless their own model is set
# (a letter falls back to the CV model first), and each may use another provider.
Role = Literal["quality", "screening", "profile", "cv", "letter"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelUsage(BaseModel):
    """Token usage reported by a provider, or explicitly marked as estimated."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False
    purpose: str = ""
    provider: str | None = None  # the provider that served the call (set by `make_structured`)


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
    profile_model: str = Field("", description="Empty: the profile uses the quality model")
    profile_effort: Effort = "high"
    profile_provider: LLMProviderName | None = Field(
        None, description="Another provider for the profile model; None: `provider`"
    )
    cv_model: str = Field("", description="Empty: tailored CVs use the quality model")
    cv_effort: Effort = "high"
    cv_provider: LLMProviderName | None = Field(
        None, description="Another provider for the CV model; None: `provider`"
    )
    letter_model: str = Field("", description="Empty: letters use the CV model, else quality")
    letter_effort: Effort = "high"
    letter_provider: LLMProviderName | None = Field(
        None, description="Another provider for the letter model; None: `provider`"
    )
    max_tokens: int = 16000
    refusal_fallback: bool = True
    api_key: SecretStr | None = None
    effort: Effort = Field(
        "medium", exclude=True, description="Set by `for_role`: the effort this client uses"
    )

    def _own(self, role: Role) -> tuple[str, Effort, LLMProviderName | None] | None:
        """(model, effort, provider) of a role with its own model (profile, cv, letter)."""
        if role == "profile" and self.profile_model:
            return self.profile_model, self.profile_effort, self.profile_provider
        if role == "letter" and self.letter_model:
            return self.letter_model, self.letter_effort, self.letter_provider
        if role in ("cv", "letter") and self.cv_model:
            return self.cv_model, self.cv_effort, self.cv_provider
        return None

    def model_for(self, role: Role) -> str:
        if own := self._own(role):
            return own[0]
        return self.screening_model if role == "screening" else self.quality_model

    def effort_for(self, role: Role) -> Effort:
        if own := self._own(role):
            return own[1]
        return self.screening_effort if role == "screening" else self.quality_effort

    def provider_for(self, role: Role) -> LLMProviderName:
        """The provider a role runs on: its own only when it has its own model and provider."""
        own = self._own(role)
        return own[2] if own and own[2] else self.provider

    def profile_provider_for(self) -> LLMProviderName:
        """The provider that builds the profile."""
        return self.provider_for("profile")

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
            profile_model=s.profile_model,
            profile_effort=s.profile_effort,
            profile_provider=s.profile_provider,
            cv_model=s.cv_model,
            cv_effort=s.cv_effort,
            cv_provider=s.cv_provider,
            letter_model=s.letter_model,
            letter_effort=s.letter_effort,
            letter_provider=s.letter_provider,
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


class NativeTurn(BaseModel):
    """The outcome of one user turn run by a model's own agent loop."""

    text: str
    session_id: str
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class NativeAgentModel(Protocol):
    """A model that runs its own agent loop natively, calling the app's tools through an MCP
    server, and keeps the conversation in a session it can resume."""

    async def run_native(
        self,
        *,
        system: str,
        prompt: str,
        mcp_url: str,
        session_id: str | None,
        workdir: Path,
        on_text: Callable[[str], Awaitable[None]],
        cancelled: Callable[[], bool],
    ) -> NativeTurn:
        """Run one user turn to its final answer."""
        ...
