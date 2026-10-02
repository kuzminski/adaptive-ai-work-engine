#!/usr/bin/env python3
"""Deterministic stand-in for the `claude` and `codex` CLIs — product tests only.

Spawned through the *real* `autonomy_adapters.DirectRoleExecutor` path, so a
product test exercises the real argv, stdin handoff, spawn receipt, ledger
lifecycle, result parsing, run lock and resume. Only the model is scripted.
Never used by production code.

    python product_fake_cli.py --as claude|codex [cli args...]

Environment:
  AAW_FAKE_SCENARIO   JSON file {ROLE: [step, ...]}; ROLE as in the AAW role
                      prompt (PLANNER, IMPLEMENTER, SELF-VERIFIER,
                      REVIEW-PRETREATMENT, REVIEWER, FINAL REVIEWER, REPAIRER).
                      Missing roles use built-in "happy path" answers.
  AAW_FAKE_LOGGED_IN  "0" → login status reports not logged in (default "1")
  AAW_FAKE_VERSION    version string (default 9.9.9)

Step keys: output (dict, with $MANDATE_HASH / $ITERATION_CRITERIA / $CHARTER /
$CHARTER_HASH / $NEXT_ITEM substitution), write_files, exit_code, sleep.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROLES = ("FINAL REVIEWER", "SELF-VERIFIER", "REVIEW-PRETREATMENT", "PLANNER", "IMPLEMENTER", "REVIEWER", "REPAIRER")


def charter(mandate: dict) -> dict:
    source = mandate["roadmap_mandate"]
    return {"mandate_hash": mandate["mandate_hash"], "objective": source["objective"],
            "roadmap_items": [{"item_id": i["item_id"], "title": i["title"], "depends_on": list(i.get("depends_on", [])),
                               "human_required": i.get("human_required") is True} for i in source["items"]],
            "acceptance_criteria": list(mandate["iteration_contract"]["acceptance_criteria"]),
            "boundaries": {"scope": list(mandate["iteration_contract"].get("scope", [])),
                           "constraints": list(mandate["iteration_contract"].get("constraints", [])),
                           "forbidden_changes": list(mandate["iteration_contract"].get("forbidden_changes", [])),
                           "allowed_areas": source["autonomy_bounds"].get("allowed_areas"),
                           "forbidden_areas": list(source["autonomy_bounds"].get("forbidden_areas", []))},
            "human_gate_conditions": ["ROADMAP_EXHAUSTED", "SCOPE_CHANGE", "ROLE_PROFILE_UNAVAILABLE",
                                      "PROMOTION_REQUIRES_HUMAN"], "risk_guidance": []}


def next_item(handoff: dict) -> str | None:
    roadmap = handoff.get("ROADMAP_STATUS") or {}
    for item_id, row in roadmap.items():
        if row.get("status") == "PENDING" and row.get("dependency_state") == "READY":
            return item_id
    return None


def default_step(role: str, handoff: dict) -> dict:
    if role == "PLANNER":
        initial = handoff.get("PLANNING_STAGE") == "INITIAL_ARCHITECT"
        item = next_item(handoff)
        out = {"status": "ITERATION", "mandate_hash": "$MANDATE_HASH",
               "goal": f"Deliver {item or 'the first iteration'}", "roadmap_refs": [item] if item else [],
               "scope_justification": "Next ready roadmap item of the frozen charter",
               "acceptance_criteria": ["$ITERATION_CRITERIA"] if initial else [f"{item} is implemented"],
               "touched_areas": ["src"], "decisions": [{"kind": "LOCAL_TECHNICAL", "summary": "small change"}],
               "skipped_items": [], "reason": None, "implementation_complexity": "NORMAL",
               "complexity_evidence": [], "semantic_verification_required": False,
               "semantic_verification_reason": None}
        if initial:
            out["directional_charter"] = "$CHARTER"
            out["directional_charter_hash"] = None
        else:
            out["directional_charter"] = None
            out["directional_charter_hash"] = "$CHARTER_HASH"
        return {"output": out}
    if role == "IMPLEMENTER":
        name = f"src/step_{(handoff.get('ITERATION_ID') or 'x')[-6:]}.py"
        return {"write_files": {name: "VALUE = 1\n"},
                "output": {"summary": f"Implemented the plan in {name}", "changed_files": [name],
                           "checks": [{"name": "unit tests", "status": "PASS",
                                       "summary": "python -m unittest: exit 0, 1 test OK"}],
                           "deviations": [], "uncertainties": []}}
    if role == "REPAIRER":
        return {"output": {"summary": "Addressed the findings", "addressed_findings": [], "changed_files": [],
                           "checks": [], "uncertainties": []}}
    if role == "SELF-VERIFIER":
        return {"output": {"summary": "verified", "checks": [{"name": "semantic", "status": "PASS", "summary": "ok"}]}}
    if role == "REVIEW-PRETREATMENT":
        return {"output": {"summary": "organized evidence", "implementation_claims": [], "check_refs": [],
                           "finding_refs": [], "changed_files": [], "source_refs": []}}
    return {"output": {"verdict": "PASS", "summary": "Change matches the plan; checks recorded.", "findings": [],
                       "raw_evidence_requests": [], "uncertainties": []}}


def substitute(value, handoff):
    mandate = handoff.get("MANDATE") or handoff.get("FROZEN_MANDATE") or {}
    if isinstance(value, str):
        if value == "$MANDATE_HASH":
            return mandate.get("mandate_hash")
        if value == "$CHARTER":
            return charter(mandate)
        if value == "$CHARTER_HASH":
            return handoff.get("FROZEN_DIRECTIONAL_CHARTER_HASH")
        if value == "$NEXT_ITEM":
            return next_item(handoff)
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            if item == "$ITERATION_CRITERIA":
                out.extend((handoff.get("ITERATION_CONTRACT") or {}).get("acceptance_criteria") or [])
            else:
                out.append(substitute(item, handoff))
        return out
    if isinstance(value, dict):
        return {k: substitute(v, handoff) for k, v in value.items()}
    return value


def main(argv: list[str]) -> int:
    harness = "claude"
    if argv[:1] == ["--as"]:
        harness, argv = argv[1], argv[2:]
    if argv[:1] == ["--version"]:
        print(f"{os.environ.get('AAW_FAKE_VERSION', '9.9.9')} (fake {harness})")
        return 0
    logged_in = os.environ.get("AAW_FAKE_LOGGED_IN", "1") == "1"
    if argv[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": logged_in, "authMethod": "fake"}))
        return 0 if logged_in else 1
    if argv[:2] == ["login", "status"]:
        if logged_in:
            print("Logged in using fake account")
            return 0
        print("Not logged in", file=sys.stderr)
        return 1
    prompt = sys.stdin.read()
    role = next((r for r in ROLES if f"You are the AAW {r}" in prompt or f"independent AAW {r}" in prompt), "UNKNOWN")
    handoff = json.loads(prompt.split("HANDOFF:\n", 1)[1]) if "HANDOFF:\n" in prompt else {}
    scenario_path = Path(os.environ.get("AAW_FAKE_SCENARIO") or "")
    scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.is_file() else {}
    calls_path = Path(os.environ.get("AAW_FAKE_CALLS") or (str(scenario_path) + ".calls.jsonl"
                                                          if scenario_path.is_file() else os.devnull))
    previous = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()] \
        if calls_path.is_file() else []
    count = sum(1 for c in previous if c["role"] == role)
    steps = scenario.get(role)
    step = steps[min(count, len(steps) - 1)] if steps else default_step(role, handoff)
    session = str(uuid.uuid4())
    if calls_path.name != os.devnull and str(calls_path) != os.devnull:
        with calls_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": role, "harness": harness, "argv": argv, "cwd": os.getcwd(),
                                     "pid": os.getpid(), "execution_id": handoff.get("EXECUTION_ID"),
                                     "iteration_id": handoff.get("ITERATION_ID")}) + "\n")
    if step.get("sleep"):
        time.sleep(float(step["sleep"]))
    for rel, content in (step.get("write_files") or {}).items():
        target = Path(os.getcwd()) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    code = int(step.get("exit_code", 0))
    if code:
        sys.stderr.write("fake provider failure\n")
        return code
    output = substitute(step.get("output") or {}, handoff)
    if harness == "codex":
        final = argv[argv.index("--output-last-message") + 1] if "--output-last-message" in argv else None
        if final:
            Path(final).write_text(json.dumps(output), encoding="utf-8")
        print(json.dumps({"type": "thread.started", "thread_id": session}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
        return 0
    sys.stdout.write(json.dumps({"session_id": session, "usage": {"input_tokens": 1, "output_tokens": 1},
                                 "total_cost_usd": 0.0, "num_turns": 1, "is_error": False,
                                 "structured_output": output}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
