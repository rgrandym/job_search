"""Every model connection in one view: who serves each task, plan windows, spend, handovers.

The right-hand panel and the header's status light read this (`GET /api/connections`); it
replaces the separate Codex and Claude Code usage routes. Nothing here makes a model call:
plan windows come from what the providers last reported (Codex: a cached app-server read).
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.config import LLMProviderName
from src.core.llm import Role, claude_accounts, usage
from src.core.llm.fallback import current_mode
from src.core.llm.usage import SpendRow, UsageWindows
from src.services.compare_usage import known_prices
from src.services.workspace import Workspace

LABELS: dict[str, str] = {
    "anthropic": "Claude API",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "openai": "OpenAI API",
    "openrouter": "OpenRouter",
}
ROLES: list[tuple[Role, str]] = [
    ("quality", "Quality"),
    ("screening", "Screening"),
    ("profile", "Profile"),
    ("cv", "CV writing"),
    ("letter", "Cover letters"),
]


class Route(BaseModel):
    """A model on a provider at an effort."""

    model_config = ConfigDict(extra="forbid")

    provider: LLMProviderName
    model: str
    effort: str | None = None


class RoleView(BaseModel):
    """Who serves one task now, and who takes over when its plan is spent."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    label: str
    configured: Route
    replacement: Route | None = None
    switched: bool = Field(False, description="The configured plan is spent; replacement serves")


class ClaudeAccountView(BaseModel):
    """One Claude account (a Claude Code folder) in the order accounts are used."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    config_dir: str | None = None  # None: the CLI's default sign-in
    signed_in: bool = False
    email: str | None = Field(None, description="Who the folder is signed in as")
    subscription: str | None = None
    message: str = ""
    active: bool = Field(False, description="Calls run on this account now")
    same_as: str | None = Field(None, description="An earlier account signed in as the same user")
    windows: UsageWindows | None = None
    exhausted_until: float | None = None


class FoundFolder(BaseModel):
    """A Claude Code folder on this computer that is not in the list yet."""

    model_config = ConfigDict(extra="forbid")

    folder: str
    signed_in: bool = False
    email: str | None = None
    subscription: str | None = None


class ClaudeAccountsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[ClaudeAccountView]
    found: list[FoundFolder] = Field(default_factory=list, description="Folders not added yet")


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: LLMProviderName
    label: str
    billing: Literal["plan", "key"]
    ready: bool
    roles: list[str] = Field(default_factory=list, description="Task labels it serves")
    windows: UsageWindows | None = None
    exhausted_until: float | None = None
    spend_today: list[SpendRow] = Field(default_factory=list)
    cost_today: float | None = None  # USD, when every model used has a known price
    cost_month: float | None = None
    accounts: list[ClaudeAccountView] = Field(default_factory=list)  # Claude Code only


class ConnectionsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connections: list[Connection]
    roles: list[RoleView]
    on_limit: Literal["switch", "ask"]
    handover_threshold: int
    updated_at: float


def _cost(rows: list[SpendRow], prices: dict[str, tuple[float, float]]) -> float | None:
    if not rows or any(r.model not in prices for r in rows):
        return None
    total = sum(
        (r.input_tokens * prices[r.model][0] + r.output_tokens * prices[r.model][1]) / 1e6
        for r in rows
    )
    return round(total, 4)


def account_views(max_age: float = claude_accounts.STATUS_TTL_S) -> list[ClaudeAccountView]:
    """Every Claude account with its sign-in, plan windows and whether it is spent."""
    active = claude_accounts.active().id
    out: list[ClaudeAccountView] = []
    for account in claude_accounts.accounts():
        status = claude_accounts.status(account, max_age)
        signed_in = bool(status.get("logged_in"))
        email = status.get("email") if signed_in else None
        same = next((v.label for v in out if email and v.email == email), None)
        windows = (
            usage.plan_windows("claude_code", account=account.id, live=True) if signed_in else None
        )
        out.append(
            ClaudeAccountView(
                id=account.id,
                label=account.label,
                config_dir=account.config_dir,
                signed_in=signed_in,
                email=email,
                subscription=status.get("subscription"),
                message=str(status.get("message") or ""),
                active=account.id == active,
                same_as=same,
                windows=windows,
                exhausted_until=claude_accounts.spent_until(account),
            )
        )
    return out


def claude_accounts_view(max_age: float = 3.0) -> ClaudeAccountsView:
    """Settings' account list: sign-in checked again within seconds, to show a new sign-in."""
    found = []
    for folder in claude_accounts.found_folders():
        status = claude_accounts.folder_status(folder)
        signed_in = bool(status.get("logged_in"))
        found.append(
            FoundFolder(
                folder=folder,
                signed_in=signed_in,
                email=status.get("email") if signed_in else None,
                subscription=status.get("subscription"),
            )
        )
    return ClaudeAccountsView(accounts=account_views(max_age), found=found)


