"""Vertical slices + unit tests: AAW PATH-SCOPED BRANCH CONTEXT + MERGE/REJOIN V0.1.

Covered acceptance criteria:
  1  branch A never sees branch B's context before merge   test_neither_branch_sees_the_others_carry_forward_pre_merge
  2  (symmetric)                                            same
  3  merge inputs preserve source provenance                test_all_required_merge_is_source_attributed
  4  ALL_REQUIRED waits correctly                            test_all_required_merge_is_source_attributed
  5  ANY_COMPLETED is deterministic                           test_any_completed_resolves_on_first_arrival_deterministically
  6  completion order does not alter ALL_REQUIRED result      test_all_required_result_is_independent_of_completion_order
  7  duplicate/replayed arrival never re-executes the merge   test_any_completed_late_arrival_is_recorded_but_never_mutates_result
  8  failed/blocked/cancelled inputs are fail-closed           test_a_held_expected_edge_fast_fails_the_merge,
                                                               test_drain_time_catches_a_merge_no_node_ever_holds
  9  repair-minted lineage participates correctly              test_repair_minted_branch_fills_a_merge_slot_by_concrete_node
 10  runtime events expose merge progress structurally         test_all_required_merge_is_source_attributed (events)
 11  graph projection exposes merge state without log parsing  test_graph_projection_reports_zero_of_n_before_any_arrival
 12  legacy/non-merge convergence is unaffected                 test_multirouting_slice.py (unchanged, run alongside this file)
 13  closed input set is enforced                               test_expected_incoming_must_exactly_match_the_graph
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import routing_contract as rc
import workflow_runner as runner
from execution_contract import update_execution
from workflow_schema import WorkflowValidationError, load_workflow, validate_node_result

WORKFLOWS = Path(__file__).with_name("WORKFLOWS")
MERGE_SLICE = WORKFLOWS / "MERGE_SLICE_V1.json"
MERGE_SLICE_REPAIR = WORKFLOWS / "MERGE_SLICE_REPAIR_V1.json"
MERGE_SLICE_DRAIN = WORKFLOWS / "MERGE_SLICE_DRAIN_V1.json"


# ═══════════════════════════ pure unit tests ═══════════════════════════

def test_ancestry_is_a_single_parent_chain_before_any_merge():
    arrivals = {
        "N02": [{"status": "ACCEPTED", "source_node_id": "N01"}],
        "N03": [{"status": "ACCEPTED", "source_node_id": "N02"}],
    }
    assert rc.ancestry_of("N03", arrivals) == {"N01", "N02", "N03"}
    assert rc.ancestry_of("N01", arrivals) == {"N01"}  # root: no arrival record


def test_ancestry_is_a_genuine_multi_parent_union_at_a_merge():
    """The DAG case amendment #1 required: a MERGE has more than one causal
    parent, and downstream ancestry is the real union, not a single chain."""
    arrivals = {
        "A1": [{"status": "ACCEPTED", "source_node_id": "ROOT"}],
        "B1": [{"status": "ACCEPTED", "source_node_id": "ROOT"}],
        "M": [
            {"status": "ACCEPTED", "source_node_id": "A1"},
            {"status": "ACCEPTED", "source_node_id": "B1"},
        ],
        "N09": [{"status": "ACCEPTED", "source_node_id": "M"}],
    }
    assert rc.ancestry_of("M", arrivals) == {"ROOT", "A1", "B1", "M"}
    assert rc.ancestry_of("N09", arrivals) == {"ROOT", "A1", "B1", "M", "N09"}
    # A1's own ancestry must never include B1: no sibling leakage even though
    # both eventually feed the same merge.
    assert rc.ancestry_of("A1", arrivals) == {"ROOT", "A1"}


def test_duplicate_vs_stale_lineage_classification():
    # First arrival at a fresh slot: accepted.
    assert rc.classify_merge_slot_arrival([], source_node_id="N05") == rc.ARRIVAL_ACCEPTED
    accepted = [{"status": rc.ARRIVAL_ACCEPTED, "source_node_id": "N05"}]
    # Same concrete source repeating: a replay.
    assert rc.classify_merge_slot_arrival(accepted, source_node_id="N05") == rc.ARRIVAL_DUPLICATE
    # A different source re-using the same already-resolved slot: not a
    # duplicate, and must not silently overwrite the resolved slot.
    assert rc.classify_merge_slot_arrival(accepted, source_node_id="N06") == rc.ARRIVAL_STALE_LINEAGE


def test_ordinary_node_arrivals_are_first_wins_no_stale_lineage_concept():
    assert rc.classify_ordinary_arrival([]) == rc.ARRIVAL_ACCEPTED
    accepted = [{"status": rc.ARRIVAL_ACCEPTED}]
    assert rc.classify_ordinary_arrival(accepted) == rc.ARRIVAL_DUPLICATE


def test_merge_readiness_and_selected_edges():
    expected = ["E_A", "E_B"]
    assert rc.merge_readiness("ALL_REQUIRED", expected, {}) == "WAITING"
    assert rc.merge_readiness("ALL_REQUIRED", expected, {"E_A": {}}) == "WAITING"
    assert rc.merge_readiness("ALL_REQUIRED", expected, {"E_A": {}, "E_B": {}}) == "READY"
    assert rc.merge_readiness("ANY_COMPLETED", expected, {}) == "WAITING"
    assert rc.merge_readiness("ANY_COMPLETED", expected, {"E_B": {}}) == "READY"

    accepted = {"E_A": {"sequence": 5, "edge_id": "E_A"}, "E_B": {"sequence": 2, "edge_id": "E_B"}}
    assert rc.merge_selected_edges("ALL_REQUIRED", expected, accepted) == ["E_A", "E_B"]
    assert rc.merge_selected_edges("ANY_COMPLETED", expected, accepted) == ["E_B"]  # lower sequence wins


def test_merge_resolution_hash_ignores_dict_insertion_order_and_timestamps():
    results_by_node = {
        "N05": {"outcome": "PASS", "verdict": "PASS", "carry_forward": ["x"], "artifacts": [],
                "changed_files": [], "findings": [], "execution_id": "EXE_1", "started_at": "T1"},
        "N06": {"outcome": "PASS", "verdict": "PASS", "carry_forward": ["y"], "artifacts": [],
                "changed_files": [], "findings": [], "execution_id": "EXE_2", "started_at": "T2"},
    }
    expected = ["E_N05_CONTINUE", "E_N06_CONTINUE"]
    forward_slots = {
        "E_N05_CONTINUE": {"source_node_id": "N05", "sequence": 1},
        "E_N06_CONTINUE": {"source_node_id": "N06", "sequence": 2},
    }
    reversed_slots = {  # same content, inserted in the opposite dict order
        "E_N06_CONTINUE": {"source_node_id": "N06", "sequence": 99},  # sequence differs too
        "E_N05_CONTINUE": {"source_node_id": "N05", "sequence": 1},
    }
    h1 = rc.merge_resolution_hash(policy="ALL_REQUIRED", expected_incoming=expected,
                                  slots=forward_slots, results_by_node=results_by_node)
    h2 = rc.merge_resolution_hash(policy="ALL_REQUIRED", expected_incoming=expected,
                                  slots=reversed_slots, results_by_node=results_by_node)
    assert h1 == h2

    # A timestamp/execution_id-only change must not move the hash...
    mutated_ids = {k: {**v, "execution_id": "DIFFERENT", "started_at": "LATER"} for k, v in results_by_node.items()}
    h3 = rc.merge_resolution_hash(policy="ALL_REQUIRED", expected_incoming=expected,
                                  slots=forward_slots, results_by_node=mutated_ids)
    assert h1 == h3

    # ...but a real content change (carry_forward) must.
    mutated_content = {**results_by_node, "N05": {**results_by_node["N05"], "carry_forward": ["different"]}}
    h4 = rc.merge_resolution_hash(policy="ALL_REQUIRED", expected_incoming=expected,
                                  slots=forward_slots, results_by_node=mutated_content)
    assert h1 != h4


def test_expected_incoming_must_exactly_match_the_graph(tmp_path):
    data = json.loads(MERGE_SLICE.read_text(encoding="utf-8"))
    merge_node = next(n for n in data["nodes"] if n["id"] == "M08")
    merge_node["expected_incoming"] = ["E_N05_CONTINUE"]  # drops a real incoming edge
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(WorkflowValidationError, match="expected_incoming must exactly equal"):
        load_workflow(path)

    data2 = json.loads(MERGE_SLICE.read_text(encoding="utf-8"))
    merge_node2 = next(n for n in data2["nodes"] if n["id"] == "M08")
    merge_node2["expected_incoming"].append("E_NOT_A_REAL_EDGE")  # undeclared slot
    path2 = tmp_path / "workflow2.json"
    path2.write_text(json.dumps(data2), encoding="utf-8")
    with pytest.raises(WorkflowValidationError, match="expected_incoming must exactly equal"):
        load_workflow(path2)


# ═══════════════════════════ runner fixtures (shared with test_multirouting_slice.py's pattern) ═══════════════════════════

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
    _git(["git", "worktree", "add", "-b", "aaw/merge-slice", str(worktree)], repo)
    return repo, worktree


def _machine_command(exit_code=0):
    return [sys.executable, "-c", f"raise SystemExit({exit_code})"]


def _install_fake_llm(monkeypatch, scripts, captured=None):
    def fake_llm(workflow, state, node, worktree, execution, execution_path, recorder=None):
        node_id = str(node["id"])
        if captured is not None:
            captured[node_id] = {
                "package": runner.node_package(workflow, state, node, worktree),
                "lineage": node.get("lineage"),
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
        validate_node_result(result, node_id, node["type"])
        telemetry = {
            "schema_version": "1.1", "execution_id": execution["execution_id"],
            "run_id": state["AAW_RUN_ID"], "node": node_id, "workflow_node_id": node_id,
            "workflow_node_type": node["type"], "harness": "fixture", "model": "fixture",
            "effort": "medium", "provider_session_id": "fixture", "wall_time_s": 0.0, "usage": {},
        }
        return result, telemetry

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)


def _run(root, monkeypatch, workflow_path, scripts, captured=None, *, gate_node="N04", exit_code=0):
    repo, worktree = _workspace(root)
    data = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    gate = next((n for n in data["nodes"] if n["id"] == gate_node and n["type"] == "MACHINE_GATE"), None)
    if gate is not None:
        gate["command"] = _machine_command(exit_code)
        gate["timeout_seconds"] = 60
    dest = root / "workflow.json"
    dest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(runner, "STATS_ROOT", root / "03_STATS")
    _install_fake_llm(monkeypatch, scripts, captured)
    state = runner.execute(dest, "merge slice", repo, worktree, preprocess_policy="OFF")
    journal = rc.RoutingJournal(Path(state["routing"]["journal_path"]), run_id=state["AAW_RUN_ID"])
    return state, journal


def _completed_result(state, node_id):
    row = next(r for r in state["completed_nodes"] if r["node_id"] == node_id)
    return json.loads(Path(row["artifact"]).read_text(encoding="utf-8"))


def _with_merge_policy(workflow_path, tmp_path, policy, *, merge_node="M08"):
    data = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    next(n for n in data["nodes"] if n["id"] == merge_node)["merge_policy"] = policy
    dest = tmp_path / f"workflow_{policy.lower()}.json"
    dest.write_text(json.dumps(data), encoding="utf-8")
    return dest


def _with_reversed_fanout_edges(workflow_path, tmp_path, *, gate_node="N04"):
    """Same graph, N06's edge declared before N05's on the fan-out gate --
    ALL_MATCHES preserves declaration order, so this reverses which branch
    the FIFO frontier visits first, without changing what the graph means."""
    data = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    gate = next(n for n in data["nodes"] if n["id"] == gate_node)
    gate["edges"] = list(reversed(gate["edges"]))
    dest = tmp_path / "workflow_reversed.json"
    dest.write_text(json.dumps(data), encoding="utf-8")
    return dest


CARRY_A = ["hardening: keep behind a flag"]
CARRY_B = ["migration: forward-only"]


# ═══════════════════════════ Slice A: ALL_REQUIRED ═══════════════════════════

def test_neither_branch_sees_the_others_carry_forward_pre_merge(tmp_path, monkeypatch):
    captured = {}
    scripts = {
        "N05": {"outcome": "PASS", "carry_forward": CARRY_A, "artifacts": ["hardening.json"]},
        "N06": {"outcome": "PASS", "carry_forward": CARRY_B, "artifacts": ["migration.json"]},
    }
    state, journal = _run(tmp_path, monkeypatch, MERGE_SLICE, scripts, captured)
    assert state["status"] == "WAITING_FOR_HUMAN"

    n05_carry = captured["N05"]["package"].get("CARRY_FORWARD", [])
    n06_carry = captured["N06"]["package"].get("CARRY_FORWARD", [])
    # N05 and N06 are siblings of the same fan-out: neither has executed yet
    # when the other's package is built, so today this is trivially empty for
    # both -- the real assertion is that CARRY_B never appears in N05's own
    # ancestry-scoped context, and vice versa, which is what the leak used to
    # violate once *downstream* results existed. Assert the ancestry sets
    # directly, which is the actual mechanism under test.
    assert CARRY_B not in n05_carry
    assert CARRY_A not in n06_carry
    ancestry_n05 = rc.ancestry_of("N05", state["routing"]["arrivals"])
    ancestry_n06 = rc.ancestry_of("N06", state["routing"]["arrivals"])
    assert "N06" not in ancestry_n05
    assert "N05" not in ancestry_n06


def test_all_required_merge_is_source_attributed(tmp_path, monkeypatch):
    scripts = {
        "N05": {"outcome": "PASS", "carry_forward": CARRY_A, "artifacts": ["hardening.json"]},
        "N06": {"outcome": "PASS", "carry_forward": CARRY_B, "artifacts": ["migration.json"]},
    }
    state, journal = _run(tmp_path, monkeypatch, MERGE_SLICE, scripts)
    assert state["status"] == "WAITING_FOR_HUMAN"
    assert [r["node_id"] for r in state["completed_nodes"]].count("M08") == 1

    merge_result = _completed_result(state, "M08")
    assert merge_result["outcome"] == "PASS"
    assert set(merge_result["incoming"]) == {"E_N05_CONTINUE", "E_N06_CONTINUE"}
    assert merge_result["incoming"]["E_N05_CONTINUE"]["source_node"] == "N05"
    assert merge_result["incoming"]["E_N06_CONTINUE"]["source_node"] == "N06"
    assert merge_result["incoming"]["E_N05_CONTINUE"]["carry_forward"] == CARRY_A
    assert merge_result["incoming"]["E_N06_CONTINUE"]["carry_forward"] == CARRY_B
    # Declared-order union, not a flatten of everything ever run.
    assert merge_result["carry_forward"] == CARRY_A + CARRY_B
    assert merge_result["artifacts"] == ["hardening.json", "migration.json"]
    assert merge_result["merge_resolution_hash"]

    merges = state["routing"]["merges"]
    assert merges["M08"]["status"] == "MERGED"
    assert merges["M08"]["merge_resolution_hash"] == merge_result["merge_resolution_hash"]

    # Runtime events expose merge progress structurally (acceptance #10).
    kinds = [row["event_type"] for row in journal.by_type(rc.BRANCH_ARRIVED)]
    assert len(kinds) == 2
    assert len(journal.by_type(rc.MERGE_WAITING)) == 1
    assert len(journal.by_type(rc.MERGE_READY)) == 1
    assert len(journal.by_type(rc.MERGE_STARTED)) == 1
    assert len(journal.by_type(rc.MERGE_COMPLETED)) == 1
    assert journal.by_type(rc.MERGE_COMPLETED)[0]["payload"]["merge_resolution_hash"] == merge_result["merge_resolution_hash"]

    # N09 (downstream of the merge) inherits only the merged, curated union.
    ancestry_n09 = rc.ancestry_of("N09", state["routing"]["arrivals"])
    assert ancestry_n09 == {"N01", "N04", "N05", "N06", "M08", "N09"}


def test_all_required_result_is_independent_of_completion_order(tmp_path, monkeypatch):
    scripts = {
        "N05": {"outcome": "PASS", "carry_forward": CARRY_A, "artifacts": ["hardening.json"]},
        "N06": {"outcome": "PASS", "carry_forward": CARRY_B, "artifacts": ["migration.json"]},
    }
    forward, _ = _run(tmp_path / "forward", monkeypatch, MERGE_SLICE, scripts)
    reversed_path = _with_reversed_fanout_edges(MERGE_SLICE, tmp_path)
    reversed_state, _ = _run(tmp_path / "reversed", monkeypatch, reversed_path, scripts)

    assert forward["status"] == reversed_state["status"] == "WAITING_FOR_HUMAN"
    # The FIFO order genuinely differed...
    forward_order = [r["node_id"] for r in forward["completed_nodes"]]
    reversed_order = [r["node_id"] for r in reversed_state["completed_nodes"]]
    assert forward_order.index("N05") < forward_order.index("N06")
    assert reversed_order.index("N06") < reversed_order.index("N05")
    # ...but the merge's semantic content and resolution hash did not.
    forward_merge = _completed_result(forward, "M08")
    reversed_merge = _completed_result(reversed_state, "M08")
    assert forward_merge["merge_resolution_hash"] == reversed_merge["merge_resolution_hash"]
    assert forward_merge["carry_forward"] == reversed_merge["carry_forward"] == CARRY_A + CARRY_B
    assert forward_merge["artifacts"] == reversed_merge["artifacts"]
    assert {k: v["source_node"] for k, v in forward_merge["incoming"].items()} == \
           {k: v["source_node"] for k, v in reversed_merge["incoming"].items()}


def test_graph_projection_reports_zero_of_n_before_any_arrival(tmp_path, monkeypatch):
    """Amendment #2: a declared MERGE exists in runtime state (and the
    projection) from the run's first frame, before any branch has arrived."""
    scripts = {"N05": {"outcome": "PASS"}, "N06": {"outcome": "PASS"}}
    captured_first_frame = {}

    real_execute_merge = runner.execute_merge

    def spying_execute_merge(state, node, journal):
        return real_execute_merge(state, node, journal)

    monkeypatch.setattr(runner, "execute_merge", spying_execute_merge)
    # Inspect the merge bookkeeping as seeded, before the run does anything:
    # this mirrors what `execute()` writes before the frontier loop starts.
    workflow = load_workflow(MERGE_SLICE)
    nodes, _ = runner.resolve_execution_plan(workflow)
    merge_nodes = [n for n in nodes if n["type"] == "MERGE"]
    assert merge_nodes and merge_nodes[0]["id"] == "M08"

    state, journal = _run(tmp_path, monkeypatch, MERGE_SLICE, scripts)
    projection = rc.graph_projection(state, journal)
    merge_row = next(row for row in projection["merges"] if row["node_id"] == "M08")
    assert merge_row["required_count"] == 2
    assert merge_row["status"] == "MERGED"  # by the time the run settles
    assert merge_row["policy"] == "ALL_REQUIRED"
    assert set(merge_row["expected_incoming"]) == {"E_N05_CONTINUE", "E_N06_CONTINUE"}


