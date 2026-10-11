"""Plan windows and spend per provider, and the classifier that tells a spent plan from any
other model error.

A wrong classification either strands the user (a limit read as an ordinary error, so no
switch or wait is offered) or switches provider on an ordinary error; the patterns are tested
against recorded messages. Nothing here makes a model call: Claude Code reports its windows
in the `rate_limit_event`s of every call, Codex through `codex app-server`.

Persistence (`data/usage_claude.json`, `data/model_spend.json`) starts once the workspace
calls `configure(data_dir)`; until then everything stays in memory, so tests and CLIs never
write into the user's data folder by accident.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorKind = Literal["limit", "auth", "context", "network", "other"]
PLAN_PROVIDERS = ("claude_code", "codex")  # paid by plan windows, not per token

LIMIT = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|limit (has been )?exceeded|rate[ _-]?limit"
    r"|quota|out of (extra )?usage|credit balance is too low|insufficient_quota|\b429\b"
    r"|usage_limit_reached|plan limit",
    re.I,
)
AUTH = re.compile(
    r"not (logged|signed) in|log ?in again|please (log|sign) ?in|unauthori[sz]ed|\b401\b|\b403\b"
    r"|invalid (x-)?api[ -]?key|authentication|oauth token|token (has )?expired|/login",
    re.I,
)
CONTEXT = re.compile(
    r"prompt is too long|context (length|window)|too many tokens|maximum context", re.I
)
NETWORK = re.compile(
    r"ECONN\w*|ENOTFOUND|EAI_AGAIN|network|fetch failed|socket hang up"
    r"|connection (reset|refused|error)|getaddrinfo|offline",
    re.I,
)
DEFAULT_WAIT_S = 15 * 60  # a limit without a stated reset is retried after this long


class UsageWindow(BaseModel):
    """One plan window ("5h", "7d"), the same shape for every provider."""

    model_config = ConfigDict(extra="forbid")

    name: str
    used_percent: float | None = None  # None: the plan did not say (Claude, far from the limit)
    resets_at: float | None = None  # epoch seconds
    status: str | None = None  # Claude: "allowed", "allowed_warning", "rejected"


class UsageWindows(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windows: list[UsageWindow] = Field(default_factory=list)
    updated_at: float | None = None
    note: str = ""


class SpendRow(BaseModel):
    """Tokens and calls of one model on one provider over a period."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False  # some calls were estimates (Codex chat does not report usage)


# ------------------------------------------------------------------ classification


def classify(text: str) -> ErrorKind:
    """The kind of a provider error from its message. Limit first: a quota message often also
    mentions the account or the request, and a spent plan must never read as auth."""
    if LIMIT.search(text):
        return "limit"
    if AUTH.search(text):
        return "auth"
    if CONTEXT.search(text):
        return "context"
    if NETWORK.search(text):
        return "network"
    return "other"


def reset_from_text(text: str, now: float | None = None) -> float | None:
    """A reset time named in a limit message: an epoch after `|` (older Claude Code), or a
    clock time ("try again at 10:21 PM", "resets 3pm"), the next time it comes round."""
    now = time.time() if now is None else now
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return float(m.group(1))
    m = re.search(r"\b(?:at|resets?(?: at)?)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", text, re.I)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    half = (m.group(3) or "").lower().replace(".", "")
    if half == "pm" and hour < 12:
        hour += 12
    elif half == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59 or (not half and not m.group(2)):
        return None  # a bare number is not a clock time
    base = time.localtime(now)
    target = time.mktime((base.tm_year, base.tm_mon, base.tm_mday, hour, minute, 0, 0, 0, -1))
    return target + 86400 if target <= now else target


# ------------------------------------------------------------------ storage

_lock = threading.Lock()
_dir: Path | None = None
_claude: dict[str, Any] = {}
_spend: dict[str, dict[str, dict[str, Any]]] = {}  # day -> "provider|model" -> counters
_exhausted: dict[str, tuple[float, str]] = {}  # provider -> (until, message)


def configure(data_dir: Path | None) -> None:
    """Persist under `data_dir` from now on (None: memory only), loading what is there."""
    global _dir
    with _lock:
        _dir = data_dir
        _claude.clear()
        _spend.clear()
        _exhausted.clear()
        _claude.update(_read("usage_claude.json"))
        _spend.update(_read("model_spend.json"))


