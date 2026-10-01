import { Bot, Download, Loader2, RotateCcw, Send, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "../lib/utils";
import { useChat, type ChatItem } from "../stores/chatStore";
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

export function AgentPanel({ ready }: { ready: boolean }) {
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
