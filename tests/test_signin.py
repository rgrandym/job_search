"""Signing in to a plan CLI from the app: its link is read, a pasted code is passed on."""

from __future__ import annotations

import sys

import pytest

from src.core.llm import signin
from src.core.llm.types import LLMError

# A stand-in for `claude auth login`: prints the link and waits for the code.
CLAUDE = """
import sys
print("Opening browser to sign in…")
print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&x=1")
print("Paste code here if prompted > ", end="", flush=True)
code = sys.stdin.readline().strip()
sys.exit(0 if code == "good-code" else 1)
"""


def test_the_link_is_read_and_a_pasted_code_finishes_the_sign_in() -> None:
    started = signin.start("claude:test", [sys.executable, "-c", CLAUDE])

    assert started.url == "https://claude.com/cai/oauth/authorize?code=true&x=1"
    assert started.opened and started.needs_code
    signin.send_code("claude:test", " good-code ")
    with pytest.raises(LLMError, match="No sign-in is waiting"):
        signin.send_code("claude:test", "again")


def test_a_wrong_code_is_reported() -> None:
    signin.start("claude:bad", [sys.executable, "-c", CLAUDE])
    with pytest.raises(LLMError, match="Sign-in failed"):
        signin.send_code("claude:bad", "wrong")


def test_a_link_without_a_code_prompt_and_a_restart() -> None:
    script = "print('Visit https://auth.openai.com/oauth?x=1 to sign in'); input()"
    first = signin.start("codex", [sys.executable, "-c", script])
    assert (first.url, first.opened, first.needs_code) == (
        "https://auth.openai.com/oauth?x=1",
        False,
        False,
    )
    old = signin._runs["codex"].proc
    signin.start("codex", [sys.executable, "-c", script])  # pressing Sign in again
    assert old.wait(5) != 0  # the earlier sign-in was stopped
    signin._stop("codex")
