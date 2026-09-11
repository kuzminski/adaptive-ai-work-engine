# AAW CANVAS FUNCTIONALIZATION V0.1

Status: **FROZEN**. Contract id: `AAW_CANVAS_FUNCTIONALIZATION_V0.1`.
Depends on `AAW_UX_RUNTIME_BRIDGE_V0.1` and `AAW_MULTIROUTING_RUNTIME_CONTRACT_V0.1` (+ V0.1.1).

Objective: turn the live canvas into the smallest genuinely usable day-to-day workflow
authoring and execution interface. Not a redesign — the existing canvas-first UX is
preserved except where a functional requirement forced a change.

```
UI  ──HTTP/SSE──▶  aaw_bridge_server  ──▶  aaw_bridge  ──▶  workflow_runner   (execution, resume)
   (aaw-canvas-live.html)               (the boundary)  ──▶  workflow_schema  (validation + attribution)
                                                        ──▶  routing_contract (projection, gate, cone, journal)
                                                        ──▶  workflow_layout  (visual, separate store)
                                                        ──▶  run_cancellation (real stop)
```

---

## 0. Substrate audit

Inspected before any change: `aaw_bridge.py` (714), `aaw_bridge_server.py` (368),
`workflow_runner.py` (1301), `routing_contract.py` (690), `workflow_schema.py` (241),
`workflow_layout.py` (247), `run_cancellation.py` (227), `aaw_llm_test_adapter.py` (153),
`ux_slice_evidence.py` (646), `UI_PROTOTYPE/aaw-canvas-live.html` (852),
`UI_PROTOTYPE/aaw-canvas.html` (simulated), `WORKFLOWS/MULTIROUTING_SLICE_V1.json`.
Baseline suite before this work: **163 passed**.

| Claim of the previous contract | Verdict |
|---|---|
| The bridge write path already supports node/edge authoring (gap **B2**) | **True.** `save_workflow` validates through the runner's own `validate_workflow` and writes atomically. Nothing new was needed in the write *rules*; what was missing was error **attribution** and workflow **creation** over HTTP. |
| Layout is outside semantics | **True and unchanged.** `semantic_workflow` is a whitelist projection; `strip_visual_fields` reports what it removed. Re-verified against a canvas that really does post coordinates. |
| Cancellation is real | **True**, but the UX had no way to learn what the stop *left behind* (gap **B5**). Closed here. |
| Adapter substitution is process-wide (gap **B1**) | **True and now fixed.** See §6. |
| `aaw-canvas.html` "is not served, not reachable" | **Was false in one respect.** It lived inside `UI_PROTOTYPE/`, the directory `_serve_static` serves, and was reachable at `/ui/aaw-canvas.html`. Corrected in §1. |

Two defects found during this work, both fixed and both regression-tested:

1. **Stale projection between edit and validation.** The canvas drew from the *last validated*
   projection while an edit was in flight, so a node created in the previous 180 ms was not
   in the drawn graph — the inspector could not find it and closed itself. Fixed with an
   explicit `graphStale` flag: while an edit is unanswered the canvas draws the draft itself.
2. **A stale async inspector could bind an edit to the wrong node.** `openInspector` awaits
   `/api/run/node`; a second selection starting mid-flight let the slower response win the
   panel while `wireNodeEditor` had bound the *previous* node's id to the *newer* node's
   fields. Fixed with a monotonic render token. This is a real data-integrity bug — it would
   have written one node's brief onto another — and it was found by driving the real canvas.

---

## 1. Establishing authority

`UI_PROTOTYPE/aaw-canvas.html` → `DESIGN_REFERENCE/aaw-canvas.simulated.html`.

Moved **out of `UI_PROTOTYPE/`**, which is the only directory `aaw_bridge_server._serve_static`
will serve and which it enforces with a resolved-path containment check. The file keeps its
`plannedVerdict` / `engine()` / `spawnRepair()` simulation, because the eight UX decisions in
`DESIGN-NOTE.md` were derived from it and destroying that history would cost more than it saves;
it gains a header banner and a fixed on-page banner naming it non-executable.

