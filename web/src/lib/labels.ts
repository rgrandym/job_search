import type { Provider } from "./types";

/** Display names shared by the activity, connections and limit views. */
export const PROVIDER_LABEL: Record<Provider, string> = {
  anthropic: "Claude API",
  claude_code: "Claude Code",
  codex: "Codex",
  openai: "OpenAI API",
  openrouter: "OpenRouter",
};

export const routeLabel = (r: { provider: Provider; model: string }) => `${PROVIDER_LABEL[r.provider]} ${r.model}`;

/** A token count as "850", "12.3k" or "1.2M". */
export function tokenCount(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export const clockLabel = (epochSeconds: number) =>
  new Date(epochSeconds * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });

// The search pipeline's stages in order, and their short names.
export const STAGES = ["cv", "summary", "plan", "capture", "prefilter", "enrich", "screen", "done"];

export const STAGE_LABEL: Record<string, string> = {
  cv: "CV",
  summary: "Profile",
  plan: "Plan",
  capture: "Sources",
  prefilter: "Pre-filter",
  enrich: "Details",
  screen: "Matching",
  done: "Done",
};
