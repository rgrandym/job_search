import type { ProfileRecord, ProfileSummary } from "../lib/types";

const SECTIONS: { key: keyof Pick<ProfileSummary,
  "core_expertise" | "domains" | "leadership" | "qualifications" | "achievements" |
  "transferable_strengths" | "target_roles" | "stretch_roles" | "not_a_fit" | "search_keywords">; label: string }[] = [
  { key: "core_expertise", label: "Core expertise" },
  { key: "domains", label: "Domains and sectors" },
  { key: "leadership", label: "Leadership" },
  { key: "qualifications", label: "Qualifications" },
  { key: "achievements", label: "Key achievements" },
  { key: "transferable_strengths", label: "Transferable strengths" },
  { key: "target_roles", label: "Target roles" },
  { key: "stretch_roles", label: "Stretch roles" },
  { key: "not_a_fit", label: "Not a fit" },
  { key: "search_keywords", label: "Search keywords" },
];

/** Read the complete stored profile in the narrow sidebar. */
export function ProfileDetails({ record, onEdit }: { record: ProfileRecord; onEdit: () => void }) {
  const summary = record.summary;
  return (
    <div className="space-y-3 rounded-md border border-border bg-surface p-2 text-[11px]">
      <div>
        <p className="font-semibold text-fg">{summary.headline}</p>
        <p className="text-faint">{summary.seniority} · {summary.years_experience} years of experience</p>
      </div>
      <p className="whitespace-pre-wrap text-muted">{summary.summary}</p>
      {summary.role_families && summary.role_families.length > 0 && (
        <section className="space-y-2">
          <h3 className="label">Role families</h3>
          {summary.role_families.map((family) => (
            <div key={`${family.tier}:${family.name}`} className="rounded-md border border-border p-2">
              <p className="font-medium text-fg">{family.name} · {family.tier}</p>
              <p className="text-muted">Titles: {family.titles.join(", ") || "None"}</p>
              {family.domain_terms.length > 0 && <p className="text-muted">Domains: {family.domain_terms.join(", ")}</p>}
              {family.rationale && <p className="text-muted">{family.rationale}</p>}
              {family.gap && <p className="text-muted">Gap: {family.gap}</p>}
              {family.evidence.length > 0 && <p className="text-muted">Evidence: {family.evidence.join(", ")}</p>}
              {family.requested && <p className="text-faint">Requested in career intent</p>}
              {family.rejected && <p className="text-warn">Not searched: {family.rejected}</p>}
            </div>
          ))}
        </section>
      )}
      {summary.key_skills.length > 0 && (
        <section>
          <h3 className="label">Key skills</h3>
          <ul className="list-inside list-disc text-muted">
            {summary.key_skills.map((skill, index) => (
              <li key={`${skill.skill}:${index}`}>
                <span className="font-medium text-fg">{skill.skill}</span> · {skill.level}
                {skill.evidence && <p className="pl-3 text-faint">{skill.evidence}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {SECTIONS.map(({ key, label }) => summary[key].length > 0 && (
        <section key={key}>
          <h3 className="label">{label}</h3>
          <ul className="list-inside list-disc text-muted">
            {summary[key].map((item, index) => <li key={`${item}:${index}`}>{item}</li>)}
          </ul>
        </section>
      ))}
      <button type="button" className="text-accent hover:underline" onClick={onEdit}>View or edit in Profiles</button>
    </div>
  );
}
