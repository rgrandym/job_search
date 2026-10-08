import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BarChart3, ChevronRight, ClipboardList, ExternalLink, Lightbulb, Loader2, Plus, Tags, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { HeadToHead, OutcomeStage, SetupAgreement, TrackedJob } from "../lib/types";
import { STAGES } from "./JobCard";
import { LearnFromLabels } from "./LearnFromLabels";
import { Empty, Modal } from "./ui";

// Where postings came from (backend `history.source_origin`): boards by name, alerts by origin.
const ORIGIN_LABELS: Record<string, string> = {
  linkedin_search: "LinkedIn search",
  linkedin_alert: "LinkedIn alerts",
  linkedin_saved: "LinkedIn (saved)",
  indeed_alert: "Indeed alerts",
  indeed_saved: "Indeed (saved)",
  reed: "Reed",
  cv_library: "CV-Library",
  adzuna: "Adzuna",
  totaljobs: "Totaljobs",
  jobs_ac_uk: "jobs.ac.uk",
  nhs_jobs: "NHS Jobs",
  biotechnologyjobs: "Biotechnology Jobs",
  company: "Company career sites",
  example: "Demo jobs",
};

type Editable = "open" | "applied" | "na";
type Edit = { status?: Editable; note?: string; reason?: string; stage?: OutcomeStage };

/** Sidebar card: the applications register (what searches skip) and source yield. */
export function TrackerPanel() {
  const [open, setOpen] = useState<"register" | "yield" | "review" | "labels" | null>(null);
  return (
    <details className="card group p-3" open>
      <summary className="label flex cursor-pointer list-none items-center gap-1 [&::-webkit-details-marker]:hidden"><ChevronRight size={13} className="group-open:rotate-90" /> Applications &amp; sources</summary>
      <div className="mt-2 space-y-2">
      <div className="flex flex-col gap-1.5">
        <button className="btn-ghost w-full justify-start text-[12px]" onClick={() => setOpen("register")}>
          <ClipboardList size={13} className="shrink-0" /> <span className="truncate">Applications</span>
        </button>
        <button className="btn-ghost w-full justify-start text-[12px]" onClick={() => setOpen("yield")}>
          <BarChart3 size={13} className="shrink-0" /> <span className="truncate">Source yield</span>
        </button>
        <button className="btn-ghost w-full justify-start text-[12px]" onClick={() => setOpen("review")}>
          <Lightbulb size={13} className="shrink-0" /> <span className="truncate">Outcome review</span>
        </button>
        <button className="btn-ghost w-full justify-start text-[12px]" onClick={() => setOpen("labels")}>
          <Tags size={13} className="shrink-0" /> <span className="truncate">Your labels</span>
        </button>
      </div>
      <p className="text-[11px] text-faint">
        Jobs you mark Applied (or N/A) are set aside in later searches.
      </p>
      </div>
      <Modal open={open === "register"} onClose={() => setOpen(null)} title="Applications" wide>
        <Register />
      </Modal>
      <Modal open={open === "yield"} onClose={() => setOpen(null)} title="Source yield" wide>
        <Yield />
      </Modal>
      <Modal open={open === "review"} onClose={() => setOpen(null)} title="Outcome review" wide>
        <Review />
      </Modal>
      <Modal open={open === "labels"} onClose={() => setOpen(null)} title="Your labels vs the models" wide>
        <Labels />
      </Modal>
    </details>
  );
}

