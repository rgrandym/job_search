import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileUp, Loader2, Search, Sparkles, UserRound } from "lucide-react";
import { useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { api } from "../lib/api";
import type { AppState } from "../lib/types";
import { useChat } from "../stores/chatStore";
import { useSearch } from "../stores/searchStore";
import { ChipInput, Field, Segmented, Toggle } from "./ui";

const DISTANCES = [5, 10, 25, 50, 100];
const CV_EXTENSIONS = [".pdf", ".docx", ".md", ".txt"];
const MAX_CV_BYTES = 10 * 1024 * 1024;

function cvFileError(file: File): string | null {
  const lower = file.name.toLowerCase();
  if (!CV_EXTENSIONS.some((extension) => lower.endsWith(extension))) {
    return "Choose a PDF, DOCX, Markdown, or text CV.";
  }
  if (!file.size) return "The selected CV file is empty.";
  if (file.size > MAX_CV_BYTES) return "The CV must be smaller than 10 MB.";
  return null;
}

export function Sidebar({ state, onShowSummary }: { state: AppState | undefined; onShowSummary: () => void }) {
  const s = useSearch();
  const q = s.query;
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadedName, setUploadedName] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const chat = useChat();

  const upload = useMutation({
    mutationFn: (f: File) => api.uploadCV(f),
    onSuccess: async (_, file) => {
      setUploadError(null);
      setUploadedName(file.name);
      await qc.invalidateQueries({ queryKey: ["state"] });
    },
    onError: (e: Error) => setUploadError(e.message),
  });

  const submitCV = (file: File) => {
    setUploadedName(null);
    if (!state?.llm.ready) {
      setUploadError("Connect an LLM in Settings before importing a CV.");
      return;
    }
    const error = cvFileError(file);
    if (error) {
      setUploadError(error);
      return;
    }
    setUploadError(null);
    upload.mutate(file);
  };

  const chooseCV = () => {
    if (upload.isPending) return;
    if (!state?.llm.ready) {
      setUploadError("Connect an LLM in Settings before importing a CV.");
      return;
    }
    if (fileRef.current) fileRef.current.value = "";
    fileRef.current?.click();
  };

  const dropCV = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (!upload.isPending && event.dataTransfer.files[0]) submitCV(event.dataTransfer.files[0]);
  };

  const openCVMenu = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      chooseCV();
    }
  };

  const runSearch = async () => {
    s.set({ loading: true, error: null, progress: s.smart ? "Searching and screening…" : "Searching…" });
    try {
      const outcome = await api.search({ query: q, use_cv: s.useCv, smart: s.smart, threshold: s.threshold });
      s.set({ outcome });
      if (outcome.report.summary)
        s.set({ summary: { summary: outcome.report.summary, fromMemory: !!outcome.summary_from_memory } });
    } catch (e) {
      s.set({ error: (e as Error).message });
    } finally {
      s.set({ loading: false, progress: null });
    }
  };

  const askAgent = () => {
    const what = q.titles.length ? q.titles.join(" / ") : "roles that fit my profile";
    chat.send(`Find jobs that truly match me: ${what}.`, q, s.useCv);
  };

  const cv = state?.cv;
  const sources = state ? [...state.sources.available, ...Object.keys(state.sources.skipped)] : [];

  return (
    <aside className="flex h-full flex-col gap-3 overflow-y-auto p-3 scroll-thin">
      {/* Profile */}
      <section className="card space-y-3 p-3">
        <div className="flex items-center justify-between">
          <span className="label">Profile</span>
          {cv && (
            <button className="flex items-center gap-1 text-[11px] text-accent hover:underline" onClick={onShowSummary}>
              <Sparkles size={12} /> Summary
            </button>
          )}
        </div>
        {cv ? (
          <div className="flex items-start gap-2">
            <UserRound size={28} className="mt-0.5 shrink-0 rounded-full bg-surface p-1 text-muted" />
            <div className="min-w-0">
              <p className="truncate font-semibold">{cv.name}</p>
              <p className="truncate text-[12px] text-muted">{cv.headline ?? "—"}</p>
              <p className="text-[11px] text-faint">
                {cv.roles} roles · {cv.skills} skills
              </p>
            </div>
          </div>
        ) : (
          <p className="text-[12px] text-muted">No CV yet. Upload one to match against your profile.</p>
        )}
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,.md,.txt"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) submitCV(file);
            event.target.value = "";
          }}
        />
        <div
          role="button"
          tabIndex={upload.isPending ? -1 : 0}
          aria-disabled={upload.isPending}
          className={`flex min-h-24 flex-col items-center justify-center gap-1 rounded-md border border-dashed px-3 py-4 text-center transition-colors ${
            dragging ? "border-accent bg-accent-bg" : "border-border bg-surface hover:border-accent"
          } ${upload.isPending ? "cursor-wait opacity-70" : "cursor-pointer"}`}
          onClick={chooseCV}
          onKeyDown={openCVMenu}
          onDragEnter={(event) => {
            event.preventDefault();
            if (!upload.isPending) setDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
          }}
          onDrop={dropCV}
        >
          {upload.isPending ? <Loader2 size={20} className="animate-spin text-accent" /> : <FileUp size={20} className="text-accent" />}
          <p className="text-[12px] font-medium text-fg">
            {upload.isPending ? "Building your profile…" : cv ? "Drop a CV to replace it" : "Drop your CV here"}
          </p>
          {!upload.isPending && <p className="text-[11px] text-muted">or choose a file from your computer</p>}
          <p className="text-[10px] text-faint">PDF, DOCX, MD or TXT · up to 10 MB</p>
        </div>
        {uploadedName && <p className="text-[11px] text-good">Imported {uploadedName}</p>}
        {uploadError && <p className="text-[12px] text-bad">{uploadError}</p>}
        <Toggle checked={s.useCv && !!cv} onChange={(v) => s.set({ useCv: v })} label="Match against my CV" />
      </section>

      {/* Filters */}
      <section className="card space-y-3 p-3">
        <span className="label">Search</span>
        <Field label="Job titles">
          <ChipInput value={q.titles} onChange={(titles) => s.setQuery({ titles })} placeholder="e.g. ML Engineer ⏎" />
        </Field>
        <Field label="Keywords">
          <ChipInput value={q.keywords} onChange={(keywords) => s.setQuery({ keywords })} placeholder="e.g. PyTorch ⏎" />
        </Field>
        <div className="grid grid-cols-[1fr_88px] gap-2">
          <Field label="Location">
            <ChipInput value={q.locations} onChange={(locations) => s.setQuery({ locations })} placeholder="City ⏎" />
          </Field>
          <Field label="Radius">
            <select
              className="input"
              value={q.distance_miles ?? 25}
              onChange={(e) => s.setQuery({ distance_miles: Number(e.target.value) })}
            >
              {DISTANCES.map((d) => (
                <option key={d} value={d}>
                  {d} mi
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Salary (annual)">
          <div className="grid grid-cols-2 gap-2">
            <input
              className="input"
              type="number"
              min={0}
              step={5000}
              placeholder="Min"
              value={q.salary_min ?? ""}
              onChange={(e) => s.setQuery({ salary_min: e.target.value ? Number(e.target.value) : null })}
            />
            <input
              className="input"
              type="number"
              min={0}
              step={5000}
              placeholder="Max"
              value={q.salary_max ?? ""}
              onChange={(e) => s.setQuery({ salary_max: e.target.value ? Number(e.target.value) : null })}
            />
          </div>
        </Field>
        <Field label="Work arrangement">
          <Segmented
            options={[
              { value: "remote", label: "Remote" },
              { value: "hybrid", label: "Hybrid" },
              { value: "onsite", label: "On-site" },
            ]}
            value={q.work_arrangements}
            onChange={(work_arrangements) => s.setQuery({ work_arrangements })}
          />
        </Field>
        <Field label="Sources" hint="None selected = all configured sources">
          <div className="flex flex-wrap gap-1">
            {sources.map((name) => {
              const skipped = state?.sources.skipped[name];
              const on = q.sources.includes(name);
              return (
                <button
                  key={name}
                  disabled={!!skipped}
                  title={skipped ?? ""}
                  onClick={() =>
                    s.setQuery({ sources: on ? q.sources.filter((x) => x !== name) : [...q.sources, name] })
                  }
                  className={`chip border ${on ? "border-accent text-fg" : "border-transparent"} disabled:opacity-40`}
                >
                  {name}
                </button>
              );
            })}
          </div>
        </Field>
        <Field label={`Match threshold · ${s.threshold}`}>
          <input
            type="range"
            min={40}
            max={95}
            step={5}
            value={s.threshold}
            onChange={(e) => s.set({ threshold: Number(e.target.value) })}
            className="w-full accent-[var(--accent)]"
          />
        </Field>
        <Toggle
          checked={s.smart}
          onChange={(v) => s.set({ smart: v })}
          label="Smart match (AI screening)"
        />
        <div className="grid grid-cols-2 gap-2 pt-1">
          <button className="btn-primary" disabled={s.loading} onClick={runSearch}>
            {s.loading ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            Search
          </button>
          <button className="btn-ghost" disabled={chat.running || !state?.llm.ready} onClick={askAgent}>
            <Sparkles size={14} /> Ask agent
          </button>
        </div>
      </section>
    </aside>
  );
}
