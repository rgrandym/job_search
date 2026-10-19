import { create } from "zustand";

/** The size of the whole app (header control, and the Dock app's View menu).
 *
 *  In the Dock app the native page zoom does it (`main.swift`, "jobSearchZoom"): it reflows the
 *  layout like a browser's own zoom. In a browser tab CSS `zoom` does it, with the page sized to
 *  the window divided by `--zoom`, and window-sized parts written as `calc(…vh / var(--zoom))`, so
 *  nothing grows past the window. The choice is remembered in this browser. */

const KEY = "jobsearch.zoom";
export const ZOOM_STEPS = [0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6];
export const DEFAULT_ZOOM = 1.25;

type Bridge = { postMessage: (zoom: number) => void };
type ZoomWindow = Window & {
  webkit?: { messageHandlers?: { jobSearchZoom?: Bridge } };
  jobSearchZoomStep?: (by: number) => void;
};

function saved(): number {
  try {
    const value = Number(localStorage.getItem(KEY));
    return ZOOM_STEPS.includes(value) ? value : DEFAULT_ZOOM;
  } catch {
    return DEFAULT_ZOOM;
  }
}

function apply(zoom: number) {
  const root = document.documentElement;
  const native = (window as ZoomWindow).webkit?.messageHandlers?.jobSearchZoom;
  if (native) native.postMessage(zoom);
  const css = native ? 1 : zoom;
  root.style.zoom = css === 1 ? "" : String(css);
  root.style.setProperty("--zoom", String(css));
  try {
    localStorage.setItem(KEY, String(zoom));
  } catch {
    // private window or blocked storage: the size still applies for this visit
  }
}

interface ZoomState {
  zoom: number;
  set: (zoom: number) => void;
  /** One step larger (1), smaller (-1), or back to the default (0). */
  step: (by: number) => void;
}

export const useZoom = create<ZoomState>((setState, get) => ({
  zoom: saved(),
  set: (zoom) => {
    apply(zoom);
    setState({ zoom });
  },
  step: (by) => {
    if (by === 0) return get().set(DEFAULT_ZOOM);
    const i = ZOOM_STEPS.indexOf(get().zoom) + by;
    if (i >= 0 && i < ZOOM_STEPS.length) get().set(ZOOM_STEPS[i]);
  },
}));

/** Apply the remembered size before the first render, and let the Dock app's menu step it. */
export function initZoom() {
  apply(useZoom.getState().zoom);
  (window as ZoomWindow).jobSearchZoomStep = (by) => useZoom.getState().step(by);
}
