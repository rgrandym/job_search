"""Live task progress: code reports steps, the web app serves them while the task runs.

A route opens a task with `track(task_id, title, total)`. Code anywhere below it reports with
`step(label)` and `model_call(purpose, model)` through a context variable, which
`asyncio.to_thread` carries into worker threads, so domain code needs no extra parameters.
Without an open task every call is a no-op. The UI polls `snapshot(task_id)` about once a
second: `done / total` is real step progress, and `waiting_on` / `waiting_since` show which
model call is running and for how long, so a slow call is visible as a slow call.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

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
