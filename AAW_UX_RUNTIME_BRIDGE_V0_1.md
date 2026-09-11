# AAW UX RUNTIME BRIDGE V0.1

Status: **FROZEN**. Contract id: `AAW_UX_RUNTIME_BRIDGE_V0.1`.
Depends on `AAW_MULTIROUTING_RUNTIME_CONTRACT_V0.1` (+ the V0.1.1 correction in §1 below).

Objective: expose the existing routing substrate through the smallest stable boundary the
canvas-first UX needs. No routing redesign, no frontend rewrite.

```
UI  ──HTTP/SSE──▶  aaw_bridge_server  ──▶  aaw_bridge  ──▶  workflow_runner   (execution)
   (aaw-canvas-live.html)               (the boundary)  ──▶  workflow_schema  (validation)
                                                        ──▶  routing_contract (projection, gate, journal)
                                                        ──▶  workflow_layout  (visual, separate store)
                                                        ──▶  run_cancellation (real stop)
```

---

## 0. Substrate audit — verifying the previous contract

Inspected: `routing_contract.py` (498 lines), `workflow_runner.py` (1046 → 1180), `workflow_schema.py`,
`execution_ledger.py`, `execution_contract.py`, `process_observation.py`,
`WORKFLOWS/MULTIROUTING_SLICE_V1.json`, `test_routing_contract.py`, `test_multirouting_slice.py`,
`multirouting_evidence.py`, `UI_PROTOTYPE/aaw-canvas.html` + `DESIGN-NOTE.md`.
Baseline before any change: **107 passed**.

### 0.1 `next_brief` / `carry_forward` / `artifacts` are first-class forwarded data — **PASS**

| Stage | Where |
|---|---|
| declared | `workflow_runner.result_schema()` — optional, additive |
| validated | `workflow_schema.validate_node_result()` — typed, non-empty `next_brief` |
| persisted | the node result artifact, verbatim |
| on the wire | `NODE_COMPLETED` payload carries all three |
| forwarded | `node_package()` → `INHERITED_BRIEF`, `CARRY_FORWARD`, `UPSTREAM_ARTIFACTS` |
| in lineage | `lineage.inherited_brief`, `lineage.carry_forward` |

No correction needed.

### 0.2 `carry_forward` is not conflated with acceptance criteria — **FAIL → corrected in V0.1.1**

`mint_branch_node` did:

```python
if carry:
    node["acceptance"] = list(node.get("acceptance") or []) + [
        f"carry_forward respected: {item}" for item in carry]
```

This is a real conflation of two different questions. `acceptance` is a **node** fact — what this
node must satisfy to be accepted. `carry_forward` is a **path** fact — a constraint inherited from
upstream that must survive downstream. Three concrete consequences:

1. Neither the runtime nor a UX could separate them except by matching the prose prefix
   `"carry_forward respected: "` — the one technique the rest of the contract refuses everywhere.
2. `node_package["ACCEPTANCE_CRITERIA"]` feeds `input_contract_hash` on the execution descriptor,
   so a path constraint silently altered the branch's execution-identity input *as if the node's
   acceptance criteria had been edited*.
3. `test_routing_contract.py:227` asserted the conflation, freezing it in.

**Correction.** The template's `acceptance` is now inherited verbatim; the constraints keep their
own first-class field `node["carry_forward"]`, mirrored in `lineage.carry_forward` and surfaced as
`node_package["INHERITED_CARRY_FORWARD"]`. The model still sees them — they go into `instructions`
under their own labelled `CARRY_FORWARD` heading, next to `INHERITED_BRIEF`, explicitly stating they
are not acceptance criteria. Proved by `test_carry_forward_is_not_folded_into_acceptance_criteria`
and, on the real runner, by `test_repair_lineage_is_visible_the_moment_it_is_minted`.

### 0.3 Repair descendants expose inherited input for runtime *and* UX inspection — **PARTIAL → corrected**

Runtime side was complete. The UX side was not: a minted node existed **only** in the runner's
in-memory `by_id` table. `workflow_state.json` recorded `routing.lineage[branch_id]` but never the
minted node's type, instructions, acceptance or inherited constraints — so a consumer reading state
plus the journal saw a bare id in `frontier` and could not draw the branch until it completed.

