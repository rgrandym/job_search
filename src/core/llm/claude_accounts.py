"""Several Claude plan accounts for Claude Code, used in turn when one's limit is spent.

Each account is a Claude Code config folder (`CLAUDE_CONFIG_DIR`): the CLI keeps each folder's
sign-in apart, so two Claude Pro accounts can be signed in at once. The default account is the
CLI's own folder (no `CLAUDE_CONFIG_DIR`). A call runs on the first account, in the user's
order, whose limit is not known to be spent; a limit moves it to the next account, and only when
every account is spent does it fail as a plan limit (which `fallback` then hands to Codex).

Staying signed in: a folder's own sign-in (`claude auth login`, kept in the macOS Keychain)
lasts hours and its refresh token works once, so when the app's parallel calls, VS Code and a
terminal renew it together the losers are signed out. The app therefore signs each account in
with `claude setup-token`: a token for the plan that lasts a year and is never renewed, kept
here per account (`data/claude_tokens.json`, readable by the user only) and given to every
call as `CLAUDE_CODE_OAUTH_TOKEN`. It stays until the user signs the account out in Settings.
The app never reads the folders' own credentials.

The list persists in `data/claude_accounts.json` once `configure(data_dir)` is called; until
then it stays in memory, so tests and CLIs never write into the user's data folder.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, SecretStr

from src.core.llm import usage
from src.core.llm.types import LLMError

R = TypeVar("R")
PROVIDER = "claude_code"
DEFAULT_ID = "default"
FILE = "claude_accounts.json"
TOKENS_FILE = "claude_tokens.json"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_DAYS = 365  # how long `claude setup-token` tokens last
STATUS_TTL_S = 30.0
SIGNED_OUT_S = 10 * 60  # an account that answered "not signed in" is skipped this long


class ClaudeAccount(BaseModel):
    """One Claude sign-in: a Claude Code config folder (None: the CLI's default folder)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    config_dir: str | None = None


class SavedToken(BaseModel):
    """An account's long-lived sign-in (`claude setup-token`)."""

    model_config = ConfigDict(extra="forbid")

    token: SecretStr
    saved_at: float
    email: str | None = None  # who the folder was signed in as then; the token does not say
    failed: str | None = None  # the last call's sign-in error, until a call works again


DEFAULT = ClaudeAccount(id=DEFAULT_ID, label="Claude (default sign-in)")

_lock = threading.Lock()
_dir: Path | None = None
_accounts: list[ClaudeAccount] = []
_status: dict[str, tuple[float, dict[str, Any]]] = {}  # id -> (checked_at, status)
_signed_out: dict[str, float] = {}  # id -> when a call found it signed out
_tokens: dict[str, SavedToken] = {}  # id -> its long-lived sign-in


def configure(data_dir: Path | None) -> None:
    """Persist under `data_dir` from now on (None: memory only), loading what is there."""
    global _dir
    with _lock:
        _dir = data_dir
        _accounts.clear()
        _status.clear()
        _signed_out.clear()
        _tokens.clear()
        if data_dir is None:
            return
        rows = _read(data_dir / FILE).get("accounts")
        for row in rows if isinstance(rows, list) else []:
            try:
                _accounts.append(ClaudeAccount.model_validate(row))
            except ValueError:
                continue
        for account_id, row in _read(data_dir / TOKENS_FILE).items():
            try:
                _tokens[account_id] = SavedToken.model_validate(row)
            except ValueError:
                continue


def _read(path: Path) -> dict[str, Any]:
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _save() -> None:
    """Write the list (caller holds `_lock`)."""
    if _dir is None:
        return
    _dir.mkdir(parents=True, exist_ok=True)
    rows = [a.model_dump() for a in _accounts]
    (_dir / FILE).write_text(json.dumps({"accounts": rows}, indent=2))


def _save_tokens() -> None:
    """Write the tokens, readable by the user only (caller holds `_lock`)."""
    if _dir is None:
        return
    _dir.mkdir(parents=True, exist_ok=True)
    rows = {k: {**t.model_dump(), "token": t.token.get_secret_value()} for k, t in _tokens.items()}
    path = _dir / TOKENS_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(rows, out, indent=2)
    os.chmod(path, 0o600)  # a file made before by hand keeps no wider access


def accounts() -> list[ClaudeAccount]:
    """The accounts in the order they are used; the default sign-in when none were added."""
    with _lock:
        return list(_accounts) or [DEFAULT]


def get(account_id: str) -> ClaudeAccount | None:
    return next((a for a in accounts() if a.id == account_id), None)


def _norm(config_dir: str | None) -> str | None:
    """An absolute folder; the CLI's own `~/.claude` is the default sign-in (None)."""
    if not config_dir or not config_dir.strip():
        return None
    path = Path(config_dir.strip()).expanduser().resolve()
    return None if path == (Path.home() / ".claude").resolve() else str(path)


def by_dir(config_dir: str | None) -> ClaudeAccount | None:
    """The account using `config_dir` (None: the default sign-in)."""
    wanted = _norm(config_dir)
    return next((a for a in accounts() if _norm(a.config_dir) == wanted), None)


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:30] or "account"


