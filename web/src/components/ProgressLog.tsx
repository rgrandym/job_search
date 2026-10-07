import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import type { Heartbeat, ProgressEvent } from "../lib/types";
import { cn } from "../lib/utils";
import { duration, ProgressBar, useNow } from "./TaskProgress";

// The pipeline's stages in order; overall progress is the stage reached plus its own share done.
const STAGES = ["cv", "summary", "plan", "capture", "prefilter", "enrich", "screen", "done"];

/** Overall search progress (0-1) from the events so far: real counts within sources and
 *  screening, stage position otherwise. */
export function searchFraction(log: ProgressEvent[]): number {
  const reached = Math.max(0, ...log.map((ev) => STAGES.indexOf(ev.stage)));
  if (STAGES[reached] === "done") return 1;
  let within = 0;
  if (STAGES[reached] === "capture") {
    const sources = new Map<string, string | undefined>();
    for (const ev of log) if (ev.source) sources.set(ev.source, ev.status);
    const finished = [...sources.values()].filter((status) => status !== "running").length;
    within = sources.size ? finished / sources.size : 0;
  } else if (STAGES[reached] === "screen") {
    // The first pass fills most of the stage, the second opinions the rest.
    const share = (phase?: string) => {
      const latest = [...log].reverse().find((ev) => ev.stage === "screen" && ev.total && ev.phase === phase);
      return latest?.total ? (latest.done ?? 0) / latest.total : 0;
    };
    const review = log.some((ev) => ev.phase === "review");
    within = review ? 0.8 + 0.2 * share("review") : share(undefined);
  }
  return (reached + within) / (STAGES.length - 1);
}

/** Elapsed time, overall bar and what the server is waiting on, above a running search's log. */
export function SearchStatus({ log, startedAt, beat }: { log: ProgressEvent[]; startedAt: number | null; beat: Heartbeat | null }) {
  const now = useNow(true) * 1000;
  const last = log.length ? log[log.length - 1].at : startedAt ?? now;
  const quiet = (now - Math.max(last, beat?.at ?? 0)) / 1000;
  return (
    <div className="space-y-1 text-[11px]" aria-live="polite">
      <div className="flex justify-between gap-2 text-faint">
        <span>{Math.round(searchFraction(log) * 100)}% · stage {STAGE_LABEL[log[log.length - 1]?.stage] ?? "starting"}</span>
        {startedAt && <span className="tabular-nums">{duration((now - startedAt) / 1000)}</span>}
      </div>
      <ProgressBar fraction={searchFraction(log)} />
      <p className={cn("text-faint", quiet > 15 && "text-warn")}>
        {quiet > 15
          ? `No word from the server for ${duration(quiet)}. It may have stopped; check that it is running.`
          : beat?.waiting_on
            ? `Waiting for the model: ${beat.waiting_on}${beat.waiting_s !== undefined ? ` · ${duration(beat.waiting_s)}` : ""}`
            : `Last update ${duration((now - last) / 1000)} ago`}
      </p>
    </div>
  );
}

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
 *  screening progress (and one for the second opinions), and every other message in order. */
function rows(log: ProgressEvent[]): ProgressEvent[] {
  const out: ProgressEvent[] = [];
  const bySource = new Map<string, number>();
  const screenRows = new Map<string, number>();
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
      const key = ev.phase ?? "first";
      const at = screenRows.get(key);
      if (at === undefined) {
        screenRows.set(key, out.length);
        out.push(ev);
      } else if (ev.phase) {
        out[at] = ev; // its message is the count itself
      } else {
        out[at] = { ...ev, message: `${out[at].message.split(" — ")[0]} — ${ev.message}` };
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
