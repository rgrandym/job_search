import { useQuery } from "@tanstack/react-query";
import { Bot, Download, Loader2, RefreshCw, RotateCcw, Send, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../lib/api";
import type { CodexUsage, LLMView, ModelInfo, UsageWindow } from "../lib/types";
import { cn } from "../lib/utils";
import { useChat, type ChatItem, type SessionModelUsage } from "../stores/chatStore";
import { useSearch } from "../stores/searchStore";
import { Empty } from "./ui";

const SUGGESTIONS = [
  "Find roles that truly match my CV in my preferred locations",
  "Which of these matches should I apply to first, and why?",
  "Tailor my CV to the best match",
];

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

function resetLabel(window: UsageWindow | null) {
  if (!window?.resets_at) return "reset unavailable";
  return `resets ${new Date(window.resets_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}`;
}

function Limit({ label, window }: { label: string; window: UsageWindow | null }) {
  if (!window) return null;
  return (
    <div>
      <div className="flex justify-between text-[11px]">
        <span className="text-muted">{label}</span>
        <span>{window.remaining_percent}% left</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface">
        <div className="h-full bg-accent" style={{ width: `${window.remaining_percent}%` }} />
      </div>
      <p className="mt-0.5 text-[10px] text-faint">{resetLabel(window)}</p>
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
    <details open className="border-b border-border bg-panel px-3 py-2 text-[11px]">
      <summary className="cursor-pointer text-muted">Models, limits & live usage</summary>
      <div className="mt-2 space-y-3">
        <div className="space-y-1 rounded-md bg-surface p-2">
          <ModelPurpose label="Orchestrator" model={llm.orchestrator_model} purpose="Planning, delegation and profile summaries" />
          <ModelPurpose label="Workers" model={llm.worker_model} purpose="Search, job matching, CV parsing and tailoring" />
          <p className="text-faint">Provider: {llm.provider} · effort: {llm.effort}</p>
        </div>
        {llm.provider === "codex" && (
          <CodexAccount usage={account.data} loading={account.isPending} error={account.error as Error | null} refresh={() => account.refetch()} />
        )}
        <div className="space-y-1.5">
          <div className="flex justify-between">
            <span className="font-medium text-fg">This agent session</span>
            <span>{costKnown ? `$${cost.toFixed(4)}` : llm.provider === "codex" ? "ChatGPT plan" : "cost unavailable"}</span>
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

export function AgentPanel({ ready, llm }: { ready: boolean; llm?: LLMView }) {
  const chat = useChat();
  const { query, useCv } = useSearch();
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const connect = chat.connect;

  useEffect(() => connect(), [connect]);
  useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), [chat.items.length, chat.status]);

  const submit = (text = draft) => {
    if (!text.trim() || chat.running) return;
    chat.send(text.trim(), query, useCv);
    setDraft("");
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-border px-3 py-2.5">
        <div className="flex items-center gap-2">
          <Bot size={15} className="text-accent" />
          <span className="font-semibold">Agent</span>
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

      <div className="flex-1 space-y-2 overflow-y-auto p-3 scroll-thin">
        {chat.items.length === 0 ? (
          <Empty title="Orchestrator + specialists">
            <p className="mb-3">
              The orchestrator summarises your profile (remembered for similar searches), sends the search to{" "}
              <b>job_search_expert</b>, and <b>job_matcher</b> keeps only true matches. <b>cv_expert</b> tailors your CV.
            </p>
            {ready ? (
              <div className="space-y-1.5">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="btn-ghost w-full justify-start text-left text-[12px]" onClick={() => submit(s)}>
                    {s}
                  </button>
                ))}
              </div>
            ) : (
              <p className="text-warn">Configure an LLM provider in Settings to start.</p>
            )}
          </Empty>
        ) : (
          chat.items.map((item, i) => {
            switch (item.kind) {
              case "user":
                return (
                  <div key={i} className="ml-8 rounded-lg bg-accent-bg px-3 py-2 text-[13px]">
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
              case "file":
                return (
                  <a key={i} href={item.url} className="btn-primary w-fit py-1 text-[12px]">
                    <Download size={12} /> {item.name}
                  </a>
                );
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

      <div className="border-t border-border p-2.5">
        <div className="flex items-end gap-2">
          <textarea
            className="input min-h-[38px] resize-none"
            rows={2}
            placeholder={ready ? "Ask the agent… (uses your filters)" : "Configure an LLM in Settings"}
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
