import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { MatchResult, ReviewAddition, TailoredCVView } from "../lib/types";

/** The wording a confirmation starts from: for added words, the bullet with them; for a
 * number question, the bullet itself (the user adds the number). */
function startingText(item: ReviewAddition): string {
  if (item.kind === "enrichment" && item.original_text) return `${item.original_text.trim().replace(/[.;]+$/, "")}, ${item.text.trim().replace(/\.$/, "")}.`;
  if (item.kind === "metric" && item.original_text) return item.original_text;
  return item.text;
}

function kindLabel(item: ReviewAddition, document: TailoredCVView): string {
  const role = document.cv.experience.find((r) => r.id === item.role_id)?.title ?? item.role_id ?? "";
  switch (item.kind) {
    case "enrichment": return `Words for an existing line · ${role}`;
    case "rewording": return `Your line in the job's terms (replaces it in this CV) · ${role}`;
    case "metric": return `Number for an existing line (${item.text}) · ${role}`;
    case "experience": return `Experience · ${role}`;
    case "summary": return "Summary sentence";
    case "headline": return "Headline";
    default: return item.skill_group ? `Skill · ${item.skill_group}` : "Skill";
  }
}

function Compressions({ document }: { document: TailoredCVView }) {
  const qc = useQueryClient();
  const pending = (document.report?.compressions ?? []).filter((c) => c.status === "pending");
  const applied = (document.report?.compressions ?? []).filter((c) => c.status === "applied" && (c.kind === "shorter_role" || c.kind === "trim_skills"));
  const decide = useMutation({
    meta: { syncLists: true },
    mutationFn: ({ id, decision }: { id: string; decision: "apply" | "reject" | "restore" }) => api.reviewCVCompression(document.id, id, decision),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["tailored-cvs"] }); void qc.invalidateQueries({ queryKey: ["cv-comparison", document.job_id] }); },
  });
  if (!pending.length && !applied.length) return null;
  const bullets = Object.fromEntries(document.cv.experience.flatMap((r) => r.bullets.map((b) => [b.id, b.text])));
  const roles = Object.fromEntries(document.cv.experience.map((r) => [r.id, `${r.title} · ${r.company}`]));
  return (
    <div className="space-y-2 rounded border border-border p-2">
      {applied.length > 0 && (
        <>
          <p className="font-medium">Shortened for this job</p>
          <p className="text-muted">This CV is the short version: these roles and skills lines are rewritten for the job, red in the Word draft. Restore your own lines for any of them.</p>
          {applied.map((item) => (
            <div key={item.id} className="space-y-1 rounded border border-border p-2">
              <p className="font-medium">{item.kind === "trim_skills" ? (item.bullets.length ? `Skills · ${item.category ?? ""}: ${item.bullets.length} of ${item.before?.length ?? item.bullets.length + item.removed.length} kept` : `Skills line removed · ${item.category ?? ""}`) : `${roles[item.role_ids[0]] ?? ""}: ${item.bullets.length} lines for ${item.before_ids?.length ?? item.source_ids.length}`}</p>
              {item.removed.length > 0 && <p className="text-faint">Left out: {item.removed.join("; ")}</p>}
              <p className="text-faint">Why: {item.reason}</p>
              <button className="btn-ghost py-1 text-[11px]" disabled={decide.isPending} onClick={() => decide.mutate({ id: item.id, decision: "restore" })}>Restore my lines</button>
            </div>
          ))}
        </>
      )}
      {pending.length > 0 && <p className="font-medium">Shorter versions to compare</p>}
      {pending.length > 0 && <p className="text-muted">Each is in red in the Word file, below the lines it would replace. Your lines stay unless you choose the shorter version. A shorter role keeps every job term and lists what it leaves out; a merge or tightened line keeps every number and job term.</p>}
      {pending.map((item) => (
        <div key={item.id} className="space-y-1 rounded border border-border p-2">
          <p className="font-medium">{item.kind === "merge_roles" ? `Merge ${item.role_ids.length} similar roles` : item.kind === "merge_bullets" ? `Merge ${item.source_ids.length} overlapping lines · ${roles[item.role_ids[0]] ?? ""}` : item.kind === "condense_role" ? `Shorter entry for an earlier role · ${roles[item.role_ids[0]] ?? ""}` : item.kind === "shorter_role" ? `Shorter version of this role: ${item.bullets.length} lines for ${item.source_ids.length} · ${roles[item.role_ids[0]] ?? ""}` : item.kind === "trim_skills" ? (item.bullets.length ? `Shorter skills line · ${item.category ?? ""}: keeps ${item.bullets.length} of ${item.bullets.length + item.removed.length}` : `Remove the skills line · ${item.category ?? ""}`) : `Tighten a line · ${roles[item.role_ids[0]] ?? ""}`}</p>
          {item.kind !== "condense_role" && item.kind !== "shorter_role" && item.kind !== "trim_skills" && (
            <div className="space-y-0.5 text-muted">
              {item.kind === "merge_roles"
                ? item.role_ids.map((id) => <p key={id}>Now: {roles[id] ?? id}</p>)
                : item.source_ids.map((id) => <p key={id}>Now: {bullets[id] ?? id}</p>)}
            </div>
          )}
          {item.kind === "condense_role" && <p className="text-muted">Keeps only these lines:</p>}
          {item.kind === "shorter_role" && <p className="text-muted">Your role's lines stay above it in the Word file. Replaced by:</p>}
          {item.kind === "trim_skills" && item.bullets.length > 0 && <p className="text-muted">Keeps:</p>}
          <div className="space-y-0.5 text-bad">
            {item.heading && <p className="font-medium">{item.heading}</p>}
            {item.kind === "trim_skills" ? item.bullets.length > 0 && <p>{item.bullets.join(", ")}</p> : item.bullets.map((text) => <p key={text}>{text}</p>)}
          </div>
          {item.removed.length > 0 && <p className="text-faint">Left out: {item.removed.join("; ")}</p>}
          <p className="text-faint">Why: {item.reason}</p>
          <div className="flex gap-2">
            <button className="btn-primary py-1 text-[11px]" disabled={decide.isPending} onClick={() => decide.mutate({ id: item.id, decision: "apply" })}>Use the shorter version</button>
            <button className="btn-ghost py-1 text-[11px]" disabled={decide.isPending} onClick={() => decide.mutate({ id: item.id, decision: "reject" })}>Keep my lines</button>
          </div>
        </div>
      ))}
      {decide.error && <p className="text-bad">{(decide.error as Error).message}</p>}
    </div>
  );
}

