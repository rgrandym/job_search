import type { FitComparison, MatchClass, Positioning, ProjectedFit, RequirementEvidence, Screening, ScreeningScore } from "../lib/types";
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

/** The score as an A–D grade, the way some screening systems (Workday HiredScore) grade
 * applicants. The app's own estimate, not any employer's number. */
export function grade(score: number): string {
  return score >= 80 ? "A" : score >= 65 ? "B" : score >= 50 ? "C" : "D";
}

function Change({ before, after, text }: { before: number; after: number; text?: string }) {
  const tone = after > before ? "text-good" : after < before ? "text-warn" : "text-muted";
  return <span className={tone}>{text ?? `${before} → ${after}`}</span>;
}

/** One line under the job: how much better the tailored CV shows the fit. */
export function FitCheckSummary({ check, capReason }: { check: FitComparison | null | undefined; capReason?: string | null }) {
  if (!check) return <span className="text-faint">AI screen reading not available for this draft</span>;
  return (
    <span className="text-muted" title="AI screen: a model reads the original and this CV for the job without knowing which is which. It changes a little from one reading to another; the screening score above is the deterministic one.">
      AI screen:
      {check.requirements_total > 0 && (
        <>
          {" requirements shown "}
          <Change before={check.requirements_before} after={check.requirements_after} text={`${check.requirements_before} → ${check.requirements_after} of ${check.requirements_total}`} />
          {" ·"}
        </>
      )}
      <span title="The search score, moved only when the reader rates function, domain, seniority, leadership or sector a whole step differently. Wording rarely changes these; new confirmed experience can.">
        {" fit for the role "}
        <Change before={check.before} after={check.after} text={`${check.before} → ${check.after}`} />
      </span>
      {capReason && check.after === check.before && (
        <span className="text-warn"> · search score limit still applies</span>
      )}
    </span>
  );
}

const PART_LABEL: Record<string, string> = {
  keywords: "job keywords",
  title: "job title",
  education: "degree",
  soft_skills: "soft skills",
};

/** The deterministic screening (ATS) score: original → tailored → with every red proposal
 * confirmed, and what it found and missed. */
export function ScreeningSummary({ screening }: { screening: Screening | null | undefined }) {
  if (!screening) return <span className="text-faint">Screening score not available for this draft (tailor again)</span>;
  const { before, after, if_confirmed: confirmed } = screening;
  const best = confirmed ?? after;
  const missing = best.keywords.filter((k) => !k.found).map((k) => k.term);
  const found = (s: ScreeningScore) => `${s.keywords.filter((k) => k.found).length}/${s.keywords.length}`;
  return (
    <div className="basis-full space-y-0.5 text-[12px]" title="Deterministic: computed by code from the CV's text, so the same CV and job always give the same score. Built like ATS match rates (keywords in the posting's own words, weighted by how often it uses them; the job title; degrees; soft skills); employers' own systems are not public. Grade A is 80 or more.">
      <p>
        <span className="font-medium">Screening score (ATS): </span>
        <span className="text-muted">original </span><b>{before.score}</b> <span className="text-muted">({before.grade})</span>
        {" → tailored "}<Change before={before.score} after={after.score} text={`${after.score} (${after.grade})`} />
        {confirmed && <>{" → if you confirm the red proposals "}<Change before={after.score} after={confirmed.score} text={`${confirmed.score} (${confirmed.grade})`} /></>}
      </p>
      <p className="text-[11px] text-muted">
        Keywords found {found(before)} → {found(after)}{confirmed ? ` → ${found(confirmed)}` : ""}
        {after.title && ` · job title "${after.title}": ${after.title_match >= 1 ? "in the CV" : after.title_match > 0 ? "partly" : "missing"}`}
        {after.education.length > 0 && ` · degree ${after.education.every((e) => e.found) ? "found" : "missing"}`}
        {` · points: ${Object.entries(best.points).map(([k, v]) => `${PART_LABEL[k] ?? k} ${Math.round(v)}`).join(", ")}`}
      </p>
      {missing.length > 0 && <p className="text-[11px] text-faint">Still missing{confirmed ? " even with the proposals" : ""}: {missing.join(", ")}</p>}
    </div>
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
        {p.ats_confirmed != null && p.ats_projected != null && (
          <span className="font-medium">
            {"screening match (ATS) "}
            <Change before={p.ats_confirmed} after={p.ats_projected} text={`${p.ats_confirmed} → ${p.ats_projected}`} />/100
            {" "}<Change before={p.ats_confirmed} after={p.ats_projected} text={`grade ${grade(p.ats_confirmed)} → ${grade(p.ats_projected)}`} />
          </span>
        )}
        {" · fit for the role "}
        <Change before={p.confirmed} after={p.projected} text={`${p.confirmed} → ${p.projected}`} />
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
