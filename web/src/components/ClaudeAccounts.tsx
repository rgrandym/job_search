import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Loader2, LogIn, LogOut, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { clockLabel } from "../lib/labels";
import type { ClaudeAccount, ClaudeAccountsView, FoundFolder, PlanWindows, SignInStart } from "../lib/types";
import { cn } from "../lib/utils";
import { SignInHelp } from "./SignInHelp";

// Several Claude plan accounts for Claude Code (each its own Claude Code folder). Calls use the
// first account whose limit is not spent and move to the next one when it is; only when every
// account is spent does work go to the other plan (Codex). Signing in here keeps a year-long
// sign-in for the account, so it stays signed in until signed out. Backend: `core/llm/claude_accounts`.

/** "5h 40% · 7d 12%" for an account's plan windows. */
export function windowsLine(w: PlanWindows | null) {
  if (!w || !w.windows.length) return "";
  return w.windows
    .map((win) => `${win.name} ${win.status === "rejected" ? "spent" : win.used_percent == null ? "ok" : `${Math.round(win.used_percent)}%`}`)
    .join(" · ");
}

const planLabel = (subscription: string | null) => (subscription ? `Claude ${subscription[0].toUpperCase()}${subscription.slice(1)}` : "Claude");
const folderLabel = (a: ClaudeAccount) => a.config_dir ?? "default sign-in (~/.claude)";

/** Who an account is: its email, or why it has none yet. */
function Identity({ a }: { a: ClaudeAccount }) {
  if (!a.signed_in) return <span className="text-bad">{a.lasting ? a.message : "Not signed in: press Sign in."}</span>;
  return (
    <span className="text-muted">
      Signed in as <strong className="text-fg">{a.email ?? "an unknown account"}</strong> · {planLabel(a.subscription)}
    </span>
  );
}

type Pending = { account: ClaudeAccount; start: SignInStart };

function AccountRow({
  a,
  first,
  pending,
  onSignIn,
  onUp,
  onSignOut,
  onRemove,
  busy,
}: {
  a: ClaudeAccount;
  first: boolean;
  pending: Pending | null;
  onSignIn: () => void;
  onUp: () => void;
  onSignOut: () => void;
  onRemove: () => void;
  busy: boolean;
}) {
  return (
    <div className={cn("space-y-1 rounded-md border p-2", a.active ? "border-accent" : "border-border")}>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", !a.signed_in ? "bg-bad" : a.exhausted_until ? "bg-warn" : "bg-good")} />
        <span className="min-w-0 truncate font-medium text-fg">{a.label}</span>
        {a.active && <span className="chip text-accent">in use now</span>}
        {a.exhausted_until && <span className="chip text-warn">limit reached · back {clockLabel(a.exhausted_until)}</span>}
        <span className="ml-auto flex shrink-0 items-center gap-1">
          <button type="button" className="btn-ghost px-2 py-1 text-[12px]" disabled={busy} onClick={onSignIn} title="Open the claude.ai sign-in page for this account">
            <LogIn size={12} /> {a.signed_in ? "Sign in again" : "Sign in"}
          </button>
          {a.lasting && (
            <button type="button" className="btn-ghost px-1.5 py-1" disabled={busy} onClick={onSignOut} title="Sign this account out of the app" aria-label="Sign out">
              <LogOut size={12} />
            </button>
          )}
          <button type="button" className="btn-ghost px-1.5 py-1" disabled={busy || first} onClick={onUp} title="Use this account earlier" aria-label="Move up">
            <ArrowUp size={12} />
          </button>
          <button type="button" className="btn-ghost px-1.5 py-1 text-bad" disabled={busy} onClick={onRemove} title="Remove from the list (its folder is kept)" aria-label="Remove">
            <Trash2 size={12} />
          </button>
        </span>
      </div>
      <p className="text-[11px]">
        <Identity a={a} />
        {a.signed_in && windowsLine(a.windows) && <span className="text-faint"> · {windowsLine(a.windows)}</span>}
      </p>
      {a.signed_in && a.lasting && <p className="text-[11px] text-faint">{a.message}</p>}
      {a.signed_in && !a.lasting && (
        <p className="text-[11px] text-warn">
          Signed in through its Claude Code folder, which can sign out when other programs use Claude. Press Sign in again once to keep it signed in.
        </p>
      )}
      {a.same_as && (
        <p className="text-[11px] text-warn">
          Same person as {a.same_as}, so it adds no usage. Press Sign in again and approve with your other Claude account.
        </p>
      )}
      <p className="truncate text-[10px] text-faint" title={folderLabel(a)}>
        {folderLabel(a)}
      </p>
      {pending && pending.account.id === a.id && (
        <SignInHelp start={pending.start} plan="Claude" onCode={(code) => api.sendClaudeCode(a.id, code)} />
      )}
    </div>
  );
}

