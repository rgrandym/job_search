import { useQuery } from "@tanstack/react-query";
import { Bot, Briefcase, ListChecks, Moon, Settings, SlidersHorizontal, Sun } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { AgentPanel } from "./components/AgentPanel";
import { ResultsPanel } from "./components/ResultsPanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { Sidebar } from "./components/Sidebar";
import { SummaryDialog } from "./components/SummaryDialog";
import { api } from "./lib/api";
import { cn } from "./lib/utils";
import { useChat } from "./stores/chatStore";
import { useSearch } from "./stores/searchStore";

const THEME_KEY = "jobsearch.theme";
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
  return <PanelResizeHandle className="w-px bg-border transition-colors hover:bg-accent data-[resize-handle-active]:bg-accent" />;
}

export default function App() {
  const state = useQuery({ queryKey: ["state"], queryFn: api.state });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [theme, setTheme] = useState(() => localStorage.getItem(THEME_KEY) ?? "dark");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  const llm = state.data?.llm;
  const ready = !!llm?.ready;
  const wide = useMediaQuery(WIDE);
  const [view, setView] = useState<View>("search");
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

  const sidebar = <Sidebar state={state.data} onShowSummary={() => setSummaryOpen(true)} />;
  const results = <ResultsPanel hasCv={!!state.data?.cv_files.selected && ready} />;
  const agent = <AgentPanel ready={ready} llm={llm} hasCv={!!state.data?.cv_files.selected} />;

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
              <span className="truncate">{ready ? `${llm.provider} · ${llm.quality_model} / ${llm.screening_model}` : "Configure LLM"}</span>
            </button>
          )}
          <button
            className="shrink-0 text-faint hover:text-fg"
            title="Toggle theme"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </button>
          <button className="shrink-0 text-faint hover:text-fg" title="Settings" onClick={() => setSettingsOpen(true)}>
            <Settings size={15} />
          </button>
        </div>
      </header>

      {wide ? (
        <PanelGroup direction="horizontal" autoSaveId="jobsearch.layout" className="min-h-0 flex-1">
          <Panel defaultSize={22} minSize={18} maxSize={32} className="min-w-0 bg-bg">
            {sidebar}
          </Panel>
          <Handle />
          <Panel defaultSize={50} minSize={30} className="min-w-0 bg-bg">
            {results}
          </Panel>
          <Handle />
          <Panel defaultSize={28} minSize={20} maxSize={45} className="min-w-0 bg-panel">
            {agent}
          </Panel>
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
      <SummaryDialog open={summaryOpen} onClose={() => setSummaryOpen(false)} />
    </div>
  );
}
