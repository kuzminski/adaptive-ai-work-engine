"""One bounded mixed scripted/live V0.3 runtime validation.

The script creates an isolated temporary Git repo and runs a single tiny
iteration. It makes real Codex CLI calls for Luna implementation, Luna review
pretreatment, Sol primary review, and Sol final review. The unavailable Claude
5.5 initial architect is preflighted and replaced only by a clearly recorded
scripted fixture for this validation; production has no substitution.

Running this script makes provider calls and may incur usage charges.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autonomy_adapters as aa
import autonomy_contract as ac
import autonomy_controller as ctl
from aaw_paths import AAW_ROOT
from test_autonomy_git_containment import build_fixture


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def provider_error(artifact: dict) -> str | None:
    if artifact.get("exit_code") in (None, 0):
        return None
    if artifact.get("stderr_tail"):
        return str(artifact["stderr_tail"])[-2000:]
    for line in (artifact.get("stdout_tail") or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("error", {}).get("message") if isinstance(event.get("error"), dict) else event.get("message")
        if isinstance(message, str):
            try:
                nested = json.loads(message)
                error = nested.get("error", {})
                if isinstance(error, dict):
                    return f"{error.get('code', 'provider_error')}: {error.get('message', message)}"
            except json.JSONDecodeError:
                return message[-2000:]
    return f"provider process exited rc={artifact.get('exit_code')}"


def charter(mandate: dict) -> dict:
    source = mandate["roadmap_mandate"]
    return {"mandate_hash": mandate["mandate_hash"], "objective": source["objective"],
            "roadmap_items": [{"item_id": item["item_id"], "title": item["title"],
                               "depends_on": list(item.get("depends_on", [])),
                               "human_required": item.get("human_required") is True}
                              for item in source["items"]],
            "acceptance_criteria": list(mandate["iteration_contract"]["acceptance_criteria"]),
            "boundaries": {"scope": list(mandate["iteration_contract"].get("scope", [])),
                           "constraints": list(mandate["iteration_contract"].get("constraints", [])),
                           "forbidden_changes": list(mandate["iteration_contract"].get("forbidden_changes", [])),
                           "allowed_areas": source["autonomy_bounds"].get("allowed_areas"),
                           "forbidden_areas": list(source["autonomy_bounds"].get("forbidden_areas", []))},
            "human_gate_conditions": ["ROADMAP_EXHAUSTED", "SCOPE_CHANGE", "ROLE_PROFILE_UNAVAILABLE",
                                      "PROMOTION_REQUIRES_HUMAN"], "risk_guidance": []}


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="aaw_v03_live_"))
    fixture = build_fixture(root / "workspace")
    wt, repo = fixture["wt"], fixture["repo"]
    (wt / "src").mkdir()
    (wt / "tests").mkdir()
    (wt / "src" / "__init__.py").write_text("", encoding="utf-8")
    (wt / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (wt / "src" / "greeting.py").write_text('def greeting():\n    return "hello"\n', encoding="utf-8")
    (wt / "tests" / "test_greeting.py").write_text(
        "import unittest\nfrom src.greeting import greeting\n\n"
        "class GreetingTest(unittest.TestCase):\n"
        "    def test_greeting(self):\n        self.assertEqual(greeting(), 'hello from AAW')\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n", encoding="utf-8")
    git(wt, "add", ".")
    git(wt, "commit", "-q", "-m", "add bounded greeting fixture")
    env = ctl.GitWorkspaceEnvironment(repo, wt)

    mandate = ac.validate_mandate({
        "mandate_id": "LIVE_V0_3_" + uuid.uuid4().hex[:8],
        "iteration_contract": {"goal": "Update the greeting function", "scope": ["src"],
            "acceptance_criteria": ["greeting() returns exactly 'hello from AAW'", "unit tests pass"],
            "constraints": ["standard library only"], "forbidden_changes": ["tests"],
            "required_evidence": ["unit tests"]},
        "roadmap_mandate": {"objective": "Make the greeting match the required text",
            "items": [{"item_id": "GREETING", "title": "Update greeting output"}],
            "autonomy_bounds": {"max_iterations": 1, "max_repair_attempts": 1,
                                "allowed_areas": ["src"], "forbidden_areas": ["secrets"]}}})

    def scripted_initial_architect(ctx: dict) -> dict:
        m = ctx["mandate"]
        return {"status": "ITERATION", "mandate_hash": m["mandate_hash"],
                "goal": "Return the required greeting text", "roadmap_refs": ["GREETING"],
                "scope_justification": "Completes the only frozen roadmap item",
                "acceptance_criteria": list(m["iteration_contract"]["acceptance_criteria"]),
                "touched_areas": ["src"], "decisions": [{"kind": "LOCAL_TECHNICAL", "summary": "update string"}],
                "skipped_items": [], "reason": None, "directional_charter": charter(m),
                "implementation_complexity": "NORMAL", "complexity_evidence": [],
                "semantic_verification_required": False, "semantic_verification_reason": None}

    # This fixture deliberately bypasses provider preflight only for its own
    # scripted architect response. DirectRoleExecutor never gets this bypass.
    scripted_initial_architect.preflight = lambda _binding: None

    roles = ac.load_roles(AAW_ROOT / "AUTONOMY_ROLES.json", AAW_ROOT / "IMPLEMENTER_PROFILES.json")
    executors = aa.build_direct_executors(timeout=300, max_turns=8)
    executors["plan"] = scripted_initial_architect
    run_id = "LIVE_V03_" + uuid.uuid4().hex[:12]
    stats_root = root / "stats"
    controller = ctl.AutonomyController.start(run_id, mandate, executors=executors, env=env,
                                               roles=roles, stats_root=stats_root)
    error = None
    state = None
    try:
        state = controller.run()
    except Exception as exc:  # retain any partial live evidence if a provider fails
        error = f"{type(exc).__name__}: {exc}"
        try:
            state = ctl.load_state(run_id, stats_root)
        except Exception:
            state = None

    lifecycle = controller.ledger.lifecycle()
    records = []
    live_executors = {"execute", "prepare_packet", "review", "final_review", "repair", "self_verify"}
    for ref in (state or {}).get("executions", []):
        execution_id = ref["execution_id"]
        artifact_path = ctl.autonomy_dir(run_id, stats_root) / "RESULTS" / f"{execution_id}.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8")) if artifact_path.is_file() else {}
        entry = lifecycle.get(execution_id, {})
        closed = entry.get("closed", [])
        records.append({"execution_id": execution_id, "iteration_id": ref.get("iteration_id"),
            "role": ref.get("role"), "executor": ref.get("executor"), "profile_id": ref.get("profile"),
            "runtime_model_id": ref.get("model"), "effort": ref.get("effort"),
            "selection": ref.get("selection"), "provider_session_id": ref.get("provider_session_id"),
            "live_provider_call": ref.get("executor") in live_executors and bool(entry.get("started")),
            "ledger_state": entry.get("state"), "started": bool(entry.get("started")),
            "close_reason": closed[-1]["payload"].get("close_reason") if closed else None,
            "usage": artifact.get("usage"), "cost_usd": artifact.get("total_cost_usd"),
            "provider_error": provider_error(artifact)})

    initial_profile = roles["policy_profiles"]["initial_planner"]
    _, initial_unavailable_reason = aa.resolve_runtime(initial_profile)
    result = {"schema_version": "AAW_DEFAULT_AUTONOMOUS_POLICY_V0.3_LIVE_EVIDENCE",
        "recorded_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
        "temporary_workspace": str(root), "git_repo": str(repo), "git_worktree": str(wt),
        "provider_roles_scheduled_for_live_validation": ["implementer", "review_prep", "reviewer", "final_reviewer"],
        "initial_architect": {"profile_id": initial_profile["profile_id"],
            "scripted_for_mixed_validation": True, "policy_invocation_count": (state or {}).get("planner_invocation_count"),
            "live_provider_invocations": 0, "preflight_unavailable_reason": initial_unavailable_reason},
        "status": (state or {}).get("status"), "hold": (state or {}).get("hold"),
        "escalation": (state or {}).get("escalation"), "controller_error": error,
        "changed_files": env.changed_files(), "executions": records,
        "provider_roles_actually_invoked": sorted({record["role"] for record in records
            if record["live_provider_call"]}),
        "live_provider_call_count": sum(record["live_provider_call"] for record in records),
        "total_cost_usd": (sum(record["cost_usd"] for record in records if record["cost_usd"] is not None)
                           if any(record["cost_usd"] is not None for record in records) else None),
        "cost_visibility": "not provided by the Codex CLI artifacts"}
    out = AAW_ROOT / "EVIDENCE" / (sys.argv[1] if len(sys.argv) > 1 else "AAW_AUTONOMOUS_POLICY_V0_3_LIVE.json")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"evidence_path": str(out), "status": result["status"], "controller_error": error,
                      "live_provider_call_count": result["live_provider_call_count"],
                      "executions": [{"execution_id": row["execution_id"], "role": row["role"],
                                      "profile_id": row["profile_id"], "provider_session_id": row["provider_session_id"],
                                      "close_reason": row["close_reason"]} for row in records]}, indent=2))


if __name__ == "__main__":
    main()
