"""Stop buttons end work at once: model calls and HTTP requests are cut off mid-flight, the
task is cancelled, and threads left behind cannot start new calls."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from src.agents import chat
from src.core import progress
from src.core.config import Settings
from src.core.llm.calls import abortable_client, call_group, cancel_group, is_cancelled
from src.core.llm.types import ChatMessage, ChatResponse, LLMError, ToolSpec
from src.cv.models import MasterCV
from src.jobs.models import JobPosting, MatchResult, SearchQuery
from src.jobs.sources.base import HttpFetcher, SourceError
from src.services.workspace import Workspace


@pytest.fixture
def silent_server() -> Iterator[str]:
    """A local server that accepts connections and never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    held: list[Any] = []
    stop = threading.Event()

    def accept() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept())
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/"
    stop.set()
    thread.join(1)
    srv.close()


def _in_thread(group: str, fn: Any) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def run() -> None:
        call_group.set(group)
        started = time.monotonic()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            out["error"] = exc
        out["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def test_a_model_call_in_flight_is_cut_off(silent_server: str) -> None:
    def call() -> None:
        with abortable_client(httpx.Client, timeout=30) as client:
            client.post(silent_server, json={})

    thread, out = _in_thread("stop-test-1", call)
    time.sleep(0.3)
    assert cancel_group("stop-test-1") == 1
    thread.join(2)
    assert not thread.is_alive() and isinstance(out["error"], LLMError)
    assert str(out["error"]) == "Cancelled" and out["seconds"] < 2


def test_a_stopped_group_refuses_new_calls() -> None:
    cancel_group("stop-test-2")
    thread, out = _in_thread("stop-test-2", lambda: abortable_client(httpx.Client).__enter__())
    thread.join(2)
    assert str(out["error"]) == "Cancelled"


def test_a_job_source_request_in_flight_is_cut_off(silent_server: str, settings: Settings) -> None:
    http = HttpFetcher(settings, delay_s=0)
    thread, out = _in_thread("stop-test-3", lambda: http.get(silent_server))
    time.sleep(0.3)
    cancel_group("stop-test-3")
    thread.join(2)
    assert isinstance(out["error"], SourceError) and "stopped" in str(out["error"])


def test_a_stopped_task_ends_at_once_and_its_thread_spends_nothing_more() -> None:
    later: list[str] = []

    def slow_work() -> None:  # a worker thread between model calls
        while not is_cancelled(call_group.get()):
            time.sleep(0.05)
        try:
            with abortable_client(httpx.Client):
                later.append("called")
        except LLMError:
            later.append("refused")

    async def main() -> float:
        async def stop_soon() -> None:
            await asyncio.sleep(0.2)
            assert progress.stop("task-a")

        stopper = asyncio.ensure_future(stop_soon())
        started = time.monotonic()
        with pytest.raises(progress.TaskStopped):
            await progress.run("task-a", "Tailoring", asyncio.to_thread(slow_work))
        await stopper
        return time.monotonic() - started

    assert asyncio.run(main()) < 1
    snap = progress.snapshot("task-a")
    assert snap is not None and snap.finished and snap.error == progress.STOPPED
    assert later == ["refused"]
    assert not progress.stop("task-a") and not progress.stop("unknown")


def test_stop_ends_a_tailoring_request_with_stopped(
    settings: Settings, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.web import app as webapp

    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)

    async def slow_tailoring(*_: Any, **__: Any) -> Any:
        await asyncio.sleep(30)

    monkeypatch.setattr(webapp.cv_service, "tailor_to_job", slow_tailoring)
    job = JobPosting(id="j1", title="ML Engineer", company="Orbit")
    body = {"result": MatchResult(job=job).model_dump(mode="json")}

    async def main() -> tuple[httpx.Response, float]:
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
            started = time.monotonic()
            request = asyncio.ensure_future(
                client.post("/api/jobs/j1/tailor?progress_id=t-1", json=body)
            )
            await asyncio.sleep(0.2)
            stopped = await client.post("/api/progress/t-1/stop")
            assert stopped.json() == {"stopped": True}
            return await request, time.monotonic() - started

    response, seconds = asyncio.run(main())
    assert response.status_code == 409 and response.json() == {"detail": "Stopped"}
    assert seconds < 2


class _SlowChat:
    """A chat model whose answer never comes (a long model call in flight)."""

    async def chat(
        self, *, system: str, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ChatResponse:
        await asyncio.sleep(30)
        raise AssertionError("not reached")


def test_assistant_stop_ends_a_model_call_in_flight(
    settings: Settings, master_cv: MasterCV, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(settings)
    ws.master_cv, ws.active_cv_id = master_cv, "master"
    monkeypatch.setattr(ws, "chat", lambda *a: _SlowChat())
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    session = chat.ChatSession()

    async def main() -> float:
        turn = asyncio.ensure_future(
            chat.handle_user_message(ws, session, "Hello", SearchQuery(), True, emit)
        )
        await asyncio.sleep(0.2)
        started = time.monotonic()
        group = session.ctx.group if session.ctx else ""
        chat.cancel(session)
        turn.cancel()  # what the chat socket does on Stop
        await turn
        assert is_cancelled(group)
        return time.monotonic() - started

    assert asyncio.run(main()) < 1
    assert ("done", {"tokens": {"input": 0, "output": 0}, "cancelled": True}) in events
    assert session.messages == []  # the stopped turn is rolled back
