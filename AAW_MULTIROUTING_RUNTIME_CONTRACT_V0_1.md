# AAW MULTIROUTING RUNTIME CONTRACT V0.1

Status: **FROZEN**. Contract id: `AAW_MULTIROUTING_RUNTIME_CONTRACT_V0.1`.

Model: `TASK/REVIEW → structured result → deterministic GATE → selected edge(s) → downstream node(s)`.
The reviewer never steers the graph. It emits a structured result; a pure gate decides the route.

---

## 1. Reuse-first inspection

Inspected: `workflow_runner.py` (1046 lines), `workflow_schema.py`, `execution_contract.py`,
`execution_ledger.py`, `local_preprocess.py`, `model_catalog.py`, `WORKFLOWS/IMPLEMENT_REVIEW_REPAIR_V1.json`,
`IMPLEMENTER_PROFILES.json`, plus the existing test suite.

| Concern | Existing implementation | Verdict |
|---|---|---|
| Workflow runner | `workflow_runner.execute()` — single-cursor `while state["current_node"] in by_id` loop | **reuse**, extend the cursor into a frontier |
| Node types | `IMPLEMENT / REVIEW / MACHINE_GATE / REPAIR / HUMAN_GATE / FINAL_GATE` (`workflow_schema.NODE_TYPES`) | **reuse unchanged** |
| Outcomes | `PASS / FAIL / BLOCKED / INVALID` (`OUTCOMES`) | **reuse**, add an orthogonal `verdict` |
| Structured output | `result_schema()` JSON Schema + `validate_node_result()` | **reuse**, extend additively |
| Routing | `edge = "on_pass" if outcome == "PASS" else "on_fail"` — binary, single-target | **extend** — this is the gap |
| Repair | graph cycle back onto the same node id, bounded by `limits.max_repair_cycles`; artifacts disambiguated by `__cycle_NN` | **replace mechanism, keep guardrail** |
| Gates | `MACHINE_GATE` runs argv and maps rc → PASS/FAIL/BLOCKED; `HUMAN_GATE` mints a candidate and sets `WAITING_FOR_HUMAN` | **reuse unchanged** |
| Model bindings | `resolve_execution_plan()` freezes `workflow_bindings.json` before the first LLM node | **reuse**, inherit for minted branches |
| Run / execution identity | `AAW_<ts>_<hex8>`, `EXE_<uuid4>`, `CAN_`, `HDE_` (`execution_contract.py`) | **reuse unchanged** |
| Lifecycle events | `EXECUTION_INTENT / STARTED / CLOSED`, `COMMIT_RECORDED`, `HUMAN_DECISION_RECORDED` | **reuse unchanged** |
| Data passing | `node_package()` — goal, previous results, changed files, test evidence, open issues, repair scope | **reuse**, extend additively |
| State | `workflow_state.json`, atomic via `save_state()` | **reuse**, extend additively |

### Gaps this contract closes

1. No conditional edges and no fan-out — one cursor, two hardcoded transitions.
2. Everything not `PASS` collapsed to `on_fail`; `PASS`/`REPAIR`/`BLOCKED` were not separable at the edge.
3. REPAIR was a graph cycle: a second pass over the same node id, i.e. a retry loop with a counter.
4. No `next_brief` / `carry_forward` / `artifacts` in the result contract.
5. No routing events at all. `execution_ledger.py` states in its own module docstring that it is
   *"not event sourcing, not execution authority, and not a place from which workflow state is
   reconstructed"* — so graph semantics could not be forced into it.
6. `NO_ROUTE` existed only implicitly, as `INVALID: ambiguous transition`.

### Decision: EXTEND. No second engine.

`workflow_runner.execute()` remains the only execution loop. Added: one pure decision module
(`routing_contract.py`) and roughly 60 lines of wiring inside the existing loop. Legacy
`on_pass`/`on_fail` nodes are **compiled into edges** and evaluated by the same gate, so there is
exactly one router in the system. Verified by `test_legacy_workflow_uses_the_same_router` and by
the untouched-and-passing pre-existing suite.

