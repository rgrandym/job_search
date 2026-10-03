import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bookmark, BookmarkX, BrainCircuit, Download, FileText, History, ListChecks, Loader2, Play, Square, X } from "lucide-react";
import { api } from "../lib/api";
import { useSearchRunner } from "../lib/useSearchRunner";
import { useState } from "react";
import type { MatchResult, SavedJob, SourceReport } from "../lib/types";
import { cn } from "../lib/utils";
import { useSearch } from "../stores/searchStore";
import { JobCard } from "./JobCard";
import { ProgressLog } from "./ProgressLog";
import { SavedJobs } from "./SavedJobs";
import { Empty } from "./ui";

type Tab = "matches" | "below_threshold" | "excluded" | "applied" | "dismissed";

const TAB_LABEL: Record<Tab, string> = {
  matches: "Matches",
  below_threshold: "Not selected",
  excluded: "Excluded",
  applied: "Already applied",
  dismissed: "N/A",
};
const TAB_HINT: Partial<Record<Tab, string>> = {
  applied: "Roles you applied for that surfaced again: set aside before screening, never ranked",
  dismissed: "Jobs you marked N/A: set aside before screening",
};

const SOURCE_NAMES: Record<string, string> = {
  reed: "Reed",
  cv_library: "CV-Library",
  adzuna: "Adzuna",
  linkedin_search: "LinkedIn",
  totaljobs: "Totaljobs",
  jobs_ac_uk: "jobs.ac.uk",
  nhs_jobs: "NHS Jobs",
  biotechnologyjobs: "Biotechnology Jobs",
  company: "Company sites",
  inbox: "LinkedIn/Indeed inbox",
  linkedin: "LinkedIn alerts",
  indeed: "Indeed alerts",
  gmail_alerts: "Gmail alerts",
  demo: "Demo jobs",
};