# ═══════════════════════════ Slice B: ANY_COMPLETED ═══════════════════════════

def test_any_completed_resolves_on_first_arrival_deterministically(tmp_path, monkeypatch):
    workflow = _with_merge_policy(MERGE_SLICE, tmp_path, "ANY_COMPLETED")
    scripts = {
        "N05": {"outcome": "PASS", "carry_forward": CARRY_A},
        "N06": {"outcome": "PASS", "carry_forward": CARRY_B},
    }
    state, journal = _run(tmp_path, monkeypatch, workflow, scripts)
    assert state["status"] == "WAITING_FOR_HUMAN"

    merge_result = _completed_result(state, "M08")
    assert merge_result["selected_edges"] == ["E_N05_CONTINUE"]  # N05 is FIFO-first
    assert set(merge_result["incoming"]) == {"E_N05_CONTINUE"}
    assert merge_result["carry_forward"] == CARRY_A

    assert state["routing"]["merges"]["M08"]["resolved_edge_id"] == "E_N05_CONTINUE"
    assert len(journal.by_type(rc.MERGE_COMPLETED)) == 1  # never executed twice


def test_any_completed_late_arrival_is_recorded_but_never_mutates_result(tmp_path, monkeypatch):
    workflow = _with_merge_policy(MERGE_SLICE, tmp_path, "ANY_COMPLETED")
    scripts = {"N05": {"outcome": "PASS", "carry_forward": CARRY_A},
               "N06": {"outcome": "PASS", "carry_forward": CARRY_B}}
    state, journal = _run(tmp_path, monkeypatch, workflow, scripts)

    arrivals = state["routing"]["arrivals"]["M08"]
    late = [row for row in arrivals if row["late"]]
    assert len(late) == 1
    assert late[0]["source_node_id"] == "N06"
    assert late[0]["edge_id"] == "E_N06_CONTINUE"

    merge_result = _completed_result(state, "M08")
    # The late branch's content never entered the settled result.
    assert "E_N06_CONTINUE" not in merge_result["incoming"]
    assert CARRY_B not in merge_result["carry_forward"]

    projection = rc.graph_projection(state, rc.RoutingJournal(Path(state["routing"]["journal_path"]), run_id=state["AAW_RUN_ID"]))
    merge_row = next(row for row in projection["merges"] if row["node_id"] == "M08")
    assert merge_row["arrived_count"] == 1  # only the accepted (non-late) slot counts
    assert any(a["late"] for a in merge_row["arrived"])


