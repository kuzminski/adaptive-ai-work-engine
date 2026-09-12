# AAW PLANNER PROPOSAL PIPELINE V0.1 — frozen contract

Status: implemented and frozen. This document describes what exists, not what
is intended. Everything named "deferred" here is absent from the code.

```
existing workflow + selected anchor node + operator instruction
                            │
                            ▼
                  planning_package()            bounded, inspectable, hashed
                            │
                            ▼
                  aaw_planner.invoke_planner()  the provider seam
                            │
                            ▼
                  raw planner output            UNTRUSTED structured data
                            │
                            ▼
                  normalize_proposal()          fills only what AAW owns
                            │
                            ▼
                  validate_proposal()           deterministic, fail-closed
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
       PROPOSAL_READY              PROPOSAL_INVALID
              │
     operator decides
       ┌──────┴──────┐
       ▼             ▼
    Accept         Reject
       │             └─▶ proposal discarded; workflow untouched
       ▼
  revalidate → stale check → apply atomically → existing validated save path
```

## 1. Authority boundary

The planner is a **proposal generator**. It has no execution authority.

| It may                                        | It may not                                           |
|-----------------------------------------------|------------------------------------------------------|
| read a bounded slice of one workflow          | execute a node, or reach a runner, run or worktree   |
| return a structured proposed subgraph         | change runtime state or select a routing outcome     |
| state assumptions and warnings                | produce a verdict, a result, or an execution id      |
| propose node type, role, effort, brief, edges | name a model, a command, a path, or any code         |
| propose MERGE policy and expected_incoming    | modify an existing node                              |
| ask that one anchor edge be detached          | bypass `workflow_schema.validate_workflow`           |

Enforcement is structural, not advisory:

* `planner_proposal` is a pure module. It imports no runner, opens no file and
  reaches no process. `apply_proposal` deep-copies its input and returns a new
  document.
* `aaw_bridge.plan_from_node` cannot reach `save_workflow`, `atomic_json`,
  `start_run` or `self.runner`. A test asserts that by reading its source.
* `accept_proposal` is the only planner method that can change anything, and
  the only way it persists is `save_workflow` — the same validated,
  stale-checked, atomic write every other canvas edit already takes.
* A planner invocation is dispatched read-only, in a throwaway temporary
  directory, with the harness's non-writing mode (`--sandbox read-only` for
  codex, `--permission-mode plan` for claude). It has no worktree.

## 2. Proposal schema

```jsonc
{
  "proposal_version": "0.1",
  "proposal_id": "PROP-<first 16 hex of proposal_hash>",   // derived, not random
  "workflow_id": "PLANNER_SLICE_V1",
  "anchor_node_id": "N01",
  "base_semantic_hash": "<rc.semantic_hash of the workflow it was generated against>",
  "intent": "one sentence",
  "nodes": [ /* proposed nodes, see below */ ],
  "edges": [ /* proposed edges, see below */ ],
  "detach_edges": ["E_N01_CONTINUE"],       // anchor's own outgoing edges only
  "anchor_routing": "FIRST_MATCH" | "ALL_MATCHES" | null,
  "replacements": [],                        // reserved; MUST be empty in V0.1
  "assumptions": ["..."],
  "warnings": ["..."]
}
```

The first five fields are filled by AAW, never by the planner. A planner that
*contradicts* one of them is refused, not corrected.

### Proposed node

Closed key set — `PROPOSAL_NODE_FIELDS`:

```
id  type  role  capability  effort  instructions  acceptance
depends_on  run_if  merge_policy  expected_incoming
```

Absent on purpose, and refused if present:

| absent field                     | why |
|----------------------------------|-----|
| `model`                          | provider binding is not a planning decision |
| `command`, `timeout_seconds`     | a planner does not author an argv the runner spawns |
| `preprocess`                     | local-preprocessing policy is not planner-owned |
| `edges`                          | edges are declared once, at the top level, with an explicit source |
| `on_pass`, `on_fail`             | scaffolding AAW fills; V0.1 proposals are contract-mode only |

Closed value sets:

* `type` ∈ `IMPLEMENT, REVIEW, REPAIR, MERGE, HUMAN_GATE, FINAL_GATE`
  — **`MACHINE_GATE` is not proposable.** Its `command` is an argv the runner
  really spawns; a machine gate is added by a human on the canvas, where the
  command is visible before it is saved.
