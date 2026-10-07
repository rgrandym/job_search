"""Compare model setups: against your labels (default), or against past verdicts.

    python -m src.services.model_compare --config model_compare.toml   # the /compare_models plan

With `reference = "labels"` (the default) every job you labelled "yes" or "no" in the app
(`services.labels`; "maybe" is left out) is re-screened by each setup, with the profile summary,
search constraints and threshold stored with the label and your current career intent and
location, and each setup is scored against your labels: agreement, good jobs kept, bad jobs let
through, ranking (yes above no), time, cost per model (API-equivalent, `compare_usage`) and the
share of the Codex / Claude Code plan windows it used. All setups judge the same jobs, with a
live progress bar. `learned = true` applies the preferences you accepted from your labels
(`services.learning`) to each label's profile, to measure what they change; `labels_since`
keeps only labels given on or after a date (labels that did not shape those preferences are
the honest test).

`reference = "history"` instead compares with the verdicts already in the search history:

    python -m src.services.model_compare --screening-model gpt-6-luna --sample 40
    python -m src.services.model_compare --baseline "screening gpt-6-sol" \
        --setup gpt-6-luna:medium --setup gpt-6-luna:low --setup gpt-6-sol:low/gpt-6-sol:medium
    python -m src.services.model_compare --config model_compare.toml   # the /compare_models plan

Takes up to `--sample` postings the job_matcher already judged (stratified: strong matches,
near the threshold, adjacent role families, clear rejections), re-screens them with the
candidate setup against the same profile summaries, and reports agreement, good jobs the
candidate misses, weak jobs it newly accepts, elapsed time and model usage. `--baseline`
keeps only searches whose models contain that text (e.g. the setup you trust), and each
`--setup` (screening[:effort][/quality[:effort]]) is compared on the same sample. `--config`
reads sample, baseline and setups from a TOML plan (flags given on the command line win); a
plan setup may also be a table naming another provider, e.g. Claude models next to Codex ones.
Each setup's result is printed as soon as it finishes. Nothing is
remembered (no verdict cache) and nothing in the app changes. Hard exclusions are
deterministic and do not depend on the model, so excluded postings are not sampled.

It calls the paid model for every sampled posting: run it deliberately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import tomllib
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.core.config import LLMProviderName
from src.core.llm import LLMConfig, ModelUsage, Role, make_structured
from src.core.llm_provider import LLMProvider
from src.jobs.models import MatchResult, ProfileSummary, SearchQuery
from src.jobs.profile_memory import intent_text
from src.jobs.screener import screen_jobs
from src.services import compare_usage, history, labels, learning, search_service
from src.services.intent import get_intent
from src.services.workspace import Workspace, get_workspace

NEAR = 10  # "near the threshold": within this many points


class Sampled(BaseModel):
    result: MatchResult
    summary: ProfileSummary
    query: SearchQuery
    threshold: float
    stratum: str


class SetupSpec(BaseModel):
    """A setup on its own provider: `screening` and `quality` are `model[:effort]`."""

    model_config = ConfigDict(extra="forbid")

    provider: LLMProviderName
    screening: str = Field(min_length=1)
    quality: str = Field(min_length=1)


class ComparePlan(BaseModel):
    """A hand-edited comparison plan (`model_compare.toml`). A string setup runs on the
    current provider; a `SetupSpec` table names its own."""

    model_config = ConfigDict(extra="forbid")

    reference: Literal["labels", "history"] = "labels"
    sample: int = Field(24, ge=5, le=60, description="History reference only")
    prices: dict[str, tuple[float, float]] = Field(
        default_factory=dict, description="Model -> USD per 1M (input, output) tokens"
    )
    learned: bool = Field(False, description="Apply the preferences accepted from your labels")
    labels_since: date | None = Field(None, description="Only labels given on or after this")
    baseline: list[str] = Field(default_factory=list)
    setups: list[str | SetupSpec] = Field(min_length=1)


def load_plan(path: Path) -> ComparePlan:
    """Read and validate a TOML plan; a typo fails before any model is called."""
    with path.open("rb") as f:
        return ComparePlan.model_validate(tomllib.load(f))


class Comparison(BaseModel):
    job_id: str
    title: str
    company: str
    stratum: str
    baseline_fit: int
    baseline_match: bool
    candidate_fit: int | None = None  # None: the candidate gave no verdict (error)
    candidate_match: bool | None = None


class CompareReport(BaseModel):
    setup: str
    compared: int
    agreement: float = Field(description="Share with the same match decision")
    missed: list[Comparison] = Field(description="Baseline match, candidate not")
    new_accepts: list[Comparison] = Field(description="Candidate match, baseline not")
    mean_abs_fit_change: float
    seconds: float
    calls: int
    input_tokens: int
    output_tokens: int
    errors: list[str] = Field(default_factory=list)
    rows: list[Comparison] = Field(default_factory=list)


def _stratum(r: MatchResult, threshold: float) -> str:
    v = r.verdict
    assert v is not None
    if abs(v.fit_score - threshold) <= NEAR:
        return "near threshold"
    if v.fit_score >= 80:
        return "strong"
    return "match" if v.match else "rejected"


def sample(ws: Workspace, size: int, baseline: list[str] | None = None) -> list[Sampled]:
    """Judged postings from the history, one per job, spread over the strata in turn. With
    `baseline`, only searches whose models contain one of those texts are used."""
    seen: set[str] = set()
    by_stratum: dict[str, list[Sampled]] = {}
    for item in history.list_history(ws):
        if baseline and not any(b in (item.models or "") for b in baseline):
            continue
        req, outcome = history.open_entry(ws, item.id)
        report = outcome.report
        if report.summary is None:
            continue
        adjacent = {f.name for f in report.summary.role_families if f.tier == "adjacent"}
        threshold = ws.settings.score_threshold if req.threshold is None else req.threshold
        for r in report.scored():
            if r.verdict is None or r.job.id in seen:
                continue
            seen.add(r.job.id)
            stratum = "adjacent" if r.family in adjacent else _stratum(r, threshold)
            by_stratum.setdefault(stratum, []).append(
                Sampled(
                    result=r,
                    summary=report.summary,
                    query=req.query,
                    threshold=threshold,
                    stratum=stratum,
                )  # fmt: skip
            )
    out: list[Sampled] = []
    while len(out) < size and any(by_stratum.values()):
        for items in by_stratum.values():
            if items and len(out) < size:
                out.append(items.pop(0))
    return out


async def compare(
    picks: list[Sampled], llm_for: Callable[[Role], LLMProvider], setup: str
) -> CompareReport:
    """Re-screen `picks` with the candidate models; group by profile so each batch sees the
    same profile summary the baseline did."""
    t0 = time.monotonic()
    groups: dict[str, list[Sampled]] = {}
    for p in picks:
        groups.setdefault(p.summary.model_dump_json(), []).append(p)
    rows: list[Comparison] = []
    errors: list[str] = []
    for items in groups.values():
        first = items[0]
        verdicts, errs = await screen_jobs(
            first.summary,
            [p.result.job for p in items],
            llm_for("screening"),
            first.query,
            threshold=first.threshold,
            review_llm=llm_for("quality"),
        )
        errors += errs
        for p in items:
            base, new = p.result.verdict, verdicts.get(p.result.job.id)
            assert base is not None
            rows.append(
                Comparison(
                    job_id=p.result.job.id,
                    title=p.result.job.title,
                    company=p.result.job.company,
                    stratum=p.stratum,
                    baseline_fit=base.fit_score,
                    baseline_match=base.match,
                    candidate_fit=new.fit_score if new else None,
                    candidate_match=new.match if new else None,
                )
            )
    judged = [r for r in rows if r.candidate_fit is not None]
    same = sum(r.baseline_match == r.candidate_match for r in judged)
    diffs = [abs((r.candidate_fit or 0) - r.baseline_fit) for r in judged]
    return CompareReport(
        setup=setup,
        compared=len(judged),
        agreement=round(same / len(judged), 3) if judged else 0.0,
        missed=[r for r in judged if r.baseline_match and not r.candidate_match],
        new_accepts=[r for r in judged if r.candidate_match and not r.baseline_match],
        mean_abs_fit_change=round(sum(diffs) / len(diffs), 1) if diffs else 0.0,
        seconds=round(time.monotonic() - t0, 1),
        calls=0,
        input_tokens=0,
        output_tokens=0,
        errors=errors,
        rows=rows,
    )


def run(ws: Workspace, cfg: LLMConfig, picks: list[Sampled]) -> CompareReport:
    """Compare `cfg` against the sampled baseline verdicts, counting the candidate's usage."""
    usage: list[ModelUsage] = []
    setup = _setup_name(cfg)

    def llm_for(role: Role) -> LLMProvider:
        return make_structured(cfg, role, usage.append, f"Model comparison ({role})")

    report = asyncio.run(compare(picks, llm_for, setup))
    return report.model_copy(
        update={
            "calls": len(usage),
            "input_tokens": sum(u.input_tokens for u in usage),
            "output_tokens": sum(u.output_tokens for u in usage),
        }
    )


