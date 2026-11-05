/** The UI's remembered settings, also kept on disk by the server (`src/services/ui_state.py`).
 *
 *  Local storage belongs to one address (host and port), so a port change or cleared browser
 *  data would lose the filters, preferences, theme, layout, zoom and window sizes. Before the
 *  first render, whatever local storage lacks is restored from the server's copy; afterwards
 *  the page's own keys are saved back whenever they change and when the page is hidden. */

const PREFIXES = ["jobsearch.", "react-resizable-panels:jobsearch"];
const URL = "/api/ui-state";
const EVERY_MS = 3000;

function snapshot(): Record<string, string> {
  const items: Record<string, string> = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !PREFIXES.some((p) => key.startsWith(p))) continue;
      const value = localStorage.getItem(key);
      if (value !== null) items[key] = value;
    }
  } catch {
    // blocked storage: nothing to save
  }
  return items;
}

/** Fill in what local storage lacks; true when the server's copy was read. */
export async function restoreUiState(): Promise<boolean> {
  try {
    const response = await fetch(URL, { signal: AbortSignal.timeout(2000) });
    if (!response.ok) return false;
    const saved = (await response.json()) as Record<string, string>;
    for (const [key, value] of Object.entries(saved)) {
      if (localStorage.getItem(key) === null) localStorage.setItem(key, value);
    }
    return true;
  } catch {
    return false; // the page still opens, with what this browser remembers
  }
}

/** Save the page's settings to the server whenever they change. Started only after a
 *  successful restore, so a page that could not read the copy never overwrites it. */
export function keepUiStateSaved(): void {
  let sent = ""; // the first save always goes out, so a launcher without a copy gets one
  const save = () => {
    const now = JSON.stringify(snapshot());
    if (now === sent) return;
    sent = now;
    void fetch(URL, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: now,
      keepalive: true,
    }).catch(() => {
      sent = ""; // try again next time
    });
  };
  save();
  window.setInterval(save, EVERY_MS);
  window.addEventListener("pagehide", save);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") save();
  });
}
