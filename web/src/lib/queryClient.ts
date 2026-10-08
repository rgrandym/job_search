import { MutationCache, QueryClient } from "@tanstack/react-query";
import { useSearch } from "../stores/searchStore";
import { api } from "./api";

/** Lists that show the same jobs: saved jobs, the applications register, the saved searches,
 *  and the documents shown under them. */
const JOB_LISTS = ["saved", "tracker", "history", "tailored-cvs", "cover-letters"] as const;

// Other tabs of the app hear about every change at once (same browser, no server round trip).
const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("jobsearch.lists");
let running: Promise<void> | null = null;
let again = false;

async function refreshHere(): Promise<void> {
  for (const key of JOB_LISTS) void queryClient.invalidateQueries({ queryKey: [key] });
  const id = useSearch.getState().outcome?.history_id;
  if (!id || useSearch.getState().loading) return;
  try {
    const { outcome } = await api.openHistory(id); // re-reads the tracker's statuses
    const now = useSearch.getState();
    if (!now.loading && now.outcome?.history_id === id) now.set({ outcome });
  } catch {
    // The search left the history: its results stay as they are on screen.
  }
}

/** Refresh this tab's lists and results, once at a time (a burst of changes runs one more pass). */
function refresh(): Promise<void> {
  if (running) {
    again = true;
    return running;
  }
  running = refreshHere().finally(() => {
    running = null;
    if (again) {
      again = false;
      void refresh();
    }
  });
  return running;
}

/** After a job is applied to, saved, set aside or deleted anywhere (a card, the register, the
 *  assistant), refresh every list showing jobs and the results on screen with their statuses,
 *  in this tab and in every other open tab, so no list shows a stale job. Applications stay
 *  in the register: only lists are tidied. */
export function syncLists(): Promise<void> {
  channel?.postMessage("changed");
  return refresh();
}

channel?.addEventListener("message", () => void refresh());
// A tab coming back into view catches up too (a message it missed, a change made elsewhere).
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void refresh();
});

export const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
  // A mutation that changes which list a job belongs in says so with `meta: { syncLists: true }`.
  mutationCache: new MutationCache({
    onSuccess: (_data, _variables, _context, mutation) => {
      if (mutation.meta?.syncLists) void syncLists();
    },
  }),
});
