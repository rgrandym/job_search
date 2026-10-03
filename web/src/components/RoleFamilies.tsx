import { Plus, X } from "lucide-react";
import type { FamilyTier, FamilyYield, RoleFamily } from "../lib/types";
import { cn } from "../lib/utils";
import { ChipInput, Field } from "./ui";

const TIERS: { value: FamilyTier; label: string; hint: string }[] = [
  { value: "core", label: "Core", hint: "the work you do now" },
  { value: "progression", label: "Progression", hint: "the next level up" },
  { value: "adjacent", label: "Adjacent", hint: "a new function your experience carries into" },
];

const TIER_COLOR: Record<FamilyTier, string> = {
  core: "var(--good)",
  progression: "var(--accent)",
  adjacent: "var(--warn)",
};

function yieldLine(y: FamilyYield | undefined) {
  if (!y) return null;
  const counts = `${y.searches} search${y.searches === 1 ? "" : "es"} · ${y.found} found · ${y.matches} matches`;
  if (y.landing === false)
    return <span className="text-warn">{counts} · not landing: the matcher has accepted none so far</span>;
  return <span>{counts}</span>;
}

/** The profile's role families, grouped by tier, with their evidence, gap and search results. */
export function RoleFamiliesView({ families, yields }: { families: RoleFamily[]; yields: FamilyYield[] }) {
  if (!families.length)
    return (
      <p className="text-[12px] text-faint">
        No role families in this profile (built before families existed). Click Update from CV to group its roles into core,
        progression and adjacent families.
      </p>
    );
  const byName = new Map(yields.map((y) => [y.family, y]));
  return (
    <div className="space-y-3">
      {TIERS.map((tier) => {
        const group = families.filter((f) => f.tier === tier.value);
        if (!group.length) return null;
        return (
          <div key={tier.value}>
            <p className="label mb-1">
              {tier.label} <span className="normal-case tracking-normal text-faint">· {tier.hint}</span>
            </p>
            <ul className="space-y-1.5">
              {group.map((f) => (
                <li
                  key={f.name}
                  className={cn("rounded-md border border-border px-2 py-1.5 text-[12px]", f.rejected && "opacity-70")}
                  style={{ borderLeft: `3px solid ${TIER_COLOR[f.tier]}` }}
                >
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-medium text-fg">{f.name}</span>
                    {f.requested && <span className="chip">you asked for this</span>}
                    {f.rejected ? (
                      <span className="text-warn">not searched: {f.rejected}</span>
                    ) : (
                      <span className="text-[11px] text-faint">{yieldLine(byName.get(f.name))}</span>
                    )}
                  </div>
                  <p className="text-muted">{f.titles.join(" · ") || "no titles"}</p>
                  {f.rationale && <p className="text-faint">{f.rationale}</p>}
                  {f.gap && <p className="text-faint">Gap: {f.gap}</p>}
                  {f.evidence.length > 0 && <p className="text-[11px] text-faint">Evidence: {f.evidence.join(", ")}</p>}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

/** Edit families by hand. Your edits are kept as they are: you decide what to search. */
export function RoleFamiliesEditor({ value, onChange }: { value: RoleFamily[]; onChange: (v: RoleFamily[]) => void }) {
  const patch = (i: number, update: Partial<RoleFamily>) => onChange(value.map((f, j) => (j === i ? { ...f, ...update } : f)));
  return (
    <div className="space-y-2">
      <p className="label">Role families</p>
      {value.map((f, i) => (
        <div key={i} className="space-y-1 rounded-md border border-border p-2">
          <div className="grid grid-cols-[minmax(0,1fr)_130px_24px] gap-1">
            <input className="input" placeholder="Family name" value={f.name} onChange={(e) => patch(i, { name: e.target.value })} />
            <select className="input" value={f.tier} onChange={(e) => patch(i, { tier: e.target.value as FamilyTier })}>
              {TIERS.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
            <button type="button" title="Remove family" className="text-faint hover:text-bad" onClick={() => onChange(value.filter((_, j) => j !== i))}>
              <X size={14} />
            </button>
          </div>
          <Field label="Titles employers advertise">
            <ChipInput value={f.titles} onChange={(titles) => patch(i, { titles })} placeholder="Add a title, press Enter" />
          </Field>
          <input className="input" placeholder="Main gap" value={f.gap} onChange={(e) => patch(i, { gap: e.target.value })} />
          {f.rejected && (
            <label className="flex items-center gap-2 text-[11px] text-warn">
              <input type="checkbox" checked={false} onChange={() => patch(i, { rejected: null })} />
              Not searched ({f.rejected}). Tick to search it anyway.
            </label>
          )}
        </div>
      ))}
      <button
        type="button"
        className="flex items-center gap-1 text-[12px] text-accent hover:underline"
        onClick={() =>
          onChange([
            ...value,
            { name: "", tier: "adjacent", titles: [], domain_terms: [], evidence: [], gap: "", rationale: "", requested: true, rejected: null },
          ])
        }
      >
        <Plus size={12} /> Add family
      </button>
    </div>
  );
}