def add(label: str, config_dir: str | None = None) -> ClaudeAccount:
    """Add an account after the others. Without a folder it gets a new `~/.claude-<name>`,
    which the user then signs in to (`claude_code_backend.start_login`)."""
    label = label.strip() or f"Claude account {len(accounts()) + 1}"
    folder = _norm(config_dir) if config_dir else str(Path.home() / f".claude-{_slug(label)}")
    if by_dir(folder) is not None:
        raise ValueError("That Claude folder is already in the list.")
    with _lock:
        taken = {a.id for a in _accounts} | {DEFAULT_ID}
        account_id = base = _slug(label)
        n = 1
        while account_id in taken:
            n += 1
            account_id = f"{base}-{n}"
        if not _accounts:
            _accounts.append(DEFAULT)  # the sign-in used so far stays first
        account = ClaudeAccount(id=account_id, label=label, config_dir=folder)
        _accounts.append(account)
        _save()
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
    usage.clear_exhausted(PROVIDER)  # a new account may serve the calls a spent one refused
    return account


def remove(account_id: str) -> None:
    """Take an account out of the list (its folder and sign-in are left as they are)."""
    with _lock:
        current = list(_accounts) or [DEFAULT]
        if not any(a.id == account_id for a in current):
            raise KeyError(account_id)
        _accounts[:] = [a for a in current if a.id != account_id]
        _status.pop(account_id, None)
        _save()
        if _tokens.pop(account_id, None) is not None:
            _save_tokens()
    usage.clear_exhausted(spent_key(account_id))


def move_up(account_id: str) -> None:
    """Use an account one place earlier."""
    with _lock:
        current = list(_accounts) or [DEFAULT]
        i = next((n for n, a in enumerate(current) if a.id == account_id), None)
        if i is None:
            raise KeyError(account_id)
        if i > 0:
            current[i - 1], current[i] = current[i], current[i - 1]
        _accounts[:] = current
        _save()


def found_folders() -> list[str]:
    """Claude Code folders on this computer (`~/.claude-*` with a sign-in file) not in the
    list yet, offered when adding an account."""
    out = []
    candidates = [Path.home() / ".claude"] + sorted(Path.home().glob(".claude-*"))
    for folder in candidates:
        has_state = (folder / ".claude.json").exists() or folder == Path.home() / ".claude"
        if folder.is_dir() and has_state and by_dir(str(folder)) is None:
            out.append(str(folder))
    return out


# ------------------------------------------------------------------ running calls


# ------------------------------------------------------------------ long-lived sign-in


def token(account_id: str) -> SavedToken | None:
    return _tokens.get(account_id)


def save_token(account_id: str, value: str, email: str | None = None) -> None:
    """Keep the long-lived sign-in `claude setup-token` printed for an account."""
    with _lock:
        _tokens[account_id] = SavedToken(token=SecretStr(value), saved_at=time.time(), email=email)
        _save_tokens()
        _status.pop(account_id, None)
        _signed_out.pop(account_id, None)


def sign_out(account_id: str) -> bool:
    """Forget an account's long-lived sign-in; False when it had none."""
    with _lock:
        if _tokens.pop(account_id, None) is None:
            return False
        _save_tokens()
        _status.pop(account_id, None)
    return True


def _token_failed(account: ClaudeAccount, exc: LLMError) -> None:
    saved = _tokens.get(account.id)
    if saved is not None and saved.failed != str(exc):
        with _lock:
            saved.failed = str(exc)[:300]
            _save_tokens()
            _status.pop(account.id, None)


def _token_worked(account: ClaudeAccount) -> None:
    """A call went through: a sign-in error seen before was passing (a network or server
    fault), not the token ending."""
    saved = _tokens.get(account.id)
    if saved is not None and saved.failed:
        with _lock:
            saved.failed = None
            _save_tokens()
            _status.pop(account.id, None)


def env(account: ClaudeAccount, with_token: bool = True) -> dict[str, str]:
    """The CLI's environment for `account`: no API keys, its own config folder, and its
    long-lived sign-in when it has one (never another account's, nor one inherited)."""
    from src.core.llm.anthropic_backend import subscription_env

    drop = {"CLAUDE_CONFIG_DIR", TOKEN_ENV}
    out = {k: v for k, v in subscription_env().items() if k not in drop}
    if account.config_dir:
        out["CLAUDE_CONFIG_DIR"] = account.config_dir
    saved = _tokens.get(account.id) if with_token and account.id else None
    if saved is not None:
        out[TOKEN_ENV] = saved.token.get_secret_value()
    return out


def spent_key(account_id: str) -> str:
    """The name `usage` keeps an account's spent limit and recent tokens under."""
    return f"{PROVIDER}@{account_id}"


def spent_until(account: ClaudeAccount) -> float | None:
    return usage.exhausted_until(spent_key(account.id))


