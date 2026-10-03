import { X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { cn } from "../lib/utils";

/** Free-text list input: Enter or comma adds a chip, Backspace removes the last. */
export function ChipInput({
  value,
  onChange,
  placeholder,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  const add = (raw: string) => {
    const v = raw.trim();
    if (v && !value.includes(v)) onChange([...value, v]);
    setDraft("");
  };
  return (
    <div className="input flex min-h-[34px] min-w-0 max-w-full flex-wrap items-center gap-1 py-1">
      {value.map((v) => (
        <span key={v} className="chip min-w-0 max-w-full bg-accent-bg text-fg">
          <span className="truncate">{v}</span>
          <button className="shrink-0" aria-label={`Remove ${v}`} onClick={() => onChange(value.filter((x) => x !== v))}>
            <X size={11} />
          </button>
        </span>
      ))}
      <input
        className="w-12 min-w-0 max-w-full flex-1 bg-transparent outline-none placeholder:text-faint"
        value={draft}
        placeholder={value.length ? "" : placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => draft && add(draft)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add(draft);
          } else if (e.key === "Backspace" && !draft && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
      />
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block min-w-0 space-y-1">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-faint">{hint}</span>}
    </label>
  );
}

function Knob({ checked }: { checked: boolean }) {
  return (
    <span className={cn("relative block h-4 w-7 shrink-0 rounded-full transition-colors", checked ? "bg-accent" : "bg-border")}>
      <span
        className={cn(
          "absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all",
          checked ? "left-[14px]" : "left-0.5",
        )}
      />
    </span>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
  title,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      title={title}
      onClick={() => onChange(!checked)}
      className="flex w-full items-center justify-between gap-2 text-[13px] text-muted disabled:cursor-not-allowed disabled:opacity-40"
    >
      <span className="min-w-0 text-left">{label}</span>
      <Knob checked={checked} />
    </button>
  );
}

/** A bare on/off switch; `label` names it for screen readers. */
export function Switch({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="mt-0.5 flex shrink-0 disabled:cursor-not-allowed disabled:opacity-40"
    >
      <Knob checked={checked} />
    </button>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T[];
  onChange: (v: T[]) => void;
}) {
  return (
    <div className="flex min-w-0 gap-1">
      {options.map((o) => {
        const on = value.includes(o.value);
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onChange(on ? value.filter((v) => v !== o.value) : [...value, o.value])}
            className={cn(
              "min-w-0 flex-1 rounded-md border px-2 py-1 text-[12px] transition-colors",
              on ? "border-accent bg-accent-bg text-fg" : "border-border bg-surface text-muted hover:text-fg",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label={title}
        className={cn("card max-h-[85vh] w-full overflow-y-auto p-5 shadow-2xl scroll-thin", wide ? "max-w-2xl" : "max-w-md")}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[15px] font-semibold">{title}</h2>
          <button aria-label="Close" className="text-faint hover:text-fg" onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1 p-8 text-center">
      <p className="text-[14px] font-medium text-muted">{title}</p>
      {children && <div className="max-w-sm text-[12px] text-faint">{children}</div>}
    </div>
  );
}
