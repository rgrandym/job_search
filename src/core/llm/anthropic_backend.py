"""Claude via the official Anthropic SDK (structured outputs + tool-calling chat)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar, cast

import anthropic
from anthropic.types import MessageParam, ToolParam
from pydantic import BaseModel

from src.core.llm.types import (
    ChatMessage,
    ChatResponse,
    LLMConfig,
    LLMError,
    ModelUsage,
    Role,
    ToolCall,
    ToolSpec,
    UsageSink,
)

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


# Models that accept the server-side `fallbacks: "default"` refusal fallback.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
NO_EFFORT = {"claude-haiku-4-5"}
NO_XHIGH = {"claude-opus-4-6", "claude-sonnet-4-6"}


def _effort(cfg: LLMConfig, model: str) -> dict[str, Any]:
    """`output_config` for the model: Haiku has no effort; 4.6 models top out below xhigh."""
    if model in NO_EFFORT:
        return {}
    effort = "high" if cfg.effort == "xhigh" and model in NO_XHIGH else cfg.effort
    return {"output_config": {"effort": effort}}


def _extras(cfg: LLMConfig, model: str) -> dict[str, Any]:
    """Server-side refusal fallback: the API re-runs a declined request on a fallback model."""
    if not cfg.refusal_fallback or model not in FALLBACK_MODELS:
        return {}
    return {
        "extra_headers": {"anthropic-beta": FALLBACK_BETA},
        "extra_body": {"fallbacks": "default"},
    }


def has_ambient_credentials() -> bool:
    """True if the SDK can authenticate without an explicit key (env vars or `ant auth login`)."""
    import os

    env = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
    return any(os.environ.get(v) for v in env) or (Path.home() / ".config" / "anthropic").exists()


def _api_key(cfg: LLMConfig) -> str | None:
    return cfg.api_key.get_secret_value() if cfg.api_key else None  # None -> SDK env/profile


class AnthropicStructured:
    """`LLMProvider` implementation: one call, validated Pydantic output."""

    def __init__(
        self,
        cfg: LLMConfig,
        role: Role = "worker",
        usage_sink: UsageSink | None = None,
        purpose: str = "structured output",
    ) -> None:
        self.cfg = cfg
        self.model = cfg.model_for(role)
        self.client = anthropic.Anthropic(api_key=_api_key(cfg))
        self.usage_sink = usage_sink
        self.purpose = purpose

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=output_model,
            **_effort(self.cfg, self.model),
            **_extras(self.cfg, self.model),
        )
        if response.stop_reason == "refusal":
            raise LLMError(f"Model declined the request: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise LLMError("Response truncated at max_tokens; raise JOBSEARCH_LLM_MAX_TOKENS")
        if response.parsed_output is None:
            raise LLMError("Model returned no parseable structured output")
        if self.usage_sink:
            self.usage_sink(
                ModelUsage(
                    model=self.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    purpose=self.purpose,
                )
            )
        return response.parsed_output


class AnthropicChat:
    """`ChatModel` implementation with tool use. Thinking blocks are replayed verbatim."""

    def __init__(self, cfg: LLMConfig, role: Role) -> None:
        self.cfg = cfg
        self.model = cfg.model_for(role)
        self.client = anthropic.AsyncAnthropic(api_key=_api_key(cfg))

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        tool_params: list[ToolParam] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=cast(list[MessageParam], _to_anthropic(messages)),
            tools=tool_params,
            cache_control={"type": "ephemeral"},
            **_effort(self.cfg, self.model),
            **_extras(self.cfg, self.model),
        )
        if resp.stop_reason == "refusal":
            raise LLMError(f"Model declined the request: {resp.stop_details}")
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in resp.content
            if b.type == "tool_use"
        ]
        msg = ChatMessage(role="assistant", content=text, tool_calls=calls, raw=resp.content)
        return ChatResponse(
            message=msg,
            stop_reason=str(resp.stop_reason),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )


def _to_anthropic(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            content: Any = m.raw
            if content is None:
                content = [{"type": "text", "text": m.content}] if m.content else []
                content += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in m.tool_calls
                ]
            out.append({"role": "assistant", "content": content})
        else:  # tool results are grouped into one user turn
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": m.content,
                "is_error": m.is_error,
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out
