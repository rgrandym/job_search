import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Effort, LLMView, ModelInfo, Provider } from "../lib/types";
import { Field, Modal } from "./ui";

const PROVIDERS: { value: Provider; label: string; hint: string }[] = [
  { value: "anthropic", label: "Claude", hint: "Anthropic API key (or ANTHROPIC_API_KEY in .env)" },
  { value: "claude_code", label: "Claude Code (Pro/Max)", hint: "Uses your Claude plan through the local Claude Code CLI" },
  { value: "codex", label: "Codex (ChatGPT)", hint: "Uses your ChatGPT plan through the local Codex CLI" },
  { value: "openai", label: "OpenAI API", hint: "OpenAI API key; billed separately from ChatGPT" },
  { value: "openrouter", label: "OpenRouter", hint: "One key for open-source models (Llama, Qwen, DeepSeek…)" },
];

const DEFAULT_MODEL: Record<Provider, string> = {
  anthropic: "claude-opus-5-5",
  codex: "",
  claude_code: "",
  openai: "",
  openrouter: "",
};

type Role = "quality" | "screening";
type ModelDraft = Record<Role, string>;
type Efforts = Record<Role, Effort>;
type Recommendation = Record<Role, { prefer: string[]; why: string }> & { footnote: string };

// Preference order per role: the smallest, fastest model that does the work well comes first.
const OPENAI_RECOMMENDATION: Recommendation = {
  quality: {
    prefer: ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna"],
    why: "for the profile, CV and cover letters and second opinions: rare calls where quality matters.",
  },
  screening: {
    prefer: ["gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-6.1-sol"],
    why: "for first-pass screening: hundreds of short calls per search, so a light model saves the most.",
  },
  footnote: "Start both at medium effort. Check a change with the model comparison (see README) before relying on it.",
};

const CLAUDE_RECOMMENDATION: Recommendation = {
  quality: {
    prefer: ["claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5"],
    why: "for the profile, CV and cover letters and second opinions, at a fraction of Opus usage.",
  },
  screening: {
    prefer: ["claude-haiku-4-5", "claude-sonnet-5-5"],
    why: "is the smallest and fastest; it screens every shortlisted job.",
  },
  footnote: "Use Opus 5.5 only if a comparison shows the lighter setup misses good matches. Haiku ignores effort.",
};

const RECOMMENDATIONS: Partial<Record<Provider, Recommendation>> = {
  openai: OPENAI_RECOMMENDATION,
  codex: OPENAI_RECOMMENDATION,
  anthropic: CLAUDE_RECOMMENDATION,
  claude_code: CLAUDE_RECOMMENDATION,
};

const ROLE_LABEL: Record<Role, string> = { quality: "Quality model", screening: "Screening model" };
const ROLE_HINT: Record<Role, string> = {
  quality: "Profile summary, CV reading and tailoring, cover letters, second opinions on matches and near the threshold, the assistant",
  screening: "First-pass job matching (the job_matcher), batch after batch",
};
const EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"];
const DEFAULT_EFFORTS: Efforts = { quality: "medium", screening: "medium" };

const isCliProvider = (p: Provider) => p === "codex" || p === "claude_code";

function preferredModel(models: ModelInfo[], choices: string[]): string {
  return choices.find((id) => models.some((model) => model.id === id)) ?? models[0]?.id ?? "";
}

function ModelPicker({
  value,
  onChange,
  models,
  loading,
  unavailable,
}: {
  value: string;
  onChange: (value: string) => void;
  models: ModelInfo[];
  loading: boolean;
  unavailable: boolean;
}) {
  if (unavailable) {
    return <input className="input" value={value} onChange={(event) => onChange(event.target.value)} />;
  }
  const currentMissing = value && !models.some((model) => model.id === value);
  return (
    <select className="input" value={value} disabled={loading} onChange={(event) => onChange(event.target.value)}>
      {loading && <option value="">Loading models…</option>}
      {!loading && !value && <option value="">Choose a model</option>}
      {currentMissing && <option value={value}>{value} (current)</option>}
      {models.map((model) => (
        <option key={model.id} value={model.id}>
          {model.name}{model.note ? ` — ${model.note}` : ""}
        </option>
      ))}
    </select>
  );
}

