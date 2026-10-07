#!/usr/bin/env python3
"""AAW TELEMETRY V1 - what a run actually cost, per execution, per role, per chain.

One flat record per executor call (`AAW_TELEMETRY_V1`), appended by the
autonomy controller to `<run>/AUTONOMY/telemetry.jsonl`. The same records can be
rebuilt from an older run (state + RESULTS artifacts) or from an EVIDENCE
report, so every number is traceable to a source file and old runs are not
lost.

Rules:
  * telemetry never stops a run (the writer swallows its own errors);
  * a missing number stays `null` - nothing is invented. Cost is `REPORTED`
    (provider said so, e.g. the Claude CLI `total_cost_usd`), `ESTIMATED`
    (tokens x an operator-supplied `MODEL_PRICING.json` row) or `UNPRICED`;
  * `proxy_units` is a price-independent cost proxy (uncached input = 1,
    cached input = `cache_factor`, output = `output_factor`) so runs of one
    model family stay comparable before prices are known.

    python aaw_telemetry.py run RUN_ID [--stats-root DIR] [--json]
    python aaw_telemetry.py evidence EVIDENCE/some_report.json [...] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "AAW_TELEMETRY_V1"
from aaw_paths import AAW_ROOT  # noqa: E402  (frozen-build aware, like the other data files)

PRICING_FILE = AAW_ROOT / "MODEL_PRICING.json"

CACHE_FACTOR = 0.1      # cached input read relative to uncached input
OUTPUT_FACTOR = 5.0     # output token relative to uncached input token

# Executor -> cost category. The categories are what the optimisation project reasons about:
# how much of a run is *building* and how much is *checking*.
CATEGORY = {"plan": "PLANNING", "execute": "IMPLEMENTATION", "repair": "IMPLEMENTATION",
            "diagnose": "IMPLEMENTATION", "self_verify": "VERIFICATION", "prepare_packet": "REVIEW",
            "review": "REVIEW", "final_review": "REVIEW"}
CATEGORIES = ("PLANNING", "IMPLEMENTATION", "VERIFICATION", "REVIEW")


# -- normalisation -----------------------------------------------------------------

def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Map a provider usage object (Codex or Claude shape) to one token vocabulary.

    `input_total` always includes cached tokens. Codex reports `input_tokens`
    inclusive of `cached_input_tokens`; the Claude CLI reports `input_tokens`
    exclusive of `cache_read_input_tokens` / `cache_creation_input_tokens`.
    """
    u = dict(usage or {})
    if not u:
        return {"input_total": None, "input_cached": None, "input_cache_write": None, "output": None,
                "reasoning": None}
    if "cache_read_input_tokens" in u or "cache_creation_input_tokens" in u:   # Claude shape
        read, write = _int(u.get("cache_read_input_tokens")) or 0, _int(u.get("cache_creation_input_tokens")) or 0
        fresh = _int(u.get("input_tokens")) or 0
        return {"input_total": fresh + read + write, "input_cached": read, "input_cache_write": write,
                "output": _int(u.get("output_tokens")), "reasoning": None}
    return {"input_total": _int(u.get("input_tokens")), "input_cached": _int(u.get("cached_input_tokens")),
            "input_cache_write": _int(u.get("cache_write_input_tokens")), "output": _int(u.get("output_tokens")),
            "reasoning": _int(u.get("reasoning_output_tokens"))}


def proxy_units(tokens: Mapping[str, Any], *, cache_factor: float = CACHE_FACTOR,
                output_factor: float = OUTPUT_FACTOR) -> float | None:
    total, out = tokens.get("input_total"), tokens.get("output")
    if total is None and out is None:
        return None
    cached = min(tokens.get("input_cached") or 0, total or 0)
    return round(((total or 0) - cached) + cached * cache_factor + (out or 0) * output_factor, 1)


