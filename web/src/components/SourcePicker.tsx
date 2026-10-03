import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Loader2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { AppState, SourceCategory, SourceInfo } from "../lib/types";
import { useSearch } from "../stores/searchStore";
import { CompanyBoards } from "./CompanyBoards";
import { Modal, Switch, Toggle } from "./ui";

const CATEGORIES: [SourceCategory, string][] = [
  ["job_boards", "Job boards"],
  ["company", "Company career sites"],
  ["alerts", "Gmail alerts"],
];

/** Where to search: three categories, each switchable as a whole; clicking a category's name
 * opens its settings, listing every source in it (from the backend catalog) to switch off. */
export function SourcePicker({ state, hasCv }: { state: AppState | undefined; hasCv: boolean }) {
  const s = useSearch();
  const q = s.query;
  const [open, setOpen] = useState<SourceCategory | null>(null);
  const skipped = state?.sources.skipped ?? {};
  const catalog = (state?.sources.catalog ?? []).filter(
    (c) => state?.sources.available.includes(c.name) || Object.hasOwn(skipped, c.name),
  );
  const usable = catalog.filter((c) => !skipped[c.name]).map((c) => c.name);
  // What the backend searches when `sources` is empty: every configured source but the demo.
  const allOn = usable.filter((name) => name !== "demo");
  const selected = q.sources.length ? q.sources.filter((name) => usable.includes(name)) : allOn;
  const members = (cat: SourceCategory) => catalog.filter((c) => c.category === cat);

  const apply = (next: string[]) => {
    if (!next.length) return; // a search needs at least one source
    const isDefault = next.length === allOn.length && allOn.every((name) => next.includes(name));
    s.setQuery({ sources: isDefault ? [] : next });
  };
  const setCategory = (cat: SourceCategory, on: boolean) => {
    const names = members(cat).map((c) => c.name).filter((name) => usable.includes(name));
    if (!on) return apply(selected.filter((name) => !names.includes(name)));
    const wanted = names.filter((name) => !s.disabledSources.includes(name));
    apply([...new Set([...selected, ...(wanted.length ? wanted : names)])]);
  };
  const setSource = (name: string, on: boolean) => {
    const disabled = on ? s.disabledSources.filter((n) => n !== name) : [...new Set([...s.disabledSources, name])];
    const next = on ? [...new Set([...selected, name])] : selected.filter((n) => n !== name);
    if (!next.length) return; // a search needs at least one source
    s.set({ disabledSources: disabled });
    apply(next);
  };

  return (
    <div className="space-y-2 rounded-md border border-border bg-surface p-2">
      {CATEGORIES.map(([cat, label]) => {
        const items = members(cat);
        const live = items.filter((c) => usable.includes(c.name));
        const on = live.filter((c) => selected.includes(c.name));
        const boardsOff = cat === "company" && on.length ? (q.exclude_boards ?? []).length : 0;
        const summary = !live.length
          ? (items.map((c) => skipped[c.name]).find(Boolean) ?? "Not available")
          : on.length
            ? `${on.map((c) => c.label).join(", ")}${boardsOff ? ` · ${boardsOff} board(s) off` : ""}`
            : "Off";
        return (
          <div key={cat} className="flex items-start justify-between gap-2">
            <button
              type="button"
              className="group min-w-0 flex-1 text-left"
              onClick={() => setOpen(cat)}
              title="Choose the sources in this category"
            >
              <span className="flex items-center gap-0.5 text-[13px] text-muted group-hover:text-fg">
                {label} <ChevronRight size={12} className="text-faint" />
              </span>
              <span className="block truncate text-[10px] text-faint">{summary}</span>
            </button>
            <Switch
              checked={on.length > 0}
              disabled={!live.length}
              label={label}
              onChange={(v) => setCategory(cat, v)}
            />
          </div>
        );
      })}
      {CATEGORIES.map(([cat, label]) => (
        <Modal key={cat} open={open === cat} onClose={() => setOpen(null)} title={label} wide={cat === "company"}>
          <div className="space-y-3">
            {members(cat).map((c) => (
              <SourceRow
                key={c.name}
                info={c}
                skipped={skipped[c.name]}
                checked={selected.includes(c.name)}
                onChange={(v) => setSource(c.name, v)}
                hint={c.name === "gmail_alerts" && !(hasCv && s.useCv && s.smart)
                  ? "Skipped unless a CV is selected and Smart match is on"
                  : undefined}
              />
            ))}
            {cat === "alerts" && <IndeedLink titles={q.titles} keywords={q.keywords} location={q.locations[0]} />}
            {cat === "company" && <CompanyBoardList searching={s.loading} />}
          </div>
        </Modal>
      ))}
    </div>
  );
}

