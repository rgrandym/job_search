import { ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useState } from "react";

const KEY = "jobsearch.zoom";
const STEPS = [0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6];

function loadZoom(): number {
  try {
    const saved = Number(localStorage.getItem(KEY));
    return STEPS.includes(saved) ? saved : 1;
  } catch {
    return 1;
  }
}

/** Header control for the size of the whole app (text and layout together). The choice is
 *  remembered in this browser; clicking the percentage goes back to 100%. */
export function ZoomControl() {
  const [zoom, setZoom] = useState(loadZoom);

  useEffect(() => {
    document.documentElement.style.zoom = zoom === 1 ? "" : String(zoom);
    try {
      if (zoom === 1) localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, String(zoom));
    } catch {
      // private window or blocked storage: the size still applies for this visit
    }
  }, [zoom]);

  const step = (by: number) => {
    const i = STEPS.indexOf(zoom) + by;
    if (i >= 0 && i < STEPS.length) setZoom(STEPS[i]);
  };

  return (
    <div className="flex shrink-0 items-center gap-1 text-faint">
      <button className="hover:text-fg disabled:opacity-40" title="Smaller" disabled={zoom === STEPS[0]} onClick={() => step(-1)}>
        <ZoomOut size={15} />
      </button>
      <button className="w-9 text-center text-[11px] tabular-nums hover:text-fg" title="Text size: click for 100%" onClick={() => setZoom(1)}>
        {Math.round(zoom * 100)}%
      </button>
      <button className="hover:text-fg disabled:opacity-40" title="Larger" disabled={zoom === STEPS[STEPS.length - 1]} onClick={() => step(1)}>
        <ZoomIn size={15} />
      </button>
    </div>
  );
}