---

## 2. Frozen contract

### 2.1 Two axes, deliberately separate

| Field | Grade | Owner | Values |
|---|---|---|---|
| `outcome` | execution | runner / adapter | `PASS` `FAIL` `BLOCKED` `INVALID` |
| `verdict` | work | node result | `PASS` `REPAIR` `BLOCKED` |

`outcome` answers *did the invocation produce a usable result*. `verdict` answers *what does the
result say about the work*. A node may state `verdict` explicitly; otherwise it is derived:

```
PASS -> PASS      FAIL -> REPAIR      BLOCKED -> BLOCKED      INVALID -> (none)
```

`INVALID` has no verdict by design: it is fail-closed **upstream** of the gate and can never route.

### 2.2 Result contract (additive)

Existing required fields are unchanged. Added, all optional:

```jsonc
{
  "verdict": "PASS | REPAIR | BLOCKED | null",
  "next_brief": "string | null",      // mandatory in practice for a REPAIR verdict
  "carry_forward": ["string"],        // constraints that must survive downstream
  "artifacts": ["string"]             // evidence paths a later node needs
}
```

A node that emits none of these routes on `outcome` exactly as before.

### 2.3 Edge contract

```jsonc
"routing": "FIRST_MATCH" | "ALL_MATCHES",
"edges": [
  { "edge_id": "E_N03_PASS", "to": "N04",  "when": {"verdict": "PASS"}, "kind": "CONTINUE", "label": "PASS" },
  { "edge_id": "E_N03_REPAIR","to": "N03R", "when": {"verdict": "REPAIR"},"kind": "REPAIR"  },
  { "edge_id": "E_N03_BLOCKED","to": "N09", "when": {"verdict": "BLOCKED"},"kind": "FALLBACK"}
]
```

* `kind: CONTINUE | FALLBACK` — `to` is the node to enter.
* `kind: REPAIR` — `to` is a **template** node of type `REPAIR`. It is never entered. The runner
  mints a lineage child from it.
* `to` may also be the existing terminals `STOP` or `HUMAN_REQUIRED`.
* A node either declares `edges` or keeps `on_pass`/`on_fail`. Both compile to the same structure.

### 2.4 Predicates — closed set of four, no DSL

| Key | Meaning |
|---|---|
| `verdict` | equals one of `PASS` `REPAIR` `BLOCKED` |
| `outcome` | equals one of `PASS` `FAIL` `BLOCKED` `INVALID` |
| `has_findings` | `len(findings) > 0` equals the given boolean |
| `min_severity` | the highest finding severity is at least this rung of `LOW < MEDIUM < HIGH < CRITICAL` |

Keys inside one `when` are **ANDed**. `when: null` is unconditional. There is no `or`, no nesting,
no expression syntax — the schema rejects any other key with *"no expression DSL"*.
An unrecognised severity string ranks as `CRITICAL` (escalates, never dampens) and is listed in the
decision's `projection.unknown_severities`.

### 2.5 Selection rules — complete

1. An edge is **matched** when its `when` block matches the result projection.
2. An edge is **eligible** unless `verdict == BLOCKED` and `kind == CONTINUE`.
   *A blocked node never starts downstream work on its own.*
3. `FIRST_MATCH` selects the first matched+eligible edge in declaration order.
   `ALL_MATCHES` selects every matched+eligible edge — this is fan-out.
4. Zero selected edges is **`NO_ROUTE`**: the run stops fail-closed with status `NO_ROUTE`, never
   silently and never by guessing.
5. Every non-selected edge is recorded as **held**, with one of exactly three reasons:
   `PREDICATE_NOT_MATCHED`, `NOT_FIRST_MATCH`, `BLOCKED_CANNOT_CONTINUE`.

### 2.6 Branch lineage — what replaces the retry loop

A selected `REPAIR` edge mints a new node instead of re-entering an old one:

```
N03 (REVIEW, verdict=REPAIR)  --E_N03_REPAIR-->  template N03R  =>  minted N03A
```

