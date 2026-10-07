import { Minus, Plus, Trash2 } from "lucide-react";
import { useLayoutEffect, useMemo, useRef, useState, type TextareaHTMLAttributes } from "react";
import type { MasterCV } from "../lib/types";
import { cn } from "../lib/utils";

const split = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean);
const optional = (value: string) => value.trim() || null;

// A4 at 96 dpi, the page size Word uses for UK CVs.
const PAGE_W = 794;
const PAGE_H = 1123;
const MARGIN_Y = 72; // matches .cv-paper padding
const ZOOMS = [0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5];

/** Words a reader would see on the page (ids and URLs are not text). */
function countWords(value: unknown, key = ""): number {
  if (typeof value === "string") return key === "id" || key === "url" ? 0 : value.split(/\s+/).filter(Boolean).length;
  if (Array.isArray(value)) return value.reduce<number>((sum, item) => sum + countWords(item), 0);
  if (value && typeof value === "object") return Object.entries(value).reduce((sum, [k, v]) => sum + countWords(v, k), 0);
  return 0;
}

/** Text that wraps and grows with its content, like a line in a document; never clipped. */
function Field({ value, onChange, multiline, className, ...rest }: Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange"> & {
  value: string;
  onChange: (value: string) => void;
  multiline?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);
  return (
    <textarea
      ref={ref}
      rows={1}
      value={value}
      className={cn("cv-line", className)}
      onChange={(e) => onChange(multiline ? e.target.value : e.target.value.replace(/\n/g, " "))}
      onKeyDown={multiline ? undefined : (e) => e.key === "Enter" && e.preventDefault()}
      {...rest}
    />
  );
}

/** Fit-to-width zoom by default; the page is scaled, never reflowed, so it keeps its layout. */
function usePageView() {
  const deskRef = useRef<HTMLDivElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const [deskWidth, setDeskWidth] = useState(PAGE_W);
  const [pageHeight, setPageHeight] = useState(PAGE_H);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  useLayoutEffect(() => {
    const desk = deskRef.current;
    const page = pageRef.current;
    if (!desk || !page) return;
    const observer = new ResizeObserver(() => {
      setDeskWidth(desk.clientWidth);
      setPageHeight(page.offsetHeight);
    });
    observer.observe(desk);
    observer.observe(page);
    return () => observer.disconnect();
  }, []);
  const fit = Math.min(1, Math.max(0.3, (deskWidth - 48) / PAGE_W));
  const scale = zoom === "fit" ? fit : zoom;
  const step = (direction: 1 | -1) => {
    const next = direction > 0 ? ZOOMS.find((z) => z > scale + 0.001) : [...ZOOMS].reverse().find((z) => z < scale - 0.001);
    if (next) setZoom(next);
  };
  return { deskRef, pageRef, pageHeight, scale, zoom, setZoom, step };
}

