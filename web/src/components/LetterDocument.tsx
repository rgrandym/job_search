import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, ExternalLink, Eye, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { CVAsset } from "../lib/types";
import { CVDocument } from "./CVDocument";
import { Modal } from "./ui";

/** "View cover letter": opens the viewer, which has Open in Word and Download. */
export function LetterViewButton({ id, title, downloadUrl }: { id: string; title: string; downloadUrl: string }) {
  const [viewing, setViewing] = useState(false);
  return (
    <>
      <button type="button" className="btn-primary py-1 text-[12px]" onClick={() => setViewing(true)}>
        <Eye size={12} /> View cover letter
      </button>
      <Modal open={viewing} onClose={() => setViewing(false)} title={title} id="cover-letter-view" document>
        <LetterDocument id={id} title={title} downloadUrl={downloadUrl} />
      </Modal>
    </>
  );
}

/** "View CV": a tailored CV's Word file in the CV viewer (Open in Word and the page view). */
export function CVViewButton({ downloadUrl, title }: { downloadUrl: string; title: string }) {
  const [viewing, setViewing] = useState(false);
  const name = decodeURIComponent(downloadUrl.split("/").pop() ?? "");
  const asset: CVAsset = { id: `generated:${name}`, filename: name, size: 0, kind: "generated", parsed: false, selected: false };
  return (
    <>
      <button type="button" className="btn-primary py-1 text-[12px]" disabled={!name} onClick={() => setViewing(true)}>
        <Eye size={12} /> View CV
      </button>
      <Modal open={viewing} onClose={() => setViewing(false)} title={title} id="tailored-cv-view" document>
        <CVDocument asset={asset} />
      </Modal>
    </>
  );
}

/** The letter's Word file as an HTML page view (Word opens only on request); edits saved in Word show on returning. */
function LetterDocument({ id, title, downloadUrl }: { id: string; title: string; downloadUrl: string }) {
  const open = useMutation({ mutationFn: () => api.openCoverLetter(id, "word") });
  const preview = useQuery({
    queryKey: ["cover-letter-preview", id],
    queryFn: () => api.coverLetterPreview(id),
    refetchOnWindowFocus: "always", // a letter edited in Word is shown again (unchanged files are cached)
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
        <p className="min-w-0 flex-1 text-muted">Your cover letter. Open in Word to edit it.</p>
        {open.isPending && <Loader2 size={13} className="animate-spin text-muted" />}
        <a className="btn-ghost py-1" href={downloadUrl} download><Download size={13} /> Download</a>
        <button type="button" className="btn-primary py-1" disabled={open.isPending} onClick={() => open.mutate()}>
          <ExternalLink size={13} /> Open in Word
        </button>
        {open.data && !open.data.opened && <span className="text-bad">Word did not open; use Download.</span>}
        {open.error && <span role="alert" className="text-bad">{open.error.message}</span>}
      </div>
      {preview.error ? (
        <div className="cv-desk flex flex-1 items-center justify-center p-8 text-center text-[12px] text-bad">{preview.error.message}</div>
      ) : preview.isPending ? (
        <div className="cv-desk flex flex-1 items-center justify-center gap-2 text-[12px] text-muted">
          <Loader2 size={14} className="animate-spin" /> Preparing the page view…
        </div>
      ) : url ? (
        <iframe title={title} src={url} sandbox="" className="cv-desk min-h-0 w-full flex-1 border-0" />
      ) : null}
    </div>
  );
}
