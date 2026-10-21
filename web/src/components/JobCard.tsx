import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bookmark, CalendarDays, ChevronDown, ExternalLink, FileText, Loader2, Mail, Scale, Search, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import { isAdjacentPossibility } from "../lib/resultGroups";
import type { Rejection } from "../lib/api";
import type { FitDimensions, JobStatus, JobVerdict, MatchResult, OutcomeStage, UserLabel } from "../lib/types";
import { cn, money, scoreColor } from "../lib/utils";
import { useSearch } from "../stores/searchStore";
import { usePrefs, type TailorPrefs } from "../stores/prefsStore";
import { useFindJob } from "../stores/findJobStore";
import { CVVersionCompare } from "./CVVersionCompare";
import { TaskError, TaskProgress, useTaskId } from "./TaskProgress";
import { CVViewButton, LetterViewButton } from "./LetterDocument";
import { TailoredCVPanel } from "./TailoredCVPanel";
import { FitCheckDetails, FitCheckSummary, IfConfirmed, PositioningDetails, ReadabilitySummary, ScreeningSummary } from "./TailorInsights";

const METRICS = [
  ["title", "Title & level"],
  ["skills", "Skills"],
  ["experience", "Experience"],
  ["location", "Location"],
  ["semantic", "Context"],
] as const;

// Where a posting's link leads, by `JobPosting.source`.
const SOURCE_SITE: Record<string, string> = {
  reed: "Reed",
  cv_library: "CV-Library",
  adzuna: "Adzuna",
  linkedin_search: "LinkedIn",
  totaljobs: "Totaljobs",
  jobs_ac_uk: "jobs.ac.uk",
  nhs_jobs: "NHS Jobs",
  biotechnologyjobs: "Biotechnology Jobs",
  company: "company site",
  linkedin_alert: "LinkedIn",
  linkedin_saved: "LinkedIn",
  indeed_alert: "Indeed",
  indeed_saved: "Indeed",
  reed_alert: "Reed",
  cv_library_alert: "CV-Library",
};

const BAND_LABEL: Record<JobVerdict["band"], string> = {
  exceptional: "Exceptional match",
  very_strong: "Very strong match",
  strong: "Strong match",
  stretch: "Reasonable stretch",
  weak: "Weak match",
};
const PRIORITY_LABEL: Record<JobVerdict["priority"], string> = {
  apply_now: "Apply now",
  worth_applying: "Worth applying",
  consider: "Consider selectively",
  low: "Low priority",
};
// Same order and maxima as FIT_WEIGHTS in src/jobs/models.py
const DIMENSIONS: [keyof FitDimensions, string, number][] = [
  ["function", "Core function", 30],
  ["domain", "Domain", 20],
  ["seniority", "Seniority & scope", 20],
  ["leadership", "Leadership", 10],
  ["sector", "Sector", 10],
  ["practicality", "Location & pattern", 10],
];

/** One explanation list: a marker, a colour token and up to `max` items. */
function Lines({ items, mark, tone, max = 3 }: { items: string[]; mark: string; tone: string; max?: number }) {
  return (
    <>
      {items.slice(0, max).map((item) => (
        <p key={item} className="text-muted">
          <span className={tone}>{mark}</span> {item}
        </p>
      ))}
    </>
  );
}

function ScoreBadge({ value, label }: { value: number; label: string }) {
  return (
    <div className="flex flex-col items-center">
      <div
        className="flex h-11 w-11 items-center justify-center rounded-full border-2 text-[15px] font-bold"
        style={{ borderColor: scoreColor(value), color: scoreColor(value) }}
      >
        {Math.round(value)}
      </div>
      <span className="mt-0.5 text-[10px] text-faint">{label}</span>
    </div>
  );
}

function Bar({ label, value, text }: { label: string; value: number; text?: string }) {
  return (
    <div className="grid grid-cols-[112px_1fr_40px] items-center gap-2 text-[11px]">
      <span className="text-muted">{label}</span>
      <div className="h-1.5 overflow-hidden rounded-full bg-surface">
        <div className="h-full rounded-full" style={{ width: `${value * 100}%`, background: scoreColor(value * 100) }} />
      </div>
      <span className="text-right text-muted">{text ?? Math.round(value * 100)}</span>
    </div>
  );
}

/** Calendar days from today to an ISO date (negative: in the past). */
function daysUntil(iso: string): number {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return Math.round((new Date(y, m - 1, d).getTime() - new Date().setHours(0, 0, 0, 0)) / 86_400_000);
}

