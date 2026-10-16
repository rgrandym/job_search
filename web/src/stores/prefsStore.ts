import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { ResultTab } from "../lib/resultGroups";

export type ResultsView = "results" | "saved" | "applied" | "documents" | "cover_letters";

/** The CV options a job card tailors with (shared by every card). */
export interface TailorPrefs {
  template: string;
  length: "auto" | "full" | "junior";
  emphasis: "auto" | "leadership" | "hands_on";
  level: "auto" | "senior" | "junior";
  audience: "auto" | "industry" | "academic";
  cvMode: "standard" | "optimised";
}

/** Choices the user makes in the panels, kept across reloads and new tabs. */
interface PrefsState {
  tailor: TailorPrefs;
  /** CV design used when preparing CVs for the ticked jobs. */
  batchTemplate: string;
  resultsView: ResultsView;
  resultTab: ResultTab;
  /** The assistant's model ("quality", "screening", "model:<id>" or "claude-code:<id>"). */
  chatChoice: string;
  setTailor: (patch: Partial<TailorPrefs>) => void;
  set: (patch: Partial<Omit<PrefsState, "set" | "setTailor">>) => void;
}

export const DEFAULT_TAILOR: TailorPrefs = {
  template: "original",
  length: "auto",
  emphasis: "auto",
  level: "auto",
  audience: "auto",
  cvMode: "standard",
};

export const usePrefs = create<PrefsState>()(
  persist(
    (set) => ({
      tailor: DEFAULT_TAILOR,
      batchTemplate: "original",
      resultsView: "results",
      resultTab: "matches",
      chatChoice: "quality",
      setTailor: (patch) => set((s) => ({ tailor: { ...s.tailor, ...patch } })),
      set: (patch) => set(patch),
    }),
    {
      name: "jobsearch.prefs",
      merge: (persisted, current) => {
        const saved = (persisted ?? {}) as Partial<PrefsState>;
        return { ...current, ...saved, tailor: { ...DEFAULT_TAILOR, ...saved.tailor } };
      },
      partialize: (s) => ({
        tailor: s.tailor,
        batchTemplate: s.batchTemplate,
        resultsView: s.resultsView,
        resultTab: s.resultTab,
        chatChoice: s.chatChoice,
      }),
    },
  ),
);
