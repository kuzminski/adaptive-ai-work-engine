"""Unit tests for AAW MULTIROUTING RUNTIME CONTRACT V0.1 — the pure gate layer.

These cover the contract itself. `test_multirouting_slice.py` covers the same
contract driving the real workflow runner end to end.
"""

import json

import pytest

import routing_contract as rc
from workflow_schema import WorkflowValidationError, validate_node_result, validate_workflow


def _result(outcome="PASS", **extra):
    base = {
        "node_id": "N03", "node_type": "REVIEW", "outcome": outcome, "summary": "fixture",
        "changed_files": [], "tests": [], "findings": [], "remaining_uncertainty": [],
        "recommended_next_action": "fixture",
    }
    base.update(extra)
    return base


def _review_node(edges=None, routing="FIRST_MATCH"):
    return {
        "id": "N03", "type": "REVIEW", "routing": routing,
        "edges": edges if edges is not None else [
            {"edge_id": "E_PASS", "to": "N04", "when": {"verdict": "PASS"}, "kind": "CONTINUE"},
            {"edge_id": "E_REPAIR", "to": "N03R", "when": {"verdict": "REPAIR"}, "kind": "REPAIR"},
            {"edge_id": "E_BLOCKED", "to": "N09", "when": {"verdict": "BLOCKED"}, "kind": "FALLBACK"},
        ],
    }


# ── verdict derivation ────────────────────────────────────────────────────────

def test_verdict_is_derived_from_outcome_when_absent():
    assert rc.derive_verdict(_result("PASS")) == "PASS"
    assert rc.derive_verdict(_result("FAIL")) == "REPAIR"
    assert rc.derive_verdict(_result("BLOCKED")) == "BLOCKED"


def test_invalid_outcome_has_no_verdict_so_it_can_never_route():
    assert rc.derive_verdict(_result("INVALID")) is None


def test_explicit_verdict_wins_over_derivation():
    assert rc.derive_verdict(_result("FAIL", verdict="BLOCKED")) == "BLOCKED"


def test_unsupported_verdict_is_rejected():
    with pytest.raises(rc.RoutingContractError):
        rc.derive_verdict(_result("FAIL", verdict="MAYBE"))


# ── selection modes ───────────────────────────────────────────────────────────

def test_first_match_selects_exactly_one_and_holds_the_rest():
    decision = rc.evaluate_gate(_review_node(), _result("PASS", verdict="PASS"))
    assert [row["edge_id"] for row in decision["selected"]] == ["E_PASS"]
    assert {row["edge_id"]: row["hold_reason"] for row in decision["held"]} == {
        "E_REPAIR": rc.HELD_NOT_MATCHED, "E_BLOCKED": rc.HELD_NOT_MATCHED,
    }
    assert decision["no_route"] is False


def test_first_match_holds_a_later_match_as_not_first():
    node = _review_node(edges=[
        {"edge_id": "E_A", "to": "N04", "when": {"outcome": "PASS"}, "kind": "CONTINUE"},
        {"edge_id": "E_B", "to": "N05", "when": {"verdict": "PASS"}, "kind": "CONTINUE"},
    ])
    decision = rc.evaluate_gate(node, _result("PASS"))
    assert [row["edge_id"] for row in decision["selected"]] == ["E_A"]
    assert decision["held"][0]["hold_reason"] == rc.HELD_NOT_FIRST


def test_all_matches_fans_out_to_every_matching_edge():
    node = _review_node(routing="ALL_MATCHES", edges=[
        {"edge_id": "E_A", "to": "N05", "when": {"outcome": "PASS"}, "kind": "CONTINUE"},
        {"edge_id": "E_B", "to": "N06", "when": {"outcome": "PASS"}, "kind": "CONTINUE"},
        {"edge_id": "E_C", "to": "N09", "when": {"outcome": "FAIL"}, "kind": "FALLBACK"},
    ])
    decision = rc.evaluate_gate(node, _result("PASS"))
    assert [row["edge_id"] for row in decision["selected"]] == ["E_A", "E_B"]
    assert [row["edge_id"] for row in decision["held"]] == ["E_C"]


