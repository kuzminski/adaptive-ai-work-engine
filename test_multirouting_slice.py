"""Vertical slice: AAW MULTIROUTING RUNTIME CONTRACT V0.1 on the real runner.

These drive `workflow_runner.execute` itself — real git worktrees, real
workspace guards, real MACHINE_GATE subprocesses, real state and artifact
writes. Only `execute_llm_node` is substituted, at the adapter boundary the
module already documents, so provider results are scriptable. That is the same
convention `test_workflow_execution_identity.py` uses.

Covered acceptance criteria:
  1 PASS routing                       test_pass_routes_forward_and_fans_out
  2 REPAIR lineage + brief forwarding  test_repair_creates_an_explicit_branch_lineage
  3 BLOCKED starts no downstream       test_blocked_reaches_the_human_gate_and_nothing_else
  4 ALL_MATCHES fan-out                test_pass_routes_forward_and_fans_out
  5 deterministic NO_ROUTE             test_no_matching_edge_is_fail_closed
  6 identical output, identical route  test_identical_structured_output_routes_identically
  7 evidence names both paths          test_evidence_names_the_path_not_taken
  8 no second engine                   test_legacy_workflow_uses_the_same_router
"""

import json
import subprocess
import sys
from pathlib import Path

import routing_contract as rc
import workflow_runner as runner
from execution_contract import update_execution
from workflow_schema import validate_node_result

WORKFLOWS = Path(__file__).with_name("WORKFLOWS")
SLICE = WORKFLOWS / "MULTIROUTING_SLICE_V1.json"
LEGACY = WORKFLOWS / "IMPLEMENT_REVIEW_REPAIR_V1.json"

REVIEW_BRIEF = (
    "Bind rotation to device_id on the mobile deep-link exchange path. "
    "Reject a refresh whose device_id does not match the family."
)
CARRY = ["scope: auth/session/ only", "no merge to canonical"]


# ───────────────────────────── fixtures ─────────────────────────────

