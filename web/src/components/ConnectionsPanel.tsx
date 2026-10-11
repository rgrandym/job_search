import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Loader2, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { clockLabel, PROVIDER_LABEL } from "../lib/labels";
import type { Connection, OnLimit, PlanWindow, RoleView, SpendRow } from "../lib/types";
import { cn } from "../lib/utils";
import { useChat } from "../stores/chatStore";
import { Resizable } from "./ui";

// One view of every model connection, replacing the per-provider blocks: which model serves
// each task (and which takes over when its plan is spent), each plan's windows, today's spend,
// and what a spent plan does. Numbers come from `GET /api/connections`.

export const tokens = (value: number) =>
  value < 1_000 ? String(value) : value < 1_000_000 ? `${(value / 1_000).toFixed(1)}k` : `${(value / 1_000_000).toFixed(1)}m`;

function WindowBar({ win, threshold }: { win: PlanWindow; threshold: number }) {
  const used = win.used_percent;
  const tone = used == null ? "bg-border" : used >= 100 ? "bg-bad" : used >= threshold ? "bg-warn" : "bg-good";
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="w-14 shrink-0 text-faint">{win.name}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface">
        {/* Unknown share: a neutral full track, so it never reads as an empty allowance. */}
        <div className={cn("h-full rounded-full", tone)} style={{ width: `${used == null ? 100 : Math.min(100, used)}%` }} />
      </div>
      <span className="w-28 shrink-0 text-right text-faint" title={win.status ?? undefined}>
        {used == null ? (win.status === "allowed_warning" ? "nearing limit" : "within limit") : `${Math.round(used)}%`}
        {win.resets_at ? ` · ${clockLabel(win.resets_at)}` : ""}
      </span>
    </div>
  );
}

export function spendLine(rows: SpendRow[]) {
  const calls = rows.reduce((n, r) => n + r.calls, 0);
  const tin = rows.reduce((n, r) => n + r.input_tokens, 0);
  const tout = rows.reduce((n, r) => n + r.output_tokens, 0);
  const estimated = rows.some((r) => r.estimated);
  return `${calls} call${calls === 1 ? "" : "s"} · ${estimated ? "≈" : ""}${tokens(tin)} in / ${tokens(tout)} out`;
}

function ConnectionCard({ c, threshold }: { c: Connection; threshold: number }) {
  return (
    <div className="space-y-1.5 rounded-md border border-border p-2">
      <div className="flex items-center gap-1.5 text-[12px]">
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", !c.ready ? "bg-bad" : c.exhausted_until ? "bg-warn" : "bg-good")} />
        <span className="font-medium text-fg">{c.label}</span>
        <span className="chip">{c.billing === "plan" ? "plan" : "API key"}</span>
        {c.exhausted_until && <span className="chip text-warn">spent · back {clockLabel(c.exhausted_until)}</span>}
        <span className="ml-auto truncate text-[11px] text-faint" title={c.roles.join(", ")}>
          {c.roles.length ? c.roles.join(", ") : c.ready ? "standby" : "not connected"}
        </span>
      </div>
      {c.windows?.windows.map((w) => <WindowBar key={w.name} win={w} threshold={threshold} />)}
      {c.windows?.note && <p className="text-[10px] text-faint">{c.windows.note}</p>}
      <p className="text-[11px] text-faint">
        Today: {c.spend_today.length ? spendLine(c.spend_today) : "no calls"}
        {c.cost_today != null && ` · $${c.cost_today.toFixed(3)}`}
        {c.cost_month != null && ` · 30 days $${c.cost_month.toFixed(2)}`}
      </p>
    </div>
  );
}