function foundLabel(f: FoundFolder) {
  return f.signed_in ? `${f.email ?? "signed in"} · ${planLabel(f.subscription)}` : "not signed in";
}

function AddAccount({
  found,
  known,
  onAdded,
}: {
  found: FoundFolder[];
  known: Set<string>;
  onAdded: (view: ClaudeAccountsView, label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [folder, setFolder] = useState("");
  const add = useMutation({
    mutationFn: () => api.addClaudeAccount({ label, config_dir: folder.trim() || null }),
    onSuccess: (view) => {
      onAdded(view, label.trim());
      setLabel("");
      setFolder("");
      setOpen(false);
    },
  });
  // Folders signed in as someone the list does not have yet: the accounts still to add.
  const missing = found.filter((f) => f.signed_in && f.email && !known.has(f.email));
  const pick = (f: FoundFolder) => {
    setFolder(f.folder);
    if (!label && f.email) setLabel(f.email.split("@")[0]);
    setOpen(true);
  };
  const slug = label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "name";

  return (
    <div className="space-y-2">
      {missing.length > 0 && !open && (
        <div className="space-y-1 rounded-md border border-dashed border-border p-2 text-[11px]">
          <p className="text-muted">Signed in on this computer but not added yet:</p>
          {missing.map((f) => (
            <div key={f.folder} className="flex items-center gap-2">
              <span className="min-w-0 truncate text-fg" title={f.folder}>
                {foundLabel(f)} <span className="text-faint">({f.folder})</span>
              </span>
              <button type="button" className="btn-ghost ml-auto px-2 py-1 text-[12px]" onClick={() => pick(f)}>
                <Plus size={12} /> Add
              </button>
            </div>
          ))}
        </div>
      )}
      {!open ? (
        <button type="button" className="btn-ghost" onClick={() => setOpen(true)}>
          <Plus size={13} /> Add Claude account
        </button>
      ) : (
        <div className="space-y-2 rounded-md border border-border p-2">
          <input className="input" placeholder="Name, e.g. Personal or Work" value={label} onChange={(e) => setLabel(e.target.value)} />
          <input className="input" placeholder={`Folder (blank: a new ~/.claude-${slug}, then sign in)`} value={folder} onChange={(e) => setFolder(e.target.value)} />
          {found.length > 0 && (
            <div className="flex flex-wrap items-center gap-1 text-[11px]">
              <span className="text-faint">Folders on this computer:</span>
              {found.map((f) => (
                <button type="button" key={f.folder} className={cn("chip", folder === f.folder && "text-accent")} onClick={() => pick(f)} title={f.folder}>
                  {foundLabel(f)}
                </button>
              ))}
            </div>
          )}
          {add.isError && <p className="text-[11px] text-bad">{(add.error as Error).message}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button type="button" className="btn-primary" disabled={add.isPending} onClick={() => add.mutate()}>
              {add.isPending && <Loader2 size={14} className="animate-spin" />} Add
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** The Claude accounts Claude Code uses in turn: who each is, add, sign in, reorder, remove. */
export function ClaudeAccounts({ open }: { open: boolean }) {
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const list = useQuery({ queryKey: ["claude-accounts"], queryFn: api.claudeAccounts, enabled: open, refetchInterval: 3_000 });
  const refresh = (view?: ClaudeAccountsView) => {
    if (view) qc.setQueryData(["claude-accounts"], view);
    void qc.invalidateQueries({ queryKey: ["cli-status", "claude_code"] });
    void qc.invalidateQueries({ queryKey: ["connections"] });
  };
  const signIn = useMutation({
    mutationFn: (a: ClaudeAccount) => api.loginClaudeAccount(a.id).then((start) => ({ a, start })),
    onSuccess: ({ a, start }) => {
      setNote("");
      setPending({ account: a, start });
      refresh();
    },
  });
  const up = useMutation({ mutationFn: api.raiseClaudeAccount, onSuccess: (view) => refresh(view) });
  const signOut = useMutation({
    mutationFn: (a: ClaudeAccount) => api.logoutClaudeAccount(a.id).then((view) => ({ view, a })),
    onSuccess: ({ view, a }) => {
      setNote(`Signed ${a.label} out of the app.`);
      refresh(view);
    },
  });
  const remove = useMutation({
    mutationFn: (a: ClaudeAccount) => api.removeClaudeAccount(a.id).then((view) => ({ view, a })),
    onSuccess: ({ view, a }) => {
      setNote(`Removed ${a.label} from the list. Its folder is untouched.`);
      refresh(view);
    },
  });
  const accounts = list.data?.accounts ?? [];

  // A sign-in in progress ends when that account reports a (new) sign-in: say who it is.
  useEffect(() => {
    if (!pending) return;
    const now = accounts.find((a) => a.id === pending.account.id);
    const before = pending.account;
    if (now?.signed_in && (!before.signed_in || now.email !== before.email || now.token_saved_at !== before.token_saved_at)) {
      setNote(`${now.label} is signed in as ${now.email ?? "your Claude account"} and stays signed in.`);
      setPending(null);
      refresh();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts, pending]);

  const busy = signIn.isPending || up.isPending || signOut.isPending || remove.isPending;
  const error = signIn.error || up.error || signOut.error || remove.error;
  const active = accounts.find((a) => a.active);
  const known = new Set(accounts.map((a) => a.email).filter((e): e is string => !!e));

  return (
    <div className="space-y-2 text-[12px]">
      <div>
        <p className="font-medium text-fg">Claude accounts</p>
        <p className="text-[11px] text-faint">Used in this order; the next takes over when one runs out.</p>
      </div>
      {active && (
        <p className="text-[11px] text-muted">
          Calls use <strong className="text-fg">{active.label}</strong>
          {active.email ? ` (${active.email})` : active.signed_in ? "" : ", which is not signed in"} now.
        </p>
      )}
      {list.isPending && (
        <p className="text-muted">
          <Loader2 size={12} className="inline animate-spin" /> Checking Claude accounts…
        </p>
      )}
      {list.isError && <p className="text-bad">Could not read the Claude accounts: {(list.error as Error).message}</p>}
      {list.data && accounts.length === 0 && <p className="text-muted">No Claude account yet. Add one below.</p>}
      {accounts.map((a, i) => (
        <AccountRow
          key={a.id}
          a={a}
          first={i === 0}
          busy={busy}
          pending={pending}
          onSignIn={() => signIn.mutate(a)}
          onUp={() => up.mutate(a.id)}
          onSignOut={() => {
            if (window.confirm(`Sign ${a.label} out of the app? You will need to sign in again to use it.`)) signOut.mutate(a);
          }}
          onRemove={() => {
            if (window.confirm(`Remove ${a.label} from the app's Claude accounts?`)) remove.mutate(a);
          }}
        />
      ))}
      <AddAccount
        found={list.data?.found ?? []}
        known={known}
        onAdded={(view, label) => {
          refresh(view);
          const added = view.accounts[view.accounts.length - 1];
          if (added && !added.signed_in) {
            signIn.mutate(added);
          } else if (added) {
            setNote(`Added ${label || added.label}, signed in as ${added.email ?? "a Claude account"}.`);
          }
        }}
      />
      {signIn.isPending && (
        <p className="text-[11px] text-muted">
          <Loader2 size={11} className="inline animate-spin" /> Opening the claude.ai sign-in page…
        </p>
      )}
      {note && <p className="text-[11px] text-good">{note}</p>}
      {error && <p className="text-[11px] text-bad">{(error as Error).message}</p>}
    </div>
  );
}