**Correction.** `routing_contract.branch_projection(node)` returns the declarative projection of a
minted node; the runner persists it at `routing.minted_nodes[branch_id]` and includes it in the
`BRANCH_CREATED` payload. `graph_projection` now returns `minted_nodes`. The branch is drawable the
moment the gate creates it. Proved by `test_branch_projection_is_drawable_before_the_branch_runs`.

### 0.4 Other V0.1.1 items

| # | Change | Why |
|---|---|---|
| a | `HUMAN_DECISION_RESOLVED` journal event, emitted by `apply_human_verdict` | `HUMAN_DECISION_REQUIRED` opened a gate a stream consumer never saw close. The ledger's `HUMAN_DECISION_RECORDED` stays the lifecycle authority; the artifact stays the detailed one. |
| b | `RUN_CANCELLED` journal event | Cancellation abandons a frontier — a graph fact — and was the one runtime outcome with no structured trace at all. |
| c | `node_kind` (`DECLARED` / `MINTED_REPAIR_BRANCH`) | The canvas draws them differently and must not have to guess. |
| d | `lineage.node_type` | The minted node's own type, so a consumer needn't resolve the template to learn it. |
| e | `legacy_transitions` is true only when `on_pass`/`on_fail` is actually set | A terminal HUMAN_GATE that declares neither is not "legacy". |
| f | `atomic_json` retries `os.replace` | See §6 — a real defect the bridge exposed. |

**Validated multirouting semantics are otherwise untouched**: the gate, predicates, selection rules,
hold reasons, `NO_ROUTE`, dedup, guardrails and every hash are unchanged. All 33 original
`test_routing_contract` tests and all 11 `test_multirouting_slice` tests pass unmodified except the
one assertion that encoded the §0.2 defect.

---

## 1. Architecture decision

### 1.1 Two layers, because they answer different questions

| Layer | File | Responsibility |
|---|---|---|
| Boundary | `aaw_bridge.py` | The contract. Pure Python, transport-free, fully testable without a socket. |
| Transport | `aaw_bridge_server.py` | Loopback HTTP + SSE. Every route is a one-line translation of a bridge call. |

Splitting them means the boundary is testable in-process (45 of the 54 bridge tests run without a
socket; the other 9 exercise the transport itself),
and a different front end — the tkinter Control Center, a CLI — can use the same contract without
HTTP.

### 1.2 Transport: stdlib HTTP + SSE

Dependency inspection first. The project has **no third-party dependency of any kind**: no
`package.json`, no `requirements.txt`, no `pyproject.toml`, and every import across all modules
resolves to the standard library or to AAW itself. Existing UI surfaces are a 249 KB tkinter Control
Center and the new HTML canvas prototype.

| Option | Verdict |
|---|---|
| **`http.server.ThreadingHTTPServer` + SSE** | **chosen.** Already available. Serving the canvas same-origin removes CORS and `file://` entirely. SSE is a plain HTTP response, and what is being streamed is an append-only journal with a monotonic `sequence` — precisely the shape `id:` / `Last-Event-ID` resume was designed for. |
| WebSockets | needs a framing library the stdlib does not provide |
| Long polling | would reimplement SSE's resume by hand |
| Desktop IPC | cannot reach a browser at all |
| Port the canvas to tkinter | a large visual redesign, explicitly deferred |
| File watching from the browser | browsers cannot |

No new infrastructure, and resume comes free.

**Security posture, stated plainly:** `serve()` refuses any non-loopback host. Single user, no
authentication, no origin allowlist beyond same-origin serving. The repo and worktree a run may
touch are fixed when the server starts — a browser cannot name them. Static serving is confined to
`UI_PROTOTYPE/` with a resolved-path containment check. This is a local development boundary and
must not be exposed to a network; multi-user and remote deployment are deferred.

### 1.3 The UI never touches runner state

Everything the UI reads is a projection assembled from durable artifacts — `workflow_state.json`,
`routing_events.jsonl`, `<node>__GATE__decision.json`. No live runner object, no frontier list, no
`by_id` table crosses the boundary. A run observed through the bridge and a run observed by reading
those files by hand are the same run. `test_the_bridge_exposes_no_way_to_mutate_runner_state` pins
the public surface; `test_a_run_handle_never_leaks_the_thread_or_the_token` pins the handle.

---

## 2. Public bridge contract

`AawBridge(workflows_root=None, stats_root=None)`. Roots resolve late through `workflow_runner`, so
one answer to "where does a run live".

### BUILD