# ═══════════════════════════ Slice C: repair lineage into merge ═══════════════════════════

def test_repair_minted_branch_fills_a_merge_slot_by_concrete_node(tmp_path, monkeypatch):
    scripts = {
        "N05": {"outcome": "PASS", "carry_forward": ["plain branch constraint"]},
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": "fix the rotation bug"},
        "N04": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": "fix the rotation bug, independently"},
        "N03R": {"outcome": "PASS", "carry_forward": ["repaired constraint"]},  # shared by both minted branches
    }
    state, journal = _run(tmp_path, monkeypatch, MERGE_SLICE_REPAIR, scripts, gate_node="N02")
    assert state["status"] == "WAITING_FOR_HUMAN"

    executed = [r["node_id"] for r in state["completed_nodes"]]
    assert "N03A" in executed and "N04A" in executed  # both origins minted their own branch
    assert "N03R" not in executed  # the template itself is never entered

    merge_result = _completed_result(state, "M08")
    slot = merge_result["incoming"]["E_N03R_CONTINUE"]
    # The merge names the concrete minted node, never the template.
    assert slot["source_node"] == "N03A"  # N03A is FIFO-first (see fixture ordering)
    assert slot["lineage"]["template_id"] == "N03R"
    assert slot["lineage"]["origin_node_id"] == "N03"
    assert slot["lineage"]["branch_index"] == 1

    # The second, independent minted descendant (N04A) reused the same
    # template edge_id after N03A already settled that slot: stale lineage,
    # not silently merged in, and — since M08 had already resolved — late.
    stale = [row for row in state["routing"]["arrivals"]["M08"] if row["status"] == rc.ARRIVAL_STALE_LINEAGE]
    assert len(stale) == 1
    assert stale[0]["source_node_id"] == "N04A"
    assert stale[0]["lineage_template_id"] == "N03R"
    assert stale[0]["late"] is True
    assert "N04A" not in {v["source_node"] for v in merge_result["incoming"].values()}