def _git(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _workspace(root):
    root.mkdir(parents=True, exist_ok=True)
    repo, worktree = root / "repo", root / "worktree"
    repo.mkdir()
    _git(["git", "init", "-b", "main"], repo)
    _git(["git", "config", "user.email", "aaw@example.invalid"], repo)
    _git(["git", "config", "user.name", "AAW Test"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-m", "baseline"], repo)
    _git(["git", "worktree", "add", "-b", "aaw/slice", str(worktree)], repo)
    return repo, worktree


def _machine_command(exit_code=0, sleep=None):
    if sleep is not None:
        return [sys.executable, "-c", f"import time; time.sleep({sleep})"]
    return [sys.executable, "-c", f"raise SystemExit({exit_code})"]


def _workflow_copy(source, destination, *, gate_node, exit_code=0, sleep=None, timeout_seconds=300):
    """Use the committed workflow verbatim, substituting only the gate command."""
    data = json.loads(source.read_text(encoding="utf-8"))
    gate = next(node for node in data["nodes"] if node["id"] == gate_node)
    gate["command"] = _machine_command(exit_code, sleep)
    gate["timeout_seconds"] = timeout_seconds
    destination.write_text(json.dumps(data), encoding="utf-8")
    return destination


def _install_fake_llm(monkeypatch, scripts, captured=None):
    def fake_llm(workflow, state, node, worktree, execution, execution_path, recorder=None):
        node_id = str(node["id"])
        if captured is not None:
            captured[node_id] = {
                "instructions": node["instructions"],
                "package": runner.node_package(workflow, state, node, worktree),
                "lineage": node.get("lineage"),
                "binding": dict(state["workflow_bindings"].get(node_id, {})),
            }
        update_execution(execution_path, execution["execution_id"], status="COMPLETED")
        lineage = node.get("lineage") or {}
        script = scripts.get(node_id) or scripts.get(lineage.get("template_id")) or {}
        result = {
            "execution_id": execution["execution_id"], "node_id": node_id, "node_type": node["type"],
            "outcome": script.get("outcome", "PASS"), "summary": script.get("summary", f"{node_id} fixture"),
            "changed_files": [], "tests": [], "findings": script.get("findings", []),
            "remaining_uncertainty": [], "recommended_next_action": "the gate decides",
        }
        for key in ("verdict", "next_brief", "carry_forward", "artifacts"):
            if key in script:
                result[key] = script[key]
        # The adapter validates before returning; keep that true here so the
        # slice really exercises the extended result contract.
        validate_node_result(result, node_id, node["type"])
        telemetry = {
            "schema_version": "1.1", "execution_id": execution["execution_id"],
            "run_id": state["AAW_RUN_ID"], "node": node_id, "workflow_node_id": node_id,
            "workflow_node_type": node["type"], "harness": "fixture", "model": "fixture",
            "effort": "medium", "provider_session_id": "fixture", "wall_time_s": 0.0, "usage": {},
        }
        return result, telemetry

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)


def _run_slice(root, monkeypatch, scripts, captured=None, *, source=SLICE, gate_node="N04", **gate_kwargs):
    repo, worktree = _workspace(root)
    workflow_path = _workflow_copy(source, root / "workflow.json", gate_node=gate_node, **gate_kwargs)
    monkeypatch.setattr(runner, "STATS_ROOT", root / "03_STATS")
    _install_fake_llm(monkeypatch, scripts, captured)
    state = runner.execute(workflow_path, "multirouting slice", repo, worktree, preprocess_policy="OFF")
    journal = rc.RoutingJournal(Path(state["routing"]["journal_path"]), run_id=state["AAW_RUN_ID"])
    return state, journal


def _executed(state):
    return [row["node_id"] for row in state["completed_nodes"]]


def _decision(state, node_id):
    return next(row for row in state["routing"]["decisions"] if row["node_id"] == node_id)


# ───────────────────────── 1 + 4: PASS and fan-out ─────────────────────────

def test_pass_routes_forward_and_fans_out(tmp_path, monkeypatch):
    scripts = {"N03": {"outcome": "PASS", "verdict": "PASS", "carry_forward": CARRY}}
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, exit_code=0)

    assert state["status"] == "WAITING_FOR_HUMAN"
    # deterministic order: declaration order of the fan-out edges, FIFO frontier
    assert _executed(state) == ["N01", "N02", "N03", "N04", "N05", "N06", "N09"]

    review = _decision(state, "N03")
    assert review["selected"] == ["E_N03_PASS"]
    assert review["verdict"] == "PASS"

    # ALL_MATCHES really selected two edges, and both downstream nodes ran
    gate = _decision(state, "N04")
    assert gate["routing_mode"] == "ALL_MATCHES"
    assert gate["selected"] == ["E_N04_HARDENING", "E_N04_MIGRATION"]
    assert [row["hold_reason"] for row in gate["held"]] == [rc.HELD_NOT_MATCHED]

    # N05 and N06 both point at N09; it is entered once, and the duplicate is
    # recorded rather than silently dropped.
    assert _executed(state).count("N09") == 1
    assert [row["reason"] for row in state["routing"]["dedup"]] == ["ALREADY_QUEUED"]
    assert state["routing"]["dedup"][0]["edge_id"] == "E_N06_CONTINUE"

    selected = [row["payload"]["edge_id"] for row in journal.by_type(rc.EDGE_SELECTED)]
    assert selected == ["E_N01_CONTINUE", "E_N02_CONTINUE", "E_N03_PASS",
                        "E_N04_HARDENING", "E_N04_MIGRATION", "E_N05_CONTINUE", "E_N06_CONTINUE"]


# ───────────────────────── 2: explicit repair lineage ─────────────────────────

def test_repair_creates_an_explicit_branch_lineage(tmp_path, monkeypatch):
    captured = {}
    scripts = {
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": REVIEW_BRIEF,
                "carry_forward": CARRY, "artifacts": ["N03_review.json"],
                "findings": [{"severity": "HIGH", "file": "auth/session/refresh.py",
                              "location": "L42", "description": "device_id dropped on rotate",
                              "required_fix": "rebind device_id"}]},
        "N03A": {"outcome": "PASS", "summary": "replay window closed"},
    }
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, captured, exit_code=0)

    assert state["status"] == "WAITING_FOR_HUMAN"
    # A new node id, not a second pass over N03. The template is never entered.
    assert _executed(state) == ["N01", "N02", "N03", "N03A", "N09"]
    assert "N03R" not in _executed(state)
    assert _executed(state).count("N03") == 1

    lineage = state["routing"]["lineage"]["N03A"]
    assert lineage["origin_node_id"] == "N03"
    assert lineage["template_id"] == "N03R"
    assert lineage["origin_verdict"] == "REPAIR"
    assert lineage["selected_edge_id"] == "E_N03_REPAIR"
    assert lineage["branch_index"] == 1
    assert lineage["inherited_brief"] == REVIEW_BRIEF

    # The reviewer's brief actually reached the repair node, both as the
    # instructions the adapter sends and as a structured package field.
    assert REVIEW_BRIEF in captured["N03A"]["instructions"]
    assert captured["N03A"]["package"]["INHERITED_BRIEF"] == REVIEW_BRIEF
    assert captured["N03A"]["package"]["CARRY_FORWARD"] == CARRY
    assert captured["N03A"]["package"]["UPSTREAM_ARTIFACTS"] == ["N03_review.json"]
    assert captured["N03A"]["package"]["BRANCH_LINEAGE"]["origin_node_id"] == "N03"

    # The branch inherits the template's frozen binding, and says so.
    assert captured["N03A"]["binding"]["binding_source"] == "INHERITED_FROM_TEMPLATE"
    assert captured["N03A"]["binding"]["binding_template_id"] == "N03R"

    # The PASS path is not taken and not erased.
    review = _decision(state, "N03")
    assert review["selected"] == ["E_N03_REPAIR"]
    assert {row["edge_id"]: row["hold_reason"] for row in review["held"]} == {
        "E_N03_PASS": rc.HELD_NOT_MATCHED, "E_N03_BLOCKED": rc.HELD_NOT_MATCHED}
    assert not {"N04", "N05", "N06"} & set(_executed(state))

    branch_events = journal.by_type(rc.BRANCH_CREATED)
    assert len(branch_events) == 1
    assert branch_events[0]["node_id"] == "N03A"
    assert branch_events[0]["payload"]["inherited_brief"] == REVIEW_BRIEF

    # The bounded-repair guardrail the runner already owned is still counted.
    assert state["repair_cycle"] == 1


