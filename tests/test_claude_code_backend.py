"""Claude Code CLI provider behavior, with every subprocess and model call faked."""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

import pytest
from pydantic import BaseModel

from src.core.llm import ChatMessage, LLMConfig, LLMError, ToolSpec, make_chat, make_structured
from src.core.llm import claude_code_backend as backend
from src.core.llm.catalog import models_for


def _config(**update: Any) -> LLMConfig:
    return LLMConfig(
        provider="claude_code",
        quality_model="claude-sonnet-5-5",
        screening_model="claude-haiku-4-5",
        effort="medium",
    ).model_copy(update=update)


def _cli_json(structured: Any, **extra: Any) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": structured,
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 5,
                "output_tokens": 30,
            },
            **extra,
        }
    )


def _fake_status(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(backend, "claude_binary", lambda: "/bin/claude")
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, json.dumps(payload), ""),
    )


def test_status_reports_plan_without_email(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_status(
        monkeypatch,
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "email": "person@example.com",
            "orgId": "org-1",
            "subscriptionType": "pro",
        },
    )

    status = backend.claude_code_status()

    assert status["logged_in"] is True
    assert status["subscription"] == "pro"
    assert status["message"] == "Signed in with Claude Pro."
    assert "person@example.com" not in json.dumps(status)


def test_status_rejects_api_account_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_status(monkeypatch, {"loggedIn": True, "authMethod": "console"})

    assert backend.claude_code_status()["logged_in"] is False


def test_status_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "claude_binary", lambda: None)

    status = backend.claude_code_status()

    assert status["installed"] is False
    assert status["logged_in"] is False


def test_command_is_headless_toolless_and_skips_haiku_effort() -> None:
    cfg = _config()
    sonnet = backend._command("/bin/claude", cfg, "claude-sonnet-5-5", "sys", {"type": "object"})
    haiku = backend._command("/bin/claude", cfg, "claude-haiku-4-5", "sys", {"type": "object"})

    assert sonnet[:2] == ["/bin/claude", "-p"]
    assert sonnet[sonnet.index("--output-format") + 1] == "stream-json"
    assert sonnet[sonnet.index("--tools") + 1] == ""
    assert "--no-session-persistence" in sonnet
    assert "--strict-mcp-config" in sonnet
    assert sonnet[sonnet.index("--effort") + 1] == "medium"
    assert "--effort" not in haiku


def test_subprocess_env_drops_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("JOBSEARCH_ANTHROPIC_API_KEY", "sk-should-not-leak")
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, _cli_json({"value": "ok"}), "")

    monkeypatch.setattr(backend, "claude_binary", lambda: "/bin/claude")
    monkeypatch.setattr(backend, "run_cli", fake_run)

    backend.run_claude(_config(), "claude-haiku-4-5", "sys", "prompt", {"type": "object"})

    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "JOBSEARCH_ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["input"] == "prompt"


def test_result_surfaces_cli_errors() -> None:
    error = json.dumps({"subtype": "error_during_execution", "is_error": True, "result": "limit"})

    with pytest.raises(LLMError, match="limit"):
        backend._result(1, error, "")
    with pytest.raises(LLMError, match="exit 2"):
        backend._result(2, "not json", "boom")