Asserted structurally, not by convention: `test_the_bridge_cannot_serve_the_archived_simulation`
checks the archive is outside `UI_ROOT` **and** that no HTML file anywhere under `UI_ROOT`
contains `plannedVerdict`. `test_the_active_frontend_contains_no_simulated_execution` additionally
refuses `spawnRepair`, `function engine(` and `scriptedTimeline` in the live canvas, and requires
that every runtime state it draws is keyed off a real journal event name.

---

## 2. The BUILD interaction contract

The canvas holds a **draft**. The draft is never authority: it is validated by the bridge —
which calls the same `workflow_schema.validate_workflow` the runner loads a workflow through —
and only an accepted candidate is written. An invalid draft stays fully editable and the last
valid saved workflow is untouched.

| Interaction | Gesture | What it does to the draft |
|---|---|---|
| create workflow | `New` → inline id field → ⏎ | `POST /api/workflow/create` with `blank_workflow` — one `HUMAN_GATE`, because the schema requires one |
| create node | palette click · double-click empty canvas · **drag an out-port into empty space** | appends a node with exactly the fields its type requires |
| edit node | inspector: type, run_if, role, model, effort, command, timeout, instructions, acceptance, depends_on | field-level, debounced re-validation |
| create edge | **drag an out-port onto another node** | `kind` follows the target's type (a `REPAIR`-typed target is a template and may only be reached by a `REPAIR` edge) |
| edit edge | inspector: kind, target, predicate + value, label, the source node's routing mode | predicate is a picker over the closed four-key set, never free text |
| insert into edge | edge inspector → `Insert node into this edge` → type | retargets the edge to the new node and adds one unconditional edge onward |
| delete node | `Delete` key · inspector button | also prunes every edge pointing at it and every `depends_on` naming it |
| delete edge | `Delete` key on a selected wire · inspector button | |
| move node | drag | `POST /api/layout/save` only — never the workflow file |
| set start | inspector, or the one forced case in §2.2 | |
| validate | continuous, debounced 180 ms | `POST /api/workflow/validate` |
| save | `Save` / ⌘S | `POST /api/workflow/save` with `base_semantic_hash` |

### 2.1 What the canvas decides, and what it refuses to decide

Three mechanical rules, each a *satisfaction of a declared schema rule* rather than a routing
decision, and each stated in the code where it happens:

* **Edge kind follows target type.** `workflow_schema` refuses a `REPAIR` edge to a non-template;
  choosing the kind from the target removes a class of error the user cannot see coming.
* **Unconditional edges are ordered last.** `FIRST_MATCH` requires it, because an unconditional
  edge shadows everything after it. Reordering changes no predicate.
* **Deleting a node prunes references to it.** A dangling `to` is not an alternative state.

Three things the canvas deliberately does **not** decide:

* **`depends_on` is never derived from edges.** It is a precondition, not a wire; conflating
  them would make a fan-out edge silently create a barrier. It is an explicit multi-select.
* **A second unconditional edge is not "fixed" with an invented predicate.** The validator
  refuses it and the canvas marks the node; picking a predicate is the user's call.
* **The predicate set is not extended.** The four keys are the contract's; there is no DSL.

### 2.2 `start_node`

Moving the entry point of a graph is a semantic change, so it happens in exactly two cases:
there is no valid start node at all, or **this is the first node added to a blank workflow**,
whose only node is the terminal `HUMAN_GATE` the schema requires. That gate cannot be the entry
point of a graph that feeds into it and there is no other candidate, so the choice is forced
rather than guessed. Every other case is explicit, and the `unreachable nodes: [...]` diagnostic
carries a one-click `Make N0x the start node` repair on the node it is attributed to.

### 2.3 Contextual validation errors — attribution, never parsing

