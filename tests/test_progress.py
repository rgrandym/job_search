"""Live task progress (steps, in-flight model calls, heartbeats) and readable profile copies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.core import progress
from src.core.llm import LLMConfig, make_structured
from src.jobs.models import ProfileSummary, PublicationRecord
from src.jobs.profile_memory import ProfileMemory
from tests.test_webapp import client, ws  # noqa: F401 - shared fixtures


def test_steps_count_up_and_finish_at_the_total() -> None:
    with progress.track("t1", "Tailoring", total=3):
        progress.step("Reading")
        first = progress.snapshot("t1")
        progress.step("Planning")
        progress.step("Exporting")
        progress.step("Checking")  # more steps than planned: the total grows, never overflows
        late = progress.snapshot("t1")
    end = progress.snapshot("t1")
    assert first and first.done == 0 and first.step == "Reading" and not first.finished
    assert late and late.done == 3 and late.total == 4
    assert end and end.finished and end.done == end.total == 4
    assert end.completed == ["Reading", "Planning", "Exporting", "Checking"]


def test_progress_is_a_no_op_without_a_task() -> None:
    progress.step("nothing tracked")
    with progress.model_call("x", "m"):
        pass
    with progress.track(None, "untracked"):
        progress.step("still nothing")
    assert progress.snapshot("missing") is None


def test_model_calls_show_as_in_flight_including_from_worker_threads() -> None:
    seen: list[str | None] = []

    class Slow:
        def generate(self, *, system: str, prompt: str, output_model: type[Any]) -> Any:
            snap = progress.snapshot("t2")
            seen.append(snap.waiting_on if snap else None)
            return output_model()

    llm = make_structured(
        LLMConfig(provider="openai", quality_model="q-model", screening_model="s"),
        "quality",
        None,
        "CV tailoring",
    )
    llm.inner = Slow()  # type: ignore[attr-defined]

    async def run() -> None:
        with progress.track("t2", "Tailoring"):
            await asyncio.to_thread(
                llm.generate, system="s", prompt="p", output_model=PublicationRecord
            )

    asyncio.run(run())
    assert seen == ["CV tailoring (q-model)"]
    end = progress.snapshot("t2")
    assert end and end.waiting_on is None and end.finished


def test_a_failed_task_reports_its_error() -> None:
    with pytest.raises(ValueError), progress.track("t3", "Letter"):
        progress.step("Drafting")
        raise ValueError("guards rejected every paragraph")
    end = progress.snapshot("t3")
    assert end and end.finished and end.error == "guards rejected every paragraph"


def test_profile_build_progress_is_served_while_and_after_it_runs(
    client: TestClient,  # noqa: F811 - the shared fixture
) -> None:
    built = client.post("/api/profile-summary?progress_id=p1", json={"query": {}})
    assert built.status_code == 200
    snap = client.get("/api/progress/p1").json()
    assert snap["finished"] and snap["done"] == snap["total"] and snap["error"] is None
    assert snap["completed"][0] == "Reading your CV"
    assert any(step.startswith("Building the profile with") for step in snap["completed"])
    assert client.get("/api/progress/unknown").status_code == 404


def test_quiet_streams_send_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "HEARTBEAT_S", 0.01)

    async def slow(emit: Any, usage: Any) -> None:
        with progress.model_call("Matching", "m"):
            await asyncio.sleep(0.08)
        await emit("search_progress", {"stage": "done", "message": "Done"})

    async def lines() -> list[dict[str, Any]]:
        response = webapp._stream(slow)
        return [json.loads(chunk) async for chunk in response.body_iterator]  # type: ignore[arg-type]

    events = asyncio.run(lines())
    beats = [e for e in events if e["type"] == "heartbeat"]
    assert beats and beats[0]["waiting_on"] == "Matching (m)"
    assert events[-1]["type"] == "search_progress"


def _summary(**kw: Any) -> ProfileSummary:
    return ProfileSummary(
        headline="Principal scientist",
        seniority="principal",
        years_experience=15,
        core_expertise=["functional genomics"],
        key_skills=[],
        target_roles=["Principal Scientist"],
        not_a_fit=[],
        summary="Scientist.",
        **kw,
    )


def test_profiles_are_kept_as_readable_files_in_sync(tmp_path: Path) -> None:
    out = tmp_path / "profiles"
    memory = ProfileMemory(tmp_path / "m.json", out)
    record = PublicationRecord(count=24, lead_author=11, years="2008-2025", summary="Strong.")
    memory.put("cv1", "any", _summary(publications=record))
    memory.label("cv1", "Jane CV.pdf")
    [file] = list(out.glob("*.md"))
    assert file.name == "Jane_CV_general.md"
    text = file.read_text()
    assert "# Principal scientist" in text and "## Publications" in text
    assert "24 publications · 11 as first, last or corresponding author · 2008-2025" in text

    memory.put("cv1", "medical affairs", _summary())
    assert {p.name for p in out.glob("*.md")} == {"Jane_CV_general.md", "cv1_medical_affairs.md"}
    memory.delete("cv1:any")
    assert [p.name for p in out.glob("*.md")] == ["cv1_medical_affairs.md"]
    ProfileMemory(tmp_path / "m.json", tmp_path / "fresh")  # existing profiles exported on load
    assert [p.name for p in (tmp_path / "fresh").glob("*.md")] == ["cv1_medical_affairs.md"]


def test_stop_returns_at_once_while_a_worker_thread_is_busy() -> None:
    import threading
    import time

    from src.services import search_service

    release = threading.Event()

    async def run() -> tuple[str, float]:
        async with search_service._running("run-stop") as run_id:
            asyncio.get_running_loop().call_later(0.05, search_service.stop_search, run_id)
            t0 = time.monotonic()
            got = await search_service._until_stopped(
                asyncio.to_thread(release.wait, 5), "stopped"
            )
            return str(got), time.monotonic() - t0

    try:
        got, waited = asyncio.run(run())
    finally:
        release.set()  # let the abandoned thread finish
    assert got == "stopped" and waited < 1


def test_stop_cancels_a_step_that_does_not_give_way() -> None:
    """Like Ctrl+C without restarting the app: STOP_GRACE_S after Stop the run is over."""
    import time

    from src.services import search_service

    async def stuck() -> search_service.SearchOutcome:
        await asyncio.sleep(60)  # a step that never checks for Stop
        raise AssertionError("not reached")

    async def run() -> float:
        async with search_service._running("run-hard") as run_id:
            asyncio.get_running_loop().call_later(0.05, search_service.stop_search, run_id)
            t0 = time.monotonic()
            with pytest.raises(search_service.SearchStopped):
                await search_service._stoppable(stuck())
            return time.monotonic() - t0

    assert asyncio.run(run()) < search_service.STOP_GRACE_S + 1.5
