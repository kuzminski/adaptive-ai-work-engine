#!/usr/bin/env python3
"""Strict dependency-free schema for AAW V0.3 static Custom Jobs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


JOB_TYPES = {"SINGLE_IMPLEMENTATION", "MULTI_STAGE", "MULTI_SUBTASK"}
NODE_TYPES = {"PLAN", "IMPLEMENT", "SUBTASK", "REVIEW", "REPAIR", "DELTA_REVIEW"}
REPAIR_SELECTION = {"HUMAN_SELECTED", "AUTO_REPAIR_ALL_BLOCKING"}
PREPROCESS_POLICIES = {"AUTO_SAFE", "OFF", "CUSTOM", "MANUAL"}


class JobValidationError(ValueError):
    pass


def require(value: bool, message: str) -> None:
    if not value:
        raise JobValidationError(message)


def load_job(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobValidationError(f"cannot read job: {exc}") from exc
    return validate_job(value)


def _validate_gate(gate: Any, label: str) -> None:
    require(isinstance(gate, Mapping), f"{label} must be an object")
    command = gate.get("command")
    require(isinstance(command, list) and command and all(isinstance(x, str) and x for x in command), f"{label}.command must be a non-empty argv array")
    require(type(gate.get("timeout_seconds")) is int and gate["timeout_seconds"] > 0, f"{label}.timeout_seconds must be positive")


def _profile(value: Any, label: str) -> None:
    require(isinstance(value, str) and bool(value.strip()), f"{label} must be a profile ID")


def validate_job(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict), "job must be a JSON object")
    required = {"schema_version", "job_id", "job_type", "goal", "repository", "worktree", "worktree_policy", "execution_adapter", "plan", "machine_gates", "review", "repair", "delta_review", "limits", "binding_source"}
    require(not (required - set(value)), f"missing job fields: {sorted(required - set(value))}")
    require(value["schema_version"] == "AAW_CUSTOM_JOB_V0.3", "unsupported schema_version")
    require(isinstance(value["job_id"], str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value["job_id"]) is not None, "job_id must be filesystem-safe")
    require(value["job_type"] in JOB_TYPES, f"unknown job_type: {value['job_type']}")
    for key in ("goal", "repository", "worktree"):
        require(isinstance(value[key], str) and bool(value[key].strip()), f"{key} is required")
    policy = value["worktree_policy"]
    require(isinstance(policy, Mapping), "worktree_policy must be an object")
    require(policy.get("isolated_worktree_required") is True, "isolated_worktree_required must be true")
    require(policy.get("checkpoint_commits") is True, "checkpoint_commits must be true")
    require(policy.get("main_merge_allowed") is False and policy.get("push_allowed") is False, "merge and push must be false")
    require(value["execution_adapter"] in {"DIRECT_CLI_CONTROL", "ORCA_SUPERVISED"}, "invalid execution_adapter")
    require(value["binding_source"] in {"HUMAN_OVERRIDE", "WORKFLOW_DEFAULT", "ADAPTIVE_POLICY"}, "invalid binding_source")
    if "preprocess_policy" in value:
        require(value["preprocess_policy"] in PREPROCESS_POLICIES, "invalid preprocess_policy")
    preprocess = value.get("preprocess", {})
    require(isinstance(preprocess, Mapping), "preprocess must be an object")
    for node_id, spec in preprocess.items():
        require(isinstance(node_id, str) and node_id, "preprocess node key must be non-empty")
        require(isinstance(spec, Mapping), f"preprocess.{node_id} must be an object")
        require(isinstance(spec.get("type"), str) and bool(spec["type"]), f"preprocess.{node_id}.type required")
        require(isinstance(spec.get("profile"), str) and bool(spec["profile"]), f"preprocess.{node_id}.profile required")
        require(type(spec.get("required", False)) is bool, f"preprocess.{node_id}.required must be boolean")
        require(isinstance(spec.get("reason", "MANUAL_USER_SELECTION"), str), f"preprocess.{node_id}.reason must be a string")

    plan = value["plan"]
    require(isinstance(plan, Mapping) and isinstance(plan.get("enabled"), bool), "plan must declare enabled")
    if plan["enabled"]:
        _profile(plan.get("profile_id"), "plan.profile_id")
        require(plan.get("approval") in {"HUMAN_APPROVAL", "AUTO_ACCEPT_BOUNDED"}, "invalid plan approval")
        if plan.get("approval") == "AUTO_ACCEPT_BOUNDED":
            require(plan.get("scope_expansion_allowed") is False, "auto-accepted plan cannot expand scope")

    gates = value["machine_gates"]
    require(isinstance(gates, Mapping), "machine_gates must be an object")
    for index, gate in enumerate(gates.get("final", [])):
        _validate_gate(gate, f"machine_gates.final[{index}]")

    for key in ("review", "repair", "delta_review"):
        require(isinstance(value[key], Mapping), f"{key} must be an object")
        _profile(value[key].get("profile_id"), f"{key}.profile_id")
    require(value["repair"].get("selection_mode") in REPAIR_SELECTION, "invalid repair.selection_mode")
    require(type(value["repair"].get("max_cycles")) is int and value["repair"]["max_cycles"] >= 0, "repair.max_cycles must be non-negative")

    limits = value["limits"]
    require(isinstance(limits, Mapping), "limits must be an object")
    for key in ("max_subtasks", "max_llm_calls", "max_wall_time_minutes"):
        require(type(limits.get(key)) is int and limits[key] > 0, f"limits.{key} must be positive")

    if value["job_type"] == "SINGLE_IMPLEMENTATION":
        _profile(value.get("implement_profile_id"), "implement_profile_id")
    elif value["job_type"] == "MULTI_SUBTASK":
        subtasks = value.get("subtasks")
        require(isinstance(subtasks, list) and subtasks, "MULTI_SUBTASK needs subtasks")
        require(len(subtasks) <= limits["max_subtasks"], "subtask count exceeds limit")
        ids: set[str] = set()
        for index, task in enumerate(subtasks):
            require(isinstance(task, Mapping), f"subtasks[{index}] must be an object")
            task_id = task.get("subtask_id")
            require(isinstance(task_id, str) and task_id and task_id not in ids, f"subtasks[{index}] needs a unique subtask_id")
            ids.add(task_id)
            require(isinstance(task.get("title"), str) and bool(task["title"].strip()), f"{task_id}: title required")
            require(isinstance(task.get("instructions"), str) and bool(task["instructions"].strip()), f"{task_id}: instructions required")
            _profile(task.get("profile_id"), f"{task_id}.profile_id")
            for gate_index, gate in enumerate(task.get("machine_gates", [])):
                _validate_gate(gate, f"{task_id}.machine_gates[{gate_index}]")
    else:
        stages = value.get("stages")
        require(isinstance(stages, list) and stages, "MULTI_STAGE needs stages")
        for index, stage in enumerate(stages):
            require(isinstance(stage, Mapping), f"stages[{index}] must be an object")
            require(stage.get("stage_type") in {"SINGLE_TASK", "WORKFLOW", "CUSTOM_JOB"}, f"stages[{index}]: invalid stage_type")
            require(isinstance(stage.get("spec_path"), str) and bool(stage["spec_path"]), f"stages[{index}]: spec_path required")
    return dict(value)


def self_test() -> int:
    template = {
        "schema_version":"AAW_CUSTOM_JOB_V0.3","job_id":"test","job_type":"SINGLE_IMPLEMENTATION","goal":"x","repository":"D:/repo","worktree":"D:/wt",
        "worktree_policy":{"isolated_worktree_required":True,"checkpoint_commits":True,"main_merge_allowed":False,"push_allowed":False},
        "execution_adapter":"DIRECT_CLI_CONTROL","binding_source":"HUMAN_OVERRIDE","plan":{"enabled":False},"implement_profile_id":"TERRA_HIGH",
        "machine_gates":{"final":[]},"review":{"profile_id":"SOL_HIGH"},"repair":{"profile_id":"LUNA_HIGH","selection_mode":"HUMAN_SELECTED","max_cycles":1},
        "delta_review":{"profile_id":"LUNA_HIGH"},"limits":{"max_subtasks":10,"max_llm_calls":10,"max_wall_time_minutes":30}
    }
    template["preprocess_policy"] = "AUTO_SAFE"; template["preprocess"] = {"REVIEW": {"type": "LOCAL_QWEN_DIFF_TRIAGE", "profile": "LOCAL_QWEN_DIFF_TRIAGE", "required": False, "reason": "MULTI_COMMIT_REVIEW"}}
    validate_job(template)
    broken = json.loads(json.dumps(template)); broken["worktree_policy"]["main_merge_allowed"] = True
    try:
        validate_job(broken)
    except JobValidationError:
        pass
    else:
        raise AssertionError("merge-capable job accepted")
    print(json.dumps({"status":"PASS","tests":2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