def usable(account: ClaudeAccount) -> bool:
    """Not known to be spent or signed out."""
    out_at = _signed_out.get(account.id)
    if out_at is not None and time.monotonic() - out_at < SIGNED_OUT_S:
        return False
    return spent_until(account) is None


def queue() -> list[ClaudeAccount]:
    """The accounts a call may try now, in order."""
    return [a for a in accounts() if usable(a)]


def active() -> ClaudeAccount:
    """The account the next call runs on (the first one when all are spent)."""
    found = queue()
    return found[0] if found else accounts()[0]


def note_failure(account: ClaudeAccount, exc: LLMError) -> bool:
    """Remember a spent or signed-out account; True when the next account may take over."""
    if exc.kind == "limit":
        usage.mark_exhausted(spent_key(account.id), exc.resets_at, str(exc))
        return True
    if exc.kind != "auth":
        return False
    _token_failed(account, exc)
    if len(accounts()) > 1:
        _signed_out[account.id] = time.monotonic()
        return True
    return False


def all_spent(last: LLMError | None = None) -> LLMError:
    """The limit error once no account is left, with the earliest reset among them."""
    resets = [t for a in accounts() if (t := spent_until(a))]
    names = ", ".join(a.label for a in accounts())
    detail = f": {last}" if last else ""
    return LLMError(
        f"Usage limit reached on every Claude account ({names}){detail}",
        kind="limit",
        resets_at=min(resets) if resets else None,
    )


def _give_up(last: LLMError | None, spent: bool) -> LLMError:
    return all_spent(last) if spent or last is None else last


def call(run: Callable[[ClaudeAccount], R]) -> R:
    """`run` on the first usable account, moving to the next on a spent limit."""
    last: LLMError | None = None
    spent = False
    for account in queue():
        try:
            out = run(account)
        except LLMError as exc:
            if not note_failure(account, exc):
                raise
            last, spent = exc, spent or exc.kind == "limit"
            continue
        _token_worked(account)
        return out
    raise _give_up(last, spent or last is None)


async def acall(run: Callable[[ClaudeAccount], Awaitable[R]]) -> R:
    """`call` for coroutines."""
    last: LLMError | None = None
    spent = False
    for account in queue():
        try:
            out = await run(account)
        except LLMError as exc:
            if not note_failure(account, exc):
                raise
            last, spent = exc, spent or exc.kind == "limit"
            continue
        _token_worked(account)
        return out
    raise _give_up(last, spent or last is None)


# ------------------------------------------------------------------ sign-in status


def status(account: ClaudeAccount, max_age: float = STATUS_TTL_S) -> dict[str, Any]:
    """`claude auth status` for one account, cached (it starts a process)."""
    from src.core.llm.claude_code_backend import claude_binary, claude_code_status

    hit = _status.get(account.id)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    saved = _tokens.get(account.id)
    if saved is not None and claude_binary() is not None:
        found = _token_status(saved)  # `claude auth status` would not check the token
    else:
        found = claude_code_status(account.config_dir, with_email=True)
    _status[account.id] = (time.monotonic(), found)
    if found.get("logged_in"):
        _signed_out.pop(account.id, None)
    return found


def _token_status(saved: SavedToken) -> dict[str, Any]:
    """An account with a long-lived sign-in: signed in until a call is refused or it ends."""
    ends = saved.saved_at + TOKEN_DAYS * 86400
    until = time.strftime("%d %b %Y", time.localtime(ends))
    base = {"installed": True, "lasting": True, "token_saved_at": saved.saved_at}
    if saved.failed or time.time() > ends:
        why = saved.failed or f"it ended on {until}"
        message = f"The saved Claude sign-in was refused ({why}). Sign in again."
        return {**base, "logged_in": False, "subscription": None, "message": message}
    message = f"Stays signed in until {until} or until you sign it out."
    return {
        **base,
        "logged_in": True,
        "subscription": None,
        "email": saved.email,
        "message": message,
    }


def folder_status(folder: str, max_age: float = STATUS_TTL_S) -> dict[str, Any]:
    """`status` of a folder not in the list (offered when adding an account)."""
    return status(ClaudeAccount(id=f"found:{folder}", label=folder, config_dir=folder), max_age)


def forget_status(account_id: str | None = None) -> None:
    """Check sign-in again next time (after a sign-in was started)."""
    if account_id is None:
        _status.clear()
        _signed_out.clear()
    else:
        _status.pop(account_id, None)
        _signed_out.pop(account_id, None)


def summary(max_age: float = STATUS_TTL_S) -> dict[str, Any]:
    """Claude Code as one provider: signed in when any account is."""
    found = [(a, status(a, max_age)) for a in accounts()]
    signed = [s for _, s in found if s.get("logged_in")]
    first = found[0][1]
    if not signed:
        out = {**first, "accounts": len(found), "signed_in": 0}
    else:
        out = {**signed[0], "accounts": len(found), "signed_in": len(signed)}
    out.pop("email", None)  # one provider's status names no account
    if signed and len(found) > 1:
        out["message"] = f"{len(signed)} of {len(found)} Claude accounts signed in."
    return out
