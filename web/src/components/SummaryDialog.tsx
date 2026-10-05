import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil, Pin, PinOff, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { FamilyYield, ProfileRecord, ProfileSummary, SkillEvidence } from "../lib/types";
import { cn } from "../lib/utils";
import { profileSearchQuery, useSearch } from "../stores/searchStore";
import { EvidenceReview } from "./EvidenceReview";
import { IntentEditor } from "./IntentEditor";
import { RoleFamiliesEditor, RoleFamiliesView } from "./RoleFamilies";
import { Field, Modal } from "./ui";

const LEVEL_COLOR = { expert: "var(--good)", proficient: "var(--accent)", familiar: "var(--text-muted)" };
const LEVELS: SkillEvidence["level"][] = ["expert", "proficient", "familiar"];
const LIST_FIELDS = [
  ["target_roles", "Target roles searched by job boards"],
  ["stretch_roles", "Stretch roles"],
  ["not_a_fit", "Not a fit (reason each)"],
  ["core_expertise", "Core expertise"],
  ["domains", "Domains and sectors"],
  ["leadership", "Leadership"],
  ["qualifications", "Qualifications"],
  ["achievements", "Key achievements"],
  ["transferable_strengths", "Transferable (not direct) experience"],
  ["search_keywords", "Search keywords"],
] as const;

const familyLabel = (family: string) => (family === "any" ? "General CV profile" : family);
type Tab = "profiles" | "intent" | "evidence";
const TABS: { value: Tab; label: string }[] = [
  { value: "profiles", label: "Profiles" },
  { value: "intent", label: "Career intent" },
  { value: "evidence", label: "Add evidence" },
];
const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "");

