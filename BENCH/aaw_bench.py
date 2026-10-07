#!/usr/bin/env python3
"""AAW-BENCH - cost per solved task for the models AAW uses to implement.

Why not a public leaderboard: the number that matters for AAW is *what one bounded implementation
iteration costs, run through AAW's own harness (Codex / Claude CLI, the exact effort levels in
IMPLEMENTER_PROFILES, the real execute prompt and schema), and how often it ends with working code*.
Public SWE-bench-style numbers use other harnesses and other effort settings and move by ~2x with the
harness alone. AAW-Bench therefore runs each task through the production `AutonomyController` with the
production `execute` executor; only the planner and the reviewers are scripted, so every token, second and
dollar measured is the implementation's own. Public leaderboards stay what they are: a prior for which
profiles are worth benchmarking at all.

Scoring is objective: a hidden acceptance test suite (never shown to the model) plus a forbidden-path check
on the produced diff. Each task has a verified reference solution, so `validate` proves, without any model,
that every task fails on the starting repo and passes with the solution.

    python BENCH/aaw_bench.py list
    python BENCH/aaw_bench.py validate                       # no model, no cost
    python BENCH/aaw_bench.py run --profiles GPT6_LUNA_HIGH,SOL_HIGH --repeats 2 --yes-spend
    python BENCH/aaw_bench.py analyze BENCH/results/aaw_bench_results.jsonl [--floor 0.8] [--basis usd|proxy]
    python BENCH/aaw_bench.py production RUN_ID [...]        # same analysis from real runs' telemetry
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
sys.path.insert(0, str(ROOT))

TASKS = BENCH / "tasks"
RESULTS = BENCH / "results" / "aaw_bench_results.jsonl"
ROW_SCHEMA = "AAW_BENCH_ROW_V1"


# -- tasks ----------------------------------------------------------------------------------

def load_tasks(ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
    tasks = []
    for path in sorted(TASKS.glob("*/task.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        task["dir"] = path.parent
        if ids and not any(task["id"] == i or task["id"].startswith(i) for i in ids):
            continue
        tasks.append(task)
    return tasks


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=bench@example.invalid", "-c", "user.name=bench",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout.strip()


def materialize(task: Mapping[str, Any], dest: Path) -> Path:
    """The starting repo of a task as a fresh git repository on `main` with one commit."""
    shutil.copytree(Path(task["dir"]) / "repo", dest)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "bench start")
    return dest


def overlay(src: Path, dest: Path) -> None:
    for path in src.rglob("*"):
        if path.is_file():
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def acceptance(task: Mapping[str, Any], workdir: Path, *, timeout: int = 120) -> dict[str, Any]:
    """Run the hidden acceptance tests against `workdir` and check the forbidden paths of the produced diff."""
    test = workdir / "accept_test.py"
    shutil.copyfile(Path(task["dir"]) / "accept_test.py", test)
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header",
                               "accept_test.py"], cwd=workdir, capture_output=True, text=True, timeout=timeout)
        passed, tail = proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        passed, tail = False, "acceptance tests timed out"
    finally:
        test.unlink(missing_ok=True)
    changed = {line.strip().replace("\\", "/") for line in
               (git(workdir, "diff", "--name-only", "HEAD") + "\n" + git(workdir, "ls-files", "--others",
                                                                         "--exclude-standard")).splitlines() if line.strip()}
    changed = {c for c in changed if "__pycache__" not in c and not c.endswith(".pyc")}
    violated = sorted(c for c in changed for f in task.get("forbidden_paths", []) if c == f or c.startswith(f.rstrip("/") + "/"))
    return {"passed": passed and not violated, "tests_passed": passed, "forbidden_violations": violated,
            "changed_files": sorted(changed), "tail": tail}


def validate_tasks(ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Every task must fail on its starting repo and pass with its reference solution (no model involved)."""
    report = []
    for task in load_tasks(ids):
        with tempfile.TemporaryDirectory(prefix="aaw_bench_validate_") as tmp:
            repo = materialize(task, Path(tmp) / "repo")
            before = acceptance(task, repo)
            overlay(Path(task["dir"]) / "solution", repo)
            after = acceptance(task, repo)
        report.append({"id": task["id"], "difficulty": task["difficulty"], "fails_before": not before["tests_passed"],
                       "passes_with_solution": after["passed"], "ok": (not before["tests_passed"]) and after["passed"],
                       "detail": None if (not before["tests_passed"]) and after["passed"] else
                       {"before": before["tail"][-400:], "after": after["tail"][-400:]}})
    return report


# -- running a task through the production controller -------------------------------------

