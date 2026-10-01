"""Model catalogues per provider, for the Settings model picker."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from src.core.llm.types import LLMConfig


class ModelInfo(BaseModel):
    id: str
    name: str
    group: str = ""
    context_length: int | None = None
    input_price: float | None = None  # USD per 1M tokens
    output_price: float | None = None
    tools: bool | None = None  # native tool calling (needed by the chat agents)
    structured: bool | None = None  # JSON-schema output (used by summary/screening/tailoring)
    note: str | None = None


# Claude: from the Anthropic model table (context, $/1M in, $/1M out, note).
CLAUDE: list[tuple[str, str, int, float, float, str]] = [
    ("claude-opus-5-5", "Claude Opus 5.5", 1_000_000, 4, 20, "Default · most capable Opus"),
    ("claude-opus-5", "Claude Opus 5", 1_000_000, 5, 25, ""),
    ("claude-opus-4-8", "Claude Opus 4.8", 1_000_000, 5, 25, ""),
    ("claude-opus-4-7", "Claude Opus 4.7", 1_000_000, 5, 25, ""),
    ("claude-opus-4-6", "Claude Opus 4.6", 1_000_000, 5, 25, "effort up to high/max (no xhigh)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", 1_000_000, 2, 10, "Fast and capable"),
    ("claude-sonnet-5", "Claude Sonnet 5", 1_000_000, 2, 10, ""),
    (
        "claude-sonnet-4-6",
        "Claude Sonnet 4.6",
        1_000_000,
        3,
        15,
        "effort up to high/max (no xhigh)",
    ),
    ("claude-fable-5-1", "Claude Fable 5.1", 1_000_000, 10, 50, "Most capable; long turns"),
    ("claude-fable-5", "Claude Fable 5", 1_000_000, 10, 50, ""),
    ("claude-haiku-4-5", "Claude Haiku 4.5", 200_000, 1, 5, "Cheapest; no effort control"),
]


def _group(model_id: str) -> str:
    for family in ("opus", "sonnet", "fable", "haiku"):
        if family in model_id:
            return family.capitalize()
    return "Claude"


def claude_models() -> list[ModelInfo]:
    return [
        ModelInfo(
            id=i,
            name=n,
            group=_group(i),
            context_length=c,
            input_price=pi,
            output_price=po,
            tools=True,
            structured=True,
            note=note or None,
        )
        for i, n, c, pi, po, note in CLAUDE
    ]


# OpenAI ids that are not chat models.
_NON_CHAT = (
    "embedding",
    "tts",
    "whisper",
    "dall-e",
    "moderation",
    "transcribe",
    "audio",
    "realtime",
    "image",
    "search",
    "davinci",
    "babbage",
    "sora",
    "computer-use",
)


def openai_models(api_key: str | None) -> list[ModelInfo]:
    """Live list for the key; without a key, the Codex catalogue (same model family) if present."""
    if api_key:
        resp = httpx.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        resp.raise_for_status()
        ids = sorted((m["id"] for m in resp.json().get("data", [])), reverse=True)
        return [
            ModelInfo(id=i, name=i, group=i.split("-")[0].upper(), tools=True, structured=True)
            for i in ids
            if not any(x in i for x in _NON_CHAT)
        ]
    return [
        m.model_copy(update={"note": "suggested (add an API key to load your list)"})
        for m in codex_models()
    ]


def openrouter_models() -> list[ModelInfo]:
    """Every model on OpenRouter (public endpoint), with pricing and capabilities."""
    resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=30)
    resp.raise_for_status()
    out: list[ModelInfo] = []
    for m in resp.json().get("data", []):
        arch: dict[str, Any] = m.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        params = set(m.get("supported_parameters") or [])
        pricing = m.get("pricing") or {}
        out.append(
            ModelInfo(
                id=m["id"],
                name=m.get("name", m["id"]),
                group=m["id"].split("/")[0],
                context_length=m.get("context_length"),
                input_price=_per_million(pricing.get("prompt")),
                output_price=_per_million(pricing.get("completion")),
                tools="tools" in params,
                structured=bool({"structured_outputs", "response_format"} & params),
            )
        )
    return sorted(out, key=lambda x: (x.group, x.name))


def codex_models() -> list[ModelInfo]:
    from src.core.llm.codex_backend import list_codex_models

    return [
        ModelInfo(
            id=m["slug"],
            name=m.get("display_name", m["slug"]),
            group="ChatGPT account",
            context_length=m.get("context_window"),
            tools=True,
            structured=True,
            note=(m.get("description") or "")[:120] or None,
        )
        for m in sorted(list_codex_models(), key=lambda m: m.get("priority", 99))
    ]


def _per_million(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price * 1_000_000, 4) if price >= 0 else None


def models_for(cfg: LLMConfig) -> list[ModelInfo]:
    key = cfg.api_key.get_secret_value() if cfg.api_key else None
    if cfg.provider == "anthropic":
        return claude_models()
    if cfg.provider == "openai":
        return openai_models(key)
    if cfg.provider == "openrouter":
        return openrouter_models()
    return codex_models()
