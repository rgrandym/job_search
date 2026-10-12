import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRightLeft, Bot, Check, ChevronDown, Loader2, Send, Square, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../lib/api";
import { routeLabel } from "../lib/labels";
import type { LLMView } from "../lib/types";
import { cn } from "../lib/utils";
import { useActivity } from "../stores/activityStore";
import { useChat, type ChatItem } from "../stores/chatStore";
import { useSearch } from "../stores/searchStore";
import { ActivityPanel } from "./ActivityPanel";
import { ConnectionsPanel } from "./ConnectionsPanel";
import { LimitOptions } from "./LimitOptions";
import { Empty } from "./ui";

function Activity({ item }: { item: Extract<ChatItem, { kind: "activity" }> }) {
  return (
    <div className="text-[11px] text-faint" style={{ paddingLeft: item.depth * 12 }} title={item.detail}>
      <span className={cn("font-medium", item.ok === false ? "text-bad" : "text-muted")}>{item.agent}</span>{" "}
      <span className="break-words">{item.text.length > 220 ? `${item.text.slice(0, 220)}…` : item.text}</span>
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
  const { query, useCv, smart, threshold, widen, areaThresholds, profileKey } = useSearch();
  const qc = useQueryClient();
  const running = chat.running;
  // A finished turn may have changed the intent, the CV (and its Word copy), profiles or documents.
  useEffect(() => {
    if (running) return;
    for (const key of ["intent", "profiles", "evidence", "state", "cv-preview", "history", "saved", "tracker", "labels", "learning", "tailored-cvs", "cover-letters", "company-boards", "connections"]) void qc.invalidateQueries({ queryKey: [key] });
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
    chat.send(text.trim(), query, useCv, chatRole, chatModel, chatProvider, { smart, threshold, widen, areaThresholds, profileKey });
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
            <span title="Tokens used by this tab (searches and assistant)">{((chat.tokens.input + chat.tokens.output) / 1000).toFixed(1)}k tok</span>
          )}
        </div>
      </header>

      <ActivityPanel />
      {llm && <ConnectionsPanel />}

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
              case "notice":
                return (
                  <p key={i} className="flex items-start gap-1.5 rounded-md border border-warn/40 px-2 py-1.5 text-[12px] text-muted">
                    <ArrowRightLeft size={12} className="mt-0.5 shrink-0 text-warn" /> {item.text}
                  </p>
                );
              case "limit":
                return item.resolved ? (
                  <p key={i} className="text-[11px] text-faint">{routeLabel(item)}&apos;s plan ran out; you chose what to do next.</p>
                ) : (
                  <LimitOptions
                    key={i}
                    what="this request"
                    configured={item}
                    resetsAt={item.resetsAt}
                    replacement={item.replacement}
                    onSwitch={() => chat.resend(i, true)}
                    onWait={() => {
                      chat.resolve(i);
                      // A minute after the reset, so the plan has really refreshed.
                      useActivity.getState().wait((item.resetsAt + 60) * 1000, `Send your message again on ${item.model}`, () => chat.resend(i, false));
                    }}
                    onStop={() => chat.resolve(i)}
                  />
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
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wide text-faint">Chat model</span>
            <button
              type="button"
              className="flex items-center gap-1 rounded border border-border px-2 py-0.5 text-[11px] text-muted hover:border-accent hover:text-fg disabled:opacity-50"
              title={chat.running ? "Stop the current run first" : "Clear the conversation and start a new one"}
              disabled={chat.running || chat.items.length === 0}
              onClick={chat.reset}
            >
              <Trash2 size={11} />
              Clear chat
            </button>
          </div>
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
