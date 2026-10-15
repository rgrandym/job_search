import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Stethoscope } from "lucide-react";
import { api } from "../lib/api";

/** Check the selected CV (its Word design and its facts) and offer an ATS-safe copy as the
 * design every tailored CV starts from (backend `services/cv_health.py`). */
export function CVHealthPanel() {
  const qc = useQueryClient();
  const check = useMutation({ mutationFn: () => api.cvHealth() });
  const copy = useMutation({
    meta: { syncLists: true },
    mutationFn: () => api.cvAtsCopy(),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["state"] });
      check.reset();
    },
  });
  const health = check.data;
  return (
    <div className="space-y-1">
      <button className="btn-ghost py-1 text-[11px]" disabled={check.isPending} onClick={() => check.mutate()}>
        {check.isPending ? <Loader2 size={12} className="animate-spin" /> : <Stethoscope size={12} />}
        Check CV
      </button>
      <p className="text-[10px] text-faint">What an ATS may miss in its Word design, and what its facts leave out. Every tailored CV starts from it.</p>
      {check.error && <p className="text-[11px] text-bad">{(check.error as Error).message}</p>}
      {health && (
        <div className="space-y-1 rounded border border-border p-2 text-[11px]">
          <p className="font-medium">{health.cv_name}{health.word_file ? ` · design: ${health.word_file}` : ""}</p>
          <p className="text-muted">Readability {health.readability.score} ({health.readability.grade})</p>
          {health.readability.notes.map((note) => <p key={note} className="text-faint">{note}</p>)}
          <p className="font-medium">Design (ATS)</p>
          {health.design.length ? health.design.map((w) => <p key={w} className="text-warn">{w}</p>) : <p className="text-good">Nothing an ATS is known to miss.</p>}
          {health.design.length > 0 && (
            <div className="space-y-0.5">
              <button className="btn-ghost py-1 text-[11px]" disabled={copy.isPending} onClick={() => copy.mutate()}>
                {copy.isPending && <Loader2 size={12} className="animate-spin" />} Make an ATS-safe copy and use it
              </button>
              <p className="text-[10px] text-faint">Single column, standard headings, contact details in the body. Added to your library and selected; your own file stays.</p>
            </div>
          )}
          {copy.data && <p className="text-good">Added and selected: {copy.data.filename}</p>}
          {copy.error && <p className="text-bad">{(copy.error as Error).message}</p>}
          <p className="font-medium">Facts</p>
          {health.knowledge.length ? health.knowledge.map((k) => <p key={k} className="text-muted">{k}</p>) : <p className="text-good">Every role has its lines, results and skills.</p>}
        </div>
      )}
    </div>
  );
}
