"""Several Claude accounts: calls move to the next account when one's limit is spent."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.core.llm import LLMConfig, LLMError, claude_accounts, usage
from src.core.llm import claude_code_backend as backend
from src.core.llm.signin import SignInStart
from src.services import connections
from src.services.workspace import Workspace


class Answer(BaseModel):
    value: str


def _config() -> LLMConfig:
    return LLMConfig(
        provider="claude_code", quality_model="claude-sonnet-5-5", screening_model="haiku"
    )


def _ok() -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {"value": "ok"},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
    )


def _spent(resets: float) -> str:
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {"rateLimitType": "five_hour", "status": "rejected", "resetsAt": resets},
    }
    result = {"type": "result", "subtype": "error", "is_error": True, "result": "limit reached"}
    return f"{json.dumps(event)}\n{json.dumps(result)}"


@pytest.fixture
def two_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The default sign-in plus a second account; returns the folders each call used."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_accounts.configure(tmp_path / "data")
    claude_accounts.add("Work", None)
    monkeypatch.setattr(backend, "claude_binary", lambda: "/bin/claude")
    return []


def _fake_cli(monkeypatch: pytest.MonkeyPatch, used: list[str], spent: set[str]) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        folder = kwargs["env"].get("CLAUDE_CONFIG_DIR", "default")
        used.append(folder)
        out = _spent(4102444800) if folder in spent or "*" in spent else _ok()
        return subprocess.CompletedProcess(cmd, 1 if folder in spent else 0, out, "")

    monkeypatch.setattr(backend, "run_cli", fake_run)


def test_a_spent_account_hands_calls_to_the_next(
    two_accounts: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = str(tmp_path / ".claude-work")
    _fake_cli(monkeypatch, two_accounts, spent={"default"})
    model = backend.ClaudeCodeStructured(_config())

    assert model.generate(system="s", prompt="p", output_model=Answer).value == "ok"
    assert two_accounts == ["default", work]
    assert claude_accounts.active().label == "Work"  # the next call goes straight there
    model.generate(system="s", prompt="p", output_model=Answer)
    assert two_accounts == ["default", work, work]
    assert usage.claude_windows(account="default").windows[0].status == "rejected"
    assert usage.exhausted_until("claude_code") is None  # the provider is not spent


def test_every_account_spent_is_a_plan_limit_with_the_earliest_reset(
    two_accounts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(monkeypatch, two_accounts, spent={"*"})
    with pytest.raises(LLMError) as caught:
        backend.run_claude(_config(), "haiku", "s", "p", {})
    assert caught.value.kind == "limit"
    assert caught.value.resets_at == 4102444800
    assert len(two_accounts) == 2
    with pytest.raises(LLMError, match="every Claude account"):  # no call is tried now
        backend.run_claude(_config(), "haiku", "s", "p", {})
    assert len(two_accounts) == 2


def test_a_signed_out_account_is_skipped_when_another_exists() -> None:
    claude_accounts.add("Work", "/tmp/claude-work-test")
    seen: list[str] = []

    def run(account: claude_accounts.ClaudeAccount) -> str:
        seen.append(account.id)
        if account.id == "default":
            raise LLMError("Not logged in · Please run /login")
        return account.id

    assert claude_accounts.call(run) == "work"
    assert claude_accounts.call(run) == "work"
    assert seen == ["default", "work", "work"]


def test_accounts_are_added_ordered_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude-work").mkdir()
    (tmp_path / ".claude-work" / ".claude.json").write_text("{}")
    claude_accounts.configure(tmp_path / "data")
    assert [a.id for a in claude_accounts.accounts()] == ["default"]
    assert claude_accounts.found_folders() == [str(tmp_path / ".claude-work")]

    added = claude_accounts.add("Work", str(tmp_path / ".claude-work"))
    claude_accounts.add("Second", None)
    assert (tmp_path / ".claude-second").is_dir()
    assert claude_accounts.found_folders() == []
    with pytest.raises(ValueError):
        claude_accounts.add("Again", str(tmp_path / ".claude-work"))
    with pytest.raises(ValueError):
        claude_accounts.add("Default", str(tmp_path / ".claude"))  # the default sign-in

    claude_accounts.move_up(added.id)
    claude_accounts.configure(tmp_path / "data")  # a restart keeps the list and order
    assert [a.id for a in claude_accounts.accounts()] == ["work", "default", "second"]
    env = claude_accounts.env(claude_accounts.accounts()[0])
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-work")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
    assert "CLAUDE_CONFIG_DIR" not in claude_accounts.env(claude_accounts.DEFAULT)

    claude_accounts.remove("default")
    claude_accounts.remove("second")
    assert [a.id for a in claude_accounts.accounts()] == ["work"]
    with pytest.raises(KeyError):
        claude_accounts.remove("missing")


def test_usage_from_before_accounts_belongs_to_the_default(tmp_path: Path) -> None:
    window = {"status": "allowed", "utilization": 0.4, "resetsAt": 4102444800}
    (tmp_path / "usage_claude.json").write_text(
        json.dumps({"five_hour": window, "observed_at": 1.0})
    )
    usage.configure(tmp_path)
    assert usage.claude_windows(account="default").windows[0].used_percent == 40.0
    assert usage.claude_windows(account="work").windows == []


def test_a_chat_resumes_only_on_its_own_account(
    two_accounts: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], str]] = []

    async def fake_stream(
        cmd: list[str], prompt: str, workdir: Path, env: Any, on_event: Any, cancelled: Any
    ) -> tuple[int, str]:
        calls.append((cmd, env.get("CLAUDE_CONFIG_DIR", "default")))
        await on_event(
            {
                "type": "result",
                "subtype": "success",
                "result": "hi",
                "session_id": "s1",
                "usage": {},
            }
        )
        return 0, ""

    monkeypatch.setattr(backend, "run_streaming", fake_stream)

    async def turn(session: str | None) -> str:
        async def on_text(_: str) -> None:
            return None

        out = await backend.run_native_turn(
            _config(), "m", "sys", "p", "http://mcp", session, tmp_path, on_text, lambda: False
        )
        return out.session_id

    assert asyncio.run(turn(None)) == "default/s1"
    assert asyncio.run(turn("default/s1")) == "default/s1"
    assert "--resume" in calls[-1][0] and "s1" in calls[-1][0]
    assert asyncio.run(turn("s0")) == "default/s1"  # an id from before accounts: default
    usage.mark_exhausted(claude_accounts.spent_key("default"), 4102444800)
    with pytest.raises(LLMError) as caught:  # the runtime then starts again from history
        asyncio.run(turn("default/s1"))
    assert caught.value.kind != "limit"
    assert asyncio.run(turn(None)) == "work/s1"
    assert calls[-1][1] == str(tmp_path / ".claude-work")


def test_warning_names_the_next_account(tmp_path: Path) -> None:
    claude_accounts.add("Work", "/tmp/claude-work-test")
    assert [a.label for a in claude_accounts.queue()[1:]] == ["Work"]


def test_account_routes(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.web import app as webapp

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ws = Workspace(settings)
    monkeypatch.setattr(webapp, "get_workspace", lambda: ws)
    signed = {"installed": True, "logged_in": True, "subscription": "pro", "message": "ok"}
    folders: list[str | None] = []

    def status(config_dir: str | None = None, with_email: bool = False) -> dict[str, Any]:
        who = "work@example.com" if config_dir else "me@example.com"
        return {**signed, "email": who} if with_email else signed

    monkeypatch.setattr(backend, "claude_code_status", status)

    def login(config_dir: str | None = None, key: str = "") -> SignInStart:
        folders.append(config_dir)
        return SignInStart(url="https://claude.com/x", opened=True, needs_code=True)

    monkeypatch.setattr(backend, "start_login", login)
    client = TestClient(webapp.app)

    listed = client.get("/api/claude-code/accounts").json()
    assert [a["id"] for a in listed["accounts"]] == ["default"]
    added = client.post("/api/claude-code/accounts", json={"label": "Work"}).json()
    assert [(a["id"], a["active"], a["email"]) for a in added["accounts"]] == [
        ("default", True, "me@example.com"),
        ("work", False, "work@example.com"),
    ]
    assert "email" not in client.get("/api/claude-code/status").json()
    assert client.post("/api/claude-code/accounts", json={"label": "Work"}).status_code == 400
    started = client.post("/api/claude-code/accounts/work/login").json()
    assert (started["url"], started["needs_code"]) == ("https://claude.com/x", True)
    no_wait = client.post("/api/claude-code/accounts/work/code", json={"code": "abc"})
    assert no_wait.status_code == 400  # the fake started no CLI
    assert folders == [str(tmp_path / ".claude-work")]
    moved = client.post("/api/claude-code/accounts/work/up").json()
    assert [a["id"] for a in moved["accounts"]] == ["work", "default"]

    reading = {
        "kind": "five_hour",
        "percent_used": 30,
        "config_dir": str(tmp_path / ".claude-work"),
    }
    other = {**reading, "config_dir": "/not/an/account"}
    assert client.post("/api/usage/claude", json=[reading, other]).json() == {"recorded": 1}
    assert usage.claude_windows(account="work").windows[0].used_percent == 30.0

    view = connections.claude_accounts_view()
    assert view.accounts[0].windows is not None
    left = client.delete("/api/claude-code/accounts/default").json()
    assert [a["id"] for a in left["accounts"]] == ["work"]
    assert client.delete("/api/claude-code/accounts/default").status_code == 404


def test_two_accounts_signed_in_as_the_same_person_are_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_accounts.add("Work", "/tmp/claude-work-test")
    same = {"installed": True, "logged_in": True, "email": "me@example.com", "message": "ok"}
    monkeypatch.setattr(backend, "claude_code_status", lambda *_, **__: same)

    views = connections.account_views()

    assert [(v.email, v.same_as) for v in views] == [
        ("me@example.com", None),
        ("me@example.com", "Claude (default sign-in)"),
    ]


def test_an_idle_account_is_read_once_until_a_call_runs_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = claude_accounts.add("Work", "/tmp/claude-work-test").config_dir
    reads: list[str | None] = []

    def text(config_dir: str | None = None) -> str:
        reads.append(config_dir)
        return "Current week (all models): 100% used · resets 11:59pm (UTC)"

    class Now:  # the background read, run in place so the test can count it
        def __init__(self, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            self.run = lambda: target(*args)

        def start(self) -> None:
            self.run()

    monkeypatch.setattr(backend, "claude_usage_text", text)
    monkeypatch.setattr(usage.threading, "Thread", Now)
    for _ in range(2):
        usage.plan_windows("claude_code", account="work", live=True)
        usage.plan_windows("claude_code", account="default", live=True)
    assert reads == [work, None]

    for state in usage._claude_cli.values():  # long after: only the account in use is read
        state["at"] -= usage.CLAUDE_TTL_S
    usage.plan_windows("claude_code", account="work", live=True)
    usage.plan_windows("claude_code", account="default", live=True)
    assert reads[2:] == [None]

    usage.record_account_tokens("work", 10, 1)  # a call ran on it: read once more
    usage._claude_cli["work"]["at"] -= usage.CODEX_STALE_S
    usage.plan_windows("claude_code", account="work", live=True)
    assert reads[3:] == [work]
