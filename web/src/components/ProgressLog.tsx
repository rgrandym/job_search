import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import type { ProgressEvent } from "../lib/types";
import { cn } from "../lib/utils";

const STAGE_LABEL: Record<string, string> = {
  cv: "CV",
  summary: "Profile",
  plan: "Plan",
  capture: "Sources",
  prefilter: "Pre-filter",
  enrich: "Details",
  screen: "Matching",
  done: "Done",
};

/** Collapse the raw event stream into display rows: one row per source, one live row for
 *  screening progress, and every other message in order. */
function rows(log: ProgressEvent[]): ProgressEvent[] {
  const out: ProgressEvent[] = [];
  const bySource = new Map<string, number>();
  let screenRow = -1;
  for (const ev of log) {
    if (ev.source) {
      const at = bySource.get(ev.source);
      if (at === undefined) {
        bySource.set(ev.source, out.length);
        out.push(ev);
      } else {
        // keep the task description, mark the outcome
        out[at] = { ...ev, message: ev.status === "running" ? ev.message : `${out[at].message} → ${ev.message}` };
      }
    } else if (ev.stage === "screen" && ev.total !== undefined) {
      if (screenRow < 0) {
        screenRow = out.length;
        out.push(ev);
      } else {
        out[screenRow] = { ...ev, message: `${out[screenRow].message.split(" — ")[0]} — ${ev.message}` };
      }
    } else {
      out.push(ev);
    }
  }
  return out;
}

export function ProgressLog({ log, running }: { log: ProgressEvent[]; running: boolean }) {
  if (!log.length) return null;
  const items = rows(log);
  const screening = [...log].reverse().find((ev) => ev.stage === "screen" && ev.total);
  return (
    <ol className="space-y-1 text-[11px]">
      {items.map((ev, i) => {
        const last = i === items.length - 1;
        const failed = ev.status === "failed";
        const active = running && (ev.status === "running" || (last && ev.stage !== "done"));
        return (
          <li key={`${ev.stage}-${ev.source ?? i}`} className="flex items-start gap-2">
            {failed ? (
              <XCircle size={12} className="mt-0.5 shrink-0 text-bad" />
            ) : active ? (
              <Loader2 size={12} className="mt-0.5 shrink-0 animate-spin text-accent" />
            ) : (
              <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-good" />
            )}
            <span className="w-14 shrink-0 text-faint sm:w-16">{STAGE_LABEL[ev.stage] ?? ev.stage}</span>
            <span className={cn("min-w-0 [overflow-wrap:anywhere]", failed ? "text-bad" : active ? "text-fg" : "text-muted")}>
              {ev.message}
            </span>
          </li>
        );
      })}
      {running && screening?.total ? (
        <li className="pl-[5rem] sm:pl-[5.5rem]">
          <div className="h-1 overflow-hidden rounded bg-surface">
            <div
              className="h-full bg-[var(--accent)] transition-all"
              style={{ width: `${Math.round(((screening.done ?? 0) / screening.total) * 100)}%` }}
            />
          </div>
        </li>
      ) : null}
    </ol>
  );
}