def _mandate(task: Mapping[str, Any]) -> dict[str, Any]:
    criteria = list(task["acceptance_criteria"])
    return {"mandate_id": "BENCH_" + task["id"],
            "iteration_contract": {"goal": task["goal"], "scope": list(task.get("touched_areas", ["shop"])),
                                   "acceptance_criteria": criteria, "constraints": list(task.get("constraints", [])),
                                   "forbidden_changes": [f"modify {p}" for p in task.get("forbidden_paths", [])],
                                   "required_evidence": ["unit tests"]},
            "roadmap_mandate": {"objective": task["title"], "priorities": ["correctness"],
                                "items": [{"item_id": "T", "title": task["title"]}],
                                "autonomy_bounds": {"max_iterations": 1, "max_repair_attempts": 1,
                                                    "allowed_areas": ["shop", "tests"],
                                                    "forbidden_areas": list(task.get("forbidden_paths", []))}}}


def _roles_for(profile_id: str) -> dict[str, Any]:
    import autonomy_contract as ac
    config = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    for key in ("policy_profiles", "routing", "repair_escalation", "chain"):
        config.pop(key, None)
    config["roles"]["implementer"] = {"profile_id": profile_id}
    config["allow_same_model_fresh_context"] = True
    catalog = {p["profile_id"]: p for p in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text(encoding="utf-8"))["profiles"]}
    return ac.validate_roles(config, catalog)


def scripted_executors(task: Mapping[str, Any], execute: Callable[[dict], Any]) -> dict[str, Callable[[dict], Any]]:
    """Real `execute`; scripted planner, self-verifier and reviewers so the cost measured is the implementation's own."""
    def plan(ctx):
        return {"status": "ITERATION", "mandate_hash": ctx["mandate"]["mandate_hash"], "goal": task["goal"],
                "roadmap_refs": ["T"], "scope_justification": "benchmark task",
                "acceptance_criteria": list(task["acceptance_criteria"]), "touched_areas": list(task.get("touched_areas", ["shop"])),
                "decisions": [], "skipped_items": []}

    def self_verify(ctx):   # supersede whatever the implementer self-reported: scoring is the hidden suite, not self-reports
        names = list((ctx["iteration"].get("evidence_state") or {}) or ["unit tests"])
        return {"summary": "scripted", "checks": [{"name": n, "status": "PASS", "summary": "bench"} for n in names]}

    def review(ctx):
        return {"verdict": "PASS", "summary": "scripted", "findings": []}

    return {"plan": plan, "execute": execute, "self_verify": self_verify, "review": review, "final_review": review,
            "repair": lambda ctx: (_ for _ in ()).throw(AssertionError("benchmark never repairs"))}


