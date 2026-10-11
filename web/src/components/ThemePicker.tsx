import { Check, Palette, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { chooseTheme, DEFAULT_THEME, loadTheme, resetTheme, showTheme, themeById, THEMES, type Theme } from "../lib/themes";
import { cn } from "../lib/utils";

function Swatch({ theme }: { theme: Theme }) {
  const [bg, panel, accent] = theme.swatch;
  return (
    <span className="flex h-5 w-8 shrink-0 overflow-hidden rounded border border-border" aria-hidden>
      <span className="w-1/3" style={{ background: bg }} />
      <span className="w-1/3" style={{ background: panel }} />
      <span className="w-1/3" style={{ background: accent }} />
    </span>
  );
}

/** Header button with the themes. The choice is remembered in this browser until the user
 *  picks another or resets to the default. */
export function ThemePicker() {
  const [theme, setTheme] = useState<Theme>(loadTheme);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => showTheme(loadTheme()), []); // the saved theme (index.html shows it before React)
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", esc);
    };
  }, [open]);

  const reset = () => {
    setTheme(resetTheme());
    setOpen(false);
  };

  return (
    <div ref={box} className="relative shrink-0">
      <button
        className="flex items-center text-faint hover:text-fg"
        title={`Theme: ${theme.label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Palette size={15} />
      </button>
      {open && (
        <div role="menu" className="card absolute right-0 top-7 z-50 max-h-[80vh] w-72 overflow-y-auto p-1.5 shadow-xl scroll-thin">
          <p className="label px-2 pb-1 pt-0.5">Theme</p>
          {THEMES.map((t) => (
            <button
              key={t.id}
              role="menuitemradio"
              aria-checked={t.id === theme.id}
              className={cn(
                "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] hover:bg-surface",
                t.id === theme.id ? "text-fg" : "text-muted",
              )}
              onClick={() => {
                chooseTheme(t);
                setTheme(t);
              }}
            >
              <Swatch theme={t} />
              <span className="min-w-0 flex-1 truncate">{t.label}</span>
              {t.id === theme.id && <Check size={13} className="shrink-0 text-accent" />}
            </button>
          ))}
          <button
            className="mt-1 flex w-full items-center gap-2 rounded-md border-t border-border px-2 py-1.5 text-left text-[12px] text-faint hover:bg-surface hover:text-fg"
            onClick={reset}
          >
            <RotateCcw size={12} /> Reset to the default ({themeById(DEFAULT_THEME).label})
          </button>
        </div>
      )}
    </div>
  );
}