def test_repair_branches_are_distinct_artifacts_not_an_overwrite(tmp_path, monkeypatch):
    scripts = {
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": REVIEW_BRIEF},
        "N03A": {"outcome": "PASS"},
    }
    state, _ = _run_slice(tmp_path, monkeypatch, scripts, exit_code=0)
    artifacts = {row["node_id"]: Path(row["artifact"]) for row in state["completed_nodes"]}
    assert artifacts["N03"] != artifacts["N03A"]
    assert artifacts["N03"].exists() and artifacts["N03A"].exists()
    assert json.loads(artifacts["N03"].read_text(encoding="utf-8"))["next_brief"] == REVIEW_BRIEF


# ───────────────────────── 3: BLOCKED starts nothing ─────────────────────────

def test_blocked_reaches_the_human_gate_and_nothing_else(tmp_path, monkeypatch):
    scripts = {"N03": {"outcome": "BLOCKED", "verdict": "BLOCKED",
                       "summary": "cannot proceed without a product decision"}}
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, exit_code=0)

    assert state["status"] == "WAITING_FOR_HUMAN"
    assert _executed(state) == ["N01", "N02", "N03", "N09"]
    # no implementation, no machine gate, no repair branch was started
    assert not {"N04", "N05", "N06", "N03A", "N03R"} & set(_executed(state))

    review = _decision(state, "N03")
    assert review["selected"] == ["E_N03_BLOCKED"]
    held = {row["edge_id"]: row["hold_reason"] for row in review["held"]}
    # the CONTINUE edge was refused by the contract, not merely unmatched
    assert held["E_N03_PASS"] == rc.HELD_BLOCKED_CONTINUE
    assert held["E_N03_REPAIR"] == rc.HELD_NOT_MATCHED

    required = journal.by_type(rc.HUMAN_DECISION_REQUIRED)
    assert len(required) == 1
    assert required[0]["node_id"] == "N09"
    assert state["candidate"]["candidate_id"].startswith("CAN_")