The objective asks for errors on the affected node or edge. The one technique this project
refuses everywhere is recovering that from message text, so the subject comes out of the
validator itself.

`workflow_schema` gains a `ContextVar` subject scope: the node loop and the edge loop declare
what they are validating, and every `_require` raised inside picks it up. `WorkflowValidationError`
gains `node_id` / `edge_id` and `as_diagnostic()`. **Messages, rules and the exception type are
unchanged**, so every existing caller and every existing assertion over `str(exc)` still holds;
`_subject_scope()` wraps one validation pass so nothing leaks into the caller's context.

`validate_candidate` returns `errors` (strings, as before) *and* `diagnostics`
(`{message, node_id, edge_id, source}`). `BridgeError` carries them; the HTTP layer returns them
on 400/409. The canvas paints the offending node red with the message glued beneath it, marks the
offending wire, and shows the same message in the inspector.

```
{"message": "E_N03_PASS: broken edge target 'NOWHERE'", "node_id": "N03",
 "edge_id": "E_N03_PASS", "source": "SCHEMA"}
```

---

## 3. RUN controls

### 3.1 Run from here — deterministic semantics

`Run from here` is **not** a new traversal rule. It is one existing run, minus the downstream
cone of one node, re-entered at that node:

```
routing_contract.downstream_cone(workflow, node_id, minted=…)
    = transitive reachability over the node's COMPILED edges
      (the same edge list `evaluate_gate` routes on)
    ∪ every runtime-minted branch whose lineage origin is in the cone
```

The cone includes `node_id` itself — re-running a node replaces its own result. A minted branch
is owned by the node that minted it, not by the template it was minted from. Terminals are
reported separately, because `STOP` and `HUMAN_REQUIRED` are not nodes.

`workflow_runner.plan_resume(workflow, source_state, from_node, worktree=)` is pure and total:
it returns a plan or raises `ResumeRefused` with a code. Everything the cone does not own is
inherited **verbatim** from the source run — which is what makes `depends_on` satisfiable at
all — and the plan is built *before a run id is minted*, so a refused resume leaves nothing
behind. `execute(resume=plan)` seeds `completed_nodes`, `node_results`, `minted_nodes` and
`lineage`, then starts the frontier at `from_node`. It is a **new run** with its own id,
artifacts and ledger; the source run is read, never reopened or mutated.

Closed refusal set, each a refusal rather than a guess:

| Code | Why |
|---|---|
| `RESUME_UNKNOWN_SOURCE_RUN` | no readable state |
| `RESUME_SOURCE_STILL_RUNNING` | a run whose frontier is still moving is not a stable basis |
| `RESUME_WORKFLOW_MISMATCH` | a different workflow |
| `RESUME_GRAPH_MOVED` | the semantics moved since the source run — **including** a source run that recorded no `workflow_semantic_hash`, because an unprovable basis is not a basis |
| `RESUME_TARGET_NOT_DECLARED` | re-entering a runtime-minted branch is branch re-execution, deferred |
| `RESUME_UNSATISFIED_DEPENDS_ON` | the inherited set does not cover the target's preconditions |
| `RESUME_WORKTREE_MISMATCH` | the inherited work is physically somewhere else |

Budget is **derived** from the inherited records — `llm_calls` counts inherited LLM nodes,
`repair_cycle` is their maximum — never copied from the source totals, so a resumed run cannot
launder its way past `limits` by resetting the nodes that spent them.

One additive journal event, `RUN_RESUMED`, emitted at sequence 1 before any node starts. Same
justification as V0.1.1's `RUN_CANCELLED`: a run that begins from a seeded frontier is a graph
fact, and it was the last runtime entry with no structured trace. Its payload carries the whole
inheritance, so a canvas subscribed to *this* run alone paints the upstream it did not execute
without reading another run's files.

`state["workflow_semantic_hash"]` is now recorded on every run. Additive.

### 3.2 Reset downstream — what is reset, stated exactly

**No durable artifact at all.**

