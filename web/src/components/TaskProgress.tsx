import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { cn } from "../lib/utils";

/** An id per run of a long action, sent as `progress_id` so `TaskProgress` can follow it. */
export function useTaskId() {
  const [id, setId] = useState<string | null>(null);
  const next = () => {
    const fresh = crypto.randomUUID();
    setId(fresh);
    return fresh;
  };
  return { id, next };
}

/** Seconds as "45 s" or "2 min 05 s". */
export const duration = (seconds: number) => {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
};

/** A ticking clock (seconds), so elapsed times move between polls. */
export function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

/** A thin bar with an optional label row; `fraction` null shows an indeterminate stripe. */
export function ProgressBar({ fraction, className }: { fraction: number | null; className?: string }) {
  return (
    <div className={cn("h-1.5 overflow-hidden rounded bg-surface", className)} role="progressbar"
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={fraction === null ? undefined : Math.round(fraction * 100)}>
      <div
        className={cn("h-full bg-[var(--accent)] transition-all duration-500", fraction === null && "w-1/3 animate-pulse")}
        style={fraction === null ? undefined : { width: `${Math.max(4, Math.round(fraction * 100))}%` }}
      />
    </div>
  );
}

/** Live progress of a request started with `progress_id`: real steps done of the total, the
 *  current step and how long it has run, the model call in flight, and a warning when the
 *  server stops answering. Render it while the request is pending. */
export function TaskProgress({ id, className }: { id: string | null; className?: string }) {
  const poll = useQuery({
    queryKey: ["task-progress", id],
    queryFn: () => api.taskProgress(id ?? ""),
    enabled: !!id,
    refetchInterval: 1000,
    retry: false,
    gcTime: 0,
  });
  const [offset, setOffset] = useState(0); // server clock minus this browser's clock
  useEffect(() => {
    if (poll.data) setOffset(poll.data.now - Date.now() / 1000);
  }, [poll.data]);
  const now = useNow(!!id) + offset;
  const task = poll.data;
  const lost = poll.isError && !poll.error.message.includes("No task");
  if (!id) return null;
  if (!task) {
    return (
      <div className={cn("space-y-1 text-[11px] text-muted", className)} aria-live="polite">
        <p className="flex items-center gap-1.5"><Loader2 size={12} className="animate-spin text-accent" /> Starting…</p>
        <ProgressBar fraction={null} />
      </div>
    );
  }
  const total = Math.max(task.total, task.done + (task.step ? 1 : 0), 1);
  const fraction = task.finished ? 1 : task.done / total;
  return (
    <div className={cn("space-y-1 text-[11px]", className)} aria-live="polite">
      <div className="flex items-center justify-between gap-2">
        <p className="flex min-w-0 items-center gap-1.5 text-fg">
          {task.error ? <AlertTriangle size={12} className="shrink-0 text-bad" />
            : task.finished ? <CheckCircle2 size={12} className="shrink-0 text-good" />
            : <Loader2 size={12} className="shrink-0 animate-spin text-accent" />}
          <span className="truncate">
            {task.error ? task.error : task.finished ? "Done" : `Step ${Math.min(task.done + 1, total)} of ${total}: ${task.step || "starting"}`}
          </span>
        </p>
        <span className="shrink-0 tabular-nums text-faint">{duration(now - task.started_at)}</span>
      </div>
      <ProgressBar fraction={fraction} />
      {!task.finished && (
        <p className={cn("text-faint", lost && "text-warn")}>
          {lost
            ? "Lost contact with the server. Is it still running? (bash scripts/dev.sh)"
            : task.waiting_on && task.waiting_since
              ? `Waiting for the model: ${task.waiting_on} · ${duration(now - task.waiting_since)}. Long answers can take a few minutes.`
              : `This step has run ${duration(now - task.step_started_at)}.`}
        </p>
      )}
    </div>
  );
}
