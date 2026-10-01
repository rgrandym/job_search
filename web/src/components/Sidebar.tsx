import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileUp, Loader2, Search, Sparkles, UserRound } from "lucide-react";
import { useRef, useState } from "react";
import { api } from "../lib/api";
import type { AppState } from "../lib/types";
import { useChat } from "../stores/chatStore";
import { useSearch } from "../stores/searchStore";
import { ChipInput, Field, Segmented, Toggle } from "./ui";

const DISTANCES = [5, 10, 25, 50, 100];

export function Sidebar({ state, onShowSummary }: { state: AppState | undefined; onShowSummary: () => void }) {
  const s = useSearch();
  const q = s.query;
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const chat = useChat();

  const upload = useMutation({
    mutationFn: (f: File) => api.uploadCV(f),
    onSuccess: () => {
      setUploadError(null);
      qc.invalidateQueries({ queryKey: ["state"] });
    },
    onError: (e: Error) => setUploadError(e.message),
  });

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
          onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])}
        />
        <button
          className="btn-ghost w-full"
          disabled={upload.isPending || !state?.llm.ready}
          title={state?.llm.ready ? "" : "Configure an LLM in Settings first"}
          onClick={() => fileRef.current?.click()}
        >
          {upload.isPending ? <Loader2 size={14} className="animate-spin" /> : <FileUp size={14} />}
          {upload.isPending ? "Reading CV…" : cv ? "Replace CV" : "Upload CV (PDF, DOCX, MD)"}
        </button>
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