| | |
|---|---|
| **Cleared** | the canvas's runtime overlay for the nodes in the cone, so the graph stops showing a verdict the next run is about to replace. UI state, and the only thing this clears. |
| **Retained** | worktree contents and every uncommitted edit in them; node result artifacts of the cleared nodes; execution descriptors and the execution ledger (append-only); this run's routing journal (append-only); runtime-minted repair lineage already in run state. |

`reset_downstream` returns `durable_changes: []` and the `retained` list verbatim, and the
canvas prints both. Rolling any of it back is destructive cleanup, which this iteration
explicitly does not implement, so the operation reports what it left alone rather than implying
a rollback that does not exist. `test_reset_downstream_resets_nothing_durable_and_says_so`
compares the whole state document, the journal bytes and the artifact listing before and after.

### 3.3 Run and Stop

`Run` is `POST /api/run/start`, unchanged. A run refuses to start while the draft is dirty —
a run executes the file, not the draft. `Stop` is `POST /api/run/cancel`, unchanged: it
terminates the child process the run is waiting on and reports each pid it signalled.

---

## 4. Cancellation truthfulness

`PARTIAL_WORK_PRESENT` is **measured**, not asserted. `bridge.run_worktree(run_id)` runs the
runner's own `changed_files` (`git status --porcelain`) in the run's own worktree and returns a
closed token: `PARTIAL_WORK_PRESENT` / `NO_PARTIAL_WORK` / `PARTIAL_WORK_UNKNOWN`, plus the file
list, HEAD, the workspace baseline HEAD, and `rollback_available: false`.

`cancel_run` probes it and returns it inline. The canvas raises a banner titled with the outcome
the bridge measured and the work state — never a hardcoded label, because *a run that never
started because the tree was already dirty is not a cancellation*. `Inspect what remains` opens
a panel listing the changed files, what was not undone, and the two `git` commands to look
yourself. There is no cleanup button; `rollback_available` is false because no rollback exists.

The worktree is recorded on the run handle at `start_run`, because a run refused by the workspace
guard writes no state and therefore has no `worktree` field to read — and that is precisely the
case the UX most needs to explain.

**A consequence worth naming, found by running the canvas:** the runner's `validate_workspace`
refuses to start a run in a dirty worktree. So partial work left by a cancelled run **blocks the
next run**, and without this banner the user saw only `SETTLED · 0 events`. The canvas now reads
`run.error` off the handle when a run settles with no events, and says so.

---

## 5. Branch behaviour

Multirouting semantics are untouched. The canvas displays selected edges (green), held edges
(dashed, with the hold reason on the label), `PASS` / `REPAIR` / `BLOCKED` badges, the REPAIR
template as a dashed non-task, the runtime-minted descendant in amber with its own lineage edge,
and the reviewer's `next_brief` as a card glued under the node that issued it.

`accumulate_carry_forward` was not touched.

> **Recorded, as the objective requires:** *path-scoped `carry_forward` is a prerequisite for
> future branch merge/rejoin.* `accumulate_carry_forward` unions across every result in the run,
> not the path that reached the node. Correct while there is no join; wrong the moment fan-out
> branches carry conflicting constraints. This is on the wire at
> `GET /api/contract` → `merge_prerequisite`, so a canvas cannot offer merge without seeing why
> it is absent. Branch merge/rejoin is **not** implemented and the canvas does not imply it.

---

## 6. Runtime hygiene — the process-wide adapter binding

Removed. `_substituted_adapter` rebound `workflow_runner.execute_llm_node`, a module attribute,
so a substitution held for one run was installed for every run in the process (gap **B1**).

Replaced with `workflow_runner.llm_adapter_scope`, a `ContextVar` mirroring the cancellation
token exactly — the smallest safe context-local mechanism, consistent with the established
`run_cancellation` / `process_observation` idiom. The call site is `current_llm_adapter()(…)`,
which returns the context-local adapter if one is in scope and otherwise the module's own
attribute, so monkeypatching `workflow_runner.execute_llm_node` — which
`test_multirouting_slice`, `test_workflow_execution_identity` and `test_aaw_bridge` all do —
keeps working unchanged. `start_run` enters the scope on the run's own thread.