* `role` / `capability` ∈ `CODE_IMPLEMENTER, INDEPENDENT_REVIEWER,
  ARCHITECT_STRONG, RESEARCH_SYNTHESIZER`
* `effort` ∈ `low, medium, high, xhigh, max`
* `run_if` ∈ `ALWAYS, ON_TRANSITION`
* `merge_policy` ∈ `ALL_REQUIRED, ANY_COMPLETED` (MERGE only; and
  `merge_policy`/`expected_incoming` are refused on any other node type)
* `id` matches `^[A-Za-z][A-Za-z0-9_]{0,31}$`

### Proposed edge

Closed key set — `PROPOSAL_EDGE_FIELDS`: `edge_id from to when kind label`.

* `from` ∈ {the anchor} ∪ {ids this proposal creates}. Nothing else.
* `to` ∈ {existing node} ∪ {proposed node} ∪ {`STOP`, `HUMAN_REQUIRED`}.
* `kind` ∈ `CONTINUE, REPAIR, FALLBACK`.
* `when` keys ⊆ `routing_contract.PREDICATE_KEYS`. There is no expression DSL.

### Materialization

`apply_proposal` adds only the schema's own scaffolding: `depends_on`,
`run_if`, `on_pass: null`, `on_fail: null`, and the **inherited model** —
the anchor's `model` if the anchor is an LLM node that binds one, else the
first bound model in the graph, else nothing (and then `capability` carries the
binding, exactly as it does for a hand-authored node). Every filled field is
listed in the report's `materialization.filled_by_aaw`, so it is visible rather
than assumed.

## 3. Deterministic validation

`planner_proposal.validate_proposal(workflow, proposal, schema_validator=...)`.
Fail-closed, in this order, first failure wins:

1. **Envelope** — closed top-level key set, `proposal_version`, safe text,
   `replacements` empty, `anchor_routing` in the closed set, canonical size
   under `max_proposal_bytes`.
2. **Nodes** — count ≤ `max_nodes`; closed key set per node; required fields;
   id shape; duplicate id inside the proposal; collision with an existing id;
   closed type/role/capability/effort/run_if; `acceptance` array and item
   count; MERGE fields present only on a MERGE and mandatory there.
3. **Edges** — count ≤ `max_edges`; closed key set; required fields; duplicate
   edge id; collision with an existing edge id; `from` allowed; `to` resolvable;
   closed kind; predicate keys; `detach_edges` names only the anchor's own
   outgoing edges, count ≤ `max_detach_edges`.
4. **Non-empty** — a proposal must add at least one node or one edge.
5. **Apply** — `apply_proposal` on a deep copy.
6. **The real validator** — `aaw_bridge.validate_candidate`, i.e. the same
   `workflow_schema.validate_workflow` + `routing_contract.compile_edges` the
   runner loads a workflow through. Cycles, reachability, start-node rules,
   FIRST_MATCH ordering, the MERGE closed-incoming contract and REPAIR-edge
   targeting are decided **here and only here**. There is no second definition
   of a legal graph.
7. **Structural protection** — a measured diff of the two *semantic*
   projections: every workflow-level field identical; no node removed; no
   undeclared node added; every pre-existing node byte-identical except the
   anchor, whose `edges` set must equal `(was − detached) ∪ added-from-anchor`
   and whose surviving edges must be unmodified; `routing` may move only if
   `anchor_routing` was declared.

Text safety (§22 of the objective): every string is length-capped and refused
if it contains a C0/C1 control character, `DEL`, `U+2028` or `U+2029`. Prose is
**not** sanitised — it is stored verbatim — and rendering safety is escaping in
the canvas, which a test enforces over every `innerHTML` interpolation in the
planner section.

Refusal codes (`planner_proposal.REFUSAL_CODES`, closed set):

```
PROPOSAL_SHAPE  UNSUPPORTED_FIELD  UNSAFE_TEXT  SIZE_LIMIT  UNKNOWN_ANCHOR
NODE_ID  NODE_TYPE  COLLIDES_WITH_EXISTING  DUPLICATE_ID  EDGE_REFERENCE
EDGE_SOURCE_NOT_ALLOWED  DETACH_NOT_ALLOWED  PROTECTED_STRUCTURE
SCHEMA  ROUTING  BASE_WORKFLOW
```

