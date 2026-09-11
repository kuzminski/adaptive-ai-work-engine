import json
import subprocess
import sys
from pathlib import Path

import workflow_runner as runner
from execution_contract import update_execution


def _run(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_workflow_run_node_review_gate_and_candidate_identity(tmp_path, monkeypatch):
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "aaw@example.invalid"], repo)
    _run(["git", "config", "user.name", "AAW Test"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _run(["git", "add", "."], repo); _run(["git", "commit", "-m", "baseline"], repo)
    _run(["git", "worktree", "add", "-b", "aaw/test", str(worktree)], repo)

    workflow = {
        "workflow_id":"IDENTITY_E2E", "version":"0.4A", "description":"identity fixture", "goal":None, "start_node":"N01",
        "workspace_policy":{"isolated_worktree_required":True,"main_merge_allowed":False},
        "limits":{"max_nodes":4,"max_repair_cycles":1,"max_wall_time_minutes":5,"max_llm_calls":3,"max_token_budget":None},
        "nodes":[
            {"id":"N01","type":"IMPLEMENT","depends_on":[],"run_if":"ALWAYS","role":"CODE_IMPLEMENTER","capability":"CODE_IMPLEMENTER","model":"gpt-5.6-terra","effort":"high","instructions":"fixture","acceptance":["fixture"],"on_pass":"N02","on_fail":"STOP"},
            {"id":"N02","type":"MACHINE_GATE","depends_on":["N01"],"run_if":"ON_TRANSITION","role":None,"model":None,"effort":None,"instructions":"fixture","acceptance":["exit zero"],"command":[sys.executable,"-c","raise SystemExit(0)"],"timeout_seconds":30,"on_pass":"N03","on_fail":"STOP"},
            {"id":"N03","type":"REVIEW","depends_on":["N02"],"run_if":"ON_TRANSITION","role":"INDEPENDENT_REVIEWER","model":"gpt-5.6-sol","effort":"high","instructions":"fixture","acceptance":["fixture"],"on_pass":"N05","on_fail":"STOP"},
            {"id":"N05","type":"HUMAN_GATE","depends_on":["N03"],"run_if":"ON_TRANSITION","role":None,"model":None,"effort":None,"instructions":"fixture","acceptance":["human"],"on_pass":"STOP","on_fail":"STOP"},
        ],
    }
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    monkeypatch.setattr(runner, "STATS_ROOT", tmp_path / "03_STATS")

    def fake_llm(workflow, state, node, worktree, execution, execution_path, recorder=None):
        update_execution(execution_path, execution["execution_id"], provider_session_id="same-session", status="COMPLETED")
        result = {"execution_id":execution["execution_id"],"node_id":node["id"],"node_type":node["type"],"outcome":"PASS","summary":"fixture","changed_files":[],"tests":[],"findings":[],"remaining_uncertainty":[],"recommended_next_action":"continue","provider_session_id":"same-session"}
        if node["type"] == "REVIEW":
            result["reviewed_execution_ids"] = list(execution["relations"]["reviewed_execution_ids"])
        telemetry = {"schema_version":"1.1","execution_id":execution["execution_id"],"run_id":state["AAW_RUN_ID"],"node":node["id"],"workflow_node_id":node["id"],"workflow_node_type":node["type"],"harness":"fixture","model":"fixture","effort":"high","provider_session_id":"same-session","wall_time_s":0.0,"usage":{},"outcome":"VALID PASS"}
        return result, telemetry

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)
    state = runner.execute(workflow_path, "identity fixture", repo, worktree, preprocess_policy="OFF")
    assert state["status"] == "WAITING_FOR_HUMAN"
    assert len(state["executions"]) == 3
    assert len({row["execution_id"] for row in state["executions"]}) == 3
    assert {row["invocation_kind"] for row in state["executions"]} == {"LLM", "MACHINE_GATE", "REVIEW"}
    implementation = next(row for row in state["completed_nodes"] if row["node_type"] == "IMPLEMENT")
    review = next(row for row in state["node_results"] if row["node_type"] == "REVIEW")
    assert review["reviewed_execution_ids"] == [implementation["execution_id"]]
    assert state["candidate"]["review_execution_ids"] == [review["execution_id"]]
    assert state["candidate"]["check_execution_ids"] == [next(row["execution_id"] for row in state["completed_nodes"] if row["node_type"] == "MACHINE_GATE")]
    decided = runner.apply_human_verdict(state["AAW_RUN_ID"], "leave-for-later")
    assert decided["human_decisions"][-1]["candidate_id"] == state["candidate"]["candidate_id"]