def test_unconditional_edge_always_matches():
    node = _review_node(edges=[{"edge_id": "E_GO", "to": "N04", "when": None, "kind": "CONTINUE"}])
    for outcome in ("PASS", "FAIL"):
        decision = rc.evaluate_gate(node, _result(outcome))
        assert [row["edge_id"] for row in decision["selected"]] == ["E_GO"]


# ── fail-closed behaviour ─────────────────────────────────────────────────────

def test_no_route_when_nothing_matches():
    node = _review_node(edges=[
        {"edge_id": "E_PASS", "to": "N04", "when": {"verdict": "PASS"}, "kind": "CONTINUE"},
    ])
    decision = rc.evaluate_gate(node, _result("FAIL"))
    assert decision["no_route"] is True
    assert decision["selected"] == []
    assert decision["held"][0]["hold_reason"] == rc.HELD_NOT_MATCHED


def test_blocked_verdict_can_never_select_a_continue_edge():
    node = _review_node(edges=[
        {"edge_id": "E_GO", "to": "N04", "when": None, "kind": "CONTINUE"},
    ])
    decision = rc.evaluate_gate(node, _result("BLOCKED"))
    assert decision["no_route"] is True
    assert decision["held"][0]["hold_reason"] == rc.HELD_BLOCKED_CONTINUE


def test_blocked_verdict_reaches_a_fallback_edge():
    decision = rc.evaluate_gate(_review_node(), _result("BLOCKED"))
    assert [row["edge_id"] for row in decision["selected"]] == ["E_BLOCKED"]
    held = {row["edge_id"]: row["hold_reason"] for row in decision["held"]}
    assert held["E_PASS"] == rc.HELD_BLOCKED_CONTINUE


# ── predicates ────────────────────────────────────────────────────────────────

def test_has_findings_predicate():
    node = _review_node(edges=[
        {"edge_id": "E_FINDINGS", "to": "N03R", "when": {"has_findings": True}, "kind": "REPAIR"},
        {"edge_id": "E_CLEAN", "to": "N04", "when": {"has_findings": False}, "kind": "CONTINUE"},
    ])
    clean = rc.evaluate_gate(node, _result("PASS"))
    assert [row["edge_id"] for row in clean["selected"]] == ["E_CLEAN"]
    dirty = rc.evaluate_gate(node, _result("FAIL", findings=[{"severity": "LOW", "description": "x"}]))
    assert [row["edge_id"] for row in dirty["selected"]] == ["E_FINDINGS"]


def test_min_severity_predicate_uses_the_highest_finding():
    node = _review_node(edges=[
        {"edge_id": "E_HIGH", "to": "N09", "when": {"min_severity": "HIGH"}, "kind": "FALLBACK"},
        {"edge_id": "E_LOW", "to": "N03R", "when": None, "kind": "REPAIR"},
    ])
    low = rc.evaluate_gate(node, _result("FAIL", findings=[{"severity": "LOW"}, {"severity": "MEDIUM"}]))
    assert [row["edge_id"] for row in low["selected"]] == ["E_LOW"]
    high = rc.evaluate_gate(node, _result("FAIL", findings=[{"severity": "LOW"}, {"severity": "CRITICAL"}]))
    assert [row["edge_id"] for row in high["selected"]] == ["E_HIGH"]


def test_unknown_severity_escalates_rather_than_dampens():
    rank, known = rc.severity_rank("catastrophic")
    assert known is False and rank == len(rc.SEVERITY_LADDER) - 1
    projection = rc.project_result(_result("FAIL", findings=[{"severity": "catastrophic"}]))
    assert projection["unknown_severities"] == ["catastrophic"]


def test_predicates_in_one_when_block_are_anded():
    node = _review_node(edges=[
        {"edge_id": "E_BOTH", "to": "N09", "when": {"verdict": "REPAIR", "min_severity": "HIGH"}, "kind": "FALLBACK"},
    ])
    assert rc.evaluate_gate(node, _result("FAIL", findings=[{"severity": "LOW"}]))["no_route"] is True
    assert rc.evaluate_gate(node, _result("FAIL", findings=[{"severity": "HIGH"}]))["no_route"] is False