def run_one(task: Mapping[str, Any], profile_id: str, repeat: int, *, workroot: Path,
            execute_factory: Callable[[], Callable[[dict], Any]] | None = None, timeout: int = 1800) -> dict[str, Any]:
    """One (task, profile, repeat) trial through the production controller; returns a result row."""
    import aaw_telemetry as tel
    import autonomy_adapters as aa
    import autonomy_controller as ctl
    run_dir = Path(tempfile.mkdtemp(prefix=f"bench_{task['id'][:3]}_", dir=workroot))
    repo = materialize(task, run_dir / "repo")
    wt = run_dir / "wt"
    git(repo, "worktree", "add", "-q", "-b", "bench/work", str(wt))
    execute = execute_factory() if execute_factory else aa.DirectRoleExecutor("execute", timeout=timeout)
    roles = _roles_for(profile_id)
    run_id = f"BENCH_{task['id']}_{profile_id}_{repeat}_{int(time.time())}"
    started = time.monotonic()
    controller = ctl.AutonomyController.start(
        run_id, _mandate(task), executors=scripted_executors(task, execute),
        env=ctl.GitWorkspaceEnvironment(repo, wt), roles=roles, stats_root=run_dir / "stats")
    state = controller.run()
    wall = round(time.monotonic() - started, 2)
    records = [r for r in tel.read_records(controller.dir / "telemetry.jsonl") if r["executor"] == "execute"]
    escalation = state.get("escalation")
    result = acceptance(task, wt)
    tokens = {k: sum((r["tokens"] or {}).get(k) or 0 for r in records) for k in
              ("input_total", "input_cached", "output", "reasoning")}
    usd = [r["cost_usd"] for r in records if r.get("cost_usd") is not None]
    row = {"schema": ROW_SCHEMA, "source": "BENCH", "task_id": task["id"], "difficulty": task["difficulty"],
           "profile_id": profile_id, "repeat": repeat, "solved": bool(result["passed"] and not escalation),
           "reason": ("EXECUTOR_" + str(escalation["code"])) if escalation else
           ("OK" if result["passed"] else "FORBIDDEN_PATH" if result["forbidden_violations"] else "ACCEPTANCE_FAILED"),
           "calls": len(records), "tokens": tokens, "proxy_units": tel.proxy_units(tokens),
           "cost_usd": round(sum(usd), 6) if usd and len(usd) == len(records) else None,
           "cost_basis": sorted({r.get("cost_basis") for r in records}) or None,
           "wall_s": round(sum(r.get("wall_s") or 0 for r in records), 2) or wall,
           "model": records[0].get("model") if records else None, "effort": records[0].get("effort") if records else None,
           "changed_files": result["changed_files"], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    shutil.rmtree(run_dir, ignore_errors=True)
    return row


def done_keys(path: Path) -> set[tuple[str, str, int]]:
    keys = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("schema") == ROW_SCHEMA:
                keys.add((row["task_id"], row["profile_id"], row["repeat"]))
    return keys


# -- analysis (pure) -------------------------------------------------------------------------------
from aaw_benchstats import (COMPLEXITY_DIFFICULTY, DIFFICULTIES, TIER_OF, _group, best_ladders, cascade,  # noqa: E402,F401
                            choose_basis, cost_of, pareto, profile_stats, stat_block, tier_recommendations, wilson)


def analyze(rows: Sequence[Mapping[str, Any]], *, basis: str = "auto", floor: float = 0.8, ladder_floor: float = 0.9,
            min_n: int = 3, max_ladder: int = 3) -> dict[str, Any]:
    rows = [r for r in rows if r.get("schema") == ROW_SCHEMA or r.get("source") == "PRODUCTION"]
    basis = choose_basis(rows, basis)
    stats = profile_stats(rows, basis)
    models = {r["profile_id"]: r.get("model") for r in rows}
    return {"basis": basis, "rows": len(rows), "profiles": stats, "pareto": pareto(stats),
            "tiers": tier_recommendations(rows, basis, floor=floor, min_n=min_n),
            "ladders": best_ladders(rows, basis, max_len=max_ladder, floor=ladder_floor),
            "warnings": [w for w in (
                "cost basis is price-independent proxy units: comparisons across different model families are NOT "
                "price-aware; fill MODEL_PRICING.json for a fair cross-model ranking" if basis == "proxy" and len(
                    {m for m in models.values() if m}) > 1 else None,
                "some profiles have fewer than min_n trials per band: their rows are shown but never recommended"
                if any(s["n"] < min_n for s in stats.values()) else None,
                "pricing rows are NOT_VERIFIED list prices; subscription quota is not billed per token"
                if basis == "usd" else None) if w]}


def render_analysis(report: Mapping[str, Any]) -> str:
    unit = "USD" if report["basis"] == "usd" else "proxy units"
    lines = [f"AAW-Bench analysis: {report['rows']} trials, cost basis: {unit}"]
    lines.append(f"{'profile':<26}{'n':>4}{'solved':>8}{'rate':>7}{'95% CI':>14}{'cost/solved':>14}{'wall s':>9}")
    for profile, s in sorted(report["profiles"].items(), key=lambda kv: (kv[1]["cost_per_solved"] is None, kv[1]["cost_per_solved"] or 0)):
        ci = f"{s['rate_ci95'][0]:.2f}-{s['rate_ci95'][1]:.2f}"
        cps = f"{s['cost_per_solved']:.4g}" if s["cost_per_solved"] is not None else "-"
        lines.append(f"{profile:<26}{s['n']:>4}{s['solved']:>8}{s['rate']:>7.2f}{ci:>14}{cps:>14}{s['wall_mean_s'] or 0:>9.0f}"
                     + ("  *pareto" if profile in report["pareto"] else ""))
    lines.append("recommended implementer per band:")
    for difficulty, t in report["tiers"].items():
        who = t["profile_id"] or t["status"]
        extra = f" (rate {t['rate']:.2f}, cost/solved {t['cost_per_solved']:.4g})" if t["profile_id"] else ""
        lines.append(f"  {difficulty:<7}-> {t['tier']:<24} {t['slot']:<20} {who}{extra}")
    lines.append("best escalation ladders (expected cost per task, success floor met):")
    for ladder in report["ladders"]:
        lines.append(f"  {' -> '.join(ladder['ladder']):<60} cost/task {ladder['cost_per_task']:.4g}  success {ladder['success_rate']:.2f}")
    lines += [f"WARNING: {w}" for w in report["warnings"]]
    return "\n".join(lines)


def rows_from_run(state: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Benchmark-shaped rows from a real run: one row per iteration, implementation cost only (execute + repair).

    `solved` = the iteration was accepted; the profile is the one that ran the first `execute` call.
    """
    out = []
    for it in state.get("iterations", []):
        mine = [r for r in records if r.get("iteration_id") == it["iteration_id"] and r.get("category") == "IMPLEMENTATION"]
        first = next((r for r in mine if r.get("executor") == "execute"), None)
        if not first:
            continue
        usd = [r["cost_usd"] for r in mine if r.get("cost_usd") is not None]
        out.append({"schema": ROW_SCHEMA, "source": "PRODUCTION", "task_id": it["iteration_id"],
                    "difficulty": COMPLEXITY_DIFFICULTY.get(str((it.get("plan") or {}).get("implementation_complexity") or "NORMAL"), "EASY"),
                    "profile_id": first.get("profile_id"), "repeat": 0,
                    "solved": it.get("status") in ("ACCEPTED", "PROVISIONAL"), "reason": it.get("outcome"),
                    "calls": len(mine), "proxy_units": round(sum(r.get("proxy_units") or 0 for r in mine), 1),
                    "cost_usd": round(sum(usd), 6) if usd and len(usd) == len(mine) else None,
                    "wall_s": round(sum(r.get("wall_s") or 0 for r in mine), 2), "model": first.get("model"),
                    "effort": first.get("effort"), "repairs": it.get("repair_attempts", 0)})
    return out


# -- CLI --------------------------------------------------------------------------------------------

def _load_rows(paths: Sequence[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    val = sub.add_parser("validate")
    val.add_argument("--tasks", nargs="*")
    run = sub.add_parser("run")
    run.add_argument("--profiles", required=True, help="comma separated profile IDs from IMPLEMENTER_PROFILES.json")
    run.add_argument("--tasks", nargs="*")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--results", type=Path, default=RESULTS)
    run.add_argument("--max-runs", type=int, default=100)
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--yes-spend", action="store_true", help="required: this calls real providers and uses quota")
    ana = sub.add_parser("analyze")
    ana.add_argument("files", nargs="+", type=Path)
    prod = sub.add_parser("production")
    prod.add_argument("run_ids", nargs="+")
    prod.add_argument("--stats-root", type=Path)
    for p in (ana, prod):
        p.add_argument("--basis", default="auto", choices=("auto", "usd", "proxy"))
        p.add_argument("--floor", type=float, default=0.8)
        p.add_argument("--ladder-floor", type=float, default=0.9)
        p.add_argument("--min-n", type=int, default=3)
        p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        for t in load_tasks():
            print(f"{t['id']:<30}{t['difficulty']:<8}{t['title']}")
        return 0
    if args.cmd == "validate":
        report = validate_tasks(args.tasks)
        for r in report:
            print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['id']:<30} fails_before={r['fails_before']} passes_with_solution={r['passes_with_solution']}")
            if r["detail"]:
                print("     ", json.dumps(r["detail"])[:600])
        return 0 if all(r["ok"] for r in report) else 1
    if args.cmd == "run":
        profiles = [p for p in args.profiles.split(",") if p]
        tasks = load_tasks(args.tasks)
        done = done_keys(args.results)
        plan = [(t, p, r) for r in range(1, args.repeats + 1) for t in tasks for p in profiles
                if (t["id"], p, r) not in done][:args.max_runs]
        print(f"{len(plan)} trials planned ({len(tasks)} tasks x {len(profiles)} profiles x {args.repeats} repeats, "
              f"{len(done)} already done). Typical implementation call: 0.2-0.5M input tokens (mostly cached), 2-8k output.")
        if not args.yes_spend:
            print("dry run: pass --yes-spend to call the providers (this uses subscription quota).")
            return 0
        args.results.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="aaw_bench_"))
        for index, (task, profile, repeat) in enumerate(plan, 1):
            row = run_one(task, profile, repeat, workroot=work, timeout=args.timeout)
            with args.results.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{index}/{len(plan)}] {task['id']:<28}{profile:<22}{'SOLVED' if row['solved'] else row['reason']:<20}"
                  f"cost {row['cost_usd'] if row['cost_usd'] is not None else row['proxy_units']}  {row['wall_s']}s")
        shutil.rmtree(work, ignore_errors=True)
        return 0
    if args.cmd == "analyze":
        rows = _load_rows(args.files)
    else:
        import aaw_telemetry as tel
        rows = []
        for run_id in args.run_ids:
            records, state = tel.run_records(run_id, args.stats_root, tel.load_pricing())
            rows.extend(rows_from_run(state, records))
    report = analyze(rows, basis=args.basis, floor=args.floor, ladder_floor=args.ladder_floor, min_n=args.min_n)
    print(json.dumps(report, indent=2) if args.json else render_analysis(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