Each refusal carries `{code, message, node_id, edge_id, source}`. Nothing is
recovered from message text.

### Limits

`planner_proposal.ProposalLimits`, each overridable by
`AAW_PLANNER_<FIELD>` in the environment (an unreadable override is ignored,
never guessed at):

| field | default |
|---|---|
| `max_nodes` | 8 |
| `max_edges` | 16 |
| `max_detach_edges` | 4 |
| `max_proposal_bytes` | 65536 |
| `max_package_bytes` | 49152 |
| `max_instruction_chars` | 2000 |
| `max_text_chars` | 4000 |
| `max_list_items` | 16 |
| `max_neighborhood_nodes` | 24 |

## 4. Planning package (bounded planner input)

`planning_package(workflow, anchor_node_id, instruction=...)` is a closed set
of keys:

```
package_version  proposal_version  workflow  base_semantic_hash  anchor
neighborhood  existing_node_ids  existing_edge_ids  constraints  rules
limits  operator_instruction  response_schema  package_bytes
```

`neighborhood` is the anchor, two hops downstream and the anchor's direct
predecessors, capped at `max_neighborhood_nodes`, and it states which node ids
it included. There is no repository content, no run, no journal, no telemetry
and no filesystem path in the package; a test walks every string and refuses
anything path-shaped.

`package_hash(package)` is the stable **input hash** recorded on
`PLANNER_STARTED`.

## 5. Proposal identity

`proposal_hash` = `routing_contract.canonical_hash` over
`canonical_proposal(proposal)` — a whitelist, so volatile and provenance fields
are *absent* rather than ignored:

* included: `proposal_version`, contract, `workflow_id`, `anchor_node_id`,
  `base_semantic_hash`, `intent`, nodes (sorted by id), edges (sorted by
  edge_id), `detach_edges` (sorted), `anchor_routing`, `replacements`,
  `assumptions`, `warnings`.
* excluded: `proposal_id`, `created_at`, `status`, `planner`, `input_hash`,
  `telemetry`, `request_id`, `validation`, `preview`, `materialization`,
  `resolved_at`, and every UI/viewport field.

`proposal_id = "PROP-" + proposal_hash[:16]`.

Equivalent proposal content hashes equivalently — including when a planner
emits the same mutation in a different order. **No claim is made that model
calls are deterministic**; the identity is of the artifact, not of the call.

## 6. Staleness

A proposal binds to `base_semantic_hash`, the semantic hash of the workflow it
was generated against. `is_stale(workflow, proposal)` is that hash compared to
the current one — so it is layout-blind, exactly like the save path's own stale
check.

On Accept, a stale proposal is refused with `PROPOSAL_STALE`, the proposal is
marked `PROPOSAL_STALE`, and the workflow is not touched. **V0.1 does not
attempt a semantic three-way merge**; the operator regenerates. Staleness is
re-measured on every `proposal` and `list_proposals` read, so a canvas that
re-attaches after a refresh is told the truth.

## 7. Accept

`accept_proposal(proposal_id, candidate=None, persist=False)`. Refusals, in
order, all before anything changes:

| # | check | refusal |
|---|---|---|
| 1 | already accepted or rejected | `PROPOSAL_ALREADY_RESOLVED` |
| 2 | not `PROPOSAL_READY` | `PROPOSAL_NOT_ACCEPTABLE` |
| 3 | a run of this workflow is ACTIVE | `PROPOSAL_RUN_ACTIVE` |
| 4 | the base no longer validates | `PLANNER_BASE_INVALID` |
| 5 | the base moved since generation | `PROPOSAL_STALE` |
| 6 | the proposal no longer validates | `PROPOSAL_INVALID` |
| 7 | the resulting graph does not validate | `PROPOSAL_INVALID` |

Atomicity: `apply_proposal` builds one whole new document from a deep copy, so
there is no state in which half a proposal has been applied. If persisting is
asked for and `save_workflow` refuses or the write fails, nothing was written
and the previous valid workflow is still the workflow.

`candidate` lets the canvas accept against the **unsaved draft** it is looking
at. With `persist=False` (what the canvas does) the bridge returns the mutated
candidate and writes nothing; the operator's existing **Save** is still the one
path to the file.

