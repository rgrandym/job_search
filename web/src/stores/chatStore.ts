import { create } from "zustand";
import type { ChatEvent, ModelUsageEvent, Provider, SearchQuery } from "../lib/types";
import { useSearch } from "./searchStore";

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string }
  | { kind: "activity"; agent: string; depth: number; text: string; ok?: boolean; detail?: string }
  | { kind: "error"; text: string };

export interface SessionModelUsage {
  model: string;
  provider?: Provider;
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
  send: (text: string, filters: SearchQuery, useCv: boolean, chatRole: "quality" | "screening", chatModel: string | null, chatProvider: Provider | null, options: {
    smart: boolean; threshold: number; widen: boolean; profileKey: string | null;
  }) => void;
  cancel: () => void;
  reset: () => void;
  recordUsage: (event: ModelUsageEvent | (Omit<ModelUsageEvent, "depth"> & { depth?: number })) => void;
}

const SESSION_KEY = "jobsearch.session";
const USAGE_KEY = "jobsearch.tabUsage";
const storedUsage = (() => {
  try {
    return JSON.parse(sessionStorage.getItem(USAGE_KEY) ?? "null") as {
      tokens: { input: number; output: number };
      modelUsage: Record<string, SessionModelUsage>;
    } | null;
  } catch {
    return null;
  }
})();

const describeArgs = (args: Record<string, unknown>) => {
  const parts = Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k}=${typeof v === "string" ? v.slice(0, 60) : JSON.stringify(v)}`);
  return parts.join(", ");
};

export const useChat = create<ChatState>()((set, get) => {
  let chatSearchActive = false;
  const push = (item: ChatItem) => set((s) => ({ items: [...s.items, item] }));

  const onEvent = (ev: ChatEvent) => {
    switch (ev.type) {
      case "session":
        localStorage.setItem(SESSION_KEY, ev.session_id);
        set((current) => ({
          sessionId: ev.session_id,
          items: current.items.length ? current.items : ev.history.map((item) => ({ kind: item.role, text: item.text })),
        }));
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
      case "profile_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: `profile updated: ${ev.reason}` });
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
      case "search_progress":
        chatSearchActive = true;
        set({ status: ev.message });
        useSearch.setState({ loading: true, progress: ev.message });
        break;
      case "search_filters_updated":
        useSearch.getState().setQuery(ev.query);
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Search filters updated" });
        break;
      case "search_results":
        chatSearchActive = false;
        useSearch.setState({
          outcome: ev.outcome, loading: false, progress: null,
          summary: ev.outcome.report.summary
            ? { summary: ev.outcome.report.summary, fromMemory: !!ev.outcome.summary_from_memory }
            : null,
        });
        push({ kind: "activity", agent: "assistant", depth: 0, text: `Search complete: ${ev.outcome.report.matches.length} matches` });
        break;
      case "saved_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: `${Math.abs(ev.count)} saved job(s) ${ev.count < 0 ? "removed" : "added"}` });
        break;
      case "tracking_updated":
        useSearch.getState().setTracking(ev.job_id, ev.tracking);
        push({ kind: "activity", agent: "assistant", depth: 0, text: `Tracking updated for ${ev.job_id}` });
        break;
      case "tracker_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Application tracker updated" });
        break;
      case "label_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: `Your call updated for ${ev.job_id}` });
        break;
      case "documents_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Document ready in your library" });
        break;
      case "cv_selected":
        useSearch.setState({ useCv: true, profileKey: null, summary: null });
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Selected CV updated" });
        break;
      case "learning_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Learned preferences updated" });
        break;
      case "companies_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Company sources updated" });
        break;
      case "history_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Search history updated" });
        break;
      case "result_removed":
        useSearch.getState().removeJob(ev.job_id);
        push({ kind: "activity", agent: "assistant", depth: 0, text: "Search result removed" });
        break;
      case "models_updated":
        push({ kind: "activity", agent: "assistant", depth: 0, text: "App models updated" });
        break;
      case "done":
        if (chatSearchActive) {
          chatSearchActive = false;
          useSearch.setState({ loading: false, progress: null });
        }
        set((s) => {
          // Usage events arrive before `done`; max() also recovers safely if a
          // client missed an event during a reconnect.
          const tokens = {
            input: Math.max(s.tokens.input, ev.tokens.input),
            output: Math.max(s.tokens.output, ev.tokens.output),
          };
          sessionStorage.setItem(USAGE_KEY, JSON.stringify({ tokens, modelUsage: s.modelUsage }));
          return { running: false, status: ev.cancelled ? "Stopped" : null, tokens };
        });
        break;
      case "error":
        push({ kind: "error", text: ev.message });
        set({ running: false, status: null });
        if (chatSearchActive) {
          chatSearchActive = false;
          useSearch.setState({ loading: false, progress: null });
        }
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
    tokens: storedUsage?.tokens ?? { input: 0, output: 0 },
    modelUsage: storedUsage?.modelUsage ?? {},
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
        if (chatSearchActive) {
          chatSearchActive = false;
          useSearch.setState({ loading: false, progress: null });
        }
        set({ connected: false, socket: null, running: false });
        setTimeout(() => get().connect(), 2000);
      };
      set({ socket: ws });
    },

    send: (text, filters, useCv, chatRole, chatModel, chatProvider, options) => {
      const ws = get().socket;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      push({ kind: "user", text });
      set({ running: true, status: "assistant is thinking…" });
      ws.send(JSON.stringify({
        type: "user_message", text, filters, use_cv: useCv, chat_role: chatRole,
        chat_model: chatModel,
        chat_provider: chatProvider,
        smart: options.smart, threshold: options.threshold, widen: options.widen,
        profile_key: options.profileKey,
      }));
    },

    cancel: () => get().socket?.send(JSON.stringify({ type: "cancel" })),

    reset: () => {
      get().socket?.send(JSON.stringify({ type: "reset" }));
      set({ items: [], status: null });
    },
    // Tab usage per model, from assistant turns and Search-button runs alike.
    recordUsage: (ev) =>
      set((s) => {
        const key = `${ev.provider ?? "current"}:${ev.model}`;
        const current = s.modelUsage[key];
        const next = {
          tokens: {
            input: s.tokens.input + ev.input_tokens,
            output: s.tokens.output + ev.output_tokens,
          },
          modelUsage: {
            ...s.modelUsage,
            [key]: {
              model: ev.model,
              provider: ev.provider,
              input: (current?.input ?? 0) + ev.input_tokens,
              output: (current?.output ?? 0) + ev.output_tokens,
              estimated: (current?.estimated ?? false) || ev.estimated,
              latestContext: ev.input_tokens + ev.output_tokens,
              agents: Array.from(new Set([...(current?.agents ?? []), ev.agent])),
              purposes: Array.from(new Set([...(current?.purposes ?? []), ev.purpose])),
            },
          },
        };
        sessionStorage.setItem(USAGE_KEY, JSON.stringify(next));
        return next;
      }),
  };
});
