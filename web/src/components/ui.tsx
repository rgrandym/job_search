import { Maximize2, Minus, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode, type TextareaHTMLAttributes } from "react";
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

type Edge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
const EDGES: { edge: Edge; className: string }[] = [
  { edge: "n", className: "inset-x-3 -top-1 h-2 cursor-ns-resize" },
  { edge: "s", className: "inset-x-3 -bottom-1 h-2 cursor-ns-resize" },
  { edge: "e", className: "inset-y-3 -right-1 w-2 cursor-ew-resize" },
  { edge: "w", className: "inset-y-3 -left-1 w-2 cursor-ew-resize" },
  { edge: "ne", className: "-right-1 -top-1 h-3 w-3 cursor-nesw-resize" },
  { edge: "sw", className: "-bottom-1 -left-1 h-3 w-3 cursor-nesw-resize" },
  { edge: "nw", className: "-left-1 -top-1 h-3 w-3 cursor-nwse-resize" },
  { edge: "se", className: "-bottom-1 -right-1 h-3 w-3 cursor-nwse-resize" },
];
const WINDOW_KEY = "jobsearch.window.";
const MIN_W = 288;
const MIN_H = 160;

function savedSize(id: string): { w: number; h: number } | null {
  try {
    const raw = localStorage.getItem(WINDOW_KEY + id);
    const size = raw ? (JSON.parse(raw) as { w: number; h: number }) : null;
    return size && size.w > 0 && size.h > 0 ? size : null;
  } catch {
    return null;
  }
}

function saveSize(id: string, size: { w: number; h: number } | null) {
  try {
    if (size) localStorage.setItem(WINDOW_KEY + id, JSON.stringify({ w: Math.round(size.w), h: Math.round(size.h) }));
    else localStorage.removeItem(WINDOW_KEY + id);
  } catch {
    /* the size just isn't remembered */
  }
}

/** A window over the app. Drag any edge or corner to resize it (the size is remembered per
 *  window; double-click an edge to restore the default). Minimise it, or double-click its title
 *  bar, to fold it into a bar at the bottom right and keep working behind it. */
