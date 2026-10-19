import { useQueries } from "@tanstack/react-query";
import { Check, Hourglass, LayoutGrid, Rows3, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { STAGE_LABEL, STAGES } from "../lib/labels";
import type { ProgressEvent, TaskProgress } from "../lib/types";
import { cn } from "../lib/utils";
import { useActivity } from "../stores/activityStore";
import { useChat } from "../stores/chatStore";
import { useSearch } from "../stores/searchStore";
import { duration, useNow } from "./TaskProgress";
import { Resizable } from "./ui";
import { sessionHint, useConnections, UsageSummary } from "./UsageSummary";

// The right-hand panel's live view of the work: the current task as a stepper (an agent walks
// toward the next step), and each agent at work as a progress track with its emoji riding the
// front; below them, how much of each plan is left and today's tokens (UsageSummary). Real
// counts drive a track when the work reports them (sources finished, postings judged, task
// steps done); otherwise it advances with each action and eases toward the end.

type Kind = "assistant" | "scout" | "reader" | "screener" | "reviewer" | "profile" | "writer" | "letter";
type Status = "running" | "done" | "failed";

const KINDS: Record<Kind, { label: string; emoji: string; color: string }> = {
  assistant: { label: "Assistant", emoji: "🤖", color: "var(--agent-assistant)" },
  profile: { label: "Profile", emoji: "🧭", color: "var(--agent-profile)" },
  scout: { label: "Sources", emoji: "🔎", color: "var(--agent-scout)" },
  reader: { label: "Reader", emoji: "📄", color: "var(--agent-reader)" },
  screener: { label: "Screener", emoji: "🔬", color: "var(--agent-screener)" },
  reviewer: { label: "Second opinion", emoji: "🧐", color: "var(--agent-reviewer)" },
  writer: { label: "CV writer", emoji: "✍️", color: "var(--agent-writer)" },
  letter: { label: "Letter writer", emoji: "✉️", color: "var(--agent-letter)" },
};

interface AgentRow {
  id: string;
  kind: Kind;
  status: Status;
  detail: string;
  started: number; // ms
  ended: number | null;
  /** Real share done (0-1), or null: eased by `calls`. */
  fraction: number | null;
  calls: number;
}

const LINGER_MS = 5000; // a finished agent stays this long at the end of its track
const VIEW_KEY = "jobsearch.agentsView";

// ------------------------------------------------------------------ rows from the live work

function searchRows(log: ProgressEvent[], running: boolean, failed: boolean): AgentRow[] {
  if (!log.length) return [];
  const later = (stages: string[]) => {
    const after = Math.max(...stages.map((st) => STAGES.indexOf(st)));
    return log.find((ev) => STAGES.indexOf(ev.stage) > after)?.at ?? null;
  };
  const lastAt = log[log.length - 1].at;
  const row = (id: string, kind: Kind, events: ProgressEvent[], fraction: number | null, detail: string): AgentRow => {
    const end = later([...new Set(events.map((ev) => ev.stage))]) ?? (running ? null : lastAt);
    const status: Status = end ? "done" : failed ? "failed" : "running";
    return { id, kind, status, detail, started: events[0].at, ended: end, fraction, calls: events.length };
  };
  const out: AgentRow[] = [];
  const profile = log.filter((ev) => ev.stage === "cv" || ev.stage === "summary");
  if (profile.length) out.push(row("profile", "profile", profile, null, profile[profile.length - 1].message));
  const capture = log.filter((ev) => ev.stage === "capture");
  if (capture.length) {
    const sources = new Map<string, string | undefined>();
    for (const ev of capture) if (ev.source) sources.set(ev.source, ev.status);
    const finished = [...sources.values()].filter((st) => st !== "running").length;
    const fraction = sources.size ? finished / sources.size : null;
    out.push(row("scout", "scout", capture, fraction, sources.size ? `${finished} of ${sources.size} sources` : capture[capture.length - 1].message));
  }
  const enrich = log.filter((ev) => ev.stage === "enrich");
  if (enrich.length) out.push(row("reader", "reader", enrich, null, enrich[enrich.length - 1].message));
  const counted = (phase?: string) => {
    const events = log.filter((ev) => ev.stage === "screen" && ev.total && ev.phase === phase);
    const last = events[events.length - 1];
    return { events, fraction: last?.total ? (last.done ?? 0) / last.total : null, detail: last?.total ? `${last.done ?? 0} of ${last.total} postings` : "" };
  };
  const first = counted(undefined);
  if (first.events.length) out.push(row("screener", "screener", first.events, first.fraction, first.detail));
  const review = counted("review");
  if (review.events.length) out.push(row("reviewer", "reviewer", review.events, review.fraction, review.detail));
  return out;
}

function taskKind(title: string): Kind {
  if (/letter/i.test(title)) return "letter";
  if (/profile|summary/i.test(title)) return "profile";
  return "writer";
}

function taskRow(task: TaskProgress, seenAt: number): AgentRow {
  const total = Math.max(task.total, task.done + (task.step ? 1 : 0), 1);
  return {
    id: `task:${task.id}`,
    kind: taskKind(task.title),
    status: task.error && task.error !== "Stopped" ? "failed" : task.finished ? "done" : "running",
    detail: task.error ?? (task.waiting_on ? `${task.step} · waiting for ${task.waiting_on}` : task.step || task.title),
    started: seenAt,
    ended: task.finished || task.error ? task.updated_at * 1000 : null,
    fraction: task.finished ? 1 : task.done / total,
    calls: task.done,
  };
}

/** How long an ended task stays listed (s), and how long one may take to start (ms). */
const TASK_LINGER_S = 30;
const TASK_START_GRACE_MS = 60_000;

function useRows(): { rows: AgentRow[]; tasks: TaskProgress[] } {
  const chat = useChat();
  const { log, loading, error } = useSearch();
  const ids = useActivity((st) => st.tasks);
  const polls = useQueries({
    queries: Object.keys(ids).map((id) => ({
      queryKey: ["task-progress", id],
      queryFn: () => api.taskProgress(id),
      refetchInterval: 1000,
      retry: false,
    })),
  });
  const tasks = polls.map((p) => p.data).filter((t): t is TaskProgress => !!t);
  // A task leaves the panel a while after it ended, or when the server never knew it.
  useEffect(() => {
    const now = Date.now();
    Object.keys(ids).forEach((id, i) => {
      const { data, isError } = polls[i] ?? {};
      const ended = data && (data.finished || data.error) && data.now - data.updated_at > TASK_LINGER_S;
      const unknown = isError && now - ids[id] > TASK_START_GRACE_MS;
      if (ended || unknown) useActivity.getState().removeTask(id);
    });
  });
  const rows: AgentRow[] = [];
  const turn = chat.items.slice(chat.turnStart).filter((item) => item.kind === "activity");
  if (chat.turnAt) {
    const last = turn[turn.length - 1];
    rows.push({
      id: "assistant",
      kind: "assistant",
      status: chat.running ? "running" : chat.items[chat.items.length - 1]?.kind === "error" ? "failed" : "done",
      detail: chat.running ? (last?.kind === "activity" ? last.text : chat.status ?? "thinking") : "",
      started: chat.turnAt.start,
      ended: chat.running ? null : chat.turnAt.end,
      fraction: null,
      calls: turn.length,
    });
  }
  rows.push(...searchRows(log, loading, !!error));
  rows.push(...tasks.map((t) => taskRow(t, ids[t.id])));
  return { rows, tasks };
}

// ------------------------------------------------------------------ pieces

function progressOf(a: AgentRow): number {
  if (a.status !== "running") return 1;
  if (a.fraction != null) return Math.min(0.97, Math.max(0.04, a.fraction));
  return 0.05 + 0.87 * (1 - Math.exp(-a.calls / 12));
}

function StatusDot({ status, color }: { status: Status; color: string }) {
  if (status === "running")
    return (
      <span className="relative flex h-2 w-2 shrink-0">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" style={{ background: color }} />
        <span className="relative inline-flex h-2 w-2 rounded-full" style={{ background: color }} />
      </span>
    );
  return <span className={cn("h-2 w-2 shrink-0 rounded-full", status === "failed" ? "bg-bad" : "bg-good")} />;
}

function Track({ a, thin }: { a: AgentRow; thin?: boolean }) {
  const k = KINDS[a.kind];
  const p = progressOf(a);
  const live = a.status === "running";
  return (
    <div className={cn("relative rounded-full bg-surface", thin ? "mt-2.5 h-1" : "mt-2 h-1.5")}>
      <div
        className={cn("track-fill absolute left-0 top-0 h-full rounded-full", live && "lane-running")}
        style={{ width: `${p * 100}%`, backgroundColor: a.status === "failed" ? "var(--bad)" : k.color }}
      />
      <span
        className={cn("rider pointer-events-none absolute leading-none", thin ? "-top-[0.6rem] text-[12px]" : "-top-[0.65rem] text-[14px]", live && "rider-live")}
        style={{ left: `${p * 100}%` }}
        aria-hidden
      >
        {a.status === "failed" ? "💥" : k.emoji}
      </span>
    </div>
  );
}

function elapsedLabel(a: AgentRow, now: number) {
  return duration(((a.ended ?? now) - a.started) / 1000);
}

function Lanes({ rows, now }: { rows: AgentRow[]; now: number }) {
  return (
    <div className="space-y-3 pt-1">
      {rows.map((a) => {
        const k = KINDS[a.kind];
        return (
          <div key={a.id} className={cn(a.status !== "running" && "agent-leaving")} title={`${k.label}: ${a.detail}`}>
            <div className="flex items-center gap-1.5 text-[11px]">
              <span className="shrink-0 font-medium" style={{ color: k.color }}>{k.label}</span>
              <span className="min-w-0 flex-1 truncate text-faint">{a.detail}</span>
              <span className={cn("shrink-0 tabular-nums", a.status === "running" ? "text-accent" : a.status === "failed" ? "text-bad" : "text-faint")}>
                {a.status === "running" ? elapsedLabel(a, now) : `${a.status} · ${elapsedLabel(a, now)}`}
              </span>
            </div>
            <Track a={a} />
          </div>
        );
      })}
    </div>
  );
}

function Cards({ rows, now }: { rows: AgentRow[]; now: number }) {
  return (
    <div className="space-y-2">
      {rows.map((a) => {
        const k = KINDS[a.kind];
        return (
          <div
            key={a.id}
            className={cn("rounded-lg border bg-surface p-2.5", a.status === "running" ? "border-accent/50 shadow-sm" : "agent-leaving border-border")}
            style={{ borderLeft: `3px solid ${k.color}` }}
          >
            <div className="flex items-center gap-1.5 text-[12px]">
              <StatusDot status={a.status} color={k.color} />
              <span className="font-medium" style={{ color: k.color }}>{k.emoji} {k.label}</span>
              <span className={cn("ml-auto text-[11px]", a.status === "running" ? "text-accent" : a.status === "failed" ? "text-bad" : "text-faint")}>{a.status}</span>
            </div>
            {a.detail && <div className="mt-0.5 line-clamp-2 text-[11px] text-fg">{a.detail}</div>}
            <div className="mt-1 flex flex-wrap gap-x-2 text-[11px] text-faint">
              <span className="tabular-nums">{elapsedLabel(a, now)}</span>
              {a.fraction == null && <span>{a.calls} action{a.calls === 1 ? "" : "s"}</span>}
              {a.fraction != null && <span>{Math.round(progressOf(a) * 100)}%</span>}
            </div>
            <Track a={a} thin />
          </div>
        );
      })}
    </div>
  );
}

interface Step {
  label: string;
  state: "done" | "current" | "pending";
}

/** The steps of the current piece of work: the search pipeline, else a document task. */
function useSteps(tasks: TaskProgress[]): { title: string; steps: Step[]; running: boolean } | null {
  const { log, loading } = useSearch();
  if (loading || log.length) {
    const shown = STAGES.filter((st) => st !== "cv" && st !== "done");
    const reached = Math.max(0, ...log.map((ev) => STAGES.indexOf(ev.stage)));
    const finished = !loading;
    return {
      title: loading ? "Search in progress" : "Last search",
      running: loading,
      steps: shown.map((st) => {
        const i = STAGES.indexOf(st);
        const state = finished || i < reached ? "done" : i === reached || (st === "summary" && reached === 0) ? "current" : "pending";
        return { label: STAGE_LABEL[st] ?? st, state };
      }),
    };
  }
  const task = tasks.find((t) => !t.finished && !t.error) ?? tasks[0];
  if (!task) return null;
  const total = Math.max(task.total, task.done + (task.step ? 1 : 0), 1);
  const steps: Step[] = task.completed.map((label) => ({ label, state: "done" }));
  if (task.step && !task.finished) steps.push({ label: task.step, state: "current" });
  for (let i = steps.length; i < Math.min(total, 6); i++) steps.push({ label: "…", state: "pending" });
  return { title: task.title, running: !task.finished, steps: steps.slice(-6) };
}

function Stepper({ title, steps, running, walker }: { title: string; steps: Step[]; running: boolean; walker: string }) {
  return (
    <div>
      <div className="flex items-baseline gap-1.5 text-[12px]">
        <span className="font-medium text-fg">{title}</span>
        <span className={cn("text-[11px]", running ? "text-accent" : "text-faint")}>{running ? "in progress" : "finished"}</span>
      </div>
      <div className="mt-2.5 flex items-start">
        {steps.map((st, i) => {
          const done = st.state === "done";
          const now = st.state === "current";
          return (
            <div key={`${st.label}-${i}`} className="flex min-w-0 flex-1 flex-col items-center">
              <div className="relative flex w-full items-center">
                <div className={cn("h-0.5 flex-1", i === 0 ? "opacity-0" : done || now ? "bg-good" : "bg-border")} />
                <div
                  className={cn(
                    "relative flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 text-[10px] font-semibold",
                    done ? "border-good bg-good text-bg" : now ? "border-accent text-accent" : "border-border text-faint",
                  )}
                >
                  {done ? <Check size={10} strokeWidth={3} /> : i + 1}
                  {now && running && <span className="absolute -inset-1 animate-ping rounded-full border border-accent opacity-40" />}
                </div>
                <div className="relative h-0.5 flex-1 bg-border">
                  {now && running && i < steps.length - 1 && (
                    <span className="walker pointer-events-none absolute -top-[0.6rem] text-[12px] leading-none" aria-hidden>{walker}</span>
                  )}
                </div>
              </div>
              <span className={cn("mt-1 max-w-full truncate px-0.5 text-[10px]", now ? "font-medium text-accent" : done ? "text-fg" : "text-faint")} title={st.label}>
                {st.label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function WaitingBanner() {
  const { waiting, cancelWait } = useActivity();
  const now = useNow(!!waiting) * 1000;
  if (!waiting) return null;
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-[11px]">
      <Hourglass size={12} className="shrink-0 text-warn" />
      <span className="min-w-0 flex-1">
        {waiting.label} at {new Date(waiting.at).toLocaleTimeString([], { timeStyle: "short" })}
        <span className="text-faint"> · in {duration((waiting.at - now) / 1000)}</span>
      </span>
      <button className="shrink-0 text-faint hover:text-fg" title="Cancel the wait" onClick={cancelWait}>
        <X size={12} />
      </button>
    </div>
  );
}

// ------------------------------------------------------------------ the section

/** Progress and agents: what is running now, on the right-hand panel. */
export function ActivityPanel() {
  const { rows, tasks } = useRows();
  const steps = useSteps(tasks);
  const now = useNow(true) * 1000;
  const [mode, setModeState] = useState<"lanes" | "cards">(() => {
    try {
      return localStorage.getItem(VIEW_KEY) === "cards" ? "cards" : "lanes";
    } catch {
      return "lanes";
    }
  });
  const setMode = (next: "lanes" | "cards") => {
    setModeState(next);
    try {
      localStorage.setItem(VIEW_KEY, next);
    } catch {
      /* the choice just isn't remembered */
    }
  };
  const shown = rows.filter((a) => a.status === "running" || (a.ended != null && now - a.ended < LINGER_MS));
  const running = rows.filter((a) => a.status === "running");
  const walker = running.length ? KINDS[running[running.length - 1].kind].emoji : "🤖";
  const [open, setOpen] = useState(true);
  const hint = sessionHint(useConnections(running.length > 0).data);
  useEffect(() => {
    if (running.length) setOpen(true); // new work reopens the section
  }, [running.length]);

  return (
    <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)} className="shrink-0 border-b border-border px-3 py-2 text-[12px]">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-[11px] text-muted">
        <span className="label">Activity</span>
        <span className="text-faint">
          {running.length ? `${running.length} agent${running.length === 1 ? "" : "s"} at work` : "nothing running"}
          {hint && ` · ${hint}`}
        </span>
        {open && (
          <span className="ml-auto flex rounded-md border border-border" onClick={(e) => e.preventDefault()}>
            {([["lanes", <Rows3 key="l" size={11} />, "Progress tracks"], ["cards", <LayoutGrid key="c" size={11} />, "Cards"]] as const).map(([id, icon, title]) => (
              <button key={id} title={title} onClick={() => setMode(id)} className={cn("px-1.5 py-0.5", mode === id ? "bg-accent-bg text-accent" : "text-faint hover:text-fg")}>
                {icon}
              </button>
            ))}
          </span>
        )}
      </summary>
      <Resizable id="activity" className="mt-2 space-y-3" fit="max-h-[calc(var(--vh)*50)] overflow-y-auto scroll-thin">
        <WaitingBanner />
        {steps ? <Stepper {...steps} walker={walker} /> : <p className="text-[11px] text-faint">No task yet. Searches, documents and assistant turns show here while they run.</p>}
        {shown.length > 0 && (mode === "lanes" ? <Lanes rows={shown} now={now} /> : <Cards rows={shown} now={now} />)}
        <UsageSummary busy={running.length > 0} />
      </Resizable>
    </details>
  );
}