def role_views(ws: Workspace) -> list[RoleView]:
    llm = ws.llm
    out = []
    for role, label in ROLES:
        provider = llm.provider_for(role)
        configured = Route(
            provider=provider, model=llm.model_for(role), effort=llm.effort_for(role)
        )
        found = ws.replacement(role)
        replacement = Route(provider=found[0], model=found[1], effort=found[2]) if found else None
        switched = bool(
            replacement
            and current_mode(llm.on_limit) == "switch"
            and usage.exhausted_until(provider)
        )
        out.append(
            RoleView(
                role=role,
                label=label,
                configured=configured,
                replacement=replacement,
                switched=switched,
            )
        )
    return out


def connections(ws: Workspace) -> ConnectionsView:
    """Every provider a task uses, a replacement could use, or that served calls this month."""
    roles = role_views(ws)
    month = usage.spend(days=30)
    today = usage.spend(days=1)
    wanted: dict[str, list[str]] = {}
    for r in roles:
        wanted.setdefault(r.configured.provider, []).append(r.label)
        if r.replacement:
            wanted.setdefault(r.replacement.provider, [])
    for row in month:
        wanted.setdefault(row.provider, [])
    prices = known_prices()
    out = []
    for provider, labels in wanted.items():
        if provider not in LABELS:
            continue
        pid: LLMProviderName = provider  # type: ignore[assignment]
        plan = provider in usage.PLAN_PROVIDERS
        ready = ws.provider_ready(pid)
        mine_today = [r for r in today if r.provider == provider]
        mine_month = [r for r in month if r.provider == provider]
        out.append(
            Connection(
                id=pid,
                label=LABELS[provider],
                billing="plan" if plan else "key",
                ready=ready,
                roles=labels,
                windows=usage.plan_windows(provider, live=True) if plan and ready else None,
                exhausted_until=usage.exhausted_until(provider),
                spend_today=mine_today,
                cost_today=None if plan else _cost(mine_today, prices),
                cost_month=None if plan else _cost(mine_month, prices),
                accounts=account_views() if provider == "claude_code" and ready else [],
            )
        )
    return ConnectionsView(
        connections=out,
        roles=roles,
        on_limit=ws.llm.on_limit,
        handover_threshold=ws.llm.handover_threshold,
        updated_at=time.time(),
    )


# ------------------------------------------------------------------ spent plans in a run


class PlanLimit(BaseModel):
    """A task's plan ran out during a run and work was left undone: the user picks stop,
    continue on `replacement`, or wait until `resets_at` and continue on the same model."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    label: str
    configured: Route
    resets_at: float
    replacement: Route | None = None  # None: no other plan signed in, or it is spent too


def plan_limit(
    ws: Workspace, roles: tuple[Role, ...] = ("screening", "quality")
) -> PlanLimit | None:
    """The first of `roles` whose provider is spent now, with what could take over."""
    for view in role_views(ws):
        if view.role not in roles:
            continue
        until = usage.exhausted_until(view.configured.provider)
        if until is None:
            continue
        replacement = view.replacement
        if replacement and usage.exhausted_until(replacement.provider):
            replacement = None
        return PlanLimit(
            role=view.role,
            label=view.label,
            configured=view.configured,
            resets_at=until,
            replacement=replacement,
        )
    return None


def handovers(ws: Workspace) -> list[str]:
    """One line per task now served by the other plan because its own is spent."""
    return [
        f"{v.label}: {LABELS[v.configured.provider]} {v.configured.model} → "
        f"{LABELS[v.replacement.provider]} {v.replacement.model} (plan limit reached)"
        for v in role_views(ws)
        if v.switched and v.replacement
    ]


def plan_warnings(ws: Workspace, roles: tuple[Role, ...] = ("screening", "quality")) -> list[str]:
    """Before a run: plan windows past the user's threshold, and what happens if they run out."""
    out = []
    seen: set[str] = set()
    for view in role_views(ws):
        provider = view.configured.provider
        if view.role not in roles or provider in seen or provider not in usage.PLAN_PROVIDERS:
            continue
        seen.add(provider)
        win = usage.over_threshold(usage.plan_windows(provider), ws.llm.handover_threshold)
        if win is None:
            continue
        then = "you will be asked what to do"
        following = claude_accounts.queue()[1:] if provider == "claude_code" else []
        if following:
            then = f"work moves to your next Claude account ({following[0].label})"
        elif view.replacement and current_mode(ws.llm.on_limit) == "switch":
            then = f"work moves to {LABELS[view.replacement.provider]} {view.replacement.model}"
        out.append(
            f"{LABELS[provider]} {win.name} window is {win.used_percent:.0f}% used; "
            f"if it runs out, {then}"
        )
    return out