| Method | HTTP | Returns |
|---|---|---|
| `list_workflows()` | `GET /api/workflows` | every definition with `valid`, `semantic_hash`, `node_count`, `has_layout`; a broken file is **listed with its error**, not hidden |
| `load_workflow(id)` | `GET /api/workflow` | `definition` + `projection` + `semantic_hash` + `layout` in one read. Invalid → `valid: false`, `errors`, `projection: null` (an editor must be able to open a broken workflow) |
| `graph_projection(id)` | `GET /api/workflow/graph` | static graph only (closes gap **U2**) |
| `validate_candidate(c)` | `POST /api/workflow/validate` | `valid`, `errors[]`, `semantic_hash`, `stripped_visual_fields[]`. Never writes |
| `save_workflow(id, c, base_semantic_hash=)` | `POST /api/workflow/save` | atomic write, or refusal (closes gap **U4**) |
| `load_layout(id)` / `save_layout(id, l)` | `GET/POST /api/layout` | positions, from a separate store (closes gap **U3**) |

### RUN

| Method | HTTP | Returns |
|---|---|---|
| `start_run(id, goal=, repo=, worktree=, adapter=)` | `POST /api/run/start` | run handle **immediately**, before the first node finishes (closes gap **U1**) |
| `list_runs()` | `GET /api/runs` | every supervised run |
| `run_projection(run_id)` | `GET /api/run` | one full canvas frame: static graph + layout + runtime overlay + minted nodes |
| `events(run_id, since=, limit=)` | `GET /api/run/events` | events with `sequence > since` |
| — | `GET /api/run/stream` | SSE, resumable via `Last-Event-ID` or `?since=` |
| `cancel_run(run_id, reason=)` | `POST /api/run/cancel` | what the stop **actually did** (closes gap **U8**) |
| `resolve_human_decision(run_id, verdict)` | `POST /api/run/human` | pass-through to `apply_human_verdict` |
| `adopt_run(run_id, workflow_id=)` | — | register a run started elsewhere, read-only |
| `public_contract()` | `GET /api/contract` | versions and vocabularies, so a canvas can refuse a bridge it does not understand |

Refusal codes are a closed set the UX renders directly: `OK`, `SCHEMA_INVALID`, `STALE_BASE_HASH`,
`WORKFLOW_ID_MISMATCH`, `UNKNOWN_WORKFLOW`, `UNKNOWN_RUN`. Lifecycles: `PENDING` / `ACTIVE` /
`SETTLED`, with the runtime's own richer status passed through as `runner_status`.

`adapter=` substitutes the runner's documented LLM adapter boundary for one run in one process. It
cannot substitute routing, gates or the runner.

---

## 3. Layout outside workflow semantics

`workflow_layout.py`, stored at `WORKFLOWS/LAYOUTS/<workflow_id>.layout.json`, schema
`AAW_WORKFLOW_LAYOUT_V0.1`. Four independent guarantees:

1. **The module cannot reach a workflow file.** It imports no runner, holds no workflow writer, does
   not know what a workflow is, and every write goes through `layout_path()`, anchored to
   `<workflows_root>/LAYOUTS/`. Asserted structurally by
   `test_the_layout_module_cannot_reach_a_workflow_file`.
2. **The stored shape is a whitelist** of numbers (`x y w h`, viewport `x y k`) and booleans
   (`collapsed pinned`). A layout cannot carry instructions, edges or predicates, so a round trip
   through the store cannot smuggle graph meaning. Non-numeric coordinates are refused, not coerced.
   A workflow id is validated as an identifier before it becomes a path component.
3. **The semantic hash cannot see visual metadata.** `routing_contract.semantic_workflow()` is a
   *whitelist* projection over `SEMANTIC_WORKFLOW_FIELDS` / `SEMANTIC_NODE_FIELDS` /
   `SEMANTIC_EDGE_FIELDS`, so no coordinate, viewport or collapsed flag reaches the hash however it
   was smuggled in — including a top-level `layout` key or a per-node `_layout`.
4. **The write path strips visual keys from candidates** and reports which. A canvas naturally holds
   coordinates next to node data; without this, one careless round trip would persist `x`/`y` into
   the workflow file and change its identity for a reason with nothing to do with execution.

Consequence worth naming: a layout save can never make somebody's open edit stale, because the
stale check is over semantics (`test_a_layout_save_never_makes_an_open_edit_stale`).

