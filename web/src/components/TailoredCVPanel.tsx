import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { MatchResult, TailoredCVView } from "../lib/types";

function Editor({ document, onSaved }: { document: TailoredCVView; onSaved: () => void }) {
  const qc = useQueryClient();
  const [headline, setHeadline] = useState(document.cv.basics.headline ?? "");
  const [summary, setSummary] = useState(document.cv.basics.summary ?? "");
  const [bullets, setBullets] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () => {
      const changed = Object.fromEntries(Object.entries(bullets).filter(([id, text]) =>
        document.cv.experience.some((role) => role.bullets.some((bullet) => bullet.id === id && bullet.text !== text)),
      ));
      return api.editTailoredCV(document.id, {
        ...(headline !== (document.cv.basics.headline ?? "") ? { headline } : {}),
        ...(summary !== (document.cv.basics.summary ?? "") ? { summary } : {}),
        bullets: changed,
      });
    },
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["tailored-cvs"] }); onSaved(); },
  });
  return (
    <div className="space-y-2 rounded-md border border-border bg-panel p-3 text-[12px]">
      <p className="text-muted">Review the text before linking it to a letter. Changes are checked against this draft's source CV; saving edits creates a new Word version.</p>
      <label className="block">Headline
        <input className="input mt-1 w-full" value={headline} onChange={(e) => setHeadline(e.target.value)} />
      </label>
      <label className="block">Summary
        <textarea className="input mt-1 w-full" rows={3} value={summary} onChange={(e) => setSummary(e.target.value)} />
      </label>
      {document.cv.experience.map((role) => (
        <div key={role.id} className="space-y-1">
          <p className="font-medium">{role.title} · {role.company} · {role.start}–{role.end ?? "present"}</p>
          {role.bullets.map((bullet) => (
            <label key={bullet.id} className="block text-muted">{bullet.id}
              <textarea className="input mt-1 w-full text-fg" rows={2} value={bullets[bullet.id] ?? bullet.text}
                onChange={(e) => setBullets((previous) => ({ ...previous, [bullet.id]: e.target.value }))} />
            </label>
          ))}
        </div>
      ))}
      <details className="rounded border border-border p-2" open={document.imported}>
        <summary className="cursor-pointer font-medium">Other extracted CV details</summary>
        <div className="mt-2 space-y-1 text-muted">
          <p>{[document.cv.basics.name, document.cv.basics.location, document.cv.basics.email, document.cv.basics.phone].filter(Boolean).join(" · ")}</p>
          {document.cv.skills.map((group) => <p key={group.category}><b>{group.category}:</b> {group.items.join(", ")}</p>)}
          {document.cv.education.map((item, index) => <p key={index}><b>Education:</b> {item.degree} {item.field} · {item.institution}</p>)}
          {document.cv.certifications.map((item, index) => <p key={index}><b>Certification:</b> {item.name}{item.issuer ? ` · ${item.issuer}` : ""}</p>)}
          {document.cv.projects.map((item) => <p key={item.id}><b>Project:</b> {item.name} · {item.description} {item.skills.join(", ")}</p>)}
          {document.cv.languages.length > 0 && <p><b>Languages:</b> {document.cv.languages.join(", ")}</p>}
        </div>
      </details>
      <button className="btn-primary py-1 text-[12px]" disabled={save.isPending} onClick={() => save.mutate()}>
        {save.isPending && <Loader2 size={12} className="animate-spin" />} Save reviewed CV
      </button>
      {save.error && <p className="text-bad">{(save.error as Error).message}</p>}
    </div>
  );
}

