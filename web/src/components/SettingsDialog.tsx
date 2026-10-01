import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Effort, LLMView, ModelInfo, Provider } from "../lib/types";
import { Field, Modal } from "./ui";

const PROVIDERS: { value: Provider; label: string; hint: string }[] = [
  { value: "anthropic", label: "Claude", hint: "Anthropic API key (or ANTHROPIC_API_KEY in .env)" },
  { value: "codex", label: "Codex (ChatGPT)", hint: "Uses your ChatGPT plan through the local Codex CLI" },
  { value: "openai", label: "OpenAI API", hint: "OpenAI API key; billed separately from ChatGPT" },
  { value: "openrouter", label: "OpenRouter", hint: "One key for open-source models (Llama, Qwen, DeepSeek…)" },
];

const DEFAULT_MODEL: Record<Provider, string> = {
  anthropic: "claude-opus-5-5",
  codex: "",
  openai: "",
  openrouter: "",
};

type ModelDraft = { orchestrator: string; worker: string };

const OPENAI_PREFERENCES = {
  orchestrator: ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna"],
  worker: ["gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-6.1-sol"],
  tailoring: ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-astra"],
};

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
  const [effort, setEffort] = useState<Effort>("high");
  const [key, setKey] = useState("");

  const draft = drafts[provider] ?? { orchestrator: DEFAULT_MODEL[provider], worker: DEFAULT_MODEL[provider] };
  const setDraft = (update: Partial<ModelDraft>) =>
    setDrafts((current) => ({ ...current, [provider]: { ...draft, ...update } }));

  useEffect(() => {
    if (!open || !llm) return;
    setProvider(llm.provider);
    setDrafts({
      [llm.provider]: {
        orchestrator: llm.orchestrator_model,
        worker: llm.worker_model,
      },
    });
    setEffort(llm.effort);
    setKey("");
  }, [open, llm]);

  const models = useQuery({
    queryKey: ["models", provider],
    queryFn: () => api.models(provider),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });

  const codex = useQuery({
    queryKey: ["codex-status"],
    queryFn: api.codexStatus,
    enabled: open && provider === "codex",
    refetchInterval: 2_000,
  });

  useEffect(() => {
    const available = models.data ?? [];
    if (!available.length) return;
    setDrafts((current) => {
      const selected = current[provider] ?? {
        orchestrator: DEFAULT_MODEL[provider],
        worker: DEFAULT_MODEL[provider],
      };
      if (selected.orchestrator && selected.worker) return current;
      const isOpenAI = provider === "codex" || provider === "openai";
      return {
        ...current,
        [provider]: {
          orchestrator:
            selected.orchestrator ||
            (isOpenAI ? preferredModel(available, OPENAI_PREFERENCES.orchestrator) : available[0].id),
          worker:
            selected.worker || (isOpenAI ? preferredModel(available, OPENAI_PREFERENCES.worker) : available[0].id),
        },
      };
    });
  }, [models.data, provider]);

  const login = useMutation({
    mutationFn: api.loginCodex,
    onSuccess: () => codex.refetch(),
  });

  const save = useMutation({
    mutationFn: () =>
      api.updateLLM({
        provider,
        orchestrator_model: draft.orchestrator,
        worker_model: draft.worker,
        effort,
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
                ? { orchestrator: llm.orchestrator_model, worker: llm.worker_model }
                : { orchestrator: DEFAULT_MODEL[p], worker: DEFAULT_MODEL[p] },
          },
    );
    setEffort(p === llm?.provider ? llm.effort : p === "codex" ? "medium" : effort);
  };

  const hint = PROVIDERS.find((p) => p.value === provider)?.hint;
  const keySaved = llm?.provider === provider && llm.key_set;
  const availableModels = models.data ?? [];
  const showOpenAIRecommendations = provider === "codex" || provider === "openai";
  const orchestratorRecommendation = preferredModel(availableModels, OPENAI_PREFERENCES.orchestrator);
  const workerRecommendation = preferredModel(availableModels, OPENAI_PREFERENCES.worker);
  const tailoringRecommendation = preferredModel(availableModels, OPENAI_PREFERENCES.tailoring);

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
        {provider === "codex" ? (
          <div className="rounded-md border border-border bg-surface p-3 text-[12px]">
            {codex.isPending && <span className="text-muted">Checking Codex…</span>}
            {codex.data?.logged_in && (
              <span className="text-good">Connected to ChatGPT. Usage comes from your ChatGPT plan.</span>
            )}
            {codex.data && !codex.data.logged_in && (
              <div className="space-y-2">
                <p className="text-muted">{codex.data.message || "Sign in to Codex with ChatGPT."}</p>
                {codex.data.installed && (
                  <button type="button" className="btn-primary" disabled={login.isPending} onClick={() => login.mutate()}>
                    {login.isPending && <Loader2 size={14} className="animate-spin" />} Continue with ChatGPT
                  </button>
                )}
              </div>
            )}
            {(codex.isError || login.isError) && (
              <p className="text-bad">
                {((codex.error || login.error) as Error).message}
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
        <Field label="Orchestrator model" hint="Plans, summarises your profile, delegates">
          <ModelPicker
            value={draft.orchestrator}
            onChange={(orchestrator) => setDraft({ orchestrator })}
            models={availableModels}
            loading={models.isPending}
            unavailable={models.isError}
          />
        </Field>
        <Field label="Subagent model" hint="job_search_expert, job_matcher (screening), cv_expert">
          <ModelPicker
            value={draft.worker}
            onChange={(worker) => setDraft({ worker })}
            models={availableModels}
            loading={models.isPending}
            unavailable={models.isError}
          />
        </Field>
        {models.isError && <p className="text-[11px] text-faint">Model list unavailable: type a model id.</p>}
        {showOpenAIRecommendations && availableModels.length > 0 && (
          <div className="space-y-2 rounded-md border border-border bg-surface p-3 text-[11px]">
            <p className="font-medium text-fg">Recommended setup</p>
            <div className="flex items-start justify-between gap-3">
              <span className="text-muted">
                <strong className="text-fg">Profile summary + orchestration:</strong> {orchestratorRecommendation} balances quality and usage.
              </span>
              <button type="button" className="text-accent" onClick={() => setDraft({ orchestrator: orchestratorRecommendation })}>
                Use
              </button>
            </div>
            <div className="flex items-start justify-between gap-3">
              <span className="text-muted">
                <strong className="text-fg">Job search + matching:</strong> {workerRecommendation} is faster for frequent, scoped screening.
              </span>
              <button type="button" className="text-accent" onClick={() => setDraft({ worker: workerRecommendation })}>
                Use
              </button>
            </div>
            <div className="flex items-start justify-between gap-3">
              <span className="text-muted">
                <strong className="text-fg">CV tailoring:</strong> {tailoringRecommendation} is the stronger balanced choice. The subagent model is shared, so select this before intensive tailoring.
              </span>
              <button type="button" className="text-accent" onClick={() => setDraft({ worker: tailoringRecommendation })}>
                Use
              </button>
            </div>
            <p className="text-faint">Use GPT-6 Astra only when you want maximum quality for unusually difficult or ambiguous work.</p>
          </div>
        )}
        {(provider === "anthropic" || provider === "codex") && (
          <Field label="Effort">
            <select className="input" value={effort} onChange={(e) => setEffort(e.target.value as Effort)}>
              {["low", "medium", "high", "xhigh", "max"].map((e) => (
                <option key={e}>{e}</option>
              ))}
            </select>
          </Field>
        )}
        <p className="text-[11px] text-faint">
          {provider === "codex"
            ? "The app delegates to the local Codex CLI and never reads your ChatGPT credentials."
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
            disabled={!draft.orchestrator || !draft.worker || save.isPending || (provider === "codex" && !codex.data?.logged_in)}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Loader2 size={14} className="animate-spin" />} Save
          </button>
        </div>
      </div>
    </Modal>
  );
}