export function CVEditor({ cv: initial, saving, error, onSave }: {
  cv: MasterCV;
  saving: boolean;
  error: string | null;
  onSave: (cv: MasterCV) => void;
}) {
  const [cv, setCV] = useState<MasterCV>(() => structuredClone(initial));
  const view = usePageView();
  const dirty = useMemo(() => JSON.stringify(cv) !== JSON.stringify(initial), [cv, initial]);
  const words = useMemo(() => countWords(cv), [cv]);
  const pages = Math.max(1, Math.ceil((view.pageHeight + 2 * MARGIN_Y) / PAGE_H));
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
    <div className="flex h-full flex-col text-[12px]">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-surface px-4 py-2">
        <p className="min-w-0 flex-1 text-muted">What the app read from your file, used for matching and tailoring. It may leave things out (such as publications). Editing it never changes your file.</p>
        <div className="flex items-center gap-1" aria-label="Zoom">
          <button type="button" className="rounded p-1 text-muted hover:bg-panel hover:text-fg" aria-label="Zoom out" onClick={() => view.step(-1)}><Minus size={13} /></button>
          <span className="w-10 text-center tabular-nums text-muted">{Math.round(view.scale * 100)}%</span>
          <button type="button" className="rounded p-1 text-muted hover:bg-panel hover:text-fg" aria-label="Zoom in" onClick={() => view.step(1)}><Plus size={13} /></button>
          <button type="button" className={cn("ml-1 rounded px-2 py-0.5", view.zoom === "fit" ? "bg-accent-bg text-accent" : "text-muted hover:text-fg")} onClick={() => view.setZoom("fit")}>Fit width</button>
        </div>
        <button type="button" className="btn-primary py-1" disabled={saving || !dirty} onClick={() => onSave(cv)}>{saving ? "Saving…" : "Save CV"}</button>
      </div>

      <div ref={view.deskRef} className="cv-desk min-h-0 flex-1 overflow-auto py-6 scroll-thin">
        <div className="mx-auto" style={{ width: PAGE_W * view.scale, height: pages * PAGE_H * view.scale }}>
          <div
            className="cv-paper"
            style={{ width: PAGE_W, height: pages * PAGE_H, transform: `scale(${view.scale})`, transformOrigin: "top left" }}
          >
            {Array.from({ length: pages - 1 }, (_, i) => (
              <div key={i} className="cv-page-break" style={{ top: (i + 1) * PAGE_H }} aria-hidden><span>Page {i + 2}</span></div>
            ))}
            <div ref={view.pageRef} className="space-y-5">
            <header className="space-y-1 text-center">
              <Field aria-label="Name" className="w-full text-center text-[22pt] font-bold leading-tight" placeholder="Your name" value={cv.basics.name} onChange={(v) => basics({ name: v })} />
              <Field aria-label="Headline" className="w-full text-center text-[12pt]" placeholder="Professional headline" value={cv.basics.headline ?? ""} onChange={(v) => basics({ headline: optional(v) })} />
              <div className="grid grid-cols-3 gap-x-3">
                <Field aria-label="Email" className="text-center" placeholder="Email" value={cv.basics.email ?? ""} onChange={(v) => basics({ email: optional(v) })} />
                <Field aria-label="Phone" className="text-center" placeholder="Phone" value={cv.basics.phone ?? ""} onChange={(v) => basics({ phone: optional(v) })} />
                <Field aria-label="Location" className="text-center" placeholder="Location" value={cv.basics.location ?? ""} onChange={(v) => basics({ location: optional(v) })} />
              </div>
              {cv.basics.links.map((link, index) => (
                <div key={index} className="flex items-start gap-2 text-[9pt]">
                  <Field aria-label="Link label" className="w-1/3 text-right" placeholder="Link label" value={link.label} onChange={(v) => basics({ links: cv.basics.links.map((row, i) => i === index ? { ...row, label: v } : row) })} />
                  <Field aria-label="Link URL" className="flex-1" placeholder="https://" value={link.url} onChange={(v) => basics({ links: cv.basics.links.map((row, i) => i === index ? { ...row, url: v } : row) })} />
                  <button type="button" className="cv-delete" aria-label={`Delete link ${link.label}`} onClick={() => basics({ links: cv.basics.links.filter((_, i) => i !== index) })}><Trash2 size={12} /></button>
                </div>
              ))}
              <button type="button" className="cv-add" onClick={() => basics({ links: [...cv.basics.links, { label: "", url: "" }] })}><Plus size={12} /> Add link</button>
            </header>

            <section>
              <h3 className="cv-heading">Profile</h3>
              <Field multiline aria-label="Professional summary" className="w-full" placeholder="Professional summary" value={cv.basics.summary ?? ""} onChange={(v) => basics({ summary: optional(v) })} />
            </section>

            <section>
              <div className="cv-section-title"><h3 className="cv-heading flex-1">Experience</h3><button type="button" className="cv-add" onClick={() => setCV((current) => ({ ...current, experience: [...current.experience, { id: newId("role"), company: "", title: "", location: null, start: "2026-01", end: null, bullets: [] }] }))}><Plus size={12} /> Add role</button></div>
              <div className="space-y-4">
                {cv.experience.map((role, roleIndex) => (
                  <div key={role.id}>
                    <div className="flex items-start gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-start gap-x-1"><Field aria-label="Job title" className="min-w-0 flex-1 font-bold" placeholder="Job title" value={role.title} onChange={(v) => experience(roleIndex, { title: v })} /><Field aria-label="Start date" className="w-20 shrink-0 text-right" placeholder="YYYY-MM" value={role.start} onChange={(v) => experience(roleIndex, { start: v })} /><span className="pt-0.5">–</span><Field aria-label="End date" className="w-20 shrink-0" placeholder="Present" value={role.end ?? ""} onChange={(v) => experience(roleIndex, { end: optional(v) })} /></div>
                        <div className="flex items-start gap-x-1"><Field aria-label="Company" className="min-w-0 flex-1 italic" placeholder="Company" value={role.company} onChange={(v) => experience(roleIndex, { company: v })} /><Field aria-label="Role location" className="w-44 shrink-0 text-right italic" placeholder="Location" value={role.location ?? ""} onChange={(v) => experience(roleIndex, { location: optional(v) })} /></div>
                      </div>
                      <button type="button" className="cv-delete" aria-label={`Delete ${role.title || "role"}`} onClick={() => setCV((current) => ({ ...current, experience: current.experience.filter((_, i) => i !== roleIndex) }))}><Trash2 size={13} /></button>
                    </div>
                    <ul className="mt-1 space-y-0.5">
                      {role.bullets.map((item, bulletIndex) => (
                        <li key={item.id} className="flex items-start gap-1.5"><span className="pl-2 pt-0.5">•</span><div className="min-w-0 flex-1"><Field multiline aria-label={`Achievement ${bulletIndex + 1} for ${role.title || "role"}`} className="w-full" placeholder="Achievement" value={item.text} onChange={(v) => bullet(roleIndex, bulletIndex, { text: v })} /><details className="cv-evidence"><summary>Evidence tags</summary><div className="grid grid-cols-2 gap-2"><label>Skills<Field className="w-full" value={item.skills.join(", ")} onChange={(v) => bullet(roleIndex, bulletIndex, { skills: split(v) })} /></label><label>Metrics<Field className="w-full" value={item.metrics.join(", ")} onChange={(v) => bullet(roleIndex, bulletIndex, { metrics: split(v) })} /></label></div></details></div><button type="button" className="cv-delete" aria-label={`Delete achievement ${bulletIndex + 1}`} onClick={() => experience(roleIndex, { bullets: role.bullets.filter((_, i) => i !== bulletIndex) })}><Trash2 size={12} /></button></li>
                      ))}
                    </ul>
                    <button type="button" className="cv-add ml-4" onClick={() => experience(roleIndex, { bullets: [...role.bullets, { id: newId(role.id), text: "", skills: [], metrics: [] }] })}><Plus size={12} /> Add achievement</button>
                  </div>
                ))}
              </div>
            </section>

            <section><h3 className="cv-heading">Skills</h3><div className="space-y-0.5">{cv.skills.map((group, index) => <div key={index} className="flex items-start gap-2"><Field aria-label="Skill category" className="w-36 shrink-0 font-bold" value={group.category} onChange={(v) => setCV((current) => ({ ...current, skills: current.skills.map((item, i) => i === index ? { ...item, category: v } : item) }))} /><Field aria-label={`${group.category} skills`} className="min-w-0 flex-1" value={group.items.join(", ")} onChange={(v) => setCV((current) => ({ ...current, skills: current.skills.map((item, i) => i === index ? { ...item, items: split(v) } : item) }))} /><button type="button" className="cv-delete" aria-label={`Delete ${group.category} skills`} onClick={() => setCV((current) => ({ ...current, skills: current.skills.filter((_, i) => i !== index) }))}><Trash2 size={12} /></button></div>)}</div><button type="button" className="cv-add" onClick={() => setCV((current) => ({ ...current, skills: [...current.skills, { category: "Additional", items: [] }] }))}><Plus size={12} /> Add skill group</button></section>

            {cv.education.length > 0 && <section><h3 className="cv-heading">Education</h3><div className="space-y-1">{cv.education.map((item, index) => <div key={index} className="grid grid-cols-2 gap-x-3"><Field aria-label="Degree" className="font-bold" placeholder="Degree" value={item.degree} onChange={(v) => setCV((current) => ({ ...current, education: current.education.map((row, i) => i === index ? { ...row, degree: v } : row) }))} /><Field aria-label="Institution" className="text-right" placeholder="Institution" value={item.institution} onChange={(v) => setCV((current) => ({ ...current, education: current.education.map((row, i) => i === index ? { ...row, institution: v } : row) }))} /></div>)}</div></section>}
            {cv.certifications.length > 0 && <section><h3 className="cv-heading">Certifications</h3><div className="space-y-1">{cv.certifications.map((item, index) => <div key={index} className="grid grid-cols-2 gap-x-3"><Field aria-label="Certification" className="font-bold" placeholder="Certification" value={item.name} onChange={(v) => setCV((current) => ({ ...current, certifications: current.certifications.map((row, i) => i === index ? { ...row, name: v } : row) }))} /><Field aria-label="Certification issuer" className="text-right" placeholder="Issuer" value={item.issuer ?? ""} onChange={(v) => setCV((current) => ({ ...current, certifications: current.certifications.map((row, i) => i === index ? { ...row, issuer: optional(v) } : row) }))} /></div>)}</div></section>}
            {cv.projects.length > 0 && <section><h3 className="cv-heading">Projects</h3><div className="space-y-2">{cv.projects.map((item, index) => <div key={item.id}><Field aria-label="Project name" className="w-full font-bold" value={item.name} onChange={(v) => setCV((current) => ({ ...current, projects: current.projects.map((row, i) => i === index ? { ...row, name: v } : row) }))} /><Field multiline aria-label={`${item.name} description`} className="w-full" value={item.description} onChange={(v) => setCV((current) => ({ ...current, projects: current.projects.map((row, i) => i === index ? { ...row, description: v } : row) }))} /></div>)}</div></section>}
            <section><h3 className="cv-heading">Languages</h3><Field aria-label="Languages, comma separated" className="w-full" placeholder="Languages, comma separated" value={cv.languages.join(", ")} onChange={(v) => setCV((current) => ({ ...current, languages: split(v) }))} /></section>
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-4 border-t border-border bg-surface px-4 py-1 text-[11px] text-faint">
        <span>{pages} {pages === 1 ? "page" : "pages"}</span>
        <span>{words} words</span>
        <span>A4</span>
        <span className="ml-auto">{error ? <span role="alert" className="text-bad">{error}</span> : saving ? "Saving…" : dirty ? "Unsaved changes" : "All changes saved"}</span>
      </div>
    </div>
  );
}
