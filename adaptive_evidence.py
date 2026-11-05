"""Adaptive evidence loop (roadmap stages 1.1 + 1.2): the Human Gate decision is the ground-truth label.

  1.1  Every closed production run -> one append-only record in `VERIFIED_PRODUCTION_RUNS.jsonl`
       (`AAW_VERIFIED_RUN_V1`): decision label, hashes, telemetry summary, SHADOW summary.
  1.2  `evaluate()` turns those records into an honest report: n, provenance, how often SHADOW would
       have diverged, outcome rates with Wilson intervals, and progress towards the stage-4 entry gates.

This module only RECORDS and REPORTS. It never unlocks CONTROLLED_ADAPTIVE / ACTIVE: `adaptive_policy`
stays locked until a signed authorization exists (roadmap 4.1). `unlock_allowed` is therefore always False.
Stdlib only; recording never raises into the run (a lost label is a diagnostic, not a stopped run).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from aaw_paths import STATS_ROOT

SCHEMA = "AAW_VERIFIED_RUN_V1"
REPORT_SCHEMA = "AAW_ADAPTIVE_EVIDENCE_REPORT_V1"
RUNS_FILE = "VERIFIED_PRODUCTION_RUNS.jsonl"

REQUIRED_VERIFIED_RUNS = 100          # roadmap stage-4 entry condition
REAL_MEASURED = "REAL_MEASURED"
NO_USAGE_EVIDENCE = "NO_USAGE_EVIDENCE"
LABELS = {"APPROVED": "ACCEPT", "REJECTED": "REJECT"}


def _sha(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _runs_path(stats_root: Path | None) -> Path:
    return Path(stats_root or STATS_ROOT) / RUNS_FILE


def read_runs(stats_root: Path | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in _runs_path(stats_root).read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("schema") == SCHEMA:
                out.append(row)
    except OSError:
        pass
    return out


def _provenance(records: Sequence[Mapping[str, Any]]) -> str:
    """REAL_MEASURED only when every executor call carries observed provider usage; scripted/no-usage runs
    can never count towards the stage-4 threshold."""
    if not records:
        return NO_USAGE_EVIDENCE
    ok = all(isinstance((r.get("tokens") or {}).get("input_total"), int) for r in records)
    return REAL_MEASURED if ok else NO_USAGE_EVIDENCE


def _shadow_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    shadows = [r["shadow"] for r in records if isinstance(r.get("shadow"), Mapping)]
    divergent = [s for s in shadows if s.get("divergence")]
    speedups = [s["predicted_speedup"]["value"] for s in shadows
                if isinstance(s.get("predicted_speedup"), Mapping)
                and isinstance(s["predicted_speedup"].get("value"), (int, float))]
    return {"observed": len(shadows), "divergent": len(divergent),
            "diverged": bool(divergent),
            "max_predicted_speedup": max(speedups) if speedups else None,
            "recommended_strategies": sorted({str(s.get("recommended_strategy")) for s in divergent})}


def build_record(*, run_id: str, state: Mapping[str, Any], records: Sequence[Mapping[str, Any]],
                 at: str | None = None) -> dict[str, Any] | None:
    """One label record from a run state at the moment the human decided. None if there is no decision."""
    human = state.get("human") or {}
    label = LABELS.get(str(human.get("verdict")))
    if label is None:
        return None
    costs = [r.get("cost_usd") for r in records]
    priced = [c for c in costs if isinstance(c, (int, float))]
    iterations = state.get("iterations") or []
    hold = state.get("hold") or {}
    return {
        "schema": SCHEMA, "run_id": run_id, "decided_at": at or human.get("at"),
        "decision": label, "decision_id": human.get("approval_id") or _sha({"r": run_id, "h": human})[:23],
        "approver": human.get("approver"), "early_end": bool(human.get("early_end")),
        "mandate_hash": state.get("mandate_hash"), "candidate_id": hold.get("candidate_id") or human.get("candidate_id"),
        "candidate_head": hold.get("candidate_head"),
        "telemetry_sha256": _sha([r.get("execution_id") for r in records]),
        "calls": len(records), "provenance": _provenance(records),
        "iterations": len(iterations),
        "iterations_accepted": sum(1 for i in iterations if i.get("status") == "ACCEPTED"),
        "repair_calls": sum(1 for r in records if r.get("phase") == "REPAIR"),
        "cost_usd": round(sum(priced), 6) if priced else None, "unpriced_calls": len(costs) - len(priced),
        "wall_s": round(sum(r.get("wall_s") or 0 for r in records), 3),
        "shadow": _shadow_summary(records),
    }


def record_gate_decision(run_id: str, state: Mapping[str, Any], records: Sequence[Mapping[str, Any]],
                         stats_root: Path | None = None) -> dict[str, Any] | None:
    """Append the label once per decision (idempotent on `decision_id`). Never raises."""
    try:
        record = build_record(run_id=run_id, state=state, records=records)
        if record is None:
            return None
        if any(r.get("decision_id") == record["decision_id"] and r.get("run_id") == run_id
               for r in read_runs(stats_root)):
            return record
        path = _runs_path(stats_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record
    except Exception:                                   # a lost label must never stop the human surface
        return None


# ── 1.2 evaluation ───────────────────────────────────────────────────────────

def wilson(successes: int, n: int, z: float = 1.96) -> dict[str, Any]:
    """Wilson score interval for a proportion; None bounds when n == 0."""
    if n <= 0:
        return {"n": 0, "rate": None, "low": None, "high": None}
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {"n": n, "rate": round(p, 4), "low": round(max(centre - half, 0.0), 4),
            "high": round(min(centre + half, 1.0), 4)}


def _rate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return wilson(sum(1 for r in rows if r["decision"] == "ACCEPT"), len(rows))


def evaluate(stats_root: Path | None = None, rows: Sequence[Mapping[str, Any]] | None = None,
             required: int = REQUIRED_VERIFIED_RUNS) -> dict[str, Any]:
    """Stage-1.2 report. Counts only REAL_MEASURED runs towards the threshold; reports, never unlocks."""
    runs = list(rows) if rows is not None else read_runs(stats_root)
    real = [r for r in runs if r.get("provenance") == REAL_MEASURED]
    with_shadow = [r for r in real if (r.get("shadow") or {}).get("observed")]
    divergent = [r for r in with_shadow if r["shadow"].get("diverged")]
    aligned = [r for r in with_shadow if not r["shadow"].get("diverged")]
    div_rate, ali_rate = _rate(divergent), _rate(aligned)
    # SHADOW did not execute its recommendation, so a divergent run's label says how the BASELINE fared exactly
    # where the policy disagreed. The policy is only "not worse" if baseline did not do clearly better there.
    not_worse = (div_rate["n"] > 0 and ali_rate["n"] > 0 and div_rate["high"] is not None
                 and ali_rate["low"] is not None and div_rate["high"] >= ali_rate["low"])
    gates = [
        {"gate": "verified_real_runs", "need": required, "have": len(real), "passed": len(real) >= required},
        {"gate": "shadow_coverage", "need": required, "have": len(with_shadow), "passed": len(with_shadow) >= required},
        {"gate": "divergence_not_worse_than_aligned", "passed": bool(not_worse and len(real) >= required),
         "detail": "baseline outcome in diverging runs must not be clearly worse than in aligned runs (CI overlap)"},
    ]
    return {
        "schema": REPORT_SCHEMA, "runs_total": len(runs), "runs_real_measured": len(real),
        "runs_excluded_no_usage": len(runs) - len(real),
        "accept": _rate(real), "shadow": {
            "runs_with_shadow": len(with_shadow),
            "divergence_rate": wilson(len(divergent), len(with_shadow)),
            "accept_rate_when_diverged": div_rate, "accept_rate_when_aligned": ali_rate},
        "progress": {"have": len(real), "need": required, "fraction": round(min(len(real) / required, 1.0), 4)},
        "stage4_entry_gates": gates, "stage4_entry_met": all(g["passed"] for g in gates),
        "unlock_allowed": False,
        "unlock_note": "CONTROLLED_ADAPTIVE stays locked until a signed authorization exists (roadmap 4.1); "
                       "this report never changes the mode.",
    }


def render_text(report: Mapping[str, Any]) -> str:
    p, s = report["progress"], report["shadow"]
    lines = [f"AAW adaptive evidence - {report['runs_real_measured']}/{p['need']} real verified runs "
             f"({p['fraction']:.0%}); {report['runs_excluded_no_usage']} excluded (no usage evidence)",
             f"  accept rate: {report['accept']}",
             f"  SHADOW divergence: {s['divergence_rate']}",
             f"  accept when diverged: {s['accept_rate_when_diverged']}",
             f"  accept when aligned : {s['accept_rate_when_aligned']}"]
    for gate in report["stage4_entry_gates"]:
        lines.append(f"  [{'x' if gate['passed'] else ' '}] {gate['gate']}")
    lines.append("  unlock allowed: NO - " + report["unlock_note"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AAW adaptive evidence report (read-only)")
    parser.add_argument("--stats-root", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args.stats_root)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
