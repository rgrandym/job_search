"""Live task progress: code reports steps, the web app serves them while the task runs.

A route opens a task with `track(task_id, title, total)`. Code anywhere below it reports with
`step(label)` and `model_call(purpose, model)` through a context variable, which
`asyncio.to_thread` carries into worker threads, so domain code needs no extra parameters.
Without an open task every call is a no-op. The UI polls `snapshot(task_id)` about once a
second: `done / total` is real step progress, and `waiting_on` / `waiting_since` show which
model call is running and for how long, so a slow call is visible as a slow call.

`run(task_id, title, work)` also makes the task stoppable: `stop(task_id)` (the UI's Stop
button) ends it at once. The work is cancelled, its model calls are killed or cut off
mid-request (`llm.calls.cancel_group`; the task id is their call group) and, since a stopped
group refuses new calls, a worker thread left behind ends without spending more. `run` then
raises `TaskStopped`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

KEEP_S = 600  # finished tasks stay readable this long (the last poll may come late)


class TaskProgress(BaseModel):
    """What a running task has done so far."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    step: str = ""
    done: int = 0
    total: int = 0
    started_at: float = Field(default_factory=time.time)
    step_started_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    waiting_on: str | None = None  # the model call in flight, e.g. "CV tailoring (gpt-5)"
    waiting_since: float | None = None
    completed: list[str] = Field(default_factory=list)  # finished steps, oldest first
    finished: bool = False
    error: str | None = None
    now: float = 0.0  # server clock at snapshot time, so the UI can measure elapsed time


_TASKS: dict[str, TaskProgress] = {}
_LOCK = threading.Lock()
_CURRENT: ContextVar[str | None] = ContextVar("progress_task", default=None)
# Stoppable work by task id: its event loop and the asyncio task running it.
_STOPPABLE: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[Any]]] = {}
STOPPED = "Stopped"
T = TypeVar("T")


class TaskStopped(Exception):
    """The user stopped the task (`stop`)."""


def _update(**changes: object) -> None:
    task_id = _CURRENT.get()
    if task_id is None:
        return
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is not None:
            _TASKS[task_id] = task.model_copy(update={**changes, "updated_at": time.time()})


@contextmanager
def track(task_id: str | None, title: str, total: int = 0) -> Iterator[None]:
    """Open a task for the duration of the block (no-op without an id)."""
    if not task_id:
        yield
        return
    now = time.time()
    with _LOCK:
        for key in [k for k, t in _TASKS.items() if t.finished and now - t.updated_at > KEEP_S]:
            del _TASKS[key]
        _TASKS[task_id] = TaskProgress(id=task_id, title=title, total=total)
    token = _CURRENT.set(task_id)
    try:
        yield
    except Exception as exc:
        _update(finished=True, error=str(exc) or type(exc).__name__, waiting_on=None)
        raise
    else:
        with _LOCK:
            task = _TASKS[task_id]
            _TASKS[task_id] = task.model_copy(
                update={
                    "finished": True,
                    "done": max(task.done + (1 if task.step else 0), task.total),
                    "completed": [*task.completed, task.step] if task.step else task.completed,
                    "step": "",
                    "waiting_on": None,
                    "updated_at": time.time(),
                }
            )
    finally:
        _CURRENT.reset(token)


def step(label: str, total: int | None = None) -> None:
    """Start the next step; the previous one counts as done. `total` re-plans the task."""
    task_id = _CURRENT.get()
    if task_id is None:
        return
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        done = task.done + 1 if task.step else task.done
        _TASKS[task_id] = task.model_copy(
            update={
                "step": label,
                "done": done,
                "total": max(total if total is not None else task.total, done + 1),
                "completed": [*task.completed, task.step] if task.step else task.completed,
                "step_started_at": time.time(),
                "updated_at": time.time(),
            }
        )


@contextmanager
def model_call(purpose: str, model: str) -> Iterator[None]:
    """Mark a model call as in flight for the duration of the block."""
    _update(waiting_on=f"{purpose} ({model})" if model else purpose, waiting_since=time.time())
    try:
        yield
    finally:
        _update(waiting_on=None, waiting_since=None)


def snapshot(task_id: str) -> TaskProgress | None:
    """The task's progress now, or None when it is unknown (not started or long finished)."""
    with _LOCK:
        task = _TASKS.get(task_id)
    return task.model_copy(update={"now": time.time()}) if task else None


def active() -> list[TaskProgress]:
    """Tasks still running, oldest first (the dev pane's `scripts/live_status.py` lists them)."""
    now = time.time()
    with _LOCK:
        running = [t for t in _TASKS.values() if not t.finished]
    return [t.model_copy(update={"now": now}) for t in sorted(running, key=lambda t: t.started_at)]


async def run(task_id: str | None, title: str, work: Awaitable[T], total: int = 0) -> T:
    """`await work` as a tracked task that `stop(task_id)` ends at once (raising
    `TaskStopped` here). Model calls made inside, in this coroutine or its worker threads,
    belong to the call group `task_id`. Without an id it simply awaits `work`."""
    from src.core.llm.calls import call_group, clear_group  # llm imports this module

    if not task_id:
        return await work
    with track(task_id, title, total):
        token = call_group.set(task_id)
        try:
            child = asyncio.ensure_future(work)  # copies the context: task and call group
        finally:
            call_group.reset(token)
        _STOPPABLE[task_id] = (asyncio.get_running_loop(), child)
        try:
            await asyncio.wait({child})
        except asyncio.CancelledError:  # the request itself went away
            child.cancel()
            raise
        finally:
            _STOPPABLE.pop(task_id, None)
        if child.cancelled():
            raise TaskStopped(STOPPED)
        result: T = child.result()
        clear_group(task_id)  # a stopped group stays cancelled for threads left behind
        return result


def stop(task_id: str) -> bool:
    """Stop a running task now: its model calls are killed or cut off and its work cancelled.
    False when no such task is running."""
    from src.core.llm.calls import cancel_group

    entry = _STOPPABLE.get(task_id)
    if entry is None:
        return False
    cancel_group(task_id)
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is not None:
            _TASKS[task_id] = task.model_copy(
                update={
                    "finished": True,
                    "error": STOPPED,
                    "waiting_on": None,
                    "updated_at": time.time(),
                }
            )
    loop, child = entry
    loop.call_soon_threadsafe(child.cancel)
    return True