/** Stored profile summaries for the selected CV: view, pin for searches, edit, rebuild, delete. */
export function SummaryDialog({ open, onClose, initialProfileKey, embedded = false }: {
  open: boolean;
  onClose: () => void;
  initialProfileKey: string | null;
  embedded?: boolean;
}) {
  const qc = useQueryClient();
  const { query, useCv, profileKey, set } = useSearch();
  const [selected, setSelected] = useState<string | null>(initialProfileKey);
  const [editing, setEditing] = useState(Boolean(initialProfileKey));
  const [tab, setTab] = useState<Tab>("profiles");

  const profiles = useQuery({ queryKey: ["profiles"], queryFn: api.profiles, enabled: open });
  const records = profiles.data?.profiles ?? [];
  const current = selected
    ? records.find((r) => r.key === selected)
    : records.find((r) => r.key === profileKey) ?? records[0];

  useEffect(() => {
    if (!open) setEditing(false);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    setSelected(initialProfileKey);
    setEditing(Boolean(initialProfileKey));
    if (initialProfileKey) {
      setTab("profiles");
    }
  }, [open, initialProfileKey]);
  useEffect(() => {
    // a pinned profile that no longer exists (deleted, or another CV selected) is unpinned
    const stored = profiles.data?.profiles;
    if (stored && profileKey && !stored.some((r) => r.key === profileKey)) set({ profileKey: null });
  }, [profiles.data, profileKey, set]);

  const refresh = () => qc.invalidateQueries({ queryKey: ["profiles"] });
  const build = useMutation({
    mutationFn: (rebuild: boolean) => api.profileSummary(profileSearchQuery(query), useCv, rebuild),
    onSuccess: (r) => {
      set({ summary: { summary: r.summary, fromMemory: r.from_memory } });
      setSelected(r.key);
      void refresh();
    },
  });
  const update = useMutation({
    mutationFn: (key: string) => api.refreshProfile(key),
    onSuccess: (r) => {
      setSelected(r.key);
      void refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (key: string) => api.deleteProfile(key),
    onSuccess: (_, key) => {
      if (profileKey === key) set({ profileKey: null });
      setSelected(null);
      void refresh();
    },
  });

  const content = (
    <>
      <div className="mb-3 flex gap-1" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={tab === t.value}
            onClick={() => setTab(t.value)}
            className={cn(
              "flex-1 rounded-md border px-2 py-1 text-[12px] transition-colors",
              tab === t.value ? "border-accent bg-accent-bg text-fg" : "border-border bg-surface text-muted hover:text-fg",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "intent" ? (
        <IntentEditor />
      ) : tab === "evidence" ? (
        <EvidenceReview />
      ) : (
        <>
          <p className="mb-3 text-[12px] text-faint">
            The quality model builds a profile from your CV for each kind of search and stores it with this CV. The job_matcher
            screens every shortlisted posting against it, and the job boards search its role families. Pin a
            profile to use it for every search with this CV; edit it to correct or steer the matching. Profiles are kept when
            you edit the CV and only change when you click Update. Selecting a different CV builds its own profiles.
          </p>
          {profiles.isPending ? (
            <p className="flex items-center gap-2 text-[12px] text-muted"><Loader2 size={14} className="animate-spin" /> Loading profiles…</p>
          ) : profiles.isError ? (
            <p className="text-[12px] text-bad">Could not load profiles: {(profiles.error as Error).message}</p>
          ) : (
            <div className="grid gap-4 md:grid-cols-[220px_minmax(0,1fr)]">
              <aside className="space-y-2">
                {records.length === 0 && (
                  <p className="text-[12px] text-muted">
                    {profiles.data?.cv_parsed ? "No profiles yet for this CV." : "This CV has not been read yet."} Build one below, or run a
                    search: it is created and stored automatically.
                  </p>
                )}
                {records.map((r) => (
                  <button
                    key={r.key}
                    type="button"
                    onClick={() => {
                      setSelected(r.key);
                      setEditing(false);
                    }}
                    className={cn(
                      "w-full rounded-md border px-2 py-1.5 text-left text-[12px]",
                      current?.key === r.key ? "border-accent bg-accent-bg" : "border-border hover:border-accent",
                    )}
                  >
                    <span className="block truncate font-medium text-fg">{familyLabel(r.role_family)}</span>
                    {r.cv_name && <span className="block truncate text-[11px] text-muted">{r.cv_name}</span>}
                    <span className="text-[11px] text-faint">
                      Created {day(r.created_at)}
                      {r.edited && " · edited"}
                      {r.key === profileKey && " · pinned"}
                      {r.cv_changed && <span className="text-warn"> · CV changed</span>}
                      {r.intent_changed && <span className="text-warn"> · intent changed</span>}
                    </span>
                  </button>
                ))}
                <div className="space-y-1 pt-1">
                  <button className="btn-ghost w-full justify-center text-[12px]" disabled={build.isPending} onClick={() => build.mutate(false)}>
                    {build.isPending ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} Build general profile
                  </button>
                </div>
                {build.error && <p className="text-[11px] text-bad">{(build.error as Error).message}</p>}
              </aside>
              <section className="min-w-0">
                {!current ? (
                  <p className="text-[12px] text-muted">Select or build a profile to see it here.</p>
                ) : editing ? (
                  <ProfileEditor record={current} onDone={() => setEditing(false)} />
                ) : (
                  <ProfileView
                    record={current}
                    yields={profiles.data?.family_yield ?? []}
                    pinned={current.key === profileKey}
                    busy={build.isPending || update.isPending || remove.isPending}
                    onPin={() => set({ profileKey: current.key === profileKey ? null : current.key })}
                    onEdit={() => setEditing(true)}
                    onRebuild={() => update.mutate(current.key)}
                    onDelete={() => {
                      if (window.confirm(`Delete the "${familyLabel(current.role_family)}" profile? It will be rebuilt on the next matching search.`))
                        remove.mutate(current.key);
                    }}
                  />
                )}
                {update.error && <p className="mt-2 text-[12px] text-bad">{(update.error as Error).message}</p>}
                {remove.error && <p className="mt-2 text-[12px] text-bad">{(remove.error as Error).message}</p>}
              </section>
            </div>
          )}
        </>
      )}
    </>
  );
  return embedded ? content : <Modal open={open} onClose={onClose} title="Profiles" resizable>{content}</Modal>;
}

function ProfileView({
  record,
  yields,
  pinned,
  busy,
  onPin,
  onEdit,
  onRebuild,
  onDelete,
}: {
  record: ProfileRecord;
  yields: FamilyYield[];
  pinned: boolean;
  busy: boolean;
  onPin: () => void;
  onEdit: () => void;
  onRebuild: () => void;
  onDelete: () => void;
}) {
  const s = record.summary;
  return (
    <div className="space-y-3 text-[13px]">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-semibold">{s.headline}</p>
          <p className="text-[12px] text-muted">
            {s.seniority} · {s.years_experience} yrs · {familyLabel(record.role_family)}
            {record.edited && " · edited by you"}
          </p>
          <p className="text-[11px] text-faint">
            From {record.cv_name ?? "this CV"} · created {day(record.created_at)}
            {record.updated_at && ` · updated ${day(record.updated_at)}`}
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          <button className={cn("py-1 text-[12px]", pinned ? "btn-primary" : "btn-ghost")} onClick={onPin}>
            {pinned ? <PinOff size={12} /> : <Pin size={12} />} {pinned ? "Unpin" : "Pin for searches"}
          </button>
          <button className="btn-ghost py-1 text-[12px]" onClick={onEdit}>
            <Pencil size={12} /> Edit
          </button>
          <button
            className={cn("py-1 text-[12px]", record.cv_changed || record.intent_changed ? "btn-primary" : "btn-ghost")}
            disabled={busy}
            title="Rebuild this profile from the CV's current content"
            onClick={onRebuild}
          >
            <RefreshCw size={12} className={cn(busy && "animate-spin")} /> Update from CV
          </button>
          <button className="btn-ghost py-1 text-[12px] text-bad" disabled={busy} onClick={onDelete}>
            <Trash2 size={12} />
          </button>
        </div>
      </div>
      {record.cv_changed && (
        <p className="text-[11px] text-warn">
          The CV has changed since this profile was built. Searches keep using it as it is until you click Update from CV.
        </p>
      )}
      {record.intent_changed && (
        <p className="text-[11px] text-warn">
          Your career intent has changed since this profile was built. Click Update from CV to rebuild its role families with it.
        </p>
      )}
      {record.edited && (
        <p className="text-[11px] text-faint">Updating replaces your edits with a fresh summary from the CV.</p>
      )}
      <p className="text-muted">{s.summary}</p>
      <div>
        <p className="label mb-1">Role families (what the job boards search)</p>
        <RoleFamiliesView families={s.role_families ?? []} yields={yields} />
      </div>
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
      {LIST_FIELDS.map(([field, title]) => (
        <Section key={field} title={title} items={s[field]} />
      ))}
    </div>
  );
}

function ProfileEditor({ record, onDone }: { record: ProfileRecord; onDone: () => void }) {
  const qc = useQueryClient();
  const set = useSearch((st) => st.set);
  const [draft, setDraft] = useState<ProfileSummary>(record.summary);
  const patch = (update: Partial<ProfileSummary>) => setDraft((d) => ({ ...d, ...update }));
  const setSkill = (i: number, update: Partial<SkillEvidence>) =>
    patch({ key_skills: draft.key_skills.map((k, j) => (j === i ? { ...k, ...update } : k)) });

  const save = useMutation({
    mutationFn: () =>
      api.editProfile(record.key, {
        ...draft,
        key_skills: draft.key_skills.filter((k) => k.skill.trim()),
        role_families: (draft.role_families ?? []).filter((f) => f.name.trim() && f.titles.length),
      }),
    onSuccess: (saved) => {
      set({ summary: { summary: saved.summary, fromMemory: true } });
      void qc.invalidateQueries({ queryKey: ["profiles"] });
      onDone();
    },
  });

  return (
    <div className="space-y-3 text-[12px]">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_140px_90px]">
        <Field label="Headline">
          <input className="input" value={draft.headline} onChange={(e) => patch({ headline: e.target.value })} />
        </Field>
        <Field label="Seniority">
          <input className="input" value={draft.seniority} onChange={(e) => patch({ seniority: e.target.value })} />
        </Field>
        <Field label="Years">
          <input
            className="input"
            type="number"
            min={0}
            step={0.5}
            value={draft.years_experience}
            onChange={(e) => patch({ years_experience: Math.max(0, Number(e.target.value) || 0) })}
          />
        </Field>
      </div>
      <Field label="Summary">
        <textarea className="input min-h-20" value={draft.summary} onChange={(e) => patch({ summary: e.target.value })} />
      </Field>
      <div>
        <p className="label mb-1">Key skills</p>
        <div className="space-y-1">
          {draft.key_skills.map((k, i) => (
            <div key={i} className="grid grid-cols-[minmax(0,1fr)_110px_minmax(0,1.4fr)_24px] gap-1">
              <input className="input" placeholder="Skill" value={k.skill} onChange={(e) => setSkill(i, { skill: e.target.value })} />
              <select className="input" value={k.level} onChange={(e) => setSkill(i, { level: e.target.value as SkillEvidence["level"] })}>
                {LEVELS.map((level) => (
                  <option key={level}>{level}</option>
                ))}
              </select>
              <input className="input" placeholder="Evidence" value={k.evidence} onChange={(e) => setSkill(i, { evidence: e.target.value })} />
              <button
                type="button"
                title="Remove skill"
                className="text-faint hover:text-bad"
                onClick={() => patch({ key_skills: draft.key_skills.filter((_, j) => j !== i) })}
              >
                <X size={14} />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="text-[12px] text-accent hover:underline"
            onClick={() => patch({ key_skills: [...draft.key_skills, { skill: "", level: "proficient", evidence: "" }] })}
          >
            + Add skill
          </button>
        </div>
      </div>
      <RoleFamiliesEditor
        value={draft.role_families ?? []}
        onChange={(role_families) => patch({ role_families })}
      />
      {LIST_FIELDS.map(([field, title]) => (
        <Field key={field} label={title} hint="One per line">
          <textarea
            className="input min-h-16"
            value={draft[field].join("\n")}
            onChange={(e) => patch({ [field]: e.target.value.split("\n").map((line) => line.trim()).filter(Boolean) })}
          />
        </Field>
      ))}
      {save.error && <p className="text-bad">{(save.error as Error).message}</p>}
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-ghost" onClick={onDone}>
          Cancel
        </button>
        <button type="button" className="btn-primary" disabled={save.isPending || !draft.headline.trim()} onClick={() => save.mutate()}>
          {save.isPending && <Loader2 size={14} className="animate-spin" />} Save profile
        </button>
      </div>
    </div>
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