# ── determinism ───────────────────────────────────────────────────────────────

def test_identical_structured_output_yields_identical_routing():
    node = _review_node()
    payload = _result("FAIL", verdict="REPAIR", findings=[{"severity": "HIGH", "description": "d"}])
    first = rc.evaluate_gate(node, json.loads(json.dumps(payload)))
    second = rc.evaluate_gate(node, json.loads(json.dumps(payload)))
    assert first["decision_hash"] == second["decision_hash"]
    assert first["routing_input_hash"] == second["routing_input_hash"]
    assert [r["edge_id"] for r in first["selected"]] == [r["edge_id"] for r in second["selected"]]


def test_a_different_verdict_changes_the_routing_hash():
    node = _review_node()
    a = rc.evaluate_gate(node, _result("PASS", verdict="PASS"))
    b = rc.evaluate_gate(node, _result("FAIL", verdict="REPAIR"))
    assert a["decision_hash"] != b["decision_hash"]


def test_fields_outside_the_projection_do_not_change_routing():
    node = _review_node()
    a = rc.evaluate_gate(node, _result("PASS", verdict="PASS", summary="one"))
    b = rc.evaluate_gate(node, _result("PASS", verdict="PASS", summary="something entirely different"))
    assert a["decision_hash"] == b["decision_hash"]


# ── legacy compilation ────────────────────────────────────────────────────────

def test_legacy_on_pass_on_fail_compiles_into_the_same_gate():
    node = {"id": "N01", "type": "IMPLEMENT", "on_pass": "N02", "on_fail": "STOP"}
    assert rc.declares_edges(node) is False
    passed = rc.evaluate_gate(node, _result("PASS"))
    assert [row["to"] for row in passed["selected"]] == ["N02"]
    failed = rc.evaluate_gate(node, _result("FAIL"))
    assert [row["to"] for row in failed["selected"]] == ["STOP"]


# ── branch lineage ────────────────────────────────────────────────────────────

def test_branch_suffixes_are_stable_and_human_readable():
    assert [rc.branch_suffix(i) for i in (1, 2, 26, 27)] == ["A", "B", "Z", "AA"]


def test_next_branch_id_skips_taken_ids():
    assert rc.next_branch_id("N03", {"N03", "N03A"}) == ("N03B", 2)


def test_mint_branch_node_carries_the_reviewer_brief_and_lineage():
    template = {"id": "N03R", "type": "REPAIR", "instructions": "template body", "acceptance": ["scoped"]}
    origin = {"id": "N03", "type": "REVIEW"}
    result = _result("FAIL", verdict="REPAIR", execution_id="EXE_x",
                     next_brief="Rebind device_id on the mobile exchange.",
                     carry_forward=["scope: auth/session/ only"])
    branch = rc.mint_branch_node(template, origin_node=origin, origin_result=result,
                                 edge={"edge_id": "E_REPAIR"}, known_ids={"N03", "N03R"})
    assert branch["id"] == "N03A"
    assert branch["type"] == "REPAIR"
    assert branch["depends_on"] == ["N03"]
    assert "Rebind device_id on the mobile exchange." in branch["instructions"]
    assert "template body" in branch["instructions"]
    assert branch["lineage"]["origin_node_id"] == "N03"
    assert branch["lineage"]["template_id"] == "N03R"
    assert branch["lineage"]["origin_execution_id"] == "EXE_x"
    assert branch["lineage"]["branch_index"] == 1
    # the template itself is untouched: lineage never mutates shared state
    assert template["instructions"] == "template body"
    assert "lineage" not in template


