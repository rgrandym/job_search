import { create } from "zustand";
import type { OneJobQuery } from "../lib/types";

/** The "Find one job" window: opened from the sidebar, or from a job card whose posting
 *  could not be read, with that job's title, employer, link and ID filled in. */
interface FindJobState {
  open: boolean;
  /** Clues to start from (null: an empty form). */
  prefill: OneJobQuery | null;
  /** A saved job the found posting fills in, instead of being saved as another job. */
  savedJobId: string | null;
  /** Increases on every opening, so the form starts again from the new clues. */
  opened: number;
  show: (prefill?: OneJobQuery | null, savedJobId?: string | null) => void;
  hide: () => void;
}

export const useFindJob = create<FindJobState>()((set) => ({
  open: false,
  prefill: null,
  savedJobId: null,
  opened: 0,
  show: (prefill = null, savedJobId = null) =>
    set((st) => ({ open: true, prefill, savedJobId, opened: st.opened + 1 })),
  hide: () => set({ open: false }),
}));