/** The clean final version: every change in black, nothing pending (backend `finalise`). */
function FinalVersion({ document }: { document: TailoredCVView }) {
  const qc = useQueryClient();
  const write = useMutation({
    mutationFn: () => api.finalTailoredCV(document.id),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["tailored-cvs"] }); },
  });
  const test = useMutation({ mutationFn: () => api.addToTestSet(document.id) });
  if (document.imported) return null;
  return (
    <div className="space-y-1 rounded border border-border p-2">
      <p className="text-muted">The draft shows every change in red. When you are happy with it, write the clean version to send: all black, with nothing still pending.</p>
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-primary py-1 text-[12px]" disabled={write.isPending} onClick={() => write.mutate()}>
          {write.isPending && <Loader2 size={12} className="animate-spin" />} Write final version
        </button>
        {document.final_url && <a className="text-accent hover:underline" href={document.final_url}><Download size={11} /> Download final version</a>}
        <button className="btn-ghost py-1 text-[12px]" disabled={test.isPending} title="Keep this CV and job to measure the optimiser on (python -m src.services.tailoring_tests run)" onClick={() => test.mutate()}>
          {test.isPending && <Loader2 size={12} className="animate-spin" />} Save to test set
        </button>
      </div>
      {test.data && <p className="text-good">Saved to the test set ({test.data.count} {test.data.count === 1 ? "case" : "cases"}).</p>}
      {test.error && <p className="text-bad">{(test.error as Error).message}</p>}
      {document.final_notes.map((note) => <p key={note} className="text-warn">{note}</p>)}
      {write.error && <p className="text-bad">{(write.error as Error).message}</p>}
    </div>
  );
}

