import { Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { notSelectedTab, type ResultTab } from "../lib/resultGroups";
import type { MatchReport, MatchResult, SavedJob, TrackedJob } from "../lib/types";
import { cn } from "../lib/utils";

export type ListView = "results" | "saved" | "applied";

/** Where a found job is shown: its view, its results list and a label for the user. */
export interface FoundJob {
  jobId: string;
  title: string;
  company: string;
  location: string | null;
  view: ListView;
  tab?: ResultTab;
  list: string;
  score: number | null;
}

const LIST_LABEL: Record<ResultTab, string> = {
  matches: "Matches",
  to_check: "To check",
  adjacent: "Adjacent possibilities",
  below_threshold: "Not selected",
  not_screened: "Not screened",
  excluded: "Excluded",
  dismissed: "Set aside",
};

function fromResult(r: MatchResult, view: ListView, list: string, tab?: ResultTab): FoundJob {
  const { job } = r;
  return {
    jobId: job.id, title: job.title, company: job.company, location: job.location ?? null,
    view, tab, list, score: r.verdict?.fit_score ?? r.score?.total ?? null,
  };
}

/** Every job in the app's lists: the current search's lists, Saved and Applied. A job
 * that is saved or applied for is shown there, as the results lists do. */
export function jobsInLists(report: MatchReport | null | undefined, saved: SavedJob[], tracked: TrackedJob[]): FoundJob[] {
  const applied = tracked.filter((entry) => entry.status === "applied");
  const appliedIds = new Set(applied.flatMap((entry) => [entry.id, entry.job_id ?? ""]));
  const savedIds = new Set(saved.map((s) => s.result.job.id));
  const out: FoundJob[] = applied.map((entry) => ({
    jobId: entry.job_id ?? entry.id, title: entry.title, company: entry.company, location: entry.location,
    view: "applied", list: "Applied", score: entry.fit_score ?? null,
  }));
  out.push(...saved.filter((s) => !appliedIds.has(s.result.job.id)).map((s) => fromResult(s.result, "saved", "Saved")));
  if (report) {
    const lists: [ResultTab | null, MatchResult[]][] = [
      ["matches", report.matches], ["to_check", report.to_check ?? []], [null, report.below_threshold],
      ["excluded", report.excluded], ["dismissed", report.dismissed ?? []],
    ];
    for (const [fixed, results] of lists) {
      for (const r of results) {
        if (appliedIds.has(r.job.id) || savedIds.has(r.job.id) || r.tracking?.status === "applied") continue;
        const tab = fixed ?? notSelectedTab(r, report.screened);
        out.push(fromResult(r, "results", LIST_LABEL[tab], tab));
      }
    }
  }
  return out;
}

/** Jobs whose title, employer, place or ID contain every word typed, best first. */
export function findJobs(jobs: FoundJob[], text: string): FoundJob[] {
  const words = text.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const seen = new Set<string>();
  const inTitle = (job: FoundJob) => (words.every((w) => job.title.toLocaleLowerCase().includes(w)) ? 0 : 1);
  return jobs
    .filter((job) => {
      const hay = `${job.title} ${job.company} ${job.location ?? ""} ${job.jobId}`.toLocaleLowerCase();
      if (seen.has(job.jobId) || !words.every((word) => hay.includes(word))) return false;
      seen.add(job.jobId);
      return true;
    })
    .sort((a, b) => inTitle(a) - inTitle(b));
}

/** Search the app's own lists (not the web) and jump to the job's card. */
export function FindInLists({ jobs, onPick }: { jobs: FoundJob[]; onPick: (job: FoundJob) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const found = useMemo(() => findJobs(jobs, text), [jobs, text]);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => { if (!box.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const pick = (job: FoundJob) => { onPick(job); setOpen(false); };
  return (
    <div ref={box} className="relative ml-auto">
      {open ? (
        <div className="flex items-center gap-1 rounded-md border border-border bg-panel px-2 py-0.5">
          <Search size={13} className="shrink-0 text-faint" />
          <input
            autoFocus
            className="w-48 bg-transparent py-0.5 text-[12px] text-fg outline-none placeholder:text-faint"
            placeholder="Title, company, place or ID"
            aria-label="Find a job in your lists"
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setOpen(false);
              if (event.key === "Enter" && found[0]) pick(found[0]);
            }}
          />
          <button className="text-faint hover:text-fg" aria-label="Close" onClick={() => setOpen(false)}><X size={13} /></button>
        </div>
      ) : (
        <button
          className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] text-muted hover:text-fg"
          title="Find a job already in the app: search results (every list), Saved and Applied"
          onClick={() => setOpen(true)}
        >
          <Search size={13} /> Find in lists
        </button>
      )}
      {open && text.trim() && (
        <div className="absolute right-0 top-full z-30 mt-1 max-h-96 w-[min(28rem,calc(100vw-2rem))] overflow-y-auto scroll-thin rounded-md border border-border bg-panel p-1 shadow-lg" role="listbox">
          {found.length === 0 ? (
            <p className="p-2 text-[12px] text-muted">
              No job in your results, Saved or Applied lists matches. To find a posting online, use <b>Find one job</b> next to Search.
            </p>
          ) : found.slice(0, 40).map((job) => (
            <button
              key={`${job.view}:${job.jobId}`}
              role="option"
              aria-selected={false}
              className="flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-surface"
              onClick={() => pick(job)}
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[12px] font-medium text-fg">{job.title}</span>
                <span className="block truncate text-[11px] text-muted">{job.company}{job.location ? ` · ${job.location}` : ""}</span>
              </span>
              <span className={cn("chip shrink-0 text-[10px]", job.view === "results" ? "text-muted" : "text-accent")}>{job.list}</span>
              {job.score != null && <span className="shrink-0 text-[11px] tabular-nums text-faint">{Math.round(job.score)}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
