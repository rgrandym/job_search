import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bookmark, CalendarDays, ChevronDown, Download, ExternalLink, FileText, Loader2, Mail, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { Rejection } from "../lib/api";
import type { FitDimensions, JobStatus, JobVerdict, MatchResult, OutcomeStage, UserLabel } from "../lib/types";
import { cn, money, scoreColor } from "../lib/utils";
import { useSearch } from "../stores/searchStore";
import { TaskProgress, useTaskId } from "./TaskProgress";
import { TailoredCVPanel } from "./TailoredCVPanel";

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
function UncheckedPosting({ onRecheck }: { onRecheck: (description?: string) => void }) {
  const [pasting, setPasting] = useState(false);
  const [text, setText] = useState("");
  return (
    <div className="space-y-1.5 rounded-md border border-border bg-surface p-2 text-[11px]">
      <p className="text-muted">
        Its requirements could not be read, so it was not checked against your profile. Fetch the full posting, or open it
        and paste the job description.
      </p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-ghost py-1 text-[12px]" onClick={() => onRecheck()}>
          Fetch full posting
        </button>
        <button type="button" className="btn-ghost py-1 text-[12px]" onClick={() => setPasting(!pasting)}>
          Paste description
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
            disabled={text.trim().length < 200}
            title={text.trim().length < 200 ? "Paste the whole description (at least 200 characters)" : undefined}
            onClick={() => onRecheck(text)}
          >
            Judge with this description
          </button>
        </div>
      )}
    </div>
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
  labelable = true,
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
  /** Read this job's full posting again (no text), or judge it on pasted `description`. */
  onRecheck?: (description?: string) => void;
  /** Show "Your call" (only for jobs of the current search, which the label snapshots). */
  labelable?: boolean;
}) {
  const { job, verdict, score } = result;
  const [open, setOpen] = useState(false);
  const [template, setTemplate] = useState("classic");
  const [emphasis, setEmphasis] = useState<"auto" | "leadership" | "hands_on">("auto");
  const [level, setLevel] = useState<"auto" | "senior" | "junior">("auto");
  const [documentsOpen, setDocumentsOpen] = useState(false);
  const qc = useQueryClient();
  const setTracking = useSearch((s) => s.setTracking);
  const tailorTask = useTaskId();
  const letterTask = useTaskId();
  const tailor = useMutation({
    mutationFn: () => api.tailor(job.id, template, result, emphasis, level, tailorTask.next()),
    onSuccess: (data) => {
      setDocumentsOpen(true);
      void qc.invalidateQueries({ queryKey: ["tailored-cvs"] });
      if (data.tracking) setTracking(job.id, data.tracking);
      void qc.invalidateQueries({ queryKey: ["state"] });
      void qc.invalidateQueries({ queryKey: ["tracker"] });
      void qc.invalidateQueries({ queryKey: ["saved"] });
    },
  });
  const letter = useMutation({
    mutationFn: (documentId?: string) => api.coverLetter(job.id, template, result, documentId, letterTask.next()),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["cover-letters"] }),
  });
  const status = result.tracking?.status;
  const alignment = verdict?.alignment && verdict.alignment !== "neutral" ? ALIGNMENT_CHIP[verdict.alignment] : null;
  const closing = job.closes_at ? closes(job.closes_at) : null;

  const salary =
    job.salary_min || job.salary_max
      ? `${money(job.salary_min)}${job.salary_max && job.salary_max !== job.salary_min ? `–${money(job.salary_max)}` : ""}`
      : job.salary_range;
  const salaryNote = salary && job.salary_maybe_estimated ? " (may be estimated)" : "";

  return (
    <article className={cn("card min-w-0 p-3.5 [overflow-wrap:anywhere]", selected && "border-accent")}>
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

          {verdict && (
            <div className="mt-2 space-y-1 text-[12px]">
              {verdict.fit_summary && <p className="text-fg">{verdict.fit_summary}</p>}
              <Lines items={verdict.reasons} mark="+" tone="text-good" />
              <Lines items={verdict.transferable} mark="~" tone="text-accent" max={2} />
              <Lines items={[...verdict.dealbreakers, ...verdict.essential_unmet, ...verdict.gaps]} mark="−" tone="text-warn" />
              <Lines items={verdict.unknowns} mark="?" tone="text-faint" max={2} />
              {verdict.cap_reason && <p className="text-[11px] text-faint">Score capped: {verdict.cap_reason}</p>}
              {!verdict.requirements_checked && onRecheck && <UncheckedPosting onRecheck={onRecheck} />}
            </div>
          )}
        </div>
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
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
        {canTailor && !result.excluded && (
          <>
            <select className="input w-auto py-1 text-[12px]" value={template} onChange={(e) => setTemplate(e.target.value)}>
              <option value="classic">Classic</option>
              <option value="modern">Modern</option>
              <option value="compact">Compact</option>
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
            <button className="btn-ghost py-1 text-[12px]" disabled={tailor.isPending} onClick={() => tailor.mutate()}>
              {tailor.isPending ? <Loader2 size={12} className="animate-spin" /> : <FileText size={12} />}
              Tailor CV
            </button>
          </>
        )}
        <button
          className="btn-ghost py-1 text-[12px]"
          title="Choose a saved tailored CV or your Master CV for the cover letter"
          onClick={() => setDocumentsOpen(!documentsOpen)}
        >
          <Mail size={12} /> Cover letter / review CV
        </button>
        {onRemove && (
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

      {tailor.data && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-surface p-2 text-[12px]">
          <a className="btn-primary py-1 text-[12px]" href={tailor.data.download_url}>
            <Download size={12} /> Download .docx
          </a>
          <span className="text-muted">Word keyword coverage {tailor.data.source_ats_keyword_coverage == null ? "" : `${Math.round(tailor.data.source_ats_keyword_coverage * 100)}% original → `}{Math.round((tailor.data.ats?.keyword_coverage ?? tailor.data.keyword_coverage) * 100)}% tailored · review the wording and claims</span>
          {tailor.data.missing_keywords.length > 0 && (
            <span className="min-w-0 text-faint" title="Not in your Master CV: gaps to address truthfully, never to fill in">
              Gaps: {tailor.data.missing_keywords.join(", ")}
            </span>
          )}
          {tailor.data.restored_keywords.length > 0 && (
            <span className="min-w-0 text-faint" title="A left-out bullet carried these keywords, so it was put back">
              Kept: {tailor.data.restored_keywords.join(", ")}
            </span>
          )}
          {tailor.data.critique.length > 0 && (
            <details className="basis-full text-[11px] text-faint">
              <summary className="cursor-pointer">Review raised {tailor.data.critique.length} point(s); revised draft created</summary>
              {tailor.data.critique.map((c) => (
                <p key={c}>· {c}</p>
              ))}
            </details>
          )}
          {tailor.data.ats?.warnings.map((w) => (
            <p key={w} className="basis-full text-[11px] text-warn">
              ATS: {w}
            </p>
          ))}
          <Rejected items={tailor.data.rejections} what="change(s)" />
        </div>
      )}
      {tailor.isPending && <TaskProgress id={tailorTask.id} className="mt-2" />}
      {tailor.error && <p className="mt-2 text-[12px] text-bad">{(tailor.error as Error).message}</p>}
      {documentsOpen && <TailoredCVPanel jobId={job.id} result={result} hasCv={canTailor} letterPending={letter.isPending} onLetter={(id) => letter.mutate(id)} />}
      {letter.data && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-surface p-2 text-[12px]">
          <a className="btn-primary py-1 text-[12px]" href={letter.data.download_url}>
            <Download size={12} /> Download cover letter
          </a>
          <span className="text-muted">{letter.data.paragraphs} paragraph(s), each backed by your CV</span>
          <Rejected items={letter.data.rejections} what="paragraph(s)" />
        </div>
      )}
      {letter.isPending && <TaskProgress id={letterTask.id} className="mt-2" />}
      {letter.error && <p className="mt-2 text-[12px] text-bad">{(letter.error as Error).message}</p>}

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
          {job.description && (
            <p className="line-clamp-6 whitespace-pre-line pt-1 text-[12px] text-muted">{job.description}</p>
          )}
        </div>
      )}
    </article>
  );
}
