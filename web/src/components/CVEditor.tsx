import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import type { MasterCV } from "../lib/types";

const split = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean);
const optional = (value: string) => value.trim() || null;

export function CVEditor({ cv: initial, saving, error, onSave }: {
  cv: MasterCV;
  saving: boolean;
  error: string | null;
  onSave: (cv: MasterCV) => void;
}) {
  const [cv, setCV] = useState<MasterCV>(() => structuredClone(initial));
  const basics = (patch: Partial<MasterCV["basics"]>) =>
    setCV((current) => ({ ...current, basics: { ...current.basics, ...patch } }));
  const experience = (index: number, patch: Partial<MasterCV["experience"][number]>) =>
    setCV((current) => ({
      ...current,
      experience: current.experience.map((role, i) => i === index ? { ...role, ...patch } : role),
    }));
  const bullet = (roleIndex: number, bulletIndex: number, patch: Partial<MasterCV["experience"][number]["bullets"][number]>) => {
    const role = cv.experience[roleIndex];
    experience(roleIndex, {
      bullets: role.bullets.map((item, i) => i === bulletIndex ? { ...item, ...patch } : item),
    });
  };
  const newId = (prefix: string) => {
    const used = new Set([
      ...cv.experience.map((role) => role.id),
      ...cv.experience.flatMap((role) => role.bullets.map((item) => item.id)),
      ...cv.projects.map((project) => project.id),
    ]);
    let number = 1;
    while (used.has(`${prefix}-${number}`)) number += 1;
    return `${prefix}-${number}`;
  };
  return (
    <div className="space-y-4 text-[12px]">
      <p className="text-muted">Edit the parsed CV below. The uploaded source file is kept unchanged; these reviewed facts are used for matching and future documents.</p>
      <section className="grid grid-cols-1 gap-2 rounded-md border border-border bg-surface p-3 sm:grid-cols-2">
        <label>Name<input className="input mt-1 w-full" value={cv.basics.name} onChange={(e) => basics({ name: e.target.value })} /></label>
        <label>Headline<input className="input mt-1 w-full" value={cv.basics.headline ?? ""} onChange={(e) => basics({ headline: optional(e.target.value) })} /></label>
        <label>Email<input className="input mt-1 w-full" value={cv.basics.email ?? ""} onChange={(e) => basics({ email: optional(e.target.value) })} /></label>
        <label>Phone<input className="input mt-1 w-full" value={cv.basics.phone ?? ""} onChange={(e) => basics({ phone: optional(e.target.value) })} /></label>
        <label>Location<input className="input mt-1 w-full" value={cv.basics.location ?? ""} onChange={(e) => basics({ location: optional(e.target.value) })} /></label>
        <label className="sm:col-span-2">Summary<textarea className="input mt-1 w-full" rows={4} value={cv.basics.summary ?? ""} onChange={(e) => basics({ summary: optional(e.target.value) })} /></label>
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between"><h3 className="font-semibold">Experience</h3>
          <button type="button" className="btn-ghost py-1" onClick={() => setCV((current) => ({
            ...current,
            experience: [...current.experience, { id: newId("role"), company: "", title: "", location: null, start: "2026-01", end: null, bullets: [] }],
          }))}><Plus size={12} /> Add role</button>
        </div>
        {cv.experience.map((role, roleIndex) => (
          <div key={role.id} className="space-y-2 rounded-md border border-border bg-surface p-3">
            <div className="flex justify-between gap-2"><span className="font-medium">{role.title || "New role"}</span>
              <button type="button" className="text-bad" aria-label={`Delete ${role.title || "role"}`} onClick={() => setCV((current) => ({ ...current, experience: current.experience.filter((_, i) => i !== roleIndex) }))}><Trash2 size={13} /></button>
            </div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <label>Title<input className="input mt-1 w-full" value={role.title} onChange={(e) => experience(roleIndex, { title: e.target.value })} /></label>
              <label>Company<input className="input mt-1 w-full" value={role.company} onChange={(e) => experience(roleIndex, { company: e.target.value })} /></label>
              <label>Location<input className="input mt-1 w-full" value={role.location ?? ""} onChange={(e) => experience(roleIndex, { location: optional(e.target.value) })} /></label>
              <div className="grid grid-cols-2 gap-2">
                <label>Start<input className="input mt-1 w-full" placeholder="YYYY-MM" value={role.start} onChange={(e) => experience(roleIndex, { start: e.target.value })} /></label>
                <label>End<input className="input mt-1 w-full" placeholder="Present" value={role.end ?? ""} onChange={(e) => experience(roleIndex, { end: optional(e.target.value) })} /></label>
              </div>
            </div>
            {role.bullets.map((item, bulletIndex) => (
              <div key={item.id} className="rounded border border-border p-2">
                <div className="mb-1 flex justify-between"><span className="text-faint">{item.id}</span>
                  <button type="button" className="text-bad" aria-label={`Delete bullet ${item.id}`} onClick={() => experience(roleIndex, { bullets: role.bullets.filter((_, i) => i !== bulletIndex) })}><Trash2 size={12} /></button>
                </div>
                <textarea className="input w-full" rows={2} value={item.text} onChange={(e) => bullet(roleIndex, bulletIndex, { text: e.target.value })} />
                <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <label>Skills, comma separated<input className="input mt-1 w-full" value={item.skills.join(", ")} onChange={(e) => bullet(roleIndex, bulletIndex, { skills: split(e.target.value) })} /></label>
                  <label>Metrics, comma separated<input className="input mt-1 w-full" value={item.metrics.join(", ")} onChange={(e) => bullet(roleIndex, bulletIndex, { metrics: split(e.target.value) })} /></label>
                </div>
              </div>
            ))}
            <button type="button" className="btn-ghost py-1" onClick={() => experience(roleIndex, { bullets: [...role.bullets, { id: newId(role.id), text: "New achievement", skills: [], metrics: [] }] })}><Plus size={12} /> Add achievement</button>
          </div>
        ))}
      </section>

      <section className="space-y-2 rounded-md border border-border bg-surface p-3">
        <h3 className="font-semibold">Skills</h3>
        {cv.skills.map((group, index) => (
          <div key={`${group.category}-${index}`} className="grid grid-cols-[minmax(0,140px)_1fr_auto] gap-2">
            <input aria-label="Skill category" className="input" value={group.category} onChange={(e) => setCV((current) => ({ ...current, skills: current.skills.map((item, i) => i === index ? { ...item, category: e.target.value } : item) }))} />
            <input aria-label={`${group.category} skills`} className="input" value={group.items.join(", ")} onChange={(e) => setCV((current) => ({ ...current, skills: current.skills.map((item, i) => i === index ? { ...item, items: split(e.target.value) } : item) }))} />
            <button type="button" className="text-bad" aria-label={`Delete ${group.category} skills`} onClick={() => setCV((current) => ({ ...current, skills: current.skills.filter((_, i) => i !== index) }))}><Trash2 size={13} /></button>
          </div>
        ))}
        <button type="button" className="btn-ghost py-1" onClick={() => setCV((current) => ({ ...current, skills: [...current.skills, { category: "Additional", items: ["New skill"] }] }))}><Plus size={12} /> Add skill group</button>
      </section>

      <details className="rounded-md border border-border bg-surface p-3">
        <summary className="cursor-pointer font-semibold">Education, certifications, projects and languages</summary>
        <div className="mt-3 space-y-3">
          {cv.education.map((item, index) => <div key={index} className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <input aria-label="Degree" className="input" value={item.degree} onChange={(e) => setCV((current) => ({ ...current, education: current.education.map((row, i) => i === index ? { ...row, degree: e.target.value } : row) }))} />
            <input aria-label="Institution" className="input" value={item.institution} onChange={(e) => setCV((current) => ({ ...current, education: current.education.map((row, i) => i === index ? { ...row, institution: e.target.value } : row) }))} />
          </div>)}
          {cv.certifications.map((item, index) => <div key={index} className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <input aria-label="Certification" className="input" value={item.name} onChange={(e) => setCV((current) => ({ ...current, certifications: current.certifications.map((row, i) => i === index ? { ...row, name: e.target.value } : row) }))} />
            <input aria-label="Certification issuer" className="input" value={item.issuer ?? ""} onChange={(e) => setCV((current) => ({ ...current, certifications: current.certifications.map((row, i) => i === index ? { ...row, issuer: optional(e.target.value) } : row) }))} />
          </div>)}
          {cv.projects.map((item, index) => <label key={item.id} className="block">{item.name}<textarea className="input mt-1 w-full" rows={2} value={item.description} onChange={(e) => setCV((current) => ({ ...current, projects: current.projects.map((row, i) => i === index ? { ...row, description: e.target.value } : row) }))} /></label>)}
          <label className="block">Languages, comma separated<input className="input mt-1 w-full" value={cv.languages.join(", ")} onChange={(e) => setCV((current) => ({ ...current, languages: split(e.target.value) }))} /></label>
        </div>
      </details>

      <div className="sticky bottom-0 flex items-center gap-3 border-t border-border bg-panel py-3">
        <button type="button" className="btn-primary" disabled={saving} onClick={() => onSave(cv)}>{saving ? "Saving…" : "Save CV"}</button>
        {error && <p className="text-bad">{error}</p>}
      </div>
    </div>
  );
}