def test_carry_forward_is_not_folded_into_acceptance_criteria():
    """V0.1.1. Two different questions, two different fields.

    `acceptance` says what this node must satisfy to be accepted.
    `carry_forward` says what constraint the path imposed on it. V0.1 appended
    the second onto the first, leaving both runtime and UX to separate them by
    matching a prose prefix.
    """
    template = {"id": "N03R", "type": "REPAIR", "instructions": "template body",
                "acceptance": ["Every point of the inherited brief is addressed."]}
    result = _result("FAIL", verdict="REPAIR", next_brief="Rebind device_id.",
                     carry_forward=["scope: auth/session/ only", "no merge to canonical"])
    branch = rc.mint_branch_node(template, origin_node={"id": "N03", "type": "REVIEW"},
                                 origin_result=result, edge={"edge_id": "E_REPAIR"},
                                 known_ids={"N03", "N03R"})
    # acceptance is inherited verbatim from the template
    assert branch["acceptance"] == ["Every point of the inherited brief is addressed."]
    # the constraints keep their own first-class field
    assert branch["carry_forward"] == ["scope: auth/session/ only", "no merge to canonical"]
    assert branch["lineage"]["carry_forward"] == branch["carry_forward"]
    # and the model still sees them, under their own labelled heading
    assert "CARRY_FORWARD" in branch["instructions"]
    assert "scope: auth/session/ only" in branch["instructions"]
    assert "INHERITED_BRIEF" in branch["instructions"]
    # nothing named acceptance leaked a carry_forward string
    assert not any("carry_forward" in item for item in branch["acceptance"])


def test_branch_projection_is_drawable_before_the_branch_runs():
    template = {"id": "N03R", "type": "REPAIR", "role": "CODE_IMPLEMENTER",
                "instructions": "template body", "acceptance": ["scoped"],
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_N03R_CONTINUE", "to": "N09", "when": None,
                           "kind": "CONTINUE", "label": "repaired"}]}
    result = _result("FAIL", verdict="REPAIR", next_brief="Rebind device_id.",
                     carry_forward=["scope: auth/session/ only"])
    branch = rc.mint_branch_node(template, origin_node={"id": "N03", "type": "REVIEW"},
                                 origin_result=result, edge={"edge_id": "E_REPAIR"},
                                 known_ids={"N03", "N03R"})
    projection = rc.branch_projection(branch)
    assert projection["node_id"] == "N03A"
    assert projection["node_type"] == "REPAIR"
    assert projection["node_kind"] == rc.MINTED_REPAIR_BRANCH
    assert projection["depends_on"] == ["N03"]
    assert projection["carry_forward"] == ["scope: auth/session/ only"]
    assert projection["acceptance"] == ["scoped"]
    assert projection["lineage"]["origin_node_id"] == "N03"
    assert projection["lineage"]["node_type"] == "REPAIR"
    assert [edge["edge_id"] for edge in projection["edges"]] == ["E_N03R_CONTINUE"]


def test_mint_branch_node_refuses_a_non_repair_template():
    with pytest.raises(rc.RoutingContractError):
        rc.mint_branch_node({"id": "N04", "type": "IMPLEMENT"}, origin_node={"id": "N03"},
                            origin_result=_result("FAIL"), edge={"edge_id": "E"}, known_ids=set())


def test_carry_forward_accumulates_in_order_without_duplicates():
    results = [
        {"carry_forward": ["a", "b"]},
        {"carry_forward": ["b", "c"]},
        {},
    ]
    assert rc.accumulate_carry_forward(results) == ["a", "b", "c"]


# ── journal ───────────────────────────────────────────────────────────────────

def test_journal_appends_in_order_and_reads_back(tmp_path):
    journal = rc.RoutingJournal.for_run("AAW_TEST", tmp_path, workflow_id="WF")
    decision = rc.evaluate_gate(_review_node(), _result("FAIL", verdict="REPAIR"))
    rc.record_decision(journal, decision)
    rows = journal.read()
    assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
    assert [row["event_type"] for row in rows] == [
        rc.GATE_EVALUATED, rc.EDGE_SELECTED, rc.EDGE_HELD, rc.EDGE_HELD,
    ]
    assert journal.by_type(rc.EDGE_SELECTED)[0]["payload"]["edge_id"] == "E_REPAIR"
    held = {row["payload"]["edge_id"] for row in journal.by_type(rc.EDGE_HELD)}
    assert held == {"E_PASS", "E_BLOCKED"}