## 8. Reject

`reject_proposal(proposal_id, reason="")` discards the proposal, leaves the
workflow semantics and file untouched, reports the unchanged semantic hash and
`workflow_changed: false`, and emits `PROPOSAL_REJECTED`. Rejecting an already
accepted proposal is refused — it would not undo the mutation, and Undo does.

## 9. Undo / Redo

An accepted proposal is **one** authoring transaction. The canvas replaces its
draft with the candidate the bridge returned, folds the ghost positions into
the layout, and calls `touch()` exactly once. Because `draft_history.js` records
whole-state snapshots, one Undo removes the entire accepted subgraph and one
Redo brings all of it back — never one step per proposed node. No change to
`DraftHistory` was needed.

## 10. Event contract

`GET /api/planner/events?since=` and `AawBridge.planner_events(since=)`. The
journal uses the routing journal's own field names (`sequence`, `at`,
`event_type`) so one consumer reads both. Event types:

```
PLANNER_STARTED    request_id, workflow_id, anchor_node_id, input_hash,
                   base_semantic_hash, package_bytes, instruction_chars
PLANNER_COMPLETED  request_id, planner {harness, provider, profile, model,
                   effort, access_class, provider_session_id, wall_time_s,
                   adapter, telemetry_status, invocation}
PLANNER_FAILED     request_id, code, message
PROPOSAL_READY     proposal_id, proposal_hash, base_semantic_hash, counts
PROPOSAL_INVALID   proposal_id?, diagnostics[], (phase: ACCEPT when on accept)
PROPOSAL_STALE     proposal_id, base_semantic_hash, semantic_hash
PROPOSAL_ACCEPTED  proposal_id, previous_semantic_hash, semantic_hash,
                   added_node_ids, added_edge_ids, detached_edge_ids, persisted
PROPOSAL_REJECTED  proposal_id, proposal_hash, reason, semantic_hash
```

The journal is bounded (500 rows) and **in memory**. It is deliberately not
persisted: a proposal is BUILD-time authoring state, and writing a planner
journal into a run's evidence tree would put authoring history inside execution
evidence. Telemetry is not the UX — the canvas never displays this journal.

## 11. Provider boundary

`aaw_planner` reuses the existing machinery rather than creating a second LLM
architecture:

* `model_catalog.resolve_profile` decides which model may plan, under the
  catalog's own availability and `NO_EXTRA_PAID_USAGE` policy. Eligible
  profiles are those whose `suitable_for` contains `PLAN`
  (`ASTRA_*`, `OPUS_HIGH`, `SOL_HIGH`, `FABLE_HIGH`); the default is `SOL_HIGH`.
* `workflow_runner.harness_executable` finds the CLI;
  `workflow_runner.run_process` spawns it with the same cancellation and
  process-observation behaviour as every other AAW dispatch.
* The seam is a `ContextVar` (`planner_adapter_scope`), mirroring
  `workflow_runner.llm_adapter_scope`, so a substitute installed for one
  request reaches only that call stack and never the process.

A planner invocation is distinguishable from a node execution: telemetry
carries `invocation: "PLANNER_PROPOSAL"`, and it has no node, no execution id,
no ledger entry, no run and no worktree.

Failure codes: `PLANNER_NO_BINDING, PLANNER_UNAVAILABLE,
PLANNER_DISPATCH_FAILED, PLANNER_TIMEOUT, PLANNER_MALFORMED_OUTPUT`. A planner
that could not answer is reported as `PLANNER_FAILED`, never as a planner that
proposed nothing.

## 12. Wire surface

```
GET  /api/planner/status                              binding probe, calls nothing
GET  /api/planner/proposals?workflow_id=              open proposals + staleness
GET  /api/planner/proposal?proposal_id=               one proposal in full
GET  /api/planner/events?since=                       the planner journal
POST /api/planner/plan    {workflow_id, anchor_node_id, instruction?, candidate?}
POST /api/planner/accept  {proposal_id, candidate?, persist?}
POST /api/planner/reject  {proposal_id, reason?}
```

The planner adapter comes from the server (`--scripted-planner`), never from
the client — exactly as the client chooses a workflow but never a worktree.
`PROPOSAL_STALE`, `PROPOSAL_ALREADY_RESOLVED` and `PROPOSAL_RUN_ACTIVE` return
**409 Conflict**; every other refusal returns 400.