export function SettingsDialog({ open, onClose, llm }: { open: boolean; onClose: () => void; llm: LLMView | undefined }) {
  const qc = useQueryClient();
  const [provider, setProvider] = useState<Provider>("anthropic");
  const [drafts, setDrafts] = useState<Partial<Record<Provider, ModelDraft>>>({});
  const [efforts, setEfforts] = useState<Efforts>(DEFAULT_EFFORTS);
  const [key, setKey] = useState("");

  const draft = drafts[provider] ?? { quality: DEFAULT_MODEL[provider], screening: DEFAULT_MODEL[provider] };
  const setDraft = (update: Partial<ModelDraft>) =>
    setDrafts((current) => ({ ...current, [provider]: { ...draft, ...update } }));

  useEffect(() => {
    if (!open || !llm) return;
    setProvider(llm.provider);
    setDrafts({
      [llm.provider]: { quality: llm.quality_model, screening: llm.screening_model },
    });
    setEfforts({ quality: llm.quality_effort, screening: llm.screening_effort });
    setKey("");
  }, [open, llm]);

  const models = useQuery({
    queryKey: ["models", provider],
    queryFn: () => api.models(provider),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });

  const cliProvider = isCliProvider(provider);
  const cli = useQuery({
    queryKey: ["cli-status", provider],
    queryFn: provider === "codex" ? api.codexStatus : api.claudeCodeStatus,
    enabled: open && cliProvider,
    refetchInterval: 2_000,
  });

  useEffect(() => {
    const available = models.data ?? [];
    if (!available.length) return;
    setDrafts((current) => {
      const selected = current[provider] ?? { quality: DEFAULT_MODEL[provider], screening: DEFAULT_MODEL[provider] };
      if (selected.quality && selected.screening) return current;
      const rec = RECOMMENDATIONS[provider];
      return {
        ...current,
        [provider]: {
          quality: selected.quality || (rec ? preferredModel(available, rec.quality.prefer) : available[0].id),
          screening: selected.screening || (rec ? preferredModel(available, rec.screening.prefer) : available[0].id),
        },
      };
    });
  }, [models.data, provider]);

  const login = useMutation({
    mutationFn: () => (provider === "codex" ? api.loginCodex() : api.loginClaudeCode()),
    onSuccess: () => cli.refetch(),
  });

  const save = useMutation({
    mutationFn: () =>
      api.updateLLM({
        provider,
        quality_model: draft.quality,
        screening_model: draft.screening,
        quality_effort: efforts.quality,
        screening_effort: efforts.screening,
        api_key: key || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["state"] });
      onClose();
    },
  });

  const switchProvider = (p: Provider) => {
    setProvider(p);
    setDrafts((current) =>
      current[p]
        ? current
        : {
            ...current,
            [p]:
              p === llm?.provider
                ? { quality: llm.quality_model, screening: llm.screening_model }
                : { quality: DEFAULT_MODEL[p], screening: DEFAULT_MODEL[p] },
          },
    );
    setEfforts(p === llm?.provider ? { quality: llm.quality_effort, screening: llm.screening_effort } : DEFAULT_EFFORTS);
  };

  const hint = PROVIDERS.find((p) => p.value === provider)?.hint;
  const keySaved = llm?.provider === provider && llm.key_set;
  const availableModels = models.data ?? [];
  const recommendation = RECOMMENDATIONS[provider];
  const planName = provider === "codex" ? "ChatGPT" : "Claude";

  return (
    <Modal open={open} onClose={onClose} title="LLM settings">
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-1">
          {PROVIDERS.map((p) => (
            <button
              type="button"
              key={p.value}
              onClick={() => switchProvider(p.value)}
              className={`rounded-md border px-2 py-2 text-[12px] ${
                provider === p.value ? "border-accent bg-accent-bg text-fg" : "border-border text-muted"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        {cliProvider ? (
          <div className="rounded-md border border-border bg-surface p-3 text-[12px]">
            {cli.isPending && <span className="text-muted">Checking {planName} sign-in…</span>}
            {cli.data?.logged_in && (
              <span className="text-good">
                {provider === "codex" ? "Connected to ChatGPT." : cli.data.message} Usage comes from your {planName} plan.
              </span>
            )}
            {cli.data && !cli.data.logged_in && (
              <div className="space-y-2">
                <p className="text-muted">{cli.data.message || `Sign in with ${planName}.`}</p>
                {cli.data.installed && (
                  <button type="button" className="btn-primary" disabled={login.isPending} onClick={() => login.mutate()}>
                    {login.isPending && <Loader2 size={14} className="animate-spin" />} Continue with {planName}
                  </button>
                )}
              </div>
            )}
            {(cli.isError || login.isError) && (
              <p className="text-bad">
                {((cli.error || login.error) as Error).message}
              </p>
            )}
          </div>
        ) : (
          <Field label="API key" hint={hint}>
            <input
              className="input"
              type="password"
              autoComplete="off"
              placeholder={keySaved ? "•••••••• saved (leave blank to keep)" : "Paste key"}
              value={key}
              onChange={(e) => setKey(e.target.value)}
            />
          </Field>
        )}
        {(["quality", "screening"] as const).map((role) => (
          <div key={role} className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_110px]">
            <Field label={ROLE_LABEL[role]} hint={ROLE_HINT[role]}>
              <ModelPicker
                value={draft[role]}
                onChange={(model) => setDraft({ [role]: model })}
                models={availableModels}
                loading={models.isPending}
                unavailable={models.isError}
              />
            </Field>
            {(provider === "anthropic" || cliProvider) && (
              <Field label="Effort">
                <select
                  className="input"
                  value={efforts[role]}
                  onChange={(e) => setEfforts({ ...efforts, [role]: e.target.value as Effort })}
                >
                  {EFFORTS.map((e) => (
                    <option key={e}>{e}</option>
                  ))}
                </select>
              </Field>
            )}
          </div>
        ))}
        {models.isError && <p className="text-[11px] text-faint">Model list unavailable: type a model id.</p>}
        {recommendation && availableModels.length > 0 && (
          <div className="space-y-2 rounded-md border border-border bg-surface p-3 text-[11px]">
            <p className="font-medium text-fg">Recommended setup</p>
            {(["quality", "screening"] as const).map((role) => {
              const model = preferredModel(availableModels, recommendation[role].prefer);
              return (
                <div key={role} className="flex items-start justify-between gap-3">
                  <span className="text-muted">
                    <strong className="text-fg">{ROLE_LABEL[role]}:</strong> {model} {recommendation[role].why}
                  </span>
                  <button type="button" className="text-accent" onClick={() => setDraft({ [role]: model })}>
                    Use
                  </button>
                </div>
              );
            })}
            <p className="text-faint">{recommendation.footnote}</p>
          </div>
        )}
        <p className="text-[11px] text-faint">
          {cliProvider
            ? `The app delegates to the local ${provider === "codex" ? "Codex" : "Claude Code"} CLI and never reads your ${planName} credentials.`
            : "Keys are stored locally in data/llm_config.json (git-ignored)."}
        </p>
        {save.error && <p className="text-[12px] text-bad">{(save.error as Error).message}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            type="button"
            disabled={!draft.quality || !draft.screening || save.isPending || (cliProvider && !cli.data?.logged_in)}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Loader2 size={14} className="animate-spin" />} Save
          </button>
        </div>
      </div>
    </Modal>
  );
}