def test_journal_records_no_route_explicitly(tmp_path):
    journal = rc.RoutingJournal.for_run("AAW_TEST_NR", tmp_path)
    node = _review_node(edges=[{"edge_id": "E_PASS", "to": "N04", "when": {"verdict": "PASS"}, "kind": "CONTINUE"}])
    rc.record_decision(journal, rc.evaluate_gate(node, _result("FAIL")))
    unresolved = journal.by_type(rc.ROUTE_UNRESOLVED)
    assert len(unresolved) == 1
    assert unresolved[0]["payload"]["status"] == rc.NO_ROUTE


def test_journal_rejects_an_unknown_event_type(tmp_path):
    journal = rc.RoutingJournal.for_run("AAW_TEST_BAD", tmp_path)
    with pytest.raises(rc.RoutingContractError):
        journal.append("SOMETHING_ELSE")


# ── schema guardrails: the contract stays small ───────────────────────────────

def _workflow(edges):
    return {
        "workflow_id": "T", "version": "0.1", "description": "t", "goal": None, "start_node": "N01",
        "workspace_policy": {"isolated_worktree_required": True, "main_merge_allowed": False},
        "limits": {"max_nodes": 4, "max_repair_cycles": 1, "max_wall_time_minutes": 5,
                   "max_llm_calls": 3, "max_token_budget": None},
        "nodes": [
            {"id": "N01", "type": "IMPLEMENT", "depends_on": [], "run_if": "ALWAYS",
             "role": "CODE_IMPLEMENTER", "model": "gpt-5.6-sol", "effort": "medium",
             "instructions": "x", "acceptance": [], "on_pass": None, "on_fail": None,
             "routing": "FIRST_MATCH", "edges": edges},
            {"id": "N09", "type": "HUMAN_GATE", "depends_on": [], "run_if": "ON_TRANSITION",
             "role": None, "model": None, "effort": None, "instructions": "", "acceptance": [],
             "on_pass": None, "on_fail": None},
        ],
    }


def test_schema_rejects_an_expression_predicate():
    with pytest.raises(WorkflowValidationError, match="no expression DSL"):
        validate_workflow(_workflow([{"edge_id": "E", "to": "N09", "when": {"expr": "x > 1"}, "kind": "CONTINUE"}]))


def test_schema_rejects_a_repair_edge_that_does_not_target_a_repair_template():
    with pytest.raises(WorkflowValidationError, match="REPAIR template"):
        validate_workflow(_workflow([{"edge_id": "E", "to": "N09", "when": None, "kind": "REPAIR"}]))


def test_schema_rejects_an_unconditional_edge_that_shadows_later_edges():
    with pytest.raises(WorkflowValidationError, match="declared last"):
        validate_workflow(_workflow([
            {"edge_id": "E_ANY", "to": "N09", "when": None, "kind": "CONTINUE"},
            {"edge_id": "E_PASS", "to": "N09", "when": {"verdict": "PASS"}, "kind": "CONTINUE"},
        ]))


def test_schema_rejects_a_cycle_in_a_routing_contract_workflow():
    data = _workflow([{"edge_id": "E", "to": "N09", "when": None, "kind": "CONTINUE"}])
    data["nodes"][1] = {
        "id": "N09", "type": "HUMAN_GATE", "depends_on": [], "run_if": "ON_TRANSITION",
        "role": None, "model": None, "effort": None, "instructions": "", "acceptance": [],
        "on_pass": None, "on_fail": None, "routing": "FIRST_MATCH",
        "edges": [{"edge_id": "E_BACK", "to": "N01", "when": None, "kind": "CONTINUE"}],
    }
    with pytest.raises(WorkflowValidationError, match="must be acyclic"):
        validate_workflow(data)


def test_node_result_accepts_the_handoff_fields():
    payload = _result("FAIL", verdict="REPAIR", next_brief="do the thing",
                      carry_forward=["scope"], artifacts=["a.json"])
    validated = validate_node_result(payload, "N03", "REVIEW")
    assert validated["verdict"] == "REPAIR"
    assert validated["next_brief"] == "do the thing"


def test_node_result_rejects_an_empty_next_brief():
    with pytest.raises(WorkflowValidationError, match="next_brief"):
        validate_node_result(_result("FAIL", next_brief="   "), "N03", "REVIEW")
