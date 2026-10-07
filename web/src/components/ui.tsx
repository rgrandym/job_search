import { X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type TextareaHTMLAttributes } from "react";
import { createPortal } from "react-dom";
import { cn } from "../lib/utils";

/** A text box that wraps and grows with its text, so nothing is hidden; drag its corner to make
 *  it taller. `singleLine` keeps it to one paragraph (Enter does not add a line break). */
export function AutoText({ value, onChange, singleLine, minRows = 1, className, ...rest }:
  Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange" | "rows"> & {
    value: string;
    onChange: (value: string) => void;
    singleLine?: boolean;
    minRows?: number;
  }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fit = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
  };
  useLayoutEffect(fit, [value]);
  useEffect(() => {
    // Re-fit when the box gets narrower or wider (resizable dialogs, window resizes).
    const el = ref.current;
    if (!el) return;
    let width = el.clientWidth;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth !== width) {
        width = el.clientWidth;
        fit();
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return (
    <textarea
      ref={ref}
      rows={minRows}
      value={value}
      className={cn("input resize-y overflow-hidden leading-snug", className)}
      onChange={(e) => onChange(singleLine ? e.target.value.replace(/\n/g, " ") : e.target.value)}
      onKeyDown={singleLine ? (e) => e.key === "Enter" && e.preventDefault() : undefined}
      {...rest}
    />
  );
}

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
        <span key={v} className="chip min-w-0 max-w-full rounded-md bg-accent-bg text-left text-fg">
          <span className="min-w-0 [overflow-wrap:anywhere]">{v}</span>
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
  resizable,
  document: isDocument,
  headerAction,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
  resizable?: boolean;
  /** Full-window frame whose child owns the scrolling (the CV page editor). */
  document?: boolean;
  headerAction?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  const header = (
    <div className={cn("flex items-center justify-between gap-3 bg-panel", isDocument ? "border-b border-border px-4 py-2.5" : "sticky top-0 z-10 mb-4 pb-2")}>
      <h2 className="truncate text-[15px] font-semibold">{title}</h2>
      <div className="flex shrink-0 items-center gap-3">
        {headerAction}
        <button aria-label="Close" className="text-faint hover:text-fg" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
    </div>
  );
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-2 sm:p-4" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label={title}
        className={cn(
          "card shadow-2xl",
          isDocument
            ? "flex h-[96vh] w-[min(1180px,100%)] flex-col overflow-hidden"
            : "max-h-[85vh] overflow-y-auto p-5 scroll-thin",
          !isDocument && (resizable
            ? "h-[70vh] w-[92vw] min-h-64 min-w-72 max-w-[calc(100vw-2rem)] resize overflow-auto"
            : wide ? "w-full max-w-2xl" : "w-full max-w-md"),
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {header}
        {isDocument ? <div className="min-h-0 flex-1">{children}</div> : children}
      </div>
    </div>,
    document.body,
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