def test_structured_validates_output_and_reports_real_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Answer(BaseModel):
        value: str

    seen: dict[str, Any] = {}
    usage: list[Any] = []

    def fake_run(
        cfg: LLMConfig, model: str, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        seen.update(model=model, system=system, prompt=prompt)
        return json.loads(_cli_json({"value": "ready"}))

    monkeypatch.setattr(backend, "run_claude", fake_run)
    provider = make_structured(_config(), "screening", usage.append, "screening")

    result = provider.generate(system="Rules", prompt="Do it", output_model=Answer)

    assert result.value == "ready"
    assert seen == {"model": "claude-haiku-4-5", "system": "Rules", "prompt": "Do it"}
    assert (usage[0].input_tokens, usage[0].output_tokens, usage[0].estimated) == (125, 30, False)


def test_chat_decodes_emulated_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(
        cfg: LLMConfig, model: str, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        assert model == "claude-sonnet-5-5"
        assert "Help the user" in prompt
        turn = {
            "message": "Searching.",
            "tool_calls": [{"name": "search_jobs", "arguments_json": '{"limit": 3}'}],
        }
        return json.loads(_cli_json(turn))

    monkeypatch.setattr(backend, "run_claude_async", fake_run)
    tool = ToolSpec(name="search_jobs", description="Search", parameters={"type": "object"})

    response = asyncio.run(
        make_chat(_config(), "quality").chat(
            system="Help the user",
            messages=[ChatMessage(role="user", content="Find roles")],
            tools=[tool],
        )
    )

    assert response.stop_reason == "tool_use"
    assert response.message.tool_calls[0].arguments == {"limit": 3}
    assert response.usage_estimated is False


def test_catalogue_is_claude_family_without_api_prices() -> None:
    models = models_for(_config())

    assert {"claude-sonnet-5-5", "claude-haiku-4-5"} <= {m.id for m in models}
    assert all(m.input_price is None and m.output_price is None for m in models)


def test_empty_env_api_key_is_not_a_credential() -> None:
    from pydantic import SecretStr

    from src.core.config import Settings

    settings = Settings(anthropic_api_key=SecretStr(""), llm_provider="anthropic")

    assert LLMConfig.from_settings(settings).api_key is None


def test_missing_anthropic_credentials_become_a_readable_error() -> None:
    from src.core.llm import anthropic_backend

    auth = TypeError('"Could not resolve authentication method. Expected one of api_key"')

    assert isinstance(anthropic_backend._llm_error(auth), LLMError)
    assert "Claude Code" in str(anthropic_backend._llm_error(auth))
    other = TypeError("unrelated bug")
    assert anthropic_backend._llm_error(other) is other  # real bugs are not masked


def test_stream_output_yields_result_and_records_plan_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "_LIMITS", {})
    limit = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning",
            "resetsAt": 1790886600,
            "rateLimitType": "five_hour",
            "utilization": 0.82,
            "isUsingOverage": False,
        },
    }
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(limit),
            "noise",
            _cli_json({"a": 1}),
        ]
    )

    data = backend._result(0, stdout, "")
    usage = backend.claude_code_usage()

    assert data["structured_output"] == {"a": 1}
    assert usage["windows"] == [
        {
            "window": "five_hour",
            "status": "allowed_warning",
            "resets_at": 1790886600,
            "utilization": 0.82,
            "using_overage": False,
            "observed_at": usage["updated_at"],
        }
    ]


def test_cli_calls_are_killed_by_their_group() -> None:
    import sys
    import threading
    import time

    from src.core.llm import calls

    errors: list[Exception] = []

    def call() -> None:
        token = calls.call_group.set("run-1/b0")
        try:
            calls.run_cli(
                [sys.executable, "-c", "import time; time.sleep(30)"], input="", timeout=60
            )
        except LLMError as exc:
            errors.append(exc)
        finally:
            calls.call_group.reset(token)

    worker = threading.Thread(target=call)
    started = time.monotonic()
    worker.start()
    while not calls._RUNNING.get("run-1/b0"):
        time.sleep(0.02)
    assert calls.cancel_group("run-1") == 1  # the parent group reaches the batch's process
    worker.join(10)
    assert time.monotonic() - started < 10 and "Cancelled" in str(errors[0])
    with pytest.raises(LLMError, match="Cancelled"):  # later calls in the group never start
        token = calls.call_group.set("run-1/b1")
        try:
            calls.run_cli([sys.executable, "-c", "pass"], input="", timeout=5)
        finally:
            calls.call_group.reset(token)
    calls.clear_group("run-1")
    assert not calls.is_cancelled("run-1/b1")
