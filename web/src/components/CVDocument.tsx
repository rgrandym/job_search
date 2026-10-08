import { useMutation, useQuery } from "@tanstack/react-query";
import { ExternalLink, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { CVAsset } from "../lib/types";

/** The CV, including edits saved in Word and the assistant's edits. Word files are shown as Word's own PDF rendering. */
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
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!preview.data) return;
    const objectUrl = URL.createObjectURL(preview.data);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [preview.data]);
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-surface px-4 py-2 text-[12px]">
        <p className="min-w-0 flex-1 text-muted">
          Your CV, kept as one document: edits you save in Word and changes the assistant makes update this CV
          (its file in output/cvs stays identical to it). Buttons open that file.
        </p>
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
          <Loader2 size={14} className="animate-spin" /> {word ? "Word is preparing the page view…" : "Loading…"}
        </div>
      ) : url ? (
        <iframe title={asset.filename} src={url} className="cv-desk min-h-0 w-full flex-1 border-0" />
      ) : null}
    </div>
  );
}