function SourcesUsed({ sources }: { sources: SourceReport[] }) {
  if (!sources.length) return null;
  const used = sources.filter((src) => src.status === "used");
  const other = sources.filter((src) => src.status !== "used");
  const label = (name: string) => SOURCE_NAMES[name] ?? name;
  return (
    <div className="space-y-0.5 break-words text-[11px]">
      <p className="text-muted">
        <span className="font-medium text-fg">Sources used:</span>{" "}
        {used.length
          ? used.map((src, i) => (
              <span key={src.name} title={src.detail ?? undefined}>
                {i > 0 && " · "}
                {label(src.name)} <b className="text-fg">{src.fetched}</b>
                {src.detail && <span className="text-warn"> ({src.detail})</span>}
              </span>
            ))
          : "none"}
      </p>
      {other.length > 0 && (
        <p className="text-faint">
          Not used:{" "}
          {other.map((src, i) => (
            <span key={src.name}>
              {i > 0 && " · "}
              <span className={src.status === "failed" ? "text-bad" : undefined}>{label(src.name)}</span>{" "}
              ({src.status === "failed" ? "failed" : "skipped"}{src.detail ? `: ${src.detail}` : ""})
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

/** The centre panel: the current search's results, or the jobs the user saved. */
export function ResultsPanel({ hasCv }: { hasCv: boolean }) {
  const [view, setView] = useState<"results" | "saved">("results");
  const saved = useQuery({ queryKey: ["saved"], queryFn: api.saved });
  const savedAt = new Map((saved.data ?? []).map((s) => [s.result.job.id, s.saved_at]));
  const applied = (saved.data ?? []).filter((s) => s.result.tracking?.status === "applied").length;
  const views = [
    { value: "results" as const, label: "Search results", icon: <ListChecks size={13} />, count: null },
    { value: "saved" as const, label: "Saved", icon: <Bookmark size={13} />, count: saved.data?.length ?? 0 },
  ];
  return (
    <div className="flex h-full min-w-0 flex-col">
      <nav className="flex shrink-0 items-center gap-1 border-b border-border px-4 py-1.5" role="tablist">
        {views.map((v) => (
          <button
            key={v.value}
            role="tab"
            aria-selected={view === v.value}
            onClick={() => setView(v.value)}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px]",
              view === v.value ? "bg-accent-bg text-fg" : "text-muted hover:text-fg",
            )}
          >
            {v.icon} {v.label}
            {v.count != null && <span className="text-faint">{v.count}</span>}
            {v.value === "saved" && applied > 0 && <span className="text-good">· {applied} applied</span>}
          </button>
        ))}
      </nav>
      <SelectionBar savedIds={savedAt} saved={saved.data ?? []} hasCv={hasCv} />
      <div className="min-h-0 flex-1">
        {view === "results" ? <SearchResults hasCv={hasCv} savedAt={savedAt} /> : <SavedJobs hasCv={hasCv} query={saved} />}
      </div>
    </div>
  );
}

interface Prepared {
  jobId: string;
  title: string;
  cv?: string;
  letter?: string;
  error?: string;
}

/** Actions on the ticked jobs: save or remove them, and prepare a CV + cover letter for each. */
function SelectionBar({ savedIds, saved, hasCv }: { savedIds: Map<string, string>; saved: SavedJob[]; hasCv: boolean }) {
  const qc = useQueryClient();
  const { selected, outcome, set } = useSearch();
  const [template, setTemplate] = useState("classic");
  const [prepared, setPrepared] = useState<Prepared[]>([]);
  const [working, setWorking] = useState<string | null>(null);
  const report = outcome?.report;
  const results: MatchResult[] = report
    ? [...report.matches, ...report.below_threshold, ...report.excluded, ...(report.applied ?? []), ...(report.dismissed ?? [])]
    : [];
  const inResults = new Set(results.map((r) => r.job.id));
  const titles = new Map([...saved.map((s) => s.result), ...results].map((r) => [r.job.id, `${r.job.title} · ${r.job.company}`]));
  const toSave = selected.filter((id) => inResults.has(id) && !savedIds.has(id));
  const toRemove = selected.filter((id) => savedIds.has(id));
  const done = () => void qc.invalidateQueries({ queryKey: ["saved"] });
  const save = useMutation({ mutationFn: () => api.saveJobs(toSave), onSuccess: done });
  const remove = useMutation({
    mutationFn: () => api.removeSaved(toRemove),
    onSuccess: () => {
      set({ selected: selected.filter((id) => !toRemove.includes(id)) });
      done();
    },
  });
  // One job at a time: each runs the guarded tailoring and the cover letter (they share the
  // job-description analysis), and a failure on one job does not stop the others.
  const prepare = async () => {
    const out: Prepared[] = [];
    for (const jobId of selected) {
      const item: Prepared = { jobId, title: titles.get(jobId) ?? jobId };
      setWorking(item.title);
      try {
        item.cv = (await api.tailor(jobId, template)).download_url;
        item.letter = (await api.coverLetter(jobId, template)).download_url;
      } catch (e) {
        item.error = (e as Error).message;
      }
      out.push(item);
      setPrepared([...out]);
    }
    setWorking(null);
    for (const key of ["saved", "tracker", "state"]) void qc.invalidateQueries({ queryKey: [key] });
  };
  if (!selected.length && !prepared.length) return null;
  const error = (save.error ?? remove.error) as Error | null;
  return (
    <div className="shrink-0 space-y-1.5 border-b border-border bg-accent-bg px-4 py-1.5 text-[12px]">
      {selected.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{selected.length} selected</span>
          {toSave.length > 0 && (
            <button className="btn-primary py-0.5 text-[12px]" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? <Loader2 size={12} className="animate-spin" /> : <Bookmark size={12} />} Save {toSave.length}
            </button>
          )}
          {toRemove.length > 0 && (
            <button className="btn-ghost py-0.5 text-[12px] text-bad" disabled={remove.isPending} onClick={() => remove.mutate()}>
              <BookmarkX size={12} /> Remove {toRemove.length} from saved
            </button>
          )}
          {hasCv && (
            <>
              <select className="input w-auto py-0.5 text-[12px]" value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="classic">Classic</option>
                <option value="modern">Modern</option>
                <option value="compact">Compact</option>
              </select>
              <button
                className="btn-ghost py-0.5 text-[12px]"
                disabled={!!working}
                title="For each selected job: a tailored CV (reviewed, guarded) and a cover letter. Marks the jobs Applied."
                onClick={() => void prepare()}
              >
                {working ? <Loader2 size={12} className="animate-spin" /> : <FileText size={12} />} Tailor CV + cover letter ({selected.length})
              </button>
            </>
          )}
          <button className="ml-auto flex items-center gap-1 text-faint hover:text-fg" onClick={() => set({ selected: [] })}>
            <X size={12} /> Clear
          </button>
        </div>
      )}
      {working && <p className="text-muted">Preparing documents for {working}…</p>}
      {prepared.length > 0 && (
        <div className="space-y-0.5">
          {prepared.map((p) => (
            <p key={p.jobId} className="flex flex-wrap items-center gap-2">
              <span className="min-w-0 truncate">{p.title}</span>
              {p.cv && (
                <a className="flex items-center gap-1 text-accent hover:underline" href={p.cv}>
                  <Download size={11} /> CV
                </a>
              )}
              {p.letter && (
                <a className="flex items-center gap-1 text-accent hover:underline" href={p.letter}>
                  <Download size={11} /> Cover letter
                </a>
              )}
              {p.error && <span className="text-bad">{p.error}</span>}
            </p>
          ))}
          {!working && (
            <button className="text-faint hover:text-fg" onClick={() => setPrepared([])}>
              Dismiss
            </button>
          )}
        </div>
      )}
      {error && <p className="text-bad">{error.message}</p>}
    </div>
  );
}

function SearchResults({ hasCv, savedAt }: { hasCv: boolean; savedAt: Map<string, string> }) {
  const { outcome, loading, progress, error, log, openedFrom, set, runId, selected, toggleSelected } = useSearch();
  const runner = useSearchRunner();
  const stopButton = loading && (
    <button
      className="flex shrink-0 items-center gap-1 text-bad hover:underline disabled:opacity-50"
      disabled={!runId || progress === "Stopping…"}
      title="Stop now; what has been found and judged so far is kept"
      onClick={runner.stop}
    >
      <Square size={12} /> Stop
    </button>
  );
  const [tab, setTab] = useState<Tab>("matches");

  if (!outcome) {
    return (
      <div className="h-full overflow-y-auto scroll-thin">
        {loading ? (
          log.length ? (
            <div className="space-y-3 p-4">
              <p className="flex flex-wrap items-center gap-2 font-medium">
                <Loader2 size={14} className="animate-spin text-accent" /> Search in progress
                <span className="ml-auto text-[12px]">{stopButton}</span>
              </p>
              <ProgressLog log={log} running />
            </div>
          ) : (
            <Empty title={progress ?? "Searching…"}>
              <Loader2 className="mx-auto mt-2 animate-spin" size={18} />
            </Empty>
          )
        ) : error && log.length ? (
          <div className="space-y-3 p-4">
            <p className="break-words font-medium text-bad">{error}</p>
            <ProgressLog log={log} running={false} />
          </div>
        ) : (
          <Empty title={error ?? "No search yet"}>
            Set your filters on the left (leave titles empty to search your profile&apos;s role families), then{" "}
            <b>Search</b>. Tick jobs to save them or to prepare a tailored CV and cover letter.
          </Empty>
        )}
      </div>
    );
  }

  const rep = outcome.report;
  const counts: Record<Tab, number> = {
    matches: rep.matches.length,
    below_threshold: rep.below_threshold.length,
    excluded: rep.excluded.length,
    applied: rep.applied?.length ?? 0,
    dismissed: rep.dismissed?.length ?? 0,
  };
  const items = rep[tab] ?? [];
  const tabs = (Object.keys(TAB_LABEL) as Tab[]).filter(
    (t) => counts[t] > 0 || t === "matches" || t === "below_threshold" || t === "excluded" || t === tab,
  );
  const fresh = [...rep.matches, ...rep.below_threshold].filter((r) => r.tracking?.status === "new").length;
  const borderline = [...rep.matches, ...rep.below_threshold].filter((r) => r.verdict?.borderline).length;
  const problems = { ...outcome.skipped_sources, ...outcome.errors };
  const alertProblem = outcome.alert_only && !!problems.gmail_alerts;
  const emptyTitle = alertProblem
    ? "Could not read job alerts"
    : outcome.alert_only && outcome.fetched === 0
      ? "No new alerts for this CV"
      : tab === "matches" ? "No true matches" : "Nothing here";

  return (
    // One scroll container: the summary above can grow (log, notices) without hiding the list,
    // and the tabs stay pinned while scrolling.
    <div className="h-full overflow-y-auto overflow-x-hidden scroll-thin">
      <header className="space-y-2 px-4 pb-2 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 text-[11px]">
          <span className="flex min-w-0 items-center gap-1 text-muted">
            {openedFrom ? (
              <>
                <History size={12} className="shrink-0 text-accent" />
                <span className="truncate">
                  Saved search from {new Date(openedFrom.createdAt).toLocaleString([], { dateStyle: "short", timeStyle: "short" })} ·{" "}
                  {openedFrom.label}
                  {openedFrom.models && <span className="text-faint"> · {openedFrom.models}</span>}
                </span>
              </>
            ) : (
              "Latest search"
            )}
          </span>
          {loading ? (
            stopButton
          ) : (
            <span className="flex shrink-0 items-center gap-3">
              <button
                className="flex items-center gap-1 text-faint hover:text-fg"
                title="Clear the results panel (the search stays in history)"
                onClick={() => set({ outcome: null, log: [], error: null, openedFrom: null })}
              >
                <X size={12} /> Clear results
              </button>
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
          <span>
            <b className="text-fg">{outcome.fetched}</b> postings
          </span>
          <span>
            <b className="text-good">{counts.matches}</b> {rep.screened ? "true matches" : "above threshold"}
          </span>
          {rep.screened ? (
            <span className="flex items-center gap-1">
              <BrainCircuit size={13} className="text-accent" /> screened by job_matcher
              {outcome.summary_from_memory != null && (
                <span className="text-faint">
                  · profile summary {outcome.summary_from_memory ? "from memory" : "new"}
                </span>
              )}
            </span>
          ) : (
            <span className="text-faint">keyword pre-filter only</span>
          )}
          {fresh > 0 && (
            <span className="text-accent" title="Jobs appearing in a search for the first time (matches and not selected)">
              {fresh} new
            </span>
          )}
          {counts.applied > 0 && (
            <button className="text-good hover:underline" title={TAB_HINT.applied} onClick={() => setTab("applied")}>
              {counts.applied} already applied
            </button>
          )}
          {borderline > 0 && (
            <span className="text-warn" title="Within 5 points of the threshold, in either tab">
              {borderline} borderline
            </span>
          )}
          <span className="text-faint">{outcome.seconds}s</span>
          {(loading || progress) && (
            <span className="flex items-center gap-1 text-accent">
              <Loader2 size={12} className="animate-spin" /> {progress ?? "Updating…"}
            </span>
          )}
        </div>
        {outcome.cv_titles?.length > 0 && (
          <p className="break-words text-[11px] text-muted">
            <span className="font-medium text-fg">Roles from your CV profile:</span> {outcome.cv_titles.join(" · ")}
            <span className="text-faint"> (job boards searched for these; add titles on the left to override)</span>
          </p>
        )}
        {outcome.families && Object.keys(outcome.families).length > 0 && (
          <p className="break-words text-[11px] text-muted">
            <span className="font-medium text-fg">Role families:</span>{" "}
            {Object.entries(outcome.families)
              .map(([name, tier]) => `${name} (${tier})`)
              .join(" · ")}
            <span className="text-faint"> (see Profiles for their evidence and results)</span>
          </p>
        )}
        {!loading && (outcome.cancelled || outcome.unscreened > 0) && (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-[12px]">
            <AlertTriangle size={13} className="text-warn" />
            <span className="text-muted">
              {outcome.cancelled ? "Stopped. " : "Some screening batches failed. "}
              {outcome.unscreened > 0
                ? `${outcome.unscreened} shortlisted job${outcome.unscreened === 1 ? "" : "s"} not judged yet.`
                : "Everything shortlisted was judged."}
            </span>
            {outcome.unscreened > 0 && outcome.history_id && (
              <button className="btn-primary ml-auto py-1 text-[12px]" onClick={() => void runner.resume(outcome.history_id!)}>
                <Play size={12} /> Continue
              </button>
            )}
          </div>
        )}
        {error && <p className="break-words text-[12px] text-bad">{error}</p>}
        <SourcesUsed sources={outcome.sources ?? []} />
        {log.length > 0 && (
          <details className="text-[11px] text-faint">
            <summary className="cursor-pointer">Process log ({log.length} steps)</summary>
            <div className="mt-1">
              <ProgressLog log={log} running={false} />
            </div>
          </details>
        )}
        {outcome.smart_unavailable && (
          <p className="flex items-center gap-1 text-[12px] text-warn">
            <AlertTriangle size={12} /> {outcome.smart_unavailable}
          </p>
        )}
        {Object.keys(problems).length > 0 && (
          <details className="text-[11px] text-faint">
            <summary className="cursor-pointer">{Object.keys(problems).length} source notice(s)</summary>
            {Object.entries(problems).map(([k, v]) => (
              <p key={k} className="break-words">
                <b>{k}</b>: {v}
              </p>
            ))}
          </details>
        )}
      </header>
      <nav className="sticky top-0 z-10 flex flex-wrap gap-1 border-b border-border bg-bg px-4 py-2">
          {tabs.map((t) => (
            <button
              key={t}
              title={TAB_HINT[t]}
              onClick={() => setTab(t)}
              className={cn(
                "rounded-md px-2.5 py-1 text-[12px]",
                tab === t ? "bg-accent-bg text-fg" : "text-muted hover:text-fg",
              )}
            >
              {TAB_LABEL[t]} <span className="text-faint">{counts[t]}</span>
            </button>
          ))}
      </nav>
      <div className="space-y-2.5 p-4">
        {TAB_HINT[tab] && items.length > 0 && <p className="text-[11px] text-faint">{TAB_HINT[tab]}.</p>}
        {items.length === 0 ? (
          <Empty title={emptyTitle}>
            {tab === "matches" && outcome.fetched > 0 &&
              "Try widening to adjacent roles, a wider radius, a lower salary floor, or more sources."}
          </Empty>
        ) : (
          items.map((r) => (
            <JobCard
              key={r.job.id}
              result={r}
              canTailor={hasCv}
              selected={selected.includes(r.job.id)}
              onSelect={() => toggleSelected(r.job.id)}
              savedAt={savedAt.get(r.job.id)}
            />
          ))
        )}
      </div>
    </div>
  );
}
