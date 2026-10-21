"""Signing in to a plan CLI (Claude Code, Codex) from the app.

The CLI does the sign-in: it opens the provider's page in the browser and stores the
credentials itself; the app never sees them. Started from the app the CLI has no terminal, so
its output is read here for the sign-in link it prints (shown in Settings, and opened by the
app when the CLI did not open the browser), and its input stays open: Claude's link ends on a
page with a code to paste back, which Settings passes on (`send_code`). A CLI that draws on a
terminal only (`claude setup-token`) runs on a pseudo-terminal (`tty=True`), and its screen
codes are taken out of what is read. `on_done` gets the CLI's whole output when it ends, for
a sign-in whose result the CLI prints (the long-lived Claude token) rather than stores.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from src.core.llm.types import LLMError

URL_WAIT_S = 10.0  # how long to wait for the CLI to print its link
CODE_WAIT_S = 20.0  # how long a pasted code may take to finish the sign-in
KEEP_S = 15 * 60  # a sign-in left unfinished is stopped after this long
_URL = re.compile(r"https://\S+")
_CODE = re.compile(r"paste\s*(the\s*)?code", re.I)
# A link drawn as an OSC 8 hyperlink: its target is whole even when the text wraps.
_LINK = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b]+)")
_RIGHT = re.compile(r"\x1b\[(\d*)C")  # "cursor right": how Ink draws spaces
_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[@-_]")
TTY_COLUMNS = 1000  # wide enough that no link or token wraps

OnDone = Callable[[str, int], str | None]  # (output, exit code) -> an error, or None


def plain(raw: str) -> str:
    """Terminal output as text: cursor moves to the right become spaces, other codes go."""
    text = _RIGHT.sub(lambda m: " " * int(m.group(1) or 1), raw)
    return _ESCAPE.sub("", text).replace("\r", "")


class SignInStart(BaseModel):
    """What Settings shows after a sign-in started."""

    model_config = ConfigDict(extra="forbid")

    started: bool = True
    url: str | None = None  # the sign-in page, when the CLI printed it
    opened: bool = False  # the CLI says it opened the browser itself
    needs_code: bool = False  # the page shows a code to paste back (Claude)


class _Run:
    def __init__(
        self, proc: subprocess.Popen[bytes], fd: int, tty: bool, on_done: OnDone | None
    ) -> None:
        self.proc = proc
        self.fd = fd  # the CLI's output (and, on a terminal, its input)
        self.tty = tty
        self.on_done = on_done
        self.raw = ""
        self.error: str | None = None
        self.changed = threading.Condition()
        self.started_at = time.monotonic()
        self.reader = threading.Thread(target=self.read, daemon=True)

    @property
    def text(self) -> str:
        return plain(self.raw) if self.tty else self.raw

    def links(self) -> list[str]:
        return _LINK.findall(self.raw) or _URL.findall(self.text)

    def read(self) -> None:
        """Collect the CLI's output (it keeps the pipe from filling up while it waits)."""
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:  # a terminal whose CLI has ended
                chunk = b""
            with self.changed:
                if chunk:
                    self.raw += chunk.decode(errors="replace")
                self.changed.notify_all()
            if not chunk:
                break
        code = self.proc.wait()
        if self.tty:
            os.close(self.fd)
        if self.on_done is not None:
            self.error = self.on_done(self.text, code)

    def write(self, data: bytes) -> None:
        if self.tty:
            os.write(self.fd, data)
            return
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

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


def _spawn(
    cmd: list[str], env: dict[str, str] | None, tty: bool
) -> tuple[subprocess.Popen[bytes], int]:
    """The CLI on pipes, or on a wide pseudo-terminal; with the fd its output is read from."""
    if not tty:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        assert proc.stdout is not None
        return proc, proc.stdout.fileno()
    main, side = pty.openpty()
    fcntl.ioctl(side, termios.TIOCSWINSZ, struct.pack("HHHH", 50, TTY_COLUMNS, 0, 0))
    try:
        proc = subprocess.Popen(
            cmd, stdin=side, stdout=side, stderr=side, start_new_session=True, env=env
        )
    except OSError:
        os.close(main)
        raise
    finally:
        os.close(side)
    return proc, main


def start(
    key: str,
    cmd: list[str],
    env: dict[str, str] | None = None,
    *,
    tty: bool = False,
    on_done: OnDone | None = None,
) -> SignInStart:
    """Start `cmd` (a CLI sign-in) under `key`, replacing one already running there, and
    return the link it prints."""
    with _lock:
        _stop(key)
        for old in [k for k, r in _runs.items() if time.monotonic() - r.started_at > KEEP_S]:
            _stop(old)
        try:
            proc, fd = _spawn(cmd, env, tty)
        except OSError as exc:
            raise LLMError(f"Could not start the sign-in: {exc}") from exc
        run = _runs[key] = _Run(proc, fd, tty, on_done)
    run.reader.start()
    run.wait_for(_URL, URL_WAIT_S)
    run.wait_for(_CODE, 1.0)  # the code prompt follows the link
    urls = run.links()
    return SignInStart(
        url=urls[-1].rstrip(".,)") if urls else None,
        opened=bool(re.search(r"opening\s*browser", run.text, re.I)),
        needs_code=bool(_CODE.search(run.text)),
    )


def send_code(key: str, code: str) -> None:
    """Pass the code the sign-in page showed to the CLI waiting for it."""
    with _lock:
        run = _runs.get(key)
    if run is None or run.proc.poll() is not None:
        raise LLMError("No sign-in is waiting for a code. Press Sign in again.")
    try:
        run.write(code.strip().encode() + (b"\r" if run.tty else b"\n"))
    except OSError as exc:
        raise LLMError(f"Could not pass the code on: {exc}") from exc
    try:
        code_out = run.proc.wait(CODE_WAIT_S)
    except subprocess.TimeoutExpired:
        return  # still finishing: Settings shows the sign-in once the CLI reports it
    with _lock:
        _runs.pop(key, None)
    run.reader.join(5.0)  # `on_done` has seen all of the output
    if run.error:
        raise LLMError(run.error)
    if code_out != 0:
        lines = [ln for ln in _URL.sub("", run.text).splitlines() if ln.strip()]
        raise LLMError(f"Sign-in failed: {lines[-1] if lines else f'exit {code_out}'}")
