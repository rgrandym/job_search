import type { FitComparison, MatchClass, Positioning, ProjectedFit, RequirementEvidence } from "../lib/types";
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
export function FitCheckSummary({ check, capReason }: { check: FitComparison | null | undefined; capReason?: string | null }) {
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
      {check.ats_before != null && check.ats_after != null && (
        <span title="ATS match: the share of the job's keywords the CV contains and of its requirements a checked reading found shown, as screening software scores it">
          {" · ATS match "}
          <Change before={check.ats_before} after={check.ats_after} text={`${check.ats_before} → ${check.ats_after}`} />
        </span>
      )}
      {capReason && check.after === check.before && (
        <span className="text-warn"> · search score limit still applies</span>
      )}
    </span>
  );
}

/** An optimised draft's pending proposals: what confirming them would show, and which
 * proposal shows each newly met requirement. Hypothetical, never the draft's own score. */
export function IfConfirmed({ projection, labels }: { projection: ProjectedFit; labels?: Record<string, string> }) {
  const p = projection;
  const moved = p.dimensions.filter((d) => d.before !== d.after);
  const silent = p.no_effect.map((id) => labels?.[id] ?? id);
  return (
    <div className="basis-full space-y-0.5 rounded border border-warn/40 p-2 text-[11px]" title="The same blind reader read this draft as it is and with every pending proposal in place, in one pass. The proposals are not confirmed facts.">
      <p className="text-muted">
        <span className="font-medium text-warn">If you confirm the {p.proposals} red proposal{p.proposals === 1 ? "" : "s"} (hypothetical): </span>
        <Change before={p.confirmed} after={p.projected} text={`${p.confirmed} → ${p.projected}`} />/100
        {p.ats_confirmed != null && p.ats_projected != null && (
          <>
            {" · ATS match "}
            <Change before={p.ats_confirmed} after={p.ats_projected} text={`${p.ats_confirmed} → ${p.ats_projected}`} />
          </>
        )}
        {p.requirements_total > 0 && (
          <>
            {" · requirements shown "}
            <Change before={p.requirements_confirmed} after={p.requirements_projected} text={`${p.requirements_confirmed} → ${p.requirements_projected} of ${p.requirements_total}`} />
          </>
        )}
        {moved.map((d) => (
          <span key={d.name} className="text-faint">
            {" · "}{DIMENSION_LABEL[d.name] ?? d.name} <Change before={d.before} after={d.after} />
          </span>
        ))}
      </p>
      {p.gained.map((g) => (
        <p key={g.requirement} className="text-good">
          + {g.requirement}
          <span className="text-faint">{g.addition_id ? ` ← ${labels?.[g.addition_id] ?? "proposal"}: “${g.quote}”` : " (shown by another line)"}</span>
        </p>
      ))}
      {p.lost.map((r) => (
        <p key={r} className="text-warn">− {r} <span className="text-faint">(no longer read as shown)</span></p>
      ))}
      {p.gained.length === 0 && p.lost.length === 0 && <p className="text-faint">No requirement is newly shown with the proposals in place.</p>}
      {p.keywords_added.length > 0 && (
        <p className="text-muted">
          Job keywords <Change before={p.keywords_confirmed} after={p.keywords_projected} text={`${pct(p.keywords_confirmed)} → ${pct(p.keywords_projected)}`} />
          <span className="text-faint">, found by ATS search only with the proposals: {p.keywords_added.join(", ")}</span>
        </p>
      )}
      {silent.length > 0 && p.gained.length > 0 && <p className="text-faint">No requirement credited to: {silent.join("; ")}</p>}
    </div>
  );
}

/** The before/after reading in detail. */
export function FitCheckDetails({ check, capReason }: { check: FitComparison; capReason?: string | null }) {
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
      {capReason && (
        <p className="text-warn">
          Search score limit: {capReason}.{" "}
          {capReason.startsWith("core requirement unmet:")
            ? "It lifts when the CV shows each of these requirements with a line of its own: add the evidence to your CV, or confirm a red proposal that covers it."
            : "Rewording can show existing evidence more clearly; the underlying assessment must change for this limit to lift."}
        </p>
      )}
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
