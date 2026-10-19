import { useMutation } from "@tanstack/react-query";
import { ExternalLink, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { SignInStart } from "../lib/types";

// After Sign in: the provider's page is open in the browser (the CLI opened it, or this opens
// it), the user approves there, and the CLI finishes by itself. Claude's fallback link ends on
// a code instead, pasted here and passed to the waiting CLI.

/** Open the sign-in page unless the CLI already did (the Dock app opens it in the browser). */
export function openSignIn(start: SignInStart) {
  if (start.url && !start.opened) window.open(start.url, "_blank", "noopener");
}

export function SignInHelp({
  start,
  plan,
  onCode,
}: {
  start: SignInStart;
  plan: string;
  onCode?: (code: string) => Promise<unknown>;
}) {
  const [code, setCode] = useState("");
  const opened = useRef<SignInStart | null>(null);
  useEffect(() => {
    if (opened.current === start) return;
    opened.current = start;
    openSignIn(start);
  }, [start]);
  const send = useMutation({ mutationFn: (value: string) => (onCode ? onCode(value) : Promise.resolve()) });

  return (
    <div className="space-y-1.5 rounded-md bg-surface p-2 text-[11px]">
      <p className="text-fg">
        The {plan} sign-in page is open in your browser: approve there and this updates by itself. If it shows another {plan} account, sign out
        there first or open the page in a private window.
      </p>
      {start.url ? (
        <a className="inline-flex items-center gap-1 text-accent" href={start.url} target="_blank" rel="noreferrer">
          <ExternalLink size={11} /> Open the sign-in page
        </a>
      ) : (
        <p className="text-faint">No page appeared? Press Sign in again.</p>
      )}
      {start.needs_code && onCode && (
        <div className="space-y-1">
          <p className="text-muted">If the page shows a code, paste it here:</p>
          <div className="flex gap-1">
            <input className="input" placeholder="Code from the sign-in page" value={code} onChange={(e) => setCode(e.target.value)} />
            <button type="button" className="btn-primary" disabled={!code.trim() || send.isPending} onClick={() => send.mutate(code)}>
              {send.isPending && <Loader2 size={13} className="animate-spin" />} Send
            </button>
          </div>
          {send.isSuccess && <p className="text-good">Code sent. Checking the sign-in…</p>}
          {send.isError && <p className="text-bad">{(send.error as Error).message}</p>}
        </div>
      )}
    </div>
  );
}
