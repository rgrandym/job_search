"""OpenAI and OpenRouter via the OpenAI-compatible Chat Completions API (raw httpx).

OpenRouter exposes hundreds of open-source and proprietary models behind one key.
The chosen model must support Chat Completions with tool calling. Models that are only
served through OpenAI's Responses API are not supported here.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

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

BASE_URLS = {"openai": "https://api.openai.com/v1", "openrouter": "https://openrouter.ai/api/v1"}
TIMEOUT = httpx.Timeout(300.0, connect=15.0)


def _headers(cfg: LLMConfig) -> dict[str, str]:
    if cfg.api_key is None:
        raise LLMError(f"No API key configured for {cfg.provider}")
    h = {"Authorization": f"Bearer {cfg.api_key.get_secret_value()}"}
    if cfg.provider == "openrouter":
        h |= {"HTTP-Referer": "http://localhost", "X-Title": "AI Job Search"}
    return h


def _check(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise LLMError(f"{resp.request.url.host} {resp.status_code}: {resp.text[:500]}")
    data: dict[str, Any] = resp.json()
    if "choices" not in data:
        raise LLMError(f"Unexpected response: {str(data)[:500]}")
    return data


class OpenAICompatStructured:
    """`LLMProvider` via JSON-schema response format, with one repair retry."""

    def __init__(
        self,
        cfg: LLMConfig,
        role: Role = "screening",
        usage_sink: UsageSink | None = None,
        purpose: str = "structured output",
    ) -> None:
        self.cfg = cfg.for_role(role)
        self.model = cfg.model_for(role)
        self.url = f"{BASE_URLS[cfg.provider]}/chat/completions"
        self.usage_sink = usage_sink
        self.purpose = purpose

    def _record(self, data: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        if self.usage_sink:
            self.usage_sink(
                ModelUsage(
                    model=self.model,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    purpose=self.purpose,
                )
            )

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        schema = output_model.model_json_schema()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": output_model.__name__, "schema": schema, "strict": False},
            },
        }
        with httpx.Client(timeout=TIMEOUT, headers=_headers(self.cfg)) as client:
            resp = client.post(self.url, json=body)
            if resp.status_code == 400:  # model lacks json_schema support: plain JSON mode
                body["response_format"] = {"type": "json_object"}
                messages[0]["content"] += (
                    "\n\nReply with a single JSON object matching this JSON Schema:\n"
                    + json.dumps(schema)
                )
                resp = client.post(self.url, json=body)
            for attempt in range(2):
                data = _check(resp)
                self._record(data)
                content = data["choices"][0]["message"].get("content") or ""
                try:
                    return output_model.model_validate_json(_strip_fences(content))
                except ValidationError as exc:
                    if attempt:
                        raise LLMError(f"Invalid structured output: {exc}") from exc
                    messages += [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": f"That JSON is invalid: {exc}. Fix it."},
                    ]
                    resp = client.post(self.url, json=body)
        raise LLMError("unreachable")


class OpenAICompatChat:
    """`ChatModel` via Chat Completions function calling."""

    def __init__(self, cfg: LLMConfig, role: Role) -> None:
        self.cfg = cfg.for_role(role)
        self.model = cfg.model_for(role)
        self.url = f"{BASE_URLS[cfg.provider]}/chat/completions"

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *map(_to_openai, messages)],
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers(self.cfg)) as client:
            data = _check(await client.post(self.url, json=body))
        choice = data["choices"][0]
        msg = choice["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": c["function"].get("arguments")}
            calls.append(ToolCall(id=c["id"], name=c["function"]["name"], arguments=args))
        usage = data.get("usage") or {}
        return ChatResponse(
            message=ChatMessage(
                role="assistant", content=msg.get("content") or "", tool_calls=calls
            ),
            stop_reason=str(choice.get("finish_reason")),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


def _to_openai(m: ChatMessage) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    if m.role == "assistant" and m.tool_calls:
        return {
            "role": "assistant",
            "content": m.content or None,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in m.tool_calls
            ],
        }
    return {"role": m.role, "content": m.content}


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    return t.strip()
