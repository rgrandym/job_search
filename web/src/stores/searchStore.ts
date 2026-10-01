import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { ProfileSummary, SearchOutcome, SearchQuery } from "../lib/types";

export const EMPTY_QUERY: SearchQuery = {
  titles: [],
  keywords: [],
  locations: [],
  distance_miles: 25,
  salary_min: null,
  salary_max: null,
  work_arrangements: [],
  sources: [],
};

interface SearchState {
  query: SearchQuery;
  useCv: boolean;
  smart: boolean;
  threshold: number;
  outcome: SearchOutcome | null;
  summary: { summary: ProfileSummary; fromMemory: boolean } | null;
  loading: boolean;
  progress: string | null;
  error: string | null;
  setQuery: (patch: Partial<SearchQuery>) => void;
  set: (patch: Partial<Omit<SearchState, "set" | "setQuery">>) => void;
}

export const useSearch = create<SearchState>()(
  persist(
    (set) => ({
      query: EMPTY_QUERY,
      useCv: true,
      smart: true,
      threshold: 70,
      outcome: null,
      summary: null,
      loading: false,
      progress: null,
      error: null,
      setQuery: (patch) => set((s) => ({ query: { ...s.query, ...patch } })),
      set: (patch) => set(patch),
    }),
    {
      name: "jobsearch.filters",
      partialize: (s) => ({ query: s.query, useCv: s.useCv, smart: s.smart, threshold: s.threshold }),
    },
  ),
);