A **runtime-minted** node's position is computed per frame from its origin and never stored — a run
must not write into a BUILD artifact. `auto_layout` gives a never-arranged graph deterministic
positions (longest-path depth → column, declaration order → lane, repair templates below the flow so
they do not read as the next task), and a stored layout is **merged over** it, so a partial layout
leaves one node in a default place instead of collapsing every unlisted node onto the origin.

---

## 4. Event contract

The existing routing journal vocabulary, reused unchanged, plus the two additive events from §0.4.
Objective name → implementation name:

| Objective | Implementation | Note |
|---|---|---|
| `NODE_STARTED` `NODE_COMPLETED` `NODE_FAILED` | same | |
| `GATE_EVALUATED` `EDGE_SELECTED` `EDGE_HELD` | same | |
| `BRANCH_CREATED` | same | now carries the drawable `node` projection |
| `HUMAN_DECISION_REQUIRED` | same | |
| `HUMAN_DECISION_RESOLVED` | same (**V0.1.1, new**) | pairs with the ledger's `HUMAN_DECISION_RECORDED` |
| — | `ROUTE_UNRESOLVED` | pre-existing; status token stays `NO_ROUTE` |
| — | `RUN_CANCELLED` (**V0.1.1, new**) | |

Every event carries stable identity: `run_id`, `node_id`, `event_id` (`REV_<hex>`), monotonic
`sequence`, `event_type`, `occurred_at`, `payload`, plus `lineage` where applicable. Branch/lineage
identity is the node id itself — a minted branch is a distinct id (`N03A`, `N03B`, …) with its own
artifacts — and `lineage` names its origin, template, edge and index.

**Reconnect.** `sequence` is per-run and monotonic. `events(since=N)` returns exactly the tail after
`N`; the SSE stream honours `Last-Event-ID` identically. A consumer that stores the last sequence it
rendered resumes from there and never replays its history. Verified over the real socket: a client
consumed ids 1–6, disconnected, reconnected with `Last-Event-ID: 6`, received 7–22, no overlap,
union contiguous.

**`execution_ledger` was not turned into event sourcing.** Its module docstring explicitly refuses to
be "event sourcing, not execution authority, and not a place from which workflow state is
reconstructed". Graph facts therefore stay in the routing journal, and the ledger keeps only process
lifecycle. The one place they meet is by design: `HUMAN_DECISION_RESOLVED` (graph) alongside
`HUMAN_DECISION_RECORDED` (lifecycle), both pointing at the same immutable decision artifact.

---

## 5. Safe workflow writes

```
candidate → strip visual keys → workflow_schema.validate_workflow
          → routing_contract.compile_edges per node
          → identity check → stale-hash check → atomic write
```

The validator is **the same function the runner loads a workflow through**, not a UI-side
approximation, so a candidate that validates here cannot fail to load later for a schema or routing
reason (`test_a_written_workflow_still_loads_through_the_runner`).

Refusals, all before any byte is written — 11 invalid edits are each proved to leave the file
byte-identical:

| Edit | Code |
|---|---|
| edge to a node that is not there | `SCHEMA_INVALID` |
| predicate outside the closed set of four | `SCHEMA_INVALID` |
| REPAIR edge targeting a non-template | `SCHEMA_INVALID` |
| cycle in a routing-contract graph | `SCHEMA_INVALID` |
| duplicate `edge_id`, unsupported edge field, unknown routing mode | `SCHEMA_INVALID` |
| unreachable node, missing HUMAN_GATE | `SCHEMA_INVALID` |
| goal baked into the file, `main_merge_allowed: true` | `SCHEMA_INVALID` |
| candidate declares a different `workflow_id` | `WORKFLOW_ID_MISMATCH` |
| the on-disk semantics moved under the edit | `STALE_BASE_HASH` |

Stale protection uses the project's existing canonical hashing (`execution_contract.canonical_hash`
via `routing_contract.semantic_hash`), so "the file changed" means "its executable semantics
changed". A semantically identical candidate writes nothing and says so (`SEMANTICALLY_IDENTICAL`).

---

## 6. Cancellation

`run_cancellation.py`. One flag per run plus the set of child processes that run is waiting on. No
workflow logic, no routing logic. The token travels in a `ContextVar`, mirroring the established
`process_observation._OBSERVER` idiom — so the documented adapter boundary
(`execute_llm_node`) keeps its exact signature and every existing substitute of it still works.

Three boundaries:

