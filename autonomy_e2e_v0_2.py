#!/usr/bin/env python3
"""AAW AUTONOMOUS ITERATIONS V0.2 — one bounded REAL autonomous iteration.

Builds a disposable Git fixture (canonical repo + isolated worktree + local bare
remote), runs the autonomy controller with the real direct-CLI role executors
against it, and writes an evidence summary. Validation only; it never touches
this repository's Git state.

    python autonomy_e2e_v0_2.py --work-dir <scratch dir> --evidence EVIDENCE/AAW_AUTONOMY_V0_2_E2E.json

Roles: the production AUTONOMY_ROLES.json is preflighted and its result is
recorded as-is. The run itself uses an explicit *validation* role config bound
to an inexpensive, currently runnable profile (default SONNET_HIGH for every
role, declared `allow_same_model_fresh_context: true`). No profile is chosen by
the controller; the config is part of the evidence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import autonomy_adapters as aa
import autonomy_contract as ac
import autonomy_controller as ctl
import autonomy_run_lock as rl
import execution_ledger as el
import workflow_runner as wr

ROOT = Path(__file__).resolve().parent


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def build_fixture(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=False)
    repo, wt, remote = root / "repo", root / "wt", root / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "AAW fixture")
    (repo / "calc.py").write_text('"""Tiny calculator."""\n\n\ndef add(a, b):\n    return a + b\n', encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\n\nimport calc\n\n\nclass AddTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "fixture base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "worktree", "add", "-q", "-b", "aaw/autonomy-e2e", str(wt))
    return {"repo": repo, "wt": wt, "remote": remote}


def mandate() -> dict:
    return {
        "mandate_id": "AAW_V0_2_E2E_MULTIPLY",
        "iteration_contract": {
            "goal": "Add a multiply(a, b) function to calc.py and a unit test for it in test_calc.py.",
            "scope": ["calc.py", "test_calc.py"],
            "acceptance_criteria": ["calc.multiply(a, b) returns a * b",
                                    "test_calc.py contains a passing unittest for multiply",
                                    "python -m unittest test_calc passes"],
            "constraints": ["standard library only", "do not change add()"],
            "forbidden_changes": ["any file other than calc.py and test_calc.py"],
            "required_evidence": ["python -m unittest test_calc output"]},
        "roadmap_mandate": {
            "objective": "calc.py supports multiplication with a test",
            "items": [{"item_id": "MUL", "title": "multiply() with unit test"}],
            "autonomy_bounds": {"max_iterations": 2, "max_repair_attempts": 1,
                                "allowed_areas": ["calc.py", "test_calc.py"], "forbidden_areas": [".git"]}}}


def validation_roles(profile_id: str) -> tuple[dict, dict]:
    config = {"contract": "AAW_AUTONOMOUS_ITERATIONS_V0.2", "purpose": "E2E validation binding (inexpensive profile)",
              "allow_same_model_fresh_context": True,
              "roles": {r: {"profile_id": profile_id} for r in (*ac.ROLES, *ac.OPTIONAL_ROLE_ALIASES)}}
    profiles = {p["profile_id"]: p for p in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
    return config, ac.validate_roles(config, profiles)


def collect(run_id: str, stats: Path, fx: dict[str, Path], before: dict, state: dict, config: dict,
            production_preflight: dict) -> dict:
    ledger = el.ExecutionLedger.for_run(run_id, stats)
    life = ledger.lifecycle()
    journal = ctl.AutonomyJournal(stats / run_id / "AUTONOMY" / "autonomy_events.jsonl", run_id).read()
    executions = []
    cost = 0.0
    for e in state["executions"]:
        entry = life.get(e["execution_id"]) or {}
        start = (entry.get("started") or [{}])[0].get("payload", {})
        close = (entry.get("closed") or [{}])[-1].get("payload", {})
        artifact = json.loads(Path(close["result_refs"][0]).read_text()) if close.get("result_refs") else {}
        cost += float((artifact.get("provider_meta") or {}).get("total_cost_usd") or 0)
        executions.append({
            "execution_id": e["execution_id"], "iteration_id": e["iteration_id"], "role": e["role"],
            "executor": e["executor"], "node_id": e["node_id"], "profile": e["profile"], "harness": e["harness"],
            "model": e["model"], "effort": e["effort"], "provider_session_id": e.get("provider_session_id"),
            "ledger": {"intent": bool(entry.get("intent")), "started": bool(entry.get("started")),
                       "closed": bool(entry.get("closed")), "state": entry.get("state"),
                       "start_evidence": start.get("start_evidence"), "process_id": start.get("process_id"),
                       "process_creation_time": start.get("process_creation_time"),
                       "close_reason": close.get("close_reason"), "exit_code": close.get("exit_code"),
                       "outcome": close.get("outcome")},
            "wall_time_s": artifact.get("wall_time_s"),
            "cost_usd": (artifact.get("provider_meta") or {}).get("total_cost_usd"),
            "num_turns": (artifact.get("provider_meta") or {}).get("num_turns"),
            "permission_denials": (artifact.get("provider_meta") or {}).get("permission_denials")})
    after = {"canonical_main": git(fx["repo"], "rev-parse", "main"),
             "remote_main": git(fx["repo"], "ls-remote", "origin", "refs/heads/main").split()[0],
             "remote_branches": git(fx["repo"], "ls-remote", "--heads", "origin").splitlines()}
    referenced = {ev["payload"].get("execution_id") for ev in journal if isinstance(ev.get("payload"), dict)}
    return {
        "contract": ac.CONTRACT_ID, "run_id": run_id, "final_status": state["status"], "final_phase": state["phase"],
        "hold": state["hold"], "escalation": state["escalation"],
        "production_roles_preflight": production_preflight, "validation_roles_config": config,
        "iterations": [{"iteration_id": i["iteration_id"], "index": i["index"], "outcome": i["outcome"],
                        "roadmap_refs": i["lineage"]["roadmap_refs"], "repair_attempts": i["repair_attempts"],
                        "checks": i["evidence_state"],
                        "review_verdicts": [r["verdict"] for r in i["reviews"]],
                        "final_review_verdicts": [r["verdict"] for r in i["final_reviews"]]}
                       for i in state["iterations"]],
        "executions": executions, "total_cost_usd": round(cost, 4),
        "distinct_provider_sessions": len({x["provider_session_id"] for x in executions if x["provider_session_id"]}),
        "ledger_summary": ledger.summary(),
        "ledger_validation": {k: v for k, v in el.validate_ledger(ledger.path, run_id).items() if k != "path"},
        "autonomy_events": [{"sequence": ev["sequence"], "event_type": ev["event_type"], "phase": ev["phase"],
                             "iteration_id": ev["iteration_id"],
                             "execution_id": (ev.get("payload") or {}).get("execution_id")} for ev in journal],
        "autonomy_events_reference_every_execution": all(x["execution_id"] in referenced for x in executions),
        "run_lock": {"acquired": [ev["payload"].get("controller_lock") for ev in journal
                                  if ev["event_type"] in ("MANDATE_FROZEN", "RUN_RESUMED")],
                     "lock_file_present_after_run": rl.lock_path(stats / run_id / "AUTONOMY").exists()},
        "git": {"before": before, "after": after,
                "merged_into_main": after["canonical_main"] != before["canonical_main"],
                "pushed_to_remote_main": after["remote_main"] != before["remote_main"],
                "worktree_changed_files": sorted(set(
                    git(fx["wt"], "diff", "--name-only", before["canonical_main"]).splitlines()
                    + git(fx["wt"], "ls-files", "--others", "--exclude-standard").splitlines())),
                "worktree_branch": git(fx["wt"], "rev-parse", "--abbrev-ref", "HEAD")},
        "worktree_unittest": subprocess.run([sys.executable, "-m", "unittest", "-q", "test_calc"], cwd=fx["wt"],
                                            capture_output=True, text=True).stderr.strip().splitlines()[-1:],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--profile", default="SONNET_HIGH")
    p.add_argument("--timeout", type=int, default=900)
    a = p.parse_args(argv)
    production = ac.load_roles(ROOT / "AUTONOMY_ROLES.json", ROOT / "IMPLEMENTER_PROFILES.json")
    production_preflight = aa.preflight_roles(production)
    config, roles = validation_roles(a.profile)
    preflight = aa.preflight_roles(roles)
    if not preflight["all_available"]:
        print(json.dumps({"status": "BLOCKED", "preflight": preflight}, indent=2))
        return 2
    fx = build_fixture(a.work_dir / "fixture")
    before = {"canonical_main": git(fx["repo"], "rev-parse", "main"),
              "remote_main": git(fx["repo"], "ls-remote", "origin", "refs/heads/main").split()[0]}
    env = ctl.GitWorkspaceEnvironment(fx["repo"], fx["wt"], remote_check=True)
    run_id = wr.run_id()
    stats = a.work_dir / "stats"
    controller = ctl.AutonomyController.start(run_id, mandate(), executors=aa.build_direct_executors(timeout=a.timeout),
                                              env=env, roles=roles, stats_root=stats)
    state = controller.run()
    evidence = collect(run_id, stats, fx, before, state, config, production_preflight)
    evidence["stats_root"], evidence["fixture"] = str(stats), {k: str(v) for k, v in fx.items()}
    a.evidence.parent.mkdir(parents=True, exist_ok=True)
    a.evidence.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("run_id", "final_status", "total_cost_usd", "iterations")}, indent=2))
    return 0 if state["status"] == ac.AWAITING_HUMAN and (state["hold"] or {}).get("promotable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
