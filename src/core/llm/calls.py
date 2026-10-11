"""Cancellable model calls, grouped by the task (search, screening batch, tailoring, letter,
profile build...) that started them.

The Codex and Claude Code backends run one CLI process per call, from worker threads; the API
backends make one HTTP request per call. A caller sets `call_group` (e.g. a search's run id,
"run-id/b3" for one batch, or a progress task id); asyncio copies it into `asyncio.to_thread`,
so every process or request the call starts is registered under that group and `cancel_group`
stops them all at once: processes are killed and HTTP requests have their sockets shut down
mid-call (`abortable_client`). A cancelled group refuses new calls, so work abandoned by a
stopped task ends on its own instead of spending more.
"""

from __future__ import annotations

import socket
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any, Protocol, TypeVar

import httpcore

from src.core.llm.types import LLMError

call_group: ContextVar[str | None] = ContextVar("llm_call_group", default=None)


class _Closable(Protocol):
    def close(self) -> None: ...


C = TypeVar("C", bound=_Closable)
_LOCK = threading.Lock()
_RUNNING: dict[str, set[subprocess.Popen[str]]] = {}
_ABORTS: dict[str, set[Callable[[], None]]] = {}  # open HTTP requests, by group
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
        aborts = [a for g, fs in _ABORTS.items() if _in(g, prefix) for a in fs]
    for proc in procs:
        proc.kill()
    for abort in aborts:
        abort()
    return len(procs) + len(aborts)


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


class _Stream(httpcore.NetworkStream):
    """A connection whose socket `_Backend.abort` can shut down from another thread; TLS
    upgrades are tracked too (the TLS socket replaces the plain one)."""

    def __init__(self, inner: httpcore.NetworkStream, backend: _Backend) -> None:
        self.inner = inner
        self.backend = backend
        backend.track(inner)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self.inner.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.inner.write(buffer, timeout)

    def close(self) -> None:
        self.inner.close()

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        return _Stream(self.inner.start_tls(ssl_context, server_hostname, timeout), self.backend)

    def get_extra_info(self, info: str) -> Any:
        return self.inner.get_extra_info(info)


class _Backend(httpcore.NetworkBackend):
    """httpcore's sync network backend, remembering each connection so it can be aborted."""

    def __init__(self, inner: Any = None) -> None:
        self.inner = inner if inner is not None else httpcore.SyncBackend()
        self.streams: list[httpcore.NetworkStream] = []
        self.aborted = False

    def track(self, stream: httpcore.NetworkStream) -> None:
        self.streams.append(stream)
        if self.aborted:
            self.abort()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        stream = self.inner.connect_tcp(host, port, timeout, local_address, socket_options)
        return _Stream(stream, self)

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.NetworkStream:
        return _Stream(self.inner.connect_unix_socket(path, timeout, socket_options), self)

    def sleep(self, seconds: float) -> None:
        self.inner.sleep(seconds)

    def abort(self) -> None:
        """Shut down every socket: a request blocked reading the answer fails at once."""
        self.aborted = True
        for stream in self.streams:
            sock = stream.get_extra_info("socket")
            if sock is None:
                continue
            # OSError: already closed, or the plain socket a TLS socket took over.
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def _attach(client: Any, backend: _Backend) -> None:
    """Route the client's connections (direct and through any proxy) through `backend`. Uses
    httpx's private transport pool; if that ever changes, calls still work, unabortable."""
    mounts = getattr(client, "_mounts", {}) or {}
    for transport in [getattr(client, "_transport", None), *mounts.values()]:
        pool = getattr(transport, "_pool", None)
        if pool is not None and hasattr(pool, "_network_backend"):
            pool._network_backend = backend


def make_abortable(client: Any, network: Any = None) -> _Backend:
    """Route a long-lived client's connections through an abortable backend; use
    `abort_scope` around each request so the group running it can cut it off."""
    backend = _Backend(network)
    _attach(client, backend)
    return backend


@contextmanager
def abort_scope(backend: _Backend | None) -> Iterator[None]:
    """While the block runs, cancelling the current call group shuts `backend`'s connections
    down (every request on that client, so use it for clients one task owns)."""
    group = call_group.get()
    if backend is None or group is None:
        yield
        return
    with _LOCK:
        _ABORTS.setdefault(group, set()).add(backend.abort)
    try:
        yield
    finally:
        with _LOCK:
            _ABORTS.get(group, set()).discard(backend.abort)


@contextmanager
def abortable_client(make: Callable[..., C], network: Any = None, **kwargs: Any) -> Iterator[C]:
    """A client made by `make(**kwargs)` (e.g. `httpx.Client`) for one model call
    that `cancel_group` can abort mid-request. `network` is the client library's own sync
    network backend (`httpcore2.SyncBackend()` for the Anthropic SDK's `httpx2`). Raises
    `LLMError("Cancelled")` if the group is (or becomes) cancelled."""
    group = call_group.get()
    if is_cancelled(group):
        raise LLMError("Cancelled")
    client = make(**kwargs)
    backend = make_abortable(client, network)
    try:
        with abort_scope(backend):
            yield client
    except Exception as exc:
        if is_cancelled(group):
            raise LLMError("Cancelled") from exc
        raise
    finally:
        client.close()
    if is_cancelled(group):
        raise LLMError("Cancelled")