* id `<origin_id><A|B|C…>`, so a second repair from `N03` is `N03B` — distinct id, distinct artifacts.
* `depends_on = [origin]`, `run_if = ON_TRANSITION`.
* the reviewer's `next_brief` is appended to the node's `instructions` as `INHERITED_BRIEF`
  **and** surfaced structurally in `node_package`.
* `carry_forward` entries remain a distinct inherited-constraint field in
  `node_package`; they are not appended to or otherwise alter `acceptance`.
* `lineage` records `template_id`, `origin_node_id`, `origin_execution_id`, `origin_verdict`,
  `selected_edge_id`, `branch_index`, `inherited_brief`, `carry_forward`, `created_at`.
* the frozen binding is inherited from the template, marked `binding_source: INHERITED_FROM_TEMPLATE`.

The old path is never overwritten. **Existing guardrails are kept unchanged**:
`limits.max_repair_cycles` still caps how many repair nodes may run, `limits.max_nodes` now also
caps minting, and `limits.max_llm_calls` / `max_wall_time_minutes` are untouched.

Contract workflows must be **acyclic** — a declared cycle would reintroduce the loop that lineage
removes. The schema enforces this only for contract workflows; legacy cyclic workflows keep the
original REPAIR-in-cycle rule.

### 2.7 Frontier

`state["current_node"]` (single cursor) is retained as the head of a new `state["frontier"]` list, so
every existing reader of `workflow_state.json` keeps working. Fan-out appends; the frontier is FIFO
and edges are evaluated in declaration order, so execution order is deterministic.

**A node is entered at most once per run.** A selected edge whose target is already completed or
already queued is recorded in `state["routing"]["dedup"]` with reason `ALREADY_COMPLETED` /
`ALREADY_QUEUED`. This is frontier dedup, **not a join**: nothing is merged. Rejoin semantics are
out of scope.

### 2.8 Event contract

The execution ledger already owns the process lifecycle and refuses semantic workflow state, so
graph facts live in a separate append-only journal:
`03_STATS/<AAW_RUN_ID>/WORKFLOW/ROUTING/routing_events.jsonl`, schema `AAW_ROUTING_JOURNAL_V0.1`.

| Event | Emitted when | Key payload |
|---|---|---|
| `NODE_STARTED` | runner commits to entering a node | `node_type`, `lineage`, `pending_frontier` |
| `NODE_COMPLETED` | validated structured result recorded | `outcome`, `verdict`, `execution_id`, `artifact`, `next_brief`, `carry_forward`, `artifacts` |
| `NODE_FAILED` | `INVALID`, or `BLOCKED` on a legacy node | `outcome`, `routable: false` |
| `GATE_EVALUATED` | one gate evaluation | full `candidates` array with per-predicate trace, `decision_hash` |
| `EDGE_SELECTED` | one per selected edge | `edge_id`, `to`, `kind`, `when`, `label` |
| `EDGE_HELD` | one per non-selected edge | the same, plus `hold_reason` |
| `BRANCH_CREATED` | repair lineage minted | `template_id`, `origin_node_id`, `lineage`, `inherited_brief`, `binding` |
| `HUMAN_DECISION_REQUIRED` | HUMAN_GATE / FINAL_GATE reached | `candidate_id`, `held_frontier` |
| `ROUTE_UNRESOLVED` | `NO_ROUTE` | `status`, `verdict`, `candidates_considered` |

Names follow the objective; `ROUTE_UNRESOLVED` is the one adaptation, so the journal keeps the
project's `<NOUN>_<PARTICIPLE>` convention while the *status token* stays `NO_ROUTE`.
`HUMAN_DECISION_REQUIRED` deliberately pairs with the ledger's existing `HUMAN_DECISION_RECORDED`.

**Authority:** the durable artifact `WORKFLOW/<node>__GATE__decision.json`
(`AAW_GATE_DECISION_V0.1`) is authoritative for a decision; the journal is the ordered index over
those artifacts. This matches the project's existing doctrine that raw artifacts, not streams, hold
semantic state.

