import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { SavedJob } from "../lib/types";
import { cn } from "../lib/utils";
import { useSearch } from "../stores/searchStore";
import { JobCard } from "./JobCard";
import { Empty } from "./ui";

type Filter = "all" | "to_apply" | "applied";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "to_apply", label: "Not applied yet" },
  { value: "applied", label: "Applied" },
];

/** Jobs the user saved, across searches; status (applied, outcome) comes live from the tracker. */
export function SavedJobs({ hasCv, query }: { hasCv: boolean; query: UseQueryResult<SavedJob[]> }) {
  const qc = useQueryClient();
  const { selected, toggleSelected, set } = useSearch();
  const [filter, setFilter] = useState<Filter>("all");
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
        their applied status and outcome.
      </Empty>
    );

  const applied = (s: SavedJob) => s.result.tracking?.status === "applied";
  const items = query.data.filter((s) => filter === "all" || (filter === "applied" ? applied(s) : !applied(s)));
  return (
    <div className="h-full overflow-y-auto overflow-x-hidden scroll-thin">
      <div className="sticky top-0 z-10 flex flex-wrap items-center gap-1 border-b border-border bg-bg px-4 py-2">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setFilter(f.value)}
            className={cn("rounded-md px-2.5 py-1 text-[12px]", filter === f.value ? "bg-accent-bg text-fg" : "text-muted hover:text-fg")}
          >
            {f.label}{" "}
            <span className="text-faint">
              {f.value === "all" ? query.data.length : query.data.filter((s) => (f.value === "applied" ? applied(s) : !applied(s))).length}
            </span>
          </button>
        ))}
        {remove.error && <span className="text-[12px] text-bad">{(remove.error as Error).message}</span>}
      </div>
      <div className="space-y-2.5 p-4">
        {items.length === 0 ? (
          <Empty title={filter === "applied" ? "No applications among your saved jobs" : "Nothing left to apply to"} />
        ) : (
          items.map((s) => (
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
          ))
        )}
      </div>
    </div>
  );
}
