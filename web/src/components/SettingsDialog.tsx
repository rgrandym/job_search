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

type Role = "quality" | "screening" | "profile";
const ROLES: Role[] = ["profile", "quality", "screening"];
type ModelDraft = Record<"quality" | "screening", string>;
/** The profile model; `provider` "" means the main provider, `model` "" the quality model. */
type ProfileDraft = { provider: Provider | ""; model: string };
type Efforts = Record<Role, Effort>;
type Recommendation = Record<Role, { prefer: string[]; why: string }> & { footnote: string };

const emptyDraft = (p: Provider): ModelDraft => ({ quality: DEFAULT_MODEL[p], screening: DEFAULT_MODEL[p] });
const draftOf = (llm: LLMView): ModelDraft => ({ quality: llm.quality_model, screening: llm.screening_model });
const profileOf = (llm: LLMView): ProfileDraft => ({
  provider: llm.profile_provider && llm.profile_provider !== llm.provider ? llm.profile_provider : "",
  model: llm.profile_model,
});
const effortsOf = (llm: LLMView): Efforts => ({
  quality: llm.quality_effort,
  screening: llm.screening_effort,
  profile: llm.profile_effort,
});

// Preference order per role: the smallest, fastest model that does the work well comes first.
const OPENAI_RECOMMENDATION: Recommendation = {
  profile: {
    prefer: ["gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-sol"],
    why: "at high effort: built once per role family, and every verdict is judged against it.",
  },
  quality: {
    prefer: ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna"],
    why: "for CV and cover letters and second opinions: rare calls where quality matters.",
  },
  screening: {
    prefer: ["gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-6.1-sol"],
    why: "for first-pass screening: hundreds of short calls per search, so a light model saves the most.",
  },
  footnote: "Start both at medium effort. Check a change with the model comparison (see README) before relying on it.",
};