def _read(name: str) -> dict[str, Any]:
    if _dir is None:
        return {}
    try:
        data = json.loads((_dir / name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(name: str, data: dict[str, Any]) -> None:
    if _dir is None:
        return
    try:
        _dir.mkdir(parents=True, exist_ok=True)
        (_dir / name).write_text(json.dumps(data))
    except OSError:
        pass  # usage is advisory; never fail a model call over it


# ------------------------------------------------------------------ Claude Code windows

_CLAUDE_NAMES = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_opus": "7d Opus",
    "seven_day_sonnet": "7d Sonnet",
}


def record_claude_limit(info: dict[str, Any]) -> None:
    """Store one `rate_limit_info` from a stream-json `rate_limit_event`."""
    kind = info.get("rateLimitType")
    if not kind:
        return
    with _lock:
        win = dict(_claude.get(str(kind)) or {})
        win.update(status=info.get("status"), resetsAt=info.get("resetsAt"))
        if info.get("utilization") is not None:
            win["utilization"] = info["utilization"]
        win["overage"] = bool(info.get("isUsingOverage"))
        _claude[str(kind)] = win
        _claude["observed_at"] = time.time()
        _write("usage_claude.json", _claude)


def claude_windows(now: float | None = None) -> UsageWindows:
    """The Claude plan's windows as last reported. A window past its reset reads as unused."""
    now = time.time() if now is None else now
    with _lock:
        stored = dict(_claude)
    if not stored.get("observed_at"):
        return UsageWindows(note="Shown after the first Claude Code call.")
    windows = []
    for key, name in _CLAUDE_NAMES.items():
        win = stored.get(key)
        if not isinstance(win, dict):
            continue
        util, resets, status = win.get("utilization"), win.get("resetsAt"), win.get("status")
        if resets and float(resets) < now:
            util, status = 0.0, "allowed"  # reset since it was observed
        used = None
        if util is not None:
            used = round(float(util) * (100 if float(util) <= 1 else 1), 1)
        elif status == "rejected":
            used = 100.0
        windows.append(
            UsageWindow(
                name=name,
                used_percent=used,
                resets_at=float(resets) if resets else None,
                status=status,
            )
        )
    return UsageWindows(windows=windows, updated_at=stored.get("observed_at"))


# ------------------------------------------------------------------ Codex windows

_codex_cache: dict[str, Any] = {}
CODEX_TTL_S = 60


def codex_windows(view: dict[str, Any]) -> UsageWindows:
    """Normalise `codex_backend.codex_usage()` (primary/secondary windows) to `UsageWindows`."""
    windows = []
    for key in ("primary", "secondary"):
        win = view.get(key)
        if not isinstance(win, dict):
            continue
        minutes = int(win.get("window_minutes") or 0)
        name = {"primary": "5h", "secondary": "7d"}[key]
        if minutes:
            name = f"{minutes // 60}h" if minutes < 1440 else f"{minutes // 1440}d"
        windows.append(
            UsageWindow(
                name=name,
                used_percent=float(win.get("used_percent") or 0),
                resets_at=float(win["resets_at"]) if win.get("resets_at") else None,
            )
        )
    plan = view.get("plan_type")
    note = f"ChatGPT {plan} plan" if plan else ""
    if view.get("ordinary_usage_allowed") is False:
        note = (note + " · usage currently unavailable").strip(" ·")
    return UsageWindows(windows=windows, updated_at=time.time(), note=note)


def codex_plan_windows(ttl: float = CODEX_TTL_S) -> UsageWindows:
    """ChatGPT plan windows, cached for a minute: reading them starts a process."""
    hit = _codex_cache.get("value")
    if isinstance(hit, UsageWindows) and time.time() - _codex_cache.get("at", 0) < ttl:
        return hit
    from src.core.llm.codex_backend import codex_usage
    from src.core.llm.types import LLMError

    try:
        value = codex_windows(codex_usage())
    except (LLMError, OSError, ValueError) as exc:
        return UsageWindows(note=f"Could not read Codex usage: {exc}")
    _codex_cache.update(value=value, at=time.time())
    return value


def plan_windows(provider: str, fresh: bool = False) -> UsageWindows | None:
    """The plan windows of a plan provider; None for providers billed per token."""
    if provider == "claude_code":
        return claude_windows()
    if provider == "codex":
        return codex_plan_windows(ttl=0 if fresh else CODEX_TTL_S)
    return None


def over_threshold(usage: UsageWindows | None, threshold: float) -> UsageWindow | None:
    """The first window at or past `threshold` percent used, if any."""
    if usage is None:
        return None
    for win in usage.windows:
        if win.used_percent is not None and win.used_percent >= threshold:
            return win
    return None


# ------------------------------------------------------------------ spent plans


def mark_exhausted(provider: str, resets_at: float | None, message: str = "") -> float:
    """Record that `provider` refused a call for a spent limit; returns when it may be retried."""
    until = resets_at if resets_at and resets_at > time.time() else time.time() + DEFAULT_WAIT_S
    with _lock:
        _exhausted[provider] = (until, message)
    return until


def exhausted_until(provider: str, now: float | None = None) -> float | None:
    """When `provider` may be used again, if its limit is known to be spent now."""
    now = time.time() if now is None else now
    with _lock:
        hit = _exhausted.get(provider)
        if hit and hit[0] <= now:
            del _exhausted[provider]
            hit = None
    return hit[0] if hit else None


def clear_exhausted(provider: str) -> None:
    with _lock:
        _exhausted.pop(provider, None)


# ------------------------------------------------------------------ spend ledger

KEEP_DAYS = 62


def record_spend(
    provider: str, model: str, input_tokens: int, output_tokens: int, estimated: bool = False
) -> None:
    """Add one call to today's ledger (`data/model_spend.json`)."""
    today = date.today().isoformat()
    with _lock:
        day = _spend.setdefault(today, {})
        row = day.setdefault(f"{provider}|{model}", {"calls": 0, "in": 0, "out": 0, "est": False})
        row["calls"] += 1
        row["in"] += int(input_tokens)
        row["out"] += int(output_tokens)
        row["est"] = bool(row["est"] or estimated)
        cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
        for old in [d for d in _spend if d < cutoff]:
            del _spend[old]
        _write("model_spend.json", _spend)


def spend(days: int = 1) -> list[SpendRow]:
    """Calls and tokens per (provider, model) over the last `days` days, busiest first."""
    first = (date.today() - timedelta(days=days - 1)).isoformat()
    totals: dict[str, SpendRow] = {}
    with _lock:
        items = [(k, dict(v)) for d, rows in _spend.items() if d >= first for k, v in rows.items()]
    for key, row in items:
        provider, _, model = key.partition("|")
        out = totals.setdefault(key, SpendRow(provider=provider, model=model))
        out.calls += int(row.get("calls", 0))
        out.input_tokens += int(row.get("in", 0))
        out.output_tokens += int(row.get("out", 0))
        out.estimated = out.estimated or bool(row.get("est"))
    return sorted(totals.values(), key=lambda r: r.input_tokens + r.output_tokens, reverse=True)
