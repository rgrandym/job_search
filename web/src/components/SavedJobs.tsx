import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { api } from "../lib/api";
import type { SavedJob } from "../lib/types";
import { useSearch } from "../stores/searchStore";
import { JobCard } from "./JobCard";
import { Empty } from "./ui";

/** Jobs saved for consideration; applications move to the Applied view. */
export function SavedJobs({ hasCv, query }: { hasCv: boolean; query: UseQueryResult<SavedJob[]> }) {
  const qc = useQueryClient();
  const { selected, toggleSelected, set } = useSearch();
  const remove = useMutation({
    mutationFn: (jobId: string) => api.removeSaved([jobId]),
    onSuccess: (_, jobId) => {
      set({ selected: selected.filter((id) => id !== jobId) });
      void qc.invalidateQueries({ queryKey: ["saved"] });
    },
  });
  if (query.isPending)
    return (
      <p className="flex items-center gap-2 p-4 text-[12px] text-muted">
        <Loader2 size={14} className="animate-spin" /> Loading saved jobs…
      </p>
    );
  if (query.isError) return <p className="p-4 text-[12px] text-bad">Could not load saved jobs: {(query.error as Error).message}</p>;
  if (!query.data.length)
    return (
      <Empty title="No saved jobs">
        Tick jobs in the search results and click <b>Save</b>. They stay here across searches until you remove them, with
        until you apply or remove them.
      </Empty>
    );

  return (
    <div className="h-full overflow-y-auto overflow-x-hidden scroll-thin">
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
              labelable={false}
            />
          ))}
      </div>
    </div>
  );
}
