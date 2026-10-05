import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Download, Eraser, FileUp, Loader2, Search, Sparkles, Square, Trash2, UserRound } from "lucide-react";
import { useEffect, useState, type DragEvent } from "react";
import { api } from "../lib/api";
import type { AppState, CVAsset } from "../lib/types";
import { useSearchRunner } from "../lib/useSearchRunner";
import { EMPTY_QUERY, useSearch } from "../stores/searchStore";
import { SourcePicker } from "./SourcePicker";
import { ProfileDetails } from "./ProfileDetails";
import { SummaryDialog } from "./SummaryDialog";
import { SearchHistory } from "./SearchHistory";
import { TrackerPanel } from "./TrackerPanel";
import { ChipInput, Field, Modal, Toggle } from "./ui";

const DISTANCES = [5, 10, 25, 50, 100];
const POSTED_WINDOWS: { days: number | null; label: string }[] = [
  { days: null, label: "Any time" },
  { days: 1, label: "Last 24 hours" },
  { days: 3, label: "Last 3 days" },
  { days: 7, label: "Last week" },
  { days: 14, label: "Last 2 weeks" },
  { days: 30, label: "Last month" },
];
// Countries the job boards can search (Adzuna's list; Reed and CV-Library are UK-only).
const COUNTRIES = [
  "United Kingdom", "United States", "Canada", "Australia", "New Zealand", "Germany", "France",
  "Netherlands", "Belgium", "Switzerland", "Austria", "Spain", "Italy", "Poland", "Singapore", "India",
  "South Africa", "Brazil", "Mexico",
];
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