export function Modal({
  open,
  onClose,
  title,
  children,
  wide,
  resizable,
  document: isDocument,
  headerAction,
  id,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
  /** A larger default size (every window can be resized). */
  resizable?: boolean;
  /** Full-window frame whose child owns the scrolling (the CV page editor). */
  document?: boolean;
  headerAction?: ReactNode;
  /** Remembers this window's size (default: its title). */
  id?: string;
}) {
  const key = id ?? title;
  const [size, setSize] = useState<{ w: number; h: number } | null>(() => savedSize(key));
  const [minimised, setMinimised] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => setSize(savedSize(key)), [key]);
  useEffect(() => {
    if (!open) setMinimised(false);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;

  // The window is centred, so an edge moves by half the size change: grow by twice the drag.
  const drag = (edge: Edge) => (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !box.current) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = box.current.getBoundingClientRect();
    const [x0, y0] = [e.clientX, e.clientY];
    const sx = edge.includes("e") ? 2 : edge.includes("w") ? -2 : 0;
    const sy = edge.includes("s") ? 2 : edge.includes("n") ? -2 : 0;
    let last = { w: rect.width, h: rect.height };
    const move = (ev: PointerEvent) => {
      last = {
        w: Math.min(window.innerWidth - 16, Math.max(MIN_W, rect.width + sx * (ev.clientX - x0))),
        h: Math.min(window.innerHeight - 16, Math.max(MIN_H, rect.height + sy * (ev.clientY - y0))),
      };
      setSize(last);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      saveSize(key, last);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const resetSize = () => {
    setSize(null);
    saveSize(key, null);
  };

  const controls = (
    <div className="flex shrink-0 items-center gap-3">
      {!minimised && headerAction}
      <button
        aria-label={minimised ? "Restore" : "Minimise"}
        title={minimised ? "Restore" : "Minimise (keep working behind it)"}
        className="text-faint hover:text-fg"
        onClick={() => setMinimised((v) => !v)}
      >
        {minimised ? <Maximize2 size={14} /> : <Minus size={16} />}
      </button>
      <button aria-label="Close" className="text-faint hover:text-fg" onClick={onClose}>
        <X size={16} />
      </button>
    </div>
  );
  if (minimised) {
    return createPortal(
      <div
        role="dialog"
        aria-label={title}
        className="card fixed bottom-3 right-3 z-50 flex w-72 max-w-[calc(100vw-1.5rem)] items-center justify-between gap-3 px-3 py-2 shadow-2xl"
        onDoubleClick={() => setMinimised(false)}
      >
        <h2 className="truncate text-[13px] font-semibold">{title}</h2>
        {controls}
      </div>,
      document.body,
    );
  }
  const header = (
    <div
      className={cn("flex items-center justify-between gap-3 bg-panel", isDocument ? "border-b border-border px-4 py-2.5" : "sticky top-0 z-10 mb-4 pb-2")}
      onDoubleClick={(e) => e.target === e.currentTarget && setMinimised(true)}
    >
      <h2 className="truncate text-[15px] font-semibold" onDoubleClick={() => setMinimised(true)}>{title}</h2>
      {controls}
    </div>
  );
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-2 sm:p-4" onMouseDown={onClose}>
      <div
        ref={box}
        role="dialog"
        aria-label={title}
        style={size ? { width: size.w, height: size.h, maxWidth: "calc(100vw - 1rem)", maxHeight: "calc(100vh - 1rem)" } : undefined}
        className={cn(
          "relative flex flex-col",
          isDocument && !size && "h-[96vh] w-[min(1180px,100%)]",
          !isDocument && !size && (resizable
            ? "h-[70vh] w-[92vw] max-w-[calc(100vw-2rem)]"
            : cn("max-h-[85vh] w-full", wide ? "max-w-2xl" : "max-w-md")),
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* The frame clips its content; the resize edges sit outside it, on the frame's border. */}
        <div className="card flex min-h-0 flex-1 flex-col overflow-hidden shadow-2xl">
          {isDocument ? (
            <>
              {header}
              <div className="min-h-0 flex-1">{children}</div>
            </>
          ) : (
            // The content scrolls inside, so the resize edges stay on the window's frame.
            <div className="min-h-0 flex-1 overflow-y-auto p-5 scroll-thin">
              {header}
              {children}
            </div>
          )}
        </div>
        {EDGES.map(({ edge, className }) => (
          <div
            key={edge}
            aria-hidden
            className={cn("absolute z-20 touch-none", className)}
            title="Drag to resize; double-click for the default size"
            onPointerDown={drag(edge)}
            onDoubleClick={resetSize}
          />
        ))}
      </div>
    </div>,
    document.body,
  );
}

const HEIGHT_KEY = "jobsearch.height.";

function savedHeight(id: string): number | null {
  try {
    const value = Number(localStorage.getItem(HEIGHT_KEY + id));
    return value > 0 ? value : null;
  } catch {
    return null;
  }
}

function saveHeight(id: string, height: number | null) {
  try {
    if (height === null) localStorage.removeItem(HEIGHT_KEY + id);
    else localStorage.setItem(HEIGHT_KEY + id, String(Math.round(height)));
  } catch {
    /* the height just isn't remembered */
  }
}

/** A section body whose height the user sets by dragging its bottom edge (or with the arrow
 *  keys on the edge); it then scrolls inside. The height is remembered per `id`; double-click
 *  the edge to fit the content again. */
export function Resizable({ id, children, className, fit, min = 48 }: {
  id: string;
  children: ReactNode;
  className?: string;
  /** Classes while the user has not set a height (e.g. a maximum before it scrolls). */
  fit?: string;
  min?: number;
}) {
  const [height, setHeight] = useState<number | null>(() => savedHeight(id));
  const body = useRef<HTMLDivElement>(null);
  const set = (value: number | null) => {
    const next = value === null ? null : Math.max(min, value);
    setHeight(next);
    saveHeight(id, next);
  };
  const drag = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !body.current) return;
    e.preventDefault();
    const startY = e.clientY;
    const startHeight = body.current.getBoundingClientRect().height;
    let last = startHeight;
    const move = (ev: PointerEvent) => {
      last = Math.max(min, startHeight + ev.clientY - startY);
      setHeight(last);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.removeProperty("cursor");
      saveHeight(id, last);
    };
    document.body.style.cursor = "row-resize";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return (
    <>
      <div
        ref={body}
        style={height === null ? undefined : { height }}
        className={cn(height !== null ? "overflow-y-auto overflow-x-hidden scroll-thin" : fit, className)}
      >
        {children}
      </div>
      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize this section"
        tabIndex={0}
        title="Drag to resize; double-click to fit the content"
        onPointerDown={drag}
        onDoubleClick={() => set(null)}
        onKeyDown={(e) => {
          if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
          e.preventDefault();
          const current = height ?? body.current?.getBoundingClientRect().height ?? min;
          set(current + (e.key === "ArrowDown" ? 24 : -24));
        }}
        className="group/edge -mx-1 mt-1 flex h-2 cursor-row-resize items-center justify-center focus-visible:outline-none"
      >
        <span className="h-1 w-8 rounded-full bg-border transition-colors group-hover/edge:w-full group-hover/edge:bg-accent group-focus-visible/edge:w-full group-focus-visible/edge:bg-accent" />
      </div>
    </>
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