function Editor({ document, onSaved }: { document: TailoredCVView; onSaved: () => void }) {
  const qc = useQueryClient();
  const [headline, setHeadline] = useState(document.cv.basics.headline ?? "");
  const [summary, setSummary] = useState(document.cv.basics.summary ?? "");
  const [bullets, setBullets] = useState<Record<string, string>>({});
  const [additionText, setAdditionText] = useState<Record<string, string>>({});
  const [declineReason, setDeclineReason] = useState<Record<string, string>>({});
  const review = useMutation({
    meta: { syncLists: true },
    mutationFn: ({ id, decision }: { id: string; decision: "confirm" | "delete" }) => {
      const item = document.report?.review_additions?.find((a) => a.id === id);
      const text = additionText[id] ?? (item && item.kind !== "skill" && item.kind !== "experience" ? startingText(item) : undefined);
      return api.reviewCVAddition(document.id, id, decision, decision === "confirm" ? text : undefined, declineReason[id]);
    },
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["tailored-cvs"] }); void qc.invalidateQueries({ queryKey: ["cv-comparison", document.job_id] }); },
  });
  const save = useMutation({
    meta: { syncLists: true },
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
      <p className="text-muted">Review the text before linking it to a letter. Changes are checked against this draft's source CV; saving updates this draft's Word file.</p>
      {!!document.report?.review_additions?.some((item) => item.status === "pending") && (
        <div className="space-y-2 rounded border border-warn/40 p-2">
          <p className="font-medium text-warn">Confirm or delete each red suggestion before using this CV</p>
          <p className="text-muted">These are what your roles very probably involved but your CV does not say, inferred from your background for this job: not verified claims. Edit the wording to what you actually did before confirming. Confirmed experience, skills, words and numbers are also added to the selected source CV; a summary sentence or headline changes this draft only. A deleted suggestion is remembered for this CV and not proposed again.</p>
          {document.report.review_additions.filter((item) => item.status === "pending").map((item) => (
            <div key={item.id} className="space-y-1 rounded border border-border p-2">
              <p className="font-medium text-warn">{kindLabel(item, document)}: {item.requirement}{item.likelihood === "possible" && <span className="ml-1 text-faint">(possible: check carefully)</span>}</p>
              {(item.kind === "enrichment" || item.kind === "metric" || item.kind === "rewording") && item.original_text && (
                <p className="text-muted">Your line: {item.original_text}</p>
              )}
              <textarea className="input w-full text-bad" rows={2} value={additionText[item.id] ?? startingText(item)}
                onChange={(event) => setAdditionText((previous) => ({ ...previous, [item.id]: event.target.value }))} />
              {item.kind === "metric" && <p className="text-faint">Add the real number to the line above; never an estimate you could not back up at interview.</p>}
              <p className="text-faint">Why ask: {item.reason}{item.basis ? ` · Typical because: ${item.basis}` : ""} · CV: “{item.background_quote}” · Job: “{item.job_quote}”</p>
              <div className="flex flex-wrap items-center gap-2">
                <button className="btn-primary py-1 text-[11px]" disabled={review.isPending} onClick={() => review.mutate({ id: item.id, decision: "confirm" })}>Confirm edited fact</button>
                <button className="btn-ghost py-1 text-[11px]" disabled={review.isPending} onClick={() => review.mutate({ id: item.id, decision: "delete" })}>Delete suggestion</button>
                <input className="input min-w-0 flex-1 py-0.5 text-[11px]" placeholder="Why not (optional, remembered)" value={declineReason[item.id] ?? ""}
                  onChange={(event) => setDeclineReason((previous) => ({ ...previous, [item.id]: event.target.value }))} />
              </div>
            </div>
          ))}
          {review.error && <p className="text-bad">{(review.error as Error).message}</p>}
        </div>
      )}
      <Compressions document={document} />
      <label className="block">Headline
        <input className="input mt-1 w-full" value={headline} onChange={(e) => setHeadline(e.target.value)} />
      </label>
      {document.headline_options.length > 0 && (
        <div className="flex flex-wrap gap-1 text-[11px]">
          <span className="text-faint">Suggestions:</span>
          {[document.original_headline ?? "", ...document.headline_options].filter(Boolean).map((option, index) => (
            <button key={option} type="button" onClick={() => setHeadline(option)}
              className={headline === option ? "rounded border border-accent px-2 py-0.5 text-fg" : "rounded border border-border px-2 py-0.5 text-muted hover:border-accent"}>
              {index === 0 ? `Your own: ${option}` : option}
            </button>
          ))}
        </div>
      )}
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
    meta: { syncLists: true },
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
  useEffect(() => {
    const report = document?.report;
    if (report?.review_additions?.some((item) => item.status === "pending") || report?.compressions?.some((item) => item.status === "pending")) setEditing(true);
  }, [document?.id]); // eslint-disable-line react-hooks/exhaustive-deps -- open on selection, not after every edit
  return (
    <div className="reading mt-2 space-y-2 rounded-md bg-surface p-3 text-[12px]">
      <p className="font-medium">Review CV</p>
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
              <option key={item.id} value={item.id}>{item.label} · {new Date(item.updated_at).toLocaleString()}</option>
            ))}
          </select>
          {document ? (
            <div className="flex flex-wrap items-center gap-2">
              <a className="text-accent hover:underline" href={document.download_url}><Download size={11} /> Download draft (changes in red)</a>
              <button className="btn-ghost py-1 text-[12px]" onClick={() => setEditing(!editing)}>
                {editing ? "Close editor" : "Review and edit CV"}
              </button>
              <span className={document.reviewed ? "text-muted" : "text-warn"}>
                {document.reviewed
                  ? "The letter will cite this saved CV."
                  : document.imported
                    ? "Review and save this imported CV before writing the letter."
                    : "Resolve red suggestions before writing the letter."}
              </span>
            </div>
          ) : <p className="text-muted">{hasCv ? "The letter will cite your selected CV. Older Word drafts can be selected in the CV library." : "No saved tailored CV is available for this job."}</p>}
          {editing && document && <Editor key={document.id + document.updated_at} document={document} onSaved={() => setEditing(false)} />}
          {document && <FinalVersion document={document} />}
          <button className="btn-primary py-1 text-[12px]" disabled={letterPending || (!hasCv && !document) || (document != null && !document.reviewed)} onClick={() => onLetter(selected || undefined)}>
            {letterPending && <Loader2 size={12} className="animate-spin" />} Write cover letter from this CV
          </button>
        </>
      )}
    </div>
  );
}
