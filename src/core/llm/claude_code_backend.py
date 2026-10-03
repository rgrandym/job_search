"""Claude via the official Claude Code CLI, authenticated with the user's Claude plan.

We never read or reuse the CLI's stored credentials. Every call runs `claude -p` (headless
mode) with built-in tools and MCP servers disabled, no session persistence and no user
settings, in an empty temp directory; the CLI handles sign-in. Inherited Anthropic API keys
are stripped from the child environment so usage comes from the Claude plan (Pro/Max), never
from an API account.
Structured output uses `--json-schema`. Tool calling for the agent loop is emulated with the
same strict `{message, tool_calls[]}` turn format as the Codex backend.

For personal, local use: the app drives the user's own signed-in CLI on their machine.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from src.core.llm.anthropic_backend import NO_EFFORT, NO_XHIGH, subscription_env
from src.core.llm.calls import run_cli
from src.core.llm.codex_backend import parse_turn, render_turn, turn_schema
from src.core.llm.types import (
    ChatMessage,
    ChatResponse,
    LLMConfig,
    LLMError,
    ModelUsage,
    Role,
    ToolSpec,
    UsageSink,
)

T = TypeVar("T", bound=BaseModel)
TIMEOUT_S = 600
# Latest plan rate-limit state per window ("five_hour", "seven_day", ...), as reported by the
# CLI's `rate_limit_event`s on each call. Process-local; refreshed by every Claude Code call.
_LIMITS: dict[str, dict[str, Any]] = {}
# Replaces Claude Code's default coding-assistant system prompt for agent turns.
AGENT_SYSTEM = "You are the reasoning model inside a job-search application. Reply in JSON only."


# ------------------------------------------------------------------ CLI discovery


@lru_cache(maxsize=1)
def claude_binary() -> str | None:
    """Find the Claude Code CLI on PATH or in its usual install locations."""
    if found := shutil.which("claude"):
        return found
    home = Path.home()
    candidates = [
        home / ".claude/local/claude",
        home / ".local/bin/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    return next((str(p) for p in candidates if p.exists()), None)


def claude_code_status() -> dict[str, Any]:
    """Installed? Signed in with a Claude plan? (No email or org is returned.)"""
    binary = claude_binary()
    if binary is None:
        return {
            "installed": False,
            "logged_in": False,
            "path": None,
            "subscription": None,
            "message": "Claude Code CLI not found. Install it from https://claude.com/claude-code.",
        }
    try:
        out = subprocess.run(
            [binary, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=20,
            env=subscription_env(),
        )
        info: dict[str, Any] = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        info = {"error": str(exc)}
    return {"installed": True, "path": binary, **_status_view(info)}


def _status_view(info: dict[str, Any]) -> dict[str, Any]:
    plan = info.get("subscriptionType")
    if not info.get("loggedIn"):
        message = info.get("error") or "Not signed in to Claude Code."
        return {"logged_in": False, "subscription": None, "message": message}
    if info.get("authMethod") != "claude.ai":
        # Signed in with a Console account: that is API billing, not the Claude plan.
        return {
            "logged_in": False,
            "subscription": None,
            "message": "Claude Code is signed in with an API account; sign in with your plan.",
        }
    label = f"Claude {str(plan).capitalize()}" if plan else "your Claude plan"
    return {"logged_in": True, "subscription": plan, "message": f"Signed in with {label}."}


def start_login() -> None:
    """Launch `claude auth login --claudeai` (opens the claude.ai sign-in page)."""
    binary = claude_binary()
    if binary is None:
        raise LLMError("Claude Code CLI not found")
    subprocess.Popen(
        [binary, "auth", "login", "--claudeai"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=subscription_env(),
    )


# ------------------------------------------------------------------ exec


def _effort(cfg: LLMConfig, model: str) -> str | None:
    """Haiku has no effort control; 4.6 models top out below xhigh."""
    if model in NO_EFFORT:
        return None
    return "high" if cfg.effort == "xhigh" and model in NO_XHIGH else cfg.effort


def _command(
    binary: str, cfg: LLMConfig, model: str, system: str, schema: dict[str, Any]
) -> list[str]:
    cmd = [
        binary,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",  # required by stream-json; it also carries the plan's rate-limit events
        "--json-schema",
        json.dumps(schema),
        "--tools",
        "",
        "--no-session-persistence",
        # No MCP servers (e.g. claude.ai connectors): their tool schemas add ~20k input
        # tokens to every call, which would burn through the plan's usage limits.
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--system-prompt",
        system,
    ]
    if model:
        cmd += ["--model", model]
    if effort := _effort(cfg, model):
        cmd += ["--effort", effort]
    return cmd


def _events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _record_limits(events: list[dict[str, Any]]) -> None:
    for event in events:
        info = event.get("rate_limit_info") if event.get("type") == "rate_limit_event" else None
        if isinstance(info, dict) and info.get("rateLimitType"):
            _LIMITS[str(info["rateLimitType"])] = {**info, "observed_at": int(time.time())}


def _result(returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Parse `--output-format stream-json`: the final `result` event holds the answer in
    `structured_output`; `rate_limit_event`s update the plan usage view."""
    events = _events(stdout)
    _record_limits(events)
    data = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if data is None:
        tail = "\n".join((stdout + stderr).strip().splitlines()[-8:])
        raise LLMError(f"claude -p failed (exit {returncode}): {tail}")
    if data.get("is_error") or data.get("subtype") != "success":
        detail = data.get("result") or data.get("api_error_status") or data.get("subtype")
        raise LLMError(f"Claude Code error: {detail}")
    if data.get("structured_output") is None:
        raise LLMError("Claude Code returned no structured output")
    return data


