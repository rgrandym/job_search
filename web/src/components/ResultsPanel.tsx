import { AlertTriangle, BrainCircuit, Loader2 } from "lucide-react";
import { useState } from "react";
import type { MatchResult } from "../lib/types";
import { cn } from "../lib/utils";
import { useChat } from "../stores/chatStore";
import { useSearch } from "../stores/searchStore";
import { JobCard } from "./JobCard";
import { Empty } from "./ui";

type Tab = "matches" | "below_threshold" | "excluded";

export function ResultsPanel({ hasCv }: { hasCv: boolean }) {
  const { outcome, loading, progress, error, query, useCv } = useSearch();
  const chat = useChat();
  const [tab, setTab] = useState<Tab>("matches");

  const ask = (r: MatchResult) =>
    chat.send(
      `Tell me about job ${r.job.id} (${r.job.title} at ${r.job.company}): is it a real fit for me, and what should I emphasise?`,
      query,
      useCv,
    );

  if (!outcome) {
    return (
      <div className="relative h-full">
        {loading ? (
          <Empty title={progress ?? "Searching…"}>
            <Loader2 className="mx-auto mt-2 animate-spin" size={18} />
          </Empty>
        ) : (
          <Empty title={error ?? "No search yet"}>
            Set titles, location, radius and salary on the left, then <b>Search</b>. Or ask the agent
            to find roles that truly match your CV.
          </Empty>
        )}
      </div>
    );
  }

  const rep = outcome.report;
  const counts: Record<Tab, number> = {
    matches: rep.matches.length,
    below_threshold: rep.below_threshold.length,
    excluded: rep.excluded.length,
  };
  const items = rep[tab];
  const problems = { ...outcome.skipped_sources, ...outcome.errors };

  return (
    <div className="flex h-full flex-col">
      <header className="space-y-2 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
          <span>
            <b className="text-fg">{outcome.fetched}</b> postings
          </span>
          <span>
            <b className="text-good">{counts.matches}</b> {rep.screened ? "true matches" : "above threshold"}
          </span>
          {rep.screened ? (
            <span className="flex items-center gap-1">
              <BrainCircuit size={13} className="text-accent" /> screened by job_matcher
              {outcome.summary_from_memory != null && (
                <span className="text-faint">
                  · profile summary {outcome.summary_from_memory ? "from memory" : "new"}
                </span>
              )}
            </span>
          ) : (
            <span className="text-faint">keyword pre-filter only</span>
          )}
          <span className="text-faint">{outcome.seconds}s</span>
          {(loading || progress) && (
            <span className="flex items-center gap-1 text-accent">
              <Loader2 size={12} className="animate-spin" /> {progress ?? "Updating…"}
            </span>
          )}
        </div>
        {outcome.smart_unavailable && (
          <p className="flex items-center gap-1 text-[12px] text-warn">
            <AlertTriangle size={12} /> {outcome.smart_unavailable}
          </p>
        )}
        {Object.keys(problems).length > 0 && (
          <details className="text-[11px] text-faint">
            <summary className="cursor-pointer">{Object.keys(problems).length} source notice(s)</summary>
            {Object.entries(problems).map(([k, v]) => (
              <p key={k}>
                <b>{k}</b>: {v}
              </p>
            ))}
          </details>
        )}
        <nav className="flex gap-1">
          {(["matches", "below_threshold", "excluded"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cn(
                "rounded-md px-2.5 py-1 text-[12px]",
                tab === t ? "bg-accent-bg text-fg" : "text-muted hover:text-fg",
              )}
            >
              {t === "matches" ? "Matches" : t === "below_threshold" ? "Not selected" : "Excluded"}{" "}
              <span className="text-faint">{counts[t]}</span>
            </button>
          ))}
        </nav>
      </header>
      <div className="flex-1 space-y-2.5 overflow-y-auto p-4 scroll-thin">
        {items.length === 0 ? (
          <Empty title={tab === "matches" ? "No true matches" : "Nothing here"}>
            {tab === "matches" &&
              "Try adjacent titles, a wider radius, a lower salary floor, or more sources, or ask the agent to refine the search."}
          </Empty>
        ) : (
          items.map((r) => <JobCard key={r.job.id} result={r} canTailor={hasCv} onAsk={ask} />)
        )}
      </div>
    </div>
  );
}