`public_contract()` gains `planner`, `planner_contract`, `planner_event_types`,
`proposal_statuses`, `proposal_codes`, `proposal_refusal_codes` and
`planner_failure_codes`. `"planner graph mutation"` has been removed from
`deferred`; `"planner Modify"` and `"automatic stale-proposal resolution"`
replace it.

## 13. UI — proposal state

The UI is frozen; these are the only additions.

* `Plan from here` plus a one-line planning-instruction input, inside the
  existing node inspector, BUILD mode only.
* One proposal strip (`#prop`) with the same geometry and weight as the
  existing partial-work banner: status, intent, counts, detached edges,
  proposal and base hashes, assumptions, warnings, planner identity, and
  **Accept** / **Reject**. Invalid proposals show their structured refusals.
* Ghost rendering: proposed nodes are **dotted** (no other node state is
  dotted), desaturated, hatched, badged `PROPOSED`, titled "proposed — not
  executable until accepted", and their wiring ports are not rendered at all.
  Proposed edges are dotted blue; an edge the proposal would detach is dotted
  red. No animation was added.
* The proposal lives in `S.proposal`, beside the draft and never inside it.
  Nothing in the canvas can move it into `S.draft` except `acceptProposal`,
  which does so once, through the bridge, as a single history entry.
* Entering RUN hides the strip and the ghosts.
* A browser refresh re-attaches an open, non-stale proposal from the bridge.

Not added, and still absent: planner dashboard, planner sidebar, prompt
console, permanent planner panel, new global navigation.

## 14. Interaction with runs and recovery

Planning is a read and is allowed at any time, including during a run.
**Accepting** is BUILD authoring and is refused with `PROPOSAL_RUN_ACTIVE`
while a run of that workflow is ACTIVE. The pipeline never touches partial-work
recovery, the adopted workspace baseline, runtime recovery or branch/merge
runtime state — it has no code path to any of them.

## 15. Known boundaries

* **Splicing into an existing MERGE is refused, not repaired.** Rerouting into
  a MERGE that already exists changes that MERGE's `expected_incoming`, which is
  existing structure. The refusal is the real schema's own message. Author the
  merge change by hand on the canvas first, then plan. Pinned by
  `test_splicing_into_an_existing_merge_is_refused_not_repaired`.
* **FIRST_MATCH ordering is not auto-corrected.** A proposal that would leave a
  node with an unconditional edge ahead of a conditional one is refused with the
  schema's own message rather than reordered — reordering planner output would
  be silent repair.
* Proposals are held in memory, bounded to 32 per bridge process. Restarting
  the bridge loses open proposals; the workflow is unaffected.

## 16. Explicitly deferred

Not implemented, and absent from the code:

* `Modify` — editing a proposal before acceptance
* `replacements` — rewriting an existing node's body (key reserved, must be empty)
* automatic resolution of a stale proposal; three-way semantic graph merge
* `MACHINE_GATE` proposals; planner-chosen model bindings
* automatic acceptance, planner-triggered execution, recursive or
  self-repairing planner loops, N-step autonomous planning
* multi-user proposal review; planner marketplace/templates; planner analytics

## 17. Where this lives

| file | role |
|---|---|
| `planner_proposal.py` | the contract: package, canonicalization, identity, validator, apply. Pure. |
| `aaw_planner.py` | the provider seam; binding via `model_catalog`; dispatch via `workflow_runner.run_process` |
| `aaw_planner_test_adapter.py` | scripted substitute for the seam, including failures and malformed output |
| `aaw_bridge.py` | `plan_from_node`, `proposal`, `list_proposals`, `accept_proposal`, `reject_proposal`, `planner_events`, `planner_status` |
| `aaw_bridge_server.py` | the seven `/api/planner/*` routes; `--scripted-planner` |
| `UI_PROTOTYPE/aaw-canvas-live.html` | `Plan from here`, the proposal strip, ghost rendering |
| `WORKFLOWS/PLANNER_SLICE_V1.json` | the committed slice fixture |
| `planner_slice_evidence.py` | runnable evidence and `--serve` for a browser walkthrough |
| `test_planner_proposal_pipeline.py` | 69 tests: unit, integration, slices, transport, UI |
