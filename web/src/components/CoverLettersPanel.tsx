import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { CoverLetterView } from "../lib/types";
import { Empty } from "./ui";

function LetterEditor({ letter, onDelete, deleting }: {
  letter: CoverLetterView;
  onDelete: (id: string) => void;
  deleting: boolean;
}) {
  const qc = useQueryClient();
  const [greeting, setGreeting] = useState(letter.greeting);
  const [paragraphs, setParagraphs] = useState(letter.paragraphs);
  const [closing, setClosing] = useState(letter.closing);
  const [saved, setSaved] = useState<CoverLetterView | null>(null);
  const current = saved ?? letter;
  const changed = greeting !== current.greeting || closing !== current.closing ||
    paragraphs.some((text, index) => text !== current.paragraphs[index]);
  const save = useMutation({
    mutationFn: () => api.editCoverLetter(current.id, { greeting, paragraphs, closing }),
    onSuccess: (updated) => {
      setSaved(updated);
      void qc.invalidateQueries({ queryKey: ["cover-letters"] });
    },
  });
  const setParagraph = (index: number, text: string) => {
    setParagraphs((old) => old.map((value, position) => position === index ? text : value));
  };
  return (
    <div className="min-w-0 space-y-3 rounded-md border border-border bg-panel p-4 text-[12px]">
      <div>
        <h3 className="font-semibold text-fg">{current.title && current.company ? `${current.title} · ${current.company}` : current.filename}</h3>
        <p className="text-muted">{current.candidate_name} · saved {new Date(current.updated_at).toLocaleString()}</p>
      </div>
      <label className="block font-medium">Greeting
        <input className="input mt-1 w-full" value={greeting} onChange={(event) => setGreeting(event.target.value)} />
      </label>
      {paragraphs.map((text, index) => (
        <label className="block font-medium" key={index}>Paragraph {index + 1}
          <textarea className="input mt-1 w-full font-normal" rows={4} value={text}
            onChange={(event) => setParagraph(index, event.target.value)} />
        </label>
      ))}
      <label className="block font-medium">Closing
        <input className="input mt-1 w-full" value={closing} onChange={(event) => setClosing(event.target.value)} />
      </label>
      <p className="text-muted">{current.candidate_name}</p>
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-primary py-1 text-[12px]" disabled={!changed || save.isPending || !greeting.trim() || !closing.trim() || paragraphs.some((text) => !text.trim())}
          onClick={() => save.mutate()}>
          {save.isPending && <Loader2 size={12} className="animate-spin" />} Save edits
        </button>
        <a className="btn-ghost py-1 text-[12px]" href={current.docx_url}><Download size={12} /> Export Word</a>
        <a className="btn-ghost py-1 text-[12px]" href={current.txt_url}><Download size={12} /> Export text</a>
        <button className="btn-ghost py-1 text-[12px] text-bad" type="button" disabled={deleting || save.isPending}
          onClick={() => onDelete(current.id)}>
          {deleting ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />} Delete letter
        </button>
      </div>
      {changed && <p className="text-warn">Save edits before exporting the updated text.</p>}
      {save.error && <p className="text-bad">{(save.error as Error).message}</p>}
    </div>
  );
}

export function CoverLettersPanel() {
  const qc = useQueryClient();
  const letters = useQuery({ queryKey: ["cover-letters"], queryFn: api.coverLetters });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = letters.data?.find((item) => item.id === selectedId) ?? letters.data?.[0];
  const remove = useMutation({
    mutationFn: api.deleteCoverLetter,
    onSuccess: () => {
      setSelectedId(null);
      void qc.invalidateQueries({ queryKey: ["cover-letters"] });
    },
  });
  const removeAll = useMutation({
    mutationFn: api.deleteAllCoverLetters,
    onSuccess: () => {
      setSelectedId(null);
      void qc.invalidateQueries({ queryKey: ["cover-letters"] });
    },
  });
  const deleteOne = (id: string) => {
    if (window.confirm("Delete this cover letter and its saved exports?")) remove.mutate(id);
  };
  const deleteAll = () => {
    if (window.confirm(`Delete all ${letters.data?.length ?? 0} cover letters and their saved exports?`)) removeAll.mutate();
  };
  return (
    <div className="h-full overflow-y-auto p-4 scroll-thin">
      {letters.isPending ? <Empty title="Loading cover letters…" /> :
        letters.error ? <Empty title="Could not load cover letters">{(letters.error as Error).message}</Empty> :
          !letters.data?.length ? <Empty title="No cover letters yet">Write one from a job card to see it here.</Empty> : (
            <div className="grid gap-4 lg:grid-cols-[minmax(12rem,16rem)_minmax(0,1fr)]">
              <section className="space-y-1" aria-label="Available cover letters">
                <div className="flex items-center justify-between gap-2 pb-1">
                  <h2 className="label">Cover letters · {letters.data.length}</h2>
                  <button className="btn-ghost py-1 text-[12px] text-bad" type="button"
                    disabled={remove.isPending || removeAll.isPending} onClick={deleteAll}>
                    <Trash2 size={12} /> Delete all
                  </button>
                </div>
                {letters.data.map((item) => (
                  <div key={item.id} className={`flex items-start rounded-md border ${selected?.id === item.id ? "border-accent bg-accent-bg" : "border-border bg-panel hover:border-accent"}`}>
                    <button type="button" onClick={() => setSelectedId(item.id)}
                      aria-current={selected?.id === item.id ? "true" : undefined}
                      className="min-w-0 flex-1 p-2 text-left text-[12px]">
                      <span className="block truncate font-medium">{item.title || item.filename}</span>
                      {item.company && <span className="block truncate text-muted">{item.company}</span>}
                      <span className="block text-faint">{new Date(item.updated_at).toLocaleDateString()}</span>
                    </button>
                    <button type="button" className="shrink-0 p-2 text-bad hover:opacity-70"
                      aria-label={`Delete cover letter ${item.title || item.filename}`}
                      title="Delete this cover letter" disabled={remove.isPending || removeAll.isPending}
                      onClick={() => deleteOne(item.id)}>
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </section>
              {selected && <LetterEditor key={selected.id + selected.updated_at} letter={selected}
                onDelete={deleteOne} deleting={remove.isPending || removeAll.isPending} />}
              {(remove.error || removeAll.error) && <p className="text-bad">
                {((remove.error || removeAll.error) as Error).message}
              </p>}
            </div>
          )}
    </div>
  );
}