const CLAUDE_RECOMMENDATION: Recommendation = {
  profile: {
    prefer: ["claude-opus-5-5", "claude-sonnet-5-5"],
    why: "at high effort: built once per role family, so the strongest model costs little here.",
  },
  quality: {
    prefer: ["claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5"],
    why: "for CV and cover letters and second opinions, at a fraction of Opus usage.",
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

const ROLE_LABEL: Record<Role, string> = {
  profile: "Profile model",
  quality: "Quality model",
  screening: "Screening model",
};
const ROLE_HINT: Record<Role, string> = {
  profile: "Builds the profile summary and role families every job is judged against. It can use another provider, e.g. Opus through Claude Code while Codex searches",
  quality: "CV reading and tailoring, cover letters, second opinions on matches and near the threshold, the assistant",
  screening: "First-pass job matching (the job_matcher), batch after batch",
};
const EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"];
const DEFAULT_EFFORTS: Efforts = { quality: "medium", screening: "medium", profile: "high" };

const isCliProvider = (p: Provider) => p === "codex" || p === "claude_code";
const hasEffort = (p: Provider) => p === "anthropic" || isCliProvider(p);
const planName = (p: Provider) => (p === "codex" ? "ChatGPT" : "Claude");
const providerLabel = (p: Provider) => PROVIDERS.find((item) => item.value === p)?.label ?? p;

function useModels(provider: Provider, open: boolean) {
  return useQuery({
    queryKey: ["models", provider],
    queryFn: () => api.models(provider),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

function useCliStatus(provider: Provider, enabled: boolean) {
  return useQuery({
    queryKey: ["cli-status", provider],
    queryFn: provider === "codex" ? api.codexStatus : api.claudeCodeStatus,
    enabled: enabled && isCliProvider(provider),
    refetchInterval: 2_000,
  });
}

/** Sign-in for a CLI provider, or the API key field for an API provider. */
function ProviderAccess({
  provider,
  open,
  apiKey,
  onKey,
  keySaved,
  label = "API key",
}: {
  provider: Provider;
  open: boolean;
  apiKey: string;
  onKey: (key: string) => void;
  keySaved: boolean;
  label?: string;
}) {
  const cli = useCliStatus(provider, open);
  const login = useMutation({
    mutationFn: () => (provider === "codex" ? api.loginCodex() : api.loginClaudeCode()),
    onSuccess: () => cli.refetch(),
  });
  const plan = planName(provider);
  if (!isCliProvider(provider)) {
    return (
      <Field label={label} hint={PROVIDERS.find((p) => p.value === provider)?.hint}>
        <input
          className="input"
          type="password"
          autoComplete="off"
          placeholder={keySaved ? "•••••••• saved (leave blank to keep)" : "Paste key"}
          value={apiKey}
          onChange={(e) => onKey(e.target.value)}
        />
      </Field>
    );
  }
  return (
    <div className="rounded-md border border-border bg-surface p-3 text-[12px]">
      {cli.isPending && <span className="text-muted">Checking {plan} sign-in…</span>}
      {cli.data?.logged_in && (
        <span className="text-good">
          {provider === "codex" ? "Connected to ChatGPT." : cli.data.message} Usage comes from your {plan} plan.
        </span>
      )}
      {cli.data && !cli.data.logged_in && (
        <div className="space-y-2">
          <p className="text-muted">{cli.data.message || `Sign in with ${plan}.`}</p>
          {cli.data.installed && (
            <button type="button" className="btn-primary" disabled={login.isPending} onClick={() => login.mutate()}>
              {login.isPending && <Loader2 size={14} className="animate-spin" />} Continue with {plan}
            </button>
          )}
        </div>
      )}
      {(cli.isError || login.isError) && <p className="text-bad">{((cli.error || login.error) as Error).message}</p>}
    </div>
  );
}

function EffortSelect({
  value,
  onChange,
  disabled,
  title,
}: {
  value: Effort;
  onChange: (effort: Effort) => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <Field label="Effort">
      <select
        className="input"
        disabled={disabled}
        title={title}
        value={value}
        onChange={(e) => onChange(e.target.value as Effort)}
      >
        {EFFORTS.map((e) => (
          <option key={e}>{e}</option>
        ))}
      </select>
    </Field>
  );
}

function preferredModel(models: ModelInfo[], choices: string[]): string {
  return choices.find((id) => models.some((model) => model.id === id)) ?? models[0]?.id ?? "";
}

function ModelPicker({
  value,
  onChange,
  models,
  loading,
  unavailable,
  emptyLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  models: ModelInfo[];
  loading: boolean;
  unavailable: boolean;
  /** When set, an empty value is a valid choice shown with this label. */
  emptyLabel?: string;
}) {
  if (unavailable) {
    return (
      <input
        className="input"
        value={value}
        placeholder={emptyLabel}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  const currentMissing = value && !models.some((model) => model.id === value);
  return (
    <select className="input" value={value} disabled={loading} onChange={(event) => onChange(event.target.value)}>
      {loading && <option value="">Loading models…</option>}
      {!loading && (emptyLabel || !value) && <option value="">{emptyLabel ?? "Choose a model"}</option>}
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
  const [profile, setProfile] = useState<ProfileDraft>({ provider: "", model: "" });
  const [key, setKey] = useState("");
  const [profileKey, setProfileKey] = useState("");

  const draft = drafts[provider] ?? emptyDraft(provider);
  const setDraft = (update: Partial<ModelDraft>) =>
    setDrafts((current) => ({ ...current, [provider]: { ...draft, ...update } }));
  const profileProvider: Provider = profile.provider || provider;
  const ownProfileProvider = profileProvider !== provider;

  useEffect(() => {
    if (!open || !llm) return;
    setProvider(llm.provider);
    setDrafts({ [llm.provider]: draftOf(llm) });
    setEfforts(effortsOf(llm));
    setProfile(profileOf(llm));
    setKey("");
    setProfileKey("");
  }, [open, llm]);

  const models = useModels(provider, open);
  const profileModels = useModels(profileProvider, open);
  const cli = useCliStatus(provider, open);
  const profileCli = useCliStatus(profileProvider, open && ownProfileProvider);

  useEffect(() => {
    const available = models.data ?? [];
    if (!available.length) return;
    setDrafts((current) => {
      const selected = current[provider] ?? emptyDraft(provider);
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

  const save = useMutation({
    mutationFn: () =>
      api.updateLLM({
        provider,
        quality_model: draft.quality,
        screening_model: draft.screening,
        quality_effort: efforts.quality,
        screening_effort: efforts.screening,
        profile_model: profile.model,
        profile_effort: efforts.profile,
        profile_provider: ownProfileProvider && profile.model ? profileProvider : null,
        api_key: key || undefined,
        profile_api_key: ownProfileProvider ? profileKey || undefined : undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["state"] });
      onClose();
    },
  });

  const switchProvider = (p: Provider) => {
    setProvider(p);
    setDrafts((current) => (current[p] ? current : { ...current, [p]: p === llm?.provider ? draftOf(llm) : emptyDraft(p) }));
    setEfforts(p === llm?.provider ? effortsOf(llm) : DEFAULT_EFFORTS);
    // A profile model on the main provider belongs to the old provider's model list.
    if (!profile.provider) setProfile({ provider: "", model: p === llm?.provider && !llm.profile_provider ? llm.profile_model : "" });
  };
  const switchProfileProvider = (value: Provider | "") =>
    setProfile({ provider: value === provider ? "" : value, model: "" });

  const keySaved = llm?.provider === provider && llm.key_set;
  const profileKeySaved = llm?.profile_provider === profileProvider && llm.profile_ready;
  const modelsFor = (role: Role) => (role === "profile" ? profileModels : models);
  const recommendationFor = (role: Role) => RECOMMENDATIONS[role === "profile" ? profileProvider : provider];
  const recommendation = RECOMMENDATIONS[provider];
  const profileSignedOut = ownProfileProvider && !!profile.model && isCliProvider(profileProvider) && !profileCli.data?.logged_in;
  const setRoleModel = (role: Role, model: string) =>
    role === "profile" ? setProfile({ ...profile, model }) : setDraft({ [role]: model });
  const roleModel = (role: Role) => (role === "profile" ? profile.model : draft[role]);

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
        <ProviderAccess provider={provider} open={open} apiKey={key} onKey={setKey} keySaved={keySaved} />
        <div className="space-y-2 rounded-md border border-border p-3">
          <Field label="Profile provider" hint={ROLE_HINT.profile}>
            <select
              className="input"
              value={profile.provider}
              onChange={(e) => switchProfileProvider(e.target.value as Provider | "")}
            >
              <option value="">Same as above ({providerLabel(provider)})</option>
              {PROVIDERS.filter((p) => p.value !== provider).map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </Field>
          {ownProfileProvider && (
            <ProviderAccess
              provider={profileProvider}
              open={open}
              apiKey={profileKey}
              onKey={setProfileKey}
              keySaved={profileKeySaved}
              label={`${providerLabel(profileProvider)} API key`}
            />
          )}
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_110px]">
            <Field label={ROLE_LABEL.profile}>
              <ModelPicker
                value={profile.model}
                onChange={(model) => setRoleModel("profile", model)}
                models={profileModels.data ?? []}
                loading={profileModels.isPending}
                unavailable={profileModels.isError}
                emptyLabel={
                  ownProfileProvider ? "Choose a model" : `Same as quality model (${draft.quality || "not set"})`
                }
              />
            </Field>
            {hasEffort(profileProvider) && (
              <EffortSelect
                value={profile.model ? efforts.profile : efforts.quality}
                onChange={(effort) => setEfforts({ ...efforts, profile: effort })}
                disabled={!profile.model}
                title={profile.model ? undefined : "Uses the quality model's effort"}
              />
            )}
          </div>
          {ownProfileProvider && !profile.model && (
            <p className="text-[11px] text-faint">Choose a model, or the profile stays with the quality model.</p>
          )}
        </div>
        {(["quality", "screening"] as const).map((role) => (
          <div key={role} className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_110px]">
            <Field label={ROLE_LABEL[role]} hint={ROLE_HINT[role]}>
              <ModelPicker
                value={draft[role]}
                onChange={(model) => setDraft({ [role]: model })}
                models={models.data ?? []}
                loading={models.isPending}
                unavailable={models.isError}
              />
            </Field>
            {hasEffort(provider) && (
              <EffortSelect value={efforts[role]} onChange={(effort) => setEfforts({ ...efforts, [role]: effort })} />
            )}
          </div>
        ))}
        {(models.isError || profileModels.isError) && (
          <p className="text-[11px] text-faint">Model list unavailable: type a model id.</p>
        )}
        {recommendation && (models.data ?? []).length > 0 && (
          <div className="space-y-2 rounded-md border border-border bg-surface p-3 text-[11px]">
            <p className="font-medium text-fg">Recommended setup</p>
            {ROLES.map((role) => {
              const rec = recommendationFor(role);
              const available = modelsFor(role).data ?? [];
              if (!rec || !available.length) return null;
              const model = preferredModel(available, rec[role].prefer);
              const use = () => {
                setRoleModel(role, model);
                if (role === "profile") setEfforts({ ...efforts, profile: "high" });
              };
              return (
                <div key={role} className="flex items-start justify-between gap-3">
                  <span className="text-muted">
                    <strong className="text-fg">{ROLE_LABEL[role]}:</strong> {model} {rec[role].why}
                  </span>
                  <button type="button" className="text-accent" disabled={roleModel(role) === model} onClick={use}>
                    Use
                  </button>
                </div>
              );
            })}
            <p className="text-faint">{recommendation.footnote}</p>
          </div>
        )}
        <p className="text-[11px] text-faint">
          {[provider, ...(ownProfileProvider ? [profileProvider] : [])]
            .map((p) =>
              isCliProvider(p)
                ? `${providerLabel(p)} runs through its local CLI; the app never reads your ${planName(p)} credentials.`
                : `${providerLabel(p)} keys are stored locally in data/llm_config.json (git-ignored).`,
            )
            .join(" ")}
        </p>
        {save.error && <p className="text-[12px] text-bad">{(save.error as Error).message}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            type="button"
            disabled={
              !draft.quality ||
              !draft.screening ||
              save.isPending ||
              (isCliProvider(provider) && !cli.data?.logged_in) ||
              profileSignedOut
            }
            onClick={() => save.mutate()}
          >
            {save.isPending && <Loader2 size={14} className="animate-spin" />} Save
          </button>
        </div>
      </div>
    </Modal>
  );
}
