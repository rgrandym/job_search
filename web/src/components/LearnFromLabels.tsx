import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Sparkles, X } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { LearnedPreference, LearningState, PreferenceKind } from "../lib/types";

const KIND: Record<PreferenceKind, [string, string, string]> = {
  requirement_gap: ["Requirement you lack", "text-warn", "Postings making this a core requirement are not a fit"],
  seniority_floor: ["Too junior", "text-warn", "Roles below your level are not a fit"],
  not_a_fit: ["Not a fit", "text-warn", "Added to the role types to reject"],
  target_role: ["Target role", "text-good", "Added to the roles you fit now"],
  transferable_strength: ["Transferable strength", "text-good", "Credits experience that carries beyond your title"],
  adjacent_family: ["Adjacent roles to search", "text-accent", "Searched when you widen to adjacent roles"],
};

function Kind({ kind }: { kind: PreferenceKind }) {
  const [label, tone, title] = KIND[kind];
  return (
    <span className={`chip shrink-0 ${tone}`} title={title}>
      {label}
    </span>
  );
}

function Family({ p }: { p: LearnedPreference }) {
  if (!p.family) return null;
  return (
    <p className="text-[11px] text-faint">
      Titles: {p.family.titles.join(", ")}
      {p.family.gap && ` · gap: ${p.family.gap}`}
    </p>
  );
}

function Proposal({ p, onDecide, busy }: { p: LearnedPreference; onDecide: (accept: boolean, text: string) => void; busy: boolean }) {
  const [text, setText] = useState(p.text);
  return (
    <li className="space-y-1 rounded-md border border-border p-2">
      <div className="flex flex-wrap items-center gap-1">
        <Kind kind={p.kind} />
        <span className="text-[11px] text-faint">from {p.label_ids.length} labelled job(s)</span>
      </div>
      <textarea
        className="input min-h-[44px] w-full text-[12px]"
        value={text}
        maxLength={300}
        onChange={(e) => setText(e.target.value)}
        title="Edit the wording before accepting: this text goes into your profile as written"
      />
      <Family p={p} />
      {p.rationale && <p className="text-[11px] text-muted">Why: {p.rationale}</p>}
      <div className="flex gap-2">
        <button className="btn-ghost py-0.5 text-[12px] text-good" disabled={busy || !text.trim()} onClick={() => onDecide(true, text)}>
          <Check size={12} /> Accept
        </button>
        <button className="btn-ghost py-0.5 text-[12px] text-bad" disabled={busy} onClick={() => onDecide(false, text)}>
          <X size={12} /> Reject
        </button>
      </div>
    </li>
  );
}

/** Preferences learned from your labels and notes: propose, review, keep in force. */
export function LearnFromLabels() {
  const qc = useQueryClient();
  const data = useQuery({ queryKey: ["learning"], queryFn: api.learning });
  const store = (s: LearningState) => qc.setQueryData(["learning"], s);
  const suggest = useMutation({ mutationFn: api.suggestPreferences, onSuccess: store });
  const decide = useMutation({
    mutationFn: (v: { id: string; accept: boolean; text: string }) => api.decidePreference(v.id, v.accept, v.text),
    onSuccess: (s) => {
      store(s);
      void qc.invalidateQueries({ queryKey: ["intent"] }); // an adjacent family adds a target area
    },
  });
  const remove = useMutation({ mutationFn: api.removePreference, onSuccess: store });
  const error = (suggest.error ?? decide.error ?? remove.error ?? data.error) as Error | null;
  const busy = decide.isPending || remove.isPending;
  const s = suggest.data ?? data.data;

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <p className="label">Learn from your labels</p>
      <p className="text-[11px] text-muted">
        Turns your calls and notes into general preferences for every search: requirements your CV lacks, roles too junior
        for you, and adjacent roles where your leadership, team management or business development count. Nothing changes
        until you accept it; you can reword each one first. Notes saying <i>why</i> make the suggestions better.
      </p>
      <button className="btn-ghost text-[12px]" disabled={suggest.isPending} onClick={() => suggest.mutate()}>
        {suggest.isPending ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
        {suggest.isPending ? "Reading your labels… (up to a minute)" : "Suggest profile updates"}
      </button>
      {error && <p className="text-[12px] text-bad">{error.message}</p>}
      {data.isPending && <p className="text-[12px] text-faint">Loading…</p>}
      {s && s.set_aside.length > 0 && (
        <details className="text-[11px] text-faint">
          <summary className="cursor-pointer">{s.set_aside.length} suggestion(s) set aside by the checks</summary>
          {s.set_aside.map((line) => (
            <p key={line}>· {line}</p>
          ))}
        </details>
      )}
      {s && s.pending.length > 0 && (
        <ul className="space-y-2">
          {s.pending.map((p) => (
            <Proposal key={p.id} p={p} busy={busy} onDecide={(accept, text) => decide.mutate({ id: p.id, accept, text })} />
          ))}
        </ul>
      )}
      {s && suggest.isSuccess && s.pending.length === 0 && <p className="text-[12px] text-faint">No new suggestions from your labels.</p>}
      {s && s.accepted.length > 0 && (
        <div className="space-y-1">
          <p className="label">In force ({s.accepted.length})</p>
          {s.accepted.map((p) => (
            <div key={p.id} className="flex items-start gap-1 text-[12px]">
              <Kind kind={p.kind} />
              <span className="min-w-0 flex-1 text-muted">
                {p.text}
                {p.auto && <span className="ml-1 text-[11px] text-faint">(added automatically from your reasons)</span>}
              </span>
              <button
                className="shrink-0 text-faint hover:text-bad"
                title="Withdraw: searches stop using it (it will not be suggested again)"
                disabled={busy}
                onClick={() => remove.mutate(p.id)}
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
      {s && s.accepted.length === 0 && s.pending.length === 0 && !suggest.isSuccess && (
        <p className="text-[12px] text-faint">No preferences yet{s.rejected ? ` (${s.rejected} rejected)` : ""}.</p>
      )}
    </div>
  );
}
