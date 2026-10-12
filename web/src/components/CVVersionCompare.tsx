import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Check, Download, Loader2, Minus, Scale, Trophy } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import type { AreaChange, SettingEffect, TailoredCVView, VersionComparison, VersionReading } from "../lib/types";
import { cn, scoreColor } from "../lib/utils";
import { TaskError, TaskProgress, useTaskId } from "./TaskProgress";
import { Modal } from "./ui";

const MAX_CVS = 6; // backend fit_check.MAX_VERSIONS, originals included

const DIMENSION_LABEL: Record<string, string> = {
  function: "Function",
  domain: "Domain",
  seniority: "Seniority",
  leadership: "Leadership",
  sector: "Sector",
  practicality: "Practicality",
};

const pct = (v: number) => `${Math.round(v * 100)}%`;
const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "";

/** A value and its change against the version's original (none for an original). */
function Delta({ value, base, text, unit = "" }: { value: number; base?: number; text: string; unit?: string }) {
  if (base === undefined) return <span>{text}</span>;
  const diff = Math.round((value - base) * (unit === "%" ? 100 : 1));
  const tone = diff > 0 ? "text-good" : diff < 0 ? "text-warn" : "text-faint";
  return (
    <span>
      {text}{" "}
      <span className={cn("text-[11px]", tone)}>
        {diff === 0 ? "=" : `${diff > 0 ? "+" : ""}${diff}${unit}`}
      </span>
    </span>
  );
}

type Row = { label: string; hint?: string; cell: (r: VersionReading, base?: VersionReading) => ReactNode; best?: (r: VersionReading) => number };

function rows(c: VersionComparison): Row[] {
  const dims: Row[] = (c.readings[0]?.dimensions ?? []).map((d, i) => ({
    label: DIMENSION_LABEL[d.name] ?? d.name,
    hint: "0-4: how clearly this CV shows it, read for this job",
    cell: (r, base) => <Delta value={r.dimensions[i].after} base={base?.dimensions[i].after} text={`${r.dimensions[i].after}/4`} />,
    best: (r) => r.dimensions[i].after,
  }));
  const total = c.requirements.length;
  return [
    ...dims,
    {
      label: "Requirements shown",
      hint: "Of the job's listed requirements, how many the reader saw clearly met",
      cell: (r, base) => <Delta value={r.requirements_shown.length} base={base?.requirements_shown.length} text={`${r.requirements_shown.length} of ${total}`} />,
      best: (r) => r.requirements_shown.length,
    },
    {
      label: "Keyword coverage",
      hint: "Job keywords found word for word anywhere in the CV",
      cell: (r, base) => <Delta value={r.keyword_coverage} base={base?.keyword_coverage} text={pct(r.keyword_coverage)} unit="%" />,
      best: (r) => r.keyword_coverage,
    },
    {
      label: "Keywords on page 1",
      hint: "Job keywords in roughly the first page, where a screener looks first",
      cell: (r, base) => <Delta value={r.first_page_coverage} base={base?.first_page_coverage} text={pct(r.first_page_coverage)} unit="%" />,
      best: (r) => r.first_page_coverage,
    },
    { label: "Words", cell: (r, base) => <Delta value={r.words} base={base?.words} text={String(r.words)} /> },
    {
      label: "Guarded rewrites",
      hint: "Rewrites the no-fabrication guards rejected · lines left out by a trim",
      cell: (r) => (r.kind === "original" ? <span className="text-faint">—</span> : `${r.rejected_changes} rejected · ${r.left_out} left out`),
    },
  ];
}

