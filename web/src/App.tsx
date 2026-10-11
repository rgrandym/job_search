import { useQuery } from "@tanstack/react-query";
import { Bot, Briefcase, ListChecks, PanelLeft, PanelRight, PanelTop, Settings, SlidersHorizontal } from "lucide-react";
import { Fragment, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Panel, PanelGroup, PanelResizeHandle, type ImperativePanelHandle } from "react-resizable-panels";
import { AgentPanel } from "./components/AgentPanel";
import { ResultsPanel } from "./components/ResultsPanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { Sidebar } from "./components/Sidebar";
import { SummaryDialog } from "./components/SummaryDialog";
import { ThemePicker } from "./components/ThemePicker";
import { api } from "./lib/api";
import { useRestoreLastSearch } from "./lib/useRestoreLastSearch";
import { cn } from "./lib/utils";
import { useChat } from "./stores/chatStore";
import { useSearch } from "./stores/searchStore";

const VIEW_KEY = "jobsearch.view"; // the narrow-screen panel open last
const WIDE = "(min-width: 1024px)"; // three resizable columns from here; tabs below

type View = "search" | "results" | "agent";

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function TabButton({ active, onClick, icon, children }: { active: boolean; onClick: () => void; icon: ReactNode; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex flex-1 items-center justify-center gap-1.5 border-b-2 py-2 text-[12px]",
        active ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
      )}
    >
      {icon} {children}
    </button>
  );
}

function Handle() {
  return (
    <PanelResizeHandle
      className="w-1 shrink-0 border-x border-transparent bg-border bg-clip-content transition-colors hover:bg-accent data-[resize-handle-active]:bg-accent"
      title="Drag to resize; drag to the edge to collapse"
    />
  );
}

// A collapsed column keeps a slim rail (percent of the window width) that opens it again.
const RAIL = 2.5;

interface ColumnSpec {
  id: string;
  label: string;
  icon: ReactNode;
  defaultSize: number;
  minSize: number;
  maxSize?: number;
  className: string;
}

/** One of the three columns: resizable by dragging its edges, collapsible to a rail (from
 *  the header buttons, the rail itself, or by dragging it past its minimum width). The layout,
 *  collapsed columns included, is remembered (`autoSaveId`). */
function Column({ spec, order, panel, onToggle, children }: {
  spec: ColumnSpec;
  order: number;
  panel: RefObject<ImperativePanelHandle>;
  onToggle: (collapsed: boolean) => void;
  children: ReactNode;
}) {
  // The group reports the restored layout through these callbacks too (a column saved
  // collapsed opens collapsed).
  const [collapsed, setCollapsed] = useState(false);
  const change = (value: boolean) => {
    setCollapsed(value);
    onToggle(value);
  };
  return (
    <Panel
      id={spec.id}
      order={order}
      ref={panel}
      collapsible
      collapsedSize={RAIL}
      defaultSize={spec.defaultSize}
      minSize={spec.minSize}
      maxSize={spec.maxSize}
      onCollapse={() => change(true)}
      onExpand={() => change(false)}
      className={cn("min-w-0", spec.className)}
    >
      {collapsed ? (
        <button
          className="flex h-full w-full flex-col items-center gap-2 pt-3 text-faint hover:bg-surface hover:text-fg"
          title={`Show ${spec.label}`}
          onClick={() => panel.current?.expand()}
        >
          {spec.icon}
          <span className="text-[11px] font-medium [writing-mode:vertical-rl]">{spec.label}</span>
        </button>
      ) : (
        children
      )}
    </Panel>
  );
}

const COLUMNS: ColumnSpec[] = [
  { id: "search", label: "Search", icon: <SlidersHorizontal size={14} />, defaultSize: 22, minSize: 16, maxSize: 40, className: "bg-bg" },
  { id: "results", label: "Results", icon: <ListChecks size={14} />, defaultSize: 50, minSize: 25, className: "bg-bg" },
  { id: "assistant", label: "Assistant", icon: <Bot size={14} />, defaultSize: 28, minSize: 18, maxSize: 55, className: "bg-panel" },
];
const TOGGLE_ICONS = [<PanelLeft key="l" size={15} />, <PanelTop key="t" size={15} />, <PanelRight key="r" size={15} />];

