import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Loader2, Plus } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { SavedJob } from "../lib/types";
import { useSearch } from "../stores/searchStore";
import { JobCard } from "./JobCard";
import { Empty } from "./ui";

/** Jobs saved for consideration; applications move to the Applied view. */
export function SavedJobs({ hasCv, query }: { hasCv: boolean; query: UseQueryResult<SavedJob[]> }) {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [location, setLocation] = useState("");
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const { selected, toggleSelected, set } = useSearch();
  const add = useMutation({
    meta: { syncLists: true },
    mutationFn: api.importJob,
    onSuccess: () => {
      setTitle(""); setCompany(""); setLocation(""); setUrl(""); setDescription("");
      setAdding(false);
      void qc.invalidateQueries({ queryKey: ["saved"] });
    },
  });
  const remove = useMutation({
    meta: { syncLists: true },
    mutationFn: (jobId: string) => api.removeSaved([jobId]),
    onSuccess: (_, jobId) => {
      set({ selected: selected.filter((id) => id !== jobId) });
      void qc.invalidateQueries({ queryKey: ["saved"] });
    },
  });
  return (
    <div className="h-full overflow-y-auto overflow-x-hidden scroll-thin">
      <div className="border-b border-border p-4">
        <button className="btn-ghost text-[12px]" onClick={() => setAdding(!adding)}><Plus size={14} /> Add a job you found</button>
        {adding && <form className="mt-3 space-y-2" onSubmit={(event) => {
          event.preventDefault();
          add.mutate({ title: title.trim(), company: company.trim(), location: location.trim(), url: url.trim(), description: description.trim() });
        }}>
          <p className="text-[12px] text-muted">Paste the full posting so tailoring and the letter can use its requirements.</p>
          <div className="grid grid-cols-2 gap-2">
            <input className="input" aria-label="Job title" placeholder="Job title *" value={title} onChange={(event) => setTitle(event.target.value)} required />
            <input className="input" aria-label="Company" placeholder="Company *" value={company} onChange={(event) => setCompany(event.target.value)} required />
          </div>
          <input className="input w-full" aria-label="Location" placeholder="Location (optional)" value={location} onChange={(event) => setLocation(event.target.value)} />
          <input className="input w-full" aria-label="Job link" type="url" placeholder="Job link (optional)" value={url} onChange={(event) => setUrl(event.target.value)} />
          <textarea className="input min-h-32 w-full" aria-label="Job description" placeholder="Full job description *" value={description} onChange={(event) => setDescription(event.target.value)} required />
          {add.error && <p className="text-[12px] text-bad">{(add.error as Error).message}</p>}
          <button className="btn-primary text-[12px]" disabled={add.isPending}>{add.isPending ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Save job</button>
        </form>}
      </div>
      {query.isPending ? <p className="flex items-center gap-2 p-4 text-[12px] text-muted"><Loader2 size={14} className="animate-spin" /> Loading saved jobs…</p> :
        query.isError ? <p className="p-4 text-[12px] text-bad">Could not load saved jobs: {(query.error as Error).message}</p> :
        !query.data?.length ? <Empty title="No saved jobs">Save jobs from search results or add one above.</Empty> : <>
      {remove.error && <p className="p-4 text-[12px] text-bad">{(remove.error as Error).message}</p>}
      <div className="space-y-2.5 p-4">
        {query.data.map((s) => (
            <JobCard
              key={s.result.job.id}
              result={s.result}
              canTailor={hasCv}
              selected={selected.includes(s.result.job.id)}
              onSelect={() => toggleSelected(s.result.job.id)}
              savedAt={s.saved_at}
              onRemove={() => remove.mutate(s.result.job.id)}
              removeLabel="Delete"
              removeTitle="Delete from saved jobs (its status, note and any application record are kept)"
              removeAtTop
              removing={remove.isPending && remove.variables === s.result.job.id}
              labelable={false}
              refetchSaved
            />
          ))}
      </div>
      </>}
    </div>
  );
}