def load_pricing(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads(Path(path or PRICING_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"models": {}}
    return data if isinstance(data, dict) and isinstance(data.get("models"), dict) else {"models": {}}


def price_row(pricing: Mapping[str, Any] | None, model: str | None) -> Mapping[str, Any] | None:
    row = ((pricing or {}).get("models") or {}).get(str(model)) if model else None
    if not isinstance(row, Mapping) or row.get("input") is None or row.get("output") is None:
        return None
    return row


def estimate_cost(tokens: Mapping[str, Any], row: Mapping[str, Any] | None) -> float | None:
    """USD from tokens and a price row (USD per million tokens). None when unpriced or no tokens."""
    if row is None or (tokens.get("input_total") is None and tokens.get("output") is None):
        return None
    total, cached = tokens.get("input_total") or 0, min(tokens.get("input_cached") or 0, tokens.get("input_total") or 0)
    write = tokens.get("input_cache_write") or 0
    cached_price = row.get("cached_input") if row.get("cached_input") is not None else row["input"]
    write_price = row.get("cache_write") if row.get("cache_write") is not None else row["input"]
    fresh = max(total - cached - write, 0)
    usd = (fresh * row["input"] + cached * cached_price + write * write_price + (tokens.get("output") or 0) * row["output"])
    return round(usd / 1_000_000, 6)


# -- records ----------------------------------------------------------------------------

def build_record(*, run_id: str, execution: Mapping[str, Any], result: Mapping[str, Any] | None = None,
                 wall_s: float | None = None, outcome: str = "COMPLETED", iteration: Mapping[str, Any] | None = None,
                 chain: Mapping[str, Any] | None = None, pricing: Mapping[str, Any] | None = None,
                 at: str | None = None) -> dict[str, Any]:
    """One record from a controller execution ref and (optionally) its persisted result artifact."""
    result = result or {}
    tokens = normalize_usage(result.get("usage") if isinstance(result.get("usage"), Mapping) else None)
    meta = result.get("provider_meta") if isinstance(result.get("provider_meta"), Mapping) else {}
    reported = meta.get("total_cost_usd") if isinstance(meta.get("total_cost_usd"), (int, float)) else None
    model = execution.get("model") or result.get("model")
    estimated = estimate_cost(tokens, price_row(pricing, model))
    if reported is not None:
        usd, basis = float(reported), "REPORTED"
    elif estimated is not None:
        usd, basis = estimated, "ESTIMATED"
    else:
        usd, basis = None, "UNPRICED"
    executor = execution.get("executor")
    selection = execution.get("selection") or {}
    chain = chain or {}
    return {
        "schema": SCHEMA, "run_id": run_id, "at": at, "execution_id": execution.get("execution_id"),
        "iteration_id": execution.get("iteration_id"), "executor": executor, "role": execution.get("role"),
        "category": CATEGORY.get(str(executor), "OTHER"), "phase": execution.get("phase"),
        "profile_id": execution.get("profile"), "model": model, "effort": execution.get("effort"),
        "harness": execution.get("harness"), "tier": selection.get("tier"),
        "selection_reason": selection.get("selection_reason"),
        "retry_of_execution_id": execution.get("retry_of_execution_id"), "outcome": outcome,
        "wall_s": wall_s if wall_s is not None else result.get("wall_time_s"),
        "tokens": tokens, "proxy_units": proxy_units(tokens), "cost_usd": usd, "cost_basis": basis,
        "iteration_index": (iteration or {}).get("index"), "chain_id": chain.get("chain_id"),
        "chain_position": chain.get("position"), "review_mode": chain.get("review_mode"),
        "complexity": (((iteration or {}).get("plan") or {}).get("implementation_complexity")),
    }


class TelemetryWriter:
    """Append-only JSONL. Never raises: a telemetry failure must not stop a run."""

    def __init__(self, path: Path) -> None:
        self.path, self.errors = Path(path), 0

    def append(self, record: Mapping[str, Any]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            return True
        except Exception:
            self.errors += 1
            return False


def read_records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("schema") == SCHEMA:
                out.append(row)
    except OSError:
        pass
    return out


def reconstruct_from_state(state: Mapping[str, Any], results_root: Path | None,
                           pricing: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Rebuild records for a run that predates the telemetry file (state + RESULTS artifacts)."""
    iterations = {i.get("iteration_id"): i for i in state.get("iterations", [])}
    out = []
    for ref in state.get("executions", []):
        result: Mapping[str, Any] = {}
        path = Path(results_root) / f"{ref.get('execution_id')}.json" if results_root else None
        if path and path.is_file():
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result = {}
        it = iterations.get(ref.get("iteration_id")) or {}
        out.append(build_record(run_id=str(state.get("run_id")), execution=ref, result=result, iteration=it,
                                chain=it.get("chain"), pricing=pricing,
                                outcome="COMPLETED" if ref.get("close_reason", "COMPLETED") == "COMPLETED" else "FAILED"))
    return out


def records_from_evidence(report: Mapping[str, Any], pricing: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Records from an EVIDENCE/*.json live-run report (`executions[]` with usage / cost_usd)."""
    out = []
    for row in report.get("executions") or []:
        if not isinstance(row, Mapping):
            continue
        ex = {"execution_id": row.get("execution_id"), "iteration_id": row.get("iteration_id"),
              "executor": row.get("executor"), "role": row.get("role"), "profile": row.get("profile_id"),
              "model": row.get("runtime_model_id"), "effort": row.get("effort"), "selection": row.get("selection"),
              "phase": row.get("phase")}
        result = {"usage": row.get("usage") or {}, "provider_meta": {"total_cost_usd": row.get("cost_usd")}}
        out.append(build_record(run_id=str(report.get("run_id")), execution=ex, result=result, pricing=pricing,
                                outcome="COMPLETED" if row.get("close_reason", "COMPLETED") == "COMPLETED" else "FAILED"))
    return out


# -- aggregation ------------------------------------------------------------------------

def _sum(rows: Iterable[Mapping[str, Any]], key: str) -> float | None:
    values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return round(sum(values), 6) if values else None


def _tok(rows: Sequence[Mapping[str, Any]], key: str) -> int:
    return int(sum((r.get("tokens") or {}).get(key) or 0 for r in rows))


def _bucket(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = _tok(rows, "input_total")
    return {"calls": len(rows), "input_tokens": total, "cached_tokens": _tok(rows, "input_cached"),
            "output_tokens": _tok(rows, "output"), "proxy_units": _sum(rows, "proxy_units") or 0.0,
            "cost_usd": _sum(rows, "cost_usd"), "wall_s": _sum(rows, "wall_s")}


def _group(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key))].append(row)
    return {k: _bucket(v) for k, v in sorted(groups.items(), key=lambda kv: -(_sum(kv[1], "proxy_units") or 0))}


def summarize(records: Sequence[Mapping[str, Any]], state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rows = list(records)
    total = _bucket(rows)
    cats = {c: _bucket([r for r in rows if r.get("category") == c]) for c in CATEGORIES}
    units = total["proxy_units"] or 0.0
    share = {c: (round(b["proxy_units"] / units, 4) if units else None) for c, b in cats.items()}
    priced = [r for r in rows if r.get("cost_usd") is not None]
    summary: dict[str, Any] = {
        "schema": "AAW_TELEMETRY_SUMMARY_V1", "records": len(rows), "total": total, "by_category": cats,
        "share_of_proxy_units": share, "by_executor": _group(rows, "executor"), "by_profile": _group(rows, "profile_id"),
        "by_review_mode": _group(rows, "review_mode"), "by_chain": _group(rows, "chain_id"),
        "cost_coverage": {"priced_calls": len(priced), "unpriced_calls": len(rows) - len(priced),
                          "bases": sorted({str(r.get("cost_basis")) for r in rows})},
        "failed_calls": sum(1 for r in rows if r.get("outcome") != "COMPLETED"),
        "cache_hit_ratio": round(total["cached_tokens"] / total["input_tokens"], 4) if total["input_tokens"] else None,
    }
    per_iter: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        per_iter[str(row.get("iteration_id"))].append(row)
    summary["by_iteration"] = {k: _bucket(v) for k, v in per_iter.items()}
    if state:
        its = state.get("iterations", [])
        accepted = [i for i in its if i.get("status") in ("ACCEPTED", "PROVISIONAL")]
        summary["outcome"] = {
            "iterations": len(its), "accepted_or_provisional": len(accepted),
            "repairs": sum(len(i.get("repairs", [])) for i in its),
            "deferred_findings": len(state.get("deferred_findings", [])),
            "status": state.get("status"), "phase": state.get("phase")}
        if accepted:
            summary["per_accepted_iteration"] = {
                "proxy_units": round(units / len(accepted), 1),
                "cost_usd": round(total["cost_usd"] / len(accepted), 6) if total["cost_usd"] is not None else None,
                "wall_s": round(total["wall_s"] / len(accepted), 1) if total["wall_s"] is not None else None}
    return summary


def render_text(summary: Mapping[str, Any], title: str = "AAW telemetry") -> str:
    t = summary["total"]
    lines = [f"{title}: {summary['records']} calls, {t['input_tokens']:,} in "
             f"({(summary['cache_hit_ratio'] or 0) * 100:.0f}% cached) / {t['output_tokens']:,} out, "
             f"proxy {t['proxy_units']:,.0f} units, cost "
             f"{('$%.4f' % t['cost_usd']) if t['cost_usd'] is not None else 'n/a'}, "
             f"wall {('%.0fs' % t['wall_s']) if t['wall_s'] is not None else 'n/a'}"]
    lines.append("share of proxy units: " + ", ".join(
        f"{c} {v * 100:.0f}%" for c, v in summary["share_of_proxy_units"].items() if v is not None))
    lines.append("by executor:")
    for name, b in summary["by_executor"].items():
        lines.append(f"  {name:<15} calls {b['calls']:>3}  proxy {b['proxy_units']:>10,.0f}  "
                     f"cost {('$%.4f' % b['cost_usd']) if b['cost_usd'] is not None else 'n/a':>9}")
    lines.append("by profile:")
    for name, b in summary["by_profile"].items():
        lines.append(f"  {name:<22} calls {b['calls']:>3}  proxy {b['proxy_units']:>10,.0f}")
    if summary.get("outcome"):
        o = summary["outcome"]
        lines.append(f"outcome: {o['iterations']} iterations ({o['accepted_or_provisional']} accepted/provisional), "
                     f"{o['repairs']} repairs, {o['deferred_findings']} deferred findings")
    if summary.get("per_accepted_iteration"):
        p = summary["per_accepted_iteration"]
        lines.append(f"per accepted iteration: proxy {p['proxy_units']:,.0f}"
                     + (f", ${p['cost_usd']:.4f}" if p.get("cost_usd") is not None else ""))
    return "\n".join(lines)


# -- CLI --------------------------------------------------------------------------------

def run_records(run_id: str, stats_root: Path | None = None, pricing: Mapping[str, Any] | None = None):
    import autonomy_controller as ctl
    root = Path(stats_root) if stats_root else ctl.STATS_ROOT
    adir = ctl.autonomy_dir(run_id, root)
    state = ctl.load_state(run_id, root)
    records = read_records(adir / "telemetry.jsonl") or reconstruct_from_state(state, adir / "RESULTS", pricing)
    return records, state


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="summarise one autonomy run")
    run.add_argument("run_id")
    run.add_argument("--stats-root", type=Path)
    ev = sub.add_parser("evidence", help="summarise EVIDENCE/*.json live-run reports")
    ev.add_argument("files", nargs="+", type=Path)
    for p in (run, ev):
        p.add_argument("--json", action="store_true")
        p.add_argument("--pricing", type=Path)
    args = parser.parse_args(argv)
    pricing = load_pricing(args.pricing)
    if args.cmd == "run":
        records, state = run_records(args.run_id, args.stats_root, pricing)
        summary, title = summarize(records, state), f"run {args.run_id}"
        print(json.dumps(summary, indent=2) if args.json else render_text(summary, title))
        return 0
    rows: list[dict[str, Any]] = []
    for path in args.files:
        report = json.loads(path.read_text(encoding="utf-8"))
        part = records_from_evidence(report, pricing)
        if not args.json:
            print(render_text(summarize(part), path.name) + "\n")
        rows.extend(part)
    if args.json:
        print(json.dumps(summarize(rows), indent=2))
    elif len(args.files) > 1:
        print(render_text(summarize(rows), "ALL FILES"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