function Register() {
  const qc = useQueryClient();
  const register = useQuery({ queryKey: ["tracker"], queryFn: api.tracker });
  const refresh = () => qc.invalidateQueries({ queryKey: ["tracker"] });
  const edit = useMutation({
    meta: { syncLists: true },
    mutationFn: (v: Edit & { id: string }) =>
      api.editTracked(v.id, v.status, v.note, { reason: v.reason, stage: v.stage }),
    onSuccess: refresh,
  });
  const remove = useMutation({ meta: { syncLists: true }, mutationFn: api.deleteTracked, onSuccess: refresh });
  const error = (edit.error ?? remove.error ?? register.error) as Error | null;
  const items = register.data ?? [];

  return (
    <div className="space-y-3 text-[12px]">
      <AddApplication onAdded={refresh} />
      {error && <p className="text-bad">{error.message}</p>}
      {register.isPending ? (
        <p className="flex items-center gap-1 text-faint">
          <Loader2 size={12} className="animate-spin" /> Loading…
        </p>
      ) : items.length === 0 ? (
        <Empty title="No applications yet">
          Mark a job <b>Applied</b> or <b>N/A</b> in the results, or add one above.
        </Empty>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((e) => (
            <RegisterRow
              key={`${e.id}:${e.note}:${e.reason ?? ""}`}
              entry={e}
              busy={edit.isPending || remove.isPending}
              onEdit={(change) => edit.mutate({ id: e.id, ...change })}
              onDelete={() => remove.mutate(e.id)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function RegisterRow({
  entry,
  busy,
  onEdit,
  onDelete,
}: {
  entry: TrackedJob;
  busy: boolean;
  onEdit: (change: Edit) => void;
  onDelete: () => void;
}) {
  const [note, setNote] = useState(entry.note);
  const [reason, setReason] = useState(entry.reason ?? "");
  const status: Editable = entry.status === "seen" ? "open" : entry.status;
  return (
    <li className="space-y-1 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 font-medium">
          {entry.title} <span className="font-normal text-muted">· {entry.company}</span>
        </span>
        {entry.url && (
          <a href={entry.url} target="_blank" rel="noreferrer" className="text-faint hover:text-fg" title="Open the posting">
            <ExternalLink size={12} />
          </a>
        )}
        <select
          className="input w-auto py-0.5 text-[11px]"
          value={status}
          disabled={busy}
          onChange={(e) => onEdit({ status: e.target.value as Editable })}
          title="Open removes it from this list: searches rank it again"
        >
          <option value="applied">Applied</option>
          <option value="na">N/A</option>
          <option value="open">Open</option>
        </select>
        {status === "applied" && (
          <select
            className="input w-auto py-0.5 text-[11px]"
            value={entry.stage ?? ""}
            disabled={busy}
            onChange={(e) => e.target.value && onEdit({ stage: e.target.value as OutcomeStage })}
            title="How the application went"
          >
            <option value="">No news yet</option>
            {STAGES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        )}
        <button className="text-faint hover:text-bad" disabled={busy} title="Forget this job" onClick={onDelete}>
          <Trash2 size={12} />
        </button>
      </div>
      <p className="text-[11px] text-faint">
        {entry.applied_at ? `Applied ${entry.applied_at}` : `Last seen ${entry.last_seen}`}
        {entry.cv_file && ` · CV ${entry.cv_file}`}
        {entry.fit_score != null && ` · fit ${entry.fit_score}`}
        {entry.family && ` · ${entry.family}`}
        {!!entry.stages?.length && ` · ${entry.stages.map((st) => `${st.stage.replace("_", " ")} ${st.at}`).join(" → ")}`}
      </p>
      <input
        className="input py-0.5 text-[11px]"
        placeholder="Your note"
        value={note}
        maxLength={200}
        onChange={(e) => setNote(e.target.value)}
        onBlur={() => note.trim() !== entry.note && onEdit({ note: note.trim() })}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      <input
        className="input py-0.5 text-[11px]"
        placeholder={status === "na" ? "Why it was not for you (feeds the outcome review)" : "Why it suits you (optional)"}
        value={reason}
        maxLength={200}
        onChange={(e) => setReason(e.target.value)}
        onBlur={() => reason.trim() !== (entry.reason ?? "") && onEdit({ reason: reason.trim() })}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
    </li>
  );
}

function AddApplication({ onAdded }: { onAdded: () => void }) {
  const [form, setForm] = useState({ title: "", company: "", url: "", note: "" });
  const add = useMutation({
    meta: { syncLists: true },
    mutationFn: () =>
      api.addApplication({
        title: form.title.trim(),
        company: form.company.trim(),
        url: form.url.trim() || undefined,
        note: form.note.trim(),
      }),
    onSuccess: () => {
      setForm({ title: "", company: "", url: "", note: "" });
      onAdded();
    },
  });
  const field = (key: keyof typeof form, placeholder: string) => (
    <input
      className="input py-1 text-[12px]"
      placeholder={placeholder}
      value={form[key]}
      onChange={(e) => setForm({ ...form, [key]: e.target.value })}
    />
  );
  return (
    <form
      className="space-y-2 rounded-md border border-border bg-surface p-2"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <p className="text-[11px] text-muted">Applied somewhere outside the app? Add it so searches skip that role.</p>
      <div className="grid grid-cols-2 gap-2">
        {field("title", "Job title")}
        {field("company", "Employer")}
        {field("url", "Link (optional)")}
        {field("note", "Note (optional)")}
      </div>
      <button className="btn-primary py-1 text-[12px]" disabled={add.isPending || !form.title.trim() || !form.company.trim()}>
        {add.isPending ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} Add application
      </button>
      {add.error && <p className="text-[11px] text-bad">{(add.error as Error).message}</p>}
    </form>
  );
}

function Yield() {
  const data = useQuery({ queryKey: ["yield"], queryFn: api.sourceYield });
  if (data.isPending) {
    return (
      <p className="flex items-center gap-1 text-[12px] text-faint">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </p>
    );
  }
  if (data.error) return <p className="text-[12px] text-bad">{(data.error as Error).message}</p>;
  const rows = data.data ?? [];
  if (!rows.length) return <Empty title="No searches logged yet">Run a search; each one adds its counts here.</Empty>;
  return (
    <div className="space-y-2 text-[12px]">
      <p className="text-[11px] text-muted">
        Unique postings and true matches each source produced over your recent searches (up to 200). A source
        that runs often and never yields a match is a candidate to switch off.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead className="text-[11px] text-faint">
            <tr>
              <th className="py-1 pr-2 font-medium">Source</th>
              <th className="py-1 pr-2 text-right font-medium">Searches</th>
              <th className="py-1 pr-2 text-right font-medium">Postings</th>
              <th className="py-1 pr-2 text-right font-medium">Matches</th>
              <th className="py-1 pr-2 text-right font-medium">Match rate</th>
              <th className="py-1 font-medium">Last match</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((y) => (
              <tr key={y.source}>
                <td className="py-1 pr-2">{ORIGIN_LABELS[y.source] ?? y.source}</td>
                <td className="py-1 pr-2 text-right text-muted">{y.searches}</td>
                <td className="py-1 pr-2 text-right text-muted">{y.found}</td>
                <td className={`py-1 pr-2 text-right font-medium ${y.matches ? "text-good" : "text-faint"}`}>{y.matches}</td>
                <td className="py-1 pr-2 text-right text-muted">
                  {y.found ? `${Math.round((y.matches / y.found) * 100)}%` : "–"}
                </td>
                <td className="py-1 text-faint">{y.last_match ?? "never"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Funnel, quiet applications and patterns: suggestions only, nothing changes on its own. */
function Review() {
  const data = useQuery({ queryKey: ["outcomes"], queryFn: api.outcomesReview });
  if (data.isPending) {
    return (
      <p className="flex items-center gap-1 text-[12px] text-faint">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </p>
    );
  }
  if (data.error) return <p className="text-[12px] text-bad">{(data.error as Error).message}</p>;
  const r = data.data;
  if (!r.applications && !r.high_fit_dismissals && !r.patterns.length)
    return (
      <Empty title="Nothing to review yet">
        Record outcomes on your applications and say why you rule out strong matches; patterns appear here after a few cases.
      </Empty>
    );
  return (
    <div className="space-y-3 text-[12px]">
      <p className="text-[11px] text-muted">
        Read-only. Patterns need at least 3 cases, and hiring outcomes are noisy: treat these as prompts to review your career
        intent or role families (you, or the assistant on your word, make any change).
      </p>
      <div>
        <p className="label mb-1">Applications · {r.applications}</p>
        <div className="flex flex-wrap gap-1">
          {Object.entries(r.by_stage).map(([stage, n]) => (
            <span key={stage} className="chip">
              {stage.replace("_", " ")}: {n}
            </span>
          ))}
        </div>
      </div>
      {r.quiet.length > 0 && (
        <div>
          <p className="label mb-1">Gone quiet (no news after 14 days)</p>
          {r.quiet.map((q) => (
            <p key={q} className="text-muted">
              · {q}
            </p>
          ))}
        </div>
      )}
      <div>
        <p className="label mb-1">Patterns · {r.high_fit_dismissals} strong matches ruled out with a reason</p>
        {r.patterns.length === 0 ? (
          <p className="text-faint">No recurring pattern yet.</p>
        ) : (
          <ul className="space-y-2">
            {r.patterns.map((p) => (
              <li key={`${p.kind}:${p.label}`} className="rounded-md border border-border p-2">
                <p className="text-fg">{p.suggestion}</p>
                {p.examples.map((x) => (
                  <p key={x} className="text-[11px] text-faint">
                    {x}
                  </p>
                ))}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

const pct = (v: number | null) => (v == null ? "–" : `${Math.round(v * 100)}%`);

function Progress({ label, value, target }: { label: string; value: number; target: number }) {
  return (
    <div className="grid grid-cols-[88px_1fr_56px] items-center gap-2 text-[11px]">
      <span className="text-muted">{label}</span>
      <div className="h-1.5 overflow-hidden rounded-full bg-surface">
        <div className="h-full rounded-full bg-accent" style={{ width: `${Math.min(1, value / target) * 100}%` }} />
      </div>
      <span className="text-right text-muted">
        {value}/{target}
      </span>
    </div>
  );
}

function Scores({ s }: { s: SetupAgreement }) {
  return (
    <div className="flex flex-wrap gap-1">
        <span className="chip" title="Your yes/no labels where this setup made the same decision">
          Agreement {pct(s.agreement)}
        </span>
        <span className="chip text-good" title="Your 'would apply' jobs it matched">
          Good jobs kept {s.kept_yes}/{s.yes}
        </span>
        <span className="chip text-warn" title="Your 'no' jobs it matched anyway">
          Let through {s.passed_no}/{s.no}
        </span>
        <span className="chip" title="Share of (yes, no) pairs where your 'yes' job got the higher fit score">
          Ranking {pct(s.ranking)}
        </span>
    </div>
  );
}

/** Two setups on the jobs both judged: the fair way to compare them. */
function Pair({ p }: { p: HeadToHead }) {
  return (
    <li className="space-y-1.5 rounded-md border border-border p-2">
      <p className="text-faint">On the same {p.shared} labelled jobs</p>
      {[p.first, p.second].map((s) => (
        <div key={s.models} className="space-y-0.5">
          <p className="text-fg">{s.models}</p>
          <Scores s={s} />
        </div>
      ))}
      {p.split.length > 0 && <p className="label pt-1">Where they decided differently</p>}
      {p.split.map((line) => (
        <p key={line} className="text-[11px] text-faint">
          {line}
        </p>
      ))}
    </li>
  );
}

function Setup({ s }: { s: SetupAgreement }) {
  return (
    <li className="space-y-1 rounded-md border border-border p-2">
      <p className="text-fg">{s.models}</p>
      <Scores s={s} />
      {s.misses.map((m) => (
        <p key={`miss:${m}`} className="text-[11px] text-faint">
          Missed: {m}
        </p>
      ))}
      {s.false_accepts.map((m) => (
        <p key={`pass:${m}`} className="text-[11px] text-faint">
          Let through: {m}
        </p>
      ))}
    </li>
  );
}

/** Your labels: progress to a usable set, and how each model setup's verdicts compare. */
function Labels() {
  const data = useQuery({ queryKey: ["labels"], queryFn: api.labels });
  if (data.isPending) {
    return (
      <p className="flex items-center gap-1 text-[12px] text-faint">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </p>
    );
  }
  if (data.error) return <p className="text-[12px] text-bad">{(data.error as Error).message}</p>;
  const r = data.data;
  if (!r.total)
    return (
      <Empty title="No labels yet">
        On a job card, set <b>Your call</b>: Would apply, Maybe or No. Judge from the posting itself, not the AI score. Label
        strong matches, borderline jobs and plausible rejections, not just obvious misses.
      </Empty>
    );
  return (
    <div className="space-y-3 text-[12px]">
      <p className="text-[11px] text-muted">
        Labels only measure the models; they never change a search. Stored in <code>data/job_labels.json</code> with each
        posting, once per posting even when it appears on several boards or in several searches. Every setup that judged a
        labelled job adds its verdict, from new searches and from past ones in the history. Each label carries the job's
        note (the note box on its card), kept up to date. "Maybe" is left out of the numbers.
      </p>
      <div className="space-y-1">
        <p className="label">
          {r.total} labels · {r.yes} would apply · {r.maybe} maybe · {r.no} no
        </p>
        <Progress label="All labels" value={r.total} target={r.target_total} />
        <Progress label="Would apply" value={r.yes} target={r.target_each} />
        <Progress label="No" value={r.no} target={r.target_each} />
        <p className={r.ready ? "text-good" : "text-faint"}>
          {r.ready
            ? "Enough labels to compare model setups."
            : `Until ${r.target_total} labels (at least ${r.target_each} of each), read the numbers below as hints.`}
        </p>
      </div>
      {r.head_to_head.length > 0 && (
        <div>
          <p className="label mb-1">Head to head (compare setups here: same jobs for both)</p>
          <ul className="space-y-2">
            {r.head_to_head.map((p) => (
              <Pair key={`${p.first.models}|${p.second.models}`} p={p} />
            ))}
          </ul>
        </div>
      )}
      <div>
        <p className="label mb-1">Each setup on all the labelled jobs it judged (different jobs per setup)</p>
        {r.setups.length === 0 ? (
          <p className="text-faint">None of your labelled jobs had an AI verdict yet.</p>
        ) : (
          <ul className="space-y-2">
            {r.setups.map((s) => (
              <Setup key={s.models} s={s} />
            ))}
          </ul>
        )}
      </div>
      <LearnFromLabels />
    </div>
  );
}