# ───────────────────────── 5: deterministic NO_ROUTE ─────────────────────────

def test_no_matching_edge_is_fail_closed(tmp_path, monkeypatch):
    """A gate timeout yields outcome BLOCKED, which no N04 edge covers."""
    scripts = {"N03": {"outcome": "PASS", "verdict": "PASS"}}
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, sleep=30, timeout_seconds=1)

    assert state["status"] == "NO_ROUTE"
    assert state["final_outcome"] == "NO_ROUTE"
    assert "no edge of N04 matched" in state["stop_reason"]
    assert _executed(state) == ["N01", "N02", "N03", "N04"]
    assert not {"N05", "N06", "N09"} & set(_executed(state))
    assert state["frontier"] == []

    gate = _decision(state, "N04")
    assert gate["no_route"] is True
    assert gate["selected"] == []
    assert {row["edge_id"]: row["hold_reason"] for row in gate["held"]} == {
        "E_N04_HARDENING": rc.HELD_BLOCKED_CONTINUE,
        "E_N04_MIGRATION": rc.HELD_BLOCKED_CONTINUE,
        "E_N04_FAILED": rc.HELD_NOT_MATCHED,
    }
    unresolved = journal.by_type(rc.ROUTE_UNRESOLVED)
    assert len(unresolved) == 1 and unresolved[0]["payload"]["status"] == rc.NO_ROUTE
    assert Path(gate["artifact"]).exists()


def test_invalid_result_never_reaches_the_gate(tmp_path, monkeypatch):
    scripts = {"N02": {"outcome": "INVALID", "summary": "unparseable provider output"}}
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, exit_code=0)
    assert state["status"] == "INVALID"
    assert _executed(state) == ["N01", "N02"]
    assert [row["node_id"] for row in state["routing"]["decisions"]] == ["N01"]
    assert journal.by_type(rc.NODE_FAILED)[0]["payload"]["routable"] is False


# ───────────────────────── 6: determinism ─────────────────────────

def test_identical_structured_output_routes_identically(tmp_path, monkeypatch):
    scripts = {
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": REVIEW_BRIEF,
                "carry_forward": CARRY,
                "findings": [{"severity": "HIGH", "file": "a.py", "location": "L1",
                              "description": "d", "required_fix": "f"}]},
        "N03A": {"outcome": "PASS"},
    }

    def fingerprint(state):
        return [(row["node_id"], row["routing_mode"], row["verdict"], tuple(row["selected"]),
                 tuple((h["edge_id"], h["hold_reason"]) for h in row["held"]),
                 row["routing_input_hash"], row["decision_hash"])
                for row in state["routing"]["decisions"]]

    first, _ = _run_slice(tmp_path / "run_a", monkeypatch, scripts, exit_code=0)
    second, _ = _run_slice(tmp_path / "run_b", monkeypatch, scripts, exit_code=0)

    assert first["AAW_RUN_ID"] != second["AAW_RUN_ID"]
    assert _executed(first) == _executed(second)
    assert fingerprint(first) == fingerprint(second)
    assert first["routing"]["lineage"]["N03A"]["template_id"] == second["routing"]["lineage"]["N03A"]["template_id"]


# ───────────────────────── 7: evidence ─────────────────────────