Provider execution was not redesigned. `execute_llm_node` is byte-identical.

---

## 7. Files

| File | Change |
|---|---|
| `UI_PROTOTYPE/aaw-canvas-live.html` | 852 → **1835**. BUILD authoring, RUN controls, restructured inspector, partial-work banner. |
| `DESIGN_REFERENCE/aaw-canvas.simulated.html` | **moved** from `UI_PROTOTYPE/`, banner added. Non-executable design reference. |
| `test_canvas_functionalization.py` | **new**, 793 lines, 30 tests. |
| `AAW_CANVAS_FUNCTIONALIZATION_V0_1.md` | **new**. This document. |
| `aaw_bridge.py` | 714 → 1062. `create_workflow`, `blank_workflow`, `plan_resume`, `reset_downstream`, `run_worktree`, `node_detail`, diagnostics, worktree on the handle, `_substituted_adapter` removed. |
| `aaw_bridge_server.py` | 368 → 399. Six routes, diagnostics on refusals. |
| `workflow_runner.py` | 1301 → 1541. `plan_resume` + `ResumeRefused` + refusal codes, `_seed_from_resume`, `execute(resume=)`, `workflow_semantic_hash` in state, `llm_adapter_scope` / `current_llm_adapter`. |
| `routing_contract.py` | 690 → 774. `downstream_cone`, `upstream_of_cone`, `RUN_RESUMED`. |
| `workflow_schema.py` | 241 → 315. Subject attribution. **No rule changed.** |
| `aaw_llm_test_adapter.py` | opt-in `"*"` fallback entry; refusing an unknown node remains the default. |
| `ux_slice_evidence.py` | `--sandbox-workflows`. |
| `UI_PROTOTYPE/DESIGN-NOTE.md` | status banner updated; decision 7 recorded as delivered. |
| `workflow_layout.py`, `run_cancellation.py`, `execution_*.py` | **unchanged.** |

---

## 8. Acceptance

Suite: **193 passed, 0 failed** (`test_canvas_functionalization` 30 new, plus the 163 pre-existing,
of which only two surface-pinning assertions in `test_aaw_bridge` were widened to name the new
read-only bridge methods and the new `resumed_from` handle field).

