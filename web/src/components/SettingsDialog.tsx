import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2, LogIn, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Effort, LLMView, ModelInfo, Provider, SignInStart } from "../lib/types";
import { ClaudeAccounts } from "./ClaudeAccounts";
import { SignInHelp } from "./SignInHelp";
import { Field, Modal } from "./ui";

// In the order Settings lists them: the plans first (signed in through their CLIs), then keys.
const PROVIDERS: { value: Provider; label: string; hint: string }[] = [
  { value: "claude_code", label: "Claude Code (Pro/Max)", hint: "Your Claude plan through the local Claude Code CLI; several accounts are used in turn" },
  { value: "codex", label: "Codex (ChatGPT)", hint: "Your ChatGPT plan through the local Codex CLI" },
  { value: "anthropic", label: "Claude API", hint: "Anthropic API key (or ANTHROPIC_API_KEY in .env), billed per token" },
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

type Role = "quality" | "screening" | "profile" | "cv" | "letter";
/** Tasks that may have their own model and provider (blank: the quality model). */
type OwnRole = "profile" | "cv" | "letter";
const OWN_ROLES: OwnRole[] = ["profile", "cv", "letter"];
const ROLES: Role[] = ["profile", "cv", "letter", "quality", "screening"];
type ModelDraft = Record<"quality" | "screening", string>;
/** A role's own model; `provider` "" means the main provider, `model` "" the quality model. */
type ProfileDraft = { provider: Provider | ""; model: string };
type Efforts = Record<Role, Effort>;
type Recommendation = Record<Role, { prefer: string[]; why: string }> & { footnote: string };

const emptyDraft = (p: Provider): ModelDraft => ({ quality: DEFAULT_MODEL[p], screening: DEFAULT_MODEL[p] });
const draftOf = (llm: LLMView): ModelDraft => ({ quality: llm.quality_model, screening: llm.screening_model });
const OWN_FIELDS = {
  profile: { model: "profile_model", effort: "profile_effort", provider: "profile_provider", ready: "profile_ready" },
  cv: { model: "cv_model", effort: "cv_effort", provider: "cv_provider", ready: "cv_ready" },
  letter: { model: "letter_model", effort: "letter_effort", provider: "letter_provider", ready: "letter_ready" },
} as const;
const ownOf = (llm: LLMView, role: OwnRole): ProfileDraft => {
  const provider = llm[OWN_FIELDS[role].provider];
  return { provider: provider && provider !== llm.provider ? provider : "", model: llm[OWN_FIELDS[role].model] };
};
const ownsOf = (llm: LLMView): Record<OwnRole, ProfileDraft> => ({
  profile: ownOf(llm, "profile"),
  cv: ownOf(llm, "cv"),
  letter: ownOf(llm, "letter"),
});
const NO_OWN: Record<OwnRole, ProfileDraft> = {
  profile: { provider: "", model: "" },
  cv: { provider: "", model: "" },
  letter: { provider: "", model: "" },
};
const effortsOf = (llm: LLMView): Efforts => ({
  quality: llm.quality_effort,
  screening: llm.screening_effort,
  profile: llm.profile_effort,
  cv: llm.cv_effort,
  letter: llm.letter_effort,
});

// Preference order per role: the smallest, fastest model that does the work well comes first.
const OPENAI_RECOMMENDATION: Recommendation = {
  profile: {
    prefer: ["gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-sol"],
    why: "at high effort: built once per role family, and every verdict is judged against it.",
  },
  cv: {
    prefer: ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"],
    why: "at high effort: tailored CVs are few, and their wording matters.",
  },
  letter: {
    prefer: ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"],
    why: "at high effort: a letter is one short, careful piece of writing.",
  },
  quality: {
    prefer: ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna"],
    why: "for CV reading and second opinions: rare calls where quality matters.",
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
  cv: {
    prefer: ["claude-opus-5-5", "claude-sonnet-5-5"],
    why: "at high effort: tailored CVs are few, and their wording matters.",
  },
  letter: {
    prefer: ["claude-opus-5-5", "claude-sonnet-5-5"],
    why: "at high effort: a letter is one short, careful piece of writing.",
  },
  quality: {
    prefer: ["claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5"],
    why: "for CV reading and second opinions, at a fraction of Opus usage.",
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
  cv: "CV writing model",
  letter: "Cover letter model",
  quality: "Quality model",
  screening: "Screening model",
};
const ROLE_HINT: Record<Role, string> = {
  profile: "Builds or updates profiles when you ask. Searches reuse saved profiles, and use the search quality model if a new summary is needed",
  cv: "Writes tailored CVs. Independent of search and matching: it can use another provider, e.g. Opus through Claude Code while Codex searches",
  letter: "Writes cover letters. Left blank, letters use the CV writing model",
  quality: "CV reading, second opinions on matches and near the threshold, the assistant; also any writing task without its own model",
  screening: "First-pass job matching (the job_matcher), batch after batch",
};
const EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"];
const DEFAULT_EFFORTS: Efforts = { quality: "medium", screening: "medium", profile: "high", cv: "high", letter: "high" };

const isCliProvider = (p: Provider) => p === "codex" || p === "claude_code";
const hasEffort = (p: Provider) => p === "anthropic" || isCliProvider(p);
const planName = (p: Provider) => (p === "codex" ? "ChatGPT" : "Claude");
const providerLabel = (p: Provider) => PROVIDERS.find((item) => item.value === p)?.label ?? p;
type Readiness = Partial<Record<Provider, { ready: boolean; key_saved: boolean }>>;

function useModels(provider: Provider, open: boolean) {
  return useQuery({
    queryKey: ["models", provider],
    queryFn: () => api.models(provider),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

function useProviders(open: boolean) {
  return useQuery({ queryKey: ["llm-providers"], queryFn: api.llmProviders, enabled: open, refetchInterval: 3_000 });
}

/** Provider names in a picker: the ones not connected yet say so. */
function ProviderOptions({ ready, exclude }: { ready: Readiness; exclude?: Provider }) {
  return (
    <>
      {PROVIDERS.filter((p) => p.value !== exclude).map((p) => (
        <option key={p.value} value={p.value}>
          {p.label}
          {ready[p.value]?.ready === false ? " (not connected)" : ""}
        </option>
      ))}
    </>
  );
}

function Dot({ ok }: { ok: boolean | undefined }) {
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${ok ? "bg-good" : ok === false ? "bg-bad" : "bg-border"}`} />;
}

/** Codex: one ChatGPT sign-in through the Codex CLI. */
function CodexCard({ open, ready }: { open: boolean; ready: boolean | undefined }) {
  const status = useQuery({ queryKey: ["cli-status", "codex"], queryFn: api.codexStatus, enabled: open, refetchInterval: 3_000 });
  const [start, setStart] = useState<SignInStart | null>(null);
  const login = useMutation({ mutationFn: api.loginCodex, onSuccess: setStart });
  const signedIn = !!status.data?.logged_in;
  useEffect(() => {
    if (signedIn) setStart(null); // approved: the prompt has done its job
  }, [signedIn]);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        {status.isPending && <span className="text-muted">Checking the ChatGPT sign-in…</span>}
        {status.data && <span className={signedIn ? "text-good" : "text-muted"}>{signedIn ? "Signed in with ChatGPT." : status.data.message || "Not signed in."}</span>}
        {status.isError && <span className="text-bad">{(status.error as Error).message}</span>}
        {(status.data?.installed ?? true) && (
          <button type="button" className="btn-ghost ml-auto px-2 py-1 text-[12px]" disabled={login.isPending} onClick={() => login.mutate()}>
            {login.isPending ? <Loader2 size={12} className="animate-spin" /> : <LogIn size={12} />} {signedIn ? "Sign in again" : "Sign in"}
          </button>
        )}
      </div>
      {start && !signedIn && <SignInHelp start={start} plan="ChatGPT" />}
      {login.isError && <p className="text-[11px] text-bad">{(login.error as Error).message}</p>}
      {ready === false && signedIn && <p className="text-[11px] text-faint">Checking the new sign-in…</p>}
    </div>
  );
}

/** An API provider: its key, saved locally, with save and remove. */
function KeyCard({ provider, keySaved, ready }: { provider: Provider; keySaved: boolean; ready: boolean | undefined }) {
  const qc = useQueryClient();
  const [key, setKey] = useState("");
  const [note, setNote] = useState("");
  const done = (text: string) => {
    setKey("");
    setNote(text);
    void qc.invalidateQueries({ queryKey: ["llm-providers"] });
    void qc.invalidateQueries({ queryKey: ["models", provider] });
    void qc.invalidateQueries({ queryKey: ["state"] });
  };
  const save = useMutation({ mutationFn: () => api.saveApiKey(provider, key.trim()), onSuccess: () => done("Key saved.") });
  const remove = useMutation({ mutationFn: () => api.removeApiKey(provider), onSuccess: () => done("Key removed.") });
  const status = keySaved ? "Key saved in the app." : ready ? "Using the key from .env or your sign-in." : "No key yet.";
  return (
    <div className="space-y-1.5">
      <p className={`text-[12px] ${ready ? "text-good" : "text-muted"}`}>{status}</p>
      <div className="flex gap-1">
        <input
          className="input"
          type="password"
          autoComplete="off"
          placeholder={keySaved ? "Paste a new key to replace it" : "Paste key"}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
        <button type="button" className="btn-primary" disabled={!key.trim() || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? <Loader2 size={13} className="animate-spin" /> : <KeyRound size={13} />} Save
        </button>
        <button type="button" className="btn-ghost text-bad" disabled={!keySaved || remove.isPending} onClick={() => remove.mutate()} title="Remove the key saved in the app">
          <Trash2 size={13} />
        </button>
      </div>
      {note && <p className="text-[11px] text-good">{note}</p>}
      {(save.error || remove.error) && <p className="text-[11px] text-bad">{((save.error || remove.error) as Error).message}</p>}
    </div>
  );
}

/** Top of Settings: every provider, connected or not, and its accounts or key. */
function ProvidersSection({ open, ready }: { open: boolean; ready: Readiness }) {
  return (
    <section className="space-y-2">
      <div>
        <p className="label">Providers and accounts</p>
        <p className="text-[11px] text-faint">
          Connect the providers you want to use. When a plan account runs out or its session expires, work moves to the next Claude account, then
          to the other plan.
        </p>
      </div>
      {PROVIDERS.map((p) => (
        <details key={p.value} className="rounded-md border border-border p-2" open={p.value === "claude_code" || undefined}>
          <summary className="flex cursor-pointer list-none items-center gap-2 text-[12px]">
            <Dot ok={ready[p.value]?.ready} />
            <span className="font-medium text-fg">{p.label}</span>
            <span className="ml-auto truncate text-[11px] text-faint">{ready[p.value]?.ready ? "connected" : "not connected"}</span>
          </summary>
          <div className="mt-2 space-y-1">
            <p className="text-[11px] text-faint">{p.hint}</p>
            {p.value === "claude_code" && <ClaudeAccounts open={open} />}
            {p.value === "codex" && <CodexCard open={open} ready={ready.codex?.ready} />}
            {!isCliProvider(p.value) && <KeyCard provider={p.value} keySaved={!!ready[p.value]?.key_saved} ready={ready[p.value]?.ready} />}
          </div>
        </details>
      ))}
    </section>
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

/** A task with its own model, optionally on another provider (profile, CV, letter). */
function OwnModelSection({
  role,
  provider,
  own,
  onOwn,
  ready,
  effort,
  qualityEffort,
  onEffort,
  fallback,
  models,
}: {
  role: OwnRole;
  provider: Provider;
  own: ProfileDraft;
  onOwn: (own: ProfileDraft) => void;
  ready: Readiness;
  effort: Effort;
  qualityEffort: Effort;
  onEffort: (effort: Effort) => void;
  /** What a blank model falls back to, e.g. "quality model (sol)". */
  fallback: string;
  models: ReturnType<typeof useModels>;
}) {
  const ownProvider: Provider = own.provider || provider;
  const separate = ownProvider !== provider;
  const name = { profile: "Profile", cv: "CV writing", letter: "Cover letters" }[role];
  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <div>
        <p className="text-[12px] font-medium text-fg">{name}</p>
        <p className="text-[11px] text-faint">{ROLE_HINT[role]}</p>
      </div>
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_110px]">
        <Field label="Provider">
          <select
            className="input"
            value={own.provider}
            onChange={(e) => {
              const value = e.target.value as Provider | "";
              onOwn({ provider: value === provider ? "" : value, model: "" });
            }}
          >
            <option value="">Same as search ({providerLabel(provider)})</option>
            <ProviderOptions ready={ready} exclude={provider} />
          </select>
        </Field>
        <Field label="Model">
          <ModelPicker
            value={own.model}
            onChange={(model) => onOwn({ ...own, model })}
            models={models.data ?? []}
            loading={models.isPending}
            unavailable={models.isError}
            emptyLabel={separate ? "Choose a model" : `Same as ${fallback}`}
          />
        </Field>
        {hasEffort(ownProvider) && (
          <EffortSelect
            value={own.model ? effort : qualityEffort}
            onChange={onEffort}
            disabled={!own.model}
            title={own.model ? undefined : `Uses the effort of the ${fallback.split(" (")[0]}`}
          />
        )}
      </div>
      {separate && ready[ownProvider]?.ready === false && (
        <p className="text-[11px] text-warn">{providerLabel(ownProvider)} is not connected yet: connect it above.</p>
      )}
      {separate && !own.model && <p className="text-[11px] text-faint">Choose a model, or this stays with the {fallback.split(" (")[0]}.</p>}
    </div>
  );
}

export function SettingsDialog({ open, onClose, llm }: { open: boolean; onClose: () => void; llm: LLMView | undefined }) {
  const qc = useQueryClient();
  const [provider, setProvider] = useState<Provider>("anthropic");
  const [drafts, setDrafts] = useState<Partial<Record<Provider, ModelDraft>>>({});
  const [efforts, setEfforts] = useState<Efforts>(DEFAULT_EFFORTS);
  const [owns, setOwns] = useState<Record<OwnRole, ProfileDraft>>(NO_OWN);

  const draft = drafts[provider] ?? emptyDraft(provider);
  const setDraft = (update: Partial<ModelDraft>) =>
    setDrafts((current) => ({ ...current, [provider]: { ...draft, ...update } }));
  const ownProvider = (role: OwnRole): Provider => owns[role].provider || provider;
  const separate = (role: OwnRole) => ownProvider(role) !== provider;
  const setOwn = (role: OwnRole, own: ProfileDraft) => setOwns((current) => ({ ...current, [role]: own }));

  useEffect(() => {
    if (!open || !llm) return;
    setProvider(llm.provider);
    setDrafts({ [llm.provider]: draftOf(llm) });
    setEfforts(effortsOf(llm));
    setOwns(ownsOf(llm));
  }, [open, llm]);

  const providers = useProviders(open);
  const ready: Readiness = providers.data ?? {};
  const models = useModels(provider, open);
  const ownModels: Record<OwnRole, ReturnType<typeof useModels>> = {
    profile: useModels(ownProvider("profile"), open),
    cv: useModels(ownProvider("cv"), open),
    letter: useModels(ownProvider("letter"), open),
  };

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
        profile_model: owns.profile.model,
        profile_effort: efforts.profile,
        profile_provider: separate("profile") && owns.profile.model ? ownProvider("profile") : null,
        cv_model: owns.cv.model,
        cv_effort: efforts.cv,
        cv_provider: separate("cv") && owns.cv.model ? ownProvider("cv") : null,
        letter_model: owns.letter.model,
        letter_effort: efforts.letter,
        letter_provider: separate("letter") && owns.letter.model ? ownProvider("letter") : null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["state"] });
      qc.invalidateQueries({ queryKey: ["connections"] });
      onClose();
    },
  });

  const switchProvider = (p: Provider) => {
    setProvider(p);
    setDrafts((current) => (current[p] ? current : { ...current, [p]: p === llm?.provider ? draftOf(llm) : emptyDraft(p) }));
    setEfforts(p === llm?.provider ? effortsOf(llm) : DEFAULT_EFFORTS);
    // A task's model on the main provider belongs to the old provider's model list.
    setOwns((current) => {
      const next = { ...current };
      for (const role of OWN_ROLES) {
        if (current[role].provider) continue;
        const kept = p === llm?.provider && !llm[OWN_FIELDS[role].provider];
        next[role] = { provider: "", model: kept && llm ? llm[OWN_FIELDS[role].model] : "" };
      }
      return next;
    });
  };

  const isOwn = (role: Role): role is OwnRole => (OWN_ROLES as Role[]).includes(role);
  const modelsFor = (role: Role) => (isOwn(role) ? ownModels[role] : models);
  const recommendationFor = (role: Role) => RECOMMENDATIONS[isOwn(role) ? ownProvider(role) : provider];
  const recommendation = RECOMMENDATIONS[provider];
  const setRoleModel = (role: Role, model: string) =>
    isOwn(role) ? setOwn(role, { ...owns[role], model }) : setDraft({ [role]: model });
  const roleModel = (role: Role) => (isOwn(role) ? owns[role].model : draft[role]);
  const fallbackFor = (role: OwnRole) =>
    role === "letter" && owns.cv.model
      ? `CV writing model (${owns.cv.model})`
      : `quality model (${draft.quality || "not set"})`;
  const searchReady = ready[provider]?.ready;

  return (
    <Modal open={open} onClose={onClose} title="LLM settings" wide>
      <div className="space-y-5">
        <ProvidersSection open={open} ready={ready} />
        {providers.isError && <p className="text-[11px] text-bad">Could not check the providers: {(providers.error as Error).message}</p>}

        <section className="space-y-3">
          <div>
            <p className="label">Models for each task</p>
            <p className="text-[11px] text-faint">Pick a provider and a model per task. Plans hand over to each other when they run out (Models &amp; usage).</p>
          </div>
          <div className="space-y-2 rounded-md border border-border p-3">
            <p className="text-[12px] font-medium text-fg">Search and matching</p>
            <Field label="Provider" hint="Used by the quality and screening models, and by any task left on “Same as search”">
              <select className="input" value={provider} onChange={(e) => switchProvider(e.target.value as Provider)}>
                <ProviderOptions ready={ready} />
              </select>
            </Field>
            {searchReady === false && <p className="text-[11px] text-warn">{providerLabel(provider)} is not connected yet: connect it above.</p>}
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
          </div>
          {OWN_ROLES.map((role) => (
            <OwnModelSection
              key={role}
              role={role}
              provider={provider}
              own={owns[role]}
              onOwn={(own) => setOwn(role, own)}
              ready={ready}
              effort={efforts[role]}
              qualityEffort={efforts.quality}
              onEffort={(effort) => setEfforts({ ...efforts, [role]: effort })}
              fallback={fallbackFor(role)}
              models={ownModels[role]}
            />
          ))}
          {(models.isError || OWN_ROLES.some((role) => ownModels[role].isError)) && (
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
                  if (isOwn(role)) setEfforts({ ...efforts, [role]: "high" });
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
        </section>
        <p className="text-[11px] text-faint">
          Claude Code and Codex run through their local CLIs; the app never reads your {planName("claude_code")} or {planName("codex")} credentials.
          API keys are stored locally in data/llm_config.json (git-ignored).
        </p>
        {save.error && <p className="text-[12px] text-bad">{(save.error as Error).message}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            type="button"
            title={searchReady === false ? `Connect ${providerLabel(provider)} above first` : undefined}
            disabled={!draft.quality || !draft.screening || save.isPending || searchReady === false}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Loader2 size={14} className="animate-spin" />} Save models
          </button>
        </div>
      </div>
    </Modal>
  );
}
