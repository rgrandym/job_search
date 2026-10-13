import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Loader2, Search } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { JobPosting, OneJobQuery, OneJobResults } from "../lib/types";
import { TaskError, TaskProgress, useTaskId } from "./TaskProgress";

const EMPTY: OneJobQuery = { title: "", company: "", job_id: "", keywords: "", description: "", posted_at: null, url: "" };

/** Find a named posting without running the profile search or matcher. */
export function FindOneJob({ onSaved }: { onSaved: () => void }) {
  const qc = useQueryClient();
  const [query, setQuery] = useState<OneJobQuery>(EMPTY);
  const [result, setResult] = useState<OneJobResults | null>(null);
  const task = useTaskId();
  const [saving, setSaving] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const search = useMutation({
    mutationFn: (progressId: string) => api.findOneJob(query, progressId),
    onSuccess: setResult,
    onMutate: () => { setResult(null); setSaveError(null); },
  });
  const set = (key: keyof OneJobQuery, value: string | null) => setQuery((current) => ({ ...current, [key]: value }));
  const save = async (job: JobPosting) => {
    setSaving(job.id);
    setSaveError(null);
    try {
      await api.saveFoundJob(job);
      await qc.invalidateQueries({ queryKey: ["saved"] });
      onSaved();
    } catch (error) {
      setSaveError((error as Error).message);
    } finally {
      setSaving(null);
    }
  };
  return (
    <div className="h-full overflow-y-auto scroll-thin p-4">
      <div className="mx-auto max-w-2xl space-y-4">
        <div>
          <h2 className="text-sm font-semibold text-fg">Find one job</h2>
          <p className="mt-1 text-[12px] text-muted">Look online for a posting you found yourself: its link is read directly; otherwise a web search finds its pages (the job ID makes this exact), with the employer's own job board and the job boards as a fallback. Save it to tailor a CV and write a letter. No CV matching runs. Jobs already in the app are found with <b>Find in lists</b> above the results.</p>
        </div>
        <form className="card space-y-3 p-4" onSubmit={(event) => { event.preventDefault(); search.mutate(task.next()); }}>
          <div>
            <input className="input w-full" aria-label="Posting link" type="url" placeholder="Posting link (fastest: read directly, no search)" value={query.url} onChange={(event) => set("url", event.target.value)} />
            <p className="mt-1 text-[11px] text-faint">Have the link? Paste it and search: the posting is read straight from it. Otherwise fill in what you know below.</p>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <input className="input" aria-label="Job title" placeholder="Job title" value={query.title} onChange={(event) => set("title", event.target.value)} />
            <input className="input" aria-label="Company" placeholder="Company" value={query.company} onChange={(event) => set("company", event.target.value)} />
            <input className="input" aria-label="Job ID" placeholder="Job ID (e.g. reed:12345)" value={query.job_id} onChange={(event) => set("job_id", event.target.value)} />
            <input className="input" aria-label="Posting date" type="date" value={query.posted_at ?? ""} onChange={(event) => set("posted_at", event.target.value || null)} />
          </div>
          <input className="input w-full" aria-label="Keywords" placeholder="Keywords, separated by commas" value={query.keywords} onChange={(event) => set("keywords", event.target.value)} />
          <textarea className="input min-h-24 w-full" aria-label="Job description clue" placeholder="Words or a passage from the job description" value={query.description} onChange={(event) => set("description", event.target.value)} />
          <p className="text-[11px] text-faint">Title and company together work best. Keywords and description words rank the postings that mention them. A date (within a few days) narrows another clue; it cannot identify a posting alone. The job ID (e.g. R79349) is the most exact clue after the link.</p>
          <button className="btn-primary" type="submit" disabled={search.isPending}>
            {search.isPending ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />} Search for this job
          </button>
          {search.isPending && <TaskProgress id={task.id} />}
          <TaskError error={search.error} className="text-[12px]" />
        </form>
        {result && <div className="space-y-2">
          <p className="text-[12px] text-muted">{result.jobs.length ? `${result.jobs.length} possible posting${result.jobs.length === 1 ? "" : "s"}` : "No matching posting found."}</p>
          {Object.entries(result.errors).length > 0 && <p className="text-[11px] text-faint">Sources unavailable: {Object.entries(result.errors).map(([name, reason]) => `${name}: ${reason}`).join(" · ")}</p>}
          {saveError && <p className="text-[12px] text-bad">{saveError}</p>}
          {result.jobs.map((job) => <article className="card space-y-2 p-3" key={job.id}>
            <div className="flex items-start justify-between gap-2">
              <div><h3 className="text-[13px] font-semibold text-fg">{job.title}</h3><p className="text-[12px] text-muted">{job.company}{job.location ? ` · ${job.location}` : ""}{job.posted_at ? ` · ${job.posted_at}` : ""}</p></div>
              {job.url && <a className="text-accent" href={job.url} target="_blank" rel="noreferrer" title="Open original posting"><ExternalLink size={14} /></a>}
            </div>
            <p className="line-clamp-4 whitespace-pre-line text-[11px] text-muted">{job.description || "No readable description found."}</p>
            <button className="btn-primary text-[12px]" disabled={!job.description || !!saving} onClick={() => void save(job)}>
              {saving === job.id ? <Loader2 size={12} className="animate-spin" /> : null} Save for CV and letter
            </button>
            {!job.description && <p className="text-[11px] text-faint">Paste the full description in Saved → Add a job you found.</p>}
          </article>)}
          {!result.jobs.length && <p className="text-[12px] text-muted">If you have the posting text, use Saved → Add a job you found so you can still tailor a CV and write a letter.</p>}
        </div>}
      </div>
    </div>
  );
}
