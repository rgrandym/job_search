import { useQuery } from "@tanstack/react-query";
import { Briefcase, Moon, Settings, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { AgentPanel } from "./components/AgentPanel";
import { ResultsPanel } from "./components/ResultsPanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { Sidebar } from "./components/Sidebar";
import { SummaryDialog } from "./components/SummaryDialog";
import { api } from "./lib/api";
import { cn } from "./lib/utils";

const THEME_KEY = "jobsearch.theme";

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

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-11 shrink-0 items-center justify-between border-b border-border bg-panel px-3">
        <div className="flex items-center gap-2">
          <Briefcase size={16} className="text-accent" />
          <span className="font-semibold">AI Job Search</span>
        </div>
        <div className="flex items-center gap-2">
          {state.isError && <span className="text-[12px] text-bad">Backend unreachable</span>}
          {llm && (
            <button
              onClick={() => setSettingsOpen(true)}
              className="chip border border-border hover:text-fg"
              title="LLM settings"
            >
              <span className={cn("h-1.5 w-1.5 rounded-full", ready ? "bg-good" : "bg-warn")} />
              {ready ? `${llm.provider} · ${llm.orchestrator_model}` : "Configure LLM"}
            </button>
          )}
          <button
            className="text-faint hover:text-fg"
            title="Toggle theme"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </button>
          <button className="text-faint hover:text-fg" title="Settings" onClick={() => setSettingsOpen(true)}>
            <Settings size={15} />
          </button>
        </div>
      </header>

      <PanelGroup direction="horizontal" autoSaveId="jobsearch.layout" className="min-h-0 flex-1">
        <Panel defaultSize={22} minSize={18} maxSize={32} className="bg-bg">
          <Sidebar state={state.data} onShowSummary={() => setSummaryOpen(true)} />
        </Panel>
        <Handle />
        <Panel defaultSize={50} minSize={30} className="bg-bg">
          <ResultsPanel hasCv={!!state.data?.cv_files.selected && ready} />
        </Panel>
        <Handle />
        <Panel defaultSize={28} minSize={20} maxSize={45} className="bg-panel">
          <AgentPanel ready={ready} llm={llm} />
        </Panel>
      </PanelGroup>

      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} llm={llm} />
      <SummaryDialog open={summaryOpen} onClose={() => setSummaryOpen(false)} />
    </div>
  );
}