export function Sidebar({ state, onShowSummary }: { state: AppState | undefined; onShowSummary: (key?: string) => void }) {
  const s = useSearch();
  const q = s.query;
  const qc = useQueryClient();
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadedName, setUploadedName] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [expandedProfileKey, setExpandedProfileKey] = useState<string | null>(null);
  const [editingProfile, setEditingProfile] = useState(false);
  const [gmailWaiting, setGmailWaiting] = useState(false);
  const gmail = useQuery({
    queryKey: ["gmail-status"],
    queryFn: api.gmailStatus,
    // While Google sign-in runs in its own tab, poll so this tab picks up the connection.
    refetchInterval: gmailWaiting ? 2_000 : false,
    refetchOnWindowFocus: true,
  });
  const gmailConnected = !!gmail.data?.connected;
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: api.profiles, enabled: !!state?.cv_files.selected });
  useEffect(() => { setExpandedProfileKey(null); setEditingProfile(false); }, [state?.cv_files.selected]);
  useEffect(() => {
    if (!gmailWaiting || !gmailConnected) return;
    setGmailWaiting(false);
    void qc.invalidateQueries({ queryKey: ["state"] }); // Gmail now appears as a source
  }, [gmailWaiting, gmailConnected, qc]);
  const connectGmail = () => {
    window.open("/api/gmail/connect", "_blank");
    setGmailWaiting(true);
  };

  const cacheSelection = (asset: CVAsset) => {
    qc.setQueryData<AppState>(["state"], (current) => {
      if (!current) return current;
      const available = current.cv_files.available.some((item) => item.id === asset.id)
        ? current.cv_files.available.map((item) => (item.id === asset.id ? asset : item))
        : [...current.cv_files.available, asset];
      return {
        ...current,
        cv: null,
        cv_files: {
          selected: asset.id,
          available: available.map((item) => ({ ...item, selected: item.id === asset.id })),
        },
      };
    });
  };

  const upload = useMutation({
    mutationFn: (f: File) => api.uploadCV(f),
    onSuccess: (asset) => {
      setUploadError(null);
      setUploadedName(asset.filename);
      s.set({ useCv: true, profileKey: null });
      cacheSelection(asset);
      void qc.invalidateQueries({ queryKey: ["state"] });
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
    onError: (e: Error) => setUploadError(e.message),
  });

  const select = useMutation({
    mutationFn: api.selectCV,
    onSuccess: (asset) => {
      setUploadError(null);
      s.set({ useCv: true, summary: null, outcome: null, profileKey: null });
      cacheSelection(asset);
      void qc.invalidateQueries({ queryKey: ["state"] });
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
    onError: (e: Error) => setUploadError(e.message),
  });
  const deleteCV = useMutation({
    mutationFn: (item: CVAsset) => api.deleteCV(item.id),
    onSuccess: (_, item) => {
      if (item.id === state?.cv_files.selected) {
        s.set({ profileKey: null, summary: null, outcome: null });
        setExpandedProfileKey(null);
      }
      void qc.invalidateQueries({ queryKey: ["state"] });
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
  const deleteProfile = useMutation({
    mutationFn: (key: string) => api.deleteProfile(key),
    onSuccess: (_, key) => {
      if (s.profileKey === key) s.set({ profileKey: null, summary: null });
      if (expandedProfileKey === key) setExpandedProfileKey(null);
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
  const generalCV = useMutation({
    mutationFn: api.exportGeneralCV,
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["state"] }); },
  });
  const buildProfile = useMutation({
    mutationFn: () => api.profileSummary(EMPTY_QUERY, true),
    onSuccess: (result) => {
      s.set({ useCv: true, summary: { summary: result.summary, fromMemory: result.from_memory }, profileKey: result.key });
      void qc.invalidateQueries({ queryKey: ["profiles"] });
      void qc.invalidateQueries({ queryKey: ["state"] });
      onShowSummary(result.key);
    },
  });

  const submitCV = (file: File) => {
    setUploadedName(null);
    const error = cvFileError(file);
    if (error) {
      setUploadError(error);
      return;
    }
    setUploadError(null);
    upload.mutate(file);
  };

  const dropCV = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    if (!upload.isPending && !buildProfile.isPending && event.dataTransfer.files[0]) submitCV(event.dataTransfer.files[0]);
  };

  const runner = useSearchRunner();

  const cv = state?.cv;
  const cvFiles = state?.cv_files.available ?? [];
  const selectedCv = cvFiles.find((item) => item.id === state?.cv_files.selected);
  const hasCv = !!selectedCv;
  const expandedProfile = profiles.data?.profiles.find((record) => record.key === expandedProfileKey);
  return (
    <aside className="flex h-full min-w-0 flex-col gap-3 overflow-x-hidden overflow-y-auto p-3 scroll-thin">
      {/* Profile */}
      <details className="card group p-3" open>
        <summary className="label flex cursor-pointer list-none items-center justify-between [&::-webkit-details-marker]:hidden">
          <span className="flex items-center gap-1"><ChevronRight size={13} className="group-open:rotate-90" /> Profile</span>
        </summary>
        <div className="mt-3 space-y-3">
        <div className="flex items-center justify-end">
          {hasCv && (
            <button className="flex items-center gap-1 text-[11px] text-accent hover:underline" onClick={() => onShowSummary()}>
              <Sparkles size={12} /> Profiles
            </button>
          )}
        </div>
        {selectedCv ? (
          <div className="flex items-start gap-2">
            <UserRound size={28} className="mt-0.5 shrink-0 rounded-full bg-surface p-1 text-muted" />
            <div className="min-w-0">
              <p className="truncate font-semibold">{selectedCv.filename}</p>
              <p className="truncate text-[12px] text-muted">
                {cv?.headline ?? `${selectedCv.kind} CV · ready when requested`}
              </p>
              {cv && <p className="text-[11px] text-faint">{cv.roles} roles · {cv.skills} skills</p>}
            </div>
          </div>
        ) : (
          <p className="text-[12px] text-muted">No CV selected. Upload one or choose from the library.</p>
        )}
        <label
          className={`flex min-h-12 flex-col items-center justify-center rounded-md border border-dashed px-2 py-1.5 text-center transition-colors ${
            dragging ? "border-accent bg-accent-bg" : "border-border bg-surface hover:border-accent"
          } ${upload.isPending || buildProfile.isPending ? "cursor-wait opacity-70" : "cursor-pointer"}`}
          onDragEnter={(event) => {
            event.preventDefault();
            if (!upload.isPending && !buildProfile.isPending) setDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
          }}
          onDrop={dropCV}
        >
          <input
            type="file"
            accept=".pdf,.docx,.md,.txt"
            className="sr-only"
            disabled={upload.isPending || buildProfile.isPending}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) submitCV(file);
              event.target.value = "";
            }}
          />
          <span className="flex items-center gap-1.5 text-[11px] font-medium text-fg">
            {upload.isPending ? <Loader2 size={14} className="animate-spin text-accent" /> : <FileUp size={14} className="text-accent" />}
            {upload.isPending ? "Saving CV…" : "Drop a CV or browse files"}
          </span>
          <span className="text-[10px] text-faint">PDF, DOCX, MD, TXT · up to 10 MB</span>
        </label>
        {uploadedName && <p className="text-[11px] text-good">Saved {uploadedName}</p>}
        {uploadError && <p className="text-[12px] text-bad">{uploadError}</p>}
        {cvFiles.length > 0 && (
          <div className="space-y-1">
            <span className="label">Available CVs · {cvFiles.length}</span>
            <ul className="max-h-40 space-y-1 overflow-y-auto pr-1 scroll-thin" aria-label="Available CVs">
              {cvFiles.map((item) => (
                <li key={item.id} className="flex items-stretch gap-1">
                  <button
                    type="button"
                    aria-current={item.id === state?.cv_files.selected ? "true" : undefined}
                    title={item.filename}
                    disabled={select.isPending || deleteCV.isPending || buildProfile.isPending}
                    onClick={() => { if (item.id !== state?.cv_files.selected) select.mutate(item.id); }}
                    className={`block min-w-0 flex-1 rounded-md border px-2 py-1 text-left text-[11px] disabled:opacity-50 ${
                      item.id === state?.cv_files.selected ? "border-accent bg-accent-bg" : "border-border bg-surface hover:border-accent"
                    }`}
                  >
                    <span className="block truncate font-medium text-fg">{item.filename}</span>
                    <span className="text-faint">{item.kind} CV</span>
                  </button>
                  <button
                    type="button"
                    className="btn-ghost shrink-0 px-2 text-bad disabled:opacity-50"
                    aria-label={`Delete ${item.filename}`}
                    title={`Delete ${item.filename} and its stored profiles`}
                    disabled={deleteCV.isPending || select.isPending || buildProfile.isPending}
                    onClick={() => {
                      if (window.confirm(`Delete "${item.filename}" and its stored profiles? Saved applications and cover letters remain.`))
                        deleteCV.mutate(item);
                    }}
                  >
                    <Trash2 size={13} />
                  </button>
                </li>
              ))}
            </ul>
            {deleteCV.error && <p className="text-[11px] text-bad">{deleteCV.error.message}</p>}
          </div>
        )}
        {hasCv && (
          <div className="space-y-1">
            <span className="label">Available profiles · {profiles.data?.profiles.length ?? 0}</span>
            <button
              type="button"
              className="btn-ghost w-full justify-center py-1 text-[11px]"
              disabled={buildProfile.isPending || select.isPending || deleteCV.isPending}
              title="Build a general profile from this CV, or open its saved profile for editing"
              onClick={() => buildProfile.mutate()}
            >
              {buildProfile.isPending ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
              {buildProfile.isPending ? "Building profile…" : profiles.data?.profiles.some((record) => record.role_family === "any") ? "Edit general profile" : "Build profile from selected CV"}
            </button>
            {buildProfile.error && <p className="text-[11px] text-bad">{buildProfile.error.message}</p>}
            {profiles.isPending ? (
              <p className="text-[11px] text-faint">Loading profiles…</p>
            ) : profiles.isError ? (
              <p className="text-[11px] text-bad">Could not load profiles.</p>
            ) : !profiles.data?.profiles.length ? (
              <p className="text-[11px] text-muted">Build a profile to review and edit it before searching.</p>
            ) : (
              <ul className="max-h-40 space-y-1 overflow-y-auto pr-1 scroll-thin" aria-label="Available profiles">
                {profiles.data.profiles.map((record) => (
                  <li key={record.key} className={`flex items-center rounded-md border ${
                    expandedProfileKey === record.key ? "border-accent bg-accent-bg" : "border-border bg-surface"
                  }`}>
                    <button
                      type="button"
                      title={record.summary.headline}
                      onClick={() => setExpandedProfileKey(expandedProfileKey === record.key ? null : record.key)}
                      className="flex min-w-0 flex-1 items-center gap-1 px-2 py-1 text-left text-[11px]"
                    >
                      <ChevronRight size={12} className="shrink-0" />
                      <span className="min-w-0">
                        <span className="block truncate font-medium text-fg">{record.role_family === "any" ? "General profile" : record.role_family}</span>
                        <span className="block truncate text-faint">{record.summary.headline}</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      aria-label={`${s.profileKey === record.key ? "Unpin" : "Pin"} ${record.role_family === "any" ? "General profile" : record.role_family} for searches`}
                      aria-pressed={s.profileKey === record.key}
                      title={s.profileKey === record.key ? "Unpin profile" : "Pin for searches"}
                      onClick={() => s.set({ profileKey: s.profileKey === record.key ? null : record.key })}
                      className="shrink-0 px-2 text-[10px] text-accent hover:underline"
                    >
                      {s.profileKey === record.key ? "Pinned" : "Pin"}
                    </button>
                    <button
                      type="button"
                      className="shrink-0 px-2 text-bad disabled:opacity-50"
                      aria-label={`Delete ${record.role_family === "any" ? "General profile" : record.role_family} profile`}
                      title="Delete profile"
                      disabled={deleteProfile.isPending}
                      onClick={() => {
                        const name = record.role_family === "any" ? "General profile" : record.role_family;
                        if (window.confirm(`Delete the "${name}" profile? It will be rebuilt on the next matching search.`))
                          deleteProfile.mutate(record.key);
                      }}
                    >
                      <Trash2 size={13} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {deleteProfile.error && <p className="text-[11px] text-bad">{deleteProfile.error.message}</p>}
          </div>
        )}
        {hasCv && (
          <div className="space-y-1">
            <button className="btn-ghost py-1 text-[11px]" disabled={generalCV.isPending} onClick={() => generalCV.mutate()}>
              {generalCV.isPending ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
              Export CV
            </button>
            <p className="text-[10px] text-faint">Includes every role in the selected CV. Review parsed facts and edit the Word file before applying.</p>
            {generalCV.data && <a className="block text-[11px] text-accent hover:underline" href={generalCV.data.download_url}>Download CV · {generalCV.data.roles} roles</a>}
            {generalCV.isError && <p className="text-[11px] text-bad">{(generalCV.error as Error).message}</p>}
          </div>
        )}
        <Toggle checked={s.useCv && hasCv} onChange={(v) => s.set({ useCv: v })} label="Match against my CV" />
        </div>
      </details>

      {/* Filters */}
      <details className="card group min-w-0 p-3" open>
        <summary className="label flex cursor-pointer list-none items-center gap-1 [&::-webkit-details-marker]:hidden"><ChevronRight size={13} className="group-open:rotate-90" /> Search</summary>
        <div className="mt-3 space-y-3">
        <Field label="Sources" hint="Switch a category on or off; click its name to choose its sources.">
          <SourcePicker state={state} hasCv={hasCv} />
        </Field>
        <Field label={`Match threshold · ${s.threshold}`}>
          <input
            type="range"
            min={40}
            max={95}
            step={5}
            value={s.threshold}
            onChange={(e) => s.set({ threshold: Number(e.target.value) })}
            className="block w-full max-w-full accent-[var(--accent)]"
          />
        </Field>
        <Toggle
          checked={s.smart}
          onChange={(v) => s.set({ smart: v })}
          label="Smart match (AI screening)"
        />
        <Toggle
          checked={s.widen}
          disabled={!s.useCv || !s.smart}
          onChange={(v) => s.set({ widen: v })}
          label="Widen to adjacent roles"
          title="With empty titles, also search the adjacent role families your profile proposes (each backed by CV evidence). Areas you asked for in your career intent are always searched."
        />
        <Field label="Country" hint={q.country ? "Cities below are searched within this country" : undefined}>
          <select
            className="input"
            value={q.country ?? ""}
            onChange={(e) => s.setQuery({ country: e.target.value || null })}
          >
            <option value="">Any country</option>
            {COUNTRIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </Field>
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,88px)] gap-2">
          <Field label="Cities">
            <ChipInput
              value={q.locations}
              onChange={(locations) => s.setQuery({ locations })}
              placeholder={q.country ? "Optional: city ⏎" : "City ⏎"}
            />
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
        <Field label="Date posted">
          <select
            className="input"
            value={q.posted_within_days ?? ""}
            onChange={(e) => s.setQuery({ posted_within_days: e.target.value ? Number(e.target.value) : null })}
            title="Postings without a date (e.g. saved pages) are always kept"
          >
            {POSTED_WINDOWS.map((w) => (
              <option key={w.label} value={w.days ?? ""}>
                {w.label}
              </option>
            ))}
          </select>
        </Field>
        <div className="pt-1">
          {s.loading ? (
            <button className="btn-ghost w-full min-w-0 justify-center px-2 text-bad" disabled={!s.runId} onClick={runner.stop} title="Stop the search; results so far are kept">
              <Square size={14} className="shrink-0" /> <span className="truncate">Stop</span>
            </button>
          ) : (
            <button
              className="btn-primary w-full min-w-0 justify-center px-2"
              title="Search the sources, then the job_matcher screens the shortlist against your profile"
              onClick={() => void runner.run()}
            >
              <Search size={14} /> <span className="truncate">Search</span>
            </button>
          )}
        </div>
        <button
          type="button"
          className="flex w-full items-center justify-center gap-1 text-[11px] text-faint hover:text-fg"
          disabled={s.loading}
          title="Reset the filters and clear the results panel (history is kept)"
          onClick={() => s.set({ query: EMPTY_QUERY, outcome: null, log: [], error: null, openedFrom: null })}
        >
          <Eraser size={12} /> Clear filters and results
        </button>
        </div>
      </details>
      <SearchHistory />
      <TrackerPanel />
      <details className="card group p-3" open>
        <summary className="label flex cursor-pointer list-none items-center gap-1 [&::-webkit-details-marker]:hidden"><ChevronRight size={13} className="group-open:rotate-90" /> Job alerts</summary>
        <div className="mt-2 space-y-2">
        <p className="text-[11px] text-muted">
          The Search button above includes Gmail alert jobs in the selected date window. Jobs judged before
          keep their verdict at no extra model cost.
        </p>
        {gmail.isPending && <p className="text-[11px] text-faint">Checking Gmail connection…</p>}
        {gmail.isError && <p className="text-[11px] text-bad">Could not check Gmail connection.</p>}
        {gmail.data && !gmail.data.configured && (
          <p className="text-[11px] text-faint">Set Gmail OAuth credentials and the alert account in .env first.</p>
        )}
        {gmail.data?.configured && !gmail.data.connected && (
          <button type="button" className="btn-ghost inline-flex text-[12px]" onClick={connectGmail}>
            Connect Gmail alerts
          </button>
        )}
        {gmailWaiting && !gmailConnected && (
          <p className="flex items-center gap-1 text-[11px] text-muted">
            <Loader2 size={12} className="animate-spin" /> Finish the Google sign-in in the new tab…
            <button type="button" className="text-accent hover:underline" onClick={() => setGmailWaiting(false)}>
              Cancel
            </button>
          </p>
        )}
        {gmail.data?.connected && (
          <div className="flex items-center justify-between gap-2 text-[11px]">
            <span className="truncate text-good">Connected: {gmail.data.account}</span>
            <button type="button" className="shrink-0 text-accent hover:underline" onClick={connectGmail}>
              Reconnect
            </button>
          </div>
        )}
        </div>
      </details>
      <Modal
        open={!!expandedProfile}
        onClose={() => { setExpandedProfileKey(null); setEditingProfile(false); }}
        title={expandedProfile?.role_family === "any" ? "General profile" : expandedProfile?.role_family ?? "Profile"}
        resizable
        headerAction={!editingProfile && <button type="button" className="text-[12px] text-accent hover:underline" onClick={() => setEditingProfile(true)}>Edit profile</button>}
      >
        {editingProfile && expandedProfileKey ? (
          <SummaryDialog open onClose={() => setEditingProfile(false)} initialProfileKey={expandedProfileKey} embedded />
        ) : (
          <>
            <p className="mb-2 text-[11px] text-faint">Drag the lower-right corner to resize this panel.</p>
            {expandedProfile && <ProfileDetails record={expandedProfile} />}
          </>
        )}
      </Modal>
    </aside>
  );
}
