import type { MatchResult } from "./types";

/** A screened stretch with concrete transferable evidence, even when a requirement blocks
 * the strict match verdict. The card keeps the unmet requirements and dealbreakers visible. */
export function isAdjacentPossibility(result: MatchResult): boolean {
  const verdict = result.verdict;
  return !result.excluded && !!verdict && !result.passed && verdict.requirements_checked &&
    verdict.fit_score >= 40 && verdict.transferable.some((point) => point.trim().length > 0);
}

/** The score a "not selected" job is reviewed by. Below 40 an *unscreened* job's pre-filter
 * score says too little to review by hand; a job the matcher judged is listed whatever it
 * scored (user's decision 2026-11-22). Excluded jobs are handled in their own list. */
export function reviewScore(result: MatchResult): number {
  return result.verdict?.fit_score ?? result.score?.total ?? 0;
}

export type ResultTab = "matches" | "to_check" | "adjacent" | "below_threshold" | "not_screened" | "excluded" | "dismissed";

/** The results list a job not selected by the matcher shows in (`screened`: the search ran
 * the job_matcher, so a job without a verdict was left outside its shortlist). */
export function notSelectedTab(result: MatchResult, screened: boolean): ResultTab {
  if (screened && !result.verdict) return "not_screened";
  return screened && isAdjacentPossibility(result) ? "adjacent" : "below_threshold";
}