1. **Before a node is entered** — `run_cancellation.check("frontier_head")`. A Stop never starts new
   work.
2. **Before a dispatch spawns** — refuses to spawn once cancellation has landed, closing the window
   where a Stop could be followed by a brand-new untracked child.
3. **During the wait on a child** — the child is registered on the token, and `request()` terminates
   it. `terminate()` → wait 5 s → `kill()`. Killing the child ends the wait, which is what makes the
   stop immediate rather than eventual.

`request()` is idempotent and returns what it actually did, including each pid signalled, so the UX
shows a stop that happened rather than one it hopes for. Cancellation lands as status `CANCELLED`
with `state["cancellation"]` naming the interrupted node, the abandoned frontier and the processes
signalled, plus a `RUN_CANCELLED` journal event.

### What cannot yet be interrupted immediately — stated, not glossed over

| | |
|---|---|
| A child process AAW spawned | **interrupted immediately.** `TerminateProcess` on Windows, `SIGTERM` on POSIX, escalated to kill. There is no cooperative shutdown handshake with provider CLIs because they offer none. |
| The frontier | **interrupted** at the next node boundary, and inside a child wait because killing the child ends it. |
| Provider-side work already dispatched | **NOT interrupted.** The remote turn may still complete and **may still bill**. Killing the local CLI ends AAW's knowledge of it, not the work. |
| A partially written worktree | **NOT rolled back.** A killed IMPLEMENT node may leave edits on the branch for a human to inspect. The runner never reverts, exactly as it never merges. |
| A `MACHINE_GATE` child's side effects | **NOT undone.** Whatever it wrote before dying stays. |
| An adopted run (started by another process) | **cannot be cancelled at all.** `cancel_run` refuses with "only its owner can stop it" rather than setting a flag that stops nothing. |

The two the UX must show a user — provider work that may still bill, and worktree edits left in
place — are returned in every `cancel_run` response and stored in
`state["cancellation"]["not_interrupted"]`, so the interface cannot quietly imply a clean stop.

### A real defect the bridge exposed

`os.replace` is atomic but on Windows is **refused** while any other handle on the destination is
open without `FILE_SHARE_DELETE` — which is what a plain reader has. Before this bridge nothing read
`workflow_state.json` while a run was writing it, so one attempt was enough. A canvas polling the
run state made an authoritative state save fail with `PermissionError` and took the run down —
observed, and reproducible at 4 Hz polling. Fixed on both sides: `atomic_json` retries the replace
(bounded, 40 × 10 ms; atomicity preserved because each attempt either replaces the whole file or does
nothing, and exhaustion raises rather than silently losing a state write), and the bridge reads state
through an mtime-guarded cache so it holds the file open only as often as the data actually changes.

---

## 7. Files

| File | Lines | Change |
|---|---|---|
| `aaw_bridge.py` | 714 | **new**. The boundary. |
| `aaw_bridge_server.py` | 368 | **new**. Loopback HTTP/SSE transport. |
| `workflow_layout.py` | 247 | **new**. Layout store, strict shape, auto layout. |
| `run_cancellation.py` | 227 | **new**. Token, child registry, termination. |
| `aaw_llm_test_adapter.py` | 153 | **new**. Scripted substitute for the LLM adapter. |
| `UI_PROTOTYPE/aaw-canvas-live.html` | 850 | **new**. Bridge-driven canvas. |
| `test_aaw_bridge.py` | 990 | **new**, 54 tests: 45 over the boundary in-process, 9 over the real HTTP/SSE socket. |
| `ux_slice_evidence.py` | 646 | **new**. Runnable evidence + `--serve`. |
| `AAW_UX_RUNTIME_BRIDGE_V0_1.md` | 409 | **new**. This document. |
| `routing_contract.py` | 690 (was 498) | `+192`: V0.1.1 lineage correction, `branch_projection`, `semantic_workflow`/`semantic_hash`, `workflow_projection`, `node_kind`, `HUMAN_DECISION_RESOLVED`, `RUN_CANCELLED`, `minted_nodes` in `graph_projection`. |
| `workflow_runner.py` | 1301 (was 1180) | `+121`: `identifier`/`cancel` on `execute`, three cancel boundaries, `RunCancelled` handler, `minted_nodes`, `INHERITED_CARRY_FORWARD`, `HUMAN_DECISION_RESOLVED`, `CANCELLED` status, `atomic_json` retry. |
| `test_routing_contract.py` | 389 | 2 new tests; 1 assertion corrected (§0.2). |
| `UI_PROTOTYPE/DESIGN-NOTE.md` | — | status banner: the prototype is a design reference, not execution authority. |
| `workflow_schema.py` | 241 | **unchanged.** The write path reuses its validator verbatim; no new rule was needed. |