def _text(r: CompareReport) -> str:
    lines = [
        f"Candidate: {r.setup}",
        f"Compared {r.compared} postings · same decision {r.agreement:.0%} · mean fit change "
        f"{r.mean_abs_fit_change} points · {r.seconds}s · {r.calls} calls "
        f"({r.input_tokens:,} in / {r.output_tokens:,} out tokens)",
        f"Missed good jobs ({len(r.missed)}):",
        *(
            f"  {m.baseline_fit}->{m.candidate_fit} [{m.stratum}] {m.title} · {m.company}"
            for m in r.missed
        ),  # fmt: skip
        f"Newly accepted ({len(r.new_accepts)}):",
        *(
            f"  {m.baseline_fit}->{m.candidate_fit} [{m.stratum}] {m.title} · {m.company}"
            for m in r.new_accepts
        ),  # fmt: skip
    ]
    if r.errors:
        lines.append(f"Errors: {len(r.errors)} (first: {r.errors[0]})")
    return "\n".join(lines)


# ---------------------------------------------------------------- against your labels


class LabelCompareReport(BaseModel):
    """One setup re-screening your labelled jobs, scored against your labels."""

    setup: str
    score: labels.SetupAgreement
    seconds: float
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    costs: list[compare_usage.ModelCost] = Field(default_factory=list)
    cost_usd: float | None = Field(None, description="None when a model has no known price")
    plan: list[compare_usage.PlanUsage] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def labelled_jobs(
    ws: Workspace, since: date | None = None
) -> tuple[list[labels.LabelledJob], dict[str, ProfileSummary]]:
    """Your "yes" and "no" labels that carry a profile summary (given on or after `since`),
    and those summaries."""
    store = labels.load(ws)
    items = [
        x
        for x in store.labels
        if x.label != "maybe"
        and x.profile_key in store.profiles
        and (since is None or x.labelled_at[:10] >= since.isoformat())
    ]
    return items, store.profiles


