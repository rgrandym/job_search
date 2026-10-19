import { ZoomIn, ZoomOut } from "lucide-react";
import { DEFAULT_ZOOM, ZOOM_STEPS, useZoom } from "../stores/zoomStore";

/** Header control for the size of the whole app (text and layout together, see `zoomStore`).
 *  Clicking the percentage goes back to the default size. */
export function ZoomControl() {
  const { zoom, step } = useZoom();
  return (
    <div className="flex shrink-0 items-center gap-1 text-faint">
      <button className="hover:text-fg disabled:opacity-40" title="Smaller" disabled={zoom === ZOOM_STEPS[0]} onClick={() => step(-1)}>
        <ZoomOut size={15} />
      </button>
      <button
        className="w-9 text-center text-[11px] tabular-nums hover:text-fg"
        title={`Size: click for ${Math.round(DEFAULT_ZOOM * 100)}%`}
        onClick={() => step(0)}
      >
        {Math.round(zoom * 100)}%
      </button>
      <button className="hover:text-fg disabled:opacity-40" title="Larger" disabled={zoom === ZOOM_STEPS[ZOOM_STEPS.length - 1]} onClick={() => step(1)}>
        <ZoomIn size={15} />
      </button>
    </div>
  );
}