(The frozen V0.1 contract records `workflow_runner.py` at 1046 lines; the file measured 1180 before
this work began, so that figure was already stale. Deltas above are against the measured file.)

### The prototype's simulation is no longer execution authority

`UI_PROTOTYPE/aaw-canvas.html` keeps its `plannedVerdict="REPAIR"` timeline and is retained **as a
design reference only** — it is not served, not reachable from the bridge, and drives nothing. The
runnable canvas is `aaw-canvas-live.html`, which contains no `plannedVerdict`, no simulated engine
and no scripted timeline: every node state comes from `NODE_STARTED`/`NODE_COMPLETED`, every verdict
from `NODE_COMPLETED.verdict`, every edge state from `EDGE_SELECTED`/`EDGE_HELD`, and the repair
branch from `BRANCH_CREATED`.

---

## 8. Acceptance

Suite: **163 passed, 0 failed** (`test_aaw_bridge` 54 new — 45 boundary + 9 transport;
`test_routing_contract` 35; `test_multirouting_slice` 11; plus `test_execution_contract`,
`test_execution_ledger`, `test_execution_ledger_integration`, `test_workflow_execution_identity`,
`test_single_task_execution_identity`, `test_local_preprocess` — all unchanged).
Baseline before this work: 107 passed.

| # | Criterion | Evidence |
|---|---|---|
| 1 | BUILD graph loaded from real AAW workflow data | `test_build_graph_comes_from_the_real_workflow_file` — `definition` is byte-equal to `WORKFLOWS/MULTIROUTING_SLICE_V1.json` and `semantic_hash` equals `rc.semantic_hash` of it. Canvas screenshot shows all 8 nodes and 11 declared edges with predicates and labels. |
| 2 | Layout persistence does not modify semantics/hash | `test_saving_a_layout_changes_no_workflow_byte`, `test_a_candidate_carrying_coordinates_is_stripped`, `test_the_semantic_hash_ignores_visual_metadata_by_construction`, `test_a_layout_cannot_carry_semantic_fields`, `test_the_layout_module_cannot_reach_a_workflow_file`. Evidence §2: bytes identical `True`, hash unchanged `True`, 17 visual keys stripped, post-strip hash equals the clean hash. |
| 3 | Invalid workflow edits cannot be committed | `test_every_invalid_edit_is_refused_before_any_write` × 11, `test_a_stale_edit_is_refused`, `test_a_candidate_declaring_another_identity_is_refused`. Evidence §3: every refusal leaves the file byte-identical; over HTTP, invalid → `400 SCHEMA_INVALID`, stale → `409`. |
| 4 | A real run can be started from the UX boundary | `test_start_run_returns_a_handle_before_the_run_finishes` — handle, journal path and event cursor exist while `lifecycle == ACTIVE`. `POST /api/run/start` → `200`, then a real `AAW_…` run id. |
| 5 | Runtime events update the canvas without log parsing | `test_events_carry_everything_the_canvas_needs`, `test_the_canvas_never_has_to_parse_prose` — each required visual is a named structured field. Canvas screenshots show N01 PASS, N02 running, then the full REPAIR frame, driven only by SSE frames. |
| 6 | REPAIR visibly materialises the runtime-minted lineage | `test_repair_lineage_is_visible_the_moment_it_is_minted` — `BRANCH_CREATED` precedes `NODE_STARTED` for `N03A` and carries a drawable node; `N03R` never entered; executed `N01 N02 N03 N03A N09`. Screenshot: template dashed and separate, minted branch amber with its own PASS badge, amber lineage edge `REPAIR · N03A`, inspector showing lineage, inherited brief, inherited carry_forward and the template's own acceptance. |
| 7 | selected/held edges come from routing evidence | `test_selected_and_held_edges_come_from_routing_evidence` — the four selected edge ids in order, `E_N03_PASS`/`E_N03_BLOCKED` held as `PREDICATE_NOT_MATCHED`, and the per-predicate `trace` (`expected: PASS`, `actual: REPAIR`) read from the durable gate-decision artifact. |
| 8 | Reconnect/resume from event sequence works | `test_a_consumer_resumes_from_its_last_rendered_sequence`, `test_resuming_in_pages_reconstructs_the_stream_exactly` at the boundary; `test_the_sse_stream_resumes_from_last_event_id` over the real socket — consume ids 1–6, disconnect, reconnect with `Last-Event-ID: 6`, receive 7–22, no overlap, contiguous union, and a cold consumer still gets all 22. Evidence §5: resume from seq 15 delivers 7 of 22. |
| 9 | Stop invokes a real cancellation mechanism | `test_stop_terminates_the_child_the_run_is_waiting_on` — a gate blocked on a 120 s subprocess with a 300 s timeout stops in **0.13 s** with the child's pid signalled `TERMINATE`; also `test_stop_between_nodes_abandons_the_frontier`, `test_a_token_refuses_to_spawn_once_cancellation_has_landed`, `test_the_token_reports_what_it_actually_did`. From the canvas Stop button: status `CANCELLED`, `stop_reason: cancelled at scripted_adapter:N01: Stop pressed on the canvas`, journal `seq 2 RUN_CANCELLED`. |
| 10 | Existing runtime and routing regression suites remain green | 163 passed. The pre-existing 107 are unchanged except the single assertion that encoded the §0.2 defect. |