async def compare_labels(
    items: list[labels.LabelledJob],
    profiles: dict[str, ProfileSummary],
    llm_for: Callable[[Role], LLMProvider],
    setup: str,
    context: tuple[str, Callable[[SearchQuery], str]] = ("", lambda _: ""),
    progress: compare_usage.Progress | None = None,
    adjust: Callable[[ProfileSummary], ProfileSummary] = lambda s: s,
) -> LabelCompareReport:
    """Re-screen `items` with one setup, each group with the summary, constraints and
    threshold its labels were given under; `context` is (career intent text, location base);
    `adjust` changes each profile first (e.g. adds your learned preferences)."""
    t0 = time.monotonic()
    intent, base_for = context
    groups: dict[tuple[str, str, float], list[labels.LabelledJob]] = {}
    for x in items:
        key = (x.profile_key or "", x.query.model_dump_json(), x.threshold)
        groups.setdefault(key, []).append(x)
    scored: list[labels.LabelledJob] = []
    errors: list[str] = []
    for g, ((profile_key, _, threshold), group) in enumerate(groups.items()):
        query = group[0].query
        verdicts, errs = await screen_jobs(
            adjust(profiles[profile_key]),
            [x.job for x in group],
            llm_for("screening"),
            query,
            threshold=threshold,
            base=base_for(query),
            intent=intent,
            review_llm=llm_for("quality"),
            progress=progress.first_pass(g) if progress else None,
            review_progress=progress.second_opinions(g) if progress else None,
        )
        errors += errs
        scored += [
            x.model_copy(update={"verdicts": {setup: verdicts[x.job.id]}})
            for x in group
            if x.job.id in verdicts
        ]
    return LabelCompareReport(
        setup=setup,
        score=labels.agreement(setup, scored),
        seconds=round(time.monotonic() - t0, 1),
        errors=errors,
    )


def run_labels(
    ws: Workspace,
    cfg: LLMConfig,
    items: list[labels.LabelledJob],
    profiles: dict[str, ProfileSummary],
    prices: dict[str, tuple[float, float]] | None = None,
    label: str = "",
    learned: bool = False,
) -> LabelCompareReport:
    """Score `cfg` against your labels, as the app would screen (intent and location), with
    its cost per model and the plan usage measured around it."""
    usage: list[ModelUsage] = []

    def llm_for(role: Role) -> LLMProvider:
        return make_structured(cfg, role, usage.append, f"Model comparison ({role})")

    cv = ws.master_cv
    intent = get_intent(ws)
    prefs = learning.load(ws) if learned else []
    context = (
        intent_text(intent),
        lambda query: search_service.screening_base(cv, query),
    )
    before = compare_usage.plan_snapshot(cfg)
    bar = compare_usage.Progress(label or _setup_name(cfg), len(items))
    try:
        report = asyncio.run(
            compare_labels(
                items,
                profiles,
                llm_for,
                _setup_name(cfg),
                context,
                bar,
                lambda s: learning.apply_learned(s, prefs, cv, intent),
            )
        )
    finally:
        bar.close()
    per_model = compare_usage.costs(usage, compare_usage.known_prices(prices))
    return report.model_copy(
        update={
            "calls": len(usage),
            "input_tokens": sum(u.input_tokens for u in usage),
            "output_tokens": sum(u.output_tokens for u in usage),
            "costs": per_model,
            "cost_usd": compare_usage.total_cost(per_model),
            "plan": compare_usage.plan_usage(before, compare_usage.plan_snapshot(cfg)),
        }
    )


