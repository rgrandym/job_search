import { create } from "zustand";
import type { ChatEvent, ModelUsageEvent, SearchQuery } from "../lib/types";

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string }
  | { kind: "activity"; agent: string; depth: number; text: string; ok?: boolean; detail?: string }
  | { kind: "error"; text: string };

export interface SessionModelUsage {
  model: string;
  input: number;
  output: number;
  estimated: boolean;
  latestContext: number;
  agents: string[];
  purposes: string[];
}

interface ChatState {
  sessionId: string | null;
  items: ChatItem[];
  running: boolean;
  status: string | null;
  connected: boolean;
  tokens: { input: number; output: number };
  modelUsage: Record<string, SessionModelUsage>;
  socket: WebSocket | null;
  connect: () => void;
  send: (text: string, filters: SearchQuery, useCv: boolean) => void;
  cancel: () => void;
  reset: () => void;
  recordUsage: (event: ModelUsageEvent | (Omit<ModelUsageEvent, "depth"> & { depth?: number })) => void;
}

const SESSION_KEY = "jobsearch.session";

const describeArgs = (args: Record<string, unknown>) => {
  const parts = Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k}=${typeof v === "string" ? v.slice(0, 60) : JSON.stringify(v)}`);
  return parts.join(", ");
};

export const useChat = create<ChatState>()((set, get) => {
  const push = (item: ChatItem) => set((s) => ({ items: [...s.items, item] }));

  const onEvent = (ev: ChatEvent) => {
    switch (ev.type) {
      case "session":
        localStorage.setItem(SESSION_KEY, ev.session_id);
        set({ sessionId: ev.session_id });
        break;
      case "agent_status":
        set({ status: `${ev.agent} is thinking…` });
        break;
      case "model_usage":
        get().recordUsage(ev);
        break;
      case "agent_message":
        if (ev.depth === 0 && ev.final) push({ kind: "assistant", text: ev.text });
        else push({ kind: "activity", agent: ev.agent, depth: ev.depth, text: ev.text });
        break;
      case "tool_call":
        push({ kind: "activity", agent: ev.agent, depth: ev.depth, text: `${ev.tool}(${describeArgs(ev.args)})` });
        break;
      case "tool_result":
        if (!ev.ok) push({ kind: "activity", agent: ev.agent, depth: ev.depth, text: `${ev.tool} failed`, ok: false, detail: ev.preview });
        break;
      case "cv_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: `search preferences updated: ${ev.reason}` });
        break;
      case "intent_updated":
        push({
          kind: "activity",
          agent: "assistant",
          depth: 0,
          text: "career intent updated (Profiles › Career intent)",
          detail: ev.changes.join("\n"),
        });
        break;
      case "evidence_proposed":
        push({
          kind: "activity",
          agent: "assistant",
          depth: 0,
          text: `${ev.count} CV fact(s) proposed: review them in Profiles › Add evidence`,
        });
        break;
      case "done":
        set((s) => ({
          running: false,
          status: ev.cancelled ? "Stopped" : null,
          // Usage events arrive before `done`; max() also recovers safely if a
          // client missed an event during a reconnect.
          tokens: {
            input: Math.max(s.tokens.input, ev.tokens.input),
            output: Math.max(s.tokens.output, ev.tokens.output),
          },
        }));
        break;
      case "error":
        push({ kind: "error", text: ev.message });
        set({ running: false, status: null });
        break;
      default:
        break;
    }
  };

  return {
    sessionId: localStorage.getItem(SESSION_KEY),
    items: [],
    running: false,
    status: null,
    connected: false,
    tokens: { input: 0, output: 0 },
    modelUsage: {},
    socket: null,

    connect: () => {
      const existing = get().socket;
      if (existing && existing.readyState <= WebSocket.OPEN) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const sid = get().sessionId;
      const ws = new WebSocket(`${proto}://${location.host}/api/ws/chat${sid ? `?session=${sid}` : ""}`);
      ws.onopen = () => set({ connected: true });
      ws.onmessage = (m) => onEvent(JSON.parse(m.data) as ChatEvent);
      ws.onclose = () => {
        set({ connected: false, socket: null, running: false });
        setTimeout(() => get().connect(), 2000);
      };
      set({ socket: ws });
    },

    send: (text, filters, useCv) => {
      const ws = get().socket;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      push({ kind: "user", text });
      set({ running: true, status: "assistant is thinking…" });
      ws.send(JSON.stringify({ type: "user_message", text, filters, use_cv: useCv }));
    },

    cancel: () => get().socket?.send(JSON.stringify({ type: "cancel" })),

    reset: () => {
      get().socket?.send(JSON.stringify({ type: "reset" }));
      set({ items: [], tokens: { input: 0, output: 0 }, modelUsage: {}, status: null });
    },
    // Session token usage per model, from agent turns and from Search-button runs alike.
    recordUsage: (ev) =>
      set((s) => {
        const current = s.modelUsage[ev.model];
        return {
          tokens: {
            input: s.tokens.input + ev.input_tokens,
            output: s.tokens.output + ev.output_tokens,
          },
          modelUsage: {
            ...s.modelUsage,
            [ev.model]: {
              model: ev.model,
              input: (current?.input ?? 0) + ev.input_tokens,
              output: (current?.output ?? 0) + ev.output_tokens,
              estimated: (current?.estimated ?? false) || ev.estimated,
              latestContext: ev.input_tokens + ev.output_tokens,
              agents: Array.from(new Set([...(current?.agents ?? []), ev.agent])),
              purposes: Array.from(new Set([...(current?.purposes ?? []), ev.purpose])),
            },
          },
        };
      }),
  };
});