### Reproducing

```
python ux_slice_evidence.py                          # sections 1-6, writes UX_SLICE_EVIDENCE/
python ux_slice_evidence.py --serve --node-delay 25  # canvas at http://127.0.0.1:8787, Stop clickable
python -m pytest -q test_aaw_bridge.py
```

---

## 9. Remaining UX/runtime gaps

Closed by this contract: **U1** (process boundary), **U2** (static projection), **U3** (layout
ownership — UI-authored, bridge-persisted, separate store), **U4** (validated write path),
**U8** (cancellation).

Still open, and deliberately so:

| # | Gap | Note |
|---|---|---|
| U5 | Planner ghost subgraph | no `PROPOSED` node/edge state exists. Deferred by the objective. |
| U6 | Explicit merge/rejoin of a repair branch | `N03A` still ends at the human gate. The canvas must not imply rejoin exists. Deferred. |
| U7 | Sub-node progress | `NODE_STARTED` → `NODE_COMPLETED` is still coarse. Would come from joining the ledger's `EXECUTION_STARTED` on `execution_id`. A join, not new events. |
| B1 | **Adapter substitution is process-wide while held.** `start_run(adapter=)` swaps a module attribute, so two concurrent adapter-backed runs in one process would interfere. Real (non-adapter) runs are unaffected. Fix: pass the adapter through a `ContextVar` like the cancellation token. |
| B2 | **No BUILD topology editing in the live canvas.** It edits instructions, drags nodes and proves the refusal path; creating nodes, drawing edges and editing predicates are not wired. The bridge write path already supports them. |
| B3 | **No undo/redo**, in either layout or workflow editing. The prototype's design note already flags this as the most-touched part of the product. |
| B4 | **`accumulate_carry_forward` is global, not per-lineage.** It unions across every result in the run, not the path that reached the node. Correct while there is no join; wrong the moment fan-out branches carry conflicting constraints. |
| B5 | **A killed run's worktree is not reconciled.** Cancellation leaves edits in place by design, but nothing surfaces "this branch holds partial work from a cancelled node" to the UX. |
| B6 | **SSE polls the journal at 4 Hz.** Adequate for one local user; a file-change watch would be the next step, not a new transport. |
| B7 | **No authentication, single user, loopback only.** By design for V0.1; a prerequisite for anything remote. |
| B8 | **`limits` are invisible until violated**, as in the prototype. A run can still stop at `WORKFLOW_LIMIT_REACHED` with no prior canvas warning. |
| B9 | **`save_layout` accepts a workflow id that does not exist**, creating `LAYOUTS/<id>.layout.json` with `workflow_semantic_hash: null`. Deliberate — a layout for a workflow that will not load is still the user's layout — but it means a client can create bounded files in `LAYOUTS/`. Contained by the id regex (no traversal) and the whitelist shape (no semantics), and by loopback-only binding; still an unbounded-write surface worth closing when the bridge stops being single-user. |
| B10 | **No workflow create/delete in the boundary.** `save_workflow(allow_create=True)` exists and works but is not exposed over HTTP, and there is no delete at all. Intentional for V0.1: the canvas edits workflows, it does not manage a library. |
