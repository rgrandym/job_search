"""Codex via the official Codex CLI, authenticated with the user's ChatGPT account.

We never read or reuse the tokens in `~/.codex/auth.json`. Every call runs
`codex exec` (non-interactive mode), and the CLI handles sign-in. Structured output uses
`--output-schema`. Tool calling for the agent loop is emulated: each turn, Codex returns
`{message, tool_calls[]}` under a strict schema, given the transcript and the tool catalogue.

Each call is a fresh, ephemeral, read-only Codex session in an empty temp directory.
"""

from __future__ import annotations

import asyncio
import glob
import json
import select
import shutil
import subprocess
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from src.core.llm.calls import run_cli
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
TIMEOUT_S = 600
USAGE_TIMEOUT_S = 20
NO_TOOLS_NOTE = (
    "You are running inside an application, not a coding session: do not run shell commands, "
    "read files or browse. Answer directly from the information provided."
)
# Keywords the strict structured-output mode may reject; Pydantic re-validates afterwards.
_STRIP = {
    "default",
    "title",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


def codex_usage() -> dict[str, Any]:
    """Read signed-in ChatGPT usage through the supported Codex app-server protocol."""
    binary = codex_binary()
    if binary is None:
        raise LLMError("Codex CLI not found")
    proc = subprocess.Popen(
        [binary, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        responses = _read_usage_responses(proc)
        return _usage_view(responses[2], responses[3])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _read_usage_responses(proc: subprocess.Popen[str]) -> dict[int, dict[str, Any]]:
    if proc.stdin is None or proc.stdout is None:
        raise LLMError("Codex app-server did not open stdio")
    requests = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "job-search", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized"},
        {
            "id": 2,
            "method": "account/rateLimits/read",
            "params": {"excludeResetCreditDetails": True},
        },
        {"id": 3, "method": "account/usage/read", "params": None},
    ]
    for request in requests:
        proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    responses: dict[int, dict[str, Any]] = {}
    deadline = time.monotonic() + USAGE_TIMEOUT_S
    while len(responses) < 3:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMError("Timed out reading Codex account usage")
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            raise LLMError("Timed out reading Codex account usage")
        line = proc.stdout.readline()
        if not line:
            raise LLMError("Codex app-server stopped before returning usage")
        message = json.loads(line)
        if isinstance(message.get("id"), int):
            if "error" in message:
                raise LLMError(f"Codex usage request failed: {message['error']}")
            responses[message["id"]] = message.get("result") or {}
    return responses


def _usage_view(rate_response: dict[str, Any], usage_response: dict[str, Any]) -> dict[str, Any]:
    limits = rate_response.get("rateLimits") or {}

    def window(name: str) -> dict[str, Any] | None:
        value = limits.get(name)
        if not value:
            return None
        used = int(value.get("usedPercent", 0))
        return {
            "used_percent": used,
            "remaining_percent": max(0, 100 - used),
            "window_minutes": value.get("windowDurationMins"),
            "resets_at": value.get("resetsAt"),
        }

    credits = limits.get("credits") or {}
    summary = usage_response.get("summary") or {}
    return {
        "plan_type": limits.get("planType"),
        "ordinary_usage_allowed": rate_response.get("ordinaryUsageAllowed"),
        "primary": window("primary"),
        "secondary": window("secondary"),
        "credits": {
            "has_credits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        },
        "lifetime_tokens": summary.get("lifetimeTokens"),
        "updated_at": int(time.time()),
    }


# ------------------------------------------------------------------ CLI discovery


@lru_cache(maxsize=1)
def codex_binary() -> str | None:
    """Find Codex on PATH or in the VS Code/Cursor extension."""
    if found := shutil.which("codex"):
        return found
    home = Path.home()
    patterns = [
        str(home / ".vscode/extensions/openai.chatgpt-*/bin/*/codex"),
        str(home / ".cursor/extensions/openai.chatgpt-*/bin/*/codex"),
        "/Applications/Codex.app/Contents/Resources/codex",
    ]
    candidates = {p for pat in patterns for p in glob.glob(pat)}
    return max(candidates, key=lambda p: Path(p).stat().st_mtime) if candidates else None


def codex_status() -> dict[str, Any]:
    """Installed? Logged in? (Shown in the Settings dialog.)"""
    binary = codex_binary()
    if binary is None:
        return {
            "installed": False,
            "logged_in": False,
            "path": None,
            "message": "Codex CLI not found. Install the Codex VS Code extension or "
            "`npm i -g @openai/codex`.",
        }
    try:
        out = subprocess.run(
            [binary, "login", "status"], capture_output=True, text=True, timeout=20
        )
        text = (out.stdout + out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        text = f"error: {exc}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    status_lines = [line for line in lines if not line.lower().startswith("warning:")]
    return {
        "installed": True,
        "logged_in": any(line.lower().startswith("logged in") for line in lines),
        "path": binary,
        "message": (status_lines or lines or [""])[0],
    }


def start_login() -> None:
    """Launch `codex login` (opens the ChatGPT sign-in page in the browser)."""
    binary = codex_binary()
    if binary is None:
        raise LLMError("Codex CLI not found")
    subprocess.Popen(
        [binary, "login"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def list_codex_models() -> list[dict[str, Any]]:
    """Models available to the signed-in account (`codex debug models`)."""
    binary = codex_binary()
    if binary is None:
        return []
    out = subprocess.run([binary, "debug", "models"], capture_output=True, text=True, timeout=30)
    data = json.loads(out.stdout or "{}")
    return [m for m in data.get("models", []) if m.get("visibility") == "list"]


# ------------------------------------------------------------------ schema + exec


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic JSON Schema → OpenAI strict-mode schema (all keys required, optional → nullable).

    Walks schema nodes explicitly, so property *names* such as "title" are never stripped.
    """

    def node(n: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in n.items() if k not in _STRIP}
        if "$defs" in out:
            out["$defs"] = {name: node(d) for name, d in out["$defs"].items()}
        if isinstance(out.get("items"), dict):
            out["items"] = node(out["items"])
        for key in ("anyOf", "allOf", "oneOf"):
            if key in out:
                out[key] = [node(x) for x in out[key]]
        if out.get("type") == "object" or "properties" in out:
            required = set(n.get("required", []))
            props: dict[str, Any] = {}
            for name, sub in out.get("properties", {}).items():
                sub = node(sub)
                props[name] = (
                    sub
                    if name in required or _allows_null(sub)
                    else {"anyOf": [sub, {"type": "null"}]}
                )
            out.update(
                type="object", properties=props, required=list(props), additionalProperties=False
            )  # free-form dicts become empty objects
        return out

    return node(schema)


def _allows_null(sub: dict[str, Any]) -> bool:
    return sub.get("type") == "null" or any(
        isinstance(s, dict) and s.get("type") == "null" for s in sub.get("anyOf", [])
    )


def _drop_nulls(value: Any) -> Any:
    """Remove null-valued keys so Pydantic applies the field defaults."""
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


def _effort(cfg: LLMConfig) -> str:
    return {"max": "xhigh"}.get(cfg.effort, cfg.effort)


def _command(
    binary: str, cfg: LLMConfig, model: str, workdir: Path, schema: Path, out: Path
) -> list[str]:
    cmd = [
        binary,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "-s",
        "read-only",
        "-C",
        str(workdir),
        "-c",
        f'model_reasoning_effort="{_effort(cfg)}"',
        "--output-schema",
        str(schema),
        "-o",
        str(out),
        "-",
    ]
    if model:
        cmd[2:2] = ["-m", model]
    return cmd


def _prepare(schema: dict[str, Any]) -> tuple[Path, Path, Path]:
    workdir = Path(tempfile.mkdtemp(prefix="jobsearch-codex-"))
    schema_path, out_path = workdir / "schema.json", workdir / "out.json"
    schema_path.write_text(json.dumps(strict_schema(schema)), encoding="utf-8")
    return workdir, schema_path, out_path


def _result(workdir: Path, out_path: Path, returncode: int, log: str) -> Any:
    try:
        if returncode != 0 or not out_path.exists():
            tail = "\n".join(log.strip().splitlines()[-8:])
            raise LLMError(f"codex exec failed (exit {returncode}): {tail}")
        return json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LLMError(f"Codex returned invalid JSON: {exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_codex(cfg: LLMConfig, model: str, prompt: str, schema: dict[str, Any]) -> Any:
    """Blocking `codex exec` returning the parsed JSON answer."""
    binary = codex_binary()
    if binary is None:
        raise LLMError("Codex CLI not found")
    workdir, schema_path, out_path = _prepare(schema)
    try:
        proc = run_cli(  # cancellable by the search that started it
            _command(binary, cfg, model, workdir, schema_path, out_path),
            input=prompt,
            timeout=TIMEOUT_S,
        )
    except LLMError:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise LLMError(f"codex exec timed out after {TIMEOUT_S}s") from exc
    return _result(workdir, out_path, proc.returncode, proc.stdout + proc.stderr)


async def run_codex_async(cfg: LLMConfig, model: str, prompt: str, schema: dict[str, Any]) -> Any:
    """Non-blocking variant for the agent loop."""
    binary = codex_binary()
    if binary is None:
        raise LLMError("Codex CLI not found")
    workdir, schema_path, out_path = _prepare(schema)
    proc = await asyncio.create_subprocess_exec(
        *_command(binary, cfg, model, workdir, schema_path, out_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return _result(workdir, out_path, proc.returncode or 0, stdout.decode(errors="replace"))


# ------------------------------------------------------------------ providers


class CodexStructured:
    """`LLMProvider` backed by `codex exec --output-schema`."""

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
        full = f"{NO_TOOLS_NOTE}\n\n{system}\n\n{prompt}"
        for attempt in range(2):
            try:
                data = run_codex(self.cfg, self.model, full, output_model.model_json_schema())
            except LLMError as exc:
                if attempt or not str(exc).startswith("Codex returned invalid JSON:"):
                    raise
                full += "\n\nYour previous response was invalid JSON. Return valid schema JSON."
                continue
            if self.usage_sink:
                self.usage_sink(
                    ModelUsage(
                        model=self.model,
                        input_tokens=_estimate_tokens(full),
                        output_tokens=_estimate_tokens(json.dumps(data)),
                        estimated=True,
                        purpose=self.purpose,
                    )
                )
            try:
                return output_model.model_validate(_drop_nulls(data))
            except ValidationError as exc:
                if attempt:
                    raise LLMError(
                        f"Codex output did not match {output_model.__name__}: {exc}"
                    ) from exc
                full += f"\n\nYour previous response did not match the schema: {exc}. Fix it."
        raise LLMError("Codex returned no structured output")


class _Turn(BaseModel):
    message: str
    tool_calls: list[dict[str, str]]


class CodexChat:
    """`ChatModel` with tool calling emulated through a strict JSON turn format."""

    def __init__(self, cfg: LLMConfig, role: Role) -> None:
        self.cfg = cfg.for_role(role)
        self.model = cfg.model_for(role)

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        prompt = render_turn(system, messages, tools)
        data = await run_codex_async(self.cfg, self.model, prompt, turn_schema(tools))
        message = parse_turn(data)
        return ChatResponse(
            message=message,
            stop_reason="tool_use" if message.tool_calls else "end_turn",
            input_tokens=_estimate_tokens(prompt),
            output_tokens=_estimate_tokens(json.dumps(data)),
            usage_estimated=True,
        )


def turn_schema(tools: list[ToolSpec]) -> dict[str, Any]:
    """JSON Schema of one emulated agent turn: `{message, tool_calls[]}`."""
    names = [t.name for t in tools]
    call_item: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", **({"enum": names} if names else {})},
            "arguments_json": {"type": "string", "description": "JSON-encoded arguments"},
        },
        "required": ["name", "arguments_json"],
    }
    return {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "tool_calls": {"type": "array", "items": call_item},
        },
        "required": ["message", "tool_calls"],
    }


def parse_turn(data: Any) -> ChatMessage:
    """Decode an emulated turn into an assistant message with real `ToolCall`s."""
    turn = _Turn.model_validate(data)
    calls = []
    for c in turn.tool_calls:
        try:
            args = json.loads(c.get("arguments_json") or "{}")
        except json.JSONDecodeError:
            args = {"_invalid_json": c.get("arguments_json")}
        calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:10]}",
                name=c.get("name", ""),
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return ChatMessage(role="assistant", content=turn.message, tool_calls=calls)


def _estimate_tokens(text: str) -> int:
    """Conservative display estimate for Codex CLI, which does not expose usage."""
    return max(1, (len(text) + 3) // 4)


def render_turn(system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> str:
    """Flatten system prompt, tool catalogue and transcript into one CLI prompt."""
    catalogue = (
        "\n".join(
            f"- {t.name}: {t.description}\n  arguments schema: {json.dumps(t.parameters)}"
            for t in tools
        )
        or "(no tools)"
    )
    lines: list[str] = []
    for m in messages:
        if m.role == "user":
            lines.append(f"[user]\n{m.content}")
        elif m.role == "assistant":
            calls = "".join(
                f"\n  -> call {c.name} (id {c.id}) {json.dumps(c.arguments)}" for c in m.tool_calls
            )
            lines.append(f"[assistant]\n{m.content}{calls}")
        else:
            status = "error" if m.is_error else "ok"
            lines.append(f"[tool result {m.tool_call_id} ({status})]\n{m.content}")
    return (
        f"{NO_TOOLS_NOTE}\n\n<instructions>\n{system}\n</instructions>\n\n"
        f"<tools>\nYou can call these application tools by listing them in `tool_calls` "
        f"(`arguments_json` = the JSON-encoded arguments object). Calls in one turn run in "
        f"parallel and their results come back in the next turn.\n{catalogue}\n</tools>\n\n"
        f"<transcript>\n" + "\n\n".join(lines) + "\n</transcript>\n\n"
        "Write the next assistant turn. Either call tools (put a short note in `message`), or "
        "give your final answer in `message` with an empty `tool_calls` list."
    )