### 2.9 Explicitly frozen out of V0.1

Expression DSL · merge/rejoin engine · dynamic planner mutation · loops beyond the bounded repair
guardrail · parallel execution · edge weights or priorities beyond declaration order · cross-run
routing.

---

## 3. Implementation

| File | Change |
|---|---|
| `routing_contract.py` | **new**, ~430 lines. Pure gate, predicates, lineage minting, journal, `graph_projection()`. No execution entry point (asserted by test). |
| `workflow_schema.py` | `+~90` lines: edge/predicate validation, reachability through edges, DAG rule for contract workflows, optional result fields. |
| `workflow_runner.py` | `+~110` lines: `apply_gate_decision()`, frontier loop, gate call, journal wiring, `node_package` handoff fields, `NO_ROUTE`/`COMPLETED` statuses. |
| `WORKFLOWS/MULTIROUTING_SLICE_V1.json` | **new** vertical-slice workflow, 8 nodes. |
| `test_routing_contract.py` | **new**, 33 tests — the pure contract. |
| `test_multirouting_slice.py` | **new**, 11 tests — the contract on the real runner. |
| `multirouting_evidence.py` | **new** reproducible evidence harness. |

The vertical slice graph:

```
N01 IMPLEMENT ──▶ N02 IMPLEMENT ──▶ N03 REVIEW ─┬─ PASS ────▶ N04 MACHINE_GATE ─┬─▶ N05 IMPLEMENT ─┐
                                                │                  (ALL_MATCHES) ├─▶ N06 IMPLEMENT ─┤
                                                │                                └─ FAIL ──────────┤
                                                ├─ REPAIR ──▶ N03R template ⇒ mints N03A ──────────┤
                                                └─ BLOCKED ───────────────────────────────────────▶ N09 HUMAN_GATE
```

---

## 4. Acceptance

| # | Criterion | Evidence |
|---|---|---|
| 1 | PASS routing works | `test_pass_routes_forward_and_fans_out` — executed `N01 N02 N03 N04 N05 N06 N09` |
| 2 | REPAIR creates explicit lineage, forwards brief/context | `test_repair_creates_an_explicit_branch_lineage` — `N03A` minted, `N03R` never entered, `INHERITED_BRIEF` + `CARRY_FORWARD` + `UPSTREAM_ARTIFACTS` verified inside the real `node_package` |
| 3 | BLOCKED starts no downstream | `test_blocked_reaches_the_human_gate_and_nothing_else` — executed `N01 N02 N03 N09`; `E_N03_PASS` held as `BLOCKED_CANNOT_CONTINUE` |
| 4 | ALL_MATCHES runs two branches | same test as 1 — `E_N04_HARDENING` + `E_N04_MIGRATION` both selected, both nodes ran |
| 5 | No match ⇒ deterministic NO_ROUTE | `test_no_matching_edge_is_fail_closed` — gate timeout ⇒ `status: NO_ROUTE`, frontier empty, `N05/N06/N09` never entered |
| 6 | Identical output ⇒ identical routing | `test_identical_structured_output_routes_identically`; also visible in evidence: `N01` `decision_hash = d5cb2a0cda38f164…` is byte-identical across all four independent runs |
| 7 | Evidence shows taken *and* untaken path | `test_evidence_names_the_path_not_taken` — every candidate carries `matched`, `eligible` and a per-predicate `trace`; `EDGE_SELECTED` / `EDGE_HELD` are separate events |
| 8 | No second engine | `test_legacy_workflow_uses_the_same_router` (legacy `on_pass`/`on_fail` compiles to `…:LEGACY_PASS` edges through the same gate), `test_the_contract_layer_exposes_no_execution_entry_point`, and 44/44 pre-existing tests unchanged and passing |

