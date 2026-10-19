"""Signing in to a plan CLI (Claude Code, Codex) from the app.

The CLI does the sign-in: it opens the provider's page in the browser and stores the
credentials itself; the app never sees them. Started from the app the CLI has no terminal, so
its output is read here for the sign-in link it prints (shown in Settings, and opened by the
app when the CLI did not open the browser), and its input stays open: Claude's link ends on a
page with a code to paste back, which Settings passes on (`send_code`).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time

from pydantic import BaseModel, ConfigDict

from src.core.llm.types import LLMError

URL_WAIT_S = 10.0  # how long to wait for the CLI to print its link
CODE_WAIT_S = 20.0  # how long a pasted code may take to finish the sign-in
KEEP_S = 15 * 60  # a sign-in left unfinished is stopped after this long
_URL = re.compile(r"https://\S+")
_CODE = re.compile(r"paste (the )?code", re.I)


class SignInStart(BaseModel):
    """What Settings shows after a sign-in started."""

    model_config = ConfigDict(extra="forbid")

    started: bool = True
    url: str | None = None  # the sign-in page, when the CLI printed it
    opened: bool = False  # the CLI says it opened the browser itself
    needs_code: bool = False  # the page shows a code to paste back (Claude)


class _Run:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.text = ""
        self.changed = threading.Condition()
        self.started_at = time.monotonic()

    def read(self) -> None:
        """Collect the CLI's output (it keeps the pipe from filling up while it waits)."""
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            with self.changed:
                if chunk:
                    self.text += chunk.decode(errors="replace")
                self.changed.notify_all()
            if not chunk:
                return

    def wait_for(self, found: re.Pattern[str], seconds: float) -> None:
        end = time.monotonic() + seconds
        with self.changed:
            while not found.search(self.text) and self.proc.poll() is None:
                left = end - time.monotonic()
                if left <= 0:
                    return
                self.changed.wait(left)


_lock = threading.Lock()
_runs: dict[str, _Run] = {}  # "claude:<account>" / "codex" -> the sign-in in progress


def _stop(key: str) -> None:
    run = _runs.pop(key, None)
    if run is not None and run.proc.poll() is None:
        run.proc.kill()


def start(key: str, cmd: list[str], env: dict[str, str] | None = None) -> SignInStart:
    """Start `cmd` (a CLI sign-in) under `key`, replacing one already running there, and
    return the link it prints."""
    with _lock:
        _stop(key)
        for old in [k for k, r in _runs.items() if time.monotonic() - r.started_at > KEEP_S]:
            _stop(old)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        except OSError as exc:
            raise LLMError(f"Could not start the sign-in: {exc}") from exc
        run = _runs[key] = _Run(proc)
    threading.Thread(target=run.read, daemon=True).start()
    run.wait_for(_URL, URL_WAIT_S)
    run.wait_for(_CODE, 1.0)  # the code prompt follows the link
    urls = _URL.findall(run.text)
    return SignInStart(
        url=urls[-1].rstrip(".,)") if urls else None,
        opened=bool(re.search(r"opening browser", run.text, re.I)),
        needs_code=bool(_CODE.search(run.text)),
    )


def send_code(key: str, code: str) -> None:
    """Pass the code the sign-in page showed to the CLI waiting for it."""
    with _lock:
        run = _runs.get(key)
    if run is None or run.proc.poll() is not None or run.proc.stdin is None:
        raise LLMError("No sign-in is waiting for a code. Press Sign in again.")
    try:
        run.proc.stdin.write(code.strip().encode() + b"\n")
        run.proc.stdin.flush()
    except OSError as exc:
        raise LLMError(f"Could not pass the code on: {exc}") from exc
    try:
        code_out = run.proc.wait(CODE_WAIT_S)
    except subprocess.TimeoutExpired:
        return  # still finishing: Settings shows the sign-in once the CLI reports it
    with _lock:
        _runs.pop(key, None)
    if code_out != 0:
        lines = [ln for ln in _URL.sub("", run.text).splitlines() if ln.strip()]
        raise LLMError(f"Sign-in failed: {lines[-1] if lines else f'exit {code_out}'}")