def run_claude(
    cfg: LLMConfig, model: str, system: str, prompt: str, schema: dict[str, Any]
) -> dict[str, Any]:
    """Blocking `claude -p` returning the full JSON result."""
    binary = claude_binary()
    if binary is None:
        raise LLMError("Claude Code CLI not found")
    with tempfile.TemporaryDirectory(prefix="jobsearch-claude-") as workdir:
        try:
            proc = run_cli(  # cancellable by the search that started it
                _command(binary, cfg, model, system, schema),
                input=prompt,
                timeout=TIMEOUT_S,
                cwd=workdir,
                env=subscription_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude -p timed out after {TIMEOUT_S}s") from exc
    return _result(proc.returncode, proc.stdout, proc.stderr)


async def run_claude_async(
    cfg: LLMConfig, model: str, system: str, prompt: str, schema: dict[str, Any]
) -> dict[str, Any]:
    """Non-blocking variant for the agent loop."""
    binary = claude_binary()
    if binary is None:
        raise LLMError("Claude Code CLI not found")
    with tempfile.TemporaryDirectory(prefix="jobsearch-claude-") as workdir:
        proc = await asyncio.create_subprocess_exec(
            *_command(binary, cfg, model, system, schema),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=subscription_env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            proc.kill()
            raise
    return _result(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


def claude_code_usage() -> dict[str, Any]:
    """Plan limits seen on the latest calls. Anthropic reports each window's status and reset
    time; utilisation is only included as a window nears its limit."""
    windows = [
        {
            "window": name,
            "status": info.get("status"),
            "resets_at": info.get("resetsAt"),
            "utilization": info.get("utilization"),
            "using_overage": bool(info.get("isUsingOverage")),
            "observed_at": info.get("observed_at"),
        }
        for name, info in sorted(_LIMITS.items())
    ]
    return {
        "windows": windows,
        "updated_at": max((w["observed_at"] or 0 for w in windows), default=None),
    }


def _tokens(data: dict[str, Any]) -> tuple[int, int]:
    """(input incl. cache reads/writes, output) as reported by the CLI."""
    u = data.get("usage") or {}
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(u.get(k) or 0) for k in keys), int(u.get("output_tokens") or 0)


# ------------------------------------------------------------------ providers


class ClaudeCodeStructured:
    """`LLMProvider` backed by `claude -p --json-schema`."""

    def __init__(
        self,
        cfg: LLMConfig,
        role: Role = "screening",
        usage_sink: UsageSink | None = None,
        purpose: str = "structured output",
    ) -> None:
        self.cfg = cfg.for_role(role)
        self.model = cfg.model_for(role)
        self.usage_sink = usage_sink
        self.purpose = purpose

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        schema = output_model.model_json_schema()
        data = run_claude(self.cfg, self.model, system, prompt, schema)
        if self.usage_sink:
            tokens_in, tokens_out = _tokens(data)
            self.usage_sink(
                ModelUsage(
                    model=self.model,
                    input_tokens=tokens_in,
                    output_tokens=tokens_out,
                    purpose=self.purpose,
                )
            )
        try:
            return output_model.model_validate(data["structured_output"])
        except ValidationError as exc:
            raise LLMError(
                f"Claude Code output did not match {output_model.__name__}: {exc}"
            ) from exc


class ClaudeCodeChat:
    """`ChatModel` with tool calling emulated through a strict JSON turn format."""

    def __init__(self, cfg: LLMConfig, role: Role) -> None:
        self.cfg = cfg.for_role(role)
        self.model = cfg.model_for(role)

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        prompt = render_turn(system, messages, tools)
        data = await run_claude_async(
            self.cfg, self.model, AGENT_SYSTEM, prompt, turn_schema(tools)
        )
        message = parse_turn(data["structured_output"])
        tokens_in, tokens_out = _tokens(data)
        return ChatResponse(
            message=message,
            stop_reason="tool_use" if message.tool_calls else "end_turn",
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
