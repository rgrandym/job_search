import { ArrowRightLeft, Hourglass, Square, Zap } from "lucide-react";
import { clockLabel, routeLabel } from "../lib/labels";
import type { Provider } from "../lib/types";

/** A plan ran out with work left: continue on the other plan's equivalent model (same effort),
 *  wait for the reset and continue on the same model, or stop and keep what was done. */
export function LimitOptions({
  what,
  configured,
  resetsAt,
  replacement,
  onSwitch,
  onWait,
  onStop,
}: {
  what: string;
  configured: { provider: Provider; model: string };
  resetsAt: number;
  replacement: { provider: Provider; model: string } | null;
  onSwitch: () => void;
  onWait: () => void;
  onStop: () => void;
}) {
  return (
    <div className="space-y-2 rounded-md border border-warn/50 bg-surface p-2.5 text-[12px]">
      <p className="flex items-start gap-1.5">
        <Zap size={13} className="mt-0.5 shrink-0 text-warn" />
        <span>
          <b>{routeLabel(configured)}</b>&apos;s plan ran out during {what}. It resets {clockLabel(resetsAt)}.
        </span>
      </p>
      <div className="flex flex-wrap gap-1.5">
        {replacement ? (
          <button className="btn-primary py-1 text-[12px]" onClick={onSwitch} title="The other plan's model of the same standing, at the same effort">
            <ArrowRightLeft size={12} /> Continue on {routeLabel(replacement)}
          </button>
        ) : (
          <span className="self-center text-[11px] text-faint">No other plan is signed in (or it is spent too).</span>
        )}
        <button className="btn-ghost py-1 text-[12px]" onClick={onWait} title="Keep this tab open; it continues by itself">
          <Hourglass size={12} /> Wait, continue at {clockLabel(resetsAt)}
        </button>
        <button className="btn-ghost py-1 text-[12px]" onClick={onStop} title="Keep what was done">
          <Square size={11} /> Stop
        </button>
      </div>
    </div>
  );
}