export default function App() {
  // Output files can change outside the browser; keep the CV library current.
  const state = useQuery({ queryKey: ["state"], queryFn: api.state, refetchOnWindowFocus: true, refetchInterval: 15_000 });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [summaryProfileKey, setSummaryProfileKey] = useState<string | null>(null);
  useRestoreLastSearch();

  const llm = state.data?.llm;
  const ready = !!llm?.ready;
  const wide = useMediaQuery(WIDE);
  const [view, setView] = useState<View>(() => {
    const saved = localStorage.getItem(VIEW_KEY);
    return saved === "results" || saved === "agent" ? saved : "search";
  });
  useEffect(() => localStorage.setItem(VIEW_KEY, view), [view]);
  const searching = useSearch((st) => st.loading);
  const chatting = useChat((st) => st.running);
  const matches = useSearch((st) => st.outcome?.report.matches.length);

  // On narrow screens, follow the work: a search shows Results, a chat request shows Agent.
  useEffect(() => {
    if (searching) setView("results");
  }, [searching]);
  useEffect(() => {
    if (chatting) setView("agent");
  }, [chatting]);

  const sidebar = <Sidebar state={state.data} onShowSummary={(key) => { setSummaryProfileKey(key ?? null); setSummaryOpen(true); }} />;
  const results = <ResultsPanel hasCv={!!state.data?.cv_files.selected && ready} />;
  const agent = <AgentPanel ready={ready} llm={llm} hasCv={!!state.data?.cv_files.selected} />;
  const panels = [useRef<ImperativePanelHandle>(null), useRef<ImperativePanelHandle>(null), useRef<ImperativePanelHandle>(null)];
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const toggles = useRef(COLUMNS.map((c) => (value: boolean) => setCollapsed((st) => (st[c.id] === value ? st : { ...st, [c.id]: value })))).current;

  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden">
      <header className="flex h-11 shrink-0 items-center justify-between gap-2 border-b border-border bg-panel px-3">
        <div className="flex min-w-0 items-center gap-2">
          <Briefcase size={16} className="shrink-0 text-accent" />
          <span className="truncate font-semibold">AI Job Search</span>
        </div>
        <div className="flex min-w-0 items-center gap-2">
          {state.isError && <span className="truncate text-[12px] text-bad">Backend unreachable</span>}
          {llm && (
            <button
              onClick={() => setSettingsOpen(true)}
              className="chip min-w-0 border border-border hover:text-fg"
              title="LLM settings"
            >
              <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", ready ? "bg-good" : "bg-warn")} />
              <span className="truncate">{ready
                  ? `${llm.provider} · ${llm.quality_model} / ${llm.screening_model}` +
                    (llm.profile_model ? ` · profile ${llm.profile_provider ?? llm.provider} ${llm.profile_model}` : "") +
                    (llm.cv_model ? ` · CVs ${llm.cv_provider ?? llm.provider} ${llm.cv_model}` : "") +
                    (llm.letter_model ? ` · letters ${llm.letter_provider ?? llm.provider} ${llm.letter_model}` : "")
                  : "Configure LLM"}</span>
            </button>
          )}
          {wide && (
            <div className="flex shrink-0 items-center gap-1.5 border-r border-border pr-2">
              {COLUMNS.map((c, i) => (
                <button
                  key={c.id}
                  className={cn("hover:text-fg", collapsed[c.id] ? "text-faint opacity-50" : "text-faint")}
                  title={`${collapsed[c.id] ? "Show" : "Hide"} the ${c.label.toLowerCase()} panel`}
                  aria-pressed={!collapsed[c.id]}
                  onClick={() => {
                    const panel = panels[i].current;
                    if (panel?.isCollapsed()) panel.expand();
                    else panel?.collapse();
                  }}
                >
                  {TOGGLE_ICONS[i]}
                </button>
              ))}
            </div>
          )}
          <ThemePicker />
          <button className="shrink-0 text-faint hover:text-fg" title="Settings" onClick={() => setSettingsOpen(true)}>
            <Settings size={15} />
          </button>
        </div>
      </header>

      {wide ? (
        <PanelGroup direction="horizontal" autoSaveId="jobsearch.layout.v2" className="min-h-0 flex-1">
          {[sidebar, results, agent].map((content, i) => (
            <Fragment key={COLUMNS[i].id}>
              {i > 0 && <Handle />}
              <Column spec={COLUMNS[i]} order={i} panel={panels[i]} onToggle={toggles[i]}>
                {content}
              </Column>
            </Fragment>
          ))}
        </PanelGroup>
      ) : (
        <>
          <nav className="flex shrink-0 border-b border-border bg-panel">
            <TabButton active={view === "search"} onClick={() => setView("search")} icon={<SlidersHorizontal size={13} />}>
              Search
            </TabButton>
            <TabButton active={view === "results"} onClick={() => setView("results")} icon={<ListChecks size={13} />}>
              Results{matches != null && <span className="text-faint">{matches}</span>}
            </TabButton>
            <TabButton active={view === "agent"} onClick={() => setView("agent")} icon={<Bot size={13} />}>
              Assistant
            </TabButton>
          </nav>
          {/* All three stay mounted, so switching tabs keeps scroll positions and drafts. */}
          <div className="relative min-h-0 flex-1">
            <div className={cn("absolute inset-0 bg-bg", view !== "search" && "hidden")}>{sidebar}</div>
            <div className={cn("absolute inset-0 bg-bg", view !== "results" && "hidden")}>{results}</div>
            <div className={cn("absolute inset-0 bg-panel", view !== "agent" && "hidden")}>{agent}</div>
          </div>
        </>
      )}

      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} llm={llm} />
      <SummaryDialog open={summaryOpen} onClose={() => setSummaryOpen(false)} initialProfileKey={summaryProfileKey} />
    </div>
  );
}
