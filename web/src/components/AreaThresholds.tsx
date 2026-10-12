import { RotateCcw } from "lucide-react";
import type { AreaTier } from "../lib/api";
import { useSearch } from "../stores/searchStore";

const AREAS: { tier: AreaTier; label: string; hint: string }[] = [
  { tier: "progression", label: "Next-level roles", hint: "the next level up in your field" },
  { tier: "adjacent", label: "Adjacent roles", hint: "other functions your experience carries into" },
];

/** Own listing threshold per role area (e.g. more lenient for adjacent roles); an area left
 *  alone follows the match threshold. */
export function AreaThresholds({ disabled }: { disabled: boolean }) {
  const s = useSearch();
  const setArea = (tier: AreaTier, value: number | null) => {
    const next = { ...s.areaThresholds };
    if (value === null) delete next[tier];
    else next[tier] = value;
    s.set({ areaThresholds: next });
  };
  return (
    <div className="min-w-0 space-y-2">
      <span className="label">Thresholds by role area</span>
      {AREAS.map(({ tier, label, hint }) => {
        const own = s.areaThresholds[tier];
        return (
          <div key={tier} className="min-w-0" title={hint}>
            <div className="flex items-center justify-between gap-2 text-[11px]">
              <span className="text-muted">
                {label} · {own ?? s.threshold}
                {own === undefined && <span className="text-faint"> (match threshold)</span>}
              </span>
              {own !== undefined && (
                <button
                  type="button"
                  className="btn-ghost px-1 py-0 text-[10px]"
                  disabled={disabled}
                  onClick={() => setArea(tier, null)}
                  title="Follow the match threshold again"
                >
                  <RotateCcw size={10} /> Reset
                </button>
              )}
            </div>
            <input
              type="range"
              min={40}
              max={95}
              step={5}
              disabled={disabled}
              value={own ?? s.threshold}
              onChange={(e) => setArea(tier, Number(e.target.value))}
              className="block w-full max-w-full accent-[var(--accent)]"
            />
          </div>
        );
      })}
      <span className="block text-[11px] text-faint">
        Jobs in these areas are listed from their own score. Adjacent roles apply when you widen or ask for them in your career intent.
      </span>
    </div>
  );
}
