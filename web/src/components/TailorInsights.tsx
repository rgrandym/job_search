import type { FitComparison, MatchClass, Positioning, RequirementEvidence } from "../lib/types";
import { cn } from "../lib/utils";

const DIMENSION_LABEL: Record<string, string> = {
  function: "Function",
  domain: "Domain",
  seniority: "Seniority",
  leadership: "Leadership",
  sector: "Sector",
};

const MATCH_LABEL: Record<MatchClass, [string, string]> = {
  direct: ["direct", "text-good"],
  transferable: ["transfers", "text-accent"],
  adjacent: ["related", "text-accent"],
  needs_confirmation: ["confirm?", "text-faint"],
  not_evidenced: ["gap", "text-warn"],
};

const pct = (v: number) => `${Math.round(v * 100)}%`;

function Change({ before, after, text }: { before: number; after: number; text?: string }) {
  const tone = after > before ? "text-good" : after < before ? "text-warn" : "text-muted";
  return <span className={tone}>{text ?? `${before} → ${after}`}</span>;
}

/** One line under the job: how much better the tailored CV shows the fit. */
export function FitCheckSummary({ check }: { check: FitComparison | null | undefined }) {
  if (!check) return <span className="text-faint">Before/after fit check not available for this draft</span>;
  return (
    <span className="text-muted" title="Both CVs read for this job by a recruiter model that does not know which is which. Your actual fit is the same (same facts); this measures how clearly each CV shows it.">
      {check.search_anchored ? "Search fit, adjusted for CV evidence" : "CV evidence shown"}: <Change before={check.before} after={check.after} text={`${check.before} → ${check.after}`} />/100
      {check.requirements_total > 0 && (
        <>
          {" · requirements shown "}
          <Change before={check.requirements_before} after={check.requirements_after} text={`${check.requirements_before} → ${check.requirements_after} of ${check.requirements_total}`} />
        </>
      )}
    </span>
  );
}

/** The before/after reading in detail. */
export function FitCheckDetails({ check }: { check: FitComparison }) {
  return (
    <div className="basis-full space-y-1 text-[11px]">
      <p className="text-muted">Before / after, read for this job (0-4 per dimension):</p>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5">
        {check.dimensions.map((d) => (
          <span key={d.name} className="text-faint">
            {DIMENSION_LABEL[d.name] ?? d.name} <Change before={d.before} after={d.after} />
          </span>
        ))}
        <span className="text-faint">
          Keywords <Change before={check.keywords_before} after={check.keywords_after} text={`${pct(check.keywords_before)} → ${pct(check.keywords_after)}`} />
        </span>
        <span className="text-faint">
          On the first page <Change before={check.first_page_before} after={check.first_page_after} text={`${pct(check.first_page_before)} → ${pct(check.first_page_after)}`} />
        </span>
      </div>
      {check.impression_after && <p className="text-faint">Top of the tailored CV: {check.impression_after}</p>}
      {check.impression_before && <p className="text-faint">Top of the original: {check.impression_before}</p>}
      {check.worse.length > 0 && <p className="text-warn">Reads worse on: {check.worse.join("; ")}</p>}
    </div>
  );
}

/** How the CV presents the candidate, and the job's requirements against the evidence. */
export function PositioningDetails({ positioning, requirements }: { positioning?: Positioning | null; requirements?: RequirementEvidence[] }) {
  const rows = requirements ?? [];
  if (!positioning && rows.length === 0) return null;
  const gaps = rows.filter((r) => r.match === "not_evidenced" || r.match === "needs_confirmation");
  return (
    <details className="basis-full text-[11px] text-faint">
      <summary className="cursor-pointer">
        Positioning and requirements{rows.length > 0 && `: ${rows.length - gaps.length} of ${rows.length} backed by your CV`}
        {gaps.length > 0 && `, ${gaps.length} to discuss`}
      </summary>
      {positioning && (
        <div className="mt-1 space-y-0.5">
          <p className="text-fg">{positioning.thesis}</p>
          {positioning.differentiators.map((d) => <p key={d}>+ {d}</p>)}
          {positioning.transfer && <p>Carries over: {positioning.transfer}</p>}
          {positioning.level && <p>Level: {positioning.level}</p>}
        </div>
      )}
      {rows.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {rows.map((r) => {
            const [label, tone] = MATCH_LABEL[r.match];
            return (
              <p key={r.requirement}>
                <span className={cn("inline-block w-16", tone)}>{label}</span>
                {r.importance === "key" && <span className="mr-1 font-semibold text-fg">Key:</span>}
                {r.requirement}
                {r.priority === "desirable" && " (desirable)"}
                {r.note && <span className="text-faint"> · {r.note}</span>}
              </p>
            );
          })}
          {gaps.some((r) => r.match === "needs_confirmation") && (
            <p className="text-muted">"confirm?" items are not on the CV. If you have that experience, add it to your CV first, then tailor again.</p>
          )}
        </div>
      )}
    </details>
  );
}
