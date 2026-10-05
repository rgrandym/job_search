import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { JobTracking, MatchReport, ProfileSummary, ProgressEvent, SearchOutcome, SearchQuery } from "../lib/types";

export const EMPTY_QUERY: SearchQuery = {
  titles: [],
  keywords: [],
  locations: [],
  country: null,
  distance_miles: 25,
  salary_min: null,
  salary_max: null,
  seniority_min: null,
  seniority_max: null,
  work_arrangements: [],
  posted_within_days: null,
  sources: [],
  exclude_boards: [],
};

/** Hidden legacy filters must never constrain a new profile-led search. */
export const profileSearchQuery = (query: SearchQuery): SearchQuery => ({
  ...query,
  titles: [],
  keywords: [],
  salary_min: null,
  salary_max: null,
  work_arrangements: [],
});

interface SearchState {
  query: SearchQuery;
  useCv: boolean;
  smart: boolean;
  /** Also search the adjacent role families the profile proposes (tough market). */
  widen: boolean;
  threshold: number;
  outcome: SearchOutcome | null;
  summary: { summary: ProfileSummary; fromMemory: boolean } | null;
  loading: boolean;
  progress: string | null;
  log: ProgressEvent[];
  profileKey: string | null;
  /** Sources switched off in a category's settings: switching the category back on skips them. */
  disabledSources: string[];
  /** Set while showing a search reopened from the history. */
  openedFrom: { id: string; label: string; createdAt: string; models: string | null } | null;
  /** Id of the running search (or continuation), so it can be stopped. */
  runId: string | null;
  error: string | null;
  /** Job ids ticked in the results or saved jobs (not persisted). */
  selected: string[];
  toggleSelected: (jobId: string) => void;
  setQuery: (patch: Partial<SearchQuery>) => void;
  /** Show a job's new status/note wherever it appears in the current results. */
  setTracking: (jobId: string, tracking: JobTracking) => void;
  set: (patch: Partial<Omit<SearchState, "set" | "setQuery" | "setTracking" | "toggleSelected">>) => void;
}

const BUCKETS = ["matches", "below_threshold", "excluded", "applied", "dismissed"] as const;

function withTracking(report: MatchReport, jobId: string, tracking: JobTracking): MatchReport {
  const next = { ...report };
  for (const bucket of BUCKETS) {
    const items = report[bucket];
    if (items) next[bucket] = items.map((r) => (r.job.id === jobId ? { ...r, tracking } : r));
  }
  return next;
}

export const useSearch = create<SearchState>()(
  persist(
    (set) => ({
      query: EMPTY_QUERY,
      useCv: true,
      smart: true,
      widen: false,
      threshold: 60,
      outcome: null,
      summary: null,
      loading: false,
      progress: null,
      log: [],
      profileKey: null,
      disabledSources: ["demo"],
      openedFrom: null,
      runId: null,
      error: null,
      selected: [],
      toggleSelected: (jobId) =>
        set((s) => ({
          selected: s.selected.includes(jobId) ? s.selected.filter((id) => id !== jobId) : [...s.selected, jobId],
        })),
      setQuery: (patch) => set((s) => ({ query: { ...s.query, ...patch } })),
      setTracking: (jobId, tracking) =>
        set((s) =>
          s.outcome ? { outcome: { ...s.outcome, report: withTracking(s.outcome.report, jobId, tracking) } } : {},
        ),
      set: (patch) => set(patch),
    }),
    {
      name: "jobsearch.filters",
      // v1: the match floor moved from 70 to 60 (a "reasonable stretch" is listed)
      // v2: sources grouped by category; demo jobs start switched off
      // v3: title, keyword, salary and arrangement controls were removed
      version: 3,
      migrate: (persisted, version) => {
        const state = persisted as Pick<
          SearchState,
          "query" | "useCv" | "smart" | "threshold" | "profileKey" | "disabledSources"
        >;
        const moved = version < 1 && state.threshold === 70 ? { ...state, threshold: 60 } : state;
        const withSources = version < 2 ? { ...moved, disabledSources: ["demo"] } : moved;
        return version < 3 && withSources.query
          ? { ...withSources, query: profileSearchQuery({ ...EMPTY_QUERY, ...withSources.query }) }
          : withSources;
      },
      partialize: (s) => ({
        query: s.query,
        useCv: s.useCv,
        smart: s.smart,
        widen: s.widen,
        threshold: s.threshold,
        profileKey: s.profileKey,
        disabledSources: s.disabledSources,
      }),
    },
  ),
);
