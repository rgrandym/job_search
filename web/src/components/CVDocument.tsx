import { useMutation, useQuery } from "@tanstack/react-query";
import { ExternalLink, Loader2, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { CVAsset } from "../lib/types";

/** Size of the CV page alone, separate from the app's zoom: a dense CV can need more than the
 *  rest of the app. Remembered in this browser. */
const CV_ZOOM_KEY = "jobsearch.cvZoom";
const CV_ZOOM_STEPS = [0.75, 1, 1.25, 1.5, 1.75, 2];

function savedCvZoom(): number {
  try {
    const value = Number(localStorage.getItem(CV_ZOOM_KEY));
    return CV_ZOOM_STEPS.includes(value) ? value : 1;
  } catch {
    return 1;
  }
}

/** The preview as the frame shows it at `zoom`. The page view of a Word file is sandboxed, so the
 *  zoom goes into its own stylesheet, where it reflows the text sharply; a PDF opens at that zoom. */
async function zoomedPreview(blob: Blob, zoom: number): Promise<{ blob: Blob; hash: string }> {
  if (blob.type.startsWith("text/html")) {
    if (zoom === 1) return { blob, hash: "" };
    const html = await blob.text();
    const style = `<style>html{zoom:${zoom}}</style>`;
    const zoomed = /<\/head>/i.test(html) ? html.replace(/<\/head>/i, () => `${style}</head>`) : style + html;
    return { blob: new Blob([zoomed], { type: blob.type }), hash: "" };
  }
  return { blob, hash: blob.type === "application/pdf" ? `#zoom=${Math.round(zoom * 100)}` : "" };
}

/** The CV, including edits saved in Word and the assistant's edits. Word files are shown as an HTML page view; Word opens only on request. */
export function CVDocument({ asset }: { asset: CVAsset }) {
  const open = useMutation({ mutationFn: (app: "default" | "word") => api.openCVSource(asset.id, app) });
  const name = asset.filename.toLowerCase();
  const pdf = name.endsWith(".pdf");
  const word = name.endsWith(".docx");
  const revision = useQuery({
    queryKey: ["cv-revision", asset.id],
    queryFn: () => api.cvRevision(asset.id),
    refetchInterval: 5_000,
    refetchOnWindowFocus: "always",
  });
  const preview = useQuery({
    queryKey: ["cv-preview", asset.id, revision.data?.revision],
    queryFn: () => api.cvPreview(asset.id),
    enabled: !!revision.data,
    staleTime: Infinity,
    retry: false,
  });
  const [zoom, setZoom] = useState(savedCvZoom);
  const stepZoom = (by: number) => {
    const next = by === 0 ? 1 : CV_ZOOM_STEPS[CV_ZOOM_STEPS.indexOf(zoom) + by];
    if (next === undefined) return;
    setZoom(next);
    try {
      localStorage.setItem(CV_ZOOM_KEY, String(next));
    } catch {
      // private window or blocked storage: the size still applies for this visit
    }
  };
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!preview.data) return;
    let objectUrl: string | null = null;
    let cancelled = false;
    void zoomedPreview(preview.data, zoom).then(({ blob, hash }) => {
      if (cancelled) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl + hash);
    });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [preview.data, zoom]);
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-surface px-4 py-2 text-[12px]">
        <p className="min-w-0 flex-1 text-muted">
          Your CV, kept as one document: edits you save in Word and changes the assistant makes update this CV
          (its file in output/cvs stays identical to it). Buttons open that file.
        </p>
        <div className="flex shrink-0 items-center gap-1 text-faint" aria-label="CV size">
          <button type="button" className="hover:text-fg disabled:opacity-40" title="Smaller CV" disabled={zoom === CV_ZOOM_STEPS[0]} onClick={() => stepZoom(-1)}>
            <ZoomOut size={15} />
          </button>
          <button type="button" className="w-9 text-center text-[11px] tabular-nums hover:text-fg" title="CV size: click for 100%" onClick={() => stepZoom(0)}>
            {Math.round(zoom * 100)}%
          </button>
          <button
            type="button"
            className="hover:text-fg disabled:opacity-40"
            title="Larger CV"
            disabled={zoom === CV_ZOOM_STEPS[CV_ZOOM_STEPS.length - 1]}
            onClick={() => stepZoom(1)}
          >
            <ZoomIn size={15} />
          </button>
        </div>
        {open.isPending && <Loader2 size={13} className="animate-spin text-muted" />}
        {pdf && (
          <button type="button" className="btn-ghost py-1" disabled={open.isPending} onClick={() => open.mutate("default")}>
            <ExternalLink size={13} /> Open in Preview
          </button>
        )}
        {(pdf || word) && (
          <button type="button" className="btn-primary py-1" disabled={open.isPending} onClick={() => open.mutate("word")}>
            <ExternalLink size={13} /> {pdf ? "Edit in Word" : "Open in Word"}
          </button>
        )}
        {open.data && !open.data.opened && <a className="text-accent hover:underline" href={api.cvSourceUrl(asset.id)} download>No app found. Download the file</a>}
        {open.error && <span role="alert" className="text-bad">{open.error.message}</span>}
      </div>
      {revision.error || preview.error ? (
        <div className="cv-desk flex flex-1 items-center justify-center p-8 text-center text-[12px] text-bad">{(revision.error ?? preview.error)?.message}</div>
      ) : revision.isPending || preview.isPending ? (
        <div className="cv-desk flex flex-1 items-center justify-center gap-2 text-[12px] text-muted">
          <Loader2 size={14} className="animate-spin" /> {word ? "Preparing the page view…" : "Loading…"}
        </div>
      ) : url ? (
        <iframe title={asset.filename} src={url} sandbox={preview.data?.type.startsWith("text/html") ? "" : undefined} className="cv-desk min-h-0 w-full flex-1 border-0" />
      ) : null}
    </div>
  );
}
