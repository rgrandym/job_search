import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Check, ChevronDown, Loader2, RefreshCw, RotateCcw, Send, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../lib/api";
import type { ClaudeCodeUsage, ClaudeLimitWindow, CodexUsage, LLMView, ModelInfo, Provider, UsageWindow } from "../lib/types";
import { cn } from "../lib/utils";
import { useChat, type ChatItem, type SessionModelUsage } from "../stores/chatStore";
import { useSearch } from "../stores/searchStore";
import { Empty } from "./ui";

function Activity({ item }: { item: Extract<ChatItem, { kind: "activity" }> }) {
  return (
    <div className="text-[11px] text-faint" style={{ paddingLeft: item.depth * 12 }} title={item.detail}>
      <span className={cn("font-medium", item.ok === false ? "text-bad" : "text-muted")}>{item.agent}</span>{" "}
      <span className="break-words">{item.text.length > 220 ? `${item.text.slice(0, 220)}…` : item.text}</span>
    </div>
  );
}

const tokens = (value: number) => {
  if (value < 1_000) return value.toLocaleString();
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1)}k`;
  return `${(value / 1_000_000).toFixed(1)}m`;
};

function resetLabel(resetsAt: number | null) {
  if (!resetsAt) return "reset unavailable";
  return `resets ${new Date(resetsAt * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}`;
}

/** One allowance bar, shared by the OpenAI and Claude plans so both read the same way. */
function UsageBar({ label, remaining, value, tone = "text-fg", footer }: {
  label: string;
  remaining: number | null;
  value: string;
  tone?: string;
  footer: string;
}) {
  return (
    <div>
      <div className="flex justify-between text-[11px]">
        <span className="text-muted">{label}</span>
        <span className={tone}>{value}</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface">
        {/* Unknown share: a neutral full track, so it never reads as an empty allowance. */}
        <div className={cn("h-full", remaining == null ? "bg-border" : "bg-accent")} style={{ width: `${remaining ?? 100}%` }} />
      </div>
      <p className="mt-0.5 text-[10px] text-faint">{footer}</p>
    </div>
  );
}

function Limit({ label, window }: { label: string; window: UsageWindow | null }) {
  if (!window) return null;
  return (
    <UsageBar
      label={label}
      remaining={window.remaining_percent}
      value={`${window.remaining_percent}% left`}
      footer={resetLabel(window.resets_at)}
    />
  );
}

const WINDOW_LABEL: Record<string, string> = {
  five_hour: "5-hour allowance",
  seven_day: "Weekly allowance",
  seven_day_opus: "Weekly Opus allowance",
  seven_day_sonnet: "Weekly Sonnet allowance",
};

const CLAUDE_STATUS: Record<string, { text: string; tone: string }> = {
  allowed: { text: "within limit", tone: "text-good" },
  allowed_warning: { text: "nearing limit", tone: "text-warn" },
  rejected: { text: "limit reached", tone: "text-bad" },
};

function ClaudeLimit({ window }: { window: ClaudeLimitWindow }) {
  const status = CLAUDE_STATUS[window.status ?? ""] ?? { text: window.status ?? "unknown", tone: "text-muted" };
  // Anthropic only reports utilisation as a window nears its limit; a rejected window is spent.
  const used = window.utilization != null ? Math.round(window.utilization * 100) : window.status === "rejected" ? 100 : null;
  const remaining = used == null ? null : Math.max(0, 100 - used);
  return (
    <UsageBar
      label={WINDOW_LABEL[window.window] ?? window.window}
      remaining={remaining}
      value={remaining != null ? `${remaining}% left` : status.text}
      tone={remaining != null && window.status === "allowed" ? "text-fg" : status.tone}
      footer={`${resetLabel(window.resets_at)}${remaining == null ? " · exact % shown as it nears the limit" : ""}${window.using_overage ? " · using extra usage" : ""}`}
    />
  );
}

function ClaudeAccount({ usage, loading, error, refresh }: { usage?: ClaudeCodeUsage; loading: boolean; error: Error | null; refresh: () => void }) {
  return (
    <div className="space-y-2 rounded-md border border-border p-2">
      <div className="flex items-center justify-between">
        <span className="font-medium text-fg">Claude plan</span>
        <button title="Refresh plan usage" className="text-faint hover:text-fg" onClick={refresh}><RefreshCw size={11} /></button>
      </div>
      {loading && <p className="text-faint">Loading plan limits…</p>}
      {error && <p className="text-bad">Usage unavailable: {error.message}</p>}
      {usage && usage.windows.length === 0 && (
        <p className="text-faint">Limits appear after the first Claude Code call in this app (a search or an agent turn).</p>
      )}
      {usage?.windows.map((window) => <ClaudeLimit key={window.window} window={window} />)}
      {usage && usage.windows.length > 0 && (
        <p className="text-faint">
          Reported by Anthropic on each call; the remaining percentage is only shared as a window nears its limit. Usage is
          shared with Claude Code in VS Code.
        </p>
      )}
    </div>
  );
}

function UsagePanel({ llm, chatProvider }: { llm: LLMView; chatProvider: Provider | null }) {
  const chat = useChat();
  const models = useQuery({
    queryKey: ["models", llm.provider],
    queryFn: () => api.models(llm.provider),
    staleTime: 5 * 60_000,
  });
  const claudeModels = useQuery({
    queryKey: ["models", "claude_code"],
    queryFn: () => api.models("claude_code"),
    enabled: llm.provider !== "claude_code",
    staleTime: 5 * 60_000,
  });
  const account = useQuery({
    queryKey: ["codex-usage"],
    queryFn: api.codexUsage,
    enabled: llm.provider === "codex",
    refetchInterval: 60_000,
  });
  const claude = useQuery({
    queryKey: ["claude-code-usage"],
    queryFn: api.claudeCodeUsage,
    enabled: llm.provider === "claude_code" || chatProvider === "claude_code" || Object.values(chat.modelUsage).some((usage) => usage.provider === "claude_code"),
    refetchInterval: 15_000,
  });
  const catalog = new Map((models.data ?? []).map((model) => [model.id, model]));
  const claudeCatalog = new Map((llm.provider === "claude_code" ? models.data ?? [] : claudeModels.data ?? []).map((model) => [model.id, model]));
  const modelFor = (usage: SessionModelUsage) => usage.provider === "claude_code" ? claudeCatalog.get(usage.model) : catalog.get(usage.model);
  const session = Object.values(chat.modelUsage);
  const cost = session.reduce((total, usage) => {
    const model = modelFor(usage);
    if (model?.input_price == null || model.output_price == null) return total;
    return total + (usage.input * model.input_price + usage.output * model.output_price) / 1_000_000;
  }, 0);
  const costKnown = session.length > 0 && session.every((usage) => {
    const model = modelFor(usage);
    return model?.input_price != null && model.output_price != null;
  });

  return (
    <details open className="max-h-[40vh] shrink-0 overflow-y-auto border-b border-border bg-panel px-3 py-2 text-[11px] scroll-thin">
      <summary className="cursor-pointer text-muted">Models, limits & live usage</summary>
      <div className="mt-2 space-y-3">
        <div className="space-y-1 rounded-md bg-surface p-2">
          <ModelPurpose
            label={`Quality · ${llm.quality_effort}`}
            model={llm.quality_model}
            purpose="CV and cover letters, second opinions, assistant default"
          />
          <ModelPurpose label={`Screening · ${llm.screening_effort}`} model={llm.screening_model} purpose="First-pass job matching, or chat when selected" />
          <p className="text-faint">Provider: {llm.provider}</p>
        </div>
        {(llm.provider === "claude_code" || chatProvider === "claude_code" || session.some((usage) => usage.provider === "claude_code")) && (
          <ClaudeAccount usage={claude.data} loading={claude.isPending} error={claude.error as Error | null} refresh={() => claude.refetch()} />
        )}
        {llm.provider === "codex" && (
          <CodexAccount usage={account.data} loading={account.isPending} error={account.error as Error | null} refresh={() => account.refetch()} />
        )}
        <div className="space-y-1.5">
          <div className="flex justify-between">
            <span className="font-medium text-fg">This tab (searches + chat)</span>
            <span>{costKnown ? `$${cost.toFixed(4)}` : "cost unavailable / plan usage"}</span>
          </div>
          {session.length === 0 ? (
            <p className="text-faint">Usage appears after the first model turn.</p>
          ) : (
            session.map((usage) => <ModelSession key={`${usage.provider ?? "current"}:${usage.model}`} usage={usage} model={modelFor(usage)} />)
          )}
          {llm.provider === "codex" && <p className="text-faint">Codex CLI token counts are estimates; account-limit percentages above are reported by OpenAI.</p>}
        </div>
      </div>
    </details>
  );
}

function ModelPurpose({ label, model, purpose }: { label: string; model: string; purpose: string }) {
  return <p><span className="font-medium text-fg">{label}:</span> {model}<br /><span className="text-faint">{purpose}</span></p>;
}

function CodexAccount({ usage, loading, error, refresh }: { usage?: CodexUsage; loading: boolean; error: Error | null; refresh: () => void }) {
  return (
    <div className="space-y-2 rounded-md border border-border p-2">
      <div className="flex items-center justify-between">
        <span className="font-medium text-fg">OpenAI account {usage?.plan_type && `· ${usage.plan_type}`}</span>
        <button title="Refresh account usage" className="text-faint hover:text-fg" onClick={refresh}><RefreshCw size={11} /></button>
      </div>
      {loading && <p className="text-faint">Loading account limits…</p>}
      {error && <p className="text-bad">Usage unavailable: {error.message}</p>}
      {usage && <>
        {usage.ordinary_usage_allowed === false && <p className="text-bad">Ordinary Codex usage is currently unavailable.</p>}
        <Limit label="5-hour allowance" window={usage.primary} />
        <Limit label="Weekly allowance" window={usage.secondary} />
        <p className="text-faint">
          {usage.lifetime_tokens != null && `${tokens(usage.lifetime_tokens)} lifetime tokens`}
          {usage.credits.has_credits && ` · ${usage.credits.balance ?? "—"} credits`}
        </p>
      </>}
    </div>
  );
}

function ModelSession({ usage, model }: { usage: SessionModelUsage; model?: ModelInfo }) {
  const remaining = model?.context_length == null ? null : Math.max(0, model.context_length - usage.latestContext);
  return (
    <div className="rounded-md bg-surface p-2">
      <div className="flex justify-between"><span className="font-medium text-fg">{usage.model}{usage.provider === "claude_code" ? " · Claude Code" : ""}</span><span>{usage.estimated && "≈"}{tokens(usage.input + usage.output)} tokens</span></div>
      <p className="text-faint">{tokens(usage.input)} input · {tokens(usage.output)} output{remaining != null ? ` · ${tokens(remaining)} latest context left` : ""}</p>
      <p className="text-faint">{usage.agents.join(", ")} · {usage.purposes.join(", ")}</p>
    </div>
  );
}

const EXAMPLES = [
  "Search for roles that fit my current profile",
  "Save the first two matches and tailor a CV for the best one",
  "Add Cambridge to my locations; I won't relocate",
];

/** The assistant: plain-language updates to profiles, intent, preferences and CV facts. */
export function AgentPanel({ ready, llm, hasCv }: { ready: boolean; llm?: LLMView; hasCv: boolean }) {
  const chat = useChat();
  const { query, useCv, smart, threshold, widen, profileKey } = useSearch();
  const qc = useQueryClient();
  const running = chat.running;
  // A finished turn may have changed the intent, the CV's preferences or proposed CV facts.
  useEffect(() => {
    if (running) return;
    for (const key of ["intent", "profiles", "evidence", "state", "history", "saved", "tracker", "labels", "learning", "tailored-cvs", "cover-letters", "company-boards", "codex-usage", "claude-code-usage"]) void qc.invalidateQueries({ queryKey: [key] });
  }, [running, qc]);
  const [draft, setDraft] = useState("");
  const [chatChoice, setChatChoice] = useState("quality");
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const pickerButtonRef = useRef<HTMLButtonElement>(null);
  const chatModels = useQuery({
    queryKey: ["models", llm?.provider],
    queryFn: () => api.models(llm!.provider),
    enabled: !!llm,
    staleTime: 5 * 60_000,
  });
  const claudeModels = useQuery({
    queryKey: ["models", "claude_code"],
    queryFn: () => api.models("claude_code"),
    enabled: !!llm && llm.provider !== "claude_code",
    staleTime: 5 * 60_000,
  });
  const chatProviders = useQuery({
    queryKey: ["chat-providers", llm?.provider],
    queryFn: api.chatProviders,
    enabled: !!llm,
    refetchInterval: 30_000,
  });
  const claudeSelected = chatChoice.startsWith("claude-code:");
  const chatReady = claudeSelected ? chatProviders.data?.claude_code === true : ready;
  const otherModels = chatModels.data?.filter((model) => model.tools === true && model.id !== llm?.quality_model && model.id !== llm?.screening_model) ?? [];
  const extraClaudeModels = llm?.provider === "claude_code" ? [] : claudeModels.data?.filter((model) => model.tools === true) ?? [];
  const selectedModel = chatChoice === "quality" ? `Quality · ${llm?.quality_model}`
    : chatChoice === "screening" ? `Screening · ${llm?.screening_model}`
      : claudeSelected ? `Claude Code · ${extraClaudeModels.find((model) => `claude-code:${model.id}` === chatChoice)?.name ?? chatChoice.slice(12)}`
        : `${llm?.provider === "claude_code" ? "Claude Code · " : ""}${otherModels.find((model) => `model:${model.id}` === chatChoice)?.name ?? chatChoice}`;
  useEffect(() => {
    if (!pickerOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setPickerOpen(false);
    };
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPickerOpen(false);
        pickerButtonRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeEscape);
    };
  }, [pickerOpen]);
  const endRef = useRef<HTMLDivElement>(null);
  const connect = chat.connect;

  // Block bodies: an effect may only return a cleanup function, and newer browsers return a
  // Promise from scrollIntoView, which React would then call on unmount.
  useEffect(() => {
    connect();
  }, [connect]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chat.items.length, chat.status]);

  const submit = (text = draft) => {
    if (!text.trim() || chat.running || !chatReady) return;
    const chatRole = chatChoice === "screening" ? "screening" : "quality";
    const chatModel = claudeSelected ? chatChoice.slice("claude-code:".length) : chatChoice.startsWith("model:") ? chatChoice.slice(6) : null;
    const chatProvider = claudeSelected ? "claude_code" : null;
    chat.send(text.trim(), query, useCv, chatRole, chatModel, chatProvider, { smart, threshold, widen, profileKey });
    setDraft("");
  };

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-2.5">
        <div className="flex items-center gap-2">
          <Bot size={15} className="text-accent" />
          <span className="font-semibold">Assistant</span>
          <span className={cn("h-1.5 w-1.5 rounded-full", chat.connected ? "bg-good" : "bg-bad")} />
        </div>
        <div className="flex items-center gap-2 text-[11px] text-faint">
          {chat.tokens.input + chat.tokens.output > 0 && (
            <span>{((chat.tokens.input + chat.tokens.output) / 1000).toFixed(1)}k tok</span>
          )}
          <button title="New conversation" className="hover:text-fg disabled:opacity-50" disabled={chat.running} onClick={chat.reset}>
            <RotateCcw size={13} />
          </button>
        </div>
      </header>

      {llm && <UsagePanel llm={llm} chatProvider={claudeSelected ? "claude_code" : null} />}

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto overflow-x-hidden p-3 scroll-thin [overflow-wrap:anywhere]">
        {chat.items.length === 0 ? (
          <Empty title="What would you like to do?">
            <p className="mb-2">
              I can search, save and track jobs, create CVs and cover letters, and update your profiles and preferences.
              New CV facts wait for your review before they are added.
            </p>
            {!chatReady ? (
              <p className="text-warn">Connect the selected chat provider in Settings to start.</p>
            ) : !hasCv ? (
              <p>Select or upload a CV on the left first.</p>
            ) : (
              <div className="space-y-1.5">
                {EXAMPLES.map((e) => (
                  <button key={e} className="btn-ghost w-full justify-start text-left text-[12px]" onClick={() => setDraft(e)}>
                    “{e}”
                  </button>
                ))}
              </div>
            )}
          </Empty>
        ) : (
          chat.items.map((item, i) => {
            switch (item.kind) {
              case "user":
                return (
                  <div key={i} className="ml-8 whitespace-pre-wrap rounded-lg bg-accent-bg px-3 py-2 text-[13px]">
                    {item.text}
                  </div>
                );
              case "assistant":
                return (
                  <div key={i} className="prose-chat text-[13px] leading-relaxed">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.text}</ReactMarkdown>
                  </div>
                );
              case "activity":
                return <Activity key={i} item={item} />;
              case "error":
                return (
                  <p key={i} className="rounded-md border border-bad/40 px-2 py-1.5 text-[12px] text-bad">
                    {item.text}
                  </p>
                );
            }
          })
        )}
        {chat.running && chat.status && (
          <p className="flex items-center gap-1.5 text-[12px] text-accent">
            <Loader2 size={12} className="animate-spin" /> {chat.status}
          </p>
        )}
        <div ref={endRef} />
      </div>

      <div className="shrink-0 border-t border-border p-2.5">
        {llm && <div ref={pickerRef} className="relative mb-2">
          <span className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-faint">Chat model</span>
          <button
            ref={pickerButtonRef}
            type="button"
            aria-label="Choose chat model"
            aria-expanded={pickerOpen}
            aria-controls="chat-model-options"
            disabled={running}
            onClick={() => setPickerOpen((open) => !open)}
            className={cn("flex w-full items-center justify-between gap-2 rounded-md border bg-surface px-2.5 py-1.5 text-left text-[12px] text-fg transition-colors hover:border-accent focus:outline-none focus:border-accent disabled:opacity-50", pickerOpen ? "border-accent" : "border-border")}
          >
            <span className="min-w-0 truncate">{selectedModel}</span>
            <ChevronDown size={13} className={cn("shrink-0 text-faint transition-transform", pickerOpen && "rotate-180")} />
          </button>
          {pickerOpen && <div id="chat-model-options" className="absolute bottom-full left-0 z-50 mb-1.5 max-h-[46vh] w-full overflow-y-auto rounded-lg border border-border bg-panel p-1.5 shadow-2xl scroll-thin">
            <p className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-faint">Configured models</p>
            {([
              { value: "quality", name: llm.quality_model, detail: `Quality · ${llm.quality_effort}` },
              { value: "screening", name: llm.screening_model, detail: `Screening · ${llm.screening_effort}` },
            ]).map((option) => <button key={option.value} type="button" aria-current={chatChoice === option.value ? "true" : undefined} onClick={() => { setChatChoice(option.value); setPickerOpen(false); }} className={cn("flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface focus:bg-surface focus:outline-none", chatChoice === option.value && "bg-accent-bg")}>
              <span className="min-w-0"><span className="block truncate text-[12px] text-fg">{option.name}</span><span className="block text-[10px] text-faint">{option.detail}</span></span>
              {chatChoice === option.value && <Check size={13} className="shrink-0 text-accent" />}
            </button>)}
            {otherModels.length > 0 && <><p className="mt-1 border-t border-border px-2 pt-2 pb-1 text-[10px] font-medium uppercase tracking-wide text-faint">{llm.provider === "claude_code" ? "More Claude Code models" : "Other available models"}</p>
              {otherModels.map((model) => <button key={model.id} type="button" onClick={() => { setChatChoice(`model:${model.id}`); setPickerOpen(false); }} className={cn("flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-fg hover:bg-surface focus:bg-surface focus:outline-none", chatChoice === `model:${model.id}` && "bg-accent-bg")}>
                <span className="truncate">{model.name}</span>{chatChoice === `model:${model.id}` && <Check size={13} className="shrink-0 text-accent" />}
              </button>)}
            </>}
            {extraClaudeModels.length > 0 && <><p className="mt-1 border-t border-border px-2 pt-2 pb-1 text-[10px] font-medium uppercase tracking-wide text-faint">Claude Code · {chatProviders.data?.claude_code ? "Connected" : "Connect in Settings"}</p>
              {extraClaudeModels.map((model) => <button key={model.id} type="button" onClick={() => { setChatChoice(`claude-code:${model.id}`); setPickerOpen(false); }} className={cn("flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-fg hover:bg-surface focus:bg-surface focus:outline-none", chatChoice === `claude-code:${model.id}` && "bg-accent-bg")}>
                <span className="truncate">{model.name}</span>{chatChoice === `claude-code:${model.id}` && <Check size={13} className="shrink-0 text-accent" />}
              </button>)}
            </>}
          </div>}
        </div>}
        <div className="flex items-end gap-2">
          <textarea
            className="input min-h-[38px] resize-none"
            rows={2}
            placeholder={chatReady ? "e.g. avoid roles with heavy travel…" : "Connect the selected chat provider in Settings"}
            disabled={!chatReady}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          {chat.running ? (
            <button className="btn-ghost h-[38px]" title="Stop" onClick={chat.cancel}>
              <Square size={14} />
            </button>
          ) : (
            <button className="btn-primary h-[38px]" title="Send" disabled={!chatReady || !draft.trim()} onClick={() => submit()}>
              <Send size={14} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
