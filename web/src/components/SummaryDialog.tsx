import { useMutation } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { useSearch } from "../stores/searchStore";
import { Modal } from "./ui";

const LEVEL_COLOR = { expert: "var(--good)", proficient: "var(--accent)", familiar: "var(--text-muted)" };

export function SummaryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { summary, query, useCv, set } = useSearch();
  const build = useMutation({
    mutationFn: (refresh: boolean) => api.profileSummary(query, useCv, refresh),
    onSuccess: (r) => set({ summary: { summary: r.summary, fromMemory: r.from_memory } }),
  });
  const s = summary?.summary;

  return (
    <Modal open={open} onClose={onClose} title="Profile summary" wide>
      <p className="mb-3 text-[12px] text-faint">
        Built by the orchestrator from your CV and remembered per role family. The job_matcher subagent screens every
        shortlisted posting against it.
      </p>
      {!s ? (
        <button className="btn-primary" disabled={build.isPending} onClick={() => build.mutate(false)}>
          {build.isPending && <Loader2 size={14} className="animate-spin" />} Build summary for current titles
        </button>
      ) : (
        <div className="space-y-3 text-[13px]">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="font-semibold">{s.headline}</p>
              <p className="text-[12px] text-muted">
                {s.seniority} · {s.years_experience} yrs · {summary?.fromMemory ? "from memory" : "new"}
              </p>
            </div>
            <button className="btn-ghost py-1 text-[12px]" disabled={build.isPending} onClick={() => build.mutate(true)}>
              {build.isPending ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} Rebuild
            </button>
          </div>
          <p className="text-muted">{s.summary}</p>
          <Section title="Core expertise" items={s.core_expertise} />
          <div>
            <p className="label mb-1">Key skills</p>
            <div className="flex flex-wrap gap-1">
              {s.key_skills.map((k) => (
                <span key={k.skill} className="chip" title={k.evidence} style={{ color: LEVEL_COLOR[k.level] }}>
                  {k.skill} · {k.level}
                </span>
              ))}
            </div>
          </div>
          <Section title="Target roles" items={s.target_roles} />
          <Section title="Stretch roles" items={s.stretch_roles} />
          <Section title="Not a fit" items={s.not_a_fit} />
          <Section title="Domains" items={s.domains} />
        </div>
      )}
      {build.error && <p className="mt-2 text-[12px] text-bad">{(build.error as Error).message}</p>}
    </Modal>
  );
}

function Section({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div>
      <p className="label mb-1">{title}</p>
      <ul className="list-disc space-y-0.5 pl-4 text-[12px] text-muted">
        {items.map((i) => (
          <li key={i}>{i}</li>
        ))}
      </ul>
    </div>
  );
}
