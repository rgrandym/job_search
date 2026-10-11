import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { clockLabel, tokenCount } from "../lib/labels";
import type { Connection, ConnectionsView, PlanWindow } from "../lib/types";
import { cn } from "../lib/utils";
import { spendLine } from "./ConnectionsPanel";
import { useNow } from "./TaskProgress";

// The Activity section's usage block: for each connection in use, how much of the plan's
// session (5h) and weekly windows is left and when they reset, and today's tokens. Same
// `GET /api/connections` query as "Models & usage", polled faster while work runs, since
// every model call adds to the ledger and Claude Code reports its windows with each call.

const WINDOW_LABEL: Record<string, string> = { "5h": "Session", "7d": "Week" };

/** Seconds until a reset as "42 min", "3 h 05 min" or "2 d 4 h". */
function until(seconds: number) {
  const m = Math.max(0, Math.round(seconds / 60));
  if (m < 60) return `${m} min`;
  if (m < 1440) return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  return `${Math.floor(m / 1440)} d ${Math.floor((m % 1440) / 60)} h`;
}

const left = (win: PlanWindow) => (win.used_percent == null ? null : Math.max(0, 100 - win.used_percent));

export function useConnections(busy: boolean) {
  return useQuery({ queryKey: ["connections"], queryFn: api.connections, refetchInterval: busy ? 10_000 : 60_000 });
}

/** Connections that serve a task or were used today. */
function inUse(view: ConnectionsView): Connection[] {
  return view.connections.filter((c) => c.ready && (c.roles.length > 0 || c.spend_today.length > 0));
}

/** "Codex session 99% left" for the Activity header, or null when no plan says. */
export function sessionHint(view: ConnectionsView | undefined): string | null {
  if (!view) return null;
  for (const c of inUse(view)) {
    if (c.exhausted_until) return `${c.label} spent`;
    const session = c.windows?.windows.find((w) => w.name === "5h");
    const share = session ? left(session) : null;
    if (share != null) return `${c.label} session ${Math.round(share)}% left`;
  }
  return null;
}

function WindowLine({ win, now, threshold }: { win: PlanWindow; now: number; threshold: number }) {
  const share = left(win);
  const spent = share === 0 || win.status === "rejected";
  const low = share != null && share <= 100 - threshold;
  const text = spent
    ? "spent"
    : share != null
      ? `${Math.round(share)}% left`
      : win.status === "allowed_warning"
        ? "nearing limit"
        : "within limit";
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="w-14 shrink-0 text-faint">{WINDOW_LABEL[win.name] ?? win.name}</span>
      <div
        className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface"
        title={`${win.used_percent != null ? `${Math.round(win.used_percent)}% used` : "The plan reports its share only near the limit"}${win.tokens ? ` · this app used ${win.tokens.toLocaleString()} tokens in this window` : ""}`}
      >
        {/* Unknown share: a neutral full track, so it never reads as an empty allowance. */}
        <div
          className={cn("h-full rounded-full", share == null ? "bg-border" : spent ? "bg-bad" : low ? "bg-warn" : "bg-good")}
          style={{ width: `${share == null ? 100 : share}%` }}
        />
      </div>
      <span className={cn("w-52 shrink-0 text-right tabular-nums", spent ? "text-bad" : low ? "text-warn" : "text-faint")}>
        {text}
        {!!win.tokens && <span title="This app's tokens inside this window"> · {tokenCount(win.tokens)} tok</span>}
        {win.resets_at != null && win.resets_at > now && <span title={`Resets ${clockLabel(win.resets_at)}`}> · resets in {until(win.resets_at - now)}</span>}
      </span>
    </div>
  );
}

function ConnectionUsage({ c, now, threshold }: { c: Connection; now: number; threshold: number }) {
  const windows = c.windows?.windows ?? [];
  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-1.5 text-[11px]">
        <span className="font-medium text-fg">{c.label}</span>
        {c.exhausted_until && <span className="text-warn">spent · back in {until(c.exhausted_until - now)}</span>}
        <span className="ml-auto truncate text-faint" title="Model calls and tokens today">
          {c.spend_today.length ? spendLine(c.spend_today) : "no calls today"}
          {c.cost_today != null && ` · $${c.cost_today.toFixed(3)}`}
        </span>
      </div>
      {windows.map((w) => <WindowLine key={w.name} win={w} now={now} threshold={threshold} />)}
      {c.billing === "plan" && !windows.length && c.windows?.note && <p className="text-[10px] text-faint">{c.windows.note}</p>}
    </div>
  );
}

/** Plan windows left and today's tokens, under the running work. */
export function UsageSummary({ busy }: { busy: boolean }) {
  const view = useConnections(busy);
  const now = useNow(true);
  let body;
  if (view.isPending) body = <p className="text-[11px] text-faint">Reading usage…</p>;
  else if (view.isError) body = <p className="text-[11px] text-bad">Usage unavailable: {(view.error as Error).message}</p>;
  else {
    const shown = inUse(view.data);
    body = shown.length ? (
      shown.map((c) => <ConnectionUsage key={c.id} c={c} now={now} threshold={view.data.handover_threshold} />)
    ) : (
      <p className="text-[11px] text-faint">No model connected yet. Choose one in Settings.</p>
    );
  }
  return (
    <div className="space-y-2 border-t border-border pt-2">
      <p className="label text-[11px] text-muted">Usage</p>
      {body}
    </div>
  );
}