**Suite: 107 passed, 0 failed** (`test_routing_contract`, `test_multirouting_slice`,
`test_execution_contract`, `test_execution_ledger`, `test_execution_ledger_integration`,
`test_workflow_execution_identity`, `test_single_task_execution_identity`, `test_local_preprocess`).

Run evidence: `python multirouting_evidence.py` → `MULTIROUTING_EVIDENCE/{pass_fanout,
repair_branch, blocked_human, no_route}/` plus `SUMMARY.json`.

---

## 5. What the canvas UX now needs to connect

The runtime already emits graph facts; the frontend must never parse prose. Read these, in order.

**Available today — wire straight through**

1. `routing_contract.graph_projection(state, journal)` — one call returning run status, frontier,
   completed nodes with `outcome`/`verdict`/`lineage`, all routing decisions and the full event
   stream. This is the intended single read for a canvas.
2. `workflow_state.json → routing.decisions[]` — per node: `routing_mode`, `verdict`, `selected[]`,
   `held[{edge_id, to, hold_reason}]`, `no_route`, `decision_hash`, artifact path.
   Renders selected vs. **held** edges directly; no inference.
3. `workflow_state.json → routing.lineage{}` — branch id → `origin_node_id`, `template_id`,
   `branch_index`, `inherited_brief`, `carry_forward`. This is the repair-branch card.
4. `WORKFLOW/ROUTING/routing_events.jsonl` — ordered, append-only, `sequence`-numbered. Tail it for
   live RUN mode; each line is self-describing.
5. `WORKFLOW/<node>__GATE__decision.json` — the "why" panel: per-candidate `matched`, `eligible`
   and per-predicate `trace` (`expected` vs `actual`).
6. `workflow_state.json → routing.dedup[]` — why an edge pointed somewhere that did not re-run.
7. Existing, unchanged: `workflow_bindings.json` (frozen bindings, for the collapsed
   Binding & telemetry block), `EXECUTIONS/` descriptors, `LEDGER/execution_events.jsonl`
   (process lifecycle), `candidate.json` + `apply_human_verdict()` (HUMAN_GATE).

**Not built yet — required before the canvas is fully live**

| # | Gap | Note |
|---|---|---|
| U1 | **Read-only run API / file watcher.** Today `graph_projection` is a Python call. The UI needs a process boundary: a small `--watch` emitter or an HTTP/IPC read endpoint over the journal. | no new semantics, transport only |
| U2 | **Static graph projection for BUILD mode.** The UI must render a workflow *before* a run: node ids, types, declared edges, `when`, `kind`, `label`. Add `routing_contract.workflow_projection(workflow)`. | trivial, pure |
| U3 | **Edge label and layout hints.** `label` exists and is carried through; there is no `x`/`y`. Decide whether layout is UI-owned (recommended) or persisted in the workflow JSON. | decision, not code |
| U4 | **Write path for BUILD.** Nothing in this contract mutates a workflow file. Canvas editing needs a validated `save_workflow()` that runs `validate_workflow` before writing. | must reuse the existing validator |
| U5 | **Planner ghost subgraph.** The UI's `Plan from here` produces a proposal. There is no proposal representation in the contract, by design. V0.2 should add a `PROPOSED` node/edge state that `Accept` promotes. | explicitly deferred |
| U6 | **Explicit merge of a repair branch.** `N03A` currently ends at the human gate. Re-review/rejoin is the deferred merge engine and the canvas must not imply it exists yet. | explicitly deferred |
| U7 | **Live node progress.** `NODE_STARTED` → `NODE_COMPLETED` is coarse. Sub-node progress would come from the ledger's `EXECUTION_STARTED`, joined on `execution_id`. | join, not new events |
| U8 | **Cancellation.** No cancel/stop path exists in the runner. The canvas Stop button has nothing to call. | new runtime capability |

**Contract guarantees the UI may rely on:** every routing decision is reproducible from its stored
inputs (`routing_input_hash`); every edge in a run is either selected or held with one of exactly
three reasons; every repair attempt is a distinct node id with its own artifacts; `NO_ROUTE` is an
explicit terminal status, never an empty frontier.