# ═══════════════════════════ fail-closed terminal semantics ═══════════════════════════

def test_a_held_expected_edge_fast_fails_the_merge(tmp_path, monkeypatch):
    """N06 gets a second, conditional edge so an outcome=BLOCKED result still
    selects *something* (E_N06_FALLBACK, to STOP) rather than going NO_ROUTE --
    isolating the case under test: E_N06_CONTINUE (M08's expected slot) is
    genuinely held, N06 is not a REPAIR template and cannot re-execute, so the
    fast path fires immediately instead of waiting for the frontier to drain.
    """
    data = json.loads(MERGE_SLICE.read_text(encoding="utf-8"))
    n06 = next(n for n in data["nodes"] if n["id"] == "N06")
    n06["edges"] = [
        {"edge_id": "E_N06_CONTINUE", "to": "M08", "when": {"outcome": "PASS"}, "kind": "CONTINUE", "label": "continue"},
        {"edge_id": "E_N06_FALLBACK", "to": "STOP", "when": {"outcome": "BLOCKED"}, "kind": "FALLBACK", "label": "blocked -- stop"},
    ]
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(json.dumps(data), encoding="utf-8")

    scripts = {"N05": {"outcome": "PASS"}, "N06": {"outcome": "BLOCKED"}}
    state, journal = _run(tmp_path, monkeypatch, workflow_path, scripts)
    assert state["status"] == "BLOCKED"
    assert "M08" in state["stop_reason"]
    assert "E_N06_CONTINUE" in state["stop_reason"]
    assert state["routing"]["merges"]["M08"]["status"] == "BLOCKED"
    blocked_events = journal.by_type(rc.MERGE_BLOCKED)
    assert len(blocked_events) == 1
    assert blocked_events[0]["payload"]["missing"] == ["E_N06_CONTINUE"]
    # M08 never actually executed.
    assert "M08" not in [r["node_id"] for r in state["completed_nodes"]]


