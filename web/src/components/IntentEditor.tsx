import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { LanguageLevel, SearchIntent } from "../lib/types";
import { AutoText, ChipInput, Field } from "./ui";

const LIST_FIELDS = [
  ["target_areas", "Areas to explore", "e.g. business development, venture investment: each becomes an adjacent role family if your CV supports it"],
  ["energising_work", "Work you want more of", ""],
  ["avoid_work", "Work you want less of", ""],
  ["preferred_sectors", "Preferred sectors", ""],
  ["avoided_sectors", "Sectors to avoid", ""],
  ["organisation_types", "Organisation types", "e.g. start-up, scale-up, large company, VC fund"],
  ["soft_dealbreakers", "Soft dealbreakers", "lower a role's priority, never hide it"],
  ["eligibility", "Confirmed eligibility", "e.g. right to work in the UK, SC clearance, full driving licence"],
] as const;
const LEVELS: LanguageLevel[] = ["native", "professional", "conversational", "basic"];

/** The selected CV's career intent: what the user wants, kept apart from what the CV proves. */
export function IntentEditor() {
  const qc = useQueryClient();
  const intent = useQuery({ queryKey: ["intent"], queryFn: api.intent });
  const [draft, setDraft] = useState<SearchIntent | null>(null);
  useEffect(() => {
    if (intent.data) setDraft(intent.data);
  }, [intent.data]);
  const save = useMutation({
    mutationFn: (value: SearchIntent) => api.saveIntent(value),
    onSuccess: (saved) => {
      qc.setQueryData(["intent"], saved);
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
  });

  if (intent.isPending || !draft)
    return (
      <p className="flex items-center gap-2 text-[12px] text-muted">
        <Loader2 size={14} className="animate-spin" /> Loading your career intent…
      </p>
    );
  if (intent.isError) return <p className="text-[12px] text-bad">Could not load the career intent: {(intent.error as Error).message}</p>;

  const patch = (update: Partial<SearchIntent>) => setDraft({ ...draft, ...update });
  const dirty = JSON.stringify(draft) !== JSON.stringify(intent.data);
  return (
    <div className="space-y-3 text-[12px]">
      <p className="text-faint">
        What you want from your next role, in your own words. It never changes what your CV proves: it orients the role
        families (your areas to explore are checked against CV evidence), marks each match as aligned or against your
        direction, and is the only source a cover letter may use for motivation. You can also tell the assistant, e.g. “I’d
        like to move into business development”. Profiles built before a change show “intent changed”; update them to use it.
      </p>
      <Field label="Direction">
        <AutoText
          minRows={2}
          placeholder="Where you want your career to go"
          value={draft.direction}
          onChange={(direction) => patch({ direction })}
        />
      </Field>
      {LIST_FIELDS.map(([field, label, hint]) => (
        <Field key={field} label={label} hint={hint || undefined}>
          <ChipInput value={draft[field]} onChange={(v) => patch({ [field]: v })} placeholder="Type, then Enter" />
        </Field>
      ))}
      <div>
        <p className="label mb-1">Working languages</p>
        <p className="mb-1 text-[11px] text-faint">
          Postings that require a language you have not listed are excluded; a higher level than yours is flagged.
          Empty: your CV’s languages are used.
        </p>
        <div className="space-y-1">
          {draft.languages.map((l, i) => (
            <div key={i} className="grid grid-cols-[minmax(0,1fr)_140px_24px] gap-1">
              <input
                className="input"
                placeholder="Language"
                value={l.language}
                onChange={(e) => patch({ languages: draft.languages.map((x, j) => (j === i ? { ...x, language: e.target.value } : x)) })}
              />
              <select
                className="input"
                value={l.level}
                onChange={(e) =>
                  patch({ languages: draft.languages.map((x, j) => (j === i ? { ...x, level: e.target.value as LanguageLevel } : x)) })
                }
              >
                {LEVELS.map((level) => (
                  <option key={level}>{level}</option>
                ))}
              </select>
              <button type="button" title="Remove" className="text-faint hover:text-bad" onClick={() => patch({ languages: draft.languages.filter((_, j) => j !== i) })}>
                <X size={14} />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="flex items-center gap-1 text-[12px] text-accent hover:underline"
            onClick={() => patch({ languages: [...draft.languages, { language: "", level: "professional" }] })}
          >
            <Plus size={12} /> Add language
          </button>
        </div>
      </div>
      {save.error && <p className="text-bad">{(save.error as Error).message}</p>}
      <div className="flex items-center justify-end gap-2">
        {intent.data?.updated_at && <span className="mr-auto text-[11px] text-faint">Saved {new Date(intent.data.updated_at).toLocaleString()}</span>}
        <button type="button" className="btn-ghost" disabled={!dirty} onClick={() => setDraft(intent.data ?? draft)}>
          Discard
        </button>
        <button
          type="button"
          className="btn-primary"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate({ ...draft, languages: draft.languages.filter((l) => l.language.trim()) })}
        >
          {save.isPending && <Loader2 size={14} className="animate-spin" />} Save intent
        </button>
      </div>
    </div>
  );
}
