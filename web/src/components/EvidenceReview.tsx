import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, FileUp, Loader2, X } from "lucide-react";
import { useRef, useState } from "react";
import { api } from "../lib/api";
import type { QueuedEvidence } from "../lib/types";
import { AutoText, Field } from "./ui";

const KIND_LABEL: Record<QueuedEvidence["kind"], string> = {
  skill: "Skill",
  certification: "Certification",
  project: "Project",
  bullet: "Achievement",
};
const CONFIDENCE_COLOR = { high: "var(--good)", medium: "var(--accent)", low: "var(--text-muted)" };

/** Propose Master CV additions from documents the user supplies; nothing is added until accepted. */
export function EvidenceReview() {
  const qc = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [source, setSource] = useState("");
  const [text, setText] = useState("");
  const queue = useQuery({ queryKey: ["evidence"], queryFn: api.evidence });
  const refresh = () => qc.invalidateQueries({ queryKey: ["evidence"] });
  const upload = useMutation({ mutationFn: (file: File) => api.evidenceUpload(file), onSuccess: () => void refresh() });
  const paste = useMutation({
    mutationFn: () => api.evidenceText(source.trim() || "pasted text", text),
    onSuccess: () => {
      setText("");
      void refresh();
    },
  });
  const reading = upload.isPending || paste.isPending;
  const lastFound = upload.data ?? paste.data;

  return (
    <div className="space-y-3 text-[12px]">
      <p className="text-faint">
        Add a document that shows more than your CV does: a portfolio page, project report, publication list, reference letter
        or certificate. The assistant proposes skills, certifications, projects and achievements, each with the exact passage
        that states it; proposals it cannot quote, or that your CV already has, are dropped. Nothing reaches your Master CV
        until you accept it.
      </p>
      <div className="flex flex-wrap gap-2">
        <input
          ref={fileInput}
          type="file"
          accept=".pdf,.docx,.md,.txt"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) upload.mutate(file);
            e.target.value = "";
          }}
        />
        <button className="btn-ghost" disabled={reading} onClick={() => fileInput.current?.click()}>
          {upload.isPending ? <Loader2 size={14} className="animate-spin" /> : <FileUp size={14} />} Upload a document
        </button>
      </div>
      <Field label="Or paste text">
        <AutoText singleLine className="mb-1" placeholder="Where it comes from (e.g. portfolio site)" value={source} onChange={setSource} />
        <AutoText minRows={4} placeholder="Paste the text here" value={text} onChange={setText} />
      </Field>
      <div className="flex justify-end">
        <button className="btn-primary" disabled={reading || text.trim().length < 40} onClick={() => paste.mutate()}>
          {paste.isPending && <Loader2 size={14} className="animate-spin" />} Find new evidence
        </button>
      </div>
      {(upload.error || paste.error) && <p className="text-bad">{((upload.error ?? paste.error) as Error).message}</p>}
      {lastFound && <p className="text-muted">{lastFound.length} new proposal(s) from that document.</p>}

      <div>
        <p className="label mb-1">Waiting for your review</p>
        {queue.isPending ? (
          <p className="flex items-center gap-2 text-muted">
            <Loader2 size={14} className="animate-spin" /> Loading…
          </p>
        ) : queue.isError ? (
          <p className="text-bad">Could not load proposals: {(queue.error as Error).message}</p>
        ) : queue.data.length === 0 ? (
          <p className="text-faint">Nothing to review.</p>
        ) : (
          <ul className="space-y-2">
            {queue.data.map((item) => (
              <EvidenceItem key={item.id} item={item} onDone={() => void refresh()} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function EvidenceItem({ item, onDone }: { item: QueuedEvidence; onDone: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState(item.text);
  const decide = useMutation({
    mutationFn: (accept: boolean) => api.decideEvidence(item.id, accept, accept && text !== item.text ? text : undefined),
    onSuccess: (_, accept) => {
      if (accept) void qc.invalidateQueries({ queryKey: ["state"] });
      onDone();
    },
  });
  return (
    <li className="space-y-1 rounded-md border border-border p-2">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="chip">{KIND_LABEL[item.kind]}</span>
        {item.attach_to && <span className="text-faint">in role {item.attach_to}</span>}
        <span style={{ color: CONFIDENCE_COLOR[item.confidence] }}>{item.confidence} confidence</span>
        <span className="ml-auto text-faint">{item.source}</span>
      </div>
      <AutoText value={text} onChange={setText} />
      <p className="text-[11px] italic text-faint">“{item.quote}”</p>
      {decide.error && <p className="text-bad">{(decide.error as Error).message}</p>}
      <div className="flex justify-end gap-1">
        <button className="btn-ghost py-1 text-[12px]" disabled={decide.isPending} onClick={() => decide.mutate(false)}>
          <X size={12} /> Reject
        </button>
        <button className="btn-primary py-1 text-[12px]" disabled={decide.isPending || !text.trim()} onClick={() => decide.mutate(true)}>
          <Check size={12} /> Add to CV
        </button>
      </div>
    </li>
  );
}
