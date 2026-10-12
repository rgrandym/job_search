"""Plan windows and spend per provider, and the classifier that tells a spent plan from any
other model error.

A wrong classification either strands the user (a limit read as an ordinary error, so no
switch or wait is offered) or switches provider on an ordinary error; the patterns are tested
against recorded messages. Nothing here makes a model call: Claude Code reports its windows
through `claude -p /usage` (a local command; its calls' `rate_limit_event`s state a share only
near a limit), Codex through `codex app-server`. Both are read at most once a minute, and again
soon after a call on that plan.

Each plan window also shows the tokens this app spent inside it (`tokens`), from a short log
of recent calls: the plans state only a percentage, never a token allowance.

Persistence (`data/usage_claude.json`, `data/model_spend.json`, `data/usage_recent.json`)
starts once the workspace calls `configure(data_dir)`; until then everything stays in memory,
so tests and CLIs never write into the user's data folder by accident.
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
    tokens: int | None = None  # this app's tokens (input + output) inside the window so far


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
_recent: dict[str, list[list[float]]] = {}  # plan provider -> [[time, input, output], ...]


def configure(data_dir: Path | None) -> None:
    """Persist under `data_dir` from now on (None: memory only), loading what is there."""
    global _dir
    with _lock:
        _dir = data_dir
        _claude.clear()
        _spend.clear()
        _exhausted.clear()
        _recent.clear()
        _claude_cli.clear()
        _claude.update(_read("usage_claude.json"))
        _spend.update(_read("model_spend.json"))
        _recent.update(_read("usage_recent.json"))


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


def record_claude_reading(kind: str, percent_used: float, resets_at: float | None) -> None:
    """Store a window's share used as a Claude Code session reports it (the dev pane passes
    these on): unlike `rate_limit_event`s, it is stated far from the limit too."""
    if kind not in _CLAUDE_NAMES:
        return
    with _lock:
        win = dict(_claude.get(kind) or {})
        previous_reset = win.get("resetsAt")
        win["utilization"] = max(0.0, min(float(percent_used), 100.0)) / 100  # a fraction
        if resets_at:
            win["resetsAt"] = resets_at
            if previous_reset and resets_at > float(previous_reset) + 60:
                win["status"] = "allowed"  # the rejected window has reset
        win.setdefault("status", "allowed")
        _claude[kind] = win
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
        if status == "rejected":
            used = 100.0  # a refused call is decisive even if /usage still reports a lower share
        elif util is not None:
            used = round(float(util) * (100 if float(util) <= 1 else 1), 1)
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
CLAUDE_TTL_S = 60
CODEX_STALE_S = 15  # after a Codex call, windows older than this are read again


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
    age = time.time() - _codex_cache.get("at", 0)
    stale = _codex_cache.get("stale") and age >= CODEX_STALE_S
    if isinstance(hit, UsageWindows) and age < ttl and not stale:
        return hit
    from src.core.llm.codex_backend import codex_usage
    from src.core.llm.types import LLMError

    try:
        value = codex_windows(codex_usage())
    except (LLMError, OSError, ValueError) as exc:
        return UsageWindows(note=f"Could not read Codex usage: {exc}")
    _codex_cache.update(value=value, at=time.time(), stale=False)
    return value


_claude_cli: dict[str, Any] = {}  # when `/usage` was last read, and whether calls moved it
_USAGE_LINE = re.compile(
    r"^\s*Current (session|week)(?: \(([^)]*)\))?:\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets (.+))?$",
    re.I | re.M,
)
_RESETS = re.compile(
    r"(?:([A-Z][a-z]{2}) (\d{1,2})(?:,? (\d{4}))? at )?(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
    r"(?:\s*\(([^)]+)\))?",
    re.I,
)


def parse_claude_usage(
    text: str, now: float | None = None
) -> list[tuple[str, float, float | None]]:
    """(window kind, share used, reset epoch) per line of Claude Code's `/usage` text, e.g.
    "Current session: 76% used · resets Oct 11 at 2:10pm (Europe/London)"."""
    out = []
    for m in _USAGE_LINE.finditer(text):
        scope = (m.group(2) or "").lower()
        if m.group(1).lower() == "session":
            kind = "five_hour"
        elif "opus" in scope:
            kind = "seven_day_opus"
        elif "sonnet" in scope:
            kind = "seven_day_sonnet"
        else:
            kind = "seven_day"
        out.append((kind, float(m.group(3)), _reset_epoch(m.group(4) or "", now)))
    return out


def _reset_epoch(text: str, now: float | None = None) -> float | None:
    """ "Oct 11 at 2:10pm (Europe/London)" or "3pm (UTC)" as epoch seconds; the next such
    time when no date (or no year) is given."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    m = _RESETS.search(text)
    if not m:
        return None
    month, day, year, hour, minute, half, zone = m.groups()
    try:
        tz = ZoneInfo(zone) if zone else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    base = datetime.fromtimestamp(time.time() if now is None else now, tz)
    hour24 = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    try:
        if month:
            stamp = datetime.strptime(f"{month} {day} {year or base.year}", "%b %d %Y")
            when = stamp.replace(hour=hour24, minute=int(minute or 0), tzinfo=tz)
            if not year and when < base - timedelta(days=1):
                when = when.replace(year=when.year + 1)  # "Jan 2" read in late December
        else:
            when = base.replace(hour=hour24, minute=int(minute or 0), second=0, microsecond=0)
            if when <= base:
                when += timedelta(days=1)
    except ValueError:
        return None
    return when.timestamp()