export function TailoredCVPanel({ jobId, result, hasCv, onLetter, letterPending }: {
  jobId: string;
  result?: MatchResult;
  hasCv: boolean;
  onLetter: (documentId?: string) => void;
  letterPending: boolean;
}) {
  const documents = useQuery({ queryKey: ["tailored-cvs", jobId], queryFn: () => api.tailoredCVs(jobId) });
  const state = useQuery({ queryKey: ["state"], queryFn: api.state });
  const [choice, setChoice] = useState<string | null>(null);
  const [assetId, setAssetId] = useState("");
  const [editing, setEditing] = useState(false);
  const qc = useQueryClient();
  const importCV = useMutation({
    mutationFn: () => api.importTailoredCV(jobId, assetId, result),
    onSuccess: (draft) => {
      setChoice(draft.id);
      setEditing(true);
      void qc.invalidateQueries({ queryKey: ["tailored-cvs"] });
    },
  });
  const wordCVs = (state.data?.cv_files.available ?? []).filter((asset) =>
    asset.filename.toLowerCase().endsWith(".docx") && !asset.filename.toLowerCase().includes("cover_letter"),
  );
  const selected = choice ?? documents.data?.[0]?.id ?? "";
  const document = documents.data?.find((item) => item.id === selected);
  return (
    <div className="mt-2 space-y-2 rounded-md bg-surface p-3 text-[12px]">
      <p className="font-medium">Cover letter source</p>
      {documents.isPending && <p className="text-muted">Loading saved tailored CVs…</p>}
      {documents.error && <p className="text-bad">{(documents.error as Error).message}</p>}
      {!documents.isPending && !documents.error && (
        <>
          {result && wordCVs.length > 0 && (
            <div className="space-y-1 border-b border-border pb-2">
              <p className="font-medium">Attach an older Word CV</p>
              <p className="text-muted">Import uses the quality model. Review the extracted text before linking it to a letter.</p>
              <div className="flex flex-wrap items-center gap-2">
                <select className="input min-w-0 flex-1" value={assetId} onChange={(e) => setAssetId(e.target.value)}>
                  <option value="">Choose a Word CV from your library</option>
                  {wordCVs.map((asset) => <option key={asset.id} value={asset.id}>{asset.filename}</option>)}
                </select>
                <button className="btn-ghost py-1 text-[12px]" disabled={!assetId || importCV.isPending} onClick={() => importCV.mutate()}>
                  {importCV.isPending && <Loader2 size={12} className="animate-spin" />} Import for this job
                </button>
              </div>
              {importCV.error && <p className="text-bad">{(importCV.error as Error).message}</p>}
            </div>
          )}
          <select className="input w-full" value={selected} onChange={(e) => { setChoice(e.target.value); setEditing(false); }}>
            {hasCv && <option value="">Selected CV from library</option>}
            {documents.data?.map((item) => (
              <option key={item.id} value={item.id}>Tailored CV · {new Date(item.updated_at).toLocaleString()}</option>
            ))}
          </select>
          {document ? (
            <div className="flex flex-wrap items-center gap-2">
              <a className="text-accent hover:underline" href={document.download_url}><Download size={11} /> Download saved CV</a>
              <button className="btn-ghost py-1 text-[12px]" onClick={() => setEditing(!editing)}>
                {editing ? "Close editor" : "Review and edit CV"}
              </button>
              <span className={document.reviewed ? "text-muted" : "text-warn"}>
                {document.reviewed ? "The letter will cite this saved CV." : "Review and save this imported CV before writing the letter."}
              </span>
            </div>
          ) : <p className="text-muted">{hasCv ? "The letter will cite your selected CV. Older Word drafts can be selected in the CV library." : "No saved tailored CV is available for this job."}</p>}
          {editing && document && <Editor key={document.id + document.updated_at} document={document} onSaved={() => setEditing(false)} />}
          <button className="btn-primary py-1 text-[12px]" disabled={letterPending || (!hasCv && !document) || (document != null && !document.reviewed)} onClick={() => onLetter(selected || undefined)}>
            {letterPending && <Loader2 size={12} className="animate-spin" />} Write cover letter
          </button>
        </>
      )}
    </div>
  );
}
