import { useMutation } from "@tanstack/react-query";
import { ChevronDown, Download, ExternalLink, FileText, Loader2, MessageSquare } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { MatchResult } from "../lib/types";
import { cn, money, scoreColor } from "../lib/utils";

const METRICS = [
  ["title", "Title & level"],
  ["skills", "Skills"],
  ["experience", "Experience"],
  ["location", "Location"],
  ["semantic", "Context"],
] as const;

const VERDICT_LABEL = { strong: "Strong fit", good: "Good fit", stretch: "Stretch", poor: "Not a fit" };

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

function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="grid grid-cols-[86px_1fr_32px] items-center gap-2 text-[11px]">
      <span className="text-muted">{label}</span>
      <div className="h-1.5 overflow-hidden rounded-full bg-surface">
        <div className="h-full rounded-full" style={{ width: `${value * 100}%`, background: scoreColor(value * 100) }} />
      </div>
      <span className="text-right text-muted">{Math.round(value * 100)}</span>
    </div>
  );
}

export function JobCard({ result, canTailor, onAsk }: { result: MatchResult; canTailor: boolean; onAsk: (r: MatchResult) => void }) {
  const { job, verdict, score } = result;
  const [open, setOpen] = useState(false);
  const [template, setTemplate] = useState("classic");
  const tailor = useMutation({ mutationFn: () => api.tailor(job.id, template) });

  const salary =
    job.salary_min || job.salary_max
      ? `${money(job.salary_min)}${job.salary_max && job.salary_max !== job.salary_min ? `–${money(job.salary_max)}` : ""}`
      : job.salary_range;

  return (
    <article className="card p-3.5">
      <div className="flex gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-2">
            <h3 className="text-[14px] font-semibold leading-tight">{job.title}</h3>
            {verdict && (
              <span
                className="chip shrink-0"
                style={{ color: scoreColor(verdict.fit_score), background: "var(--accent-bg)" }}
              >
                {VERDICT_LABEL[verdict.verdict]}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[12px] text-muted">
            {job.company}
            {job.location && ` · ${job.location}`}
            {!job.location?.toLowerCase().includes(job.work_arrangement) && (
              <span className="capitalize"> · {job.work_arrangement}</span>
            )}
            {salary && ` · ${salary}`}
            <span className="text-faint"> · {job.source}</span>
          </p>

          {result.excluded && (
            <p className="mt-2 text-[12px] text-bad">Excluded: {result.exclusion_reasons.join("; ")}</p>
          )}

          {verdict && (
            <div className="mt-2 space-y-1 text-[12px]">
              {verdict.reasons.slice(0, 3).map((r) => (
                <p key={r} className="text-muted">
                  <span className="text-good">+</span> {r}
                </p>
              ))}
              {[...verdict.dealbreakers, ...verdict.gaps].slice(0, 3).map((g) => (
                <p key={g} className="text-muted">
                  <span className="text-warn">−</span> {g}
                </p>
              ))}
            </div>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
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
            <ExternalLink size={12} /> Posting
          </a>
        )}
        {canTailor && !result.excluded && (
          <>
            <select className="input w-auto py-1 text-[12px]" value={template} onChange={(e) => setTemplate(e.target.value)}>
              <option value="classic">Classic</option>
              <option value="modern">Modern</option>
              <option value="compact">Compact</option>
            </select>
            <button className="btn-ghost py-1 text-[12px]" disabled={tailor.isPending} onClick={() => tailor.mutate()}>
              {tailor.isPending ? <Loader2 size={12} className="animate-spin" /> : <FileText size={12} />}
              Tailor CV
            </button>
          </>
        )}
        <button className="btn-ghost py-1 text-[12px]" onClick={() => onAsk(result)}>
          <MessageSquare size={12} /> Ask agent
        </button>
        {score && (
          <button className="ml-auto flex items-center gap-1 text-[11px] text-faint hover:text-fg" onClick={() => setOpen(!open)}>
            Details <ChevronDown size={12} className={cn("transition-transform", open && "rotate-180")} />
          </button>
        )}
      </div>

      {tailor.data && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-surface p-2 text-[12px]">
          <a className="btn-primary py-1 text-[12px]" href={tailor.data.download_url}>
            <Download size={12} /> Download .docx
          </a>
          <span className="text-muted">ATS keyword coverage {Math.round(tailor.data.keyword_coverage * 100)}%</span>
          {tailor.data.missing_keywords.length > 0 && (
            <span className="text-faint">Missing: {tailor.data.missing_keywords.join(", ")}</span>
          )}
        </div>
      )}
      {tailor.error && <p className="mt-2 text-[12px] text-bad">{(tailor.error as Error).message}</p>}

      {open && score && (
        <div className="mt-3 space-y-1.5 border-t border-border pt-3">
          {METRICS.filter(([k]) => score.metrics_used.includes(k)).map(([k, label]) => (
            <Bar key={k} label={label} value={score[k]} />
          ))}
          {score.matched_skills.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-1">
              {score.matched_skills.map((k) => (
                <span key={k} className="chip text-good">{k}</span>
              ))}
              {score.missing_required_skills.map((k) => (
                <span key={k} className="chip text-warn">{k}</span>
              ))}
            </div>
          )}
          {score.notes.map((n) => (
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
