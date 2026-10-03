"""Cancellable CLI model calls, grouped by the search (or screening batch) that started them.

The Codex and Claude Code backends run one CLI process per call, from worker threads. A caller
sets `call_group` (e.g. a search's run id, or "run-id/b3" for one batch); asyncio copies it into
`asyncio.to_thread`, so every process the call starts is registered under that group and
`cancel_group` can kill them all. Killing the process stops it spending the plan's allowance.
"""

from __future__ import annotations

import subprocess
import threading
from contextvars import ContextVar

from src.core.llm.types import LLMError

call_group: ContextVar[str | None] = ContextVar("llm_call_group", default=None)

_LOCK = threading.Lock()
_RUNNING: dict[str, set[subprocess.Popen[str]]] = {}
_CANCELLED: set[str] = set()


def _in(group: str, prefix: str) -> bool:
    return group == prefix or group.startswith(f"{prefix}/")


def is_cancelled(group: str | None) -> bool:
    with _LOCK:
        return group is not None and any(_in(group, p) for p in _CANCELLED)


def cancel_group(prefix: str) -> int:
    """Mark `prefix` (and its sub-groups) cancelled and kill their running processes."""
    with _LOCK:
        _CANCELLED.add(prefix)
        procs = [p for g, ps in _RUNNING.items() if _in(g, prefix) for p in ps]
    for proc in procs:
        proc.kill()
    return len(procs)


def clear_group(prefix: str) -> None:
    """Forget a finished group's cancellation mark."""
    with _LOCK:
        _CANCELLED.difference_update({p for p in _CANCELLED if _in(p, prefix)})


def run_cli(
    cmd: list[str],
    *,
    input: str,
    timeout: float,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """`subprocess.run` that the current `call_group` can cancel. Raises `LLMError` when
    cancelled and `subprocess.TimeoutExpired` (after killing the process) on timeout."""
    group = call_group.get()
    if is_cancelled(group):
        raise LLMError("Cancelled")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )
    key = group or ""
    with _LOCK:
        _RUNNING.setdefault(key, set()).add(proc)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        with _LOCK:
            _RUNNING.get(key, set()).discard(proc)
    if is_cancelled(group):
        raise LLMError("Cancelled")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
