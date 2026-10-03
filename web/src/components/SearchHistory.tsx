import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, Loader2, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { HistoryItem } from "../lib/types";
import { cn } from "../lib/utils";
import { EMPTY_QUERY, profileSearchQuery, useSearch } from "../stores/searchStore";

const when = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

/** The last 10 searches: open one to see its results again, delete one, or clear them all. */
export function SearchHistory() {
  const qc = useQueryClient();
  const s = useSearch();
  const [confirmClear, setConfirmClear] = useState(false);
  const history = useQuery({ queryKey: ["history"], queryFn: api.history });
  const refresh = () => qc.invalidateQueries({ queryKey: ["history"] });

  const open = useMutation({
    mutationFn: (item: HistoryItem) => api.openHistory(item.id),
    onSuccess: ({ request, outcome }, item) =>
      s.set({
        query: profileSearchQuery({ ...EMPTY_QUERY, ...request.query }),
        useCv: request.use_cv,
        smart: request.smart,
        threshold: request.threshold,
        profileKey: request.profile_key ?? null,
        outcome,
        log: [],
        error: null,
        openedFrom: { id: item.id, label: item.label, createdAt: item.created_at, models: item.models },
      }),
    onError: () => void refresh(),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteHistory(id),
    onSuccess: (_, id) => {
      if (s.openedFrom?.id === id) s.set({ openedFrom: null });
      void refresh();
    },
  });
  const clear = useMutation({
    mutationFn: api.clearHistory,
    onSuccess: () => {
      setConfirmClear(false);
      s.set({ openedFrom: null });
      void refresh();
    },
  });
  const items = history.data ?? [];
  const error = (open.error ?? remove.error ?? clear.error) as Error | null;

  return (
    <section className="card space-y-2 p-3">
      <div className="flex items-center justify-between">
        <span className="label flex items-center gap-1"><History size={12} /> Recent searches</span>
        {items.length > 0 && (
          confirmClear ? (
            <span className="flex gap-2 text-[11px]">
              <button className="text-bad hover:underline" disabled={clear.isPending} onClick={() => clear.mutate()}>Clear all</button>
              <button className="text-faint hover:text-fg" onClick={() => setConfirmClear(false)}>Cancel</button>
            </span>
          ) : (
            <button className="text-[11px] text-faint hover:text-fg" onClick={() => setConfirmClear(true)}>Clear history</button>
          )
        )}
      </div>
      {history.isPending ? (
        <p className="flex items-center gap-1 text-[11px] text-faint"><Loader2 size={12} className="animate-spin" /> Loading history…</p>
      ) : history.isError ? (
        <p className="text-[11px] text-bad">Could not load history: {(history.error as Error).message}</p>
      ) : items.length === 0 ? (
        <p className="text-[11px] text-faint">Your last 10 searches appear here, with their results.</p>
      ) : (
        <ul className="space-y-1">
          {items.map((item) => (
            <li key={item.id} className="flex items-start gap-1">
              <button
                type="button"
                title="Show this search's results again"
                disabled={open.isPending}
                onClick={() => open.mutate(item)}
                className={cn(
                  "min-w-0 flex-1 rounded-md border px-2 py-1 text-left text-[11px]",
                  s.openedFrom?.id === item.id ? "border-accent bg-accent-bg" : "border-border hover:border-accent",
                )}
              >
                <span className="block truncate font-medium text-fg">{item.label}</span>
                <span className="text-faint">
                  {when(item.created_at)} · {item.fetched} postings ·{" "}
                  <span className="text-good">{item.matches} {item.screened ? "matches" : "above threshold"}</span>
                </span>
                {item.models && <span className="block truncate text-faint" title={item.models}>{item.models}</span>}
              </button>
              <button
                type="button"
                aria-label={`Delete search ${item.label}`}
                title="Delete from history"
                className="mt-1 text-faint hover:text-bad"
                disabled={remove.isPending}
                onClick={() => remove.mutate(item.id)}
              >
                <Trash2 size={12} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="text-[11px] text-bad">{error.message}</p>}
    </section>
  );
}