function RoleLine({ r }: { r: RoleView }) {
  const effort = r.configured.effort ? ` · ${r.configured.effort}` : "";
  return (
    <div className="flex min-w-0 items-center gap-1 text-[11px]" title={`${r.label}: ${PROVIDER_LABEL[r.configured.provider]} ${r.configured.model}${effort}`}>
      <span className="w-[5.5rem] shrink-0 text-faint">{r.label}</span>
      <span className={cn("min-w-0 truncate", r.switched ? "text-faint line-through" : "text-fg")}>
        {r.configured.model}
        <span className="text-faint">{effort}</span>
      </span>
      {r.replacement && (
        <>
          <ArrowRight size={10} className={cn("shrink-0", r.switched ? "text-warn" : "text-faint")} />
          <span
            className={cn("min-w-0 truncate", r.switched ? "font-medium text-warn" : "text-faint")}
            title={r.switched ? "Serving now: the configured plan is spent" : "Takes over when the plan runs out"}
          >
            {r.replacement.model}
          </span>
        </>
      )}
    </div>
  );
}

/** Models, plans and spend, with what a spent plan does; collapsed by default. */
export function ConnectionsPanel() {
  const qc = useQueryClient();
  const running = useChat((st) => st.running);
  const view = useQuery({
    queryKey: ["connections"],
    queryFn: api.connections,
    refetchInterval: running ? 15_000 : 60_000,
  });
  const setMode = useMutation({
    mutationFn: (on_limit: OnLimit) => api.updateHandover({ on_limit }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["connections"] });
      void qc.invalidateQueries({ queryKey: ["state"] });
    },
  });
  const data = view.data;
  const spent = data?.connections.some((c) => c.exhausted_until);
  const warn = data?.connections.some((c) => c.windows?.windows.some((w) => (w.used_percent ?? 0) >= (data?.handover_threshold ?? 90)));

  return (
    <details className="shrink-0 border-b border-border px-3 py-2 text-[12px]">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-[11px] text-muted">
        <span className="label">Models &amp; usage</span>
        {view.isPending && <Loader2 size={11} className="animate-spin text-faint" />}
        {view.isError && <span className="text-bad">unavailable</span>}
        {data && (
          <span className={cn("truncate", spent ? "text-warn" : warn ? "text-warn" : "text-faint")}>
            {spent ? "a plan is spent" : warn ? "a plan is nearly spent" : `${data.connections.filter((c) => c.ready).length} connected`}
          </span>
        )}
        <button
          className="ml-auto text-faint hover:text-fg"
          title="Refresh"
          onClick={(e) => {
            e.preventDefault();
            void view.refetch();
          }}
        >
          <RefreshCw size={11} className={cn(view.isFetching && "animate-spin")} />
        </button>
      </summary>
      {view.isError && <p className="mt-2 text-[11px] text-bad">Could not read the connections: {(view.error as Error).message}</p>}
      {data && (
        <Resizable id="connections" className="mt-2 space-y-3" fit="max-h-[45vh] overflow-y-auto scroll-thin">
          <div className="space-y-1 rounded-md bg-surface p-2">
            {data.roles.map((r) => <RoleLine key={r.role} r={r} />)}
          </div>
          <div className="space-y-1">
            <p className="text-[11px] text-muted">When a plan runs out mid-task</p>
            <div className="flex rounded-md border border-border text-[11px]">
              {([["switch", "Switch to the other plan"], ["ask", "Ask me"]] as const).map(([id, label]) => (
                <button
                  key={id}
                  disabled={setMode.isPending}
                  onClick={() => setMode.mutate(id)}
                  className={cn("flex-1 px-2 py-1", data.on_limit === id ? "bg-accent-bg text-accent" : "text-faint hover:text-fg")}
                >
                  {label}
                </button>
              ))}
            </div>
            <p className="text-[10px] text-faint">
              Plans only (Claude Code ⇄ Codex): the model of the same standing takes over at the same effort. Providers billed per token are never switched to automatically.
            </p>
            {setMode.isError && <p className="text-[11px] text-bad">{(setMode.error as Error).message}</p>}
          </div>
          {data.connections.length === 0 ? (
            <p className="text-[11px] text-faint">No model connected yet. Open Settings to connect one.</p>
          ) : (
            data.connections.map((c) => <ConnectionCard key={c.id} c={c} threshold={data.handover_threshold} />)
          )}
        </Resizable>
      )}
    </details>
  );
}