function ScoreTable({ comparison, downloads, onPick }: {
  comparison: VersionComparison;
  downloads: Record<string, string>;
  onPick?: (id: string) => void;
}) {
  const { readings } = comparison;
  const byId = Object.fromEntries(readings.map((r) => [r.id, r]));
  const ranked = [...readings].sort((a, b) => b.score - a.score);
  const rank = (r: VersionReading) => ranked.findIndex((x) => x.score === r.score) + 1;
  return (
    <div className="overflow-x-auto scroll-thin">
      <table className="w-full min-w-[560px] border-collapse text-[12px]">
        <thead>
          <tr className="align-bottom">
            <th className="w-36 p-2 text-left font-medium text-muted">For this job</th>
            {readings.map((r) => (
              <th key={r.id} className={cn("p-2 text-left font-normal", r.id === comparison.best_id && "bg-accent-bg")}>
                <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-faint">
                  {r.kind === "original" ? "Original" : "Tailored"}
                  {r.id === comparison.best_id && <span className="flex items-center gap-0.5 text-accent"><Trophy size={10} /> best</span>}
                </div>
                <div className="font-semibold text-fg">{r.label}</div>
                {r.created_at && <div className="text-[11px] text-faint">{when(r.created_at)}</div>}
                <div className="mt-1 flex flex-wrap gap-2 text-[11px]">
                  {downloads[r.id] && <a className="text-accent hover:underline" href={downloads[r.id]}><Download size={10} className="inline" /> .docx</a>}
                  {onPick && r.kind === "tailored" && <button className="text-accent hover:underline" onClick={() => onPick(r.id)}>Show in card</button>}
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr className="border-t border-border">
            <td className="p-2 font-medium" title="Original search score plus weighted changes in the five CV evidence dimensions; practicality stays fixed">Job fit shown /100</td>
            {readings.map((r) => {
              const base = r.baseline_id ? byId[r.baseline_id] : undefined;
              return (
                <td key={r.id} className={cn("p-2", r.id === comparison.best_id && "bg-accent-bg")}>
                  <span className="text-[20px] font-semibold tabular-nums" style={{ color: scoreColor(r.score) }}>{r.score}</span>
                  {base && <Delta value={r.score} base={base.score} text="" />}
                  <span className="ml-1 text-[11px] text-faint">#{rank(r)}</span>
                </td>
              );
            })}
          </tr>
          {rows(comparison).map((row) => {
            const top = row.best ? Math.max(...readings.map(row.best)) : null;
            return (
              <tr key={row.label} className="border-t border-border">
                <td className="p-2 text-muted" title={row.hint}>{row.label}</td>
                {readings.map((r) => (
                  <td key={r.id} className={cn("p-2 tabular-nums", r.id === comparison.best_id && "bg-accent-bg", row.best && readings.length > 1 && row.best(r) === top && "font-semibold")}>
                    {row.cell(r, r.baseline_id ? byId[r.baseline_id] : undefined)}
                  </td>
                ))}
              </tr>
            );
          })}
          <tr className="border-t border-border align-top">
            <td className="p-2 text-muted">First impression</td>
            {readings.map((r) => (
              <td key={r.id} className={cn("p-2 text-[11px] text-muted", r.id === comparison.best_id && "bg-accent-bg")}>{r.impression || "—"}</td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}

const CHANGE_STYLE: Record<AreaChange["change"], { title: string; tone: string; icon: ReactNode }> = {
  better: { title: "Improved", tone: "text-good", icon: <ArrowUp size={11} /> },
  worse: { title: "Worse", tone: "text-warn", icon: <ArrowDown size={11} /> },
  same: { title: "No change", tone: "text-faint", icon: <Minus size={11} /> },
};

const VERDICT_STYLE: Record<Exclude<VersionReading["verdict"], "">, { title: string; tone: string }> = {
  better: { title: "Meaningful improvement", tone: "text-good" },
  worse: { title: "Reads worse", tone: "text-warn" },
  mixed: { title: "Mixed", tone: "text-warn" },
  same: { title: "No meaningful change", tone: "text-faint" },
};

function Changes({ reading, original }: { reading: VersionReading; original?: VersionReading }) {
  if (!reading.areas.length) return null;
  const verdict = reading.verdict ? VERDICT_STYLE[reading.verdict] : null;
  return (
    <div className="rounded-md border border-border p-3">
      <p className="mb-1 text-[12px]">
        <span className="font-semibold">{reading.label}</span>
        <span className="text-muted"> against {original?.label ?? "its original"}</span>
        {verdict && <span className={cn("ml-2 font-medium", verdict.tone)}>{verdict.title}</span>}
      </p>
      {reading.verdict_reason && <p className="mb-1 text-[11px] text-muted">{reading.verdict_reason}</p>}
      <p className="mb-2 text-[11px] text-faint">
        {reading.rewritten} line{reading.rewritten === 1 ? "" : "s"} reworded · {reading.rejected_changes} rewrite
        {reading.rejected_changes === 1 ? "" : "s"} rejected by the guards · {reading.left_out} left out
      </p>
      <div className="grid gap-3 sm:grid-cols-3">
        {(["better", "worse", "same"] as const).map((kind) => {
          const items = reading.areas.filter((a) => a.change === kind);
          const style = CHANGE_STYLE[kind];
          return (
            <div key={kind} className="min-w-0 text-[11px]">
              <p className={cn("mb-1 flex items-center gap-1 font-medium", style.tone)}>{style.icon} {style.title} ({items.length})</p>
              {items.length === 0 && <p className="text-faint">—</p>}
              {items.map((a) => (
                <p key={a.area} className="text-muted">
                  <span className="text-fg">{a.area}</span>
                  {kind === "same" ? (a.after ? `: ${a.after}` : "") : `: ${a.before || "—"} → ${a.after || "—"}`}
                </p>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** What changing one setting did, between two versions that differ only in it. */
function SettingEffects({ effects }: { effects: SettingEffect[] }) {
  return (
    <div className="rounded-md border border-border p-3 text-[12px]">
      <p className="mb-1 font-medium">Did the settings matter?</p>
      {effects.length === 0 ? (
        <p className="text-[11px] text-faint">
          To see what a setting does (for example Level), tailor two versions that differ only in that setting and
          compare them together.
        </p>
      ) : (
        effects.map((e) => (
          <p key={`${e.from_id}-${e.to_id}`} className="text-[11px] text-muted">
            <span className={cn("font-medium", e.effect === "clear" ? "text-fg" : "text-faint")}>
              {e.effect === "clear" ? "Clear effect" : "No real effect"}
            </span>{" "}
            · {e.text}
          </p>
        ))
      )}
    </div>
  );
}

/** Rows (requirements or keywords) against CVs: shown or not, rows that differ first. */
function Grid({ title, items, has, readings, open, quote }: {
  title: string;
  items: string[];
  has: (r: VersionReading, index: number) => boolean;
  readings: VersionReading[];
  open?: boolean;
  /** The CV line behind a tick, shown on hover. */
  quote?: (r: VersionReading, index: number) => string | undefined;
}) {
  if (!items.length) return null;
  const rowsOf = items.map((item, i) => ({ item, cells: readings.map((r) => has(r, i)) }));
  const differs = (cells: boolean[]) => cells.some((c) => c !== cells[0]);
  rowsOf.sort((a, b) => Number(differs(b.cells)) - Number(differs(a.cells)));
  const split = rowsOf.filter((r) => differs(r.cells)).length;
  return (
    <details className="rounded-md border border-border p-3" open={open}>
      <summary className="cursor-pointer text-[12px] font-medium">
        {title} <span className="font-normal text-muted">· {split} differ between the CVs</span>
      </summary>
      <div className="mt-2 overflow-x-auto scroll-thin">
        <table className="w-full min-w-[480px] border-collapse text-[11px]">
          <thead>
            <tr>
              <th className="p-1 text-left font-normal text-faint" />
              {readings.map((r) => <th key={r.id} className="p-1 text-left font-normal text-faint">{r.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {rowsOf.map(({ item, cells }) => (
              <tr key={item} className={cn("border-t border-border", differs(cells) && "bg-surface")}>
                <td className="p-1 text-fg">{item}</td>
                {cells.map((shown, i) => (
                  <td key={readings[i].id} className="p-1" title={shown ? quote?.(readings[i], items.indexOf(item)) : undefined}>
                    {shown ? <Check size={13} className="text-good" aria-label="shown" /> : <Minus size={13} className="text-faint" aria-label="not shown" />}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function Report({ comparison, downloads, onPick }: {
  comparison: VersionComparison;
  downloads: Record<string, string>;
  onPick?: (id: string) => void;
}) {
  const byId = Object.fromEntries(comparison.readings.map((r) => [r.id, r]));
  const tailored = comparison.readings.filter((r) => r.kind === "tailored");
  return (
    <div className="space-y-3">
      <div className="flex items-start gap-2 rounded-md bg-accent-bg p-3 text-[13px]">
        <Trophy size={16} className="mt-0.5 shrink-0 text-accent" />
        <div>
          <p className="text-fg">{comparison.summary}</p>
          <p className="mt-0.5 text-[11px] text-faint">Read {when(comparison.created_at)}</p>
        </div>
      </div>
      <p className="text-[11px] text-muted">
        {comparison.search_fit_score == null
          ? `${comparison.search_fit_note || "No saved search verdict was available for this job."} Scores use the search weights with neutral practicality, the same for every version.`
          : `Original ${comparison.search_fit_score}/100 is the saved search score. Tailored scores apply changes in CV evidence to that score using the same dimension weights. Location and working pattern stay fixed.`}
      </p>
      <ScoreTable comparison={comparison} downloads={downloads} onPick={onPick} />
      {tailored.length > 0 && (
        <div className="space-y-2">
          <p className="text-[12px] font-medium">What each version changed</p>
          {tailored.map((r) => <Changes key={r.id} reading={r} original={r.baseline_id ? byId[r.baseline_id] : undefined} />)}
        </div>
      )}
      <SettingEffects effects={comparison.setting_effects ?? []} />
      <Grid title="Job requirements shown" items={comparison.requirements} readings={comparison.readings}
        has={(r, i) => r.requirements_shown.includes(i + 1)} open
        quote={(r, i) => r.requirement_quotes?.[String(i + 1)]} />
      <Grid title="Job keywords found" items={comparison.keywords} readings={comparison.readings}
        has={(r, i) => r.keywords_found.includes(comparison.keywords[i])} />
      <p className="text-[11px] text-faint">
        Every CV was read {comparison.reads > 1 ? `${comparison.reads} times` : "once"} by the same recruiter model, in a
        different order each time and without being told which is the original; the median rating counts. A requirement
        or a rating above 1 counts only when the reader quoted a line that is really in that CV (hover a tick to see it).
        Differences under {comparison.margin || 5} points are treated as the same. Readings are kept, so comparing the
        same CVs again gives the same result. Your facts are the same in every version; the scores measure how clearly
        each CV shows them for this job, not your actual fit.
      </p>
    </div>
  );
}

/** Pick a job's tailored CV versions and read them beside one original from the CV library
 *  (the Master or selected CV unless the user picks another), side by side. */
export function CVVersionCompare({ open, onClose, jobId, versions, onPick }: {
  open: boolean;
  onClose: () => void;
  jobId: string;
  /** This job's tailored CVs, newest first. */
  versions: TailoredCVView[];
  /** Show this version's report in the job card. */
  onPick?: (id: string) => void;
}) {
  const qc = useQueryClient();
  const task = useTaskId();
  const saved = useQuery({ queryKey: ["cv-comparison", jobId], queryFn: () => api.cvComparison(jobId), enabled: open });
  const state = useQuery({ queryKey: ["state"], queryFn: api.state, enabled: open });
  const library = state.data?.cv_files.available ?? []; // deleted CVs are no longer listed
  const [chosen, setChosen] = useState<string[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const baseline = picked && library.some((c) => c.id === picked) ? picked : (saved.data?.baseline ?? null);
  useEffect(() => {
    if (open) {
      setChosen(versions.slice(0, MAX_CVS - 1).map((v) => v.id));
      setPicked(null);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps -- reset on opening only
  const run = useMutation({
    mutationFn: () => api.compareCVs(jobId, chosen, baseline, task.next()),
    onSuccess: (data) => qc.setQueryData(["cv-comparison", jobId], { comparison: data, stale: false }),
  });
  const total = chosen.length + 1; // the one original
  const comparison = run.data ?? (saved.data?.stale ? null : saved.data?.comparison) ?? null;
  const downloads = Object.fromEntries(versions.map((v) => [v.id, v.download_url]));
  const toggle = (id: string) => setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));
  return (
    <Modal open={open} onClose={onClose} title="Compare CV versions" id="cv-version-compare" resizable>
      <div className="space-y-3">
        <div className="rounded-md bg-surface p-3 text-[12px]">
          <p className="mb-2 text-muted">
            Choose the tailored versions to read beside one original CV. To add a version, change Level, Length,
            Emphasis or Audience on the card and tailor again.
          </p>
          <label className="mb-2 flex flex-wrap items-center gap-2">
            <span className="text-muted">Compare against</span>
            <select
              className="input max-w-full py-1 text-[12px]"
              value={baseline ?? ""}
              disabled={state.isPending || !library.length}
              onChange={(e) => setPicked(e.target.value || null)}
            >
              {!baseline && <option value="">{state.isPending ? "Loading your CVs…" : "Choose a CV"}</option>}
              {library.map((c) => (
                <option key={c.id} value={c.id}>{c.kind === "master" ? "Master CV" : c.filename}</option>
              ))}
            </select>
          </label>
          {state.error && <p className="mb-2 text-bad">{(state.error as Error).message}</p>}
          {!state.isPending && !library.length && <p className="mb-2 text-faint">No CV in your library to compare against.</p>}
          {versions.length === 0 && <p className="text-faint">No tailored CV for this job yet. Tailor one first.</p>}
          <div className="flex flex-wrap gap-2">
            {versions.map((v) => (
              <label key={v.id} className={cn("flex cursor-pointer items-center gap-1.5 rounded border px-2 py-1", chosen.includes(v.id) ? "border-accent text-fg" : "border-border text-muted")}>
                <input type="checkbox" className="accent-[var(--accent)]" checked={chosen.includes(v.id)} onChange={() => toggle(v.id)} />
                {v.label}
                {v.report?.fit_check && <span className="text-faint">· {v.report.fit_check.after}/100</span>}
              </label>
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button className="btn-primary py-1 text-[12px]" disabled={!chosen.length || !baseline || total > MAX_CVS || run.isPending} onClick={() => run.mutate()}>
              {run.isPending ? <Loader2 size={12} className="animate-spin" /> : <Scale size={12} />}
              {comparison ? "Compare again" : "Compare"} {chosen.length} version{chosen.length === 1 ? "" : "s"} with the original
            </button>
            <span className={cn("text-[11px]", total > MAX_CVS ? "text-bad" : "text-faint")}>
              {total > MAX_CVS ? `At most ${MAX_CVS} CVs in one reading, the original included` : "Three reads with the CV model; CVs read before keep their scores (no call)"}
            </span>
          </div>
          {run.isPending && <TaskProgress id={task.id} className="mt-2" />}
          <TaskError error={run.error} className="mt-2" />
        </div>
        {saved.isPending && <p className="text-[12px] text-muted">Loading the last comparison…</p>}
        {saved.error && <p className="text-[12px] text-bad">{(saved.error as Error).message}</p>}
        {!run.data && saved.data?.stale && (
          <p className="text-[12px] text-warn">The saved report uses an older comparison method. Select Compare to recalculate it.</p>
        )}
        {comparison ? (
          <Report comparison={comparison} downloads={downloads} onPick={onPick && ((id) => { onPick(id); onClose(); })} />
        ) : (
          !saved.isPending && !saved.error && <p className="text-[12px] text-faint">No comparison yet for this job.</p>
        )}
      </div>
    </Modal>
  );
}