function SourceRow({
  info,
  skipped,
  checked,
  onChange,
  hint,
}: {
  info: SourceInfo;
  skipped: string | undefined;
  checked: boolean;
  onChange: (v: boolean) => void;
  hint?: string;
}) {
  return (
    <div>
      <Toggle checked={!skipped && checked} disabled={!!skipped} title={skipped} onChange={onChange} label={info.label} />
      <p className="pr-10 text-[11px] text-faint">{skipped ?? hint ?? info.note}</p>
    </div>
  );
}

function IndeedLink({ titles, keywords, location }: { titles: string[]; keywords: string[]; location?: string }) {
  const url = new URL("https://uk.indeed.com/jobs");
  const terms = [titles[0], ...keywords].filter(Boolean).join(" ");
  if (terms) url.searchParams.set("q", terms);
  if (location) url.searchParams.set("l", location);
  return (
    <a className="text-[11px] text-accent hover:underline" href={url.toString()} target="_blank" rel="noreferrer">
      Search Indeed UK live ↗ (opens Indeed; results are not imported)
    </a>
  );
}

/** Every company job board, each one switchable (`SearchQuery.exclude_boards`). */
function CompanyBoardList({ searching }: { searching: boolean }) {
  const s = useSearch();
  const off = s.query.exclude_boards ?? [];
  const [filter, setFilter] = useState("");
  const boards = useQuery({ queryKey: ["company-boards"], queryFn: api.companyBoards });
  const setOff = (next: string[]) => s.setQuery({ exclude_boards: next });
  const shown = (boards.data ?? []).filter((b) =>
    `${b.companies.join(" ")} ${b.ats}`.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <CompanyBoards searching={searching} />
      {boards.isPending ? (
        <p className="flex items-center gap-1 text-[11px] text-faint">
          <Loader2 size={12} className="animate-spin" /> Loading job boards…
        </p>
      ) : boards.isError ? (
        <p className="text-[11px] text-bad">Could not load the job boards: {(boards.error as Error).message}</p>
      ) : !boards.data.length ? (
        <p className="text-[11px] text-faint">No job boards yet. The next search with company sites finds them.</p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <input
              className="input min-w-[160px] flex-1 py-1 text-[12px]"
              placeholder="Filter companies"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <span className="text-faint">
              {boards.data.length - off.length} of {boards.data.length} on
            </span>
            <button className="text-accent hover:underline" onClick={() => setOff([])}>All on</button>
            <button
              className="text-accent hover:underline"
              onClick={() => setOff([...new Set([...off, ...shown.map((b) => b.key)])])}
            >
              {filter ? "Shown off" : "All off"}
            </button>
          </div>
          <div className="max-h-[45vh] space-y-1.5 overflow-y-auto pr-1 scroll-thin">
            {shown.map((b) => (
              <div key={b.key}>
                <Toggle
                  checked={!off.includes(b.key)}
                  label={b.companies.join(" · ")}
                  onChange={(v) => setOff(v ? off.filter((k) => k !== b.key) : [...off, b.key])}
                />
                <p className="text-[10px] text-faint">{b.ats}</p>
              </div>
            ))}
            {!shown.length && <p className="text-[11px] text-faint">No company matches “{filter}”.</p>}
          </div>
        </>
      )}
    </div>
  );
}
