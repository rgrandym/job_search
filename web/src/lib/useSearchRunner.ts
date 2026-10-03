import { useQueryClient } from "@tanstack/react-query";
import { useChat } from "../stores/chatStore";
import { profileSearchQuery, useSearch } from "../stores/searchStore";
import { api } from "./api";
import type { SearchOutcome, SearchStreamEvent } from "./types";

/** Start, stop, continue and repeat searches; shared by the sidebar and the results panel. */
export function useSearchRunner() {
  const qc = useQueryClient();

  const onEvent = (event: SearchStreamEvent) => {
    if (event.type === "search_progress") {
      const step = { stage: event.stage, message: event.message, source: event.source, status: event.status, done: event.done, total: event.total, at: Date.now() };
      useSearch.setState((st) => ({ progress: step.message, log: [...st.log, step] }));
    } else if (event.type === "model_usage") {
      useChat.getState().recordUsage(event);
    }
  };

  /** Run one streamed job; `keep` leaves the current results on screen while it runs. */
  const start = async (label: string, call: (runId: string) => Promise<SearchOutcome>, keep = false) => {
    const runId = crypto.randomUUID();
    useSearch.setState({
      loading: true, error: null, progress: label, log: [], runId,
      ...(keep ? {} : { outcome: null, openedFrom: null }),
    });
    try {
      const outcome = await call(runId);
      useSearch.setState({ outcome });
      if (outcome.report.summary)
        useSearch.setState({ summary: { summary: outcome.report.summary, fromMemory: !!outcome.summary_from_memory } });
      void qc.invalidateQueries({ queryKey: ["profiles"] }); // a search may store a new profile
      void qc.invalidateQueries({ queryKey: ["history"] });
      void qc.invalidateQueries({ queryKey: ["yield"] });
      void qc.invalidateQueries({ queryKey: ["claude-code-usage"] });
    } catch (e) {
      useSearch.setState({ error: (e as Error).message });
    } finally {
      useSearch.setState({ loading: false, progress: null, runId: null });
    }
  };

  /** A new search with the sidebar's filters (`alertOnly`: only new Gmail alerts). */
  const run = (alertOnly = false) => {
    const s = useSearch.getState();
    const label = alertOnly ? "Reading new job alerts…" : s.smart ? "Searching and screening…" : "Searching…";
    return start(label, (runId) =>
      api.searchStream(
        {
          query: alertOnly
            ? { ...profileSearchQuery(s.query), sources: ["gmail_alerts"] }
            : profileSearchQuery(s.query),
          use_cv: alertOnly ? true : s.useCv,
          smart: alertOnly ? true : s.smart,
          threshold: s.threshold,
          profile_key: s.useCv || alertOnly ? s.profileKey : null,
          widen: s.widen && s.useCv && !alertOnly,
          run_id: runId,
        },
        onEvent,
      ),
    );
  };

  /** Judge the shortlisted jobs a stopped or partly failed search left unjudged. */
  const resume = (historyId: string) =>
    start("Continuing the screening…", (runId) => api.continueStream({ history_id: historyId, run_id: runId }, onEvent), true);

  /** Stop the running search; it ends with what it has done so far. */
  const stop = () => {
    const runId = useSearch.getState().runId;
    if (!runId) return;
    useSearch.setState({ progress: "Stopping…" });
    api.stopSearch(runId).catch((e: Error) => useSearch.setState({ error: e.message }));
  };

  return { run, resume, stop };
}
