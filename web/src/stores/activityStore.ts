import { create } from "zustand";

/** Work the activity panel follows besides the search and the chat: document tasks started
 *  anywhere in the app (`TaskProgress` registers them), and a run waiting for a plan's reset. */
export interface Waiting {
  /** When the run starts (ms). */
  at: number;
  /** What will run, e.g. "Continue the screening on claude-opus-5". */
  label: string;
}

interface ActivityState {
  /** Running document tasks: progress id -> when it was first seen (ms). */
  tasks: Record<string, number>;
  waiting: Waiting | null;
  addTask: (id: string) => void;
  removeTask: (id: string) => void;
  /** Run `run` at `at` (ms), replacing any earlier wait. */
  wait: (at: number, label: string, run: () => void) => void;
  cancelWait: () => void;
}

let timer: number | undefined;

export const useActivity = create<ActivityState>()((set) => ({
  tasks: {},
  waiting: null,
  addTask: (id) => set((st) => (st.tasks[id] ? st : { tasks: { ...st.tasks, [id]: Date.now() } })),
  removeTask: (id) =>
    set((st) => {
      if (!st.tasks[id]) return st;
      const rest = { ...st.tasks };
      delete rest[id];
      return { tasks: rest };
    }),
  wait: (at, label, run) => {
    window.clearTimeout(timer);
    // Timers are capped near 24.8 days; a plan window resets well within that.
    timer = window.setTimeout(() => {
      set({ waiting: null });
      run();
    }, Math.max(0, at - Date.now()));
    set({ waiting: { at, label } });
  },
  cancelWait: () => {
    window.clearTimeout(timer);
    set({ waiting: null });
  },
}));
