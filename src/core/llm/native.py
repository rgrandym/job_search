"""Shared runner for CLIs that run their own agent loop (Claude Code, Codex).

The CLI gets the user's turn on stdin and calls the app's tools through its MCP endpoint;
its JSON-lines output is handed to the backend's parser line by line as it arrives. The user's
Stop kills the process.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.core.llm.types import LLMError

NATIVE_TIMEOUT_S = 3600  # a turn may run a full search
STREAM_LIMIT = 64 * 1024 * 1024  # JSON lines carry whole tool results
MCP_SERVER = "jobsearch"

OnEvent = Callable[[dict[str, Any]], Awaitable[None]]


async def _read(stream: asyncio.StreamReader, on_event: OnEvent) -> None:
    while line := await stream.readline():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            await on_event(event)


async def _kill_when(proc: asyncio.subprocess.Process, cancelled: Callable[[], bool]) -> None:
    while proc.returncode is None:
        if cancelled():
            proc.kill()
            return
        await asyncio.sleep(0.3)


async def run_streaming(
    cmd: list[str],
    prompt: str,
    workdir: Path,
    env: dict[str, str] | None,
    on_event: OnEvent,
    cancelled: Callable[[], bool],
) -> tuple[int, str]:
    """Run `cmd` with `prompt` on stdin; returns (exit code, stderr). Raises when stopped."""
    workdir.mkdir(parents=True, exist_ok=True)  # CLIs keep sessions per working directory
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=env,
        limit=STREAM_LIMIT,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    watcher = asyncio.create_task(_kill_when(proc, cancelled))
    stderr = asyncio.create_task(proc.stderr.read())
    try:
        await asyncio.wait_for(_read(proc.stdout, on_event), NATIVE_TIMEOUT_S)
        code = await proc.wait()
    finally:
        watcher.cancel()
        if proc.returncode is None:
            proc.kill()
    if cancelled():
        raise LLMError("Stopped")
    return code, (await stderr).decode(errors="replace")


class TextRelay:
    """Passes on text the model writes before calling a tool; holds the latest text, which is
    the final answer when the turn ends."""

    def __init__(self, on_text: Callable[[str], Awaitable[None]]) -> None:
        self.on_text = on_text
        self.held: list[str] = []

    async def text(self, texts: list[str]) -> None:
        for text in self.held:
            await self.on_text(text)
        self.held = texts

    async def tool_use(self, texts: list[str] | None = None) -> None:
        for text in self.held + (texts or []):
            await self.on_text(text)
        self.held = []

    @property
    def answer(self) -> str:
        return "\n\n".join(self.held)
