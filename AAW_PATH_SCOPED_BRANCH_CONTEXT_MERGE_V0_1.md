# AAW PATH-SCOPED BRANCH CONTEXT + MERGE/REJOIN V0.1

Status: **FROZEN**. Contract id: `AAW_PATH_SCOPED_BRANCH_CONTEXT_MERGE_V0.1`.

Model: every node inherits context from its own **executed ancestry** — a genuine DAG, with a real
multi-parent union at a declared `MERGE` node — never from the run's global result list. A `MERGE`
node is the only way two branches' context may legally combine, and it always says, explicitly and
per source, what it combined.

---

## 1. Reuse-first inspection / previous failure mode

Inspected: `workflow_runner.py`, `routing_contract.py`, `workflow_schema.py`, `aaw_bridge.py`,
`WORKFLOWS/MULTIROUTING_SLICE_V1.json`, `test_multirouting_slice.py`, `test_aaw_bridge.py`.

| Concern | Existing implementation | Verdict |
|---|---|---|
| Execution loop | `workflow_runner.execute()` — one FIFO `state["frontier"]`, one node at a time | **reuse unchanged** — no second scheduler |
| Routing | `routing_contract.evaluate_gate()` / `apply_gate_decision()` | **reuse unchanged** — no second router |
| Context passing | `node_package()` built from `state["node_results"]`, the *entire run's* results | **the gap** — run-global, not path-scoped |
| Convergence | Two edges targeting one node: first FIFO arrival wins, the rest silently dropped (`state["routing"]["dedup"]`, reason `ALREADY_QUEUED`/`ALREADY_COMPLETED`) | **reuse unchanged for non-MERGE nodes** — this is deliberate frontier dedup, not a join, and stays exactly as documented and tested |
| Repair lineage | `mint_branch_node()` deep-copies the template's edges verbatim onto every minted branch | **reuse unchanged** |
| Node table | `workflow_schema.NODE_TYPES` | **extend** — add `MERGE` |

### The proven leak

`aaw_bridge.py`'s own wire contract stated this verbatim, before this contract existed:

> `"merge_prerequisite": "path-scoped carry_forward is a prerequisite for future branch merge/rejoin;
> accumulate_carry_forward is currently run-global, not per-lineage"`