def test_evidence_names_the_path_not_taken(tmp_path, monkeypatch):
    scripts = {
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": REVIEW_BRIEF},
        "N03A": {"outcome": "PASS"},
    }
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, exit_code=0)

    # the durable artifact carries the whole candidate set, matched and not
    decision = json.loads(Path(_decision(state, "N03")["artifact"]).read_text(encoding="utf-8"))
    assert decision["schema_version"] == rc.GATE_DECISION_SCHEMA_VERSION
    assert [row["edge_id"] for row in decision["candidates"]] == ["E_N03_PASS", "E_N03_REPAIR", "E_N03_BLOCKED"]
    matched = {row["edge_id"]: row["matched"] for row in decision["candidates"]}
    assert matched == {"E_N03_PASS": False, "E_N03_REPAIR": True, "E_N03_BLOCKED": False}
    # every candidate carries its per-predicate trace, so "why not" is readable
    trace = next(row["trace"] for row in decision["candidates"] if row["edge_id"] == "E_N03_PASS")
    assert trace == [{"predicate": "verdict", "expected": "PASS", "actual": "REPAIR", "matched": False}]

    # the journal names selected and held separately, in order
    kinds = [row["event_type"] for row in journal.read() if row["node_id"] == "N03"]
    assert kinds == [rc.NODE_STARTED, rc.NODE_COMPLETED, rc.GATE_EVALUATED,
                     rc.EDGE_SELECTED, rc.EDGE_HELD, rc.EDGE_HELD]

    # and the projection a canvas would consume needs no log parsing
    projection = rc.graph_projection(state, journal)
    assert projection["contract_version"] == rc.CONTRACT_VERSION
    node_row = next(row for row in projection["nodes"] if row["node_id"] == "N03A")
    assert node_row["lineage"]["origin_node_id"] == "N03"
    assert {row["event_type"] for row in projection["events"]} >= {
        rc.NODE_STARTED, rc.NODE_COMPLETED, rc.GATE_EVALUATED,
        rc.EDGE_SELECTED, rc.EDGE_HELD, rc.BRANCH_CREATED, rc.HUMAN_DECISION_REQUIRED}


def test_held_frontier_is_reported_at_the_human_gate(tmp_path, monkeypatch):
    scripts = {"N03": {"outcome": "PASS", "verdict": "PASS"}}
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, exit_code=0)
    required = journal.by_type(rc.HUMAN_DECISION_REQUIRED)[0]
    assert required["payload"]["held_frontier"] == []
    assert required["payload"]["candidate_id"] == state["candidate"]["candidate_id"]


# ───────────────────────── 8: one engine ─────────────────────────

def test_legacy_workflow_uses_the_same_router(tmp_path, monkeypatch):
    """The unchanged V0.2 workflow runs through the same loop and the same gate.

    Its `on_pass`/`on_fail` transitions are compiled into edges rather than
    handled by a second code path, which is what keeps this an extension
    instead of a parallel engine.
    """
    scripts = {}
    state, journal = _run_slice(tmp_path, monkeypatch, scripts, source=LEGACY, gate_node="N02", exit_code=0)

    assert state["status"] == "WAITING_FOR_HUMAN"
    assert state["routing"]["contract_version"] == rc.CONTRACT_VERSION
    decisions = state["routing"]["decisions"]
    assert decisions, "legacy transitions must still produce gate decisions"
    assert all(edge.startswith(row["node_id"] + ":LEGACY_")
               for row in decisions for edge in row["selected"])
    assert all(row["routing_mode"] == "FIRST_MATCH" for row in decisions)
    assert journal.by_type(rc.EDGE_SELECTED)


def test_the_contract_layer_exposes_no_execution_entry_point():
    """`routing_contract` decides routes; it must not be able to run anything."""
    forbidden = {"execute", "run", "main", "dispatch", "run_node", "execute_workflow"}
    assert not forbidden & set(dir(rc))
    assert not hasattr(rc, "subprocess")