/** "Posted 28 Sep 2026 · 4 days ago" from an ISO date (local calendar days). */
function posted(iso: string): string {
  const days = -daysUntil(iso);
  const date = new Date(`${iso.slice(0, 10)}T00:00`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  const ago = days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
  return `Posted ${date} · ${ago}`;
}

const shortDate = (iso: string) => new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" });

function closes(iso: string): { text: string; soon: boolean } {
  const days = daysUntil(iso);
  const date = new Date(`${iso.slice(0, 10)}T00:00`).toLocaleDateString([], { day: "numeric", month: "short" });
  if (days < 0) return { text: `closed ${date}`, soon: true };
  if (days === 0) return { text: "closes today", soon: true };
  return { text: `closes ${date}${days <= 7 ? ` (${days} d)` : ""}`, soon: days <= 7 };
}

export const STAGES: [OutcomeStage, string][] = [
  ["screening", "Screening"],
  ["interview", "Interview"],
  ["final_round", "Final round"],
  ["offer", "Offer"],
  ["accepted", "Accepted"],
  ["rejected", "Rejected"],
  ["no_response", "No response"],
  ["withdrawn", "Withdrawn"],
];

const ALIGNMENT_CHIP = {
  aligned: ["Fits your direction", "text-good"],
  against: ["Against your intent", "text-warn"],
} as const;

const STATUS_CHIP: Record<JobStatus, [string, string, string]> = {
  new: ["New", "text-accent", "First time this job appears in a search"],
  open: ["Open", "text-muted", "Seen in an earlier search, not acted on yet"],
  applied: ["Applied", "text-good", "You applied for this role"],
  na: ["N/A", "text-faint", "You ruled this job out"],
};

/** Applied / N/A / Open, and the user's own note: kept across searches by the job tracker. */
function TrackingControls({ result }: { result: MatchResult }) {
  const { job, tracking } = result;
  const setTracking = useSearch((s) => s.setTracking);
  const qc = useQueryClient();
  const [note, setNote] = useState(tracking?.note ?? "");
  const [reason, setReason] = useState(tracking?.reason ?? "");
  const save = useMutation({
    meta: { syncLists: true },
    mutationFn: (v: { status?: "open" | "applied" | "na"; note?: string; reason?: string; stage?: OutcomeStage }) =>
      api.trackJob(job.id, v.status, v.note, { reason: v.reason, stage: v.stage }),
    onSuccess: (t) => {
      setTracking(job.id, t);
      void qc.invalidateQueries({ queryKey: ["tracker"] });
      void qc.invalidateQueries({ queryKey: ["saved"] });
    },
  });
  const current = tracking?.status === "applied" || tracking?.status === "na" ? tracking.status : "open";
  const saveNote = () => {
    if (note.trim() !== (tracking?.note ?? "")) save.mutate({ note: note.trim() });
  };
  const options: ["open" | "applied" | "na", string, string][] = [
    ["open", "Open", "Not acted on: keep showing it"],
    ["applied", "Applied", "Move this job to the Applied tab"],
    ["na", "N/A", "Not for me: later searches set it aside"],
  ];
  return (
    <div className="mt-2 space-y-1">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <div className="flex overflow-hidden rounded-md border border-border">
          {options.map(([value, label, title]) => (
            <button
              key={value}
              type="button"
              title={title}
              disabled={save.isPending}
              onClick={() => value !== current && save.mutate({ status: value })}
              className={cn(
                "px-2 py-0.5",
                value === current ? "bg-accent-bg text-fg" : "text-muted hover:text-fg",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <input
          className="input min-w-[140px] flex-1 py-0.5 text-[11px]"
          placeholder="Your note (kept across searches)"
          value={note}
          maxLength={200}
          onChange={(e) => setNote(e.target.value)}
          onBlur={saveNote}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        {save.isPending && <Loader2 size={12} className="animate-spin text-faint" />}
      </div>
      {(current === "na" || current === "applied") && (
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          {current === "applied" && (
            <select
              className="input w-auto py-0.5 text-[11px]"
              value={tracking?.stage ?? ""}
              disabled={save.isPending}
              onChange={(e) => e.target.value && save.mutate({ stage: e.target.value as OutcomeStage })}
              title="How the application went (used by the outcome review)"
            >
              <option value="">Outcome: no news yet</option>
              {STAGES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          )}
          <input
            className="input min-w-[160px] flex-1 py-0.5 text-[11px]"
            placeholder={current === "na" ? "Why not for you? (helps the outcome review)" : "Why it suits you (optional)"}
            value={reason}
            maxLength={200}
            onChange={(e) => setReason(e.target.value)}
            onBlur={() => reason.trim() !== (tracking?.reason ?? "") && save.mutate({ status: current, reason: reason.trim() })}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
        </div>
      )}
      {tracking?.applied_at && (
        <p className="text-[11px] text-faint">
          Applied {tracking.applied_at}
          {tracking.cv_file && ` · CV ${tracking.cv_file}`}
        </p>
      )}
      {tracking?.related && <p className="text-[11px] text-faint">{tracking.related}</p>}
      {save.error && <p className="text-[11px] text-bad">{(save.error as Error).message}</p>}
    </div>
  );
}

const LABELS: [UserLabel, string, string][] = [
  ["yes", "Would apply", "text-good"],
  ["maybe", "Maybe", "text-warn"],
  ["no", "No", "text-bad"],
];

/** Your own call on the job, kept to measure the models; it never changes a search. */
function LabelControls({ jobId }: { jobId: string }) {
  const qc = useQueryClient();
  const labels = useQuery({ queryKey: ["labels"], queryFn: api.labels });
  const current = labels.data?.by_job[jobId] ?? null;
  const save = useMutation({
    mutationFn: (label: UserLabel | null) => api.labelJob(jobId, label),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["labels"] }),
  });
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
      <span
        className="text-faint"
        title="Your call, judged from the posting itself rather than the AI score. Used only to measure how well the models match (sidebar: Your labels); it never changes a search. Your note on the job is kept with the label."
      >
        Your call
      </span>
      <div className="flex overflow-hidden rounded-md border border-border">
        {LABELS.map(([value, text, tone]) => (
          <button
            key={value}
            type="button"
            disabled={save.isPending || labels.isPending}
            title={value === current ? "Click again to remove your label" : undefined}
            onClick={() => save.mutate(value === current ? null : value)}
            className={cn("px-2 py-0.5", value === current ? cn("bg-accent-bg font-medium", tone) : "text-muted hover:text-fg")}
          >
            {text}
          </button>
        ))}
      </div>
      {save.isPending && <Loader2 size={12} className="animate-spin text-faint" />}
      {(save.error ?? labels.error) && <span className="text-bad">{((save.error ?? labels.error) as Error).message}</span>}
    </div>
  );
}

function Rejected({ items, what }: { items: Rejection[]; what: string }) {
  if (!items.length) return null;
  return (
    <details className="basis-full text-[11px] text-faint">
      <summary className="cursor-pointer">
        {items.length} {what} rejected by the no-fabrication guards
      </summary>
      {items.map((r) => (
        <p key={`${r.source_id}:${r.reason}`}>
          {r.source_id}: {r.reason}
        </p>
      ))}
    </details>
  );
}

/** For a posting whose requirements were not checked: fetch it again, or paste its text. */
/** The posting's text: a preview, and the whole of it on request (nothing is cut). */
function Description({ text }: { text: string }) {
  const [full, setFull] = useState(false);
  const long = text.length > 600 || text.split("\n").length > 6;
  return (
    <div className="pt-1 text-[12px] text-muted">
      <p className={cn("whitespace-pre-line", !full && long && "line-clamp-6")}>{text}</p>
      {long && (
        <button className="mt-1 text-[11px] text-accent hover:underline" onClick={() => setFull(!full)}>
          {full ? "Show less" : `Show full description (${text.split(/\s+/).length} words)`}
        </button>
      )}
    </div>
  );
}

const UNCHECKED = "Its requirements could not be read, so it was not checked against your profile. Fetch the full posting, or open it and paste the job description.";

/** Postings shorter than this are a snippet (the search's own `ENRICH_BELOW`). */
const THIN_POSTING = 600;

function UncheckedPosting({ onRecheck, onSearch, message = UNCHECKED, pending = false }: { onRecheck: (description?: string) => void; onSearch: () => void; message?: string; pending?: boolean }) {
  const [pasting, setPasting] = useState(false);
  const [text, setText] = useState("");
  return (
    <div className="space-y-1.5 rounded-md border border-border bg-surface p-2 text-[11px]">
      <p className="text-muted">{message}</p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-ghost py-1 text-[12px]" disabled={pending} onClick={() => onRecheck()}>
          {pending && <Loader2 size={12} className="animate-spin" />} Fetch full posting
        </button>
        <button type="button" className="btn-ghost py-1 text-[12px]" onClick={() => setPasting(!pasting)}>
          Paste description
        </button>
        <button
          type="button"
          className="btn-ghost py-1 text-[12px]"
          title="Open Find one job with this job's title, employer, link and ID filled in: it looks for a readable copy of the posting, also on the employer's site and job boards"
          onClick={onSearch}
        >
          <Search size={12} /> Search for this job
        </button>
      </div>
      {pasting && (
        <div className="space-y-1">
          <textarea
            className="input min-h-[120px] resize-y"
            placeholder="Paste the whole job description, requirements included"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <button
            type="button"
            className="btn-primary py-1 text-[12px]"
            disabled={pending || text.trim().length < 200}
            title={text.trim().length < 200 ? "Paste the whole description (at least 200 characters)" : undefined}
            onClick={() => onRecheck(text)}
          >
            Use this description
          </button>
        </div>
      )}
    </div>
  );
}

/** "Save to test set": keep this CV and job to measure the optimiser on (backend
 * `tailoring_tests.add`). */
function SaveToTestSet({ documentId }: { documentId: string }) {
  const save = useMutation({ mutationFn: () => api.addToTestSet(documentId) });
  if (!documentId) return null;
  return (
    <>
      <button
        className="btn-ghost py-0.5 text-[11px]"
        disabled={save.isPending}
        title="Keep this CV and job to measure the optimiser on (python -m src.services.tailoring_tests run)"
        onClick={() => save.mutate()}
      >
        {save.isPending && <Loader2 size={11} className="animate-spin" />} Save to test set
      </button>
      {save.data && <span className="text-good">Saved to the test set ({save.data.count} {save.data.count === 1 ? "case" : "cases"})</span>}
      {save.error && <span className="text-bad">{(save.error as Error).message}</span>}
    </>
  );
}

export function JobCard({
  result,
  canTailor,
  selected = false,
  onSelect,
  savedAt,
  onRemove,
  onRecheck,
  removeLabel = "Remove",
  removeTitle = "Remove from saved jobs (its application record is kept)",
  removeAtTop = false,
  removing = false,
  labelable = true,
  notScreened = false,
  refetchSaved = false,
  recheckPending = false,
}: {
  result: MatchResult;
  canTailor: boolean;
  selected?: boolean;
  onSelect?: () => void;
  /** When the job was saved (shown on saved jobs and on saved results). */
  savedAt?: string | null;
  /** Saved-jobs view: remove it from the saved list. */
  onRemove?: () => void;
  removeLabel?: string;
  removeTitle?: string;
  /** Show the remove button in the card's top-right corner instead of the action row. */
  removeAtTop?: boolean;
  /** The remove request is running (the button shows a spinner). */
  removing?: boolean;
  /** Read this job's full posting again (no text), or judge it on pasted `description`. */
  onRecheck?: (description?: string) => void;
  /** Show "Your call" (only for jobs of the current search, which the label snapshots). */
  labelable?: boolean;
  /** Smart search left this posting outside the screening shortlist. */
  notScreened?: boolean;
  /** A saved job: its full posting can be read again (or pasted) outside any search. */
  refetchSaved?: boolean;
  /** A search run (which `onRecheck` starts) is going: its posting options wait for it. */
  recheckPending?: boolean;
}) {
  const { job, verdict, score } = result;
  const [open, setOpen] = useState(false);
  const { template, length, emphasis, level, audience, cvMode } = usePrefs((s) => s.tailor);
  const setTailor = usePrefs((s) => s.setTailor);
  const setTemplate = (value: string) => setTailor({ template: value });
  const setLength = (value: TailorPrefs["length"]) => setTailor({ length: value });
  const setEmphasis = (value: TailorPrefs["emphasis"]) => setTailor({ emphasis: value });
  const setLevel = (value: TailorPrefs["level"]) => setTailor({ level: value });
  const setAudience = (value: TailorPrefs["audience"]) => setTailor({ audience: value });
  const setCvMode = (value: TailorPrefs["cvMode"]) => setTailor({ cvMode: value });
  const [headlineChoice, setHeadlineChoice] = useState<{ headline: string; url: string } | null>(null);
  const [reportOpen, setReportOpen] = useState(true);
  const [documentsOpen, setDocumentsOpen] = useState(false);
  // The tailored version whose report the card shows (null: the newest) and the comparison window.
  const [versionId, setVersionId] = useState<string | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [tailoredAt, setTailoredAt] = useState(0);
  const qc = useQueryClient();
  const setTracking = useSearch((s) => s.setTracking);
  const tailorTask = useTaskId();
  const letterTask = useTaskId();
  // Keyed by job, with the task id as the variable, so a card mounted again (another tab, then
  // back) finds a run still going and shows its progress.
  const tailor = useMutation({
    mutationKey: ["tailor", job.id],
    meta: { syncLists: true },
    mutationFn: (taskId: string) => api.tailor(job.id, template, result, emphasis, level, length, taskId, audience, cvMode),
    onSuccess: (data) => {
      setTailoredAt(Date.now());
      setHeadlineChoice(null);
      setVersionId(data.document_id);
      setDocumentsOpen(true);
      void qc.invalidateQueries({ queryKey: ["tailored-cvs"] });
      if (data.tracking) setTracking(job.id, data.tracking);
      void qc.invalidateQueries({ queryKey: ["state"] });
      void qc.invalidateQueries({ queryKey: ["tracker"] });
      void qc.invalidateQueries({ queryKey: ["saved"] });
    },
  });
  // The chosen (else newest) saved version's report, so it is shown again after a reload or in another tab.
  const drafts = useQuery({ queryKey: ["tailored-cvs", "all"], queryFn: api.allTailoredCVs, staleTime: 60_000 });
  const versions = drafts.data?.filter((d) => d.job_id === job.id) ?? [];
  const letters = useQuery({ queryKey: ["cover-letters"], queryFn: api.coverLetters, staleTime: 60_000 });
  const jobLetter = letters.data?.find((l) => l.job_id === job.id); // newest first
  // The cover letter cites the newest reviewed tailored CV for this job, else the selected CV.
  const letterSource = versions.find((d) => d.reviewed);
  const canWriteLetter = !!letterSource || canTailor;
  const chosenDraft = versionId ? versions.find((d) => d.id === versionId && d.report) : undefined;
  // The last tailoring's report until the saved drafts list it; gone once its CV is deleted.
  const freshListed = !!tailor.data && (drafts.dataUpdatedAt <= tailoredAt || versions.some((d) => d.id === tailor.data.document_id));
  const fresh = freshListed && (!versionId || tailor.data?.document_id === versionId) ? tailor.data : null;
  const savedDraft = chosenDraft ?? (fresh ? undefined : versions.find((d) => d.report));
  const report = savedDraft?.report ?? fresh ?? null;
  const pickVersion = (id: string) => {
    setVersionId(id);
    setHeadlineChoice(null);
    setReportOpen(true);
  };
  const pickHeadline = useMutation({
    meta: { syncLists: true },
    mutationFn: ({ documentId, headline }: { documentId: string; headline: string }) =>
      api.editTailoredCV(documentId, { headline }),
    onSuccess: (view) => {
      setHeadlineChoice({ headline: view.cv.basics.headline ?? "", url: view.download_url });
      void qc.invalidateQueries({ queryKey: ["tailored-cvs"] });
    },
  });
  const letter = useMutation({
    mutationKey: ["letter", job.id],
    meta: { syncLists: true },
    mutationFn: ({ documentId, taskId }: { documentId?: string; taskId: string }) =>
      api.coverLetter(job.id, template, result, documentId, taskId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["cover-letters"] }),
  });
  const writeLetter = (documentId?: string) => letter.mutate({ documentId, taskId: letterTask.next() });
  const tailorRuns = useMutationState({
    filters: { mutationKey: ["tailor", job.id], status: "pending" },
    select: (m) => m.state.variables as string,
  });
  const letterRuns = useMutationState({
    filters: { mutationKey: ["letter", job.id], status: "pending" },
    select: (m) => (m.state.variables as { taskId: string }).taskId,
  });
  const tailorRun = tailor.isPending ? tailorTask.id : (tailorRuns.at(-1) ?? null);
  const letterRun = letter.isPending ? letterTask.id : (letterRuns.at(-1) ?? null);
  const thin = job.description.trim().length < THIN_POSTING;
  const postingTask = useTaskId();
  const posting = useMutation({
    mutationKey: ["posting", job.id],
    meta: { syncLists: true },
    mutationFn: ({ description, taskId }: { description?: string; taskId: string }) =>
      api.refreshSavedPosting(job.id, description, taskId),
  });
  const postingRuns = useMutationState({
    filters: { mutationKey: ["posting", job.id], status: "pending" },
    select: (m) => (m.state.variables as { taskId: string }).taskId,
  });
  const postingRun = posting.isPending ? postingTask.id : (postingRuns.at(-1) ?? null);
  // A board's own id ("linkedin:4478453986") is a search clue; the app's made-up ids are not.
  const boardId = /^(manual|web):/.test(job.id) ? "" : job.id;
  const searchForJob = () =>
    useFindJob.getState().show(
      { title: job.title, company: job.company, job_id: boardId, keywords: "", description: "", posted_at: null, url: job.url ?? "" },
      refetchSaved ? job.id : null,
    );
  const readPosting =
    onRecheck ?? (refetchSaved ? (description?: string) => posting.mutate({ description, taskId: postingTask.next() }) : undefined);
  const status = result.tracking?.status;
  const alignment = verdict?.alignment && verdict.alignment !== "neutral" ? ALIGNMENT_CHIP[verdict.alignment] : null;
  const closing = job.closes_at ? closes(job.closes_at) : null;

  const salary =
    job.salary_min || job.salary_max
      ? `${money(job.salary_min)}${job.salary_max && job.salary_max !== job.salary_min ? `–${money(job.salary_max)}` : ""}`
      : job.salary_range;
  const salaryNote = salary && job.salary_maybe_estimated ? " (may be estimated)" : "";

  return (
    <article data-job-id={job.id} className={cn("card reading min-w-0 p-3.5 [overflow-wrap:anywhere]", selected && "border-accent")}>
      <div className="flex gap-3">
        {onSelect && (
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 shrink-0 accent-[var(--accent)]"
            checked={selected}
            onChange={onSelect}
            aria-label={`Select ${job.title} at ${job.company}`}
            title="Select: save it, or prepare a tailored CV and cover letter for it"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start gap-x-2 gap-y-1">
            <h3 className="min-w-0 basis-full text-[14px] font-semibold leading-tight sm:basis-auto">
              {job.url ? (
                <a href={job.url} target="_blank" rel="noreferrer" className="hover:text-accent hover:underline" title="Open the original posting">
                  {job.title}
                </a>
              ) : (
                job.title
              )}
            </h3>
            {status && (
              <span className={cn("chip shrink-0 font-medium", STATUS_CHIP[status][1])} title={STATUS_CHIP[status][2]}>
                {STATUS_CHIP[status][0]}
              </span>
            )}
            {savedAt && (
              <span className="chip shrink-0 text-accent" title={`Saved ${new Date(savedAt).toLocaleString()}`}>
                <Bookmark size={10} /> Saved {shortDate(savedAt)}
              </span>
            )}
            {verdict && (
              <span
                className="chip shrink-0"
                style={{ color: scoreColor(verdict.fit_score), background: "var(--accent-bg)" }}
              >
                {BAND_LABEL[verdict.band]}
              </span>
            )}
            {verdict && <span className="chip shrink-0 text-muted">{PRIORITY_LABEL[verdict.priority]}</span>}
            {isAdjacentPossibility(result) && (
              <span className="chip shrink-0 text-accent" title="Transferable experience makes this worth reviewing; check unmet requirements and dealbreakers">
                Adjacent possibility
              </span>
            )}
            {notScreened && <span className="chip shrink-0 text-warn">Not screened</span>}
            {alignment && (
              <span className={cn("chip shrink-0", alignment[1])} title={verdict?.alignment_note || undefined}>
                {alignment[0]}
              </span>
            )}
            {result.family && (
              <span className="chip shrink-0 text-faint" title="The role family whose search found this job">
                {result.family}
              </span>
            )}
            {verdict?.borderline && (
              <span
                className="chip shrink-0 text-warn"
                title={
                  verdict.reviewed
                    ? "Within 5 points of the match threshold, after averaging two independent assessments"
                    : "Within 5 points of the match threshold"
                }
              >
                Borderline
              </span>
            )}
            {verdict?.reviewed && !verdict.borderline && (
              <span className="chip shrink-0 text-muted" title="A match or near the threshold at first, so it was assessed twice and averaged">
                Checked twice
              </span>
            )}
            {verdict?.model && <JudgedBy model={verdict.model} />}
            {verdict?.from_memory && (
              <span className="chip shrink-0 text-faint" title="Judged in an earlier search with the same profile and model: same verdict">
                Judged before
              </span>
            )}
            {verdict && !verdict.requirements_checked && (
              <span
                className="chip shrink-0 text-warn"
                title="Only the title and a snippet could be read, so its requirements were not checked against your profile; the score is held to a low stretch"
              >
                Requirements not checked
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[12px] text-muted">
            {job.company}
            {job.location && ` · ${job.location}`}
            {job.source !== "manual" && !job.location?.toLowerCase().includes(job.work_arrangement) && (
              <span className="capitalize"> · {job.work_arrangement}</span>
            )}
            {salary && ` · ${salary}${salaryNote}`}
            <span className="text-faint"> · {job.source}</span>
          </p>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-muted">
            <span className="flex items-center gap-1">
              <CalendarDays size={11} className="shrink-0 text-faint" />
              {job.posted_at ? posted(job.posted_at) : <span className="text-faint">Post date not given</span>}
            </span>
            {closing && (
              <span className={closing.soon ? "text-warn" : undefined} title={`Applications close ${job.closes_at}`}>
                · {closing.text}
              </span>
            )}
          </p>
          {job.source === "biotechnologyjobs" && (
            // Required by the feed's CC BY 4.0 licence: a visible link back to the site.
            <p className="mt-1 text-[11px] text-faint">
              Jobs from{" "}
              <a href="https://biotechnologyjobs.co.uk" target="_blank" rel="noreferrer" className="text-accent hover:underline">
                Biotechnology Jobs
              </a>{" "}
              (CC BY 4.0)
            </p>
          )}
          {job.source === "adzuna" && (
            <div className="mt-1 flex h-[23px] min-w-[116px] items-center gap-1 text-[11px]">
              <a href="https://www.adzuna.co.uk/" target="_blank" rel="noreferrer">Jobs by</a>
              <a href="https://www.adzuna.co.uk/" target="_blank" rel="noreferrer" aria-label="Adzuna">
                <img src="https://upload.wikimedia.org/wikipedia/commons/5/51/Adzuna_Logo.png" alt="Adzuna" className="h-[20px] w-auto" />
              </a>
            </div>
          )}

          {result.excluded && (
            <p className="mt-2 text-[12px] text-bad">Excluded: {result.exclusion_reasons.join("; ")}</p>
          )}
          {!!result.flags?.length && (
            <div className="mt-2 text-[11px]">
              <p className="text-warn">Check before applying:</p>
              {result.flags.map((f) => (
                <p key={f} className="text-muted">
                  · {f}
                </p>
              ))}
            </div>
          )}

          {!verdict && posting.isSuccess && !thin && (
            <p className="mt-2 text-[12px] text-good">
              Full posting read ({job.description.length.toLocaleString()} characters): tailoring and cover letters use it now.
            </p>
          )}
          {!verdict && readPosting && (notScreened || thin) && (
            <div className="mt-2 space-y-1">
              <UncheckedPosting
                onRecheck={readPosting}
                onSearch={searchForJob}
                pending={!!postingRun || recheckPending}
                message={
                  thin
                    ? "Only the title and a snippet of this posting were read, so there is little to screen or tailor a CV to. Fetch the full posting, or open it and paste the job description."
                    : "Not screened yet. Fetch the full posting to screen it against your profile, or paste the job description."
                }
              />
              {postingRun && <TaskProgress id={postingRun} />}
              <TaskError error={posting.error} className="text-[12px]" />
            </div>
          )}
          {verdict && (
            <div className="mt-2 space-y-1 text-[12px]">
              {verdict.fit_summary && <p className="text-fg">{verdict.fit_summary}</p>}
              <Lines items={verdict.reasons} mark="+" tone="text-good" />
              <Lines items={verdict.transferable} mark="~" tone="text-accent" max={2} />
              <Lines items={[...verdict.dealbreakers, ...verdict.essential_unmet, ...verdict.gaps]} mark="−" tone="text-warn" />
              <Lines items={verdict.unknowns} mark="?" tone="text-faint" max={2} />
              {verdict.cap_reason && <p className="text-[11px] text-faint">Score capped: {verdict.cap_reason}</p>}
              {!verdict.requirements_checked && onRecheck && <UncheckedPosting onRecheck={onRecheck} onSearch={searchForJob} pending={recheckPending} />}
            </div>
          )}
        </div>
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
          {onRemove && removeAtTop && (
            <button
              className="btn-ghost h-fit py-1 text-[12px] text-bad"
              title={removeTitle}
              aria-label={`${removeLabel} ${job.title} at ${job.company}`}
              disabled={removing}
              onClick={onRemove}
            >
              {removing ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />} {removeLabel}
            </button>
          )}
          {verdict && <ScoreBadge value={verdict.fit_score} label="AI fit" />}
          {score && !result.excluded && (
            <div className={cn(verdict && "opacity-60")}>
              <ScoreBadge value={score.total} label={verdict ? "keywords" : "score"} />
            </div>
          )}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {job.url && (
          <a className="btn-ghost py-1 text-[12px]" href={job.url} target="_blank" rel="noreferrer">
            <ExternalLink size={12} /> View on {SOURCE_SITE[job.source] ?? job.company}
          </a>
        )}
        {!job.url && <span className="text-[11px] text-faint">No link provided by the source</span>}
        <select aria-label="CV design" title="Same as my CV: a copy of your Word CV (for a PDF, its Word version). Pick a design only to change the look." className="input w-auto py-1 text-[12px]" value={template} onChange={(e) => setTemplate(e.target.value)}>
          <option value="original">Design: same as my CV</option>
          <option value="classic">Classic design</option>
          <option value="modern">Modern design</option>
          <option value="compact">Compact design</option>
        </select>
        <select aria-label="CV length" title="Automatic keeps every line unless the job is clearly more junior than your experience" className="input w-auto py-1 text-[12px]" value={length} onChange={(e) => setLength(e.target.value as typeof length)}>
          <option value="auto">Length: automatic</option>
          <option value="full">Keep full length</option>
          <option value="junior">Junior role (trim)</option>
        </select>
        <select aria-label="CV emphasis" title="Which evidenced work should lead the CV" className="input w-auto py-1 text-[12px]" value={emphasis} onChange={(e) => setEmphasis(e.target.value as typeof emphasis)}>
          <option value="auto">Emphasis: automatic</option>
          <option value="leadership">Leadership</option>
          <option value="hands_on">Hands-on / wet lab</option>
        </select>
        <select aria-label="CV level emphasis" title="Adjust presentation while keeping your actual titles and experience" className="input w-auto py-1 text-[12px]" value={level} onChange={(e) => setLevel(e.target.value as typeof level)}>
          <option value="auto">Level: automatic</option>
          <option value="senior">More senior emphasis</option>
          <option value="junior">More junior emphasis</option>
        </select>
        <select aria-label="CV audience" title="Academic: worded as a research profile, with education and publications before experience (sections you already have, moved whole). Automatic: academic when the job is an academic post." className="input w-auto py-1 text-[12px]" value={audience} onChange={(e) => setAudience(e.target.value as typeof audience)}>
          <option value="auto">Audience: automatic</option>
          <option value="industry">Industry</option>
          <option value="academic">Academic</option>
        </select>
        <select aria-label="CV writing mode" title="Optimised draft makes the strongest evidenced case and places plausible missing skills or experience in red for your review" className="input w-auto py-1 text-[12px]" value={cvMode} onChange={(e) => setCvMode(e.target.value as typeof cvMode)}>
          <option value="standard">Standard CV</option>
          <option value="optimised">Optimised draft · review red text</option>
        </select>
        <button
          className="btn-ghost py-1 text-[12px]"
          title={
            thin
              ? "Only a snippet of this posting was read: fetch the full posting first, or the CV is tailored to little more than its title"
              : canTailor
                ? "Write a tailored CV for this job (each run adds a version)"
                : "Tailor from your selected CV: select one in the CV library if none is"
          }
          disabled={!!tailorRun}
          onClick={() => tailor.mutate(tailorTask.next())}
        >
          {tailorRun ? <Loader2 size={12} className="animate-spin" /> : <FileText size={12} />}
          Tailor CV
        </button>
        <button
          className="btn-ghost py-1 text-[12px]"
          disabled={!!letterRun || !canWriteLetter}
          title={
            letterSource
              ? `Write a cover letter citing ${letterSource.label} (to use another CV, open Review CV)`
              : canTailor
                ? "Write a cover letter citing your selected CV"
                : "Select a CV in the library, or tailor one for this job, to write a cover letter"
          }
          onClick={() => writeLetter(letterSource?.id)}
        >
          {letterRun ? <Loader2 size={12} className="animate-spin" /> : <Mail size={12} />} Cover letter
        </button>
        <button
          className="btn-ghost py-1 text-[12px]"
          title="Review and edit a saved tailored CV, attach an older Word CV, or write the letter from a CV you choose"
          aria-expanded={documentsOpen}
          onClick={() => setDocumentsOpen(!documentsOpen)}
        >
          <FileText size={12} /> Review CV
        </button>
        {onRemove && !removeAtTop && (
          <button className="btn-ghost py-1 text-[12px] text-bad" title={removeTitle} onClick={onRemove}>
            <Trash2 size={12} /> {removeLabel}
          </button>
        )}
        {(score || verdict) && (
          <button className="ml-auto flex items-center gap-1 text-[11px] text-faint hover:text-fg" onClick={() => setOpen(!open)}>
            Details <ChevronDown size={12} className={cn("transition-transform", open && "rotate-180")} />
          </button>
        )}
      </div>

      <TrackingControls key={`${job.id}:${result.tracking?.note ?? ""}:${result.tracking?.reason ?? ""}`} result={result} />
      {labelable && !result.excluded && <LabelControls jobId={job.id} />}

      {versions.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="text-faint" title="Each tailoring is kept as its own version. Change Level, Length, Emphasis or Audience and tailor again to add one.">
            CV versions:
          </span>
          {[...versions].reverse().map((v) => {
            const shown = v.report?.document_id === report?.document_id;
            return (
              <button
                key={v.id}
                className={cn("rounded border px-2 py-0.5 hover:border-accent", shown ? "border-accent text-fg" : "border-border text-muted")}
                title={`Show this version's report · tailored ${new Date(v.created_at).toLocaleString()}`}
                aria-pressed={shown}
                onClick={() => pickVersion(v.id)}
              >
                {v.label}
                {v.report?.screening && <span className="ml-1" title="Screening score (ATS)" style={{ color: scoreColor(v.report.screening.after.score) }}>{v.report.screening.after.score}</span>}
              </button>
            );
          })}
          <button
            className="btn-ghost ml-1 py-0.5 text-[11px]"
            title="Read the versions beside your original CV for this job: scores, requirements, keywords, what improved or got worse"
            onClick={() => setCompareOpen(true)}
          >
            <Scale size={12} /> Compare versions
          </button>
        </div>
      )}
      <CVVersionCompare open={compareOpen} onClose={() => setCompareOpen(false)} jobId={job.id} versions={versions} onPick={pickVersion} />
      {report && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-surface p-2 text-[12px]">
          <CVViewButton downloadUrl={headlineChoice?.url ?? report.download_url} title={`Tailored CV · ${job.title}`} />
          <SaveToTestSet documentId={report.document_id} />
          {!!report.review_additions?.some((item) => item.status === "pending") && (
            <span className="text-warn">Review red suggestions before using this CV</span>
          )}
          {jobLetter && <LetterViewButton id={jobLetter.id} title={`Cover letter · ${job.title}`} downloadUrl={jobLetter.docx_url} />}
          {!fresh && savedDraft && (
            <span className="text-faint" title="Saved with this tailored CV">
              {savedDraft.label} · {new Date(savedDraft.created_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}
            </span>
          )}
          <ScreeningSummary screening={report.screening} />
          <ReadabilitySummary readability={report.readability} />
          <FitCheckSummary check={report.fit_check} capReason={report.fit_check?.search_anchored && report.fit_check.before === verdict?.fit_score ? verdict?.cap_reason : null} />
          {report.fit_check?.if_confirmed && (
            <IfConfirmed
              projection={report.fit_check.if_confirmed}
              labels={Object.fromEntries((report.review_additions ?? []).map((a) => [a.id, a.text.length > 70 ? `${a.text.slice(0, 70)}…` : a.text]))}
            />
          )}
          <span className="text-muted">Word keyword coverage {report.source_ats_keyword_coverage == null ? "" : `${Math.round(report.source_ats_keyword_coverage * 100)}% original → `}{Math.round((report.ats?.keyword_coverage ?? report.keyword_coverage) * 100)}% tailored · review the wording and claims</span>
          <button
            className="ml-auto flex items-center gap-1 text-muted hover:text-fg"
            aria-expanded={reportOpen}
            title={reportOpen ? "Hide the tailoring report" : "Show the tailoring report"}
            onClick={() => setReportOpen((v) => !v)}
          >
            Report <ChevronDown size={12} className={cn("transition-transform", reportOpen && "rotate-180")} />
          </button>
          {reportOpen && <>
          {report.fit_check && <FitCheckDetails check={report.fit_check} capReason={report.fit_check.search_anchored && report.fit_check.before === verdict?.fit_score ? verdict?.cap_reason : null} />}
          <PositioningDetails positioning={report.positioning} requirements={report.requirements} />
          {!!report.key_strengths?.length && (
            <p className="basis-full text-[11px] text-faint">Key strengths under your summary: {report.key_strengths.join(" · ")}</p>
          )}
          {!!report.role_skills?.length && (
            <p className="basis-full text-[11px] text-faint">Added under your summary: Relevant skills for this role: {report.role_skills.join(" · ")}</p>
          )}
          {report.revision && <p className="basis-full text-[11px] text-muted">{report.revision}</p>}
          {!!report.length_notes?.length && (
            <div className="basis-full text-[11px] text-muted" title="Advice only: nothing was removed. Edit the lines yourself if you agree.">
              <p className="font-medium">Length advice (nothing changed):</p>
              {report.length_notes.map((note) => <p key={note} className="text-faint">· {note}</p>)}
            </div>
          )}
          {!!report.compressions?.some((c) => c.status === "pending") && (
            <p className="basis-full text-[11px] text-warn">
              {report.compressions.filter((c) => c.status === "pending").length} shorter version(s) in red below the lines they would replace: compare them under Review and edit CV.
            </p>
          )}
          {!!report.term_bridges?.length && (
            <div className="basis-full text-[11px] text-muted" title="The job's own words for work your CV shows in other words. Code checked each quoted line; check that each one says the same thing.">
              <p className="font-medium">Job terms used for your own wording (check each one):</p>
              {report.term_bridges.map((b) => (
                <p key={`${b.term}:${b.source_id}`} className="text-faint">· {b.term} ← “{b.quote}”</p>
              ))}
            </div>
          )}
          {!!report.skills_to_confirm?.length && (
            <div className="basis-full rounded border border-warn/40 p-2 text-[11px] text-warn">
              <p className="font-semibold">Skills to confirm before use — shown in red in Word</p>
              <p>These are plausible suggestions, not confirmed CV facts. Edit or remove them in Word. Add any you can verify to your CV before relying on them in applications.</p>
              {report.skills_to_confirm.map((item) => (
                <p key={item.skill}>· {item.skill} — {item.reason} <span className="text-faint">(CV: “{item.background_quote}”)</span></p>
              ))}
            </div>
          )}
          {report.missing_keywords.length > 0 && (
            <span className="min-w-0 text-faint" title="Job keywords not found word for word in the tailored CV. Requirements written as sentences are judged by the match verdict, not here. If your experience covers one, add it truthfully to the Master CV.">
              Keywords not found word for word: {report.missing_keywords.join(", ")}
            </span>
          )}
          {report.restored_keywords.length > 0 && (
            <span className="min-w-0 text-faint" title="A left-out bullet carried these keywords, so it was put back">
              Kept: {report.restored_keywords.join(", ")}
            </span>
          )}
          <p className="basis-full text-[11px] text-faint">
            {report.trim?.allowed
              ? `Trimmed for a more junior role (${report.trim.reason}): ${report.left_out.length} line(s) left out.`
              : "Nothing removed: the most relevant lines lead each section, the rest follow."}
            {report.trim && !report.trim.allowed ? ` Not trimmed: ${report.trim.reason}.` : ""}
          </p>
          {report.headline_options.length > 0 && (
            <div className="basis-full space-y-1 text-[11px]">
              <p className="text-muted">
                Headline: <span className="text-fg">{headlineChoice?.headline ?? report.headline ?? "none"}</span>
                {!headlineChoice || headlineChoice.headline === report.headline ? " (your own)" : " (chosen; saved in the Word file)"}
              </p>
              <div className="flex flex-wrap gap-1">
                {[report.headline ?? "", ...report.headline_options].filter(Boolean).map((option, index) => (
                  <button
                    key={option}
                    className={cn("rounded border px-2 py-0.5 hover:border-accent", (headlineChoice?.headline ?? report.headline) === option ? "border-accent text-fg" : "border-border text-muted")}
                    disabled={pickHeadline.isPending || (headlineChoice?.headline ?? report.headline) === option}
                    onClick={() => pickHeadline.mutate({ documentId: report.document_id, headline: option })}
                  >
                    {index === 0 ? `Keep: ${option}` : option}
                  </button>
                ))}
                {pickHeadline.isPending && <Loader2 size={12} className="animate-spin text-muted" />}
              </div>
              {pickHeadline.error && <p className="text-bad">{pickHeadline.error.message}</p>}
            </div>
          )}
          {report.document_notes.map((note) => (
            <p key={note} className="basis-full text-[11px] text-faint">Word file: {note}</p>
          ))}
          {report.left_out.length > 0 && (
            <details className="basis-full text-[11px] text-faint">
              <summary className="cursor-pointer">Left out ({report.left_out.length})</summary>
              {report.left_out.map((line) => (
                <p key={line}>· {line}</p>
              ))}
            </details>
          )}
          {report.critique.length > 0 && (
            <details className="basis-full text-[11px] text-faint">
              <summary className="cursor-pointer">Review of the first draft: {report.critique.length} point(s), used for the revised draft you downloaded (it never removes lines, changes the design or headline, or adds facts)</summary>
              {report.critique.map((c) => (
                <p key={c}>· {c}</p>
              ))}
            </details>
          )}
          {report.ats?.warnings.map((w) => (
            <p key={w} className="basis-full text-[11px] text-warn">
              ATS: {w}
            </p>
          ))}
          <Rejected items={report.rejections} what="change(s)" />
          </>}
        </div>
      )}
      {tailorRun && <TaskProgress id={tailorRun} className="mt-2" />}
      <TaskError error={tailor.error} className="mt-2 text-[12px]" />
      {documentsOpen && <TailoredCVPanel jobId={job.id} result={result} hasCv={canTailor} letterPending={!!letterRun} onLetter={writeLetter} />}
      {letter.data && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-surface p-2 text-[12px]">
          {!report && letter.data.id && <LetterViewButton id={letter.data.id} title={`Cover letter · ${job.title}`} downloadUrl={letter.data.download_url} />}
          <span className="text-muted">Cover letter written: {letter.data.paragraphs} paragraph(s), each backed by your CV{report ? " · View cover letter is next to View CV" : ""}</span>
          {letter.data.flags.length > 0 && (
            <span className="text-bad">
              {letter.data.flags.length} paragraph{letter.data.flags.length === 1 ? "" : "s"} in red to review (open the letter in Cover letters to see why)
            </span>
          )}
          {letter.data.score && (
            <span className="basis-full" title="Computed by code: the job's main terms named, the company and exact role named, a one-page length, readable sentences and paragraphs the checks did not question. Grade A is 80 or more.">
              <span className="font-medium">Letter score: </span>{letter.data.score.score} ({letter.data.score.grade})
              {letter.data.score.notes.map((n) => <span key={n} className="block text-[11px] text-faint">{n}</span>)}
            </span>
          )}
          {letter.data.notes.map((n) => <span key={n} className="basis-full text-[11px] text-warn">To check: {n}</span>)}
        </div>
      )}
      {letterRun && <TaskProgress id={letterRun} className="mt-2" />}
      <TaskError error={letter.error} className="mt-2 text-[12px]" />

      {open && (
        <div className="mt-3 space-y-1.5 border-t border-border pt-3">
          {verdict && (
            <>
              <p className="label">Degree of match · {verdict.fit_score}/100</p>
              {DIMENSIONS.map(([k, label, max]) => (
                <Bar
                  key={k}
                  label={label}
                  value={verdict.dimensions[k] / max}
                  text={`${verdict.dimensions[k]}/${max}${verdict.ratings ? ` · level ${verdict.ratings[k]}/4` : ""}`}
                />
              ))}
              <Lines items={verdict.reasons.slice(3)} mark="+" tone="text-good" max={10} />
              <Lines items={verdict.transferable.slice(2)} mark="~" tone="text-accent" max={10} />
              <Lines items={[...verdict.dealbreakers, ...verdict.essential_unmet, ...verdict.gaps].slice(3)} mark="−" tone="text-warn" max={10} />
              <Lines items={verdict.unknowns.slice(2)} mark="?" tone="text-faint" max={10} />
              {score && <p className="label pt-2">Keyword pre-filter</p>}
            </>
          )}
          {score && METRICS.filter(([k]) => score.metrics_used.includes(k)).map(([k, label]) => (
            <Bar key={k} label={label} value={score[k]} />
          ))}
          {score && score.matched_skills.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-1">
              {score.matched_skills.map((k) => (
                <span key={k} className="chip text-good">{k}</span>
              ))}
              {score.missing_required_skills.map((k) => (
                <span key={k} className="chip text-warn">{k}</span>
              ))}
            </div>
          )}
          {score?.notes.map((n) => (
            <p key={n} className="text-[11px] text-faint">{n}</p>
          ))}
          {job.description && <Description text={job.description} />}
        </div>
      )}
    </article>
  );
}

/** A verdict from another plan's model (its own plan was spent mid-search). */
function JudgedBy({ model }: { model: string }) {
  const view = useQuery({ queryKey: ["connections"], queryFn: api.connections, staleTime: 60_000 });
  const own = new Set(
    (view.data?.roles ?? [])
      .filter((r) => r.role === "screening" || r.role === "quality")
      .map((r) => `${r.configured.provider}:${r.configured.model}`),
  );
  if (!view.data || own.has(model)) return null;
  return (
    <span className="chip shrink-0 text-warn" title={`Judged by ${model}: the configured plan was spent, so the other plan's model of the same standing took over`}>
      by {model.split(":").slice(1).join(":")}
    </span>
  );
}