def _label_text(r: LabelCompareReport) -> str:
    s = r.score
    lines = [
        f"Setup: {r.setup}",
        f"  agreement {_pct(s.agreement)} · good jobs kept {s.kept_yes}/{s.yes} · "
        f"let through {s.passed_no}/{s.no} · ranking {_pct(s.ranking)} · {r.seconds}s · "
        f"{r.calls} calls ({r.input_tokens:,} in / {r.output_tokens:,} out tokens)",
        *(f"  {_cost_line(c)}" for c in r.costs),
        *(f"  plan {_plan_line(u)}" for u in r.plan),
        *(f"  missed: {m}" for m in s.misses),
        *(f"  let through: {m}" for m in s.false_accepts),
    ]
    if r.errors:
        lines.append(f"  errors: {len(r.errors)} (first: {r.errors[0]})")
    return "\n".join(lines)


def _label_summary(reports: list[LabelCompareReport]) -> str:
    """One line per setup, so a sweep reads as a table."""
    lines = ["agree  kept   let-through  ranking  seconds  calls  cost $   session  setup"]
    for r in reports:
        s = r.score
        cost = "-" if r.cost_usd is None else f"{r.cost_usd:.2f}"
        session = next((u for u in r.plan if u.window.startswith("session")), None)
        used = "-" if session is None or session.used is None else f"+{session.used:g}%"
        lines.append(
            f"{_pct(s.agreement):>5}  {f'{s.kept_yes}/{s.yes}':>5}  "
            f"{f'{s.passed_no}/{s.no}':>11}  {_pct(s.ranking):>7}  {r.seconds:>7}  "
            f"{r.calls:>5}  {cost:>6}  {used:>7}  {r.setup}"
        )
    return "\n".join(lines)


def _cost_line(c: compare_usage.ModelCost) -> str:
    price = "no price known" if c.cost_usd is None else f"${c.cost_usd:.2f}"
    return f"{c.model}: {c.calls} calls, {c.input_tokens:,} in / {c.output_tokens:,} out · {price}"


def _plan_line(u: compare_usage.PlanUsage) -> str:
    if u.used is not None:
        return f"{u.window}: +{u.used:g}% ({u.before:g}% -> {u.after:g}%)"
    seen = "" if u.after is None else f" (now {u.after:g}%)"
    return f"{u.window}: {u.note}{seen}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _setup_name(cfg: LLMConfig) -> str:
    return (
        f"{cfg.provider} · screening {cfg.screening_model} ({cfg.screening_effort}) "
        f"· quality {cfg.quality_model} ({cfg.quality_effort})"
    )


def parse_setup(spec: str) -> dict[str, str]:
    """`screening[:effort][/quality[:effort]]` -> LLMConfig fields; omitted parts keep the flags."""
    screening, _, quality = spec.partition("/")
    out: dict[str, str] = {}
    for role, part in (("screening", screening), ("quality", quality)):
        model, _, effort = part.partition(":")
        if model:
            out[f"{role}_model"] = model
        if effort:
            out[f"{role}_effort"] = effort
    return out


def setup_config(ws: Workspace, base: dict[str, Any], spec: str | SetupSpec) -> LLMConfig:
    """The LLMConfig for one setup. `base` is the current provider's config (with the command
    line flags); another provider starts from its own settings and saved key."""
    if isinstance(spec, str):
        return LLMConfig.model_validate(base | parse_setup(spec))
    fields = parse_setup(f"{spec.screening}/{spec.quality}")
    if spec.provider == base["provider"]:
        return LLMConfig.model_validate(base | fields)
    cfg = LLMConfig.from_settings(ws.settings, spec.provider)
    key = ws.saved_key(spec.provider)
    update: dict[str, Any] = fields | ({"api_key": SecretStr(key)} if key else {})
    return LLMConfig.model_validate(cfg.model_dump() | {"api_key": cfg.api_key} | update)


