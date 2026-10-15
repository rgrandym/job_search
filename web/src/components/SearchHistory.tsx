import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Loader2, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { HistoryItem } from "../lib/types";
import { cn } from "../lib/utils";
import { Resizable } from "./ui";
import { openedSearch, useSearch } from "../stores/searchStore";

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
    onSuccess: ({ outcome }, item) => s.set(openedSearch(outcome, item)),
    onError: () => void refresh(),
  });
  const remove = useMutation({
    mutationFn: ({ id }: { id: string; index: number }) => api.deleteHistory(id),
    onSuccess: async (_, { id, index }) => {
      const now = useSearch.getState();
      const shown = !now.loading && (now.openedFrom?.id === id || now.outcome?.history_id === id);
      const left = await qc.fetchQuery({ queryKey: ["history"], queryFn: api.history, staleTime: 0 });
      if (!shown) {
        if (now.lastSearchId === id) s.set({ lastSearchId: null });
        return;
      }
      // The list on screen was deleted: show the one that took its place (or the one before),
      // or nothing when no saved lists are left.
      const next = left[Math.min(index, left.length - 1)];
      if (next) open.mutate(next);
      else s.set({ outcome: null, openedFrom: null, lastSearchId: null, log: [], error: null, selected: [] });
    },
  });
  const clear = useMutation({
    mutationFn: api.clearHistory,
    onSuccess: () => {
      setConfirmClear(false);
      // No saved lists are left: the results panel empties too (a running search keeps its own).
      if (useSearch.getState().loading) s.set({ openedFrom: null, lastSearchId: null });
      else s.set({ outcome: null, openedFrom: null, lastSearchId: null, log: [], error: null, selected: [] });
      void refresh();
    },
  });
  const items = history.data ?? [];
  const error = (open.error ?? remove.error ?? clear.error) as Error | null;

  return (
    <details className="card group p-3" open>
      <summary className="label flex cursor-pointer list-none items-center gap-1 [&::-webkit-details-marker]:hidden"><ChevronRight size={13} className="group-open:rotate-90" /> Recent searches</summary>
      <Resizable id="history">
      <div className="mt-2 space-y-2">
      <div className="flex items-center justify-end">
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
          {items.map((item, index) => (
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
                onClick={() => remove.mutate({ id: item.id, index })}
              >
                <Trash2 size={12} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="text-[11px] text-bad">{error.message}</p>}
      </div>
      </Resizable>
    </details>
  );
}
