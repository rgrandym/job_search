import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Loader2, RefreshCw, RotateCcw, Send, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../lib/api";
import type { ClaudeCodeUsage, ClaudeLimitWindow, CodexUsage, LLMView, ModelInfo, UsageWindow } from "../lib/types";
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

function UsagePanel({ llm }: { llm: LLMView }) {
  const chat = useChat();
  const models = useQuery({
    queryKey: ["models", llm.provider],
    queryFn: () => api.models(llm.provider),
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
    enabled: llm.provider === "claude_code",
    refetchInterval: 15_000,
  });
  const catalog = new Map((models.data ?? []).map((model) => [model.id, model]));
  const session = Object.values(chat.modelUsage);
  const cost = session.reduce((total, usage) => {
    const model = catalog.get(usage.model);
    if (model?.input_price == null || model.output_price == null) return total;
    return total + (usage.input * model.input_price + usage.output * model.output_price) / 1_000_000;
  }, 0);
  const costKnown = session.length > 0 && session.every((usage) => {
    const model = catalog.get(usage.model);
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
            purpose="Profile summary, CV and cover letters, second opinions, this assistant"
          />
          <ModelPurpose label={`Screening · ${llm.screening_effort}`} model={llm.screening_model} purpose="First-pass job matching" />
          <p className="text-faint">Provider: {llm.provider}</p>
        </div>
        {llm.provider === "claude_code" && (
          <ClaudeAccount usage={claude.data} loading={claude.isPending} error={claude.error as Error | null} refresh={() => claude.refetch()} />
        )}
        {llm.provider === "codex" && (
          <CodexAccount usage={account.data} loading={account.isPending} error={account.error as Error | null} refresh={() => account.refetch()} />
        )}
        <div className="space-y-1.5">
          <div className="flex justify-between">
            <span className="font-medium text-fg">This session (searches + agent)</span>
            <span>{costKnown ? `$${cost.toFixed(4)}` : llm.provider === "codex" ? "ChatGPT plan" : llm.provider === "claude_code" ? "Claude plan" : "cost unavailable"}</span>
          </div>
          {session.length === 0 ? (
            <p className="text-faint">Usage appears after the first model turn.</p>
          ) : (
            session.map((usage) => <ModelSession key={usage.model} usage={usage} model={catalog.get(usage.model)} />)
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
      <div className="flex justify-between"><span className="font-medium text-fg">{usage.model}</span><span>{usage.estimated && "≈"}{tokens(usage.input + usage.output)} tokens</span></div>
      <p className="text-faint">{tokens(usage.input)} input · {tokens(usage.output)} output{remaining != null ? ` · ${tokens(remaining)} latest context left` : ""}</p>
      <p className="text-faint">{usage.agents.join(", ")} · {usage.purposes.join(", ")}</p>
    </div>
  );
}

const EXAMPLES = [
  "I'd like to move into business development",
  "Add Cambridge to my locations; I won't relocate",
  "I also led a team of 6 at my last job",
];

/** The assistant: plain-language updates to the career intent, preferences and CV facts. */
export function AgentPanel({ ready, llm, hasCv }: { ready: boolean; llm?: LLMView; hasCv: boolean }) {
  const chat = useChat();
  const { query, useCv } = useSearch();
  const qc = useQueryClient();
  const running = chat.running;
  // A finished turn may have changed the intent, the CV's preferences or proposed CV facts.
  useEffect(() => {
    if (running) return;
    for (const key of ["intent", "profiles", "evidence", "state"]) void qc.invalidateQueries({ queryKey: [key] });
  }, [running, qc]);
  const [draft, setDraft] = useState("");
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
    if (!text.trim() || chat.running) return;
    chat.send(text.trim(), query, useCv);
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
          <button title="New conversation" className="hover:text-fg" onClick={chat.reset}>
            <RotateCcw size={13} />
          </button>
        </div>
      </header>

      {llm && <UsagePanel llm={llm} />}

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto overflow-x-hidden p-3 scroll-thin [overflow-wrap:anywhere]">
        {chat.items.length === 0 ? (
          <Empty title="Tell me what to update">
            <p className="mb-2">
              The buttons do the work: <b>Search</b> finds and screens jobs; each job has <b>Tailor CV</b> and{" "}
              <b>Cover letter</b>. Here you can say, in your own words, what should change in your records: your career
              direction, search preferences, or a fact your CV is missing (you approve it before it is added).
            </p>
            {!ready ? (
              <p className="text-warn">Configure an LLM provider in Settings to start.</p>
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
        <div className="flex items-end gap-2">
          <textarea
            className="input min-h-[38px] resize-none"
            rows={2}
            placeholder={ready ? "e.g. avoid roles with heavy travel…" : "Configure an LLM in Settings"}
            disabled={!ready}
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
            <button className="btn-primary h-[38px]" title="Send" disabled={!ready || !draft.trim()} onClick={() => submit()}>
              <Send size={14} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