def claude_plan_windows(ttl: float = CLAUDE_TTL_S) -> UsageWindows:
    """The Claude plan's windows, refreshed from `/usage` once a minute (sooner after calls
    on the plan); what the calls reported stands when it cannot be read."""
    age = time.time() - float(_claude_cli.get("at") or 0)
    stale = _claude_cli.get("stale") and age >= CODEX_STALE_S
    if age >= ttl or stale:
        _claude_cli.update(at=time.time(), stale=False)  # one reader at a time
        from src.core.llm.claude_code_backend import claude_usage_text
        from src.core.llm.types import LLMError

        try:
            readings = parse_claude_usage(claude_usage_text())
        except (LLMError, OSError, ValueError):
            readings = []
        for kind, used, resets in readings:
            record_claude_reading(kind, used, resets)
    return claude_windows()


def plan_windows(provider: str, fresh: bool = False) -> UsageWindows | None:
    """The plan windows of a plan provider, each with this app's tokens inside it; None for
    providers billed per token."""
    if provider == "claude_code":
        found = claude_plan_windows(ttl=0 if fresh else CLAUDE_TTL_S)
    elif provider == "codex":
        found = codex_plan_windows(ttl=0 if fresh else CODEX_TTL_S)
    else:
        return None
    windows = [w.model_copy(update={"tokens": window_tokens(provider, w)}) for w in found.windows]
    return found.model_copy(update={"windows": windows})


def _window_seconds(name: str) -> float | None:
    """A window's length from its name ("5h", "7d Opus"); None when it names none."""
    m = re.match(r"(\d+)\s*([hd])", name)
    return int(m.group(1)) * (3600 if m.group(2) == "h" else 86400) if m else None


def window_tokens(provider: str, win: UsageWindow, now: float | None = None) -> int | None:
    """This app's tokens on `provider` since `win` opened (its reset minus its length, or the
    last window-length when the reset is unknown); None when the window has no length."""
    length = _window_seconds(win.name)
    if length is None:
        return None
    now = time.time() if now is None else now
    end = win.resets_at if win.resets_at and win.resets_at > now else now
    start = end - length
    with _lock:
        rows = list(_recent.get(provider, []))
    return int(sum(r[1] + r[2] for r in rows if r[0] >= start))


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
        if provider in PLAN_PROVIDERS:
            _log_recent(provider, int(input_tokens), int(output_tokens))
    if provider == "codex":
        _codex_cache["stale"] = True  # its windows moved: read them again soon
    elif provider == "claude_code":
        _claude_cli["stale"] = True


RECENT_S = 8 * 86400  # longer than the longest plan window (7 days)


def _log_recent(provider: str, input_tokens: int, output_tokens: int) -> None:
    """Add a call to the recent log (caller holds `_lock`), dropping calls past `RECENT_S`."""
    now = time.time()
    rows = [r for r in _recent.get(provider, []) if now - r[0] < RECENT_S]
    rows.append([round(now, 1), input_tokens, output_tokens])
    _recent[provider] = rows
    _write("usage_recent.json", _recent)


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