def _summary(reports: list[CompareReport]) -> str:
    """One line per setup, so a sweep reads as a table."""
    lines = ["agree  missed  new  |fit|  seconds  calls  tokens in/out  setup"]
    for r in reports:
        lines.append(
            f"{r.agreement:>5.0%}  {len(r.missed):>6}  {len(r.new_accepts):>3}  "
            f"{r.mean_abs_fit_change:>5}  {r.seconds:>7}  {r.calls:>5}  "
            f"{r.input_tokens:,}/{r.output_tokens:,}  {r.setup}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ws = get_workspace()
    cur = ws.llm
    p = argparse.ArgumentParser(description="Compare model setups with past verdicts")
    p.add_argument("--config", type=Path, help="TOML plan: reference, sample, baseline, setups")
    p.add_argument("--reference", choices=["labels", "history"], help="Default: the plan's")
    p.add_argument("--sample", type=int, help="Postings to re-screen (default 24, or the plan's)")
    p.add_argument("--baseline", action="append", help="Only searches whose models contain this")
    p.add_argument("--setup", action="append", help="screening[:effort][/quality[:effort]]")
    p.add_argument("--provider", default=cur.provider)
    p.add_argument("--screening-model", default=cur.screening_model)
    p.add_argument("--screening-effort", default=cur.screening_effort)
    p.add_argument("--quality-model", default=cur.quality_model)
    p.add_argument("--quality-effort", default=cur.quality_effort)
    p.add_argument("--json", action="store_true", help="Print the full reports as JSON")
    args = p.parse_args(argv)
    plan = load_plan(args.config) if args.config else ComparePlan(setups=[""])
    size = args.sample or plan.sample
    baseline = args.baseline or plan.baseline
    flags: dict[str, Any] = {
        "provider": args.provider,
        "screening_model": args.screening_model,
        "screening_effort": args.screening_effort,
        "quality_model": args.quality_model,
        "quality_effort": args.quality_effort,
    }
    base = cur.model_dump() | flags | {"api_key": cur.api_key}
    specs: list[str | SetupSpec] = [*args.setup] if args.setup else plan.setups
    configs = [setup_config(ws, base, s) for s in specs]
    if (args.reference or plan.reference) == "labels":
        _main_labels(ws, configs, args.json, plan)
    else:
        _main_history(ws, configs, size, baseline, args.json)


def _main_labels(ws: Workspace, configs: list[LLMConfig], as_json: bool, plan: ComparePlan) -> None:
    items, profiles = labelled_jobs(ws, plan.labels_since)
    if not items:
        raise SystemExit("No 'yes' or 'no' labels (in that period): label jobs in the app first.")
    yes = sum(x.label == "yes" for x in items)
    if not as_json:
        since = f" since {plan.labels_since}" if plan.labels_since else ""
        learned = len([p for p in learning.load(ws) if p.status == "accepted"])
        prefs = f" · with your {learned} learned preference(s)" if plan.learned else ""
        print(
            f"Reference: your labels{since} · {len(items)} jobs "
            f"({yes} yes, {len(items) - yes} no){prefs}"
        )
    reports: list[LabelCompareReport] = []
    for i, cfg in enumerate(configs, 1):
        label = f"setup {i}/{len(configs)}"
        reports.append(run_labels(ws, cfg, items, profiles, plan.prices, label, plan.learned))
        if not as_json:
            print(f"\n{_label_text(reports[-1])}", flush=True)  # sweeps are slow: show each
    if as_json:
        print(json.dumps([r.model_dump() for r in reports], indent=2))
    elif len(reports) > 1:
        print(f"\n{_label_summary(reports)}")


def _main_history(
    ws: Workspace, configs: list[LLMConfig], size: int, baseline: list[str], as_json: bool
) -> None:
    picks = sample(ws, size, baseline)
    if not picks:
        raise SystemExit("No judged postings in the history match the baseline.")
    if not as_json:
        print(f"Baseline: {len(picks)} postings · {', '.join(baseline or ['all searches'])}")
    reports: list[CompareReport] = []
    for cfg in configs:
        reports.append(run(ws, cfg, picks))
        if not as_json:
            print(f"\n{_text(reports[-1])}", flush=True)  # sweeps are slow: show each result
    if as_json:
        dumped = [r.model_dump() for r in reports]
        print(json.dumps(dumped[0] if len(dumped) == 1 else dumped, indent=2))
    elif len(reports) > 1:
        print(f"\n{_summary(reports)}")


if __name__ == "__main__":
    main()