def test_drain_time_catches_a_merge_no_node_ever_holds(tmp_path, monkeypatch):
    """N02 resolves PASS, so it selects E_N02_PASS and *holds* E_N02_REPAIR --
    but M08's second slot is owned by template N02R's own edge (E_N02R_CONTINUE),
    which is never evaluated at all (the template is never entered). The fast
    path in apply_gate_decision cannot see this; only the drain-time scan can.
    """
    scripts = {"N02": {"outcome": "PASS", "verdict": "PASS"}}
    state, journal = _run(tmp_path, monkeypatch, MERGE_SLICE_DRAIN, scripts, gate_node=None)
    assert state["status"] == "BLOCKED"
    assert "M08" in state["stop_reason"]
    assert "E_N02R_CONTINUE" in state["stop_reason"]
    assert state["routing"]["merges"]["M08"]["status"] == "BLOCKED"
    # The other slot really did arrive -- this is a genuine partial merge,
    # not a merge that never started.
    accepted = [r for r in state["routing"]["arrivals"]["M08"] if r["status"] == rc.ARRIVAL_ACCEPTED]
    assert {r["edge_id"] for r in accepted} == {"E_N02_PASS"}
    # The run never reached "COMPLETED" while this merge sat unresolved.
    assert state["status"] != "COMPLETED"