| # | Criterion | Evidence |
|---|---|---|
| 1 | authored entirely from the canvas | `test_a_four_node_workflow_is_authored_through_the_bridge_alone` — starts from an empty workflow directory. Live: `TRIAL_FOUR_NODE_V1` built in 22 gestures from an empty canvas. |
| 2 | no manual JSON editing | same test: every mutation is a bridge call. |
| 3 | node creation / deletion | `test_deleting_a_node_takes_every_edge_that_pointed_at_it`. Live: palette, double-click, port-drag-to-empty; `Delete` key. |
| 4 | edge creation / deletion | `test_an_edge_can_be_created_and_deleted_through_the_write_path`. Live: port-drag onto a node; edge inspector. Insert-into-edge verified live on `E_N01_CONTINUE` → new `N07` → `E_N07_N02`. |
| 5 | brief editing persists | `test_an_edited_brief_survives_the_validated_write`. Live: four distinct briefs written and reread from disk. |
| 6 | layout separate from semantics | `test_authoring_never_writes_a_coordinate_into_the_workflow` — 5 nodes' coordinates stripped, hash identical, workflow bytes identical across a layout save. The authored file on disk contains **no** visual key. |
| 7 | invalid edits cannot corrupt | `test_an_invalid_draft_never_replaces_the_last_valid_workflow` (byte-identical after refusal, then the fixed draft writes), `test_every_refusal_names_the_node_or_edge_that_caused_it` × 5. |
| 8 | Run uses the real bridge/runtime | `test_the_authored_workflow_runs_on_the_real_runner`. Live: `AAW_20260911_010256_682040ef`, 19 events, `WAITING_FOR_HUMAN`. |
| 9 | Run from here is deterministic and tested | `test_run_from_here_re_executes_exactly_the_downstream_cone`, `test_the_downstream_cone_is_the_declared_reachability_of_a_node`, `test_run_from_here_refuses_every_unsound_basis`, `test_a_run_that_never_recorded_a_semantic_hash_is_not_a_basis`, `test_a_source_run_that_is_still_moving_is_refused`. Live: resume at `N04` inherited `N02,N03`, executed `N04,N05,N01`. |
| 10 | Stop is real cancellation | `test_aaw_bridge::test_stop_terminates_the_child_the_run_is_waiting_on` (unchanged, 0.13 s). Live: `RUN_CANCELLED` at seq 2, `@scripted_adapter:N02`. |
| 11 | cancellation warns about partial work | `test_cancellation_reports_measured_partial_work`, `test_a_clean_stop_is_reported_as_clean`, `test_partial_work_left_by_one_run_is_reported_on_the_run_it_blocks`. Live banner: `RUNNER_ERROR · PARTIAL_WORK_PRESENT`, 1 file named, inspect panel listing it. |
| 12 | minted REPAIR lineage appears | `test_reset_downstream_owns_the_branch_its_origin_minted`. |
| 13 | inspector consumes structured data | `test_the_inspector_payload_is_assembled_from_artifacts_not_prose` — every PRIMARY and SECONDARY field is a named field of run state, a result artifact, a telemetry row or a gate-decision artifact. |
| 14 | regression suites green | 193 passed; the 163 pre-existing unchanged but for the two surface pins. |
| 15 | no simulated execution authority | `test_the_active_frontend_contains_no_simulated_execution`, `test_the_canvas_only_calls_endpoints_the_transport_serves`, `test_the_bridge_cannot_serve_the_archived_simulation`. |

### Reproducing

```
python -m pytest -q test_canvas_functionalization.py
python ux_slice_evidence.py --serve --sandbox-workflows --node-delay 0.35
```

---

## 9. Remaining gaps, ranked by observed friction

Ranked by what actually cost time while building a four-node workflow on the real canvas.

| Rank | Gap | Evidence |
|---|---|---|
| 1 | **A dirty worktree blocks the next run and there is no supported way to clear it from the canvas.** The banner now explains it, but the only remedy is a shell. | Observed: `RUNNER_ERROR · execution worktree has unexpected dirty state`, twice, during the trial. |
| 2 | **No undo.** Delete is one keystroke and the only recovery is reopening from disk, which discards every unsaved edit at once. | Structural; gap **B3** of the previous contract, still open. |
| 3 | **A second unconditional edge on a `FIRST_MATCH` node is a dead end until the user finds the predicate picker.** Correct, and correctly refused, but the repair is two clicks away in a panel. | The `Make N0x the start node` pattern shows the fix; only the unreachable case has it today. |
| 4 | `depends_on` is a raw multi-select of node ids with no explanation on the canvas | deliberate (§2.1), but unexplained in the UI. |
| 5 | **U5** planner ghost subgraph | deferred by the objective. |
| 6 | **U6** branch merge/rejoin — blocked on path-scoped `carry_forward` (§5) | deferred. |
| 7 | **U7** sub-node progress; **B8** limits invisible until violated | unchanged. |
| 8 | **B6** SSE polls the journal at 4 Hz; **B7** loopback, single user, no auth | unchanged, by design for V0.1. |
| 9 | **B9** `save_layout` accepts an unknown workflow id; **B10** no workflow *delete* | creation is now exposed; delete is still absent. |

Closed by this contract: **B1** (process-wide adapter), **B2** (BUILD topology editing),
**B5** (cancelled worktree not surfaced), and the creation half of **B10**.