Concretely: `node_package()` called `routing_contract.accumulate_carry_forward(state["node_results"])`
— the flattened list of *every* result the run had produced so far, in FIFO completion order, with no
regard for which of them actually causally preceded the node being packaged. Two sibling branches of
an `ALL_MATCHES` fan-out (e.g. `MULTIROUTING_SLICE_V1.json`'s `N05`/`N06`) therefore contaminated each
other: whichever branch happened to finish *later* saw the other's `carry_forward`, `artifacts`, and
`findings` — a scheduling accident promoted to semantic content. `aaw_bridge.py` also listed `"repair
branch merge/rejoin"` as explicitly deferred, for the same reason: there was no way to require two
branches to actually converge, only an accidental single-entry race with the loser thrown away.

### Decision: EXTEND. No second engine, no second scheduler.

Added: path-scoped ancestry bookkeeping (`state["routing"]["arrivals"]`) and one new node type
(`MERGE`) with its own deterministic runtime primitive (`execute_merge()`), wired into the existing
frontier loop and the existing `evaluate_gate`/`apply_gate_decision` pipeline. Verified by the
untouched, still-passing pre-existing suite (`test_multirouting_slice.py` in particular, whose
non-MERGE dedup behavior is pinned exactly as before).

---

## 2. Path-scoped ancestry (the general fix, not merge-specific)

Every node records **how it was actually entered**: `state["routing"]["arrivals"][node_id]` is a list
of arrival records, each `{arrival_id, edge_id, source_node_id, source_execution_id,
lineage_template_id, lineage_branch_index, lineage_origin_node_id, sequence, arrived_at, status,
late}`. An arrival is classified against the slot's existing history:

* **`ACCEPTED`** — the (at most one, per slot) causal parent.
* **`DUPLICATE`** — the identical concrete source repeating (a replay).
* **`STALE_LINEAGE`** — a *different* source re-using an already-resolved slot (a later repair
  descendant reusing a template's `edge_id` after an earlier sibling already filled it).
* **`LATE`** — a slot that would otherwise be `ACCEPTED` (genuinely new), but the `MERGE` it belongs to
  already settled under `ANY_COMPLETED`. Recorded honestly; never a causal parent.

`routing_contract.ancestry_of(node_id, arrivals)` is the pure, memoized walk: `{node_id} ∪ ancestry_of(parent)`
for every `ACCEPTED` parent. An ordinary node has exactly one `ACCEPTED` arrival, so this degenerates
to the single-parent chain a linear workflow always had (**no behavior change there**). A `MERGE` node
can have one `ACCEPTED` arrival per resolved slot — a **genuine multi-parent DAG union**, not an
opaque merge boundary, so a node two hops downstream of a merge still resolves correct, real ancestry.

`workflow_runner.node_package()` filters `state["node_results"]` to `path_scoped_results(results,
ancestry_of(node_id, arrivals))` — this is the single change that fixes the leak for **every** node,
not only ones adjacent to a `MERGE`.

A resumed run (`plan_resume` / `_seed_from_resume`) seeds one synthetic `ACCEPTED` arrival per
inherited node, all attached to the resume's `from_node` — treating a resume as, in effect, an
implicit merge of everything the source run already settled. This is the same mechanism a real
`MERGE` uses, not a special case.

---

## 3. `MERGE` node contract

```jsonc
{
  "id": "M08", "type": "MERGE",
  "merge_policy": "ALL_REQUIRED",              // or "ANY_COMPLETED"
  "expected_incoming": ["E_N05_CONTINUE", "E_N06_CONTINUE"],
  "depends_on": [], "run_if": "ON_TRANSITION", "role": null, "model": null, "effort": null,
  "instructions": "...", "acceptance": [...], "on_pass": null, "on_fail": null,
  "edges": [{"edge_id": "E_M08_CONTINUE", "to": "N09", "when": null, "kind": "CONTINUE"}]
}
```

* **Slots are named by `edge_id`, not by source node id.** A REPAIR template's edges are copied
  verbatim onto every minted branch, so `"E_N03R_CONTINUE"` names "whichever concrete descendant
  fires this" as one logical slot — this is the entire mechanism behind repair-lineage participation;
  no special-casing exists anywhere else.
* **Closed input set.** Validation requires `set(expected_incoming) == {edge_id for every edge in the
  whole graph whose `to` is this node}` — **exactly**, not a subset. An undeclared edge targeting a
  `MERGE`, or a declared slot backed by nothing, is a schema error. Optional/partial incoming sets are
  explicitly deferred (§8), never inferred.
* **No geometry inference.** `expected_incoming` is the only source of truth for what a `MERGE` waits
  on; nothing is derived from graph shape.
* `merge_policy`/`expected_incoming` are in `routing_contract.SEMANTIC_NODE_FIELDS`, so merge
  configuration affects `semantic_hash` (workflow-definition identity); runtime arrival data is never
  part of a workflow *definition* and cannot reach that hash by construction.

### Eager runtime state (no lazy hiding)

Every declared `MERGE` gets `state["routing"]["merges"][id] = {policy, expected, status: "WAITING",
resolved_edge_id: None, merge_resolution_hash: None}` **at run start**, before any branch has arrived.
`MERGE` nodes are always statically declared, never minted, so this scan is total. `"0/N arrived"` is
observable from the run's first saved frame, and a `MERGE` nothing ever reaches cannot silently vanish
from the drain-time scan (§4) the way a lazily-created entry could.

---

## 4. Terminal semantics

| Input state | `ALL_REQUIRED` | `ANY_COMPLETED` |
|---|---|---|
| A branch reaches `PASS` and selects the expected edge | slot `ACCEPTED`; `MERGE_WAITING` until all slots fill, then `MERGE_READY` | first one: slot `ACCEPTED`, `MERGE_READY` immediately |
| A second branch's `PASS` arrives after resolution | n/a (all slots already required) | recorded `LATE`; never mutates the settled result |
| The same concrete node's edge fires twice (replay) | `DUPLICATE`; ignored | `DUPLICATE`; ignored |
| A different lineage descendant reuses an already-filled slot's `edge_id` | `STALE_LINEAGE`; ignored (and `LATE` if the merge already settled) | same |
| A single-shot (non-REPAIR) node **holds** an edge that is an expected slot | **fast-closed `BLOCKED`** immediately (`apply_gate_decision`) — that slot can never arrive again | same |
| A REPAIR-template-owned slot is simply never minted this run | not caught by the fast path (no node ever "holds" the template's own edge) — caught by the **drain-time scan**: `BLOCKED` before the run may claim `COMPLETED` | same |
| `BLOCKED`/`FAILED` branch whose own gate routes elsewhere (never toward the merge) | that expected slot never arrives; caught by fast path (ordinary node) or drain-time (template-owned) | same |
| `CANCELLED` run | the frontier is abandoned (existing `RunCancelled` handling); an unresolved `MERGE` is reported honestly in `state["routing"]["merges"]`, not silently dropped | same |
| Missing branch (upstream subtree never reached this run) | drain-time `BLOCKED` | drain-time `BLOCKED` if zero slots ever arrived |
| `HUMAN_GATE` reached before the merge resolves | the run stops at `WAITING_FOR_HUMAN` (existing early-return); the unresolved `MERGE` is left exactly as `WAITING`/partially-filled in `state["routing"]["merges"]` — this is an honest wait, not a false `COMPLETED`, and is out of scope for drain-time enforcement (§8) | same |

Both fail-closed paths (`_fail_closed_merge`) emit `MERGE_BLOCKED` and raise `WorkflowStop("BLOCKED",
...)` naming the missing `expected_incoming` members — the same philosophy `NO_ROUTE` already uses:
never a silent stop, never a false `COMPLETED`.

### Order-independence

`ALL_REQUIRED`'s selected-slot set, `incoming` mapping, merged `carry_forward`/`artifacts`, and
`merge_resolution_hash` are computed by iterating **`expected_incoming` in its declared order**, never
arrival order — reversed branch-completion order therefore produces byte-identical merge content and
hash (`test_all_required_result_is_independent_of_completion_order`). `ANY_COMPLETED`'s winner is the
accepted arrival with the lowest run-wide `sequence`; because this runner has one FIFO frontier and no
real concurrency, "first accepted" is fully deterministic for a fixed graph and fixed branch outcomes.

---

## 5. Merge resolution hash (execution identity)

```
merge_resolution_hash = canonical_hash({
  contract, policy,
  expected_incoming,                 # declared order, fixed
  slots: [
    {edge_id, source_node_id, lineage_template_id, lineage_branch_index,
     content: {outcome, verdict, carry_forward, artifacts, changed_files, findings}}
    for edge_id in expected_incoming if that slot resolved
  ],
})
```

Deliberately **excluded**: `execution_id`, `provider_session_id`, every timestamp/duration, telemetry,
and journal/arrival `sequence` numbers. None of that is semantic; including it would make identity
depend on wall-clock and scheduling accidents instead of content. `merge_input_identity()` is the
narrow per-result projection that enforces this.

---

## 6. Event contract

New: `BRANCH_ARRIVED, MERGE_WAITING, MERGE_READY, MERGE_BLOCKED, MERGE_STARTED, MERGE_COMPLETED`
(`routing_contract.EVENT_TYPES`). `BRANCH_ARRIVED` fires for **every** arrival — accepted, duplicate,
stale-lineage, or late alike — so the canvas can show a rejected arrival, not only accepted ones.
`MERGE_STARTED`/`MERGE_COMPLETED` bracket the merge's own (adapter-free) execution, the same
relationship `HUMAN_DECISION_REQUIRED`/`RESOLVED` already has to the generic `NODE_STARTED`/
`NODE_COMPLETED` pair every node type already gets.

## 7. UX / runtime projection

`routing_contract.graph_projection()` gains `"merges"`: one row per declared `MERGE` (present from the
run's first frame, per §3), `{node_id, policy, expected_incoming, arrived: [...], arrived_count,
required_count, status, resolved_edge_id, merge_resolution_hash}` — `"2/3 branches arrived"` is
`arrived_count`/`required_count` directly, no journal parsing. `workflow_projection()` (BUILD mode, no
run yet) marks `is_merge`/`merge_policy`/`expected_incoming` on projected nodes. `aaw_bridge.public_contract()`
exposes `merge_policies` and `arrival_statuses`, and no longer lists `"repair branch merge/rejoin"` as
deferred.

## 8. Explicitly deferred (unchanged scope from the task brief)

* Planner graph mutation.
* N-of-M quorum / weighted-vote merge policies.
* An expression DSL for merge predicates.
* Optional / non-closed merge inputs (today's `expected_incoming` must exactly equal the graph).
* Cyclic graph execution (contract-mode workflows remain acyclic, `MERGE` included).
* Enforcing merge resolution across a `HUMAN_GATE` early-return (an honest `WAITING` state today; see
  the terminal-semantics table above).
* Distributed / multi-user execution.

`PLANNER PREREQUISITES: SATISFIED` — path-scoped context and a real, tested join primitive are both
in place; nothing in this contract stands between here and planner-driven graph mutation.
