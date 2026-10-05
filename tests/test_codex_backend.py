"""Codex CLI provider behavior, with every subprocess and model call faked."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest
from pydantic import BaseModel

from src.core.llm import ChatMessage, LLMConfig, ToolSpec
from src.core.llm import codex_backend as backend


def _config() -> LLMConfig:
    return LLMConfig(
        provider="codex",
        quality_model="gpt-test",
        screening_model="gpt-test",
    )


def test_codex_status_uses_cli_chatgpt_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "codex_binary", lambda: "/bin/codex")
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            "WARNING: could not create PATH aliases\nLogged in using ChatGPT\n",
            "",
        ),
    )

    status = backend.codex_status()

    assert status == {
        "installed": True,
        "logged_in": True,
        "path": "/bin/codex",
        "message": "Logged in using ChatGPT",
    }


def test_codex_model_catalogue_keeps_listed_models(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "models": [
            {"slug": "gpt-visible", "visibility": "list"},
            {"slug": "gpt-hidden", "visibility": "hide"},
        ]
    }
    monkeypatch.setattr(backend, "codex_binary", lambda: "/bin/codex")
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, backend.json.dumps(payload), ""
        ),
    )

    assert backend.list_codex_models() == [payload["models"][0]]


def test_codex_usage_view_exposes_limits_without_account_id() -> None:
    view = backend._usage_view(
        {
            "ordinaryUsageAllowed": True,
            "accountId": "private-account-id",
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 123},
                "secondary": {
                    "usedPercent": 20,
                    "windowDurationMins": 10080,
                    "resetsAt": 456,
                },
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            },
        },
        {"summary": {"lifetimeTokens": 123456}},
    )

    assert view["plan_type"] == "plus"
    assert view["primary"]["remaining_percent"] == 93
    assert view["secondary"]["remaining_percent"] == 80
    assert view["lifetime_tokens"] == 123456
    assert "account_id" not in view


def test_strict_schema_preserves_field_names_and_makes_defaults_nullable() -> None:
    class Answer(BaseModel):
        title: str = ""
        note: str | None = None

    schema = backend.strict_schema(Answer.model_json_schema())

    assert schema["required"] == ["title", "note"]
    assert "title" in schema["properties"]
    assert {item["type"] for item in schema["properties"]["title"]["anyOf"]} == {
        "string",
        "null",
    }


def test_codex_structured_validates_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class Answer(BaseModel):
        value: str

    seen: dict[str, Any] = {}

    def fake_run(cfg: LLMConfig, model: str, prompt: str, schema: dict[str, Any]) -> dict[str, str]:
        seen.update(model=model, prompt=prompt, schema=schema)
        return {"value": "ready"}

    monkeypatch.setattr(backend, "run_codex", fake_run)

    result = backend.CodexStructured(_config()).generate(
        system="Follow the rules", prompt="Return a result", output_model=Answer
    )

    assert result.value == "ready"
    assert seen["model"] == "gpt-test"
    assert "Follow the rules" in seen["prompt"]


def test_codex_structured_repairs_invalid_output_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class Answer(BaseModel):
        value: str

    prompts: list[str] = []

    def fake_run(cfg: LLMConfig, model: str, prompt: str, schema: dict[str, Any]) -> dict[str, str]:
        prompts.append(prompt)
        return {"wrong": "field"} if len(prompts) == 1 else {"value": "ready"}

    monkeypatch.setattr(backend, "run_codex", fake_run)
    result = backend.CodexStructured(_config()).generate(
        system="Follow the rules", prompt="Return a result", output_model=Answer
    )
    assert result.value == "ready"
    assert len(prompts) == 2
    assert "did not match the schema" in prompts[1]


def test_codex_structured_repairs_invalid_json_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class Answer(BaseModel):
        value: str

    prompts: list[str] = []

    def fake_run(cfg: LLMConfig, model: str, prompt: str, schema: dict[str, Any]) -> dict[str, str]:
        prompts.append(prompt)
        if len(prompts) == 1:
            raise backend.LLMError("Codex returned invalid JSON: malformed")
        return {"value": "ready"}

    monkeypatch.setattr(backend, "run_codex", fake_run)
    assert backend.CodexStructured(_config()).generate(
        system="Follow the rules", prompt="Return a result", output_model=Answer
    ).value == "ready"
    assert len(prompts) == 2


def test_codex_structured_does_not_retry_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Answer(BaseModel):
        value: str

    calls = 0

    def fake_run(cfg: LLMConfig, model: str, prompt: str, schema: dict[str, Any]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise backend.LLMError("codex exec timed out after 600s")

    monkeypatch.setattr(backend, "run_codex", fake_run)
    with pytest.raises(backend.LLMError, match="timed out"):
        backend.CodexStructured(_config()).generate(
            system="Follow the rules", prompt="Return a result", output_model=Answer
        )
    assert calls == 1


def test_codex_chat_decodes_emulated_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(
        cfg: LLMConfig, model: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "message": "I will search.",
            "tool_calls": [{"name": "search_jobs", "arguments_json": '{"limit": 3}'}],
        }

    monkeypatch.setattr(backend, "run_codex_async", fake_run)
    response = asyncio.run(
        backend.CodexChat(_config(), "quality").chat(
            system="Help the user",
            messages=[ChatMessage(role="user", content="Find roles")],
            tools=[
                ToolSpec(
                    name="search_jobs",
                    description="Search for jobs",
                    parameters={"type": "object"},
                )
            ],
        )
    )

    assert response.stop_reason == "tool_use"
    assert response.message.tool_calls[0].name == "search_jobs"
    assert response.message.tool_calls[0].arguments == {"limit": 3}
