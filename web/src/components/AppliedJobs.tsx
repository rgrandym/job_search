import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { Fragment, useState } from "react";
import { api } from "../lib/api";
import type { MatchResult, OutcomeStage, TrackedJob } from "../lib/types";
import { JobCard } from "./JobCard";
import { Empty } from "./ui";

const CLOSED_STAGES = new Set(["accepted", "rejected", "withdrawn"]);
const displayDate = (value: string | null) => value || "—";
const closureDate = (entry: TrackedJob) =>
  entry.stage && CLOSED_STAGES.has(entry.stage)
    ? [...(entry.stages ?? [])].reverse().find((event) => event.stage === entry.stage)?.at ?? null
    : null;
const responseValue = (entry: TrackedJob) => entry.stage ?? "screening";

function applicationResult(entry: TrackedJob): MatchResult {
  const result: MatchResult = entry.application_result ?? {
    job: {
      id: entry.job_id ?? `manual:${entry.id}`,
      title: entry.title,
      company: entry.company,
      location: entry.location,
      url: entry.url,
      source: entry.source,
      work_arrangement: "onsite",
      description: "",
      required_skills: [],
      preferred_skills: [],
      salary_range: null,
      salary_min: null,
      salary_max: null,
      posted_at: null,
    },
    excluded: false,
    exclusion_reasons: [],
    similarity: null,
    score: null,
    verdict: null,
    passed: false,
  };
  return {
    ...result,
    tracking: {
      status: "applied",
      note: entry.note,
      first_seen: entry.first_seen,
      applied_at: entry.applied_at,
      cv_file: entry.cv_file,
      related: null,
      reason: entry.reason,
      stage: entry.stage,
    },
  };
}

/** Application register: compact by default, with the saved job card on demand. */
export function AppliedJobs({ query }: { query: UseQueryResult<TrackedJob[]> }) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const qc = useQueryClient();
  const response = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: OutcomeStage }) => api.editTracked(id, undefined, undefined, { stage }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["tracker"] }),
  });
  if (query.isPending) return <Empty title="Loading applications…" />;
  if (query.isError) return <Empty title="Could not load applications">{(query.error as Error).message}</Empty>;
  const items = (query.data ?? []).filter((entry) => entry.status === "applied");
  if (!items.length) return <Empty title="No applied jobs yet">Mark a job Applied or add an application in the sidebar.</Empty>;
  return (
    <div className="h-full overflow-auto scroll-thin p-4">
      <div className="overflow-x-auto rounded-lg border border-border bg-panel">
        {response.isError && <p role="alert" className="px-3 py-2 text-[12px] text-bad">Could not save response: {(response.error as Error).message}</p>}
        <table className="w-full min-w-[820px] border-collapse text-left text-[12px]">
          <thead className="bg-surface text-[11px] text-muted">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">Role</th>
              <th scope="col" className="px-3 py-2 font-medium">Company</th>
              <th scope="col" className="px-3 py-2 font-medium">Source</th>
              <th scope="col" className="whitespace-nowrap px-3 py-2 font-medium">Applied</th>
              <th scope="col" className="px-3 py-2 font-medium">Response</th>
              <th scope="col" className="whitespace-nowrap px-3 py-2 font-medium">Closed</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.map((entry) => {
              const expanded = expandedId === entry.id;
              return (
                <Fragment key={entry.id}>
                  <tr className={expanded ? "bg-accent-bg" : "hover:bg-surface"}>
                    <td className="max-w-[280px] px-3 py-2 font-medium">
                      <button
                        type="button"
                        className="flex items-center gap-1 text-left text-accent hover:underline"
                        aria-expanded={expanded}
                        onClick={() => setExpandedId(expanded ? null : entry.id)}
                      >
                        {expanded ? <ChevronDown size={13} className="shrink-0" /> : <ChevronRight size={13} className="shrink-0" />}
                        <span>{entry.title}</span>
                      </button>
                    </td>
                    <td className="px-3 py-2 text-fg">{entry.company}</td>
                    <td className="px-3 py-2 text-muted">{entry.source === "manual" ? "Added manually" : entry.source.replaceAll("_", " ")}</td>
                    <td className="whitespace-nowrap px-3 py-2 text-muted">{displayDate(entry.applied_at)}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1">
                        <select
                          className="input min-w-[145px] py-1 text-[12px]"
                          aria-label={`Response for ${entry.title} at ${entry.company}`}
                          value={responseValue(entry)}
                          disabled={response.isPending}
                          onChange={(event) => response.mutate({ id: entry.id, stage: event.target.value as OutcomeStage })}
                        >
                          <option value="screening">Waiting</option>
                          <option value="interview">Invited for interview</option>
                          <option value="rejected">Rejected</option>
                          {entry.stage && !["screening", "interview", "rejected"].includes(entry.stage) && (
                            <option value={entry.stage}>{entry.stage.replaceAll("_", " ")}</option>
                          )}
                        </select>
                        {response.isPending && response.variables?.id === entry.id && <Loader2 size={12} className="shrink-0 animate-spin text-faint" />}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-muted">{displayDate(closureDate(entry))}</td>
                  </tr>
                  {expanded && (
                    <tr>
                      <td colSpan={6} className="bg-bg p-3">
                        <JobCard result={applicationResult(entry)} canTailor={false} labelable={false} />
                        <p className="mt-1 px-2 text-[11px] text-faint">
                          Applied {displayDate(entry.applied_at)}
                          {entry.fit_score != null && ` · fit ${entry.fit_score}`}
                          {entry.family && ` · ${entry.family}`}
                          {!!entry.stages?.length && ` · ${entry.stages.map((event) => `${event.stage.replaceAll("_", " ")} ${event.at}`).join(" → ")}`}
                        </p>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
